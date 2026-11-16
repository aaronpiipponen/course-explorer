"""Anonymous public Peppi legacy JSON client and the shared curriculum-tree walk.

This module owns the request contract of the legacy ``/api`` deployments and the one node walk
shared by the capture driver and the parser. It issues the requests whose responses the capture
driver records, and the family parser converts the records.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

from course_explorer.config import KoriSettings
from course_explorer.http_retry import decode_json_response, request_json_with_retries
from course_explorer.ingestion.source import CapturedResponse, SourceResponse


class PeppiLegacyRequestError(RuntimeError):
    """A public Peppi legacy request failed after its configured retry budget."""


class PeppiLegacyNotFound(PeppiLegacyRequestError):
    """A public Peppi legacy request returned HTTP 404 for an optional route."""


class PeppiLegacyContractError(ValueError):
    """A public Peppi legacy response violates the source contract."""


def walk_nodes(value: object) -> Iterator[dict[str, Any]]:
    """Yield every node of one Peppi curriculum tree in pre-order.

    A node's ``children`` list is walked in order. Any value that is neither a node object nor a
    node list raises :class:`PeppiLegacyContractError`.
    """
    if isinstance(value, list):
        for item in value:
            yield from walk_nodes(item)
        return
    if not isinstance(value, dict):
        raise PeppiLegacyContractError("curriculum tree node must be an object or a list")
    yield value
    children = value.get("children")
    if children is None:
        return
    if not isinstance(children, list):
        raise PeppiLegacyContractError("curriculum tree children must be a list")
    yield from walk_nodes(children)


def _classify_request_error(
    failure: int | type[httpx.TransportError],
) -> type[RuntimeError]:
    """Map an absent discovery route to the not-found error and every other failure to a request
    error."""
    if failure == 404:
        return PeppiLegacyNotFound
    return PeppiLegacyRequestError


class PeppiLegacySource:
    """One synchronous public Peppi legacy client with spaced, bounded-retry requests."""

    def __init__(
        self,
        settings: KoriSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        before_request: Callable[[], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._before_request = before_request
        self._last_request_start = float("-inf")
        self._client = httpx.Client(
            base_url=settings.university.peppi_legacy.api_base_url.rstrip("/") + "/",
            timeout=settings.kori_request_timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        """Close the shared HTTP client and release its connections."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _get(self, path: str, params: tuple[tuple[str, str], ...]) -> httpx.Response:
        """Start one request at least the configured interval after the previous request start."""
        wait = (
            self._last_request_start
            + self._settings.kori_request_interval_seconds
            - time.monotonic()
        )
        if wait > 0:
            self._sleep(wait)
        self._last_request_start = time.monotonic()
        return self._client.get(path.removeprefix("/"), params=list(params))

    def _request(self, path: str, params: tuple[tuple[str, str], ...] = ()) -> SourceResponse[Any]:
        """Fetch one required ``/api`` route and return its decoded value with its exact bytes."""
        if not path.startswith("/api/"):
            raise PeppiLegacyContractError("Peppi legacy request path must begin with /api/")
        frozen_params = tuple(params)

        def validate(response: httpx.Response) -> tuple[httpx.Response, Any]:
            return response, decode_json_response(
                response, error_prefix=f"GET {path}", contract_error=PeppiLegacyContractError
            )

        response, value = request_json_with_retries(
            request=lambda: self._get(path, frozen_params),
            validate=validate,
            max_attempts=self._settings.kori_max_attempts,
            retry_backoff_seconds=self._settings.kori_retry_backoff_seconds,
            sleep=self._sleep,
            error_prefix=f"GET {path}",
            request_error=PeppiLegacyRequestError,
            contract_error=PeppiLegacyContractError,
            retry_contract_errors=False,
            before_request=self._before_request,
            check_initial=False,
            classify_request_error=_classify_request_error,
        )
        return SourceResponse(
            captured=CapturedResponse(
                request_path=path,
                request_params=frozen_params,
                body=response.content,
            ),
            value=value,
        )

    def _optional_request(
        self, path: str, params: tuple[tuple[str, str], ...] = ()
    ) -> SourceResponse[Any] | None:
        """Fetch one ``/api`` route whose HTTP 404 means the discovery combination is absent."""
        try:
            return self._request(path, params)
        except PeppiLegacyNotFound:
            return None

    def get_navigation(self) -> SourceResponse[Any]:
        """Fetch the navigation entries every discovery variant walks."""
        return self._request("/api/navigation")

    def get_organisations(self, period: str) -> SourceResponse[Any]:
        """Fetch the top-level organisations of one curriculum period."""
        return self._request("/api/organisation", (("period", period),))

    def get_organisation_units(
        self, organisation_id: str, period: str
    ) -> SourceResponse[Any] | None:
        """Fetch one organisation's child units, or ``None`` when the organisation is absent."""
        return self._optional_request(
            f"/api/organisation/units/{quote(organisation_id, safe='')}",
            (("period", period),),
        )

    def get_education_types(self, organisation_id: str) -> SourceResponse[Any] | None:
        """Fetch one organisation's education types, or ``None`` when it has none."""
        return self._optional_request(f"/api/education-type/{quote(organisation_id, safe='')}")

    def get_educations(
        self, organisation_id: str, navigation_id: str, period: str
    ) -> SourceResponse[Any] | None:
        """Fetch one organisation's and navigation entry's educations, or ``None`` when absent."""
        return self._optional_request(
            f"/api/education/{quote(organisation_id, safe='')}/{quote(navigation_id, safe='')}",
            (("period", period),),
        )

    def get_educations_for_navigation(
        self, navigation_id: str, period: str
    ) -> SourceResponse[Any] | None:
        """Fetch one navigation entry's educations, or ``None`` when it has none."""
        return self._optional_request(
            f"/api/education/{quote(navigation_id, safe='')}/education-type",
            (("period", period),),
        )

    def get_educations_for_type(
        self, organisation_id: str, education_type_id: str, period: str
    ) -> SourceResponse[Any] | None:
        """Fetch one organisation's and education type's educations, or ``None`` when absent."""
        return self._optional_request(
            f"/api/education/{quote(organisation_id, safe='')}/{quote(education_type_id, safe='')}",
            (("period", period),),
        )

    def get_accomplishment_plan(self, programme_id: str, period: str) -> SourceResponse[Any]:
        """Fetch one programme's curriculum tree for one curriculum period."""
        return self._request(
            f"/api/accomplishment-plan/{quote(programme_id, safe='')}",
            (("period", period),),
        )

    def get_course(self, course_id: str, period: str) -> SourceResponse[Any]:
        """Fetch one course detail for one curriculum period."""
        return self._request(
            f"/api/course/{quote(course_id, safe='')}",
            (("period", period),),
        )
