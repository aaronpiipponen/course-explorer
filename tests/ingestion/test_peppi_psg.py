"""Exercise the Peppi PSG parser and discovery capture over source-shaped fixture bodies."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from course_explorer.config import KoriSettings
from course_explorer.enums import (
    IngestionRunStatus,
    ModuleRuleType,
    ModuleType,
    OfficialTextField,
    SourceObjectType,
)
from course_explorer.ingestion.capture import capture_curriculum
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.normalization import parse_capture
from course_explorer.ingestion.peppi_psg import (
    PeppiPsgContractError,
    PeppiPsgRequestError,
    PeppiPsgSource,
)
from course_explorer.ingestion.peppi_psg_capture import PeppiPsgCapture
from course_explorer.ingestion.peppi_psg_normalization import PeppiPsgParser
from course_explorer.ingestion.source import CapturedResponse, CurriculumPeriodRef
from course_explorer.instance import UNIVERSITIES, PeppiPsgSourceMetadata
from tests.ingestion._capture_support import RecordingCapturePersistence
from tests.support import settings_factory

TWO_YEARS = (2026, 2027)
TWO_PERIODS = ("2026-2027", "2027-2028")


def psg_block(key: str = "centria") -> PeppiPsgSourceMetadata:
    """Return one configured Peppi PSG source block."""
    block = UNIVERSITIES[key].peppi_psg
    assert block is not None
    return block


def fixture_block(key: str = "centria") -> PeppiPsgSourceMetadata:
    """Return one configured block replaced with the two fixture years, oldest first."""
    return replace(psg_block(key), years=TWO_YEARS)


def psg_parser(key: str = "centria") -> PeppiPsgParser:
    """Build the family parser of one configured Peppi PSG university."""
    university = UNIVERSITIES[key]
    return PeppiPsgParser(university.peppi_psg, university.official_languages)


def parser_for(block: PeppiPsgSourceMetadata, key: str = "centria") -> PeppiPsgParser:
    """Build the family parser of one replaced block."""
    return PeppiPsgParser(block, UNIVERSITIES[key].official_languages)


def plan_entry(
    order: int,
    level: int,
    code: str,
    unit_type: str,
    *,
    credits: float | None,
    unit_id: int,
) -> dict[str, Any]:
    """Build one flat plan entry with its published tree position."""
    return {
        "id": order,
        "learningUnit": {
            "id": unit_id,
            "code": code,
            "names": {"fi": f"Nimi {code}", "en": f"Name {code}"},
            "objectives": None,
            "content": None,
            "type": unit_type,
            "studyTypes": None,
        },
        "orderNumber": order,
        "level": level,
        "credits": credits,
        "divisionsList": [{"periodNumber": 1, "credits": 0.0}],
    }


def plan_body() -> list[dict[str, Any]]:
    """Build one plan carrying a module root, a nested module and a category."""
    return [
        plan_entry(2, 1, "ROOT-1", "STUDY_MODULE", credits=60.0, unit_id=1),
        plan_entry(3, 2, "MOD-2", "STUDY_MODULE", credits=20.0, unit_id=2),
        plan_entry(4, 3, "C-1", "COURSE_UNIT", credits=5.0, unit_id=101),
        plan_entry(5, 3, "C-2", "COURSE_UNIT", credits=5.0, unit_id=102),
        plan_entry(6, 2, "CAT-1", "CATEGORY", credits=10.0, unit_id=3),
        plan_entry(7, 3, "C-3", "COURSE_UNIT", credits=5.0, unit_id=103),
        plan_entry(8, 3, "C-4", "COURSE_UNIT", credits=None, unit_id=104),
    ]


def second_plan_body() -> list[dict[str, Any]]:
    """Build one plan listed under the later year only, with its own plan credit."""
    return [
        plan_entry(2, 1, "ROOT-2", "STUDY_MODULE", credits=30.0, unit_id=2),
        plan_entry(3, 2, "C-5", "COURSE_UNIT", credits=8.0, unit_id=105),
        plan_entry(4, 2, "OFF-1", "OFFERING", credits=2.0, unit_id=4),
        plan_entry(5, 3, "C-1", "COURSE_UNIT", credits=5.0, unit_id=101),
    ]


def plan_body_with_conflicting_credit() -> list[dict[str, Any]]:
    """Build the same plan with one course carrying a different plan credit."""
    body = plan_body()
    body[2]["credits"] = 6.0
    return body


def course_body(code: str, *, credits: float | None = None) -> dict[str, Any]:
    """Build one learning-unit detail with its published text fields."""
    body: dict[str, Any] = {
        "id": 40796,
        "code": code,
        "names": {"fi": f"Nimi {code}", "en": f"Name {code}"},
        "objectives": {"fi": "Tavoite.", "en": "Outcome."},
        "content": {"fi": "Sisältö.", "en": "Content."},
        "type": "COURSE_UNIT",
        "studyTypes": None,
    }
    if credits is not None:
        body["credits"] = credits
    return body


def compact(value: object) -> bytes:
    """Serialize one fixture body as exact capture bytes."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def captured_plan(body: list[dict[str, Any]], plan_id: str = "9001") -> CapturedResponse:
    """Build one captured accomplishment-plan response."""
    return CapturedResponse(
        f"/app/rest/accomplishmentplan/{plan_id}/learningunits", (), compact(body)
    )


def captured_group(year: int, plan_ids: tuple[int, ...]) -> CapturedResponse:
    """Build one captured group response whose path names the year and lists its plans."""
    return CapturedResponse(
        f"/app/rest/accomplishmentplan/year/{year}/educationtype/51/degreeprogramme/15502",
        (),
        compact(
            [
                {
                    "id": year,
                    "code": f"GROUP-{year}",
                    "names": {"fi": "Ryhmä", "en": "Group"},
                    "accomplishmentPlans": [{"id": plan_id} for plan_id in plan_ids],
                }
            ]
        ),
    )


def captured_course(body: dict[str, Any], path_id: str) -> CapturedResponse:
    """Build one captured learning-unit detail response."""
    return CapturedResponse(f"/app/rest/learningunit/{path_id}", (), compact(body))


def parsed_store(
    tmp_path: Path,
    responses: tuple[tuple[CapturedResponse, SourceObjectType | None], ...],
    *,
    block: PeppiPsgSourceMetadata | None = None,
    parser: PeppiPsgParser | None = None,
    name: str = "raw",
) -> RawCaptureStore:
    """Build one completed Peppi PSG capture store without database or network access."""
    block = block if block is not None else fixture_block()
    store = RawCaptureStore.create(
        tmp_path / name,
        run_id=uuid4(),
        started_at=datetime(2026, 11, 10, tzinfo=UTC),
        parser=parser if parser is not None else parser_for(block),
    )
    store.checkpoint_responses(responses)
    store.set_periods(
        tuple(CurriculumPeriodRef(period.id, period.start_year) for period in block.periods)
    )
    store.mark_completed()
    return store


def test_peppi_psg_parser_maps_tree_credits_and_identities(tmp_path: Path) -> None:
    """One plan supplies a version per listing year, with its own year's plan credit."""
    block = fixture_block()
    parser = parser_for(block)
    parser.bind_discovery({"9001": TWO_YEARS, "9002": (2027,), "9003": (2026,)})
    store = parsed_store(
        tmp_path,
        (
            (captured_group(2026, (9001, 9003)), None),
            (captured_group(2027, (9001, 9002)), None),
            (captured_plan(plan_body(), "9001"), SourceObjectType.MODULE),
            (captured_plan(second_plan_body(), "9002"), SourceObjectType.MODULE),
            (captured_plan([], "9003"), SourceObjectType.MODULE),
            (captured_course(course_body("C-1"), "101"), SourceObjectType.COURSE_UNIT),
            (
                captured_course(course_body("C-2", credits=3.0), "102"),
                SourceObjectType.COURSE_UNIT,
            ),
            (captured_course(course_body("C-3"), "103"), SourceObjectType.COURSE_UNIT),
            (captured_course(course_body("C-4"), "104"), SourceObjectType.COURSE_UNIT),
            (captured_course(course_body("C-5"), "105"), SourceObjectType.COURSE_UNIT),
        ),
        block=block,
        parser=parser,
    )

    batch = parse_capture(store)
    courses = {course.provenance.source_object_id: course for course in batch.courses}
    modules = {module.provenance.source_object_id: module for module in batch.modules}

    assert set(modules) == {
        "PLAN:9001:STUDY_MODULE:ROOT-1|2026-2027",
        "PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028",
        "PLAN:9001:STUDY_MODULE:MOD-2|2026-2027",
        "PLAN:9001:STUDY_MODULE:MOD-2|2027-2028",
        "PLAN:9001:CATEGORY:CAT-1|2026-2027",
        "PLAN:9001:CATEGORY:CAT-1|2027-2028",
        "PLAN:9002:STUDY_MODULE:ROOT-2|2027-2028",
        "PLAN:9002:OFFERING:OFF-1|2027-2028",
    }
    assert {source_id: module.module_type for source_id, module in modules.items()} == {
        "PLAN:9001:STUDY_MODULE:ROOT-1|2026-2027": ModuleType.STUDY_MODULE,
        "PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028": ModuleType.STUDY_MODULE,
        "PLAN:9001:STUDY_MODULE:MOD-2|2026-2027": ModuleType.STUDY_MODULE,
        "PLAN:9001:STUDY_MODULE:MOD-2|2027-2028": ModuleType.STUDY_MODULE,
        "PLAN:9001:CATEGORY:CAT-1|2026-2027": ModuleType.GROUPING_MODULE,
        "PLAN:9001:CATEGORY:CAT-1|2027-2028": ModuleType.GROUPING_MODULE,
        "PLAN:9002:STUDY_MODULE:ROOT-2|2027-2028": ModuleType.STUDY_MODULE,
        "PLAN:9002:OFFERING:OFF-1|2027-2028": ModuleType.GROUPING_MODULE,
    }
    for source_id, module in modules.items():
        group_id, period = source_id.rsplit("|", 1)
        assert module.code == group_id.rsplit(":", 1)[1]
        assert module.provenance.source_group_id == group_id
        assert module.curriculum_period_ids == (period,)
        assert module.document_state is None
        assert module.validity_start is None and module.validity_end is None
        assert module.rule is not None

    assert (
        modules["PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028"].target_credits_min,
        modules["PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028"].target_credits_max,
    ) == (60, 60)
    assert modules["PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028"].names == {
        "fi": "Nimi ROOT-1",
        "en": "Name ROOT-1",
    }

    root = modules["PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028"].rule
    assert root.node_type is ModuleRuleType.COMPOSITE_RULE
    assert root.all_mandatory is False
    assert root.require_min == 0 and root.require_max is None
    assert root.credits_min is None and root.credits_max is None
    assert root.count_min is None and root.count_max is None
    assert root.description is None and root.referenced_group_id is None
    assert [child.referenced_group_id for child in root.children] == [
        "PLAN:9001:STUDY_MODULE:MOD-2",
        "PLAN:9001:CATEGORY:CAT-1",
    ]
    assert [child.node_type for child in root.children] == [
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
    ]
    assert [child.sibling_position for child in root.children] == [0, 1]

    nested = modules["PLAN:9001:STUDY_MODULE:MOD-2|2027-2028"].rule
    assert [child.referenced_group_id for child in nested.children] == ["C-1", "C-2"]
    assert [child.node_type for child in nested.children] == [
        ModuleRuleType.COURSE_UNIT_RULE,
        ModuleRuleType.COURSE_UNIT_RULE,
    ]
    category = modules["PLAN:9001:CATEGORY:CAT-1|2027-2028"].rule
    assert [child.referenced_group_id for child in category.children] == ["C-3", "C-4"]
    second_root = modules["PLAN:9002:STUDY_MODULE:ROOT-2|2027-2028"].rule
    assert [child.referenced_group_id for child in second_root.children] == [
        "C-5",
        "PLAN:9002:OFFERING:OFF-1",
    ]
    assert [child.node_type for child in second_root.children] == [
        ModuleRuleType.COURSE_UNIT_RULE,
        ModuleRuleType.MODULE_RULE,
    ]
    offering = modules["PLAN:9002:OFFERING:OFF-1|2027-2028"].rule
    assert [child.referenced_group_id for child in offering.children] == ["C-1"]
    for node in (root, nested, category, second_root, offering):
        for child in node.children:
            assert child.require_min is None and child.require_max is None
            assert child.all_mandatory is None and child.description is None
            assert child.credits_min is None and child.credits_max is None

    assert set(courses) == {
        "C-1|2026-2027",
        "C-1|2027-2028",
        "C-2|2026-2027",
        "C-2|2027-2028",
        "C-3|2026-2027",
        "C-3|2027-2028",
        "C-4|2026-2027",
        "C-4|2027-2028",
        "C-5|2027-2028",
    }
    for period in TWO_PERIODS:
        assert (courses[f"C-1|{period}"].credits_min, courses[f"C-1|{period}"].credits_max) == (
            5,
            5,
        )
        # A detail's own credit wins over that year's plan credit.
        assert (courses[f"C-2|{period}"].credits_min, courses[f"C-2|{period}"].credits_max) == (
            3,
            3,
        )
        assert (courses[f"C-3|{period}"].credits_min, courses[f"C-3|{period}"].credits_max) == (
            5,
            5,
        )
        assert courses[f"C-4|{period}"].credits_min is None
        assert courses[f"C-4|{period}"].credits_max is None
    # The later year's own plan supplies the credit of a code it lists alone.
    assert (courses["C-5|2027-2028"].credits_min, courses["C-5|2027-2028"].credits_max) == (8, 8)

    course = courses["C-1|2027-2028"]
    assert course.group_id == "C-1"
    assert course.curriculum_period_ids == ("2027-2028",)
    assert course.official_url == "https://centria.opinto-opas.fi/coursedescription/101?lang=fi"
    assert course.document_state is None
    assert course.validity_start is None and course.validity_end is None
    assert course.texts[OfficialTextField.NAME] == {"fi": "Nimi C-1", "en": "Name C-1"}
    assert course.texts[OfficialTextField.OUTCOMES] == {"fi": "Tavoite.", "en": "Outcome."}
    assert course.texts[OfficialTextField.CONTENT] == {"fi": "Sisältö.", "en": "Content."}
    assert course.texts[OfficialTextField.PREREQUISITE_TEXT] == {}
    assert course.texts[OfficialTextField.ADDITIONAL_INFO] == {}
    assert course.prerequisite_options == () and course.substitution_options == ()

    conflicting = parser_for(block)
    conflicting.bind_discovery({"9001": TWO_YEARS})
    conflicting.decode_objects(captured_plan(plan_body()), SourceObjectType.MODULE)
    with pytest.raises(PeppiPsgContractError):
        conflicting.decode_objects(
            captured_plan(plan_body_with_conflicting_credit()), SourceObjectType.MODULE
        )

    # One plan entry whose learning-unit id differs from its code still joins the listing year and
    # that year's plan credit, because the detail is looked up by its requested id. A course-unit
    # entry publishes no names, so the join must not read that key.
    joined_parser = parser_for(block)
    joined_parser.bind_discovery({"9004": (2027,)})
    joined_entry = plan_entry(2, 1, "C-7", "COURSE_UNIT", credits=4.0, unit_id=107)
    del joined_entry["learningUnit"]["names"]
    joined_parser.decode_objects(captured_plan([joined_entry], "9004"), SourceObjectType.MODULE)
    (joined,) = joined_parser.decode_objects(
        captured_course(course_body("C-7"), "107"), SourceObjectType.COURSE_UNIT
    )
    assert joined.source_group_id == "C-7"
    assert joined.value["code"] == "C-7"
    assert joined.value["period"] == "2027-2028"
    assert (Decimal(joined.value["credits_min"]), Decimal(joined.value["credits_max"])) == (
        4,
        4,
    )


_ABSENT = object()


def route_handler(routes: dict[str, object], calls: list[str]) -> Any:
    """Answer the declared routes, return 404 for a declared absence, and fail on the unexpected."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path not in routes:
            raise AssertionError(f"unexpected Peppi PSG request {path}")
        payload = routes[path]
        if payload is _ABSENT:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return handle


def discovery_routes() -> dict[str, object]:
    """Build both years' discovery chain, with plan 9001 listed under both and 9002 under 2027."""
    plan_one = [
        plan_entry(2, 1, "ROOT-1", "STUDY_MODULE", credits=60.0, unit_id=1),
        plan_entry(3, 2, "C-1", "COURSE_UNIT", credits=5.0, unit_id=101),
        plan_entry(4, 2, "C-2", "COURSE_UNIT", credits=5.0, unit_id=102),
    ]
    plan_two = [
        plan_entry(2, 1, "ROOT-2", "STUDY_MODULE", credits=30.0, unit_id=2),
        plan_entry(3, 2, "C-3", "COURSE_UNIT", credits=5.0, unit_id=103),
    ]

    def group_path(year: int, education_type: int) -> str:
        return (
            f"/app/rest/accomplishmentplan/year/{year}/educationtype/{education_type}"
            "/degreeprogramme/15502"
        )

    def group(year: int, code: str, plans: tuple[int, ...]) -> list[dict[str, Any]]:
        return [
            {
                "id": year,
                "code": code,
                "names": {"fi": "Ryhmä", "en": "Group"},
                "accomplishmentPlans": [{"id": plan_id} for plan_id in plans],
            }
        ]

    routes: dict[str, object] = {
        "/app/rest/accomplishmentplan/years": [2026, 2027],
        "/app/rest/accomplishmentplan": [
            {"id": 51, "code": "A", "names": {"fi": "Tutkinto", "en": "Degree"}},
            {"id": 50, "code": "N", "names": {"fi": "Tutkinto", "en": "Degree"}},
        ],
        "/app/rest/accomplishmentplan/9001/learningunits": plan_one,
        "/app/rest/accomplishmentplan/9002/learningunits": plan_two,
        "/app/rest/learningunit/101": course_body("C-1"),
        "/app/rest/learningunit/102": course_body("C-2"),
        "/app/rest/learningunit/103": course_body("C-3"),
    }
    for year in TWO_YEARS:
        for education_type in (51, 50):
            routes[f"/app/rest/accomplishmentplan/year/{year}/educationtype/{education_type}"] = [
                {"id": 15502, "code": "BMK"}
            ]
        routes[group_path(year, 51)] = group(year, "GROUP-A", (9001,))
        routes[group_path(year, 50)] = group(year, "GROUP-B", (9002,) if year == 2027 else ())
    return routes


def new_psg_store(
    tmp_path: Path,
    name: str = "capture",
    *,
    checkpoint_response_count: int = 100,
    block: PeppiPsgSourceMetadata | None = None,
) -> tuple[RawCaptureStore, PeppiPsgParser]:
    """Create one running Peppi PSG capture store and the parser instance it shares."""
    block = block if block is not None else fixture_block()
    parser = parser_for(block)
    store = RawCaptureStore.create(
        tmp_path / name,
        run_id=uuid4(),
        started_at=datetime(2026, 11, 10, tzinfo=UTC),
        parser=parser,
    )
    return store, parser


def psg_source(calls: list[str], routes: dict[str, object]) -> PeppiPsgSource:
    """Build one PSG client over a mock transport answering the declared routes."""
    settings = settings_factory(
        KoriSettings, INSTANCE_KEY="centria", CURRENT_ACADEMIC_YEAR="2027-2028"
    )
    return PeppiPsgSource(
        settings,
        transport=httpx.MockTransport(route_handler(routes, calls)),
        sleep=lambda _: None,
    )


def test_peppi_psg_capture_walks_the_discovery_chain_and_resumes(tmp_path: Path) -> None:
    """Both years are walked, one plan is fetched once for two listings, and a resume skips a detail."""
    block = fixture_block()
    routes = discovery_routes()
    store, parser = new_psg_store(tmp_path, checkpoint_response_count=1)
    calls: list[str] = []
    paused = False

    def pause_after_first_course(stage: str, completed: int, total: int | None) -> bool:
        nonlocal paused
        if paused:
            return True
        if any(
            response.object_type is SourceObjectType.COURSE_UNIT
            for response in store.manifest.responses
        ):
            paused = True
            return False
        return True

    with psg_source(calls, routes) as source:
        assert (
            capture_curriculum(
                PeppiPsgCapture(source, block, parser),
                store,
                RecordingCapturePersistence(),
                checkpoint_response_count=1,
                on_boundary=pause_after_first_course,
            )
            is None
        )
    assert store.manifest.status is IngestionRunStatus.RUNNING
    paused_counts = Counter(calls)
    assert paused_counts["/app/rest/learningunit/101"] == 1
    assert paused_counts["/app/rest/learningunit/102"] == 0
    assert paused_counts["/app/rest/learningunit/103"] == 0
    assert paused_counts["/app/rest/accomplishmentplan/9001/learningunits"] == 1

    reopened_parser = parser_for(block)
    reopened = RawCaptureStore.open(
        tmp_path / "capture", store.manifest_path, parser=reopened_parser
    )
    resumed_calls: list[str] = []
    with psg_source(resumed_calls, routes) as source:
        report = capture_curriculum(
            PeppiPsgCapture(source, block, reopened_parser),
            reopened,
            RecordingCapturePersistence(),
            checkpoint_response_count=100,
        )
    assert report is not None and report.status is IngestionRunStatus.COMPLETED
    resumed_detail_counts = Counter(path for path in resumed_calls if "learningunit/" in path)
    assert resumed_detail_counts == Counter(
        {
            "/app/rest/learningunit/102": 1,
            "/app/rest/learningunit/103": 1,
        }
    )
    assert sorted(path for path in resumed_calls if "/accomplishmentplan/9" in path) == [
        "/app/rest/accomplishmentplan/9001/learningunits",
        "/app/rest/accomplishmentplan/9002/learningunits",
    ]
    assert resumed_calls.count("/app/rest/accomplishmentplan/9001/learningunits") == 1
    assert resumed_calls.count("/app/rest/accomplishmentplan/9002/learningunits") == 1
    group_counts = Counter(
        path for path in resumed_calls if path.endswith("/degreeprogramme/15502")
    )
    assert group_counts == Counter(
        {
            f"/app/rest/accomplishmentplan/year/{year}/educationtype/{education_type}"
            "/degreeprogramme/15502": 1
            for year in TWO_YEARS
            for education_type in (51, 50)
        }
    )

    object_types = {
        (response.request_path, response.request_params): response.object_type
        for response in reopened.manifest.responses
    }
    assert object_types[("/app/rest/accomplishmentplan/years", (("educationTypeId", ""),))] is None
    assert (
        object_types[
            (
                "/app/rest/accomplishmentplan/year/2027/educationtype/51/degreeprogramme/15502",
                (),
            )
        ]
        is None
    )
    assert object_types[("/app/rest/accomplishmentplan/9001/learningunits", ())] is (
        SourceObjectType.MODULE
    )
    assert object_types[("/app/rest/learningunit/101", ())] is SourceObjectType.COURSE_UNIT
    education_type_requests = [
        response.request_params
        for response in reopened.manifest.responses
        if response.request_path == "/app/rest/accomplishmentplan"
    ]
    assert set(education_type_requests) == {
        (("year", str(year)), ("educationTypeId", "")) for year in TWO_YEARS
    }

    batch = parse_capture(reopened)
    assert {module.provenance.source_object_id for module in batch.modules} == {
        "PLAN:9001:STUDY_MODULE:ROOT-1|2026-2027",
        "PLAN:9001:STUDY_MODULE:ROOT-1|2027-2028",
        "PLAN:9002:STUDY_MODULE:ROOT-2|2027-2028",
    }
    assert {course.provenance.source_object_id for course in batch.courses} == {
        "C-1|2026-2027",
        "C-1|2027-2028",
        "C-2|2026-2027",
        "C-2|2027-2028",
        "C-3|2027-2028",
    }

    # A missing course detail fails the run, and an unpublished mapped year is a contract error.
    missing = dict(routes)
    missing["/app/rest/learningunit/102"] = _ABSENT
    missing_store, missing_parser = new_psg_store(tmp_path, "missing")
    with psg_source([], missing) as source:
        with pytest.raises(PeppiPsgRequestError):
            capture_curriculum(
                PeppiPsgCapture(source, block, missing_parser),
                missing_store,
                RecordingCapturePersistence(),
                checkpoint_response_count=100,
            )
    assert missing_store.manifest.status is IngestionRunStatus.FAILED

    wrong_year = dict(routes)
    wrong_year["/app/rest/accomplishmentplan/years"] = [2027]
    year_store, year_parser = new_psg_store(tmp_path, "wrong-year")
    with psg_source([], wrong_year) as source:
        with pytest.raises(PeppiPsgContractError, match="2026"):
            capture_curriculum(
                PeppiPsgCapture(source, block, year_parser),
                year_store,
                RecordingCapturePersistence(),
                checkpoint_response_count=100,
            )
    assert year_store.manifest.status is IngestionRunStatus.FAILED
