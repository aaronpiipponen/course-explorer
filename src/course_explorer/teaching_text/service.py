"""Effective teaching-text resolution over official and separately derived translations.

This module owns the product-neutral contract for derived teaching text and the deterministic
resolution of per-field, per-language effective text. Official source text always wins. A derived
translation is usable only when its target and source pair is accepted by the configured
translation priority map and its recorded source hash matches the current same-field official source
text. Rows excluded by the pair policy are stored but contribute no effective text, and accepted
rows with a changed source are reported as stale, never silently used or deleted. Prerequisite
and additional-information text never participate here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from course_explorer.config import load_instance_settings
from course_explorer.curriculum import (
    CatalogueCourse,
    CatalogueCourseVersion,
    CatalogueRepresentativeTeachingText,
    CatalogueTeachingCourse,
    CatalogueTeachingTextInput,
    CurriculumRead,
)
from course_explorer.enums import (
    LanguageCode,
    OfficialTextField,
    TeachingTextField,
    TeachingTextOrigin,
    TranslationMethod,
)
from course_explorer.instance import UniversityMetadata
from course_explorer.validation import require_aware, require_sha256


def _method_model_consistent(
    translation_method: TranslationMethod, translation_model: str | None
) -> None:
    if translation_method is TranslationMethod.MANUAL:
        if translation_model is not None:
            raise ValueError("manual translations must not record a translation model")
        return
    if translation_model is None or not translation_model.strip():
        raise ValueError("model translations require a nonblank translation model")


class _TeachingTextModel(BaseModel):
    """Frozen, closed DTO base for the effective-teaching-text contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _DerivedTranslationModel(_TeachingTextModel):
    """Shared validation for both derived-translation wire DTOs.

    Fieldless so each concrete DTO keeps its own field declaration order and defaults. The
    field validators apply to whichever concrete subclass declares the named field.
    """

    @field_validator("source_version_id", check_fields=False)
    @classmethod
    def reject_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_version_id must not be blank")
        return value

    @field_validator("text", check_fields=False)
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("generated_at", check_fields=False)
    @classmethod
    def reject_naive_timestamp(cls, value: datetime) -> datetime:
        return require_aware(value, "generated_at")

    @model_validator(mode="after")
    def validate_provenance_relations(self) -> Self:
        if self.source_language == self.language:
            raise ValueError("source and target languages must differ")
        _method_model_consistent(self.translation_method, self.translation_model)
        return self


class DerivedTranslationInput(_DerivedTranslationModel):
    """Operator- or worker-supplied derived translation awaiting persistence."""

    source_version_id: str
    field: TeachingTextField
    language: LanguageCode
    source_language: LanguageCode
    source_text: str
    text: str
    translation_method: TranslationMethod
    generated_at: datetime
    translation_model: str | None = None

    @field_validator("source_text")
    @classmethod
    def reject_blank_source_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_text must not be blank")
        return value


class DerivedTranslation(_DerivedTranslationModel):
    """Persisted derived translation for one course unit, field and language."""

    source_version_id: str
    field: TeachingTextField
    language: LanguageCode
    source_language: LanguageCode
    source_text_hash: str
    text: str
    translation_method: TranslationMethod
    generated_at: datetime
    translation_model: str | None = None

    @field_validator("source_text_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        return require_sha256(value, "hash values")


TranslationKey = tuple[str, TeachingTextField, str]


def translation_key(
    source_version_id: str,
    field: TeachingTextField,
    language: str,
) -> TranslationKey:
    """Return one derived translation identity key without normalizing its parts."""
    return (source_version_id, field, language)


def index_derived_translations(
    translations: Sequence[DerivedTranslation],
) -> dict[TranslationKey, DerivedTranslation]:
    """Index derived rows by exact identity, refusing ambiguous duplicate keys."""
    by_key: dict[TranslationKey, DerivedTranslation] = {}
    for translation in translations:
        key = translation_key(
            translation.source_version_id, translation.field, translation.language
        )
        if key in by_key:
            raise ValueError(
                "duplicate derived translation rows for "
                f"{translation.source_version_id}/{translation.field}/{translation.language}"
            )
        by_key[key] = translation
    return by_key


class TranslationImportReport(_TeachingTextModel):
    """Insert, update, or identical outcomes of one validated translation import batch."""

    requested_count: int = Field(ge=0)
    inserted_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)


class EffectiveTeachingText(_TeachingTextModel):
    """One resolved effective field/language cell with its origin and provenance."""

    field: TeachingTextField
    language: LanguageCode
    text: str
    origin: TeachingTextOrigin
    text_hash: str
    source_text_hash: str
    translation_method: TranslationMethod | None = None
    translation_model: str | None = None
    translation_generated_at: datetime | None = None


class EffectiveCourseTeachingText(_TeachingTextModel):
    """All effective cells for one canonical course's selected representative."""

    group_id: str
    source_version_id: str
    representative_source_hash: str
    is_active: bool
    texts: tuple[EffectiveTeachingText, ...]


class StaleDerivedText(_TeachingTextModel):
    """One retained derived row whose recorded source hash differs from official text."""

    group_id: str
    source_version_id: str
    field: TeachingTextField
    language: LanguageCode
    source_language: LanguageCode
    stored_source_text_hash: str
    current_source_text_hash: str | None


class TeachingTextCoverageCount(_TeachingTextModel):
    """Origin classification counts for one field/language cell over representatives."""

    field: TeachingTextField
    language: LanguageCode
    official_count: int = Field(ge=0)
    derived_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)


class TeachingTextCoverageReport(_TeachingTextModel):
    """Representative-catalogue teaching-text coverage and stale-row inventory."""

    canonical_course_count: int = Field(ge=0)
    representative_course_count: int = Field(ge=0)
    no_representative_course_count: int = Field(ge=0)
    name_only_course_count: int = Field(ge=0)
    unembeddable_course_count: int = Field(ge=0)
    fields: tuple[TeachingTextCoverageCount, ...]
    stale_texts: tuple[StaleDerivedText, ...]
    unrepresented_group_ids: tuple[str, ...] = ()


class EffectiveTeachingTextCatalogue(_TeachingTextModel):
    """Resolved effective teaching text for every representative plus coverage."""

    canonical_group_ids: tuple[str, ...]
    courses: tuple[EffectiveCourseTeachingText, ...]
    coverage: TeachingTextCoverageReport


class DerivedTranslationRead(Protocol):
    """Read boundary for persisted derived translations consumed by the effective service."""

    def list_for_course_versions(
        self, source_version_ids: Collection[str]
    ) -> tuple[DerivedTranslation, ...]: ...


def teaching_text_sha256(text: str) -> str:
    """Return the SHA-256 of exact text bytes without normalizing the text."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("cannot hash blank teaching text")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def official_teaching_text(
    version: CatalogueCourseVersion | CatalogueRepresentativeTeachingText,
    field: TeachingTextField,
    language: str,
) -> str | None:
    """Return one nonblank official name/content/outcomes value, ignoring other fields.

    An absent field, absent language or blank value returns ``None``. This read never falls back to
    another official or derived cell.
    """
    value = version.texts.get(OfficialTextField(field), {}).get(language)
    if value is None or not value.strip():
        return None
    return value


def _resolve_cell(
    version: CatalogueCourseVersion | CatalogueRepresentativeTeachingText,
    translation: DerivedTranslation | None,
    *,
    field: TeachingTextField,
    language: str,
    translation_priority: Mapping[str, Sequence[str]],
) -> tuple[
    EffectiveTeachingText | None, Literal["official", "derived", "stale", "missing"], str | None
]:
    """Resolve one field/language cell and classify its origin.

    The third element is the current same-field official source text hash for a stale derived row
    (``None`` when that source is absent). Every non-stale classification returns ``None`` there.
    Official text wins. A stored row counts only when its target is mapped and its source is listed
    for that target. An accepted row is usable only while its recorded source hash matches the
    current same-field official source text. A row excluded by the pair policy is missing, not
    stale.
    """
    official = official_teaching_text(version, field, language)
    if official is not None:
        cell_hash = teaching_text_sha256(official)
        return (
            EffectiveTeachingText(
                field=field,
                language=language,
                text=official,
                origin=TeachingTextOrigin.OFFICIAL,
                text_hash=cell_hash,
                source_text_hash=cell_hash,
            ),
            "official",
            None,
        )
    if translation is None:
        return None, "missing", None
    if translation.source_language not in translation_priority.get(language, ()):
        return None, "missing", None
    source = official_teaching_text(version, field, translation.source_language)
    current_hash = None if source is None else teaching_text_sha256(source)
    if current_hash == translation.source_text_hash:
        return (
            EffectiveTeachingText(
                field=field,
                language=language,
                text=translation.text,
                origin=TeachingTextOrigin.DERIVED,
                text_hash=teaching_text_sha256(translation.text),
                source_text_hash=translation.source_text_hash,
                translation_method=translation.translation_method,
                translation_model=translation.translation_model,
                translation_generated_at=translation.generated_at,
            ),
            "derived",
            None,
        )
    return None, "stale", current_hash


def _resolve_representative_cells(
    group_id: str,
    representative: CatalogueRepresentativeTeachingText,
    derived_by_key: dict[TranslationKey, DerivedTranslation],
    counts: dict[tuple[TeachingTextField, str], dict[str, int]],
    stale_texts: list[StaleDerivedText],
    *,
    languages: Sequence[str],
    translation_priority: Mapping[str, Sequence[str]],
) -> tuple[EffectiveTeachingText, ...]:
    """Resolve one represented input's configured cells and accumulate its cell/stale counts.

    Called only for represented entries. The caller derives name-only/unembeddable bookkeeping
    from the returned effective cells. ``_resolve_cell`` is the sole per-cell classifier.
    """
    cells: list[EffectiveTeachingText] = []
    for field in TeachingTextField:
        for language in languages:
            translation = derived_by_key.get(
                translation_key(representative.source_version_id, field, language)
            )
            cell, classification, current_hash = _resolve_cell(
                representative,
                translation,
                field=field,
                language=language,
                translation_priority=translation_priority,
            )
            counts[(field, language)][classification] += 1
            if cell is not None:
                cells.append(cell)
            elif classification == "stale":
                stale_texts.append(
                    StaleDerivedText(
                        group_id=group_id,
                        source_version_id=representative.source_version_id,
                        field=field,
                        language=language,
                        source_language=translation.source_language,
                        stored_source_text_hash=translation.source_text_hash,
                        current_source_text_hash=current_hash,
                    )
                )
    return tuple(cells)


def _resolve_teaching_inputs(
    inputs: Sequence[CatalogueTeachingTextInput],
    derived: Sequence[DerivedTranslation],
    *,
    languages: Sequence[str],
    translation_priority: Mapping[str, Sequence[str]],
) -> tuple[dict[str, tuple[EffectiveTeachingText, ...]], TeachingTextCoverageReport]:
    """Resolve every input's configured cells once and accumulate the shared coverage report.

    This one accumulator owns the unique derived-key check, the configured-cell origin counts, the
    stale inventory, and the name-only/unembeddable/unrepresented bookkeeping for both the effective
    catalogue and the projected coverage read. It never materializes a full ``CatalogueCourse``.
    """
    by_key = index_derived_translations(derived)

    counts: dict[tuple[TeachingTextField, str], dict[str, int]] = {
        (field, language): {"official": 0, "derived": 0, "stale": 0, "missing": 0}
        for field in TeachingTextField
        for language in languages
    }
    cells_by_group: dict[str, tuple[EffectiveTeachingText, ...]] = {}
    stale_texts: list[StaleDerivedText] = []
    unrepresented_group_ids: list[str] = []
    representative_count = 0
    name_only_count = 0
    unembeddable_count = 0
    for entry in inputs:
        representative = entry.representative
        if representative is None:
            unrepresented_group_ids.append(entry.group_id)
            continue
        representative_count += 1
        cells = _resolve_representative_cells(
            entry.group_id,
            representative,
            by_key,
            counts,
            stale_texts,
            languages=languages,
            translation_priority=translation_priority,
        )
        cells_by_group[entry.group_id] = cells
        has_name = any(cell.field is TeachingTextField.NAME for cell in cells)
        has_body = any(cell.field is not TeachingTextField.NAME for cell in cells)
        if has_name and not has_body:
            name_only_count += 1
        if not has_name and not has_body:
            unembeddable_count += 1
    stale_texts.sort(
        key=lambda stale: (
            stale.group_id,
            stale.source_version_id,
            stale.field.value,
            stale.language,
        )
    )
    coverage = TeachingTextCoverageReport(
        canonical_course_count=len(inputs),
        representative_course_count=representative_count,
        no_representative_course_count=len(unrepresented_group_ids),
        name_only_course_count=name_only_count,
        unembeddable_course_count=unembeddable_count,
        fields=tuple(
            TeachingTextCoverageCount(
                field=field,
                language=language,
                official_count=counts[(field, language)]["official"],
                derived_count=counts[(field, language)]["derived"],
                stale_count=counts[(field, language)]["stale"],
                missing_count=counts[(field, language)]["missing"],
            )
            for field in TeachingTextField
            for language in languages
        ),
        stale_texts=tuple(stale_texts),
        unrepresented_group_ids=tuple(unrepresented_group_ids),
    )
    return cells_by_group, coverage


class TeachingTextService:
    """Resolve deterministic effective teaching text over the official read seam."""

    def __init__(
        self,
        curriculum: CurriculumRead,
        translations: DerivedTranslationRead,
        *,
        university: UniversityMetadata | None = None,
    ) -> None:
        self._curriculum = curriculum
        self._translations = translations
        self._university = load_instance_settings().university if university is None else university

    def build_catalogue(
        self,
        *,
        courses: Sequence[CatalogueCourse | CatalogueTeachingCourse] | None = None,
    ) -> EffectiveTeachingTextCatalogue:
        """Resolve every representative cell and report origin/coverage evidence.

        ``courses`` lets one caller that already read the current catalogue reuse that exact
        tuple. The supplied courses go through the identical
        resolver/coverage logic. With the default ``None`` this reads the projected teaching
        catalogue, which carries only the identity, source hash, code, names and teaching text.
        The resolver's carrier holds exactly the three teaching fields, so prerequisite and
        additional-information text never crosses this boundary.
        """
        courses = (
            self._curriculum.list_teaching_courses(active_only=None)
            if courses is None
            else tuple(courses)
        )
        representative_ids = tuple(
            course.representative.source_version_id
            for course in courses
            if course.representative is not None
        )
        derived = self._translations.list_for_course_versions(representative_ids)
        inputs = tuple(
            CatalogueTeachingTextInput(
                group_id=course.group_id,
                representative=(
                    None
                    if course.representative is None
                    else CatalogueRepresentativeTeachingText(
                        source_version_id=course.representative.source_version_id,
                        texts={
                            OfficialTextField(field): course.representative.texts.get(
                                OfficialTextField(field), {}
                            )
                            for field in TeachingTextField
                        },
                    )
                ),
            )
            for course in courses
        )
        cells_by_group, coverage = _resolve_teaching_inputs(
            inputs,
            derived,
            languages=self._university.official_languages,
            translation_priority=self._university.translation_priority,
        )
        effective_courses = tuple(
            EffectiveCourseTeachingText(
                group_id=course.group_id,
                source_version_id=course.representative.source_version_id,
                representative_source_hash=course.representative.source_hash,
                is_active=course.is_active,
                texts=cells_by_group[course.group_id],
            )
            for course in courses
            if course.representative is not None
        )
        return EffectiveTeachingTextCatalogue(
            canonical_group_ids=tuple(course.group_id for course in courses),
            courses=effective_courses,
            coverage=coverage,
        )

    def coverage(self) -> TeachingTextCoverageReport:
        """Return representative-catalogue teaching-text coverage without full-course reads.

        It reads the narrow projected teaching-text inputs and the derived rows once and shares the
        exact resolution/coverage accumulator with ``build_catalogue``, so its report is identical
        for the same state while no full ``CatalogueCourse`` or ``EffectiveCourseTeachingText`` is
        materialized.
        """
        inputs = self._curriculum.list_teaching_text_inputs()
        representative_ids = tuple(
            entry.representative.source_version_id
            for entry in inputs
            if entry.representative is not None
        )
        derived = self._translations.list_for_course_versions(representative_ids)
        _cells, coverage = _resolve_teaching_inputs(
            inputs,
            derived,
            languages=self._university.official_languages,
            translation_priority=self._university.translation_priority,
        )
        return coverage
