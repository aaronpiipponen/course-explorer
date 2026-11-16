"""Sisu discovery, exact fetching, typed reference closure and shared-checkpoint driving."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import get_args

from course_explorer.enums import SourceObjectType
from course_explorer.ingestion.capture import (
    CaptureBoundary,
    boundary_allows,
    next_controlled_batch,
    record_responses,
)
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.references import (
    SourceReferences,
    extract_course_references,
    extract_module_references,
)
from course_explorer.ingestion.repository import CapturePersistence
from course_explorer.ingestion.source import (
    KoriModuleType,
    SisuSource,
    SourceObject,
    SourceResponse,
    SourceVersionRef,
)

_TaggedGroup = tuple[SourceObjectType, str]


class _CaptureState:
    """Invocation-local accumulator of represented objects and pending typed references.

    It is seeded once from the incoming verified manifest, then updated directly from each
    committed exact/group response, so closure discovery never reconstructs the catalogue.
    """

    def __init__(self) -> None:
        self.represented_ids: set[_TaggedGroup] = set()
        self.known_groups: set[_TaggedGroup] = set()
        self.processed: set[_TaggedGroup] = set()
        self.pending: set[_TaggedGroup] = set()
        self.unresolved_course: set[str] = set()
        self.unresolved_module: set[str] = set()

    def add_references(self, object_type: SourceObjectType, value: SourceObject) -> None:
        """Extract one unique captured object's typed references through the reference helpers."""
        identifier = value.get("id")
        if not isinstance(identifier, str):
            raise ValueError("captured source object ID is not a string")
        key = (object_type, identifier)
        if key in self.processed:
            return
        self.processed.add(key)
        references: SourceReferences = (
            extract_course_references(value)
            if object_type is SourceObjectType.COURSE_UNIT
            else extract_module_references(value)
        )
        self.pending.update(
            (SourceObjectType.COURSE_UNIT, group_id) for group_id in references.course_group_ids
        )
        self.pending.update(
            (SourceObjectType.MODULE, group_id) for group_id in references.module_group_ids
        )

    def observe_object(self, object_type: SourceObjectType, value: SourceObject) -> None:
        """Record one captured object's identity, group, and references."""
        identifier = value.get("id")
        if not isinstance(identifier, str):
            raise ValueError("captured source object ID is not a string")
        group_id = value.get("groupId")
        if isinstance(group_id, str) and group_id.strip():
            self.known_groups.add((object_type, group_id))
        self.represented_ids.add((object_type, identifier))
        self.add_references(object_type, value)

    def observe_group(
        self, object_type: SourceObjectType, group_id: str, values: Sequence[SourceObject]
    ) -> None:
        """Record one fetched group as resolved, or as an empty unresolved reference."""
        if not values:
            if object_type is SourceObjectType.COURSE_UNIT:
                self.unresolved_course.add(group_id)
            else:
                self.unresolved_module.add(group_id)
            return
        self.known_groups.add((object_type, group_id))
        for value in values:
            self.observe_object(object_type, value)


def _seed_capture_state(store: RawCaptureStore) -> _CaptureState:
    """Seed the accumulator once by replaying the incoming verified manifest objects."""
    state = _CaptureState()
    state.unresolved_course = set(store.manifest.unresolved_course_group_ids)
    state.unresolved_module = set(store.manifest.unresolved_module_group_ids)
    for replayed in store.iter_objects():
        record = replayed.record
        state.represented_ids.add((record.object_type, record.source_object_id))
        state.known_groups.add((record.object_type, record.source_group_id))
        state.add_references(record.object_type, replayed.value)
    return state


def _capture_closure(
    source: SisuSource,
    store: RawCaptureStore,
    repository: CapturePersistence,
    *,
    state: _CaptureState,
    checkpoint_response_count: int,
    on_boundary: CaptureBoundary | None,
) -> bool:
    """Close typed references breadth-first, committing each fetched group response."""
    while state.pending:
        frontier = tuple(sorted(state.pending, key=lambda item: (item[0].value, item[1])))
        state.pending.clear()
        course_ids = tuple(
            group_id
            for object_type, group_id in frontier
            if object_type is SourceObjectType.COURSE_UNIT
            and (object_type, group_id) not in state.known_groups
            and group_id not in state.unresolved_course
        )
        module_ids = tuple(
            group_id
            for object_type, group_id in frontier
            if object_type is SourceObjectType.MODULE
            and (object_type, group_id) not in state.known_groups
            and group_id not in state.unresolved_module
        )
        for object_type, group_ids, responses in (
            (
                SourceObjectType.COURSE_UNIT,
                course_ids,
                source.iter_course_group_versions(course_ids),
            ),
            (SourceObjectType.MODULE, module_ids, source.iter_module_group_versions(module_ids)),
        ):
            if not group_ids:
                continue
            pairs_iterator = zip(group_ids, responses, strict=True)
            while True:
                pairs, allowed = next_controlled_batch(
                    pairs_iterator,
                    checkpoint_response_count,
                    on_boundary=on_boundary,
                    completed=len(store.manifest.responses),
                )
                if not allowed:
                    return False
                if not pairs:
                    break
                response_batch = tuple(response for _, response in pairs)
                persisted_course = set(state.unresolved_course)
                persisted_module = set(state.unresolved_module)
                for group_id, response in pairs:
                    if not response.value:
                        if object_type is SourceObjectType.COURSE_UNIT:
                            persisted_course.add(group_id)
                        else:
                            persisted_module.add(group_id)
                record_responses(
                    store,
                    repository,
                    response_batch,
                    object_type=object_type,
                    unresolved_course_group_ids=persisted_course,
                    unresolved_module_group_ids=persisted_module,
                )
                for group_id, response in pairs:
                    state.observe_group(object_type, group_id, response.value)
                if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
                    return False
    return True


def _record_reference_batches(
    responses: Iterator[SourceResponse[tuple[SourceVersionRef, ...]]],
    accumulated_ids: set[str],
    store: RawCaptureStore,
    repository: CapturePersistence,
    *,
    checkpoint_response_count: int,
    on_boundary: CaptureBoundary | None,
) -> bool:
    """Record reference-only responses in bounded batches and accumulate every discovered ID."""
    while True:
        batch, allowed = next_controlled_batch(
            responses,
            checkpoint_response_count,
            on_boundary=on_boundary,
            completed=len(store.manifest.responses),
        )
        if not allowed:
            return False
        if not batch:
            return True
        record_responses(store, repository, batch)
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            return False
        for response in batch:
            accumulated_ids.update(ref.id for ref in response.value)


def _discover_catalogue(
    source: SisuSource,
    store: RawCaptureStore,
    repository: CapturePersistence,
    *,
    checkpoint_response_count: int,
    on_boundary: CaptureBoundary | None,
) -> tuple[set[str], set[str]] | None:
    """Record periods and course/module version references, returning discovered IDs.

    ``None`` means the controller paused or stopped at a durable boundary. Source discovery is
    fresh on resume.
    """
    if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
        return None
    periods = source.get_curriculum_periods()
    record_responses(store, repository, (periods,))
    store.set_periods(periods.value)
    if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
        return None

    period_ids = tuple(period.id for period in periods.value)
    course_ids: set[str] = set()
    if not _record_reference_batches(
        source.iter_course_version_refs(period_ids),
        course_ids,
        store,
        repository,
        checkpoint_response_count=checkpoint_response_count,
        on_boundary=on_boundary,
    ):
        return None

    module_ids: set[str] = set()
    for module_type in get_args(KoriModuleType):
        if not _record_reference_batches(
            source.iter_module_version_refs(period_ids, module_type),
            module_ids,
            store,
            repository,
            checkpoint_response_count=checkpoint_response_count,
            on_boundary=on_boundary,
        ):
            return None
    return course_ids, module_ids


def _fetch_exact_objects(
    source: SisuSource,
    store: RawCaptureStore,
    repository: CapturePersistence,
    state: _CaptureState,
    *,
    object_type: SourceObjectType,
    missing_ids: Sequence[str],
    checkpoint_response_count: int,
    on_boundary: CaptureBoundary | None,
) -> bool:
    """Fetch missing exact versions in bounded batches and observe each committed response."""
    if not missing_ids:
        return True
    responses = (
        source.iter_course_versions(missing_ids)
        if object_type is SourceObjectType.COURSE_UNIT
        else source.iter_module_versions(missing_ids)
    )
    while True:
        batch, allowed = next_controlled_batch(
            responses,
            checkpoint_response_count,
            on_boundary=on_boundary,
            completed=len(store.manifest.responses),
        )
        if not allowed:
            return False
        if not batch:
            break
        record_responses(store, repository, batch, object_type=object_type)
        for response in batch:
            for value in response.value:
                state.observe_object(object_type, value)
        if not boundary_allows(on_boundary, "capture", len(store.manifest.responses), None):
            return False
    return True


class SisuCapture:
    """Drive Sisu discovery, exact fetching and typed closure into shared capture checkpoints."""

    def __init__(self, source: SisuSource) -> None:
        self._source = source

    def capture_pending(
        self,
        store: RawCaptureStore,
        repository: CapturePersistence,
        *,
        checkpoint_response_count: int,
        on_boundary: CaptureBoundary | None,
    ) -> bool:
        """Record pending Sisu evidence and return whether discovery and closure finished."""
        state = _seed_capture_state(store)
        discovered = _discover_catalogue(
            self._source,
            store,
            repository,
            checkpoint_response_count=checkpoint_response_count,
            on_boundary=on_boundary,
        )
        if discovered is None:
            return False
        course_ids, module_ids = discovered

        represented_course_ids = {
            identifier
            for object_type, identifier in state.represented_ids
            if object_type is SourceObjectType.COURSE_UNIT
        }
        represented_module_ids = {
            identifier
            for object_type, identifier in state.represented_ids
            if object_type is SourceObjectType.MODULE
        }
        if not _fetch_exact_objects(
            self._source,
            store,
            repository,
            state,
            object_type=SourceObjectType.COURSE_UNIT,
            missing_ids=sorted(course_ids - represented_course_ids),
            checkpoint_response_count=checkpoint_response_count,
            on_boundary=on_boundary,
        ):
            return False
        if not _fetch_exact_objects(
            self._source,
            store,
            repository,
            state,
            object_type=SourceObjectType.MODULE,
            missing_ids=sorted(module_ids - represented_module_ids),
            checkpoint_response_count=checkpoint_response_count,
            on_boundary=on_boundary,
        ):
            return False
        return _capture_closure(
            self._source,
            store,
            repository,
            state=state,
            checkpoint_response_count=checkpoint_response_count,
            on_boundary=on_boundary,
        )
