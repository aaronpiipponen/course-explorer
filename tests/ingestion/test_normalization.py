"""Source-shaped normalization and closure validation tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pytest
from pydantic import AnyHttpUrl, TypeAdapter

from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.normalization import (
    parse_capture,
)
from course_explorer.ingestion.sisu_normalization import SisuParser
from course_explorer.ingestion.source import CurriculumParser, CurriculumPeriodRef
from tests.ingestion._capture_support import SISU_PARSER
from tests.ingestion._normalization_repository_support import (
    fixture_bytes,
    fixture_response_inputs,
)


def build_store(
    courses: bytes | None = None,
    modules: bytes | None = None,
    *,
    unresolved_courses: tuple[str, ...] = (),
    unresolved_modules: tuple[str, ...] = (),
    parser: CurriculumParser = SISU_PARSER,
) -> tuple[TemporaryDirectory[str], RawCaptureStore]:
    """Build a completed immutable fixture store without database or network access."""
    directory = TemporaryDirectory()
    store = RawCaptureStore.create(
        Path(directory.name) / "raw",
        run_id=uuid4(),
        started_at=datetime(2026, 9, 11, tzinfo=UTC),
        parser=parser,
    )
    courses = fixture_bytes("courses.json") if courses is None else courses
    modules = fixture_bytes("modules.json") if modules is None else modules
    store.checkpoint_responses(
        fixture_response_inputs(courses, modules),
        unresolved_course_group_ids=unresolved_courses,
        unresolved_module_group_ids=unresolved_modules,
    )
    store.set_periods((CurriculumPeriodRef("period-2026", 2026),))
    store.mark_completed()
    return directory, store


def compact(value: Any) -> bytes:
    """Serialize mutated fixture data with capture's stable JSON representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_parse_capture_treats_blank_optional_module_code_as_absent() -> None:
    """Public grouping-module records may encode their absent code as an empty string."""
    modules = json.loads(fixture_bytes("modules.json"))
    modules[2]["code"] = ""
    directory, store = build_store(modules=compact(modules))
    try:
        batch = parse_capture(store)
    finally:
        directory.cleanup()

    assert batch.modules[2].code is None


def test_parse_capture_rejects_missing_or_blank_course_code() -> None:
    """Only explicit null is authorized. A missing course code remains malformed."""
    courses = json.loads(fixture_bytes("courses.json"))
    course = next(course for course in courses if course["id"] == "course-inactive-v1")
    del course["code"]
    directory, store = build_store(courses=compact(courses))
    try:
        with pytest.raises(ValueError):
            parse_capture(store)
    finally:
        directory.cleanup()


def test_parse_capture_rejects_noncanonical_guide_base_and_quotes_source_id() -> None:
    """Direct parser calls enforce canonical guide URLs and quote exact source IDs."""
    aalto_guide = TypeAdapter(AnyHttpUrl).validate_python(
        "https://sisu.aalto.fi/student/courseunit"
    )
    courses = json.loads(fixture_bytes("courses.json"))
    courses[0]["id"] = "course/a-v1"
    directory, store = build_store(compact(courses), parser=SisuParser(aalto_guide))
    try:
        batch = parse_capture(store)
    finally:
        directory.cleanup()

    assert batch.courses[0].official_url == (
        "https://sisu.aalto.fi/student/courseunit/course%2Fa-v1/brochure"
    )

    hostile_directory, hostile_store = build_store()
    try:
        with pytest.raises(ValueError):
            SisuParser("https://evil.example/path")
    finally:
        hostile_directory.cleanup()


def test_parse_capture_deduplicates_snapshots_and_drops_unpublished_memberships() -> None:
    """Repeated raw objects normalize once and memberships outside the published periods drop."""
    courses = json.loads(fixture_bytes("courses.json"))
    modules = json.loads(fixture_bytes("modules.json"))
    courses[0]["curriculumPeriodIds"] = ["missing-period", "period-2026"]
    modules[1]["curriculumPeriodIds"] = ["missing-period"]
    courses.append(courses[0])
    directory, store = build_store(
        compact(courses),
        compact(modules),
        unresolved_courses=("typed-unresolved-course",),
        unresolved_modules=("typed-unresolved-module",),
    )
    try:
        batch = parse_capture(store)
    finally:
        directory.cleanup()

    assert batch.courses[0].curriculum_period_ids == ("period-2026",)
    assert batch.modules[1].curriculum_period_ids == ()
    assert batch.dropped_period_membership_count == 2
    assert batch.unresolved_course_group_ids == ("typed-unresolved-course",)
    assert batch.unresolved_module_group_ids == ("typed-unresolved-module",)


@pytest.mark.parametrize(
    "mutation",
    ["unknown_rule", "absent_reference"],
)
def test_parse_capture_rejects_malformed_or_incomplete_official_evidence_before_write(
    mutation: str,
) -> None:
    """Malformed semantic evidence and incomplete typed closure fail before persistence."""
    courses = json.loads(fixture_bytes("courses.json"))
    modules = json.loads(fixture_bytes("modules.json"))
    if mutation == "unknown_rule":
        modules[2]["rule"]["rules"][0]["rule"]["rule"]["type"] = "UnsupportedRule"
    else:
        courses[0]["compulsoryFormalPrerequisites"][0]["prerequisites"][0]["courseUnitGroupId"] = (
            "absent-course"
        )

    directory, store = build_store(
        compact(courses),
        compact(modules),
    )
    try:
        with pytest.raises(ValueError):
            parse_capture(store)
    finally:
        directory.cleanup()
