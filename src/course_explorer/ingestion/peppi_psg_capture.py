"""Peppi Solenovo PSG discovery, plan reading and shared-checkpoint driving.

This module owns the fixed PSG discovery chain and the checkpointing of every successful response.
The family parser owns decoding. This driver only walks source identities. Discovery runs once for
every published academic year of the configured source block, and a plan listed under several years
is fetched once while its listing years are handed to the parser.
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
from course_explorer.ingestion.peppi_psg import PeppiPsgContractError, PeppiPsgSource
from course_explorer.ingestion.peppi_psg_normalization import PeppiPsgParser
from course_explorer.ingestion.repository import CapturePersistence
from course_explorer.ingestion.source import CurriculumPeriodRef, SourceResponse
from course_explorer.instance import PeppiPsgSourceMetadata


def _identity(value: object, label: str) -> str:
    """Return one non-blank source identity as text or raise a contract error."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise PeppiPsgContractError(f"Peppi PSG {label} must be a non-blank identity")
    return value


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    """Return one source array of objects or raise a contract error."""
    if not isinstance(value, list):
        raise PeppiPsgContractError(f"Peppi PSG {label} response must be a list")
    for item in value:
        if not isinstance(item, dict):
            raise PeppiPsgContractError(f"Peppi PSG {label} response item must be an object")
    return value


def _ordered_identities(objects: list[dict[str, Any]], label: str) -> tuple[str, ...]:
    """Return the distinct non-blank identities of one object list in source order."""
    ordered: dict[str, None] = {}
    for item in objects:
        ordered.setdefault(_identity(item.get("id"), label), None)
    return tuple(ordered)


def _year_list(value: object) -> tuple[int, ...]:
    """Return every published academic year of one years response."""
    if not isinstance(value, list):
        raise PeppiPsgContractError("Peppi PSG years response must be a list")
    years: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise PeppiPsgContractError("Peppi PSG years response must hold integers")
        years.append(item)
    return tuple(years)


def _plan_course_ids(value: object) -> dict[str, str]:
    """Return one plan's ordered course code to learning-unit id map."""
    if not isinstance(value, list):
        raise PeppiPsgContractError("Peppi PSG accomplishment-plan response must be a list")
    course_ids: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, dict):
            raise PeppiPsgContractError("Peppi PSG plan entry must be an object")
        unit = entry.get("learningUnit")
        if not isinstance(unit, dict):
            raise PeppiPsgContractError("Peppi PSG plan entry requires a learning unit")
        if unit.get("type") != "COURSE_UNIT":
            continue
        code = _identity(unit.get("code"), "course code")
        unit_id = _identity(unit.get("id"), "course learning-unit id")
        previous = course_ids.setdefault(code, unit_id)
        if previous != unit_id:
            raise PeppiPsgContractError(
                f"course code {code} carries more than one learning-unit id"
            )
    return course_ids


class PeppiPsgCapture:
    """Drive one Peppi PSG discovery and course fetch into shared capture checkpoints."""

    def __init__(
        self, source: PeppiPsgSource, block: PeppiPsgSourceMetadata, parser: PeppiPsgParser
    ) -> None:
        self._source = source
        self._block = block
        self._parser = parser

    def capture_pending(
        self,
        store: RawCaptureStore,
        repository: CapturePersistence,
        *,
        checkpoint_response_count: int,
        on_boundary: CaptureBoundary | None,
    ) -> bool:
        """Record pending Peppi PSG evidence and return whether discovery and fetching finished.

        One discovery walk runs for every mapped year in source-block order. A plan id listed
        under several years is fetched once, and every listing year is handed to the parser before
        any plan response is recorded.
        """
        store.set_periods(
            tuple(
                CurriculumPeriodRef(period.id, period.start_year) for period in self._block.periods
            )
        )
        recorder = ResponseRecorder(store, repository, batch_size=checkpoint_response_count)
        try:
            years = _year_list(
                self._fetch(self._source.get_years, recorder, store, on_boundary, None).value
            )
            missing = next(
                (year for year in self._block.years if year not in years),
                None,
            )
            if missing is not None:
                raise PeppiPsgContractError(
                    f"Peppi PSG source does not publish the mapped year {missing}"
                )
            plan_ids: dict[str, None] = {}
            plan_years: dict[str, tuple[int, ...]] = {}
            for year in self._block.years:
                for plan_id, listing in self._discover_year(
                    year, recorder, store, on_boundary
                ).items():
                    plan_ids.setdefault(plan_id, None)
                    previous = plan_years.get(plan_id, ())
                    plan_years[plan_id] = previous + tuple(
                        listed for listed in listing if listed not in previous
                    )
            self._parser.bind_discovery(plan_years)
            course_ids = self._read_plans(tuple(plan_ids), recorder, store, on_boundary)
            self._fetch_courses(course_ids, recorder, store, on_boundary)
        except BoundaryRefused:
            recorder.flush()
            return False
        recorder.flush()
        return True

    def _fetch(
        self,
        call: Callable[[], SourceResponse[Any]],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
        object_type: SourceObjectType | None,
    ) -> SourceResponse[Any]:
        """Record one successful request unless the controller refuses the next request."""
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            raise BoundaryRefused
        response = call()
        recorder.add(response, object_type)
        return response

    def _discover_year(
        self,
        year: int,
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> dict[str, tuple[int, ...]]:
        """Record one year's discovery chain and return its plan ids with that listing year.

        One group read runs for every education-type and degree-programme pair, because one
        programme id can be published under several education types with its own groups and plans.
        Plans are deduplicated by plan id.
        """
        education_types = _ordered_identities(
            _object_list(
                self._fetch(
                    lambda: self._source.get_education_types(year),
                    recorder,
                    store,
                    on_boundary,
                    None,
                ).value,
                "education types",
            ),
            "education type id",
        )
        programme_pairs: dict[tuple[str, str], None] = {}
        for education_type_id in education_types:
            response = self._fetch(
                lambda education_type=education_type_id: self._source.get_programmes(
                    year, education_type
                ),
                recorder,
                store,
                on_boundary,
                None,
            )
            for programme in _object_list(response.value, "programmes"):
                programme_pairs.setdefault(
                    (education_type_id, _identity(programme.get("id"), "degree programme id")),
                    None,
                )

        plans: dict[str, tuple[int, ...]] = {}
        for education_type_id, programme_id in programme_pairs:
            response = self._fetch(
                lambda programme=programme_id, education_type=education_type_id: (
                    self._source.get_groups(year, education_type, programme)
                ),
                recorder,
                store,
                on_boundary,
                None,
            )
            for group in _object_list(response.value, "groups"):
                listed = group.get("accomplishmentPlans")
                if not isinstance(listed, list):
                    raise PeppiPsgContractError(
                        "Peppi PSG group accomplishment plans must be a list"
                    )
                for plan in listed:
                    if not isinstance(plan, dict):
                        raise PeppiPsgContractError(
                            "Peppi PSG accomplishment plan must be an object"
                        )
                    plans.setdefault(_identity(plan.get("id"), "accomplishment plan id"), (year,))
        return plans

    def _read_plans(
        self,
        plan_ids: tuple[str, ...],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> dict[str, str]:
        """Record every accomplishment plan and return its course code to learning-unit id map."""
        course_ids: dict[str, str] = {}
        for plan_id in plan_ids:
            response = self._fetch(
                lambda plan=plan_id: self._source.get_plan(plan),
                recorder,
                store,
                on_boundary,
                SourceObjectType.MODULE,
            )
            for code, unit_id in _plan_course_ids(response.value).items():
                previous = course_ids.setdefault(code, unit_id)
                if previous != unit_id:
                    raise PeppiPsgContractError(
                        f"course code {code} carries more than one learning-unit id"
                    )
        return course_ids

    def _fetch_courses(
        self,
        course_ids: Mapping[str, str],
        recorder: ResponseRecorder,
        store: RawCaptureStore,
        on_boundary: CaptureBoundary | None,
    ) -> None:
        """Fetch each distinct missing course detail once in sorted code order."""
        fetched = {
            (response.request_path, response.request_params)
            for response in store.manifest.responses
            if response.object_type is SourceObjectType.COURSE_UNIT
        }
        for code in sorted(course_ids):
            unit_id = course_ids[code]
            key = (f"/app/rest/learningunit/{quote(unit_id, safe='')}", ())
            if key in fetched:
                continue
            fetched.add(key)
            self._fetch(
                lambda unit=unit_id: self._source.get_learning_unit(unit),
                recorder,
                store,
                on_boundary,
                SourceObjectType.COURSE_UNIT,
            )
