"""Deterministic missing-cell selection and resumable translation-only worker.

This module owns the product-neutral contracts that select every missing representative
name/content/outcomes target for every configured official language in the university's
translation priority map, translate each cell from its same-field official source through the one
translation provider, and emit a provenance-complete ``DerivedTranslationInput`` JSON array. Machine
selection uses the first populated listed source, while an accepted manual row may use any listed
source. A derived cell is current only when its accepted official source matches and, for MODEL
rows, its persisted model identity equals the configured provenance model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from course_explorer.administration import atomic_write_private_text
from course_explorer.administration import utc_now as _utc_now
from course_explorer.config import TranslationSettings, load_instance_settings
from course_explorer.curriculum import (
    CatalogueCourse,
    CatalogueCourseVersion,
    CatalogueTeachingCourse,
    CatalogueTeachingCourseVersion,
    CurriculumRead,
)
from course_explorer.enums import (
    LanguageCode,
    TeachingTextField,
    TranslationMethod,
)
from course_explorer.http_retry import RequestNotStarted
from course_explorer.instance import UniversityMetadata
from course_explorer.teaching_text.commands import load_translation_file
from course_explorer.teaching_text.service import (
    DerivedTranslation,
    DerivedTranslationInput,
    DerivedTranslationRead,
    TranslationKey,
    index_derived_translations,
    official_teaching_text,
    teaching_text_sha256,
    translation_key,
)
from course_explorer.translation.provider import (
    TranslationContractError,
    TranslationRequestError,
)

_SAFE_FAILURE_MESSAGE = re.compile(r"^[A-Za-z0-9_ /:().-]+$")


class _TranslationModel(BaseModel):
    """Frozen, closed DTO base for the translation worker contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TranslationWorkItem(_TranslationModel):
    """One official-gap cell that must be translated from its official same-field source."""

    group_id: str
    source_version_id: str
    field: TeachingTextField
    language: LanguageCode
    source_language: LanguageCode
    source_text: str
    source_text_hash: str


class TranslationSelectionCount(_TranslationModel):
    """One field/language cell's four-way partition over selected representatives."""

    field: TeachingTextField
    language: LanguageCode
    official_target_count: int = Field(ge=0)
    current_derived_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    missing_source_count: int = Field(ge=0)


class TranslationWorkSelection(_TranslationModel):
    """Deterministic translation selection with per-cell counts and ordered pending work."""

    canonical_course_count: int = Field(ge=0)
    representative_course_count: int = Field(ge=0)
    no_representative_course_count: int = Field(ge=0)
    active_representative_count: int = Field(ge=0)
    inactive_representative_count: int = Field(ge=0)
    fields: tuple[TranslationSelectionCount, ...]
    items: tuple[TranslationWorkItem, ...]


class TranslationFailureRecord(_TranslationModel):
    """One deterministic, text-free record of a terminal worker failure.

    The cell tuple is the failure identity and the record type identifies the stage. It carries
    only the safe error class and a short safe reason.
    """

    source_version_id: str
    field: TeachingTextField
    language: LanguageCode
    error_type: str
    error_message: str

    @field_validator("source_version_id", "error_type", "error_message")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("failure record text values must not be blank")
        return value


class TranslationRunReport(_TranslationModel):
    """One completed worker run's partition counts and output totals, without any text."""

    canonical_course_count: int = Field(ge=0)
    representative_course_count: int = Field(ge=0)
    no_representative_course_count: int = Field(ge=0)
    active_representative_count: int = Field(ge=0)
    inactive_representative_count: int = Field(ge=0)
    fields: tuple[TranslationSelectionCount, ...]
    desired_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    generated_count: int = Field(ge=0)
    output_count: int = Field(ge=0)


class TranslationTextProvider(Protocol):
    """Provider boundary consumed by the worker for one same-field source-cell translation."""

    def translate(
        self,
        text: str,
        *,
        source_language: LanguageCode,
        target_language: LanguageCode,
    ) -> str: ...


def _safe_failure_message(exc: Exception) -> str:
    """Keep only short fixed-shape provider text. Otherwise return the exception class name."""
    if isinstance(exc, (TranslationRequestError, TranslationContractError)):
        message = str(exc)
        if (
            1 <= len(message) <= 200
            and message.isascii()
            and _SAFE_FAILURE_MESSAGE.fullmatch(message) is not None
        ):
            return message
    return type(exc).__name__


def _translation_targets(university: UniversityMetadata) -> tuple[str, ...]:
    """Return official-language order restricted to targets with a mapped source list."""
    return tuple(
        language
        for language in university.official_languages
        if language in university.translation_priority
    )


def _selection_counts(
    targets: Sequence[str],
) -> dict[tuple[TeachingTextField, str], dict[str, int]]:
    return {
        (field, language): {
            "official_target": 0,
            "current_derived": 0,
            "pending": 0,
            "missing_source": 0,
        }
        for field in TeachingTextField
        for language in targets
    }


def _current_derived(
    existing: DerivedTranslation | None,
    representative: CatalogueCourseVersion | CatalogueTeachingCourseVersion,
    *,
    field: TeachingTextField,
    listed_sources: Sequence[str],
    source_language: str,
    source_text_hash: str,
    provenance_model: str,
) -> bool:
    """Return whether a stored row is current for one representative under the accepted-pair policy.

    A manual row is current when its stored source language is listed for the target and the
    current text of that source hashes to the recorded value, even when an earlier listed source is
    populated. A model row is current only for the first populated listed source and the configured
    provenance model.
    """
    if existing is None:
        return False
    if existing.translation_method is TranslationMethod.MANUAL:
        if existing.source_language not in listed_sources:
            return False
        source = official_teaching_text(representative, field, existing.source_language)
        return source is not None and teaching_text_sha256(source) == existing.source_text_hash
    return (
        existing.source_language == source_language
        and existing.source_text_hash == source_text_hash
        and existing.translation_model == provenance_model
    )


def _collect_course_work(
    course: CatalogueCourse | CatalogueTeachingCourse,
    derived_by_key: dict[TranslationKey, DerivedTranslation],
    *,
    provenance_model: str,
    counts: dict[tuple[TeachingTextField, str], dict[str, int]],
    university: UniversityMetadata,
) -> tuple[tuple[TranslationWorkItem, ...], tuple[TranslationWorkItem, ...]]:
    """Classify one course's mapped target cells, returning candidates and pending work.

    An unrepresented course returns two empty tuples. Official targets, absent listed sources,
    current manual/current-model rows, stale sources and changed models classify exactly as the
    selector contract requires. A machine candidate always names the first populated listed source.
    Stored rows are never modified.
    """
    version = course.representative
    if version is None:
        return (), ()
    targets = _translation_targets(university)
    candidates: list[TranslationWorkItem] = []
    pending: list[TranslationWorkItem] = []
    for field in TeachingTextField:
        for language in targets:
            if official_teaching_text(version, field, language) is not None:
                counts[(field, language)]["official_target"] += 1
                continue
            listed_sources = university.translation_priority[language]
            source_language: str | None = None
            source_text: str | None = None
            for listed in listed_sources:
                listed_text = official_teaching_text(version, field, listed)
                if listed_text is not None:
                    source_language = listed
                    source_text = listed_text
                    break
            if source_language is None or source_text is None:
                counts[(field, language)]["missing_source"] += 1
                continue
            item = TranslationWorkItem(
                group_id=course.group_id,
                source_version_id=version.source_version_id,
                field=field,
                language=language,
                source_language=source_language,
                source_text=source_text,
                source_text_hash=teaching_text_sha256(source_text),
            )
            candidates.append(item)
            existing = derived_by_key.get(
                translation_key(version.source_version_id, field, language)
            )
            if _current_derived(
                existing,
                version,
                field=field,
                listed_sources=listed_sources,
                source_language=source_language,
                source_text_hash=item.source_text_hash,
                provenance_model=provenance_model,
            ):
                counts[(field, language)]["current_derived"] += 1
            else:
                counts[(field, language)]["pending"] += 1
                pending.append(item)
    return tuple(candidates), tuple(pending)


def _collect_translation_work(
    courses: Sequence[CatalogueCourse | CatalogueTeachingCourse],
    translations: DerivedTranslationRead,
    *,
    provenance_model: str,
    course_limit: int | None,
    university: UniversityMetadata,
) -> tuple[TranslationWorkSelection, tuple[TranslationWorkItem, ...]]:
    """Partition every selected representative's mapped target cells in stable order.

    A derived cell is current only when its accepted listed source and exact source hash match.
    A manual row may use any listed source. A model row must use the first populated listed source
    and the configured ``provenance_model``. A row excluded by the pair policy or produced by a
    different configured model is requeued as pending. Selection never overwrites or deletes stored
    rows.
    """
    if course_limit is not None:
        courses = courses[:course_limit]
    representative_ids = tuple(
        course.representative.source_version_id
        for course in courses
        if course.representative is not None
    )
    derived = translations.list_for_course_versions(representative_ids)
    derived_by_key = index_derived_translations(derived)

    targets = _translation_targets(university)
    counts = _selection_counts(targets)
    candidates: list[TranslationWorkItem] = []
    pending: list[TranslationWorkItem] = []
    active_representative_count = 0
    for course in courses:
        if course.representative is None:
            continue
        if course.is_active:
            active_representative_count += 1
        course_candidates, course_pending = _collect_course_work(
            course,
            derived_by_key,
            provenance_model=provenance_model,
            counts=counts,
            university=university,
        )
        candidates.extend(course_candidates)
        pending.extend(course_pending)

    representative_course_count = len(representative_ids)
    selection = TranslationWorkSelection(
        canonical_course_count=len(courses),
        representative_course_count=representative_course_count,
        no_representative_course_count=len(courses) - representative_course_count,
        active_representative_count=active_representative_count,
        inactive_representative_count=representative_course_count - active_representative_count,
        fields=tuple(
            TranslationSelectionCount(
                field=field,
                language=language,
                official_target_count=counts[(field, language)]["official_target"],
                current_derived_count=counts[(field, language)]["current_derived"],
                pending_count=counts[(field, language)]["pending"],
                missing_source_count=counts[(field, language)]["missing_source"],
            )
            for field in TeachingTextField
            for language in targets
        ),
        items=tuple(pending),
    )
    return selection, tuple(candidates)


def _collect(
    curriculum: CurriculumRead,
    translations: DerivedTranslationRead,
    settings: TranslationSettings,
    *,
    course_limit: int | None,
    university: UniversityMetadata,
) -> tuple[TranslationWorkSelection, tuple[TranslationWorkItem, ...]]:
    """Read all courses once and partition their representative cells in stable order."""
    courses = curriculum.list_teaching_courses(active_only=None)
    return _collect_translation_work(
        courses,
        translations,
        provenance_model=settings.provenance_model,
        course_limit=course_limit,
        university=university,
    )


def select_translation_work(
    curriculum: CurriculumRead,
    translations: DerivedTranslationRead,
    settings: TranslationSettings,
    *,
    course_limit: int | None = None,
    university: UniversityMetadata | None = None,
) -> TranslationWorkSelection:
    """Partition mapped target cells and queue missing or invalidated accepted-pair gaps.

    A matching-source derived cell is current only when it is an accepted manual row from any listed
    source or a model row produced from the first populated listed source by
    ``settings.provenance_model``. A row outside the accepted pairs or produced by another model is
    requeued without being modified.
    """
    if course_limit is not None and course_limit <= 0:
        raise ValueError("course_limit must be positive or None")
    resolved = load_instance_settings().university if university is None else university
    selection, _ = _collect(
        curriculum,
        translations,
        settings,
        course_limit=course_limit,
        university=resolved,
    )
    return selection


def _entries_json(entries: Sequence[DerivedTranslationInput]) -> str:
    return (
        json.dumps(
            [entry.model_dump(mode="json") for entry in entries],
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _load_retained_output(output_path: Path) -> dict[TranslationKey, DerivedTranslationInput]:
    """Load retained output entries by key, or return empty when the output is absent."""
    if not output_path.exists():
        return {}
    retained: dict[TranslationKey, DerivedTranslationInput] = {}
    for entry in load_translation_file(output_path):
        key = translation_key(entry.source_version_id, entry.field, entry.language)
        if key in retained:
            raise ValueError(
                "duplicate retained output entry for "
                f"{entry.source_version_id}/{entry.field}/{entry.language}"
            )
        retained[key] = entry
    return retained


class TranslationWorkerService:
    """One idempotent, resumable worker over selected gap cells and the sole provider."""

    def __init__(
        self,
        curriculum: CurriculumRead,
        translations: DerivedTranslationRead,
        provider: TranslationTextProvider,
        settings: TranslationSettings,
        *,
        clock: Callable[[], datetime] = _utc_now,
        university: UniversityMetadata | None = None,
    ) -> None:
        self._curriculum = curriculum
        self._translations = translations
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._university = load_instance_settings().university if university is None else university

    def _validate_retained(
        self,
        retained: dict[TranslationKey, DerivedTranslationInput],
        candidates: dict[TranslationKey, TranslationWorkItem],
    ) -> None:
        for key, entry in retained.items():
            candidate = candidates.get(key)
            if candidate is None:
                raise ValueError(
                    "retained output is not an official-gap candidate in the selected scope: "
                    f"{entry.source_version_id}/{entry.field}/{entry.language}"
                )
            if (
                entry.source_language != candidate.source_language
                or entry.source_text != candidate.source_text
            ):
                raise ValueError(
                    "retained output source language or text no longer matches the official source: "
                    f"{entry.source_version_id}/{entry.field}/{entry.language}"
                )
            if entry.translation_method is not TranslationMethod.MODEL:
                raise ValueError(
                    "retained output must be a model translation: "
                    f"{entry.source_version_id}/{entry.field}/{entry.language}"
                )
            if entry.translation_model != self._settings.provenance_model:
                raise ValueError(
                    "retained output was produced by a different translation model: "
                    f"{entry.source_version_id}/{entry.field}/{entry.language}"
                )

    def _write_failure(self, sidecar: Path, item: TranslationWorkItem, exc: Exception) -> None:
        record = TranslationFailureRecord(
            source_version_id=item.source_version_id,
            field=item.field,
            language=item.language,
            error_type=type(exc).__name__,
            error_message=_safe_failure_message(exc),
        )
        payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        atomic_write_private_text(sidecar, payload)

    def _translate_item(self, item: TranslationWorkItem, sidecar: Path) -> DerivedTranslationInput:
        """Translate one item, publishing a terminal safe sidecar for a real provider failure.

        ``RequestNotStarted`` is a cooperative refusal and is re-raised without a sidecar. Any
        other provider exception records the sidecar and re-raises.
        """
        try:
            text = self._provider.translate(
                item.source_text,
                source_language=item.source_language,
                target_language=item.language,
            )
        except RequestNotStarted:
            raise
        except Exception as exc:
            self._write_failure(sidecar, item, exc)
            raise
        return DerivedTranslationInput(
            source_version_id=item.source_version_id,
            field=item.field,
            language=item.language,
            source_language=item.source_language,
            source_text=item.source_text,
            text=text,
            translation_method=TranslationMethod.MODEL,
            translation_model=self._settings.provenance_model,
            generated_at=self._clock(),
        )

    def run(
        self,
        *,
        output_path: Path,
        course_limit: int | None = None,
        before_pending_item: Callable[[], bool] | None = None,
        on_checkpoint: Callable[[int], None] | None = None,
    ) -> TranslationRunReport:
        """Translate pending gaps, checkpoint each success, and return text-free counts.

        ``before_pending_item`` is consulted immediately before provider I/O for each
        item without retained text. Returning ``False`` ends the run normally without calling
        the provider for that item or subsequent items. ``on_checkpoint`` runs only after a
        success has been atomically published and its matching failure sidecar removed,
        with the number of output entries retained. Neither callback fires for
        retained or completed entries, and the default ``None`` preserves the
        caller contract.
        """
        if course_limit is not None and course_limit <= 0:
            raise ValueError("course_limit must be positive or None")
        if output_path.exists() and output_path.is_dir():
            raise ValueError("output_path must name a file, not a directory")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar = output_path.with_name(output_path.name + ".failure.json")

        selection, ordered_candidates = _collect(
            self._curriculum,
            self._translations,
            self._settings,
            course_limit=course_limit,
            university=self._university,
        )
        candidates = {
            translation_key(item.source_version_id, item.field, item.language): item
            for item in ordered_candidates
        }
        order = [
            translation_key(item.source_version_id, item.field, item.language)
            for item in ordered_candidates
        ]

        retained = _load_retained_output(output_path)
        self._validate_retained(retained, candidates)

        output: dict[TranslationKey, DerivedTranslationInput] = dict(retained)
        generated_count = 0
        for item in selection.items:
            key = translation_key(item.source_version_id, item.field, item.language)
            if key in output:
                continue
            if before_pending_item is not None and not before_pending_item():
                break
            try:
                entry = self._translate_item(item, sidecar)
            except RequestNotStarted:
                # A pause/stop refusal ends the run at this durable boundary. The retained
                # output is the partial report and no failure sidecar is fabricated.
                break
            output[key] = entry
            generated_count += 1
            atomic_write_private_text(
                output_path,
                _entries_json(
                    [output[candidate_key] for candidate_key in order if candidate_key in output]
                ),
            )
            sidecar.unlink(missing_ok=True)
            if on_checkpoint is not None:
                on_checkpoint(len(output))

        reused_count = len(retained)
        output_count = len(output)
        return TranslationRunReport(
            canonical_course_count=selection.canonical_course_count,
            representative_course_count=selection.representative_course_count,
            no_representative_course_count=selection.no_representative_course_count,
            active_representative_count=selection.active_representative_count,
            inactive_representative_count=selection.inactive_representative_count,
            fields=selection.fields,
            desired_count=reused_count + generated_count,
            reused_count=reused_count,
            generated_count=generated_count,
            output_count=output_count,
        )
