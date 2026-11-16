"""Exercise the Peppi legacy parser and discovery driver over source-shaped fixture bodies."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

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
from course_explorer.ingestion.normalization import parse_capture, walk_normalized_rule
from course_explorer.ingestion.peppi_legacy import PeppiLegacySource
from course_explorer.ingestion.peppi_legacy_capture import PeppiLegacyCapture
from course_explorer.ingestion.peppi_legacy_normalization import PeppiLegacyParser
from course_explorer.ingestion.source import CapturedResponse, CurriculumPeriodRef, SourcePeriod
from course_explorer.instance import UNIVERSITIES, PeppiLegacySourceMetadata, PeppiPeriod
from tests.ingestion._capture_support import RecordingCapturePersistence
from tests.support import settings_factory

PERIOD = "2026-2027"
SECOND_PERIOD = "2024-2026"


def peppi_block(key: str = "oulu") -> PeppiLegacySourceMetadata:
    """Return one configured Peppi legacy source block."""
    block = UNIVERSITIES[key].peppi_legacy
    assert block is not None
    return block


def peppi_parser(key: str = "oulu") -> PeppiLegacyParser:
    """Build the family parser of one configured Peppi legacy university."""
    university = UNIVERSITIES[key]
    return PeppiLegacyParser(university.peppi_legacy, university.official_languages)


def two_period_block(key: str = "oulu") -> PeppiLegacySourceMetadata:
    """Return one configured block replaced with the two fixture periods, oldest first."""
    return replace(peppi_block(key), periods=(PeppiPeriod(2024, 2026), PeppiPeriod(2026, 2027)))


def single_period_block(key: str, period: str) -> PeppiLegacySourceMetadata:
    """Return one configured block replaced with the one period its fixture requests use."""
    start_year = int(period.split("-", 1)[0])
    end_year = int(period.rsplit("-", 1)[1])
    return replace(peppi_block(key), periods=(PeppiPeriod(start_year, end_year),))


def block_parser(block: PeppiLegacySourceMetadata, key: str = "oulu") -> PeppiLegacyParser:
    """Build the family parser of one replaced block."""
    return PeppiLegacyParser(block, UNIVERSITIES[key].official_languages)


def course_body(code: str = "BIO-1") -> dict[str, Any]:
    """Build one legacy course detail carrying every observed section variant."""
    return {
        "id": None,
        "code": code,
        "name": {"valueFi": "Biomolekyylit", "valueEn": "Biomolecules", "valueSv": None},
        "credits": 5.0,
        "minCredits": 5,
        "maxCredits": 5,
        "contentList": [
            {
                "title": {"valueFi": "Osaamistavoitteet", "valueEn": "Learning outcomes"},
                "content": {"valueFi": "Tavoite.", "valueEn": "Outcome."},
            },
            {
                "title": {"valueFi": "Osaamistavoitteet"},
                "content": {"valueFi": "Toinen tavoite."},
            },
            {
                "title": {"valueFi": "Sisältö", "valueEn": "Content"},
                "content": {"valueFi": "Rakenne.", "valueEn": "Structure."},
            },
            {
                "title": {"valueEn": "Qualifications"},
                "content": {
                    "valueEn": "Biomolecules, Biomolecules for Biochemists or Biomolecules for Bioscientists.",
                    "valueFi": "Biomolekyylit.",
                },
            },
            {
                "title": {"valueEn": "Further information"},
                "content": {"valueEn": "Given in English.", "valueSv": "Ges på engelska."},
            },
            {
                "title": {"valueEn": "Study methods"},
                "content": {"valueEn": "Lectures."},
            },
            {
                "title": {"valueEn": "Assessment scale"},
                "content": {"valueEn": "Pass/fail."},
            },
        ],
        "createdAt": 1759999999000,
    }


def plan_body() -> list[dict[str, Any]]:
    """Build one programme tree carrying every observed node type and optionality value."""
    first = {"id": "c1", "code": "BIO-1", "type": "COURSE_UNIT"}
    second = {"id": "c2", "code": "BIO-2", "type": "COURSE_UNIT"}
    return [
        {
            "id": "p1",
            "code": "PROG-1",
            "type": "PROGRAMME",
            "name": {"valueFi": "Ohjelma", "valueEn": "Programme"},
            "optionality": None,
            "minCredits": 180,
            "maxCredits": 180,
            "children": [
                {
                    "id": "m1",
                    "code": "MOD-1",
                    "type": "STUDY_MODULE",
                    "name": {"valueFi": "Moduuli", "valueEn": "Module"},
                    "optionality": "ALL_OF",
                    "minCredits": 20,
                    "maxCredits": 20,
                    "children": [
                        first,
                        {
                            "id": "cat1",
                            "code": "CAT-1",
                            "type": "CATEGORY",
                            "name": {"valueFi": "Kategoria", "valueEn": "Category"},
                            "optionality": "ONE_OF",
                            "minCredits": 5,
                            "maxCredits": 10,
                            "children": [
                                first,
                                {
                                    "id": "off1",
                                    "code": "OFF-1",
                                    "type": "OFFERING",
                                    "name": {"valueFi": "Toteutus", "valueEn": "Offering"},
                                    "optionality": "SOME_OF",
                                    "minCredits": 2,
                                    "maxCredits": 4,
                                    "children": [
                                        {
                                            "id": "part1",
                                            "code": "PART-1",
                                            "type": "COURSE_UNIT_PART",
                                            "name": {
                                                "valueFi": "Osasuoritus",
                                                "valueEn": "Course part",
                                            },
                                            "optionality": "ECTS",
                                            "minCredits": 1,
                                            "maxCredits": 1,
                                            "children": [first],
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "id": "grp1",
                            "code": "GRP-1",
                            "type": "GROUPING_MODULE",
                            "name": {"valueFi": "Ryhmä", "valueEn": "Grouping"},
                            "optionality": "ALL_OF",
                            "minCredits": 5,
                            "maxCredits": 5,
                            "children": [first],
                        },
                    ],
                },
                {
                    "id": "m2",
                    "code": "MOD-2",
                    "type": "STUDY_MODULE",
                    "name": {"valueFi": "Toinen moduuli", "valueEn": "Second module"},
                    "optionality": "ECTS",
                    "minCredits": 10,
                    "maxCredits": 15,
                    "children": [
                        second,
                        {
                            "id": "p2",
                            "code": "PROG-1",
                            "type": "STUDY_MODULE",
                            "name": {"valueFi": "Ohjelman osa", "valueEn": "Programme part"},
                            "optionality": "ALL_OF",
                            "minCredits": 5,
                            "maxCredits": 5,
                            "children": [second],
                        },
                    ],
                },
                {
                    "id": "m3",
                    "code": "MOD-3",
                    "type": "STUDY_MODULE",
                    "name": {"valueFi": "Valinnainen moduuli", "valueEn": "Optional module"},
                    "optionality": "NUMBER_OF",
                    "minCredits": 12,
                    "maxCredits": 13,
                    "children": [first, second],
                },
            ],
        }
    ]


def plan_body_with_unresolved_course() -> list[dict[str, Any]]:
    """Build the programme tree with one course detail the source omits and one it serves."""
    body = deepcopy(plan_body())
    body[0]["children"][1]["children"].extend(
        [
            {"id": "c3", "code": "BIO-3", "type": "COURSE_UNIT"},
            {"id": "c4", "code": "BIO-4", "type": "COURSE_UNIT"},
        ]
    )
    return body


def compact(value: object) -> bytes:
    """Serialize one fixture body as exact capture bytes."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def captured_course(
    body: dict[str, Any], path_id: str = "8686", period: str = PERIOD
) -> CapturedResponse:
    """Build one captured course-detail response for the named published period."""
    return CapturedResponse(f"/api/course/{path_id}", (("period", period),), compact(body))


def parsed_store(
    tmp_path: Path,
    responses: tuple[tuple[CapturedResponse, SourceObjectType | None], ...],
    *,
    block: PeppiLegacySourceMetadata | None = None,
    name: str = "raw",
) -> RawCaptureStore:
    """Build one completed Peppi capture store without database or network access."""
    block = block if block is not None else peppi_block()
    store = RawCaptureStore.create(
        tmp_path / name,
        run_id=uuid4(),
        started_at=datetime(2026, 11, 10, tzinfo=UTC),
        parser=block_parser(block),
    )
    store.checkpoint_responses(responses)
    store.set_periods(
        tuple(CurriculumPeriodRef(period.id, period.start_year) for period in block.periods)
    )
    store.mark_completed()
    return store


_MODULE_GROUPS = (
    "PROGRAMME:PROG-1",
    "PROGRAMME:PROG-1:STUDY_MODULE:PROG-1",
    "PROGRAMME:PROG-1:STUDY_MODULE:MOD-1",
    "PROGRAMME:PROG-1:STUDY_MODULE:MOD-2",
    "PROGRAMME:PROG-1:STUDY_MODULE:MOD-3",
    "PROGRAMME:PROG-1:CATEGORY:CAT-1",
    "PROGRAMME:PROG-1:OFFERING:OFF-1",
    "PROGRAMME:PROG-1:COURSE_UNIT_PART:PART-1",
    "PROGRAMME:PROG-1:GROUPING_MODULE:GRP-1",
)
# A multi-year source period stores one membership per academic year it covers.
_MEMBERSHIPS = {
    "2024-2026": ("2024-2026:2024-2025", "2024-2026:2025-2026"),
    "2026-2027": ("2026-2027",),
}


def test_peppi_legacy_parser_maps_sections_rules_and_identities(tmp_path: Path) -> None:
    """Sections, module types, rule shapes and per-period identities map onto the records."""
    block = two_period_block()
    periods = tuple(period.id for period in block.periods)
    responses: list[tuple[CapturedResponse, SourceObjectType | None]] = []
    for period in periods:
        responses.append(
            (
                CapturedResponse(
                    "/api/accomplishment-plan/p1", (("period", period),), compact(plan_body())
                ),
                SourceObjectType.MODULE,
            )
        )
        responses.append(
            (captured_course(course_body(), "c1", period), SourceObjectType.COURSE_UNIT)
        )
        responses.append(
            (
                captured_course(course_body("BIO-2-OFFICIAL"), "c2", period),
                SourceObjectType.COURSE_UNIT,
            )
        )
    store = parsed_store(tmp_path, tuple(responses), block=block)

    assert block_parser(block).read_periods(store) == (
        SourcePeriod("2024-2026", 2024, 2026),
        SourcePeriod("2026-2027", 2026, 2027),
    )

    batch = parse_capture(store)
    courses = {course.provenance.source_object_id: course for course in batch.courses}
    modules = {module.provenance.source_object_id: module for module in batch.modules}

    # A course-unit child references the fetched detail's official code, not the plan code.
    assert set(courses) == {f"BIO-1|{period}" for period in periods} | {
        f"BIO-2-OFFICIAL|{period}" for period in periods
    }
    assert set(modules) == {
        f"{group_id}|{period}" for group_id in _MODULE_GROUPS for period in periods
    }

    for period in periods:
        for code, path_id in (("BIO-1", "c1"), ("BIO-2-OFFICIAL", "c2")):
            course = courses[f"{code}|{period}"]
            assert course.group_id == code
            assert course.provenance.source_group_id == code
            assert course.curriculum_period_ids == _MEMBERSHIPS[period]
            assert (course.credits_min, course.credits_max) == (5, 5)
            assert course.document_state is None
            assert course.validity_start is None and course.validity_end is None
            assert course.official_url == (
                f"https://opas.peppi.oulu.fi/fi/opintojakso/{code}/{path_id}?period={period}"
            )
            assert course.prerequisite_options == () and course.substitution_options == ()
        for group_id in _MODULE_GROUPS:
            module = modules[f"{group_id}|{period}"]
            assert module.code == group_id.rsplit(":", 1)[1]
            assert module.provenance.source_group_id == group_id
            assert module.curriculum_period_ids == _MEMBERSHIPS[period]
            assert module.document_state is None
            assert module.validity_start is None and module.validity_end is None
            assert module.rule is not None
            for node in walk_normalized_rule(module.rule):
                assert node.local_id is None
                assert node.description is None
                assert node.count_min is None and node.count_max is None

    # Both periods carry the same extracted texts and rule payload under their own identities.
    for group_id in _MODULE_GROUPS:
        assert modules[f"{group_id}|{periods[0]}"].rule == modules[f"{group_id}|{periods[1]}"].rule
    assert courses[f"BIO-1|{periods[0]}"].texts == courses[f"BIO-1|{periods[1]}"].texts

    course = courses[f"BIO-1|{periods[1]}"]
    assert course.texts[OfficialTextField.NAME] == {
        "fi": "Biomolekyylit",
        "en": "Biomolecules",
        "sv": None,
    }
    assert course.texts[OfficialTextField.OUTCOMES] == {
        "fi": "Tavoite.\n\nToinen tavoite.",
        "en": "Outcome.",
    }
    assert course.texts[OfficialTextField.CONTENT] == {"fi": "Rakenne.", "en": "Structure."}
    assert course.texts[OfficialTextField.PREREQUISITE_TEXT] == {
        "en": "Biomolecules, Biomolecules for Biochemists or Biomolecules for Bioscientists.",
        "fi": "Biomolekyylit.",
    }
    assert course.texts[OfficialTextField.ADDITIONAL_INFO] == {
        "en": "Given in English.",
        "sv": "Ges på engelska.",
    }

    assert {
        group_id: modules[f"{group_id}|{periods[0]}"].module_type for group_id in _MODULE_GROUPS
    } == {
        "PROGRAMME:PROG-1": ModuleType.DEGREE_PROGRAMME,
        "PROGRAMME:PROG-1:STUDY_MODULE:PROG-1": ModuleType.STUDY_MODULE,
        "PROGRAMME:PROG-1:STUDY_MODULE:MOD-1": ModuleType.STUDY_MODULE,
        "PROGRAMME:PROG-1:STUDY_MODULE:MOD-2": ModuleType.STUDY_MODULE,
        "PROGRAMME:PROG-1:STUDY_MODULE:MOD-3": ModuleType.STUDY_MODULE,
        "PROGRAMME:PROG-1:CATEGORY:CAT-1": ModuleType.GROUPING_MODULE,
        "PROGRAMME:PROG-1:OFFERING:OFF-1": ModuleType.GROUPING_MODULE,
        "PROGRAMME:PROG-1:COURSE_UNIT_PART:PART-1": ModuleType.GROUPING_MODULE,
        "PROGRAMME:PROG-1:GROUPING_MODULE:GRP-1": ModuleType.GROUPING_MODULE,
    }

    root = modules[f"PROGRAMME:PROG-1|{periods[1]}"].rule
    assert root.node_type is ModuleRuleType.COMPOSITE_RULE
    assert root.all_mandatory is False
    assert (root.require_min, root.require_max) == (0, None)
    assert root.credits_min is None
    assert [child.referenced_group_id for child in root.children] == [
        "PROGRAMME:PROG-1:STUDY_MODULE:MOD-1",
        "PROGRAMME:PROG-1:STUDY_MODULE:MOD-2",
        "PROGRAMME:PROG-1:STUDY_MODULE:MOD-3",
    ]
    assert [child.node_type for child in root.children] == [
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
    ]
    assert [child.sibling_position for child in root.children] == [0, 1, 2]

    mandatory = modules[f"PROGRAMME:PROG-1:STUDY_MODULE:MOD-1|{periods[1]}"].rule
    assert mandatory.all_mandatory is True
    assert [child.referenced_group_id for child in mandatory.children] == [
        "BIO-1",
        "PROGRAMME:PROG-1:CATEGORY:CAT-1",
        "PROGRAMME:PROG-1:GROUPING_MODULE:GRP-1",
    ]
    assert [child.node_type for child in mandatory.children] == [
        ModuleRuleType.COURSE_UNIT_RULE,
        ModuleRuleType.MODULE_RULE,
        ModuleRuleType.MODULE_RULE,
    ]

    choose_one = modules[f"PROGRAMME:PROG-1:CATEGORY:CAT-1|{periods[1]}"].rule
    assert (choose_one.require_min, choose_one.require_max) == (1, 1)
    assert choose_one.all_mandatory is False
    assert [child.referenced_group_id for child in choose_one.children] == [
        "BIO-1",
        "PROGRAMME:PROG-1:OFFERING:OFF-1",
    ]

    some_of = modules[f"PROGRAMME:PROG-1:OFFERING:OFF-1|{periods[1]}"].rule
    assert some_of.node_type is ModuleRuleType.CREDITS_RULE
    assert (some_of.credits_min, some_of.credits_max) == (2, 4)
    assert some_of.all_mandatory is None
    assert (some_of.require_min, some_of.require_max) == (None, None)
    some_pool = some_of.children[0]
    assert some_pool.node_type is ModuleRuleType.COMPOSITE_RULE
    assert [child.referenced_group_id for child in some_pool.children] == [
        "PROGRAMME:PROG-1:COURSE_UNIT_PART:PART-1"
    ]

    number_of = modules[f"PROGRAMME:PROG-1:STUDY_MODULE:MOD-3|{periods[1]}"].rule
    assert number_of.node_type is ModuleRuleType.CREDITS_RULE
    assert (number_of.credits_min, number_of.credits_max) == (12, 13)
    assert number_of.all_mandatory is None
    number_pool = number_of.children[0]
    assert number_pool.node_type is ModuleRuleType.COMPOSITE_RULE
    assert [child.referenced_group_id for child in number_pool.children] == [
        "BIO-1",
        "BIO-2-OFFICIAL",
    ]
    assert [child.node_type for child in number_pool.children] == [
        ModuleRuleType.COURSE_UNIT_RULE,
        ModuleRuleType.COURSE_UNIT_RULE,
    ]

    credits_node = modules[f"PROGRAMME:PROG-1:COURSE_UNIT_PART:PART-1|{periods[1]}"].rule
    assert credits_node.node_type is ModuleRuleType.CREDITS_RULE
    assert (credits_node.credits_min, credits_node.credits_max) == (1, 1)
    assert credits_node.all_mandatory is None
    part_pool = credits_node.children[0]
    assert part_pool.node_type is ModuleRuleType.COMPOSITE_RULE
    assert [child.referenced_group_id for child in part_pool.children] == ["BIO-1"]

    second_module = modules[f"PROGRAMME:PROG-1:STUDY_MODULE:MOD-2|{periods[1]}"]
    assert second_module.target_credits_min == 10
    assert second_module.target_credits_max == 15
    assert second_module.rule.node_type is ModuleRuleType.CREDITS_RULE
    second_pool = second_module.rule.children[0]
    assert second_pool.node_type is ModuleRuleType.COMPOSITE_RULE
    assert [child.referenced_group_id for child in second_pool.children] == [
        "BIO-2-OFFICIAL",
        "PROGRAMME:PROG-1:STUDY_MODULE:PROG-1",
    ]

    # A credit-bounded node is one credits rule over one non-mandatory selection pool.
    for pool in (some_pool, number_pool, part_pool, second_pool):
        assert (pool.require_min, pool.require_max) == (0, None)
        assert pool.all_mandatory is False


def test_peppi_legacy_parser_hashes_the_extracted_record() -> None:
    """A request-time value never enters the identity, and extracted edits change the hash."""
    parser = block_parser(two_period_block())
    body = course_body()

    def decode(value: dict[str, Any], period: str = PERIOD) -> Any:
        return parser.decode_objects(
            captured_course(value, period=period), SourceObjectType.COURSE_UNIT
        )[0]

    first = decode(body)
    later = deepcopy(body)
    later["createdAt"] = body["createdAt"] + 1000
    assert decode(later).source_hash == first.source_hash

    changed_content = deepcopy(body)
    changed_content["contentList"][2]["content"]["valueEn"] = "Changed structure."
    assert decode(changed_content).source_hash != first.source_hash

    changed_name = deepcopy(body)
    changed_name["name"]["valueEn"] = "Changed name"
    assert decode(changed_name).source_hash != first.source_hash

    assert {
        decode(body).source_object_id,
        decode(later).source_object_id,
        decode(changed_content).source_object_id,
        decode(changed_name).source_object_id,
    } == {"BIO-1|2026-2027"}
    assert decode(body, SECOND_PERIOD).source_object_id == "BIO-1|2024-2026"


_ABSENT = object()


def route_handler(
    routes: dict[str, object],
    calls: list[str],
    period_routes: dict[tuple[str, str], object] | None = None,
) -> Any:
    """Answer the declared routes, including any per-period answer, and fail on the unexpected."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        period = request.url.params.get("period")
        key = (path, period) if period is not None else None
        if period_routes is not None and key in period_routes:
            payload = period_routes[key]
        elif path in routes:
            payload = routes[path]
        else:
            raise AssertionError(f"unexpected Peppi request {path}")
        if payload is _ABSENT:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return handle


def capture_variant(
    tmp_path: Path,
    key: str,
    block: PeppiLegacySourceMetadata,
    routes: dict[str, object],
    calls: list[str],
    *,
    period_routes: dict[tuple[str, str], object] | None = None,
) -> RawCaptureStore:
    """Capture one configured institutional variant through a mock transport."""
    store = RawCaptureStore.create(
        tmp_path / key,
        run_id=uuid4(),
        started_at=datetime(2026, 11, 10, tzinfo=UTC),
        parser=block_parser(block, key),
    )
    settings = settings_factory(KoriSettings, INSTANCE_KEY=key)
    with PeppiLegacySource(
        settings,
        transport=httpx.MockTransport(route_handler(routes, calls, period_routes)),
        sleep=lambda _: None,
    ) as source:
        report = capture_curriculum(
            PeppiLegacyCapture(source, block),
            store,
            RecordingCapturePersistence(),
            checkpoint_response_count=100,
        )
    assert report is not None and report.status is IngestionRunStatus.COMPLETED
    return store


def test_peppi_legacy_capture_walks_each_discovery_variant(tmp_path: Path) -> None:
    """Every listed period is walked, a per-period 404 stays unresolved only when no period serves it."""
    education = {
        "id": "edu1",
        "children": [
            {"id": "plan1", "type": "PROGRAMME"},
            {
                "id": "edu2",
                "type": "EDUCATION",
                "children": [{"id": "plan2", "type": "PROGRAMME"}],
            },
        ],
    }
    repeated = {"id": "edu3", "children": [{"id": "plan1", "type": "PROGRAMME"}]}
    plans = {
        "/api/accomplishment-plan/plan1": plan_body_with_unresolved_course(),
        "/api/accomplishment-plan/plan2": [],
    }
    courses = {
        # The detail's official code differs from the plan code, so the plan code BIO-1 resolves
        # only through its recorded request key.
        "/api/course/c1": course_body("KKLT0014"),
        "/api/course/c2": course_body("BIO-2"),
        "/api/course/c3": _ABSENT,
        "/api/course/c4": course_body("BIO-4"),
    }

    turku_periods = ("2024-2026", "2026-2027")
    turku = ["/api/navigation"]
    for _ in turku_periods:
        turku += ["/api/organisation", "/api/organisation/units/org1"]
        turku += ["/api/education/org1/nav1", "/api/education/org1/nav2"]
        turku += ["/api/education/unit1/nav1", "/api/education/unit1/nav2"]
        turku += ["/api/education/unit2/nav1", "/api/education/unit2/nav2"]
        turku += ["/api/accomplishment-plan/plan1", "/api/accomplishment-plan/plan2"]
    for _ in turku_periods:
        turku += ["/api/course/c1", "/api/course/c2", "/api/course/c3", "/api/course/c4"]
    turku_routes = {
        "/api/navigation": [{"id": "nav1", "type": "code"}, {"id": "nav2", "type": "code"}],
        "/api/organisation": [
            {"id": "org1", "children": [{"id": "unit1", "children": [{"id": "unit2"}]}]}
        ],
        "/api/organisation/units/org1": [{"id": "unit1", "children": [{"id": "unit2"}]}],
        "/api/education/org1/nav1": [education],
        "/api/education/org1/nav2": [repeated],
        "/api/education/unit1/nav1": [],
        "/api/education/unit1/nav2": [],
        "/api/education/unit2/nav1": [],
        "/api/education/unit2/nav2": [],
        **plans,
        **courses,
    }
    # One code is absent in the oldest period and served in the later one, so it resolves.
    turku_period_routes = {("/api/course/c4", turku_periods[0]): _ABSENT}

    abo_periods = ("2024-2027",)
    abo = [
        "/api/navigation",
        "/api/education/nav1/education-type",
        "/api/education/nav2/education-type",
    ]
    abo += ["/api/accomplishment-plan/plan1", "/api/accomplishment-plan/plan2"]
    abo += ["/api/course/c1", "/api/course/c2", "/api/course/c3", "/api/course/c4"]
    abo_routes = {
        "/api/navigation": [{"id": "nav1", "type": "code"}, {"id": "nav2", "type": "code"}],
        "/api/education/nav1/education-type": [education],
        "/api/education/nav2/education-type": _ABSENT,
        **plans,
        **courses,
    }

    uniarts_periods = ("2024-2027",)
    uniarts = ["/api/navigation", "/api/education-type/13744"]
    uniarts += ["/api/education/13744/13642", "/api/education/13744/14032"]
    uniarts += ["/api/accomplishment-plan/plan1", "/api/accomplishment-plan/plan2"]
    uniarts += ["/api/course/c1", "/api/course/c2", "/api/course/c3", "/api/course/c4"]
    uniarts_routes = {
        "/api/navigation": [{"id": "13744", "type": "organisation_unit"}],
        "/api/education-type/13744": [{"id": "13642"}, {"id": "14032"}],
        "/api/education/13744/13642": [education],
        "/api/education/13744/14032": [],
        **plans,
        **courses,
    }

    for key, periods, routes, period_routes, expected in (
        (
            "turku",
            turku_periods,
            turku_routes,
            turku_period_routes,
            turku,
        ),
        (
            "abo",
            abo_periods,
            abo_routes,
            {},
            abo,
        ),
        (
            "uniarts",
            uniarts_periods,
            uniarts_routes,
            {},
            uniarts,
        ),
    ):
        block = two_period_block(key) if key == "turku" else single_period_block(key, periods[0])
        calls: list[str] = []
        store = capture_variant(tmp_path, key, block, routes, calls, period_routes=period_routes)
        assert Counter(calls) == Counter(expected)
        object_types = {
            (response.request_path, response.request_params): response.object_type
            for response in store.manifest.responses
        }
        assert object_types[("/api/navigation", ())] is None
        assert object_types[("/api/accomplishment-plan/plan1", (("period", periods[0]),))] is (
            SourceObjectType.MODULE
        )
        for period in periods:
            for code in ("c1", "c2"):
                assert object_types[(f"/api/course/{code}", (("period", period),))] is (
                    SourceObjectType.COURSE_UNIT
                )
            assert ("/api/course/c3", (("period", period),)) not in object_types
        assert store.manifest.unresolved_course_group_ids == ("BIO-3",)
        for response in store.manifest.responses:
            if response.request_path.startswith("/api/course/"):
                continue
            if response.request_path in {"/api/navigation", "/api/education-type/13744"}:
                assert response.request_params == ()
                continue
            assert response.request_params[0][0] == "period"
            assert response.request_params[0][1] in periods

    # A reopened two-period store re-walks discovery and fetches only the details it lacks.
    key = "turku"
    block = two_period_block(key)
    settings = settings_factory(KoriSettings, INSTANCE_KEY=key)
    raw_root = tmp_path / "resume"
    store = RawCaptureStore.create(
        raw_root,
        run_id=uuid4(),
        started_at=datetime(2026, 11, 10, tzinfo=UTC),
        parser=block_parser(block, key),
    )

    def pause_after_first_detail(stage: str, completed: int, total: int | None) -> bool:
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

    paused = False
    calls = []
    with PeppiLegacySource(
        settings,
        transport=httpx.MockTransport(route_handler(turku_routes, calls, turku_period_routes)),
        sleep=lambda _: None,
    ) as source:
        assert (
            capture_curriculum(
                PeppiLegacyCapture(source, block),
                store,
                RecordingCapturePersistence(),
                checkpoint_response_count=1,
                on_boundary=pause_after_first_detail,
            )
            is None
        )
    assert store.manifest.status is IngestionRunStatus.RUNNING
    assert [path for path in calls if path.startswith("/api/course/")] == ["/api/course/c1"]

    reopened = RawCaptureStore.open(raw_root, store.manifest_path, parser=block_parser(block, key))
    resumed_calls: list[str] = []
    with PeppiLegacySource(
        settings,
        transport=httpx.MockTransport(
            route_handler(turku_routes, resumed_calls, turku_period_routes)
        ),
        sleep=lambda _: None,
    ) as source:
        report = capture_curriculum(
            PeppiLegacyCapture(source, block),
            reopened,
            RecordingCapturePersistence(),
            checkpoint_response_count=100,
        )
    assert report is not None and report.status is IngestionRunStatus.COMPLETED
    # The plan-code space is the one unresolved key space: the resumed run resolves the recorded
    # code-change course through its request key and leaves only the genuinely absent plan code.
    assert reopened.manifest.unresolved_course_group_ids == ("BIO-3",)
    assert [path for path in resumed_calls if path.startswith("/api/course/")] == [
        "/api/course/c2",
        "/api/course/c3",
        "/api/course/c4",
        "/api/course/c1",
        "/api/course/c2",
        "/api/course/c3",
        "/api/course/c4",
    ]
