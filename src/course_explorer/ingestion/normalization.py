"""Shared neutral normalization of one completed capture.

This module owns the family-independent normalized record vocabulary, period expansion, published
membership filtering, whole-batch deduplication and reference-closure validation. The configured
family parser converts each replayed source object into one normalized record, and the transactional
normalization repository consumes the resulting batch.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from course_explorer.enums import (
    IngestionRunStatus,
    ModuleRuleType,
    ModuleType,
    OfficialTextField,
    PrerequisiteMemberType,
    PrerequisiteRequirementKind,
    SourceObjectType,
)
from course_explorer.ingestion.manifest import RawCaptureStore
from course_explorer.ingestion.source import CurriculumPeriodRef, SourcePeriod


class _OutputModel(BaseModel):
    """Frozen DTO base that prevents accidental normalized-field expansion."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NormalizedPeriod(_OutputModel):
    """One curriculum period with its id and starting academic year."""

    id: str
    academic_year_start: int


class SnapshotProvenance(_OutputModel):
    """The captured source position and hash of one normalized record."""

    object_type: SourceObjectType
    source_object_id: str
    source_group_id: str
    response_path: str
    response_item_index: int
    source_hash: str


class NormalizedPrerequisiteMember(_OutputModel):
    """One referenced course or module group of a normalized prerequisite option."""

    member_index: int
    member_type: PrerequisiteMemberType
    referenced_group_id: str


class NormalizedPrerequisiteOption(_OutputModel):
    """One option of a normalized prerequisite requirement."""

    option_index: int
    requirement_kind: PrerequisiteRequirementKind
    members: tuple[NormalizedPrerequisiteMember, ...]


class NormalizedSubstitutionMember(_OutputModel):
    """One course member of a normalized substitution option."""

    member_index: int
    course_group_id: str
    credits: Decimal | None


class NormalizedSubstitutionOption(_OutputModel):
    """One option of a normalized substitution."""

    option_index: int
    members: tuple[NormalizedSubstitutionMember, ...]


class NormalizedModuleRule(_OutputModel):
    """One node of a normalized module rule tree with its bounds and children."""

    sibling_position: int
    local_id: str | None
    node_type: ModuleRuleType
    require_min: int | None
    require_max: int | None
    all_mandatory: bool | None
    description: dict[str, str] | None
    credits_min: Decimal | None
    credits_max: Decimal | None
    count_min: int | None
    count_max: int | None
    referenced_group_id: str | None
    children: tuple[NormalizedModuleRule, ...]


class NormalizedCourseVersion(_OutputModel):
    """One course version parsed from one capture, with its official text and rules."""

    provenance: SnapshotProvenance
    group_id: str
    code: str
    texts: dict[OfficialTextField, dict[str, str | None]]
    credits_min: Decimal | None
    credits_max: Decimal | None
    official_url: str
    document_state: str | None
    validity_start: date | None
    validity_end: date | None
    source_revision: int | None
    source_modification_ordinal: int | None
    curriculum_period_ids: tuple[str, ...]
    prerequisite_options: tuple[NormalizedPrerequisiteOption, ...]
    substitution_options: tuple[NormalizedSubstitutionOption, ...]


class NormalizedModuleVersion(_OutputModel):
    """One module version parsed from one capture, with its names and rule tree."""

    provenance: SnapshotProvenance
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
    rule: NormalizedModuleRule | None


class NormalizationBatch(_OutputModel):
    """Every validated period, course version and module version one normalization run produces."""

    run_id: UUID
    periods: tuple[NormalizedPeriod, ...]
    covered_period_ids: tuple[str, ...]
    courses: tuple[NormalizedCourseVersion, ...]
    excluded_null_code_course_versions: tuple[SnapshotProvenance, ...]
    modules: tuple[NormalizedModuleVersion, ...]
    unresolved_course_group_ids: tuple[str, ...]
    unresolved_module_group_ids: tuple[str, ...]
    dropped_period_membership_count: int = Field(default=0, ge=0)


class NormalizationReport(_OutputModel):
    """Counts and change classifications produced by one transactional apply."""

    run_id: UUID
    period_count: int = Field(ge=0)
    canonical_course_count: int = Field(ge=0)
    active_course_count: int = Field(ge=0)
    canonical_module_count: int = Field(ge=0)
    course_version_count: int = Field(ge=0)
    excluded_null_code_course_versions: tuple[SnapshotProvenance, ...]
    new_course_version_count: int = Field(ge=0)
    changed_course_version_count: int = Field(ge=0)
    unchanged_course_version_count: int = Field(ge=0)
    module_version_count: int = Field(ge=0)
    new_module_version_count: int = Field(ge=0)
    changed_module_version_count: int = Field(ge=0)
    unchanged_module_version_count: int = Field(ge=0)
    unresolved_course_group_count: int = Field(ge=0)
    unresolved_module_group_count: int = Field(ge=0)
    unrestricted_rule_count: int = Field(ge=0)
    dropped_period_membership_count: int = Field(ge=0)


class NormalizationPersistence(Protocol):
    """Transactional writer for one normalized batch."""

    def apply(
        self,
        batch: NormalizationBatch,
        *,
        current_academic_year_start: int,
        activity_window_years: int,
    ) -> NormalizationReport: ...


def walk_normalized_rule(rule: NormalizedModuleRule) -> Iterator[NormalizedModuleRule]:
    """Yield one normalized module-rule tree depth-first, deepest children last."""
    yield rule
    for child in rule.children:
        yield from walk_normalized_rule(child)


def _expanded_periods(
    source_periods: tuple[SourcePeriod, ...],
) -> tuple[
    tuple[NormalizedPeriod, ...],
    dict[str, tuple[str, ...]],
]:
    """Expand published source periods into annual memberships.

    A one-year source period keeps its ID. A multi-year interval ``[start, end)`` expands to one
    period per academic year in year order.
    """
    periods: list[NormalizedPeriod] = []
    expansion: dict[str, tuple[str, ...]] = {}
    for source in source_periods:
        if source.id in expansion:
            raise ValueError("source period IDs must be unique")
        if (
            source.academic_year_start <= 0
            or source.academic_year_end <= source.academic_year_start
        ):
            raise ValueError("source period must span at least one positive academic year")
        years = tuple(range(source.academic_year_start, source.academic_year_end))
        if len(years) == 1:
            identifiers = (source.id,)
        else:
            identifiers = tuple(f"{source.id}:{year}-{year + 1}" for year in years)
        expansion[source.id] = identifiers
        for year, identifier in zip(years, identifiers, strict=True):
            periods.append(NormalizedPeriod(id=identifier, academic_year_start=year))
    return tuple(periods), expansion


def _expanded_memberships(
    period_ids: tuple[str, ...],
    expansion: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], int]:
    """Return expanded published memberships and the dropped source-membership count."""
    memberships: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for identifier in period_ids:
        expanded = expansion.get(identifier)
        if expanded is None:
            dropped += 1
            continue
        for expanded_id in expanded:
            if expanded_id in seen:
                continue
            seen.add(expanded_id)
            memberships.append(expanded_id)
    return tuple(memberships), dropped


def _covered_period_ids(
    selected_periods: tuple[CurriculumPeriodRef, ...],
    expansion: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """Expand the capture's selected source periods into the annual periods it covers.

    Only the selected capture interval is covered by this import. A published period outside it
    stays a published membership source without being reconciled by this run.
    """
    covered: list[str] = []
    seen: set[str] = set()
    for source in selected_periods:
        expanded = expansion[source.id]
        for identifier in expanded:
            if identifier in seen:
                continue
            seen.add(identifier)
            covered.append(identifier)
    return tuple(covered)


def _validate_reference_closure(
    courses: tuple[NormalizedCourseVersion, ...],
    modules: tuple[NormalizedModuleVersion, ...],
    excluded_course_group_ids: frozenset[str],
    unresolved_courses: tuple[str, ...],
    unresolved_modules: tuple[str, ...],
) -> None:
    represented_courses = {course.group_id for course in courses}
    resolved_courses = represented_courses | excluded_course_group_ids
    represented_modules = {module.group_id for module in modules}
    if resolved_courses & set(unresolved_courses) or represented_modules & set(unresolved_modules):
        raise ValueError("represented and unresolved typed group IDs overlap")
    for course in courses:
        references = {
            member.referenced_group_id
            for option in course.prerequisite_options
            for member in option.members
            if member.member_type is PrerequisiteMemberType.COURSE
        }
        references.update(
            member.course_group_id
            for option in course.substitution_options
            for member in option.members
        )
        invalid = references - resolved_courses - set(unresolved_courses)
        if invalid:
            raise ValueError(f"unresolved course references absent from closure: {sorted(invalid)}")
        module_references = {
            member.referenced_group_id
            for option in course.prerequisite_options
            for member in option.members
            if member.member_type is PrerequisiteMemberType.MODULE
        }
        invalid_modules = module_references - represented_modules - set(unresolved_modules)
        if invalid_modules:
            raise ValueError(
                f"unresolved module references absent from closure: {sorted(invalid_modules)}"
            )
    for module in modules:
        if module.rule is None:
            continue
        for node in walk_normalized_rule(module.rule):
            if node.node_type is ModuleRuleType.COURSE_UNIT_RULE:
                if node.referenced_group_id not in resolved_courses | set(unresolved_courses):
                    raise ValueError("module rule references absent from course closure")
            elif node.node_type is ModuleRuleType.MODULE_RULE:
                if node.referenced_group_id not in represented_modules | set(unresolved_modules):
                    raise ValueError("module rule references absent from module closure")


def parse_capture(store: RawCaptureStore) -> NormalizationBatch:
    """Parse the current run's completed verified capture into immutable normalized DTOs."""
    if store.manifest.status is not IngestionRunStatus.COMPLETED:
        raise ValueError("normalization requires a completed capture manifest")

    source_periods = store.parser.read_periods(store)
    periods, expansion = _expanded_periods(source_periods)

    courses: list[NormalizedCourseVersion] = []
    excluded_null_code_course_versions: list[SnapshotProvenance] = []
    modules: list[NormalizedModuleVersion] = []
    seen: dict[tuple[SourceObjectType, str], tuple[str, str]] = {}
    dropped_period_membership_count = 0
    for replayed in store.iter_objects():
        key = (replayed.record.object_type, replayed.record.source_object_id)
        identity = (replayed.record.source_group_id, replayed.record.source_hash)
        previous = seen.get(key)
        if previous is not None:
            if previous != identity:
                raise ValueError("repeated source object has conflicting identity or hash")
            continue
        seen[key] = identity
        parsed = store.parser.parse_object(replayed)
        if isinstance(parsed, SnapshotProvenance):
            if replayed.record.object_type is not SourceObjectType.COURSE_UNIT:
                raise ValueError("only a course object can be excluded from normalization")
            excluded_null_code_course_versions.append(parsed)
            continue
        if replayed.record.object_type is SourceObjectType.COURSE_UNIT:
            if not isinstance(parsed, NormalizedCourseVersion):
                raise ValueError("course object did not parse into a normalized course version")
            memberships, dropped = _expanded_memberships(parsed.curriculum_period_ids, expansion)
            dropped_period_membership_count += dropped
            courses.append(parsed.model_copy(update={"curriculum_period_ids": memberships}))
            continue
        if replayed.record.object_type is SourceObjectType.MODULE:
            if not isinstance(parsed, NormalizedModuleVersion):
                raise ValueError("module object did not parse into a normalized module version")
            memberships, dropped = _expanded_memberships(parsed.curriculum_period_ids, expansion)
            dropped_period_membership_count += dropped
            modules.append(parsed.model_copy(update={"curriculum_period_ids": memberships}))
            continue
        raise ValueError("unsupported captured source object type")

    unresolved_courses = store.manifest.unresolved_course_group_ids
    unresolved_modules = store.manifest.unresolved_module_group_ids
    normalized_courses = tuple(courses)
    normalized_modules = tuple(modules)
    _validate_reference_closure(
        normalized_courses,
        normalized_modules,
        frozenset(item.source_group_id for item in excluded_null_code_course_versions),
        unresolved_courses,
        unresolved_modules,
    )
    return NormalizationBatch(
        run_id=store.manifest.run_id,
        periods=periods,
        covered_period_ids=_covered_period_ids(store.manifest.periods, expansion),
        courses=normalized_courses,
        excluded_null_code_course_versions=tuple(excluded_null_code_course_versions),
        modules=normalized_modules,
        unresolved_course_group_ids=unresolved_courses,
        unresolved_module_group_ids=unresolved_modules,
        dropped_period_membership_count=dropped_period_membership_count,
    )


def normalize_curriculum(
    store: RawCaptureStore,
    repository: NormalizationPersistence,
    *,
    current_academic_year_start: int,
    activity_window_years: int,
) -> NormalizationReport:
    """Parse the current run completely before its one normalized transaction."""
    batch = parse_capture(store)
    return repository.apply(
        batch,
        current_academic_year_start=current_academic_year_start,
        activity_window_years=activity_window_years,
    )
