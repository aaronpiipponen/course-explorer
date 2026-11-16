"""Opt-in PostgreSQL proofs for the composed import boundary and retained verification."""

from __future__ import annotations

import contextlib
import gzip
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from course_explorer.administration import (
    administrative_json,
    atomic_write_private_text,
    session_directory,
    utc_now,
)
from course_explorer.config import CaptureSettings, NormalizationSettings
from course_explorer.db.models import (
    CourseVersion,
    CourseVersionCurriculumPeriod,
    CurriculumPeriod,
    IngestionRun,
    SourceSnapshot,
)
from course_explorer.enums import IngestionRunStatus, OfficialTextField, SourceObjectType
from course_explorer.hashing import json_sha256
from course_explorer.ingestion import commands as ingestion_commands
from course_explorer.ingestion.capture import boundary_allows, record_responses
from course_explorer.ingestion.importing import import_curriculum
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.normalization import (
    NormalizedCourseVersion,
    SnapshotProvenance,
)
from course_explorer.ingestion.normalization_repository import NormalizationRepository
from course_explorer.ingestion.repository import CaptureRepository
from course_explorer.ingestion.source import (
    CapturedResponse,
    CurriculumPeriodRef,
    DecodedSourceObject,
    SourcePeriod,
    SourceResponse,
)
from course_explorer.instance import UNIVERSITIES
from course_explorer_admin import lifecycle, local_stages, mechanics
from course_explorer_admin.lifecycle import (
    IngestionOptions,
    IngestionProgress,
    LifecycleContext,
    LifecycleSession,
    LifecycleState,
    read_lifecycle_state,
)
from course_explorer_admin.local_stages import IngestionStage
from course_explorer_admin.worker import run_lifecycle_worker
from tests.admin._lifecycle_support import LocalLifecycleRunner, configure_environment
from tests.datasets._support import write_dataset_roles
from tests.ingestion._normalization_repository_support import (
    build_completed_fixture_store,
    fixture_bytes,
    normalized_database_digest,
)
from tests.support import settings_factory

pytestmark = [
    pytest.mark.integration,
]

SESSION_ID = "0" * 32
REVISION = "a" * 40
IDENTITY = "b" * 64


def _seed_ingestion_session(
    root: Path, store, *, options: IngestionOptions
) -> tuple[LifecycleSession, LifecycleState]:
    """Publish one current receipt/state naming this test process as the owned worker."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = session_directory(root, SESSION_ID)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    session = LifecycleSession(
        schema_version=1,
        session_id=SESSION_ID,
        stage="ingestion",
        revision=REVISION,
        identity_sha256=IDENTITY,
        options=options,
        created_at=utc_now(),
    )
    atomic_write_private_text(directory / "session.json", administrative_json(session))
    process = mechanics.OwnedProcessRecord(
        schema_version=1,
        pid=os.getpid(),
        argv=mechanics.read_process_command_line(os.getpid()) or ("pytest",),
        boot_id=mechanics.current_boot_id(),
    )
    state = LifecycleState(
        schema_version=1,
        session_id=SESSION_ID,
        lifecycle="paused",
        requested_action="run",
        process=process,
        progress=IngestionProgress(
            kind="ingestion",
            stage="selecting",
            response_count=0,
            verified_response_count=0,
            normalized=False,
        ),
        work_complete=False,
        owned_services=(),
        active_seconds=0.0,
        running_since=None,
        error_type=None,
    )
    lifecycle.write_lifecycle_state(root, state)
    return session, state


def _clear_owned_process(root: Path) -> None:
    """Clear only the recorded owner so a foreground stop never waits on the pytest process."""
    state = read_lifecycle_state(root)
    assert state is not None
    lifecycle.write_lifecycle_state(root, state.model_copy(update={"process": None}))


def _provenance_counts(factory) -> tuple[int, int]:
    """Return scalar ingestion-run and source-snapshot row counts, never full rows."""
    with factory.begin() as session:
        runs = session.scalar(select(func.count()).select_from(IngestionRun))
        snapshots = session.scalar(select(func.count()).select_from(SourceSnapshot))
    return int(runs), int(snapshots)


def test_import_boundary_callbacks_gate_capture_to_normalization_atomically(
    database: tuple[object, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real worker refuses normalization atomically, then a re-armed worker completes it."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    # The fixture store records a cwd-relative manifest path, so build and open it from tmp_path.
    monkeypatch.chdir(tmp_path)
    course_values = json.loads(fixture_bytes("courses.json"))
    course_values[0]["curriculumPeriodIds"] = [
        *course_values[0]["curriculumPeriodIds"],
        "period-unpublished",
    ]
    store = build_completed_fixture_store(
        tmp_path,
        capture_repository,
        courses_body=json.dumps(course_values).encode(),
    )
    before = normalized_database_digest(factory)

    capture_settings = settings_factory(CaptureSettings).model_copy(
        update={"raw_data_root": store.run_root.parent}
    )
    normalization_settings = settings_factory(NormalizationSettings)
    monkeypatch.setattr(
        ingestion_commands,
        "_database_context",
        lambda settings, **_kwargs: contextlib.nullcontext(factory),
    )
    monkeypatch.setattr(ingestion_commands, "load_capture_settings", lambda: capture_settings)
    monkeypatch.setattr(
        ingestion_commands, "load_normalization_settings", lambda: normalization_settings
    )
    monkeypatch.setattr(local_stages, "load_capture_settings", lambda: capture_settings)

    options = IngestionOptions(kind="ingestion", manifest_path=store.manifest_relative_path)
    root = tmp_path / "lifecycle"
    _, seeded_state = _seed_ingestion_session(root, store, options=options)
    process = seeded_state.process
    assert process is not None

    configure_environment(monkeypatch, repository_root=tmp_path / "repository")
    write_dataset_roles()
    monkeypatch.setattr(local_stages, "stage_state_root", lambda stage: root)
    runner = LocalLifecycleRunner(root=root, running={"db"})

    # One unretained capture directory under the same raw root with one tiny file.
    older = store.run_root.parent / "older-run"
    older.mkdir()
    (older / "tiny.txt").write_text("older\n", encoding="utf-8")

    real_checkpoint = LifecycleContext.checkpoint
    requested = {"enabled": True, "done": False}

    def checkpoint_with_pause(self, progress) -> None:
        real_checkpoint(self, progress)
        if (
            requested["enabled"]
            and not requested["done"]
            and isinstance(progress, IngestionProgress)
            and progress.stage == "before_normalization"
        ):
            requested["done"] = True
            with mechanics.exclusive_action_lock(
                self.root, controller_name=self.session.stage, blocking=True
            ):
                current = read_lifecycle_state(self.root)
                assert current is not None
                lifecycle.write_lifecycle_state(
                    self.root, current.model_copy(update={"requested_action": "pause"})
                )

    monkeypatch.setattr(LifecycleContext, "checkpoint", checkpoint_with_pause)

    stage = IngestionStage()
    refused = run_lifecycle_worker(stage, root, SESSION_ID)
    assert refused.lifecycle == "paused"
    # The capture-to-normalization gate refused, so no normalized row was published.
    assert normalized_database_digest(factory) == before

    # Status never prunes. An ordinary idle stop deletes only the unretained capture and
    # preserves every retained byte and the pre-import database digest.
    retained_manifest_bytes = store.manifest_path.read_bytes()
    envelope_bytes = {
        path.name: path.read_bytes() for path in sorted((store.run_root / "responses").glob("*"))
    }
    status = local_stages.run_ingestion_command("status", runner=runner)
    assert status.lifecycle == "paused"
    assert store.run_root.is_dir() and older.is_dir()

    _clear_owned_process(root)
    stopped = local_stages.run_ingestion_command("stop", runner=runner)
    assert stopped.lifecycle == "idle"
    assert not older.exists()
    assert store.manifest_path.read_bytes() == retained_manifest_bytes
    assert {
        path.name: path.read_bytes() for path in sorted((store.run_root / "responses").glob("*"))
    } == envelope_bytes
    assert normalized_database_digest(factory) == before

    # Re-arm the retained state with its recorded owner and complete through the worker.
    requested["enabled"] = False
    state = read_lifecycle_state(root)
    assert state is not None
    lifecycle.write_lifecycle_state(
        root, state.model_copy(update={"requested_action": "run", "process": process})
    )
    completed = run_lifecycle_worker(stage, root, SESSION_ID)
    assert completed.lifecycle == "completed"
    final_state = read_lifecycle_state(root)
    assert final_state is not None
    assert isinstance(final_state.progress, IngestionProgress)
    assert final_state.progress.normalized is True
    assert final_state.progress.dropped_period_membership_count == 1
    after = normalized_database_digest(factory)
    counts = {entry[0]: len(entry) - 1 for entry in after}
    assert counts["course_versions"] == 6
    assert counts["module_versions"] == 3

    # The normalization result is idempotent: a repeat worker run writes the same rows.
    lifecycle.write_lifecycle_state(
        root, final_state.model_copy(update={"requested_action": "run"})
    )
    assert run_lifecycle_worker(stage, root, SESSION_ID).lifecycle == "completed"
    assert normalized_database_digest(factory) == after

    # A completed idle stop prunes the whole raw root while normalized and provenance rows stay.
    provenance_counts = _provenance_counts(factory)
    _clear_owned_process(root)
    final_stop = local_stages.run_ingestion_command("stop", runner=runner)
    assert final_stop.lifecycle == "idle"
    final_idle = read_lifecycle_state(root)
    assert final_idle is not None and final_idle.work_complete is True
    assert not store.run_root.exists()
    assert normalized_database_digest(factory) == after
    assert _provenance_counts(factory) == provenance_counts


def test_import_verification_prefix_resumes_from_retained_evidence(
    database: tuple[object, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened current run re-checks its whole retained prefix before normalization."""
    _, factory = database
    capture_repository = CaptureRepository(factory)
    # The fixture store records a cwd-relative manifest path, so build and open it from tmp_path.
    monkeypatch.chdir(tmp_path)
    store = build_completed_fixture_store(tmp_path, capture_repository)
    total = len(store.manifest.responses)

    capture_settings = settings_factory(CaptureSettings).model_copy(
        update={"raw_data_root": store.run_root.parent}
    )
    normalization_settings = settings_factory(NormalizationSettings)
    monkeypatch.setattr(
        ingestion_commands,
        "_database_context",
        lambda settings, **_kwargs: contextlib.nullcontext(factory),
    )
    monkeypatch.setattr(ingestion_commands, "load_capture_settings", lambda: capture_settings)
    monkeypatch.setattr(
        ingestion_commands, "load_normalization_settings", lambda: normalization_settings
    )
    write_dataset_roles()

    # Corrupt an envelope in the retained prefix while leaving manifest hashes/bytes untouched.
    course_response = next(
        response
        for response in store.manifest.responses
        if response.object_type is SourceObjectType.COURSE_UNIT and response.objects
    )
    offset = store.manifest.responses.index(course_response) + 1
    envelope_path = store.run_root / course_response.response_path
    original_bytes = envelope_path.read_bytes()
    body = json.loads(gzip.decompress(original_bytes))
    body[0]["content"]["en"] = "corrupt prefix content"
    envelope_path.write_bytes(
        gzip.compress(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
            compresslevel=9,
            mtime=0,
        )
    )

    before = normalized_database_digest(factory)
    with pytest.raises(ValueError):
        ingestion_commands.run_capture_command(
            resume_manifest=Path(store.manifest_relative_path),
            verification_start_index=offset,
        )
    # The reopened run refuses the durable offset and detects the corrupt prefix before any write.
    assert normalized_database_digest(factory) == before

    envelope_path.write_bytes(original_bytes)
    verification_boundaries: list[tuple[int, int | None]] = []

    def boundary(stage: str, completed: int, total: int | None) -> bool:
        if stage == "verification":
            verification_boundaries.append((completed, total))
        return True

    result = ingestion_commands.run_capture_command(
        resume_manifest=Path(store.manifest_relative_path),
        verification_start_index=offset,
        on_boundary=boundary,  # type: ignore[arg-type]
    )

    assert result is not None
    assert result.capture.response_count == total
    # One entry verification pass starts at zero. The completed store adds no parser/report pass.
    assert verification_boundaries == [(index, total) for index in range(total)]
    assert result.normalization.new_course_version_count == 6


_PERIODS_BODY = b'{"periods":[{"id":"p-multi","start":2024,"end":2027}]}'
_COURSE_BODY = (
    b'{"record":{"id":"wrapped-course-v1","groupId":"wrapped-course",'
    b'"code":"WRAP.1","credits":null,"documentState":null,'
    b'"curriculumPeriodIds":["p-multi"],"name":{"fi":"Kurssi","en":"Course"}}}'
)


def _wrapped_extracted_hash() -> str:
    """Return the hash the fixture family computes over its extracted neutral record."""
    return json_sha256(
        {
            "id": "wrapped-course-v1",
            "groupId": "wrapped-course",
            "code": "WRAP.1",
            "credits": None,
            "documentState": None,
            "curriculumPeriodIds": ["p-multi"],
        },
        ensure_ascii=False,
    )


class _WrappedEvidenceParser:
    """One fixture family parser over wrapped JSON evidence and a multi-year period."""

    def decode_objects(self, response, object_type, *, store=None):
        del store
        payload = json.loads(response.body)
        record = payload["record"]
        extracted = {
            "id": record["id"],
            "groupId": record["groupId"],
            "code": record["code"],
            "credits": record["credits"],
            "documentState": record["documentState"],
            "curriculumPeriodIds": record["curriculumPeriodIds"],
        }
        return (
            DecodedSourceObject(
                source_object_id=extracted["id"],
                source_group_id=extracted["groupId"],
                source_hash=json_sha256(extracted, ensure_ascii=False),
                value=record,
            ),
        )

    def read_periods(self, store: RawCaptureStore) -> tuple[SourcePeriod, ...]:
        response = tuple(store.iter_responses(request_path="/api/periods"))
        assert len(response) == 1
        payload = json.loads(response[0].body)
        return tuple(
            SourcePeriod(
                id=item["id"],
                academic_year_start=item["start"],
                academic_year_end=item["end"],
            )
            for item in payload["periods"]
        )

    def parse_object(self, replayed) -> NormalizedCourseVersion:
        value = replayed.value
        return NormalizedCourseVersion(
            provenance=SnapshotProvenance(
                object_type=replayed.record.object_type,
                source_object_id=replayed.record.source_object_id,
                source_group_id=replayed.record.source_group_id,
                response_path=replayed.response_path,
                response_item_index=replayed.record.response_item_index,
                source_hash=replayed.record.source_hash,
            ),
            group_id=value["groupId"],
            code=value["code"],
            texts={
                OfficialTextField.NAME: {
                    language: text
                    for language, text in (
                        ("fi", value["name"].get("fi")),
                        ("en", value["name"].get("en")),
                    )
                    if text is not None
                }
            },
            credits_min=None,
            credits_max=None,
            official_url="https://example.test/wrapped-course-v1",
            document_state=None,
            validity_start=None,
            validity_end=None,
            source_revision=None,
            source_modification_ordinal=None,
            curriculum_period_ids=tuple(value["curriculumPeriodIds"]),
            prerequisite_options=(),
            substitution_options=(),
        )


class _WrappedEvidenceSource:
    """One resumable fixture family source that writes wrapped evidence to the shared checkpoints."""

    def __init__(self, parser: _WrappedEvidenceParser) -> None:
        self._parser = parser

    @staticmethod
    def _has(store: RawCaptureStore, request_path: str) -> bool:
        return any(response.request_path == request_path for response in store.manifest.responses)

    def capture_pending(
        self,
        store: RawCaptureStore,
        repository: CaptureRepository,
        *,
        checkpoint_response_count: int,
        on_boundary,
    ) -> bool:
        del checkpoint_response_count
        if not self._has(store, "/api/periods"):
            record_responses(
                store,
                repository,
                (
                    SourceResponse(
                        CapturedResponse("/api/periods", (("instance", "fixture"),), _PERIODS_BODY),
                        (),
                    ),
                ),
            )
            store.set_periods((CurriculumPeriodRef("p-multi", 2024),))
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            return False
        if not self._has(store, "/api/courses"):
            record_responses(
                store,
                repository,
                (
                    SourceResponse(
                        CapturedResponse("/api/courses", (("id", "wrapped"),), _COURSE_BODY),
                        ({},),
                    ),
                ),
                object_type=SourceObjectType.COURSE_UNIT,
            )
        return True


def test_imports_neutral_records_from_non_kori_evidence(
    database: tuple[object, object], tmp_path: Path
) -> None:
    """A non-array fixture family imports through the shared spine after a checkpoint pause."""
    _, factory = database
    parser = _WrappedEvidenceParser()
    raw_root = tmp_path / "raw"
    store = RawCaptureStore.create(raw_root, parser=parser)
    capture_repository = CaptureRepository(factory)

    paused = import_curriculum(
        source=_WrappedEvidenceSource(parser),
        store=store,
        capture_repository=capture_repository,
        normalization_repository=NormalizationRepository(
            factory, university=UNIVERSITIES["example"]
        ),
        checkpoint_response_count=1,
        current_academic_year_start=2026,
        activity_window_years=3,
        on_boundary=lambda stage, completed, total: not (stage == "capture" and completed >= 1),
    )
    assert paused is None
    assert store.manifest.status is IngestionRunStatus.RUNNING

    reopened = RawCaptureStore.open(raw_root, store.manifest_path, parser=parser)
    report = import_curriculum(
        source=_WrappedEvidenceSource(parser),
        store=reopened,
        capture_repository=capture_repository,
        normalization_repository=NormalizationRepository(
            factory, university=UNIVERSITIES["example"]
        ),
        checkpoint_response_count=1,
        current_academic_year_start=2026,
        activity_window_years=3,
    )
    assert report is not None
    assert report.capture.status is IngestionRunStatus.COMPLETED
    assert report.capture.course_object_count == 1
    assert report.normalization.course_version_count == 1
    assert report.normalization.period_count == 3

    with factory() as session:
        version = session.scalar(
            select(CourseVersion).where(CourseVersion.source_version_id == "wrapped-course-v1")
        )
        assert version is not None
        assert version.credits_min is None
        assert version.document_state is None
        memberships = tuple(
            session.scalars(
                select(CourseVersionCurriculumPeriod.curriculum_period_id)
                .where(CourseVersionCurriculumPeriod.course_version_id == version.id)
                .order_by(CourseVersionCurriculumPeriod.curriculum_period_id)
            )
        )
        assert memberships == (
            "p-multi:2024-2025",
            "p-multi:2025-2026",
            "p-multi:2026-2027",
        )
        assert (
            session.scalar(
                select(CurriculumPeriod.id).where(CurriculumPeriod.id == "p-multi:2025-2026")
            )
            == "p-multi:2025-2026"
        )
        snapshot = session.get(SourceSnapshot, version.source_snapshot_id)
        assert snapshot is not None
        assert snapshot.source_hash == _wrapped_extracted_hash()
