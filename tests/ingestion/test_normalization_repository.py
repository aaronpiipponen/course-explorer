"""Opt-in PostgreSQL proofs for transactional normalization and exact reruns."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from course_explorer.db.models import (
    Course,
    CourseVersion,
    CourseVersionCurriculumPeriod,
    CurriculumPeriod,
    IngestionRun,
    ModuleRuleNode,
    ModuleVersion,
    ModuleVersionCurriculumPeriod,
    OfficialCourseText,
    OfficialModuleText,
    OfficialPrerequisiteOption,
    OfficialPrerequisiteOptionMember,
    OfficialSubstitutionOption,
    OfficialSubstitutionOptionMember,
    SourceSnapshot,
)
from course_explorer.enums import OfficialTextField
from course_explorer.ingestion.importing import import_curriculum
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


class _CompletedStoreSource:
    """Source boundary that fails if a completed capture asks for further source work."""

    def capture_pending(self, *_args, **_kwargs):
        """Reject a capture call that a completed store must never require."""
        raise AssertionError("a completed capture must not ask the source for new responses")


def test_normalization_round_trip_is_idempotent_and_preserves_official_structure(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """Normalizing the same official structure twice is idempotent and preserves every row."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])
    store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        unresolved_course_group_ids=("typed-unresolved-course",),
        unresolved_module_group_ids=("typed-unresolved-module",),
        duplicate_first_course_response=True,
    )

    first = normalize_curriculum(
        store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert first.run_id == store.manifest.run_id
    with factory.begin() as session:
        membership_years = session.execute(
            select(CourseVersion.source_version_id, CurriculumPeriod.academic_year_start)
            .join(
                CourseVersionCurriculumPeriod,
                CourseVersionCurriculumPeriod.course_version_id == CourseVersion.id,
            )
            .join(
                CurriculumPeriod,
                CurriculumPeriod.id == CourseVersionCurriculumPeriod.curriculum_period_id,
            )
            .order_by(CourseVersion.source_version_id, CurriculumPeriod.academic_year_start)
        ).all()
        assert membership_years == [
            ("course-a-v2", 2026),
            ("course-a-v2", 2027),
            ("course-a-z1", 2025),
            ("course-a-z1", 2026),
            ("course-future-v1", 2027),
            ("course-inactive-v1", 2020),
            ("course-tie-a", 2020),
            ("course-tie-b", 2020),
        ]
        module_membership_years = session.execute(
            select(ModuleVersion.source_version_id, CurriculumPeriod.academic_year_start)
            .join(
                ModuleVersionCurriculumPeriod,
                ModuleVersionCurriculumPeriod.module_version_id == ModuleVersion.id,
            )
            .join(
                CurriculumPeriod,
                CurriculumPeriod.id == ModuleVersionCurriculumPeriod.curriculum_period_id,
            )
            .order_by(ModuleVersion.source_version_id, CurriculumPeriod.academic_year_start)
        ).all()
        assert module_membership_years == [
            ("module-group-degree-v1", 2026),
            ("module-group-grouping-v1", 2027),
            ("module-group-study-v1", 2025),
            ("module-group-study-v1", 2026),
        ]
        course_a = session.scalar(select(Course).where(Course.group_id == "course-group-a"))
        assert course_a is not None
        assert (course_a.activity_as_of_academic_year_start, course_a.activity_window_years) == (
            2026,
            3,
        )
        courses = {
            course.group_id: course
            for course in session.scalars(
                select(Course).where(
                    Course.group_id.in_(
                        (
                            "course-group-a",
                            "course-group-inactive",
                            "course-group-future",
                            "course-group-tie",
                        )
                    )
                )
            )
        }
        assert {
            group_id: (
                course.first_listed_academic_year_start,
                course.last_listed_academic_year_start,
                course.is_active,
                course.activity_as_of_academic_year_start,
                course.activity_window_years,
            )
            for group_id, course in courses.items()
        } == {
            "course-group-a": (2025, 2027, True, 2026, 3),
            "course-group-inactive": (2020, 2020, False, 2026, 3),
            "course-group-future": (2027, 2027, False, 2026, 3),
            "course-group-tie": (2020, 2020, False, 2026, 3),
        }
        prerequisite_options = session.scalars(
            select(OfficialPrerequisiteOption)
            .join(CourseVersion)
            .where(CourseVersion.source_version_id == "course-a-z1")
            .order_by(
                OfficialPrerequisiteOption.requirement_kind, OfficialPrerequisiteOption.option_index
            )
        ).all()
        assert [
            (option.requirement_kind.value, option.option_index) for option in prerequisite_options
        ] == [
            ("compulsory", 0),
            ("compulsory", 1),
            ("recommended", 0),
        ]
        prerequisite_members = session.scalars(
            select(OfficialPrerequisiteOptionMember).where(
                OfficialPrerequisiteOptionMember.prerequisite_option_id.in_(
                    [option.id for option in prerequisite_options]
                )
            )
        ).all()
        prerequisite_members = sorted(
            prerequisite_members,
            key=lambda member: (
                next(
                    index
                    for index, option in enumerate(prerequisite_options)
                    if option.id == member.prerequisite_option_id
                ),
                member.member_index,
            ),
        )
        assert [
            (member.member_index, member.member_type.value) for member in prerequisite_members
        ] == [
            (0, "course"),
            (1, "module"),
            (0, "course"),
            (0, "module"),
        ]
        substitutions = session.scalars(
            select(OfficialSubstitutionOption)
            .join(CourseVersion)
            .where(CourseVersion.source_version_id == "course-a-z1")
            .order_by(OfficialSubstitutionOption.option_index)
        ).all()
        assert [option.option_index for option in substitutions] == [0, 1]
        substitution_members = session.scalars(
            select(OfficialSubstitutionOptionMember).where(
                OfficialSubstitutionOptionMember.substitution_option_id.in_(
                    [option.id for option in substitutions]
                )
            )
        ).all()
        substitution_members = sorted(
            substitution_members,
            key=lambda member: (
                next(
                    index
                    for index, option in enumerate(substitutions)
                    if option.id == member.substitution_option_id
                ),
                member.member_index,
            ),
        )
        assert [member.credits for member in substitution_members] == [None, 2.5, 5]
        nodes = session.scalars(
            select(ModuleRuleNode)
            .join(ModuleVersion)
            .where(ModuleVersion.source_version_id == "module-group-grouping-v1")
            .order_by(ModuleRuleNode.parent_node_id, ModuleRuleNode.sibling_position)
        ).all()
        root = next(node for node in nodes if node.parent_node_id is None)
        assert root.sibling_position == 0
        assert [node.sibling_position for node in nodes if node.parent_node_id == root.id] == [
            0,
            1,
            2,
            3,
        ]
        credits_node = next(node for node in nodes if node.node_type.value == "CreditsRule")
        count_node = next(node for node in nodes if node.node_type.value == "CourseUnitCountRule")
        course_node = next(node for node in nodes if node.node_type.value == "CourseUnitRule")
        assert credits_node.parent_node_id == root.id and credits_node.sibling_position == 0
        assert count_node.parent_node_id == credits_node.id and count_node.sibling_position == 0
        assert course_node.parent_node_id == count_node.id and course_node.sibling_position == 0
        assert {node.node_type.value for node in nodes} == {
            "CompositeRule",
            "CreditsRule",
            "CourseUnitCountRule",
            "CourseUnitRule",
            "ModuleRule",
            "AnyCourseUnitRule",
            "AnyModuleRule",
        }
    first_digest = normalized_database_digest(factory)
    unchanged_store = build_completed_fixture_store(tmp_path, capture_repository)
    second = import_curriculum(
        source=_CompletedStoreSource(),
        store=unchanged_store,
        capture_repository=capture_repository,
        normalization_repository=normalization_repository,
        checkpoint_response_count=100,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert second is not None
    assert second.normalization.run_id == unchanged_store.manifest.run_id
    assert second.capture.status.value == "completed"

    assert first.new_course_version_count == 6
    assert first.new_module_version_count == 3
    assert first.active_course_count == 1
    assert first.unresolved_course_group_count == 1
    assert first.unresolved_module_group_count == 1
    assert first.unrestricted_rule_count == 2
    assert second.normalization.unchanged_course_version_count == 6
    assert second.normalization.unchanged_module_version_count == 3
    assert second.normalization.new_course_version_count == 0
    assert second.normalization.changed_course_version_count == 0
    assert normalized_database_digest(factory) == first_digest
    with factory.begin() as session:
        assert (
            session.scalar(
                select(func.count(SourceSnapshot.id)).where(
                    SourceSnapshot.ingestion_run_id == unchanged_store.manifest.run_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(IngestionRun.id).where(IngestionRun.id == unchanged_store.manifest.run_id)
            )
            is None
        )

    with factory.begin() as session:
        course = session.scalar(select(Course).where(Course.group_id == "course-group-a"))
        assert course is not None and course.is_active
        unbounded = session.scalar(
            select(CourseVersion.credits_max).where(
                CourseVersion.source_version_id == "course-a-v2"
            )
        )
        assert unbounded is None
        assert session.scalar(
            select(ModuleRuleNode).where(ModuleRuleNode.node_type == "AnyModuleRule")
        )


def test_normalization_replaces_changed_versions_and_reselects_the_representative(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """Changed source hashes rewrite dependent normalized rows and reselect the representative."""
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
        first_version_ids = {
            row.source_version_id: row.id for row in session.scalars(select(CourseVersion))
        }
        first_module_ids = {
            row.source_version_id: row.id for row in session.scalars(select(ModuleVersion))
        }
        unchanged_text_content = session.scalar(
            select(OfficialCourseText.text).where(
                OfficialCourseText.course_version_id == first_version_ids["course-a-v2"],
                OfficialCourseText.field == OfficialTextField.CONTENT,
                OfficialCourseText.language == "en",
            )
        )
        old_prerequisite_ids = set(
            session.scalars(
                select(OfficialPrerequisiteOption.id)
                .join(CourseVersion)
                .where(CourseVersion.source_version_id == "course-a-z1")
            )
        )
        old_rule_ids = set(
            session.scalars(
                select(ModuleRuleNode.id)
                .join(ModuleVersion)
                .where(ModuleVersion.source_version_id == "module-group-grouping-v1")
            )
        )

    changed_courses: list[dict[str, Any]] = copy.deepcopy(json.loads(fixture_bytes("courses.json")))
    changed_courses[0]["code"] = "CS.A1-CHANGED"
    changed_courses[0]["content"]["en"] = "Changed content"
    changed_courses[0]["metadata"]["modificationOrdinal"] = 10
    changed_courses.append(
        {
            **copy.deepcopy(changed_courses[1]),
            "id": "course-a-v3",
            "code": "CS.A3",
            "metadata": {"revision": 3, "modificationOrdinal": 11},
            "curriculumPeriodIds": ["period-2026"],
        }
    )
    changed_modules: list[dict[str, Any]] = copy.deepcopy(json.loads(fixture_bytes("modules.json")))
    changed_modules[2]["rule"]["rules"][2]["localId"] = "changed-any-course"
    changed_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(changed_courses, sort_keys=True, separators=(",", ":")).encode(),
        modules_body=json.dumps(changed_modules, sort_keys=True, separators=(",", ":")).encode(),
    )
    report = normalize_curriculum(
        changed_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )

    assert report.changed_course_version_count == 1
    assert report.new_course_version_count == 1
    assert report.changed_module_version_count == 1
    with factory.begin() as session:
        assert (
            session.scalar(
                select(CourseVersion.code).where(CourseVersion.source_version_id == "course-a-z1")
            )
            == "CS.A1-CHANGED"
        )
        assert session.scalar(
            select(CourseVersion.id).where(CourseVersion.source_version_id == "course-a-v3")
        )
        new_prerequisite_ids = set(
            session.scalars(
                select(OfficialPrerequisiteOption.id)
                .join(CourseVersion)
                .where(CourseVersion.source_version_id == "course-a-z1")
            )
        )
        assert new_prerequisite_ids.isdisjoint(old_prerequisite_ids)
        new_rule_ids = set(
            session.scalars(
                select(ModuleRuleNode.id)
                .join(ModuleVersion)
                .where(ModuleVersion.source_version_id == "module-group-grouping-v1")
            )
        )
        assert new_rule_ids.isdisjoint(old_rule_ids)
        assert (
            session.scalar(
                select(ModuleRuleNode.local_id).where(
                    ModuleRuleNode.local_id == "changed-any-course"
                )
            )
            == "changed-any-course"
        )
        surviving_snapshot_id = session.scalar(
            select(CourseVersion.source_snapshot_id).where(
                CourseVersion.source_version_id == "course-a-z1"
            )
        )
        assert surviving_snapshot_id is not None
        surviving_snapshot = session.get(SourceSnapshot, surviving_snapshot_id)
        assert surviving_snapshot is not None
        assert surviving_snapshot.source_object_id == "course-a-z1"
        assert (
            session.scalar(
                select(func.count(SourceSnapshot.id)).where(
                    SourceSnapshot.source_object_id == "course-a-z1"
                )
            )
            == 1
        )

        # Exact versions and module versions keep their exact primary keys. Only the
        # changed course-a-z1 and the added course-a-v3 differ.
        assert {
            row.source_version_id: row.id
            for row in session.scalars(
                select(CourseVersion).where(
                    CourseVersion.source_version_id.notin_(("course-a-z1", "course-a-v3"))
                )
            )
        } == {
            source_id: version_id
            for source_id, version_id in first_version_ids.items()
            if source_id != "course-a-z1"
        }
        assert {
            row.source_version_id: row.id
            for row in session.scalars(
                select(ModuleVersion).where(
                    ModuleVersion.source_version_id != "module-group-grouping-v1"
                )
            )
        } == {
            source_id: version_id
            for source_id, version_id in first_module_ids.items()
            if source_id != "module-group-grouping-v1"
        }
        # The untouched exact course keeps its exact text content, while the changed version's
        # dependent payload is rewritten with this fixture's content.
        assert (
            session.scalar(
                select(OfficialCourseText.text).where(
                    OfficialCourseText.course_version_id == first_version_ids["course-a-v2"],
                    OfficialCourseText.field == OfficialTextField.CONTENT,
                    OfficialCourseText.language == "en",
                )
            )
            == unchanged_text_content
        )
        assert (
            session.scalar(
                select(OfficialCourseText.text).where(
                    OfficialCourseText.course_version_id == first_version_ids["course-a-z1"],
                    OfficialCourseText.field == OfficialTextField.CONTENT,
                    OfficialCourseText.language == "en",
                )
            )
            == "Changed content"
        )


def _reconciliation_course(
    source_id: str, group_id: str, code: str, period_ids: list[str]
) -> dict[str, Any]:
    """Build one Sisu-shaped course record for the reconciliation fixture."""
    return {
        "id": source_id,
        "groupId": group_id,
        "code": code,
        "name": {"fi": f"Nimi {code}", "en": f"Name {code}"},
        "credits": {"min": 5, "max": None},
        "curriculumPeriodIds": period_ids,
        "documentState": "ACTIVE",
        "validityPeriod": {"startDate": None, "endDate": None},
        "metadata": {"revision": 1, "modificationOrdinal": 1},
        "content": None,
        "outcomes": None,
        "prerequisites": None,
        "additional": None,
        "compulsoryFormalPrerequisites": None,
        "recommendedFormalPrerequisites": None,
        "substitutions": None,
    }


def _reconciliation_module(source_id: str, group_id: str, code: str) -> dict[str, Any]:
    """Build one Sisu-shaped module record for the reconciliation fixture."""
    return {
        "id": source_id,
        "groupId": group_id,
        "type": "StudyModule",
        "code": code,
        "name": {"fi": f"Moduuli {code}", "en": f"Module {code}"},
        "targetCredits": {"min": 20, "max": None},
        "curriculumPeriodIds": ["period-2026"],
        "documentState": "ACTIVE",
        "validityPeriod": {"startDate": None, "endDate": None},
        "metadata": {"revision": 1, "modificationOrdinal": 1},
        "rule": None,
    }


def test_completed_import_reconciles_only_covered_period_listings(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """A complete import replaces covered listings and keeps uncovered history and stored rows."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=UNIVERSITIES["example"])
    kept_course = _reconciliation_course(
        "recon-course-v1", "recon-course", "RECON.1", ["period-2020", "period-2026"]
    )
    dropped_course = _reconciliation_course(
        "recon-dropped-v1", "recon-dropped", "RECON.2", ["period-2026"]
    )
    first_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(
            [kept_course, dropped_course], sort_keys=True, separators=(",", ":")
        ).encode(),
        modules_body=json.dumps(
            [_reconciliation_module("recon-module-v1", "recon-module", "RECON.M")],
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    normalize_curriculum(
        first_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )

    dropped_from_coverage = _reconciliation_course(
        "recon-course-v1", "recon-course", "RECON.1", ["period-2020"]
    )
    second_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(
            [dropped_from_coverage], sort_keys=True, separators=(",", ":")
        ).encode(),
        modules_body=b"[]",
    )
    normalize_curriculum(
        second_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )

    with factory.begin() as session:
        memberships = session.execute(
            select(CourseVersion.source_version_id, CurriculumPeriod.id)
            .join(
                CourseVersionCurriculumPeriod,
                CourseVersionCurriculumPeriod.course_version_id == CourseVersion.id,
            )
            .join(
                CurriculumPeriod,
                CurriculumPeriod.id == CourseVersionCurriculumPeriod.curriculum_period_id,
            )
            .order_by(CourseVersion.source_version_id, CurriculumPeriod.id)
        ).all()
        assert memberships == [("recon-course-v1", "period-2020")]
        module_memberships = session.execute(
            select(ModuleVersion.source_version_id, CurriculumPeriod.id)
            .join(
                ModuleVersionCurriculumPeriod,
                ModuleVersionCurriculumPeriod.module_version_id == ModuleVersion.id,
            )
            .join(
                CurriculumPeriod,
                CurriculumPeriod.id == ModuleVersionCurriculumPeriod.curriculum_period_id,
            )
        ).all()
        assert module_memberships == []
        dropped = session.scalar(select(Course).where(Course.group_id == "recon-dropped"))
        assert dropped is not None
        assert dropped.is_active is False
        assert (
            session.scalar(
                select(CourseVersion.id).where(
                    CourseVersion.source_version_id == "recon-dropped-v1"
                )
            )
            is not None
        )
        module = session.scalar(
            select(ModuleVersion).where(ModuleVersion.source_version_id == "recon-module-v1")
        )
        assert module is not None


def test_official_cells_follow_order_only_for_written_payloads(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """Configured order and duplicate suppression apply only to payloads that are first written or
    changed at the source.
    """
    _, factory = database
    hanken = UNIVERSITIES["hanken"]
    capture_repository = CaptureRepository(factory)
    normalization_repository = NormalizationRepository(factory, university=hanken)
    course = {
        "id": "dup-course-v1",
        "groupId": "dup-course",
        "code": "DUP.1",
        "name": {"sv": "Sama", "fi": "Sama", "en": "English", "de": "Deutsch"},
        "credits": {"min": 5, "max": 5},
        "curriculumPeriodIds": ["period-2026"],
        "documentState": "ACTIVE",
        "validityPeriod": {"startDate": None, "endDate": None},
        "metadata": {"revision": 1, "modificationOrdinal": 1},
        "content": {"sv": "Svensk", "fi": "Suomi", "de": "Deutsch"},
        "outcomes": None,
        "prerequisites": None,
        "additional": None,
        "compulsoryFormalPrerequisites": None,
        "recommendedFormalPrerequisites": None,
        "substitutions": None,
    }
    module = {
        "id": "dup-module-v1",
        "groupId": "dup-module",
        "type": "StudyModule",
        "code": "DUP.M",
        "name": {"sv": "Mod", "fi": "Mod", "en": "Modul", "de": "Deutsch"},
        "targetCredits": {"min": 5, "max": 5},
        "curriculumPeriodIds": ["period-2026"],
        "documentState": "ACTIVE",
        "validityPeriod": {"startDate": None, "endDate": None},
        "metadata": {"revision": 1, "modificationOrdinal": 1},
        "rule": None,
    }

    def bodies(course_record: dict[str, Any], module_record: dict[str, Any]) -> tuple[bytes, bytes]:
        return (
            json.dumps([course_record], ensure_ascii=False).encode(),
            json.dumps([module_record], ensure_ascii=False).encode(),
        )

    def course_cells() -> dict[tuple[str, str], str]:
        with factory.begin() as session:
            return {
                (field.value, language): text
                for field, language, text in session.execute(
                    select(
                        OfficialCourseText.field,
                        OfficialCourseText.language,
                        OfficialCourseText.text,
                    )
                    .join(CourseVersion, CourseVersion.id == OfficialCourseText.course_version_id)
                    .where(CourseVersion.source_version_id == "dup-course-v1")
                ).all()
            }

    def module_cells() -> dict[str, str]:
        with factory.begin() as session:
            return {
                language: text
                for language, text in session.execute(
                    select(OfficialModuleText.language, OfficialModuleText.text)
                    .join(
                        ModuleVersion,
                        ModuleVersion.id == OfficialModuleText.module_version_id,
                    )
                    .where(ModuleVersion.source_version_id == "dup-module-v1")
                ).all()
            }

    course_body, module_body = bodies(course, module)
    first_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=course_body,
        modules_body=module_body,
    )
    normalize_curriculum(
        first_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert course_cells() == {
        ("name", "sv"): "Sama",
        ("name", "en"): "English",
        ("content", "sv"): "Svensk",
        ("content", "fi"): "Suomi",
    }
    assert module_cells() == {"sv": "Mod", "en": "Modul"}

    # A retained migrated duplicate is not repaired by a hash-equal completed reimport.
    with factory.begin() as session:
        course_version_id = session.scalar(
            select(CourseVersion.id).where(CourseVersion.source_version_id == "dup-course-v1")
        )
        module_version_id = session.scalar(
            select(ModuleVersion.id).where(ModuleVersion.source_version_id == "dup-module-v1")
        )
        assert course_version_id is not None and module_version_id is not None
        session.add(
            OfficialCourseText(
                course_version_id=course_version_id,
                field=OfficialTextField.NAME,
                language="fi",
                text="Sama",
            )
        )
        session.add(
            OfficialModuleText(
                module_version_id=module_version_id,
                field="name",
                language="fi",
                text="Mod",
            )
        )
    equal_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=course_body,
        modules_body=module_body,
    )
    normalize_curriculum(
        equal_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert course_cells()[("name", "fi")] == "Sama"
    assert module_cells()["fi"] == "Mod"

    # A genuine source change deletes the retained duplicate and applies the write policy again.
    changed_course = {**course, "code": "DUP.1-CHANGED"}
    changed_module = {**module, "code": "DUP.M-CHANGED"}
    changed_store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=bodies(changed_course, changed_module)[0],
        modules_body=bodies(changed_course, changed_module)[1],
    )
    normalize_curriculum(
        changed_store,
        normalization_repository,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert course_cells() == {
        ("name", "sv"): "Sama",
        ("name", "en"): "English",
        ("content", "sv"): "Svensk",
        ("content", "fi"): "Suomi",
    }
    assert module_cells() == {"sv": "Mod", "en": "Modul"}
