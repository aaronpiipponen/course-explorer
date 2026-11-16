"""Resumable immutable capture orchestration and shared checkpoint helpers."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from itertools import batched, islice
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from course_explorer.enums import (
    IngestionRunStatus,
    SourceObjectType,
)
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.repository import CapturePersistence
from course_explorer.ingestion.source import (
    CurriculumSource,
    SourceObject,
    SourceResponse,
)


class CaptureReport(BaseModel):
    """Operator-visible counts derived from one verified capture manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: IngestionRunStatus
    manifest_path: str
    response_count: int
    course_object_count: int
    module_object_count: int
    unresolved_course_group_count: int
    unresolved_module_group_count: int
    selected_period_count: int = Field(ge=0)


_ItemT = TypeVar("_ItemT")

CaptureBoundary = Callable[
    [Literal["verification", "capture", "before_normalization"], int, int | None], bool
]
"""Cooperative lifecycle gate over durable response units.

The callback receives only a stage name plus completed/total response counts. It never receives
captured content, a path, exception text, or source identity. The total is known only while
verifying a manifest. Returning ``False`` means the controller asked to pause or stop
at this durable boundary.
"""


def _report(store: RawCaptureStore) -> CaptureReport:
    object_ids = {
        (record.object_type, record.source_object_id)
        for response in store.manifest.responses
        for record in response.objects
    }
    return CaptureReport(
        run_id=store.manifest.run_id,
        status=store.manifest.status,
        manifest_path=store.manifest_relative_path,
        response_count=len(store.manifest.responses),
        course_object_count=sum(
            object_type is SourceObjectType.COURSE_UNIT for object_type, _ in object_ids
        ),
        module_object_count=sum(
            object_type is SourceObjectType.MODULE for object_type, _ in object_ids
        ),
        unresolved_course_group_count=len(store.manifest.unresolved_course_group_ids),
        unresolved_module_group_count=len(store.manifest.unresolved_module_group_ids),
        selected_period_count=len(store.manifest.periods),
    )


class BoundaryRefused(Exception):
    """Internal unwind signal: the controller asked to pause or stop before a request."""


def boundary_allows(
    on_boundary: CaptureBoundary | None,
    stage: Literal["verification", "capture", "before_normalization"],
    completed: int,
    total: int | None,
) -> bool:
    """Consult the optional lifecycle gate, defaulting to uninterrupted progress."""
    if on_boundary is None:
        return True
    return on_boundary(stage, completed, total)


def next_controlled_batch(  # noqa: UP047 (shared helper keeps the explicit TypeVar contract)
    iterator: Iterator[_ItemT],
    size: int,
    *,
    on_boundary: CaptureBoundary | None,
    completed: int,
) -> tuple[tuple[_ItemT, ...], bool]:
    """Pull one source batch only after the controller allows a request.

    The cooperative gate is consulted immediately before consuming from the iterator, so a pause or
    stop arriving after the previous durable checkpoint is observed before any further source work
    starts. ``((), False)`` means the controller asked to pause or stop. ``((), True)`` means the
    iterator was exhausted normally. Otherwise one batch of at most ``size`` items is returned.
    """
    if not boundary_allows(on_boundary, "capture", completed, None):
        return (), False
    return tuple(islice(iterator, size)), True


def record_responses(
    store: RawCaptureStore,
    repository: CapturePersistence,
    responses: Sequence[SourceResponse[tuple[SourceObject, ...]]],
    *,
    object_type: SourceObjectType | None = None,
    unresolved_course_group_ids: Collection[str] | None = None,
    unresolved_module_group_ids: Collection[str] | None = None,
) -> None:
    """Checkpoint one batch of authoritative response bytes and register its provenance."""
    records = store.checkpoint_responses(
        tuple((response.captured, object_type) for response in responses),
        unresolved_course_group_ids=unresolved_course_group_ids,
        unresolved_module_group_ids=unresolved_module_group_ids,
    )
    repository.register_responses(store.manifest.run_id, records)


class ResponseRecorder:
    """Accumulate successful responses into shared checkpoints of one object type."""

    def __init__(
        self,
        store: RawCaptureStore,
        repository: CapturePersistence,
        *,
        batch_size: int,
    ) -> None:
        self._store = store
        self._repository = repository
        self._batch_size = batch_size
        self._pending: list[SourceResponse[Any]] = []
        self._object_type: SourceObjectType | None = None

    def add(self, response: SourceResponse[Any], object_type: SourceObjectType | None) -> None:
        """Queue one response and flush when its object type changes or the batch is full."""
        if self._pending and object_type is not self._object_type:
            self.flush()
        self._object_type = object_type
        self._pending.append(response)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self, *, unresolved_course_group_ids: Collection[str] | None = None) -> None:
        """Record every queued response and an optional unresolved course update."""
        if not self._pending and unresolved_course_group_ids is None:
            return
        batch, self._pending = self._pending, []
        record_responses(
            self._store,
            self._repository,
            batch,
            object_type=self._object_type,
            unresolved_course_group_ids=unresolved_course_group_ids,
        )


def _reconcile_manifest(
    store: RawCaptureStore,
    repository: CapturePersistence,
    *,
    checkpoint_response_count: int,
    verification_start_index: int,
    on_boundary: CaptureBoundary | None,
) -> bool:
    """Repair database provenance only from evidence already verified on disk.

    A same-invocation resume verifies only the envelopes this instance has not checked, while
    a reopened store checks its whole retained prefix. Either way provenance reconciliation then
    runs for the complete manifest in durable batches. ``False`` means the controller asked to
    pause or stop, so nothing further runs this invocation.
    """
    if not store.verify(
        start_response_index=verification_start_index,
        before_response=lambda completed, total: boundary_allows(
            on_boundary, "verification", completed, total
        ),
    ):
        return False
    repository.ensure_run(store.manifest, store.manifest_relative_path)
    for responses in batched(store.manifest.responses, checkpoint_response_count):
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            return False
        repository.register_responses(store.manifest.run_id, responses)
    return True


def capture_curriculum(
    source: CurriculumSource,
    store: RawCaptureStore,
    repository: CapturePersistence,
    *,
    checkpoint_response_count: int,
    verification_start_index: int = 0,
    on_boundary: CaptureBoundary | None = None,
) -> CaptureReport | None:
    """Reconcile incoming evidence, run family work, and finalize one verified capture.

    ``None`` means the controller paused or stopped at a durable boundary: the manifest holds
    whatever running/failed state it reached after the last committed checkpoint and is never
    relabelled as a source failure. The one exception is the completed-manifest checkpoint
    published after ``mark_completed()`` and matching provenance: rejecting that checkpoint
    returns ``None`` with completed manifest bytes plus matching provenance committed, so the
    next ordinary start continues into normalization instead of losing the final identity. Real
    source, evidence, or database errors mark the manifest failed.
    """
    if checkpoint_response_count < 1:
        raise ValueError("checkpoint response count must be at least 1")
    incoming_response_count = len(store.manifest.responses)
    try:
        if not _reconcile_manifest(
            store,
            repository,
            checkpoint_response_count=checkpoint_response_count,
            verification_start_index=verification_start_index,
            on_boundary=on_boundary,
        ):
            return None
        if store.manifest.status is IngestionRunStatus.COMPLETED:
            repository.verify_run(store.manifest, store.manifest_relative_path)
            return _report(store)
        store.mark_running()
        repository.ensure_run(store.manifest, store.manifest_relative_path)

        if not source.capture_pending(
            store,
            repository,
            checkpoint_response_count=checkpoint_response_count,
            on_boundary=on_boundary,
        ):
            return None
        if not store.verify(
            start_response_index=incoming_response_count,
            before_response=lambda completed, total: boundary_allows(
                on_boundary, "verification", completed, total
            ),
        ):
            return None
        store.mark_completed()
        repository.ensure_run(store.manifest, store.manifest_relative_path)
        # Completion rewrites the manifest bytes and publishes matching completed provenance, so
        # this is a distinct durable checkpoint before the completed-directory final-set check.
        # A pause/stop observed here must record the completed manifest identity. The shorter final
        # verification below only re-runs the directory/orphan/final-set block.
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            return None
        if not store.verify(
            start_response_index=len(store.manifest.responses),
            before_response=lambda completed, total: boundary_allows(
                on_boundary, "verification", completed, total
            ),
        ):
            return None
        repository.verify_run(store.manifest, store.manifest_relative_path)
        return _report(store)
    except Exception as original_error:
        if store.manifest.status is IngestionRunStatus.COMPLETED:
            raise
        try:
            store.mark_failed(original_error)
            repository.ensure_run(store.manifest, store.manifest_relative_path)
        except Exception as persistence_error:
            raise ExceptionGroup(
                "capture and failure-state persistence failed", [original_error, persistence_error]
            ) from original_error
        raise
