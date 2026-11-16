"""One-shot import of the fictional example dataset and its bundled vectors.

This is the offline adapter behind ``scripts/mock_data.py``. It registers the authored neutral
fixture records through the ``RawCaptureStore`` exactly like a completed capture, decodes them with
the fixture's own parser, and stores the fixture curriculum and its bundled vectors through the
shared normalization repository, embedding reconciliation and worker readiness owners.

The fixture is authored fiction: neutral common-model course and module records occupy the source
layer of an entirely fictional dataset and are never presented as university data. The
``load-curriculum``, ``load``, ``write-artifacts`` and ``write-bundle`` actions are the supported
actions. Both the loader and the writer actions prepare the fixture through one shared helper and
then apply the same projected fixture-identity guard at their entry: foreign or changed retained
curriculum fails before any database provenance/normalized write and before any fixture-bundle or
artifact output file is touched. The artifact writer then calls the one serving artifact builder, so
the example and administrative paths share one artifact owner. The separate ``write-bundle`` action
regenerates the tracked bundled-vector file as explicit fixture maintenance, so a deployment never
rewrites the tracked bundle.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from course_explorer import administration
from course_explorer.config import (
    DatabaseSettings,
    EmbeddingRuntimeSettings,
    NormalizationSettings,
    embedding_configuration_for_site,
    load_embedding_runtime_settings,
    load_normalization_settings,
    load_raw_database_settings,
    load_web_settings,
)
from course_explorer.constants import COURSE_EMBEDDING_DIMENSION
from course_explorer.curriculum_repository import CurriculumRepository
from course_explorer.datasets import read_dataset_role, require_database_schema
from course_explorer.db.models import Course, Module, SourceSnapshot
from course_explorer.db.session import database_context
from course_explorer.embeddings.generation import (
    CourseEmbeddingChunkInput,
    CourseEmbeddingCoverageReport,
    CourseEmbeddingResult,
    build_course_embedding_chunks,
)
from course_explorer.embeddings.hub_files import read_current_chunk_vectors
from course_explorer.embeddings.repository import CourseEmbeddingRepository
from course_explorer.enums import (
    IngestionRunStatus,
    ModuleType,
    OfficialTextField,
    SourceObjectType,
    TeachingTextField,
    TeachingTextOrigin,
)
from course_explorer.example_data import EXAMPLE_SOURCE_ATTRIBUTION
from course_explorer.hashing import json_sha256, parse_strict_json
from course_explorer.ingestion.manifest import (
    CapturedResponseRecord,
    RawCaptureStore,
    ReplayedSourceObject,
    discover_capture_manifests,
)
from course_explorer.ingestion.normalization import (
    NormalizationBatch,
    NormalizationReport,
    NormalizedCourseVersion,
    NormalizedModuleRule,
    NormalizedModuleVersion,
    NormalizedPrerequisiteOption,
    NormalizedSubstitutionOption,
    SnapshotProvenance,
    parse_capture,
)
from course_explorer.ingestion.normalization_repository import NormalizationRepository
from course_explorer.ingestion.repository import CaptureRepository
from course_explorer.ingestion.source import (
    CapturedResponse,
    CurriculumParser,
    CurriculumPeriodRef,
    DecodedSourceObject,
    SourcePeriod,
)
from course_explorer.instance import GuideSourceMetadata, PeppiPeriod, UniversityMetadata
from course_explorer.serving_build import ServingArtifactsReport, build_serving_artifacts
from course_explorer.teaching_text.repository import DerivedTranslationRepository
from course_explorer.teaching_text.service import TeachingTextService
from course_explorer.validation import require_sha256

# Explicit request parameters marking these captured envelopes as authored fixture input rather
# than a fetched response. They participate in the content-addressed request identity.
_FIXTURE_REQUEST_PARAMS: tuple[tuple[str, str], ...] = (("fixture", "fictional"),)
_BUNDLE_NAME = "embeddings.json"
# The fixture's own two-language preparation policy, which builds its bundled chunk set and
# vectors. It is not a site's instance, is never selected through the university map and is never
# displayed. The fixture records also carry Swedish official cells, and the configured instance's
# normalization policy decides which of those cells its database stores.
_FIXTURE_UNIVERSITY = UniversityMetadata(
    name_en="Example University",
    name_fi="Esimerkkiyliopisto",
    display_label="example-uni",
    source_attribution_en=EXAMPLE_SOURCE_ATTRIBUTION["en"],
    source_attribution_fi=EXAMPLE_SOURCE_ATTRIBUTION["fi"],
    source_family="guide",
    sisu=None,
    peppi_legacy=None,
    peppi_psg=None,
    guide=GuideSourceMetadata(
        guide_base_url="https://example.invalid/study-guide",
        fetch_variant="plain_http",
        periods=(PeppiPeriod(2026, 2027),),
    ),
    official_languages=("fi", "en"),
    display_fallback={"fi": ("fi", "en"), "en": ("en", "fi")},
    translation_priority={"fi": ("en",), "en": ("fi",)},
    inference_languages=(
        ("fi", TeachingTextOrigin.OFFICIAL),
        ("en", TeachingTextOrigin.OFFICIAL),
    ),
    inference_target_fields=(
        OfficialTextField.NAME,
        OfficialTextField.CONTENT,
        OfficialTextField.OUTCOMES,
        OfficialTextField.PREREQUISITE_TEXT,
    ),
    inference_pool_fields=(
        OfficialTextField.NAME,
        OfficialTextField.CONTENT,
        OfficialTextField.OUTCOMES,
    ),
)

MockDataAction = Literal["load-curriculum", "load", "write-artifacts", "write-bundle"]


class _MockModel(BaseModel):
    """Frozen, closed DTO base for this adapter's fixture/bundle boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _FixtureModel(BaseModel):
    """Frozen, closed DTO base for the authored neutral fixture records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _FixturePeriod(_FixtureModel):
    """One authored curriculum period over its academic-year interval."""

    id: str = Field(min_length=1)
    academic_year_start: int = Field(gt=0)
    academic_year_end: int = Field(gt=0)


class _FixtureCourse(_FixtureModel):
    """One authored neutral course version with its official text cells and official options."""

    id: str
    group_id: str
    code: str
    texts: dict[OfficialTextField, dict[str, str | None]]
    credits_min: Decimal | None
    credits_max: Decimal | None
    official_url: AnyHttpUrl
    document_state: str | None
    validity_start: date | None
    validity_end: date | None
    source_revision: int | None
    source_modification_ordinal: int | None
    curriculum_period_ids: tuple[str, ...]
    prerequisite_options: tuple[NormalizedPrerequisiteOption, ...] = ()
    substitution_options: tuple[NormalizedSubstitutionOption, ...] = ()


class _FixtureModule(_FixtureModel):
    """One authored neutral module version with its module type, names and rule tree."""

    id: str
    group_id: str
    module_type: ModuleType
    code: str | None
    names: dict[str, str | None]
    target_credits_min: Decimal | None
    target_credits_max: Decimal | None
    document_state: str | None
    validity_start: date | None
    validity_end: date | None
    source_revision: int | None
    source_modification_ordinal: int | None
    curriculum_period_ids: tuple[str, ...]
    rule: NormalizedModuleRule | None = None


class _MockEmbeddingChunk(_MockModel):
    """One bundled fixture chunk vector keyed by its exact teaching-text chunk identity."""

    group_id: str
    field: TeachingTextField
    language: str
    chunk_index: int = Field(ge=0)
    embedding: tuple[float, ...]

    @field_validator("group_id")
    @classmethod
    def reject_blank_group_id(cls, value: str) -> str:
        """Require a nonblank bundled chunk group ID."""
        if not value.strip():
            raise ValueError("bundled chunk group_id must not be blank")
        return value

    @field_validator("embedding")
    @classmethod
    def validate_embedding(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """Require one finite vector at the fixed embedding dimension."""
        if len(value) != COURSE_EMBEDDING_DIMENSION:
            raise ValueError(
                f"bundled chunk embedding must have exactly {COURSE_EMBEDDING_DIMENSION} components"
            )
        if any(not math.isfinite(component) for component in value):
            raise ValueError("bundled chunk embedding components must be finite")
        return value


class _MockEmbeddingBundle(_MockModel):
    """The one closed bundled-vector artifact: its coarse input identity and its chunks."""

    input_sha256: str
    chunks: tuple[_MockEmbeddingChunk, ...]

    @field_validator("input_sha256")
    @classmethod
    def validate_input_sha256(cls, value: str) -> str:
        """Require one full lowercase SHA-256 digest of the bundle input."""
        return require_sha256(value, "bundled embeddings input_sha256")


def _read_fixture_json(fixture_dir: Path, name: str) -> object:
    """Read one authored fixture document with strict JSON validation."""
    try:
        raw = (fixture_dir / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"mock fixture file is not readable: {name}") from exc
    try:
        return parse_strict_json(raw)
    except ValueError as exc:
        raise ValueError(f"mock fixture file is not valid JSON: {name}") from exc


def _require_object_list(value: object, name: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"mock fixture {name} must be a JSON list of objects")
    return tuple(value)


def _fixture_response_inputs(
    fixture_dir: Path,
) -> tuple[tuple[CapturedResponse, SourceObjectType | None], ...]:
    """Build the three authored neutral fixture envelopes in fixed representative order."""
    periods_body = (fixture_dir / "periods.json").read_bytes()
    courses_body = (fixture_dir / "courses.json").read_bytes()
    modules_body = (fixture_dir / "modules.json").read_bytes()
    return (
        (
            CapturedResponse("/fixture/periods", _FIXTURE_REQUEST_PARAMS, periods_body),
            None,
        ),
        (
            CapturedResponse("/fixture/courses", _FIXTURE_REQUEST_PARAMS, courses_body),
            SourceObjectType.COURSE_UNIT,
        ),
        (
            CapturedResponse("/fixture/modules", _FIXTURE_REQUEST_PARAMS, modules_body),
            SourceObjectType.MODULE,
        ),
    )


def _fixture_periods(fixture_dir: Path) -> tuple[CurriculumPeriodRef, ...]:
    """Return the fixture's explicit period selection with its authored academic-year starts."""
    values = _require_object_list(_read_fixture_json(fixture_dir, "periods.json"), "periods.json")
    selected = tuple(
        CurriculumPeriodRef(id=period.id, academic_year_start=period.academic_year_start)
        for period in (_FixturePeriod.model_validate(value) for value in values)
    )
    if not selected:
        raise ValueError("mock fixture must select at least one curriculum period")
    return selected


def _mock_store(raw_root: Path, parser: CurriculumParser) -> RawCaptureStore:
    """Reuse the sole retained mock import manifest or create the first one."""
    manifests = discover_capture_manifests(raw_root, parser=parser)
    if len(manifests) > 1:
        raise ValueError("multiple mock import manifests are ambiguous")
    if manifests:
        return RawCaptureStore.open(raw_root, manifests[0], parser=parser)
    return RawCaptureStore.create(raw_root, parser=parser)


def _require_reused_mock_inputs(
    store: RawCaptureStore,
    inputs: tuple[tuple[CapturedResponse, SourceObjectType | None], ...],
) -> None:
    """Compare a retained completed capture's response bodies with the current fixture once."""
    expected = {
        (captured.request_path, captured.request_params): captured.body for captured, _ in inputs
    }
    replayed = tuple(store.iter_responses())
    if len(replayed) != len(expected):
        raise ValueError("retained mock import manifest does not match the current fixture")
    for response in replayed:
        key = (response.record.request_path, response.record.request_params)
        if expected.get(key) != response.body:
            raise ValueError("retained mock import manifest does not match the current fixture")


def _checkpoint_mock_capture(
    store: RawCaptureStore,
    inputs: tuple[tuple[CapturedResponse, SourceObjectType | None], ...],
    periods: tuple[CurriculumPeriodRef, ...],
) -> tuple[CapturedResponseRecord, ...]:
    """Complete the fixture capture once, or verify and reuse the already completed one."""
    if store.manifest.status is IngestionRunStatus.COMPLETED:
        _require_reused_mock_inputs(store, inputs)
        return store.manifest.responses
    if store.manifest.status is IngestionRunStatus.FAILED:
        store.mark_running()
    store.set_periods(periods)
    records = store.checkpoint_responses(inputs)
    store.mark_completed()
    return records


def _fixture_provenance(replayed: ReplayedSourceObject) -> SnapshotProvenance:
    """Return one fixture record's capture provenance."""
    return SnapshotProvenance(
        object_type=replayed.record.object_type,
        source_object_id=replayed.record.source_object_id,
        source_group_id=replayed.record.source_group_id,
        response_path=replayed.response_path,
        response_item_index=replayed.record.response_item_index,
        source_hash=replayed.record.source_hash,
    )


class _FixtureParser:
    """Decode the authored neutral fixture records into the shared normalized batch."""

    def decode_objects(
        self,
        response: CapturedResponse,
        object_type: SourceObjectType,
        *,
        store: RawCaptureStore | None = None,
    ) -> tuple[DecodedSourceObject, ...]:
        """Decode one fixture envelope's complete records with their identity and source hash.

        A fixture envelope carries everything its decode needs, so the shared store evidence is
        accepted and not read.
        """
        del store
        if object_type not in (SourceObjectType.COURSE_UNIT, SourceObjectType.MODULE):
            raise ValueError("fixture response must carry course or module records")
        try:
            values = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("fixture response body must be valid JSON") from exc
        if not isinstance(values, list):
            raise ValueError("fixture response body must be a JSON list")
        decoded: list[DecodedSourceObject] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("fixture record must be a JSON object")
            identifier = value.get("id")
            group_id = value.get("group_id")
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("fixture record id must be nonblank")
            if not isinstance(group_id, str) or not group_id.strip():
                raise ValueError("fixture record group_id must be nonblank")
            decoded.append(
                DecodedSourceObject(
                    source_object_id=identifier,
                    source_group_id=group_id,
                    source_hash=json_sha256(value, ensure_ascii=False),
                    value=value,
                )
            )
        return tuple(decoded)

    def read_periods(self, store: RawCaptureStore) -> tuple[SourcePeriod, ...]:
        """Read every authored fixture period and require the manifest selection to match it."""
        period_responses = tuple(store.iter_responses(request_path="/fixture/periods"))
        if len(period_responses) != 1:
            raise ValueError("capture must contain exactly one fixture periods response")
        try:
            values = json.loads(period_responses[0].body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("fixture periods response must be valid JSON") from exc
        if not isinstance(values, list):
            raise ValueError("fixture periods response must be a JSON list")
        periods = tuple(_FixturePeriod.model_validate(value) for value in values)
        if len({period.id for period in periods}) != len(periods):
            raise ValueError("fixture periods response contains duplicate IDs")
        if any(period.academic_year_end <= period.academic_year_start for period in periods):
            raise ValueError("fixture period must span at least one academic year")
        return tuple(
            SourcePeriod(
                id=period.id,
                academic_year_start=period.academic_year_start,
                academic_year_end=period.academic_year_end,
            )
            for period in periods
        )

    def parse_object(
        self, replayed: ReplayedSourceObject
    ) -> NormalizedCourseVersion | NormalizedModuleVersion | SnapshotProvenance:
        """Validate and convert one authored fixture course or module record."""
        if replayed.record.object_type is SourceObjectType.COURSE_UNIT:
            course = _FixtureCourse.model_validate(replayed.value)
            return NormalizedCourseVersion(
                provenance=_fixture_provenance(replayed),
                group_id=course.group_id,
                code=course.code,
                texts=course.texts,
                credits_min=course.credits_min,
                credits_max=course.credits_max,
                official_url=str(course.official_url),
                document_state=course.document_state,
                validity_start=course.validity_start,
                validity_end=course.validity_end,
                source_revision=course.source_revision,
                source_modification_ordinal=course.source_modification_ordinal,
                curriculum_period_ids=course.curriculum_period_ids,
                prerequisite_options=course.prerequisite_options,
                substitution_options=course.substitution_options,
            )
        if replayed.record.object_type is SourceObjectType.MODULE:
            module = _FixtureModule.model_validate(replayed.value)
            return NormalizedModuleVersion(
                provenance=_fixture_provenance(replayed),
                group_id=module.group_id,
                module_type=module.module_type,
                code=module.code,
                names=module.names,
                target_credits_min=module.target_credits_min,
                target_credits_max=module.target_credits_max,
                document_state=module.document_state,
                validity_start=module.validity_start,
                validity_end=module.validity_end,
                source_revision=module.source_revision,
                source_modification_ordinal=module.source_modification_ordinal,
                curriculum_period_ids=module.curriculum_period_ids,
                rule=module.rule,
            )
        raise ValueError("unsupported fixture object type")


def _prepare_mock_fixture(
    *,
    fixture_dir: Path,
    raw_root: Path,
) -> tuple[RawCaptureStore, tuple[CapturedResponseRecord, ...], NormalizationBatch]:
    """Prepare the authored fixture capture and parse it into a normalization batch.

    This is the one preparation path the loader and the artifact writer share: it builds the
    fixture response inputs and periods, selects or creates the sole retained capture, completes
    and reuses that capture, then parses it into a normalization batch. It writes no database row,
    so each caller can apply the same projected fixture-identity guard before its own database or
    output work.
    """
    parser = _FixtureParser()
    inputs = _fixture_response_inputs(fixture_dir)
    periods = _fixture_periods(fixture_dir)
    store = _mock_store(raw_root, parser)
    records = _checkpoint_mock_capture(store, inputs, periods)
    return store, records, parse_capture(store)


def _require_fixture_dataset(factory: sessionmaker[Session], expected: NormalizationBatch) -> None:
    """Refuse a dataset holding anything but this fiction fixture before any database write.

    The guard projects only the canonical group IDs and the retained source identity/hash set, so an
    empty dataset or exactly this fixture's identities may proceed. A foreign or changed retained
    curriculum fails here, before any provenance or normalized row is written. Duplicate identical
    provenance rows collapse by set semantics.
    """
    expected_courses = {course.group_id for course in expected.courses}
    expected_modules = {module.group_id for module in expected.modules}
    expected_snapshots = {
        (
            provenance.object_type,
            provenance.source_object_id,
            provenance.source_group_id,
            provenance.source_hash,
        )
        for provenance in (
            *(course.provenance for course in expected.courses),
            *(module.provenance for module in expected.modules),
        )
    }
    with factory() as session:
        course_groups = set(session.scalars(select(Course.group_id)))
        module_groups = set(session.scalars(select(Module.group_id)))
        snapshots = set(
            session.execute(
                select(
                    SourceSnapshot.object_type,
                    SourceSnapshot.source_object_id,
                    SourceSnapshot.source_group_id,
                    SourceSnapshot.source_hash,
                )
            )
        )
    if not (course_groups or module_groups or snapshots):
        return
    if (
        course_groups != expected_courses
        or module_groups != expected_modules
        or snapshots != expected_snapshots
    ):
        raise ValueError("database already holds curriculum outside the fictional mock fixture")


def load_mock_curriculum(
    factory: sessionmaker[Session],
    *,
    fixture_dir: Path,
    raw_root: Path,
    normalization: NormalizationSettings,
) -> NormalizationReport:
    """Import the authored fictional fixture exactly like one completed capture run."""
    store, records, batch = _prepare_mock_fixture(fixture_dir=fixture_dir, raw_root=raw_root)
    _require_fixture_dataset(factory, batch)
    capture = CaptureRepository(factory)
    capture.ensure_run(store.manifest, store.manifest_relative_path)
    capture.register_responses(store.manifest.run_id, records)
    return NormalizationRepository(factory, university=normalization.university).apply(
        batch,
        current_academic_year_start=normalization.current_academic_year_start,
        activity_window_years=normalization.active_academic_year_window,
    )


def _bundle_input_sha256(chunks: Sequence[CourseEmbeddingChunkInput]) -> str:
    """Hash the ordered desired chunk identity/provenance dumps excluding the chunk text."""
    payload = sorted(
        (chunk.model_dump(mode="json", exclude={"text"}) for chunk in chunks),
        key=lambda item: (
            item["group_id"],
            item["field"],
            item["language"],
            item["chunk_index"],
        ),
    )
    return json_sha256(payload, ensure_ascii=True)


def _read_bundle(fixture_dir: Path) -> _MockEmbeddingBundle:
    """Read the one bundled-vector artifact through its strict boundary."""
    try:
        raw = (fixture_dir / _BUNDLE_NAME).read_bytes()
    except OSError as exc:
        raise ValueError(f"bundled mock embeddings are not readable: {_BUNDLE_NAME}") from exc
    try:
        parsed = parse_strict_json(raw)
    except ValueError as exc:
        raise ValueError("bundled mock embeddings are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("bundled mock embeddings must be a JSON object")
    try:
        return _MockEmbeddingBundle.model_validate_json(raw, strict=True)
    except ValueError as exc:
        raise ValueError("bundled mock embeddings violate the artifact contract") from exc


def _require_bundle_matches(
    chunks: Sequence[CourseEmbeddingChunkInput], bundle: _MockEmbeddingBundle
) -> None:
    """Require the bundled artifact to name exactly the current fixture's chunk identities."""
    desired = sorted(
        (chunk.group_id, chunk.field, chunk.language, chunk.chunk_index) for chunk in chunks
    )
    bundled = sorted(
        (chunk.group_id, chunk.field, chunk.language, chunk.chunk_index) for chunk in bundle.chunks
    )
    if bundled != desired:
        raise ValueError("bundled mock embeddings do not match the current fixture chunks")
    if bundle.input_sha256 != _bundle_input_sha256(chunks):
        raise ValueError(
            "bundled mock embeddings input identity does not match the current fixture"
        )


def load_mock_embeddings(
    factory: sessionmaker[Session],
    *,
    fixture_dir: Path,
    embedding: EmbeddingRuntimeSettings,
) -> CourseEmbeddingCoverageReport:
    """Import the bundled fixture vectors through the one embedding reconciliation owner."""
    catalogue = TeachingTextService(
        CurriculumRepository(factory, university=_FIXTURE_UNIVERSITY),
        DerivedTranslationRepository(factory, university=_FIXTURE_UNIVERSITY),
        university=_FIXTURE_UNIVERSITY,
    ).build_catalogue()
    scope = tuple(catalogue.canonical_group_ids)
    chunks = build_course_embedding_chunks(catalogue, embedding)
    bundle = _read_bundle(fixture_dir)
    _require_bundle_matches(chunks, bundle)
    vectors = {
        (chunk.group_id, chunk.field, chunk.language, chunk.chunk_index): chunk.embedding
        for chunk in bundle.chunks
    }
    repository = CourseEmbeddingRepository(factory, university=_FIXTURE_UNIVERSITY)
    reconciliation = repository.reconcile(scope, chunks)
    pending_ids = tuple(pending.row_id for pending in reconciliation.pending)
    if pending_ids:
        keys = repository.read_chunk_keys(pending_ids)
        missing = [row_id for row_id in pending_ids if row_id not in keys]
        if missing:
            raise ValueError("reconciled bundled-vector rows are missing")
        repository.complete(
            tuple(
                CourseEmbeddingResult(row_id=row_id, embedding=vectors[keys[row_id]])
                for row_id in pending_ids
            )
        )
    return repository.coverage(scope)


def _write_json(path: Path, payload: object) -> None:
    """Write one deterministic JSON artifact with a trailing newline."""
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _prepare_writer_fixture(
    factory: sessionmaker[Session],
    *,
    fixture_dir: Path,
    raw_root: Path,
) -> None:
    """Prepare the authored fixture capture and apply the projected fixture-identity guard.

    A foreign or changed retained dataset is refused before any artifact or bundle output file is
    touched, so both writer actions share one entry guard.
    """
    _, _, batch = _prepare_mock_fixture(fixture_dir=fixture_dir, raw_root=raw_root)
    _require_fixture_dataset(factory, batch)


def write_mock_artifacts(
    factory: sessionmaker[Session],
    *,
    fixture_dir: Path,
    raw_root: Path,
    embedding: EmbeddingRuntimeSettings | None,
    database_settings: DatabaseSettings | None = None,
) -> ServingArtifactsReport:
    """Build the mock dataset's serving artifacts for the site's embedding mode.

    ``embedding`` is the resolved embedding configuration on a site that serves the embedding
    features and ``None`` on a site with them off. The entry guard runs first, so a foreign or
    changed retained dataset is refused before any artifact path is touched. The full
    serving-artifact builder then produces the study fragments, catalogue, ``current`` build and,
    when the site serves the embedding features, the hubs, shared stores and packed search relation.
    The writer never rewrites the bundled fixture file. That file changes only through the separate
    ``write-bundle`` action.
    """
    _prepare_writer_fixture(factory, fixture_dir=fixture_dir, raw_root=raw_root)
    return build_serving_artifacts(
        database_settings or load_raw_database_settings(),
        embedding=embedding,
    )


def write_mock_bundle(
    factory: sessionmaker[Session],
    *,
    fixture_dir: Path,
    raw_root: Path,
    embedding: EmbeddingRuntimeSettings,
) -> None:
    """Rewrite the bundled fixture vectors from the prepared fixture's stored vectors.

    This is the explicit fixture-maintenance action that regenerates the tracked example bundle
    when the fixture's chunks or vectors change. The serving artifact build never writes it, so a
    deployment never rewrites the tracked bundle.
    """
    _prepare_writer_fixture(factory, fixture_dir=fixture_dir, raw_root=raw_root)
    current = read_current_chunk_vectors(factory, embedding, university=_FIXTURE_UNIVERSITY)
    _write_json(
        fixture_dir / _BUNDLE_NAME,
        {
            "input_sha256": _bundle_input_sha256(current.chunks),
            "chunks": [
                {
                    "group_id": chunk.group_id,
                    "field": chunk.field.value,
                    "language": chunk.language,
                    "chunk_index": chunk.chunk_index,
                    "embedding": list(vector),
                }
                for chunk, vector in zip(current.chunks, current.vectors, strict=True)
            ],
        },
    )


def run_mock_data_command(
    action: MockDataAction, *, fixture_dir: Path, raw_root: Path
) -> BaseModel | None:
    """Run exactly one mock action against the configured database.

    A site whose state root resolves an enrolled dataset is refused before any engine is opened, and
    the single Alembic head is required on the database this command opens. The two loader actions
    read the normalization policy of the configured instance, ``write-artifacts`` reads the site's
    embedding switch once here and passes that mode to the one artifact builder, and the loader and
    the bundle writer read the configured embedding family whether or not the site serves the
    embedding features.
    """
    if action not in ("load-curriculum", "load", "write-artifacts", "write-bundle"):
        raise ValueError(f"unknown mock data action: {action}")
    database = load_raw_database_settings()
    if read_dataset_role(administration.repository_root() / database.admin_state_root, "selected"):
        raise ValueError("mock data import requires a bootstrap dataset state root")
    with database_context(database) as factory:
        with factory() as session:
            require_database_schema(session.connection())
        if action == "write-artifacts":
            return write_mock_artifacts(
                factory,
                fixture_dir=fixture_dir,
                raw_root=raw_root,
                embedding=embedding_configuration_for_site(load_web_settings()),
                database_settings=database,
            )
        if action == "write-bundle":
            write_mock_bundle(
                factory,
                fixture_dir=fixture_dir,
                raw_root=raw_root,
                embedding=load_embedding_runtime_settings(),
            )
            return None
        report = load_mock_curriculum(
            factory,
            fixture_dir=fixture_dir,
            raw_root=raw_root,
            normalization=load_normalization_settings(),
        )
        if action == "load":
            return load_mock_embeddings(
                factory,
                fixture_dir=fixture_dir,
                embedding=load_embedding_runtime_settings(),
            )
        return report
