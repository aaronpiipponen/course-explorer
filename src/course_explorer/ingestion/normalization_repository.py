"""Transactional persistence for normalized official curriculum DTOs.

The repository is the sole writer for normalized curriculum tables. It applies one complete
normalization batch through one transaction, retaining immutable capture provenance and replacing
only exact versions whose source hash changed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterator, Mapping, Sequence
from itertools import batched
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from course_explorer.db.models import (
    Course,
    CourseVersion,
    CourseVersionCurriculumPeriod,
    CurriculumPeriod,
    IngestionRun,
    Module,
    ModuleRuleNode,
    ModuleVersion,
    ModuleVersionCurriculumPeriod,
    OfficialCourseText,
    OfficialModuleText,
    OfficialPrerequisiteOption,
    OfficialPrerequisiteOptionMember,
    OfficialSubstitutionOption,
    OfficialSubstitutionOptionMember,
    SourceSnapshot,
)
from course_explorer.db.session import session_scope
from course_explorer.enums import (
    IngestionRunStatus,
    ModuleRuleType,
    OfficialTextField,
    SourceObjectType,
)
from course_explorer.ingestion.normalization import (
    NormalizationBatch,
    NormalizationPersistence,
    NormalizationReport,
    NormalizedCourseVersion,
    NormalizedModuleRule,
    NormalizedModuleVersion,
    NormalizedPeriod,
    SnapshotProvenance,
    walk_normalized_rule,
)
from course_explorer.instance import UniversityMetadata

# One PostgreSQL statement carries at most 65535 bind parameters, and a supported university's
# version set exceeds it, so one id query runs in fixed-size chunks.
_ID_QUERY_CHUNK_SIZE = 10_000


def _id_chunks(ids: Collection[str]) -> Iterator[tuple[str, ...]]:
    """Yield an id collection in statement-sized chunks."""
    return batched(tuple(ids), _ID_QUERY_CHUNK_SIZE)


class NormalizationRepository(NormalizationPersistence):
    """Apply normalized source DTOs atomically and report exact rerun classifications."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        university: UniversityMetadata,
    ):
        self._factory = factory
        self._university = university

    def apply(
        self,
        batch: NormalizationBatch,
        *,
        current_academic_year_start: int,
        activity_window_years: int,
    ) -> NormalizationReport:
        """Validate provenance, reconcile versions, and commit one all-or-nothing batch."""
        if current_academic_year_start <= 0:
            raise ValueError("current academic year start must be positive")
        if activity_window_years <= 0:
            raise ValueError("activity window years must be positive")

        course_groups = {course.group_id for course in batch.courses}
        excluded_course_source_ids = {
            exclusion.source_object_id for exclusion in batch.excluded_null_code_course_versions
        }
        excluded_course_groups = {
            exclusion.source_group_id for exclusion in batch.excluded_null_code_course_versions
        }
        affected_course_groups = course_groups | excluded_course_groups
        module_groups = {module.group_id for module in batch.modules}
        course_source_ids = {course.provenance.source_object_id for course in batch.courses}
        course_source_ids |= excluded_course_source_ids
        module_source_ids = {module.provenance.source_object_id for module in batch.modules}
        input_provenance = {
            (
                course.provenance.response_path,
                course.provenance.response_item_index,
            ): course.provenance
            for course in batch.courses
        }
        input_provenance.update(
            {
                (
                    module.provenance.response_path,
                    module.provenance.response_item_index,
                ): module.provenance
                for module in batch.modules
            }
        )
        input_provenance.update(
            {
                (
                    exclusion.response_path,
                    exclusion.response_item_index,
                ): exclusion
                for exclusion in batch.excluded_null_code_course_versions
            }
        )

        with session_scope(self._factory) as session:
            run = session.get(IngestionRun, batch.run_id)
            if run is None or run.status is not IngestionRunStatus.COMPLETED:
                raise ValueError("normalization requires a completed ingestion run")

            snapshots = self._load_snapshots(session, batch.run_id, input_provenance)
            periods = self._load_periods(session, {period.id for period in batch.periods})
            courses = self._load_courses(session, affected_course_groups)
            modules = self._load_modules(session, module_groups)
            existing_course_versions = self._load_course_versions(session, course_source_ids)
            existing_module_versions = self._load_module_versions(session, module_source_ids)

            self._prepare_periods(session, batch.periods, periods)
            self._ensure_owners(
                session,
                courses=courses,
                modules=modules,
                course_groups=course_groups,
                module_groups=module_groups,
                current_academic_year_start=current_academic_year_start,
                activity_window_years=activity_window_years,
            )
            session.flush()

            course_statuses, course_versions = self._apply_versions(
                session,
                object_type=SourceObjectType.COURSE_UNIT,
                versions=batch.courses,
                owners=courses,
                existing=existing_course_versions,
                snapshots=snapshots,
            )
            module_statuses, module_versions = self._apply_versions(
                session,
                object_type=SourceObjectType.MODULE,
                versions=batch.modules,
                owners=modules,
                existing=existing_module_versions,
                snapshots=snapshots,
            )
            session.flush()

            self._remove_excluded_null_code_versions(
                session, batch, existing_course_versions, courses
            )
            self._reconcile_period_memberships(
                session,
                object_type=SourceObjectType.COURSE_UNIT,
                versions=batch.courses,
                rows_by_source_id=course_versions,
                covered_period_ids=batch.covered_period_ids,
            )
            self._reconcile_period_memberships(
                session,
                object_type=SourceObjectType.MODULE,
                versions=batch.modules,
                rows_by_source_id=module_versions,
                covered_period_ids=batch.covered_period_ids,
            )
            session.flush()

            stored_courses = self._recalculate_courses(
                session,
                current_academic_year_start=current_academic_year_start,
                activity_window_years=activity_window_years,
            )
            self._prune_unreferenced_provenance(session)

            unrestricted_rule_count = sum(
                1
                for module in batch.modules
                if module.rule is not None
                for node in walk_normalized_rule(module.rule)
                if node.node_type
                in {ModuleRuleType.ANY_COURSE_UNIT_RULE, ModuleRuleType.ANY_MODULE_RULE}
            )
            return NormalizationReport(
                run_id=batch.run_id,
                period_count=len(batch.periods),
                canonical_course_count=len(course_groups),
                active_course_count=sum(
                    1
                    for course in stored_courses
                    if course.group_id in course_groups and course.is_active
                ),
                canonical_module_count=len(module_groups),
                course_version_count=len(batch.courses),
                excluded_null_code_course_versions=batch.excluded_null_code_course_versions,
                new_course_version_count=course_statuses["new"],
                changed_course_version_count=course_statuses["changed"],
                unchanged_course_version_count=course_statuses["unchanged"],
                module_version_count=len(batch.modules),
                new_module_version_count=module_statuses["new"],
                changed_module_version_count=module_statuses["changed"],
                unchanged_module_version_count=module_statuses["unchanged"],
                unresolved_course_group_count=len(batch.unresolved_course_group_ids),
                unresolved_module_group_count=len(batch.unresolved_module_group_ids),
                unrestricted_rule_count=unrestricted_rule_count,
                dropped_period_membership_count=batch.dropped_period_membership_count,
            )

    def refresh_course_activity(
        self, *, current_academic_year_start: int, activity_window_years: int
    ) -> None:
        """Recalculate every stored canonical course's activity context in one transaction.

        This is the shared activity owner for a current-year or window change. It creates no
        course, membership or result and changes no pointer or receipt.
        """
        if current_academic_year_start <= 0:
            raise ValueError("current academic year start must be positive")
        if activity_window_years <= 0:
            raise ValueError("activity window years must be positive")
        with session_scope(self._factory) as session:
            self._recalculate_courses(
                session,
                current_academic_year_start=current_academic_year_start,
                activity_window_years=activity_window_years,
            )

    @staticmethod
    def _prepare_periods(
        session: Session,
        period_values: Sequence[NormalizedPeriod],
        periods: dict[str, CurriculumPeriod],
    ) -> None:
        """Create or update the canonical curriculum-period rows for one batch."""
        for period in period_values:
            current = periods.get(period.id)
            if current is None:
                current = CurriculumPeriod(
                    id=period.id, academic_year_start=period.academic_year_start
                )
                session.add(current)
                periods[period.id] = current
            else:
                current.academic_year_start = period.academic_year_start

    @staticmethod
    def _ensure_owners(
        session: Session,
        *,
        courses: dict[str, Course],
        modules: dict[str, Module],
        course_groups: set[str],
        module_groups: set[str],
        current_academic_year_start: int,
        activity_window_years: int,
    ) -> None:
        """Create the missing canonical course and module containers for one batch."""
        for group_id in course_groups:
            if group_id in courses:
                continue
            courses[group_id] = Course(
                id=uuid4(),
                group_id=group_id,
                first_listed_academic_year_start=None,
                last_listed_academic_year_start=None,
                is_active=False,
                activity_as_of_academic_year_start=current_academic_year_start,
                activity_window_years=activity_window_years,
            )
            session.add(courses[group_id])
        for group_id in module_groups:
            if group_id in modules:
                continue
            modules[group_id] = Module(id=uuid4(), group_id=group_id)
            session.add(modules[group_id])

    @staticmethod
    def _remove_excluded_null_code_versions(
        session: Session,
        batch: NormalizationBatch,
        existing_course_versions: dict[str, tuple[CourseVersion, Course, SourceSnapshot]],
        courses: dict[str, Course],
    ) -> None:
        """Remove null-code exact versions and any canonical course left with no versions."""
        excluded_course_groups = {
            exclusion.source_group_id for exclusion in batch.excluded_null_code_course_versions
        }
        for exclusion in batch.excluded_null_code_course_versions:
            current = existing_course_versions.get(exclusion.source_object_id)
            if current is None:
                continue
            version, current_course, _ = current
            if current_course.group_id != exclusion.source_group_id:
                raise ValueError("exact source version is linked to a different canonical group")
            session.delete(version)
        session.flush()

        remaining_groups = set(
            session.scalars(
                select(Course.group_id)
                .join(CourseVersion, CourseVersion.course_id == Course.id)
                .where(Course.group_id.in_(excluded_course_groups))
            )
        )
        for group_id in excluded_course_groups - remaining_groups:
            course = courses.get(group_id)
            if course is not None:
                session.delete(course)
        session.flush()

    @staticmethod
    def _load_snapshots(
        session: Session,
        run_id: UUID,
        provenance: dict[tuple[str, int], SnapshotProvenance],
    ) -> dict[tuple[str, int], SourceSnapshot]:
        if not provenance:
            return {}
        indexed: dict[tuple[str, int], SourceSnapshot] = {}
        response_paths = tuple(dict.fromkeys(path for path, _ in provenance))
        for paths in _id_chunks(response_paths):
            rows = tuple(
                session.scalars(
                    select(SourceSnapshot).where(
                        SourceSnapshot.ingestion_run_id == run_id,
                        SourceSnapshot.response_path.in_(paths),
                    )
                )
            )
            indexed.update({(row.response_path, row.response_item_index): row for row in rows})
        if not set(provenance).issubset(indexed):
            raise ValueError("normalization provenance snapshot is missing")
        for key, expected in provenance.items():
            actual = indexed[key]
            if (
                actual.ingestion_run_id != run_id
                or actual.object_type is not expected.object_type
                or actual.source_object_id != expected.source_object_id
                or actual.source_group_id != expected.source_group_id
                or actual.source_hash != expected.source_hash
            ):
                raise ValueError("normalization provenance snapshot conflicts with source evidence")
        return indexed

    @staticmethod
    def _load_periods(session: Session, ids: set[str]) -> dict[str, CurriculumPeriod]:
        if not ids:
            return {}
        return {
            period.id: period
            for chunk in _id_chunks(ids)
            for period in session.scalars(
                select(CurriculumPeriod).where(CurriculumPeriod.id.in_(chunk))
            )
        }

    @staticmethod
    def _load_courses(session: Session, groups: set[str]) -> dict[str, Course]:
        if not groups:
            return {}
        return {
            course.group_id: course
            for chunk in _id_chunks(groups)
            for course in session.scalars(select(Course).where(Course.group_id.in_(chunk)))
        }

    @staticmethod
    def _load_modules(session: Session, groups: set[str]) -> dict[str, Module]:
        if not groups:
            return {}
        return {
            module.group_id: module
            for chunk in _id_chunks(groups)
            for module in session.scalars(select(Module).where(Module.group_id.in_(chunk)))
        }

    @staticmethod
    def _load_course_versions(
        session: Session, source_ids: set[str]
    ) -> dict[str, tuple[CourseVersion, Course, SourceSnapshot]]:
        if not source_ids:
            return {}
        rows = (
            row
            for chunk in _id_chunks(source_ids)
            for row in session.execute(
                select(CourseVersion, Course, SourceSnapshot)
                .join(Course, Course.id == CourseVersion.course_id)
                .join(SourceSnapshot, SourceSnapshot.id == CourseVersion.source_snapshot_id)
                .where(CourseVersion.source_version_id.in_(chunk))
            )
        )
        return {
            version.source_version_id: (version, course, snapshot)
            for version, course, snapshot in rows
        }

    @staticmethod
    def _load_module_versions(
        session: Session, source_ids: set[str]
    ) -> dict[str, tuple[ModuleVersion, Module, SourceSnapshot]]:
        if not source_ids:
            return {}
        rows = (
            row
            for chunk in _id_chunks(source_ids)
            for row in session.execute(
                select(ModuleVersion, Module, SourceSnapshot)
                .join(Module, Module.id == ModuleVersion.module_id)
                .join(SourceSnapshot, SourceSnapshot.id == ModuleVersion.source_snapshot_id)
                .where(ModuleVersion.source_version_id.in_(chunk))
            )
        )
        return {
            version.source_version_id: (version, module, snapshot)
            for version, module, snapshot in rows
        }

    @staticmethod
    def _version_values(
        version: NormalizedCourseVersion | NormalizedModuleVersion,
        *,
        owner_id: UUID,
        snapshot_id: UUID,
        object_type: SourceObjectType,
    ) -> dict[str, Any]:
        """Map one normalized version's shared fields plus its type-specific owner columns."""
        values: dict[str, Any] = {
            "code": version.code,
            "document_state": version.document_state,
            "validity_start": version.validity_start,
            "validity_end": version.validity_end,
            "source_revision": version.source_revision,
            "source_modification_ordinal": version.source_modification_ordinal,
            "source_snapshot_id": snapshot_id,
        }
        if object_type is SourceObjectType.COURSE_UNIT:
            values.update(
                {
                    "course_id": owner_id,
                    "source_version_id": version.provenance.source_object_id,
                    "credits_min": version.credits_min,
                    "credits_max": version.credits_max,
                    "official_url": version.official_url,
                }
            )
        else:
            values.update(
                {
                    "module_id": owner_id,
                    "source_version_id": version.provenance.source_object_id,
                    "module_type": version.module_type,
                    "target_credits_min": version.target_credits_min,
                    "target_credits_max": version.target_credits_max,
                }
            )
        return values

    def _apply_versions(
        self,
        session: Session,
        *,
        object_type: SourceObjectType,
        versions: tuple[NormalizedCourseVersion, ...] | tuple[NormalizedModuleVersion, ...],
        owners: dict[str, Any],
        existing: dict[str, tuple[Any, Any, SourceSnapshot]],
        snapshots: dict[tuple[str, int], SourceSnapshot],
    ) -> tuple[dict[str, int], dict[str, UUID]]:
        """Reconcile one object type's versions: reuse, replace in place, or create rows."""
        row_type = CourseVersion if object_type is SourceObjectType.COURSE_UNIT else ModuleVersion
        counts = {"new": 0, "changed": 0, "unchanged": 0}
        payloads: list[tuple[Any, Any]] = []
        rows_by_source_id: dict[str, UUID] = {}
        for normalized in versions:
            source_id = normalized.provenance.source_object_id
            snapshot = snapshots[_provenance_key(normalized.provenance)]
            current = existing.get(source_id)
            if current is not None:
                version, current_owner, current_snapshot = current
                if current_owner.group_id != normalized.group_id:
                    raise ValueError(
                        "exact source version is linked to a different canonical group"
                    )
                rows_by_source_id[source_id] = version.id
                if current_snapshot.source_hash == normalized.provenance.source_hash:
                    counts["unchanged"] += 1
                    continue
                counts["changed"] += 1
                self._delete_version_payload(session, object_type, version.id)
                for field, value in self._version_values(
                    normalized,
                    owner_id=current_owner.id,
                    snapshot_id=snapshot.id,
                    object_type=object_type,
                ).items():
                    setattr(version, field, value)
            else:
                counts["new"] += 1
                version = row_type(
                    id=uuid4(),
                    **self._version_values(
                        normalized,
                        owner_id=owners[normalized.group_id].id,
                        snapshot_id=snapshot.id,
                        object_type=object_type,
                    ),
                )
                session.add(version)
                rows_by_source_id[source_id] = version.id
            payloads.append((version, normalized))
        session.flush()
        for version, normalized in payloads:
            self._insert_version_payload(session, object_type, version, normalized)
        return counts, rows_by_source_id

    @staticmethod
    def _delete_version_payload(
        session: Session, object_type: SourceObjectType, version_id: UUID
    ) -> None:
        """Delete the payload shapes for one exact version being rewritten or removed."""
        if object_type is SourceObjectType.COURSE_UNIT:
            prerequisite_ids = select(OfficialPrerequisiteOption.id).where(
                OfficialPrerequisiteOption.target_course_version_id == version_id
            )
            substitution_ids = select(OfficialSubstitutionOption.id).where(
                OfficialSubstitutionOption.source_course_version_id == version_id
            )
            session.execute(
                delete(OfficialPrerequisiteOptionMember).where(
                    OfficialPrerequisiteOptionMember.prerequisite_option_id.in_(prerequisite_ids)
                )
            )
            session.execute(
                delete(OfficialPrerequisiteOption).where(
                    OfficialPrerequisiteOption.target_course_version_id == version_id
                )
            )
            session.execute(
                delete(OfficialSubstitutionOptionMember).where(
                    OfficialSubstitutionOptionMember.substitution_option_id.in_(substitution_ids)
                )
            )
            session.execute(
                delete(OfficialSubstitutionOption).where(
                    OfficialSubstitutionOption.source_course_version_id == version_id
                )
            )
            session.execute(
                delete(OfficialCourseText).where(OfficialCourseText.course_version_id == version_id)
            )
        else:
            session.execute(
                delete(ModuleRuleNode).where(ModuleRuleNode.module_version_id == version_id)
            )
            session.execute(
                delete(OfficialModuleText).where(OfficialModuleText.module_version_id == version_id)
            )

    def _written_cells(self, values: Mapping[str, str | None]) -> dict[str, str]:
        """Keep configured-language values, their exact bytes and their first occurrence only."""
        kept: dict[str, str] = {}
        seen: list[str] = []
        for language in self._university.official_languages:
            value = values.get(language)
            if value is None or not value.strip() or value in seen:
                continue
            seen.append(value)
            kept[language] = value
        return kept

    def _insert_official_texts(
        self,
        session: Session,
        object_type: SourceObjectType,
        version_id: UUID,
        normalized: NormalizedCourseVersion | NormalizedModuleVersion,
    ) -> None:
        """Write the filtered official cells of one added or source-changed exact version."""
        if object_type is SourceObjectType.MODULE:
            for language, text in self._written_cells(normalized.names).items():
                session.add(
                    OfficialModuleText(
                        module_version_id=version_id,
                        field="name",
                        language=language,
                        text=text,
                    )
                )
            return
        for field in OfficialTextField:
            for language, text in self._written_cells(normalized.texts.get(field, {})).items():
                session.add(
                    OfficialCourseText(
                        course_version_id=version_id,
                        field=field,
                        language=language,
                        text=text,
                    )
                )

    @staticmethod
    def _reconcile_period_memberships(
        session: Session,
        *,
        object_type: SourceObjectType,
        versions: tuple[NormalizedCourseVersion, ...] | tuple[NormalizedModuleVersion, ...],
        rows_by_source_id: Mapping[str, UUID],
        covered_period_ids: tuple[str, ...],
    ) -> None:
        """Make covered period listings equal this run's membership set.

        A covered period's stored membership rows become exactly the incoming normalized set,
        including an empty set. Memberships outside the covered periods are historical and stay.
        Incoming memberships outside coverage are added when absent, so a selected period never
        removes a valid stored membership.
        """
        membership_type = (
            CourseVersionCurriculumPeriod
            if object_type is SourceObjectType.COURSE_UNIT
            else ModuleVersionCurriculumPeriod
        )
        version_column = (
            CourseVersionCurriculumPeriod.course_version_id
            if object_type is SourceObjectType.COURSE_UNIT
            else ModuleVersionCurriculumPeriod.module_version_id
        )
        period_column = (
            CourseVersionCurriculumPeriod.curriculum_period_id
            if object_type is SourceObjectType.COURSE_UNIT
            else ModuleVersionCurriculumPeriod.curriculum_period_id
        )
        desired: set[tuple[UUID, str]] = {
            (rows_by_source_id[version.provenance.source_object_id], period_id)
            for version in versions
            for period_id in version.curriculum_period_ids
        }
        covered = set(covered_period_ids)
        stored: set[tuple[UUID, str]] = set()
        if covered:
            stored.update(
                session.execute(
                    select(version_column, period_column).where(
                        period_column.in_(covered_period_ids)
                    )
                ).all()
            )
        for chunk in batched(tuple(rows_by_source_id.values()), _ID_QUERY_CHUNK_SIZE):
            stored.update(
                session.execute(
                    select(version_column, period_column).where(version_column.in_(chunk))
                ).all()
            )
        deletions: dict[str, list[UUID]] = defaultdict(list)
        for version_id, period_id in stored:
            if period_id in covered and (version_id, period_id) not in desired:
                deletions[period_id].append(version_id)
        for period_id, version_ids in deletions.items():
            for chunk in batched(tuple(version_ids), _ID_QUERY_CHUNK_SIZE):
                session.execute(
                    delete(membership_type).where(
                        period_column == period_id,
                        version_column.in_(chunk),
                    )
                )
        missing = sorted(desired - stored)
        for chunk in batched(missing, _ID_QUERY_CHUNK_SIZE):
            session.add_all(
                [
                    membership_type(
                        **{version_column.key: version_id, period_column.key: period_id}
                    )
                    for version_id, period_id in chunk
                ]
            )
        session.flush()

    def _insert_version_payload(
        self,
        session: Session,
        object_type: SourceObjectType,
        version: Any,
        normalized: NormalizedCourseVersion | NormalizedModuleVersion,
    ) -> None:
        """Insert the necessary payload shapes for one added or changed exact version."""
        if object_type is SourceObjectType.MODULE:
            if normalized.rule is not None:
                _insert_rule_tree(session, version.id, normalized.rule, None)
            self._insert_official_texts(session, object_type, version.id, normalized)
            return
        self._insert_official_texts(session, object_type, version.id, normalized)
        for option in normalized.prerequisite_options:
            option_row = OfficialPrerequisiteOption(
                id=uuid4(),
                target_course_version_id=version.id,
                option_index=option.option_index,
                requirement_kind=option.requirement_kind,
            )
            session.add(option_row)
            for member in option.members:
                session.add(
                    OfficialPrerequisiteOptionMember(
                        id=uuid4(),
                        prerequisite_option_id=option_row.id,
                        member_index=member.member_index,
                        member_type=member.member_type,
                        referenced_group_id=member.referenced_group_id,
                    )
                )
        for option in normalized.substitution_options:
            option_row = OfficialSubstitutionOption(
                id=uuid4(), source_course_version_id=version.id, option_index=option.option_index
            )
            session.add(option_row)
            for member in option.members:
                session.add(
                    OfficialSubstitutionOptionMember(
                        id=uuid4(),
                        substitution_option_id=option_row.id,
                        member_index=member.member_index,
                        course_group_id=member.course_group_id,
                        credits=member.credits,
                    )
                )

    @staticmethod
    def _recalculate_courses(
        session: Session,
        *,
        current_academic_year_start: int,
        activity_window_years: int,
    ) -> tuple[Course, ...]:
        """Recalculate every stored course's activity context from its stored memberships.

        One grouped read supplies each course's listing bounds and whether any membership lies in the
        activity interval. The stored false-to-true transitions reset the inference processing marker
        through its owning repository. A course with no marker is unaffected.
        """
        courses = tuple(session.scalars(select(Course)))
        before_active = {course.group_id: course.is_active for course in courses}
        first_window_year = current_academic_year_start - activity_window_years + 1
        listings = {
            group_id: (first_listed, last_listed, in_window)
            for group_id, first_listed, last_listed, in_window in session.execute(
                select(
                    Course.group_id,
                    func.min(CurriculumPeriod.academic_year_start),
                    func.max(CurriculumPeriod.academic_year_start),
                    func.coalesce(
                        func.bool_or(
                            CurriculumPeriod.academic_year_start.between(
                                first_window_year, current_academic_year_start
                            )
                        ),
                        False,
                    ),
                )
                .select_from(Course)
                .outerjoin(CourseVersion, CourseVersion.course_id == Course.id)
                .outerjoin(
                    CourseVersionCurriculumPeriod,
                    CourseVersionCurriculumPeriod.course_version_id == CourseVersion.id,
                )
                .outerjoin(
                    CurriculumPeriod,
                    CurriculumPeriod.id == CourseVersionCurriculumPeriod.curriculum_period_id,
                )
                .group_by(Course.group_id)
            )
        }
        reactivated: list[UUID] = []
        for course in courses:
            first_listed, last_listed, in_window = listings[course.group_id]
            course.first_listed_academic_year_start = first_listed
            course.last_listed_academic_year_start = last_listed
            course.is_active = in_window
            course.activity_as_of_academic_year_start = current_academic_year_start
            course.activity_window_years = activity_window_years
            if not before_active[course.group_id] and course.is_active:
                reactivated.append(course.id)
        if reactivated:
            from course_explorer.inference.scheduling_repository import (
                InferenceSchedulingRepository,
            )

            InferenceSchedulingRepository.reset_reactivated_processing(session, reactivated)
        return courses

    @staticmethod
    def _prune_unreferenced_provenance(session: Session) -> None:
        """Delete completed-run snapshots no stored version references, then their empty runs."""
        snapshot_referenced = or_(
            exists(
                select(CourseVersion.id).where(
                    CourseVersion.source_snapshot_id == SourceSnapshot.id
                )
            ),
            exists(
                select(ModuleVersion.id).where(
                    ModuleVersion.source_snapshot_id == SourceSnapshot.id
                )
            ),
        )
        completed_runs = select(IngestionRun.id).where(
            IngestionRun.status == IngestionRunStatus.COMPLETED
        )
        session.execute(
            delete(SourceSnapshot).where(
                SourceSnapshot.ingestion_run_id.in_(completed_runs),
                ~snapshot_referenced,
            )
        )
        session.execute(
            delete(IngestionRun).where(
                IngestionRun.status == IngestionRunStatus.COMPLETED,
                ~exists(
                    select(SourceSnapshot.id).where(
                        SourceSnapshot.ingestion_run_id == IngestionRun.id
                    )
                ),
            )
        )


def _provenance_key(provenance: SnapshotProvenance) -> tuple[str, int]:
    return provenance.response_path, provenance.response_item_index


def _insert_rule_tree(
    session: Session,
    module_version_id: UUID,
    rule: NormalizedModuleRule,
    parent_node_id: UUID | None,
) -> None:
    node_id = uuid4()
    session.add(
        ModuleRuleNode(
            id=node_id,
            module_version_id=module_version_id,
            parent_node_id=parent_node_id,
            sibling_position=rule.sibling_position,
            local_id=rule.local_id,
            node_type=rule.node_type,
            require_min=rule.require_min,
            require_max=rule.require_max,
            all_mandatory=rule.all_mandatory,
            description=rule.description,
            credits_min=rule.credits_min,
            credits_max=rule.credits_max,
            count_min=rule.count_min,
            count_max=rule.count_max,
            referenced_group_id=rule.referenced_group_id,
        )
    )
    session.flush()
    for child in rule.children:
        _insert_rule_tree(session, module_version_id, child, node_id)
