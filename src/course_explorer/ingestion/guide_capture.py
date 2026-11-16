"""Peppi guide discovery, page fetching and shared-checkpoint driving.

This module owns the guide walk from the root page through programme and structure pages to one
course page per code. The family parser owns decoding. This driver only walks source paths. One
walk serves every mapped period, structures are recorded before any course page, and a course page
is fetched once per code because it is period-independent.
"""

from __future__ import annotations

from course_explorer.enums import SourceObjectType
from course_explorer.ingestion.capture import (
    BoundaryRefused,
    CaptureBoundary,
    ResponseRecorder,
    boundary_allows,
)
from course_explorer.ingestion.guide import (
    GuideContractError,
    GuideCourseLink,
    GuideServerError,
    GuideSource,
    GuideStructureLink,
    discover_course_links,
    discover_programme_paths,
    discover_structure_links,
)
from course_explorer.ingestion.guide_normalization import rendered_error_page
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.repository import CapturePersistence
from course_explorer.ingestion.source import CurriculumPeriodRef
from course_explorer.instance import GuideSourceMetadata


class GuideCapture:
    """Drive one guide discovery and page fetch into shared capture checkpoints.

    A course page that keeps answering HTTP 500 after the shared retry policy, or answers HTTP 200
    with the guide's own rendered server-error page, is recorded once as an unresolved course
    reference. Every other failure fails the run.
    """

    def __init__(self, source: GuideSource, block: GuideSourceMetadata) -> None:
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
        """Record pending guide evidence and return whether discovery and fetching finished.

        The root, programme and structure pages are recorded as evidence-only responses. The
        selected periods are the distinct year groups the structure pages publish, and a structure
        year outside the map fails the run. A reopen records no period before its walk finds a
        structure page. A reopened store re-reads its recorded pages and fetches only the pages it
        lacks.
        """
        recorder = ResponseRecorder(store, repository, batch_size=checkpoint_response_count)
        recorded_by_path = {
            response.request_path: response for response in store.manifest.responses
        }
        this_run: dict[str, str] = {}

        def page(path: str, object_type: SourceObjectType | None) -> str:
            if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
                raise BoundaryRefused
            fetched = this_run.get(path)
            if fetched is not None:
                return fetched
            recorded = recorded_by_path.get(path)
            if recorded is not None:
                return store.read_response_body(recorded).decode("utf-8")
            response = self._source.get_page(path)
            recorder.add(response, object_type)
            this_run[path] = response.value
            return response.value

        def course_page(path: str, code: str) -> None:
            if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
                raise BoundaryRefused
            if path in this_run or path in recorded_by_path:
                return
            try:
                response = self._source.get_page(path)
            except GuideServerError:
                unresolved.add(code)
                return
            if rendered_error_page(response.value):
                unresolved.add(code)
                return
            recorder.add(response, SourceObjectType.COURSE_UNIT)
            this_run[path] = response.value

        try:
            root_html = page("/", None)
            programmes = discover_programme_paths(
                root_html,
                base_url=self._block.guide_base_url,
                primary_language=self._primary_language,
            )
            structures: dict[str, GuideStructureLink] = {}
            for programme_path in programmes:
                programme_html = page(programme_path, None)
                for link in discover_structure_links(
                    programme_html,
                    base_url=self._block.guide_base_url,
                    programme_path=programme_path,
                    primary_language=self._primary_language,
                ):
                    existing = structures.get(link.path)
                    if existing is not None:
                        if existing.period_id != link.period_id:
                            raise GuideContractError(
                                f"structure path {link.path} is published under "
                                f"{existing.period_id} and {link.period_id}"
                            )
                        continue
                    structures[link.path] = link
            if not structures:
                raise GuideContractError("guide discovery found no structure page")
            periods = {period.id: index for index, period in enumerate(self._block.periods)}
            for link in structures.values():
                _require_mapped_period(link, periods)
            published_period_ids = {link.period_id for link in structures.values()}
            store.set_periods(
                tuple(
                    CurriculumPeriodRef(period.id, period.start_year)
                    for period in self._block.periods
                    if period.id in published_period_ids
                )
            )
            ordered = sorted(
                structures.values(),
                key=lambda link: (periods[link.period_id], link.path),
            )
            courses: dict[str, GuideCourseLink] = {}
            for link in ordered:
                structure_html = page(link.path, SourceObjectType.MODULE)
                for course in discover_course_links(
                    structure_html, base_url=self._block.guide_base_url
                ):
                    courses.setdefault(course.code, course)
            unresolved = set(store.manifest.unresolved_course_group_ids)
            for code in sorted(courses):
                if code in unresolved:
                    continue
                course_page(courses[code].path, code)
        except BoundaryRefused:
            recorder.flush()
            return False
        recorder.flush(unresolved_course_group_ids=unresolved)
        return True

    @property
    def _primary_language(self) -> str:
        return self._source.primary_language


def _require_mapped_period(link: GuideStructureLink, periods: dict[str, int]) -> None:
    """Require one structure year group to name a mapped one-year period."""
    start_text, end_text = link.period_id.split("-", maxsplit=1)
    if int(end_text) - int(start_text) != 1:
        raise GuideContractError(
            f"published structure year {link.period_id} for {link.path} must span one academic year"
        )
    if link.period_id not in periods:
        raise GuideContractError(
            f"published structure year {link.period_id} for {link.path} is not a mapped period"
        )
