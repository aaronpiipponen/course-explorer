"""Immutable public curriculum-source contracts and captured response evidence."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

if TYPE_CHECKING:
    from course_explorer.enums import SourceObjectType
    from course_explorer.ingestion.capture import CaptureBoundary
    from course_explorer.ingestion.manifest import RawCaptureStore, ReplayedSourceObject
    from course_explorer.ingestion.normalization import (
        NormalizedCourseVersion,
        NormalizedModuleVersion,
        SnapshotProvenance,
    )
    from course_explorer.ingestion.repository import CapturePersistence

KoriModuleType = Literal["DegreeProgramme", "StudyModule"]


@dataclass(frozen=True)
class CapturedResponse:
    """Exact successful response bytes and ordered request parameters for capture."""

    request_path: str
    request_params: tuple[tuple[str, str], ...]
    body: bytes


T = TypeVar("T")


@dataclass(frozen=True)
class SourceResponse(Generic[T]):  # noqa: UP046 (contract uses explicit Generic[T] form)
    """A validated source value paired with its exact HTTP response evidence."""

    captured: CapturedResponse
    value: T


@dataclass(frozen=True)
class CurriculumPeriodRef:
    """A selected source curriculum-period ID and its parsed academic-year start."""

    id: str
    academic_year_start: int


@dataclass(frozen=True)
class SourceVersionRef:
    """An exact source version ID, canonical group ID, and period memberships."""

    id: str
    group_id: str
    curriculum_period_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourcePeriod:
    """One published source period over its academic-year interval.

    ``academic_year_end`` is exclusive, so one academic year spans
    ``academic_year_start`` to ``academic_year_start + 1``.
    """

    id: str
    academic_year_start: int
    academic_year_end: int


SourceObject = dict[str, Any]


@dataclass(frozen=True)
class DecodedSourceObject:
    """One source object decoded from authoritative response bytes with its identity and hash."""

    source_object_id: str
    source_group_id: str
    source_hash: str
    value: SourceObject


class KoriRequestError(RuntimeError):
    """A public Kori request failed after its configured retry budget."""


class KoriContractError(ValueError):
    """A public Kori response violates the source contract."""


class SisuSource(Protocol):
    """Stable Sisu source seam consumed by the shared capture driver."""

    def get_curriculum_periods(self) -> SourceResponse[tuple[CurriculumPeriodRef, ...]]: ...

    def iter_course_version_refs(
        self, period_ids: Sequence[str]
    ) -> Iterator[SourceResponse[tuple[SourceVersionRef, ...]]]: ...

    def iter_module_version_refs(
        self, period_ids: Sequence[str], module_type: KoriModuleType
    ) -> Iterator[SourceResponse[tuple[SourceVersionRef, ...]]]: ...

    def iter_course_versions(
        self, ids: Sequence[str]
    ) -> Iterator[SourceResponse[tuple[SourceObject, ...]]]: ...

    def iter_module_versions(
        self, ids: Sequence[str]
    ) -> Iterator[SourceResponse[tuple[SourceObject, ...]]]: ...

    def get_course_group_versions(
        self, group_id: str
    ) -> SourceResponse[tuple[SourceObject, ...]]: ...

    def get_module_group_versions(
        self, group_id: str
    ) -> SourceResponse[tuple[SourceObject, ...]]: ...

    def iter_course_group_versions(
        self, group_ids: Sequence[str]
    ) -> Iterator[SourceResponse[tuple[SourceObject, ...]]]: ...

    def iter_module_group_versions(
        self, group_ids: Sequence[str]
    ) -> Iterator[SourceResponse[tuple[SourceObject, ...]]]: ...


class CurriculumParser(Protocol):
    """Family-owned decoding of captured bytes into neutral normalized records."""

    def decode_objects(
        self,
        response: CapturedResponse,
        object_type: SourceObjectType,
        *,
        store: RawCaptureStore | None = None,
    ) -> tuple[DecodedSourceObject, ...]:
        """Decode one stored response into its ordered records.

        A family whose response does not carry its own period reads the already recorded
        discovery evidence through ``store``. A family that carries its period ignores the
        argument. A family that needs it raises one clear contract error when it is absent.
        """
        ...

    def read_periods(self, store: RawCaptureStore) -> tuple[SourcePeriod, ...]: ...

    def parse_object(
        self, replayed: ReplayedSourceObject
    ) -> NormalizedCourseVersion | NormalizedModuleVersion | SnapshotProvenance: ...


class CurriculumSource(Protocol):
    """Neutral source seam that records family discovery and fetching into shared checkpoints."""

    def capture_pending(
        self,
        store: RawCaptureStore,
        repository: CapturePersistence,
        *,
        checkpoint_response_count: int,
        on_boundary: CaptureBoundary | None,
    ) -> bool: ...
