"""Anonymous public Peppi Solenovo PSG JSON client for the plan and learning-unit routes.

This module owns the request contract of the ``/app/rest`` deployments. It issues the requests
whose responses the capture driver records, and the family parser converts the records.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

from course_explorer.config import KoriSettings
from course_explorer.http_retry import decode_json_response, request_json_with_retries
from course_explorer.ingestion.source import CapturedResponse, SourceResponse


class PeppiPsgRequestError(RuntimeError):
    """A public Peppi PSG request failed after its configured retry budget."""


class PeppiPsgContractError(ValueError):
    """A public Peppi PSG response violates the source contract."""


class PeppiPsgSource:
    """One synchronous public Peppi PSG client with spaced, bounded-retry requests."""

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
        block = settings.university.peppi_psg
        if block is None:
            raise ValueError("the configured university has no Peppi PSG source block")
        self._client = httpx.Client(
            base_url=block.api_base_url.rstrip("/") + "/",
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
        """Fetch one required ``/app/rest`` route and return its decoded value with its exact bytes."""
        if not path.startswith("/app/rest/"):
            raise PeppiPsgContractError("Peppi PSG request path must begin with /app/rest/")
        frozen_params = tuple(params)

        def validate(response: httpx.Response) -> tuple[httpx.Response, Any]:
            return response, decode_json_response(
                response, error_prefix=f"GET {path}", contract_error=PeppiPsgContractError
            )

        response, value = request_json_with_retries(
            request=lambda: self._get(path, frozen_params),
            validate=validate,
            max_attempts=self._settings.kori_max_attempts,
            retry_backoff_seconds=self._settings.kori_retry_backoff_seconds,
            sleep=self._sleep,
            error_prefix=f"GET {path}",
            request_error=PeppiPsgRequestError,
            contract_error=PeppiPsgContractError,
            retry_contract_errors=False,
            before_request=self._before_request,
            check_initial=False,
        )
        return SourceResponse(
            captured=CapturedResponse(
                request_path=path,
                request_params=frozen_params,
                body=response.content,
            ),
            value=value,
        )

    def get_years(self) -> SourceResponse[Any]:
        """Fetch the published academic years of the mapped education type space."""
        return self._request("/app/rest/accomplishmentplan/years", (("educationTypeId", ""),))

    def get_education_types(self, year: int) -> SourceResponse[Any]:
        """Fetch one academic year's education types."""
        return self._request(
            "/app/rest/accomplishmentplan",
            (("year", str(year)), ("educationTypeId", "")),
        )

    def get_programmes(self, year: int, education_type_id: str) -> SourceResponse[Any]:
        """Fetch one education type's degree programmes for one academic year."""
        return self._request(
            f"/app/rest/accomplishmentplan/year/{year}"
            f"/educationtype/{quote(education_type_id, safe='')}"
        )

    def get_groups(
        self, year: int, education_type_id: str, programme_id: str
    ) -> SourceResponse[Any]:
        """Fetch one degree programme's groups and their accomplishment plans."""
        return self._request(
            f"/app/rest/accomplishmentplan/year/{year}"
            f"/educationtype/{quote(education_type_id, safe='')}"
            f"/degreeprogramme/{quote(programme_id, safe='')}"
        )

    def get_plan(self, plan_id: str) -> SourceResponse[Any]:
        """Fetch one accomplishment plan's flat learning-unit list."""
        return self._request(
            f"/app/rest/accomplishmentplan/{quote(plan_id, safe='')}/learningunits"
        )

    def get_learning_unit(self, unit_id: str) -> SourceResponse[Any]:
        """Fetch one learning unit's detail."""
        return self._request(f"/app/rest/learningunit/{quote(unit_id, safe='')}")
