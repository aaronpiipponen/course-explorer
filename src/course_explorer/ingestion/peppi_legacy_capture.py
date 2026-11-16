"""Peppi legacy discovery, exact course fetching and shared-checkpoint driving.

This module owns the three legacy discovery variants and the checkpointing of every successful
response. The family parser owns decoding. This driver only walks source identities. The driver
reads every published period of the configured source block, in block order, and fetches one course
detail per period and code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from course_explorer.enums import SourceObjectType
from course_explorer.ingestion.capture import (
    BoundaryRefused,
    CaptureBoundary,
    ResponseRecorder,
    boundary_allows,
)
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.peppi_legacy import (
    PeppiLegacyContractError,
    PeppiLegacyNotFound,
    PeppiLegacySource,
    walk_nodes,
)
from course_explorer.ingestion.repository import CapturePersistence
from course_explorer.ingestion.source import CurriculumPeriodRef, SourceResponse
from course_explorer.instance import PeppiLegacySourceMetadata, PeppiPeriod


def _required_text(value: object, label: str) -> str:
    """Return one non-blank source identity string or raise a contract error."""
    if not isinstance(value, str) or not value.strip():
        raise PeppiLegacyContractError(f"Peppi {label} must be a non-blank string")
    return value


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    """Return one source array of node objects or raise a contract error."""
    if not isinstance(value, list):
        raise PeppiLegacyContractError(f"Peppi {label} response must be a list")
    for item in value:
        if not isinstance(item, dict):
            raise PeppiLegacyContractError(f"Peppi {label} response item must be an object")
    return value


def _ordered_ids(nodes: list[dict[str, Any]], label: str) -> tuple[str, ...]:
    """Return the distinct non-blank ids of one node list in source order."""
    ordered: dict[str, None] = {}
    for node in nodes:
        ordered.setdefault(_required_text(node.get("id"), label), None)
    return tuple(ordered)


class PeppiLegacyCapture:
    """Drive one Peppi legacy discovery and course fetch into shared capture checkpoints."""

    def __init__(self, source: PeppiLegacySource, block: PeppiLegacySourceMetadata) -> None:
        self._source = source
        self._block = block

    def capture_pending(
        self,
        store: RawCaptureStore,
        repository: CapturePersistence,
        *,
        checkpoint_response_count: int,
        on_boundary: CaptureBoundary | None,
    ) -> bool:
        """Record pending Peppi evidence and return whether discovery and fetching finished.

        One walk runs for every published period in source-block order. The navigation entries
        and one organisation's education types do not depend on the period, so each is read once
        and reused for the later periods.
        """
        store.set_periods(
            tuple(
                CurriculumPeriodRef(period.id, period.start_year) for period in self._block.periods
            )
        )
        recorder = ResponseRecorder(store, repository, batch_size=checkpoint_response_count)
        try:
            entries = self._navigation_entries(recorder, store, on_boundary)
            education_types: dict[str, tuple[str, ...]] = {}
            course_ids: dict[tuple[str, str], str] = {}
            for period in self._block.periods:
                programme_ids = self._discover(
                    period, entries, education_types, recorder, store, on_boundary
                )
                for key, path_id in self._read_plans(
                    programme_ids, period, recorder, store, on_boundary
                ).items():
                    course_ids[key] = path_id
            self._fetch_courses(course_ids, recorder, store, on_boundary)
        except BoundaryRefused:
            recorder.flush()
            return False
        recorder.flush()
        return True

    def _fetch(
        self,
        call: Callable[[], SourceResponse[Any] | None],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
        object_type: SourceObjectType | None,
    ) -> SourceResponse[Any] | None:
        """Record one successful request unless the controller refuses the next request."""
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            raise BoundaryRefused
        response = call()
        if response is not None:
            recorder.add(response, object_type)
        return response

    def _navigation_entries(
        self,
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> list[dict[str, Any]]:
        """Record the navigation response once and return its entries."""
        navigation = self._fetch(self._source.get_navigation, recorder, store, on_boundary, None)
        if navigation is None:
            raise PeppiLegacyContractError("Peppi navigation response is required")
        return _object_list(navigation.value, "navigation")

    def _discover(
        self,
        period: PeppiPeriod,
        entries: list[dict[str, Any]],
        education_types: dict[str, tuple[str, ...]],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> tuple[str, ...]:
        """Record one period's discovery walk and return its distinct programme ids.

        Every period-dependent request carries the period. An organisation's education types are
        period-independent, so the first period reads them into ``education_types`` and a later
        period reuses them.
        """
        collected: dict[str, dict[str, Any]] = {}

        def collect(response: SourceResponse[Any] | None) -> None:
            if response is None:
                return
            for education in _object_list(response.value, "education"):
                collected.setdefault(_required_text(education.get("id"), "education id"), education)

        variant = self._block.discovery_variant
        if variant == "navigation_education_type":
            for entry in entries:
                navigation_id = _required_text(entry.get("id"), "navigation id")
                collect(
                    self._fetch(
                        lambda entry_id=navigation_id: self._source.get_educations_for_navigation(
                            entry_id, period.id
                        ),
                        recorder,
                        store,
                        on_boundary,
                        None,
                    )
                )
        elif variant == "organisation_navigation":
            organisations = self._fetch(
                lambda: self._source.get_organisations(period.id),
                recorder,
                store,
                on_boundary,
                None,
            )
            for organisation_id in self._organisation_ids(
                _object_list(organisations.value, "organisation"),
                period,
                recorder,
                store,
                on_boundary,
            ):
                for entry in entries:
                    navigation_id = _required_text(entry.get("id"), "navigation id")
                    collect(
                        self._fetch(
                            lambda organisation=organisation_id, entry_id=navigation_id: (
                                self._source.get_educations(organisation, entry_id, period.id)
                            ),
                            recorder,
                            store,
                            on_boundary,
                            None,
                        )
                    )
        else:
            unit_entries = [entry for entry in entries if entry.get("type") == "organisation_unit"]
            if unit_entries:
                organisation_ids = _ordered_ids(unit_entries, "organisation id")
            else:
                organisations = self._fetch(
                    lambda: self._source.get_organisations(period.id),
                    recorder,
                    store,
                    on_boundary,
                    None,
                )
                organisation_ids = _ordered_ids(
                    _object_list(organisations.value, "organisation"), "organisation id"
                )
            for organisation_id in organisation_ids:
                types = self._period_independent_education_types(
                    organisation_id, education_types, recorder, store, on_boundary
                )
                for type_id in types:
                    collect(
                        self._fetch(
                            lambda organisation=organisation_id, type_id=type_id: (
                                self._source.get_educations_for_type(
                                    organisation, type_id, period.id
                                )
                            ),
                            recorder,
                            store,
                            on_boundary,
                            None,
                        )
                    )

        return self._programme_ids(collected.values())

    def _period_independent_education_types(
        self,
        organisation_id: str,
        education_types: dict[str, tuple[str, ...]],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> tuple[str, ...]:
        """Return one organisation's education-type ids, reading them only on first use."""
        cached = education_types.get(organisation_id)
        if cached is not None:
            return cached
        types = self._fetch(
            lambda organisation=organisation_id: self._source.get_education_types(organisation),
            recorder,
            store,
            on_boundary,
            None,
        )
        ids = (
            ()
            if types is None
            else tuple(
                _required_text(education_type.get("id"), "education type id")
                for education_type in _object_list(types.value, "education-type")
            )
        )
        education_types[organisation_id] = ids
        return ids

    def _organisation_ids(
        self,
        organisations: list[dict[str, Any]],
        period: PeppiPeriod,
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> tuple[str, ...]:
        """Return the top-level organisation and nested unit ids in one ordered list."""
        ordered: dict[str, None] = {}
        for organisation in organisations:
            organisation_id = _required_text(organisation.get("id"), "organisation id")
            ordered.setdefault(organisation_id, None)
            units = self._fetch(
                lambda organisation=organisation_id: self._source.get_organisation_units(
                    organisation, period.id
                ),
                recorder,
                store,
                on_boundary,
                None,
            )
            if units is None:
                continue
            for unit in _object_list(units.value, "organisation units"):
                for node in walk_nodes(unit):
                    ordered.setdefault(_required_text(node.get("id"), "organisation id"), None)
        return tuple(ordered)

    @staticmethod
    def _programme_ids(educations: Any) -> tuple[str, ...]:
        """Return the distinct programme ids of every reached education tree in source order."""
        programmes: dict[str, None] = {}
        processed: set[str] = set()
        pending = list(educations)
        while pending:
            education = pending.pop(0)
            education_id = _required_text(education.get("id"), "education id")
            if education_id in processed:
                continue
            processed.add(education_id)
            children = education.get("children")
            if children is None:
                continue
            for node in walk_nodes(children):
                node_type = node.get("type")
                if node_type == "EDUCATION":
                    child_id = _required_text(node.get("id"), "education id")
                    if child_id not in processed:
                        pending.append(node)
                elif node_type == "PROGRAMME":
                    programmes.setdefault(_required_text(node.get("id"), "programme id"), None)
        return tuple(programmes)

    def _read_plans(
        self,
        programme_ids: tuple[str, ...],
        period: PeppiPeriod,
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> dict[tuple[str, str], str]:
        """Record one period's programme trees and return its (period, code) to path-id map."""
        course_ids: dict[tuple[str, str], str] = {}
        for programme_id in programme_ids:
            response = self._fetch(
                lambda programme=programme_id: self._source.get_accomplishment_plan(
                    programme, period.id
                ),
                recorder,
                store,
                on_boundary,
                SourceObjectType.MODULE,
            )
            body = response.value
            if not isinstance(body, list):
                raise PeppiLegacyContractError("accomplishment-plan response must be a list")
            if len(body) > 1:
                raise PeppiLegacyContractError(
                    "accomplishment-plan response must hold at most one tree"
                )
            if not body:
                continue
            for node in walk_nodes(body[0]):
                if node.get("type") != "COURSE_UNIT":
                    continue
                code = _required_text(node.get("code"), "course code")
                course_id = _required_text(node.get("id"), "course id")
                previous = course_ids.setdefault((period.id, code), course_id)
                if previous != course_id:
                    raise PeppiLegacyContractError(
                        f"course code {code} carries more than one path id"
                    )
        return course_ids

    def _fetch_courses(
        self,
        course_ids: Mapping[tuple[str, str], str],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> None:
        """Fetch each distinct missing course detail once per period and code.

        The unresolved set is computed after every listed period is attempted in the plan-code
        space: a plan code whose detail request key is recorded in any listed period resolves,
        including a recorded resume, and a code the source omits in every listed period is recorded
        once. The parser maps the request path id to the detail's official code. A pause or stop
        before the end records no unresolved result for the unfinished periods, so the resumed run
        re-requests the details of those periods.
        """
        recorded = {
            (response.request_path, response.request_params)
            for response in store.manifest.responses
            if response.object_type is SourceObjectType.COURSE_UNIT
        }
        resolved_codes: set[str] = set()
        unresolved_previous = set(store.manifest.unresolved_course_group_ids)
        for period, code in sorted(course_ids):
            if code in unresolved_previous:
                continue
            course_id = course_ids[(period, code)]
            key = (f"/api/course/{quote(course_id, safe='')}", (("period", period),))
            if key in recorded:
                resolved_codes.add(code)
                continue
            recorded.add(key)
            try:
                self._fetch(
                    lambda course=course_id, source_period=period: self._source.get_course(
                        course, source_period
                    ),
                    recorder,
                    store,
                    on_boundary,
                    SourceObjectType.COURSE_UNIT,
                )
            except PeppiLegacyNotFound:
                # The code stays out of resolved_codes, so it is unresolved only when no listed
                # period serves it.
                continue
            else:
                resolved_codes.add(code)
        unresolved = {code for period, code in course_ids if code not in resolved_codes}
        recorder.flush(unresolved_course_group_ids=unresolved)
