"""Opt-in PostgreSQL proofs for derived translation persistence and effective-text resolution."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import Engine, event, select

from course_explorer.curriculum_repository import CurriculumRepository
from course_explorer.db.models import (
    Course,
    CourseVersion,
    CourseVersionCurriculumPeriod,
    DerivedCourseText,
    OfficialCourseText,
    SourceSnapshot,
)
from course_explorer.db.session import session_scope
from course_explorer.enums import (
    OfficialTextField,
    SourceObjectType,
    TeachingTextField,
    TranslationMethod,
)
from course_explorer.instance import UNIVERSITIES
from course_explorer.teaching_text.repository import DerivedTranslationRepository
from course_explorer.teaching_text.service import TeachingTextService, teaching_text_sha256
from tests.teaching_text._repository_support import (
    manual_input,
    seed_representative_catalogue,
)

pytestmark = [
    pytest.mark.integration,
]


def test_repository_upserts_reads_and_updates_self_written_translation(
    database: tuple[Engine, object],
) -> None:
    """Imports insert once, a rerun is a no-op, and update only changed translated text."""
    active_source, _ = seed_representative_catalogue(database)
    _, factory = database
    repository = DerivedTranslationRepository(factory, university=UNIVERSITIES["hanken"])
    first = manual_input(
        active_source,
        TeachingTextField.CONTENT,
        "en",
        "sv",
        "Matriser",
        "Matrices",
    )
    second = manual_input(
        active_source,
        TeachingTextField.OUTCOMES,
        "fi",
        "en",
        "Use matrices",
        "Käytä matriiseja",
    )

    report = repository.upsert((first, second))
    assert (report.requested_count, report.inserted_count, report.updated_count) == (2, 2, 0)
    assert report.unchanged_count == 0

    with session_scope(factory) as session:
        rows = list(session.scalars(select(DerivedCourseText).order_by(DerivedCourseText.field)))
        identity = {row.field: row.id for row in rows}
        active_version_id = rows[0].course_version_id
        official_before = tuple(
            session.scalars(
                select(OfficialCourseText.text)
                .where(OfficialCourseText.course_version_id == active_version_id)
                .order_by(OfficialCourseText.field, OfficialCourseText.language)
            )
        )
        assert len(rows) == 2
        assert rows[0].language == "en"
        assert rows[0].source_language == "sv"
        assert rows[0].source_text_hash == teaching_text_sha256("Matriser")
        assert rows[1].source_text_hash == teaching_text_sha256("Use matrices")

    assert repository.upsert((first, second)).unchanged_count == 2

    changed = manual_input(
        active_source,
        TeachingTextField.CONTENT,
        "en",
        "sv",
        "Matriser",
        "Matrices revisited",
    )
    report = repository.upsert((changed, second))
    assert (report.requested_count, report.inserted_count, report.updated_count) == (2, 0, 1)
    assert report.unchanged_count == 1

    with session_scope(factory) as session:
        rows = list(session.scalars(select(DerivedCourseText)))
        assert len(rows) == 2
        assert {row.field: row.id for row in rows} == identity
        persisted = next(row for row in rows if row.field is TeachingTextField.CONTENT)
        assert persisted.text == "Matrices revisited"
        assert (
            tuple(
                session.scalars(
                    select(OfficialCourseText.text)
                    .where(OfficialCourseText.course_version_id == active_version_id)
                    .order_by(OfficialCourseText.field, OfficialCourseText.language)
                )
            )
            == official_before
        )

    read = repository.list_for_course_versions((active_source,))
    assert [(row.field, row.language, row.text) for row in read] == [
        (TeachingTextField.CONTENT, "en", "Matrices revisited"),
        (TeachingTextField.OUTCOMES, "fi", "Käytä matriiseja"),
    ]
    assert read[0].source_text_hash == teaching_text_sha256("Matriser")
    assert read[0].translation_method is TranslationMethod.MANUAL
    assert read[0].translation_model is None
    assert repository.list_for_course_versions(()) == ()


def test_repository_rejects_unknown_nonrepresentative_official_target_and_wrong_source(
    database: tuple[Engine, object],
) -> None:
    """Unknown, persisted non-selected, official-target, and mismatched-source inputs all fail."""
    seed_representative_catalogue(database)
    _, factory = database
    repository = DerivedTranslationRepository(factory)

    with session_scope(factory) as session:
        active_course_id = session.scalar(
            select(Course.id).where(Course.group_id == "course-active")
        )
        active_run_id = session.scalar(
            select(SourceSnapshot.ingestion_run_id)
            .join(CourseVersion, CourseVersion.source_snapshot_id == SourceSnapshot.id)
            .where(CourseVersion.source_version_id == "course-active-2026")
        )
        older_snapshot_id = uuid.uuid4()
        session.add(
            SourceSnapshot(
                id=older_snapshot_id,
                ingestion_run_id=active_run_id,
                object_type=SourceObjectType.COURSE_UNIT,
                source_object_id="course-active",
                source_group_id="course-active-2025",
                response_path="courses/teaching-text.json",
                response_item_index=4,
                source_hash="b" * 64,
            )
        )
        session.flush()
        older_version_id = uuid.uuid4()
        session.add(
            CourseVersion(
                id=older_version_id,
                course_id=active_course_id,
                source_version_id="course-active-2025",
                code="CODE.0",
                credits_min=5,
                credits_max=5,
                official_url="https://example.test/course",
                document_state="ARCHIVED",
                source_snapshot_id=older_snapshot_id,
            )
        )
        session.flush()
        session.add(
            OfficialCourseText(
                course_version_id=older_version_id,
                field=OfficialTextField.NAME,
                language="fi",
                text="Lineaarialgebra",
            )
        )
        session.add(
            CourseVersionCurriculumPeriod(
                course_version_id=older_version_id, curriculum_period_id="period-2025"
            )
        )

    with session_scope(factory) as session:
        assert (
            session.scalar(
                select(CourseVersion.source_version_id).where(
                    CourseVersion.source_version_id == "course-active-2025"
                )
            )
            == "course-active-2025"
        )

    def assert_no_rows() -> None:
        with session_scope(factory) as session:
            assert session.scalar(select(DerivedCourseText.id)) is None

    with pytest.raises(ValueError):
        repository.upsert(
            (
                manual_input(
                    "missing-version",
                    TeachingTextField.NAME,
                    "en",
                    "fi",
                    "Lineaarialgebra",
                    "Linear algebra",
                ),
            )
        )
    assert_no_rows()

    with pytest.raises(ValueError):
        repository.upsert(
            (
                manual_input(
                    "course-active-2025",
                    TeachingTextField.NAME,
                    "en",
                    "fi",
                    "Lineaarialgebra",
                    "Linear algebra",
                ),
            )
        )
    assert_no_rows()

    with pytest.raises(ValueError):
        repository.upsert(
            (
                manual_input(
                    "course-active-2026",
                    TeachingTextField.NAME,
                    "fi",
                    "en",
                    "Linear algebra",
                    "Lineaarialgebra",
                ),
            )
        )
    assert_no_rows()

    with pytest.raises(ValueError):
        repository.upsert(
            (
                manual_input(
                    "course-active-2026",
                    TeachingTextField.OUTCOMES,
                    "fi",
                    "en",
                    "Use matrices",
                    "Käytä matriiseja",
                ),
                manual_input(
                    "course-active-2026",
                    TeachingTextField.CONTENT,
                    "en",
                    "fi",
                    "Wrong matrices",
                    "Matrices",
                ),
            )
        )
    assert_no_rows()

    # An unconfigured target language is refused before any row is written.
    with pytest.raises(ValueError):
        repository.upsert(
            (
                manual_input(
                    "course-active-2026",
                    TeachingTextField.NAME,
                    "de",
                    "fi",
                    "Lineaarialgebra",
                    "Lineare Algebra",
                ),
            )
        )
    assert_no_rows()

    # An unmapped configured target is refused even when its source is official.
    with pytest.raises(ValueError):
        repository.upsert(
            (
                manual_input(
                    "course-active-2026",
                    TeachingTextField.NAME,
                    "sv",
                    "fi",
                    "Lineaarialgebra",
                    "Linjär algebra",
                ),
            )
        )
    assert_no_rows()

    # A configured source that the target's priority list omits is an unaccepted pair.
    unlisted = replace(
        UNIVERSITIES["hanken"],
        translation_priority={"fi": ("en",), "en": ("fi",)},
    )
    with pytest.raises(ValueError):
        DerivedTranslationRepository(factory, university=unlisted).upsert(
            (
                manual_input(
                    "course-active-2026",
                    TeachingTextField.NAME,
                    "fi",
                    "sv",
                    "Linjär algebra",
                    "Lineaarialgebra",
                ),
            )
        )
    assert_no_rows()


def test_real_repository_and_service_resolve_staleness_and_coverage(
    database: tuple[Engine, object],
) -> None:
    """Real seams resolve valid translations, then report and omit a stale cell."""
    active_source, empty_source = seed_representative_catalogue(database)
    engine, factory = database
    repository = DerivedTranslationRepository(factory)
    repository.upsert(
        (
            manual_input(
                active_source,
                TeachingTextField.CONTENT,
                "en",
                "fi",
                "Matriisit",
                "Matrices",
            ),
            manual_input(
                active_source,
                TeachingTextField.OUTCOMES,
                "fi",
                "en",
                "Use matrices",
                "Käytä matriiseja",
            ),
            manual_input(
                "course-inactive-2020",
                TeachingTextField.NAME,
                "fi",
                "en",
                "Programming",
                "Ohjelmointi",
            ),
        )
    )
    service = TeachingTextService(
        CurriculumRepository(factory, university=UNIVERSITIES["example"]), repository
    )

    catalogue = service.build_catalogue()
    coverage = catalogue.coverage
    assert catalogue.canonical_group_ids == (
        "course-active",
        "course-empty",
        "course-inactive",
        "course-no-period",
    )
    assert coverage.unrepresented_group_ids == ()
    active_hash = next(
        course.representative_source_hash
        for course in catalogue.courses
        if course.source_version_id == active_source
    )
    assert active_hash == "a" * 64
    assert (
        coverage.canonical_course_count,
        coverage.representative_course_count,
        coverage.no_representative_course_count,
    ) == (4, 4, 0)
    assert (coverage.name_only_course_count, coverage.unembeddable_course_count) == (1, 2)
    assert coverage.stale_texts == ()
    # The projected coverage read agrees with the full resolved catalogue and never reads official
    # prerequisite/substitution structures.
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        projected = service.coverage()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert projected == coverage
    assert not any(
        "official_prerequisite_options" in statement or "official_substitution_options" in statement
        for statement in statements
    )
    counts = {(count.field, count.language): count for count in coverage.fields}
    assert (
        counts[(TeachingTextField.NAME, "fi")].official_count,
        counts[(TeachingTextField.NAME, "fi")].derived_count,
        counts[(TeachingTextField.NAME, "fi")].stale_count,
        counts[(TeachingTextField.NAME, "fi")].missing_count,
    ) == (1, 1, 0, 2)
    assert (
        counts[(TeachingTextField.NAME, "en")].official_count,
        counts[(TeachingTextField.NAME, "en")].derived_count,
        counts[(TeachingTextField.NAME, "en")].stale_count,
        counts[(TeachingTextField.NAME, "en")].missing_count,
    ) == (1, 0, 0, 3)
    assert (
        counts[(TeachingTextField.CONTENT, "fi")].official_count,
        counts[(TeachingTextField.CONTENT, "fi")].derived_count,
        counts[(TeachingTextField.CONTENT, "fi")].stale_count,
        counts[(TeachingTextField.CONTENT, "fi")].missing_count,
    ) == (1, 0, 0, 3)
    assert (
        counts[(TeachingTextField.CONTENT, "en")].official_count,
        counts[(TeachingTextField.CONTENT, "en")].derived_count,
        counts[(TeachingTextField.CONTENT, "en")].stale_count,
        counts[(TeachingTextField.CONTENT, "en")].missing_count,
    ) == (0, 1, 0, 3)
    assert (
        counts[(TeachingTextField.OUTCOMES, "fi")].official_count,
        counts[(TeachingTextField.OUTCOMES, "fi")].derived_count,
        counts[(TeachingTextField.OUTCOMES, "fi")].stale_count,
        counts[(TeachingTextField.OUTCOMES, "fi")].missing_count,
    ) == (0, 1, 0, 3)
    assert (
        counts[(TeachingTextField.OUTCOMES, "en")].official_count,
        counts[(TeachingTextField.OUTCOMES, "en")].derived_count,
        counts[(TeachingTextField.OUTCOMES, "en")].stale_count,
        counts[(TeachingTextField.OUTCOMES, "en")].missing_count,
    ) == (1, 0, 0, 3)

    active_course = next(
        course for course in catalogue.courses if course.source_version_id == active_source
    )
    resolved_cells = {(cell.field, cell.language): cell for cell in active_course.texts}
    assert resolved_cells[(TeachingTextField.CONTENT, "en")].text == "Matrices"
    assert resolved_cells[(TeachingTextField.OUTCOMES, "fi")].text == ("Käytä matriiseja")

    with session_scope(factory) as session:
        version_id = session.scalar(
            select(CourseVersion.id).where(CourseVersion.source_version_id == active_source)
        )
        text = session.scalar(
            select(OfficialCourseText).where(
                OfficialCourseText.course_version_id == version_id,
                OfficialCourseText.field == OfficialTextField.CONTENT,
                OfficialCourseText.language == "fi",
            )
        )
        assert text is not None
        text.text = "Muuttuneet matriisit"

    catalogue = service.build_catalogue()
    coverage = catalogue.coverage
    assert len(coverage.stale_texts) == 1
    assert service.coverage() == coverage
    stale = coverage.stale_texts[0]
    assert stale.source_version_id == active_source
    assert stale.field is TeachingTextField.CONTENT
    assert stale.language == "en"
    assert stale.source_language == "fi"
    assert stale.stored_source_text_hash == teaching_text_sha256("Matriisit")
    assert stale.current_source_text_hash == teaching_text_sha256("Muuttuneet matriisit")
    counts = {(count.field, count.language): count for count in coverage.fields}
    assert (
        counts[(TeachingTextField.CONTENT, "en")].derived_count,
        counts[(TeachingTextField.CONTENT, "en")].stale_count,
        counts[(TeachingTextField.CONTENT, "en")].missing_count,
    ) == (0, 1, 3)
    assert coverage.name_only_course_count == 1
    assert coverage.unembeddable_course_count == 2
    assert (coverage.canonical_course_count, coverage.representative_course_count) == (4, 4)
    active_course = next(
        course for course in catalogue.courses if course.source_version_id == active_source
    )
    assert (TeachingTextField.CONTENT, "en") not in {
        (cell.field, cell.language) for cell in active_course.texts
    }
    empty_course = next(
        course for course in catalogue.courses if course.source_version_id == empty_source
    )
    assert empty_course.texts == ()
