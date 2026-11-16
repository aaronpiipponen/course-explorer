"""Persistence seam for provenance-aware derived teaching-text translations.

This is the sole writer and reader for `derived_course_texts`. It validates every input against
the configured university's accepted translation pairs and the selected representative's current
official same-field source text before any database write, computes the persisted source hash
itself, and reconciles rows transactionally. It never touches the official text cells or module
name cells.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from course_explorer.config import load_instance_settings
from course_explorer.curriculum import CatalogueRepresentativeTeachingText
from course_explorer.curriculum_repository import CurriculumRepository
from course_explorer.db.models import (
    CourseVersion,
    DerivedCourseText,
)
from course_explorer.db.session import session_scope
from course_explorer.instance import UniversityMetadata
from course_explorer.teaching_text.service import (
    DerivedTranslation,
    DerivedTranslationInput,
    DerivedTranslationRead,
    TranslationImportReport,
    TranslationKey,
    official_teaching_text,
    teaching_text_sha256,
    translation_key,
)


def _prepare_inputs(
    inputs: tuple[DerivedTranslationInput, ...],
    representatives: dict[str, CatalogueRepresentativeTeachingText],
    university: UniversityMetadata,
) -> tuple[tuple[DerivedTranslationInput, str], ...]:
    """Validate every input against the configured policy and official text.

    The whole batch is prepared before any database write, so one invalid entry leaves no rows.
    An accepted manual import may use any source listed for its mapped target.
    """
    prepared: list[tuple[DerivedTranslationInput, str]] = []
    seen: set[TranslationKey] = set()
    official_languages = set(university.official_languages)
    for item in inputs:
        key = translation_key(item.source_version_id, item.field, item.language)
        if key in seen:
            raise ValueError(
                "duplicate translation input for "
                f"{item.source_version_id}/{item.field}/{item.language}"
            )
        seen.add(key)
        if item.language not in official_languages:
            raise ValueError(f"{item.source_version_id}/{item.field} uses an unconfigured language")
        listed_sources = university.translation_priority.get(item.language)
        if listed_sources is None or item.source_language not in listed_sources:
            raise ValueError(
                f"{item.source_version_id}/{item.field} is not an accepted translation pair"
            )
        representative = representatives.get(item.source_version_id)
        if representative is None:
            raise ValueError(
                f"{item.source_version_id} is not a selected representative course version"
            )
        if official_teaching_text(representative, item.field, item.language) is not None:
            raise ValueError(
                f"{item.source_version_id}/{item.field}/{item.language} already has official text"
            )
        source = official_teaching_text(representative, item.field, item.source_language)
        if source is None:
            raise ValueError(
                f"{item.source_version_id}/{item.field}/{item.source_language} "
                "has no official source text"
            )
        if source != item.source_text:
            raise ValueError(
                f"{item.source_version_id}/{item.field} source text does not match official text"
            )
        prepared.append((item, teaching_text_sha256(item.source_text)))
    return tuple(prepared)


def _translation_values(item: DerivedTranslationInput, source_hash: str) -> dict[str, Any]:
    """Return the exact mutable derived-row column mapping for one prepared input."""
    return {
        "text": item.text,
        "source_language": item.source_language,
        "source_text_hash": source_hash,
        "translation_method": item.translation_method,
        "translation_model": item.translation_model,
        "generated_at": item.generated_at,
    }


class DerivedTranslationRepository(DerivedTranslationRead):
    """Validate, upsert, and read derived translations through one transaction seam."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        university: UniversityMetadata | None = None,
    ) -> None:
        self._factory = factory
        self._university = load_instance_settings().university if university is None else university

    def list_for_course_versions(
        self, source_version_ids: Collection[str]
    ) -> tuple[DerivedTranslation, ...]:
        """Return detached derived rows for the requested source IDs in stable order."""
        source_ids = tuple(source_version_ids)
        if not source_ids:
            return ()
        with self._factory() as session:
            rows = session.execute(
                select(DerivedCourseText, CourseVersion.source_version_id)
                .join(CourseVersion, CourseVersion.id == DerivedCourseText.course_version_id)
                .where(CourseVersion.source_version_id.in_(source_ids))
                .order_by(
                    CourseVersion.source_version_id,
                    DerivedCourseText.field,
                    DerivedCourseText.language,
                )
            )
            return tuple(
                DerivedTranslation(
                    source_version_id=source_version_id,
                    field=row.field,
                    language=row.language,
                    source_language=row.source_language,
                    source_text_hash=row.source_text_hash,
                    text=row.text,
                    translation_method=row.translation_method,
                    generated_at=row.generated_at,
                    translation_model=row.translation_model,
                )
                for row, source_version_id in rows
            )

    def upsert(self, translations: Sequence[DerivedTranslationInput]) -> TranslationImportReport:
        """Validate inputs against official text and reconcile rows in one transaction."""
        inputs = tuple(translations)
        if not inputs:
            return TranslationImportReport(
                requested_count=0,
                inserted_count=0,
                updated_count=0,
                unchanged_count=0,
            )
        representatives: dict[str, CatalogueRepresentativeTeachingText] = {
            representative.source_version_id: representative
            for representative in CurriculumRepository(
                self._factory, university=self._university
            ).get_representative_teaching_texts(tuple(item.source_version_id for item in inputs))
        }
        prepared = _prepare_inputs(inputs, representatives, self._university)

        inserted = 0
        updated = 0
        unchanged = 0
        with session_scope(self._factory) as session:
            versions = {
                source_version_id: version_id
                for source_version_id, version_id in session.execute(
                    select(CourseVersion.source_version_id, CourseVersion.id).where(
                        CourseVersion.source_version_id.in_(
                            tuple(item.source_version_id for item, _ in prepared)
                        )
                    )
                )
            }
            for item, _ in prepared:
                if item.source_version_id not in versions:
                    raise ValueError(
                        f"selected representative course version {item.source_version_id} is missing"
                    )
            version_ids = tuple(versions.values())
            existing = {
                (row.course_version_id, row.field, row.language): row
                for row in session.scalars(
                    select(DerivedCourseText).where(
                        DerivedCourseText.course_version_id.in_(version_ids)
                    )
                )
            }
            for item, source_hash in prepared:
                version_id = versions[item.source_version_id]
                row = existing.get((version_id, item.field, item.language))
                values = _translation_values(item, source_hash)
                if row is None:
                    session.add(
                        DerivedCourseText(
                            course_version_id=version_id,
                            field=item.field,
                            language=item.language,
                            **values,
                        )
                    )
                    inserted += 1
                    continue
                if all(getattr(row, name) == value for name, value in values.items()):
                    unchanged += 1
                    continue
                for name, value in values.items():
                    setattr(row, name, value)
                updated += 1
            session.flush()
        return TranslationImportReport(
            requested_count=len(prepared),
            inserted_count=inserted,
            updated_count=updated,
            unchanged_count=unchanged,
        )
