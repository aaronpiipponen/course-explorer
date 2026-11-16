"""Opt-in PostgreSQL proofs for null-code exclusion and normalization rollback recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from course_explorer.curriculum_repository import CurriculumRepository
from course_explorer.db.models import (
    Course,
    CourseVersion,
    IngestionRun,
    ModuleRuleNode,
    OfficialCourseText,
    SourceSnapshot,
)
from course_explorer.ingestion.normalization import normalize_curriculum
from course_explorer.ingestion.normalization_repository import NormalizationRepository
from course_explorer.ingestion.repository import CaptureRepository
from course_explorer.instance import UNIVERSITIES
from tests.ingestion._normalization_repository_support import (
    build_completed_fixture_store,
    fixture_bytes,
    normalized_database_digest,
)

pytestmark = [
    pytest.mark.integration,
]


def test_normalization_removes_a_normalized_version_when_code_becomes_null(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """A coded sole version is excluded when its refreshed source code becomes explicitly null."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])
    first_store = build_completed_fixture_store(tmp_path, capture_repository)
    normalize_curriculum(
        first_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )

    refreshed_courses = json.loads(fixture_bytes("courses.json"))
    next(course for course in refreshed_courses if course["id"] == "course-inactive-v1")["code"] = (
        None
    )
    second_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(refreshed_courses, sort_keys=True, separators=(",", ":")).encode(),
    )
    second = normalize_curriculum(
        second_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    exclusion = second.excluded_null_code_course_versions[0]
    assert exclusion.source_object_id == "course-inactive-v1"
    with factory.begin() as session:
        assert (
            session.scalar(
                select(CourseVersion.id).where(
                    CourseVersion.source_version_id == exclusion.source_object_id
                )
            )
            is None
        )
        assert (
            session.scalar(select(Course).where(Course.group_id == exclusion.source_group_id))
            is None
        )
        assert (
            session.scalar(
                select(func.count(SourceSnapshot.id)).where(
                    SourceSnapshot.source_object_id == exclusion.source_object_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(ModuleRuleNode).where(
                    ModuleRuleNode.referenced_group_id == exclusion.source_group_id
                )
            )
            is not None
        )


def test_normalization_removes_only_null_code_version_and_keeps_coded_sibling(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """Removing one exact version retains its canonical coded sibling and representative."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])
    first_store = build_completed_fixture_store(tmp_path, capture_repository)
    normalize_curriculum(
        first_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    with factory.begin() as session:
        old_version_id = session.scalar(
            select(CourseVersion.id).where(CourseVersion.source_version_id == "course-a-z1")
        )
        assert old_version_id is not None

    refreshed_courses = json.loads(fixture_bytes("courses.json"))
    next(course for course in refreshed_courses if course["id"] == "course-a-z1")["code"] = None
    second_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(refreshed_courses, sort_keys=True, separators=(",", ":")).encode(),
    )
    second = normalize_curriculum(
        second_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert [item.source_object_id for item in second.excluded_null_code_course_versions] == [
        "course-a-z1"
    ]
    with factory.begin() as session:
        course_a = session.scalar(select(Course).where(Course.group_id == "course-group-a"))
        assert course_a is not None
        assert (
            session.scalar(
                select(CourseVersion.id).where(CourseVersion.source_version_id == "course-a-z1")
            )
            is None
        )
        assert (
            session.scalar(
                select(CourseVersion.id).where(CourseVersion.source_version_id == "course-a-v2")
            )
            is not None
        )
        assert (
            session.scalar(
                select(OfficialCourseText).where(
                    OfficialCourseText.course_version_id == old_version_id
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(func.count(SourceSnapshot.id)).where(
                    SourceSnapshot.source_object_id == "course-a-z1"
                )
            )
            == 0
        )

    course = next(
        item
        for item in CurriculumRepository(factory, university=UNIVERSITIES["example"]).list_courses()
        if item.group_id == "course-group-a"
    )
    assert course.representative is not None
    assert course.representative.source_version_id == "course-a-v2"


def test_null_code_exclusion_rejects_a_canonical_identity_conflict(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """A null refresh cannot relink an exact source version to another group."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])
    first_store = build_completed_fixture_store(tmp_path, capture_repository)
    normalize_curriculum(
        first_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    before = normalized_database_digest(factory)

    conflicting_courses = json.loads(fixture_bytes("courses.json"))
    conflicting = next(
        course for course in conflicting_courses if course["id"] == "course-inactive-v1"
    )
    conflicting["code"] = None
    conflicting["groupId"] = "conflicting-canonical-group"
    conflict_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(
            conflicting_courses, sort_keys=True, separators=(",", ":")
        ).encode(),
        unresolved_course_group_ids=("course-group-inactive",),
    )

    with pytest.raises(ValueError):
        normalize_curriculum(
            conflict_store,
            normalization_repository,
            current_academic_year_start=2026,
            activity_window_years=3,
        )
    assert normalized_database_digest(factory) == before


def test_normalization_identity_conflict_rolls_back(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """A canonical identity conflict aborts the complete batch without partial rows."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])
    first_store = build_completed_fixture_store(tmp_path, capture_repository)
    normalize_curriculum(
        first_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    before = normalized_database_digest(factory)
    conflict_courses = json.loads(fixture_bytes("courses.json"))
    conflict_courses[0]["groupId"] = "conflicting-canonical-group"
    conflict_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(conflict_courses, sort_keys=True, separators=(",", ":")).encode(),
    )

    with pytest.raises(ValueError):
        normalize_curriculum(
            conflict_store,
            normalization_repository,
            current_academic_year_start=2026,
            activity_window_years=3,
        )
    assert normalized_database_digest(factory) == before
    with factory.begin() as session:
        assert (
            session.scalar(select(Course).where(Course.group_id == "conflicting-canonical-group"))
            is None
        )
        assert session.scalar(
            select(IngestionRun).where(IngestionRun.id == conflict_store.manifest.run_id)
        )


def test_normalization_excludes_null_code_course_and_reconciles_raw_provenance(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """Null-code exclusion keeps raw capture evidence without an unused database snapshot."""
    _, factory = database
    courses = json.loads(fixture_bytes("courses.json"))
    next(course for course in courses if course["id"] == "course-inactive-v1")["code"] = None
    courses_body = json.dumps(courses, sort_keys=True, separators=(",", ":")).encode()
    capture_repository = CaptureRepository(factory)
    store = build_completed_fixture_store(tmp_path, capture_repository, courses_body=courses_body)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])

    report = normalize_curriculum(
        store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    exclusions = report.excluded_null_code_course_versions
    assert len(exclusions) == 1
    exclusion = exclusions[0]
    assert (store.run_root / exclusion.response_path).is_file()
    with factory.begin() as session:
        snapshot = session.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.ingestion_run_id == store.manifest.run_id,
                SourceSnapshot.response_path == exclusion.response_path,
                SourceSnapshot.response_item_index == exclusion.response_item_index,
            )
        )
        assert snapshot is None
        assert (
            session.scalar(
                select(CourseVersion.id).where(
                    CourseVersion.source_version_id == exclusion.source_object_id
                )
            )
            is None
        )
        assert (
            session.scalar(select(Course).where(Course.group_id == exclusion.source_group_id))
            is None
        )
        assert (
            session.scalar(
                select(ModuleRuleNode).where(
                    ModuleRuleNode.referenced_group_id == exclusion.source_group_id
                )
            )
            is not None
        )
