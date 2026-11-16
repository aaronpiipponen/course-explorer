"""Pure next-course discovery: direct-prerequisite coverage, eligibility and ordering.

This module owns the request-local arithmetic and ordering for next courses. It
consumes already-compiled native direct structures or their frozen summaries, the chosen basis and
the requested mode. Coverage is a course-share meter over each category's OR-of-AND options,
carried as a canonical course-slot set plus its implicit unresolved slot count. Full ALL-mode
eligibility is decided by the native evaluator with the actual numeric constraints. Official and
inferred contributions stay distinguishable through the relationship-local, official-wins
`depends_on_inferred` flag.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from course_explorer.curriculum import CourseSummary
from course_explorer.prerequisite.graph import (
    DirectPrerequisiteSummaryCompilation,
)
from course_explorer.search.lookup import DisplayLanguage, name_code_key, official_display_name


class NextCourseMode(StrEnum):
    """Which direct-prerequisite eligibility mode one discovery request uses."""

    ALL = "all"
    AT_LEAST_ONE = "at_least_one"


class _NextCourseModel(BaseModel):
    """Frozen, closed base for the next-course result contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NextCourseResult(_NextCourseModel):
    """One proposed next course with its direct-prerequisite coverage and inferred attribution."""

    course: CourseSummary
    required_coverage: float | None
    recommended_coverage: float | None
    depends_on_inferred: bool


@dataclass(frozen=True)
class NextCourseOutcomes:
    """One direct-compilation pass's root-keyed outcomes and its assumed-credit use flag.

    ``by_group`` maps each root group ID to its ``(required, recommended)`` coverage pair, or to
    ``None`` when the structure has no outcome. ``uses_assumed_credits`` is true when an outcome's
    credit-bound requirement retained a course whose minimum credit was assumed.
    """

    by_group: dict[str, tuple[float | None, float | None] | None]
    uses_assumed_credits: bool


def _ranked(fraction: float | None) -> float:
    """Return one coverage fraction for ordering, where a category without slots counts as zero."""
    return 0.0 if fraction is None else fraction


def evaluate_next_course_outcomes(
    compilations: Sequence[DirectPrerequisiteSummaryCompilation],
    basis_group_ids: Collection[str],
    *,
    mode: NextCourseMode,
    assumed_course_credits: Decimal,
) -> NextCourseOutcomes:
    """Return the coverage outcomes carrier of one direct-compilation pass.

    The carrier holds one root-keyed ``(required, recommended)`` pair per compilation and the
    assumed-credit flag, which is true when any outcome retained a course whose minimum credit was
    assumed.
    """
    if not compilations:
        return NextCourseOutcomes(by_group={}, uses_assumed_credits=False)
    bundle = compilations[0].native_bundle
    if bundle is None:
        raise ValueError("direct compilations were not produced by the native compiler")
    native_mode = 1 if mode is NextCourseMode.AT_LEAST_ONE else 0
    outcomes = bundle.evaluate(
        tuple(basis_group_ids),
        mode=native_mode,
        assumed_course_credits=assumed_course_credits,
    )
    by_root = {outcome.compilation.summary.root_group_id: outcome for outcome in outcomes}
    return NextCourseOutcomes(
        by_group={
            compilation.summary.root_group_id: (
                None
                if by_root[compilation.summary.root_group_id].has_outcome is False
                else (
                    by_root[compilation.summary.root_group_id].required_coverage,
                    by_root[compilation.summary.root_group_id].recommended_coverage,
                )
            )
            for compilation in compilations
        },
        uses_assumed_credits=any(outcome.uses_assumed_credits for outcome in outcomes),
    )


def eligible_next_course_group_ids(
    compilations: Sequence[DirectPrerequisiteSummaryCompilation],
    basis_group_ids: Collection[str],
    *,
    mode: NextCourseMode,
    assumed_course_credits: Decimal,
    candidate_outcomes: NextCourseOutcomes | None = None,
) -> tuple[str, ...]:
    """Return the sorted canonical IDs eligible in one direct-compilation pass."""
    outcomes = (
        candidate_outcomes
        if candidate_outcomes is not None
        else evaluate_next_course_outcomes(
            compilations,
            basis_group_ids,
            mode=mode,
            assumed_course_credits=assumed_course_credits,
        )
    )
    return tuple(
        sorted(
            compilation.summary.root_group_id
            for compilation in compilations
            if outcomes.by_group[compilation.summary.root_group_id] is not None
        )
    )


def next_courses_from_compilations(
    compilations: Sequence[DirectPrerequisiteSummaryCompilation],
    basis_group_ids: Collection[str],
    *,
    mode: NextCourseMode,
    display_language: DisplayLanguage,
    assumed_course_credits: Decimal,
    official_compilations: Sequence[DirectPrerequisiteSummaryCompilation] = (),
    official_outcomes: NextCourseOutcomes | None = None,
    candidate_outcomes: NextCourseOutcomes | None = None,
) -> tuple[NextCourseResult, ...]:
    """Return the ordered next-course list for one already-compiled candidate set."""
    outcomes = (
        candidate_outcomes
        if candidate_outcomes is not None
        else evaluate_next_course_outcomes(
            compilations,
            basis_group_ids,
            mode=mode,
            assumed_course_credits=assumed_course_credits,
        )
    )
    official_by_root = {
        compilation.summary.root_group_id: compilation for compilation in official_compilations
    }
    official = (
        official_outcomes
        if official_outcomes is not None
        else (
            evaluate_next_course_outcomes(
                official_compilations,
                basis_group_ids,
                mode=mode,
                assumed_course_credits=assumed_course_credits,
            )
            if official_compilations
            else NextCourseOutcomes(by_group={}, uses_assumed_credits=False)
        )
    )
    summary_by_group = {
        course.group_id: course
        for compilation in compilations
        for course in compilation.summary.courses
    }
    results: list[NextCourseResult] = []
    for compilation in compilations:
        structure = compilation.summary
        candidate_id = structure.root_group_id
        outcome = outcomes.by_group[candidate_id]
        if outcome is None:
            continue
        required_coverage, recommended_coverage = outcome
        official_compilation = official_by_root.get(candidate_id)
        depends_on_inferred = (
            official_compilation is not None and official.by_group[candidate_id] is None
        )
        summary = summary_by_group.get(candidate_id)
        if summary is None:
            raise ValueError(f"candidate {candidate_id} is missing from its structure courses")
        results.append(
            NextCourseResult(
                course=summary,
                required_coverage=required_coverage,
                recommended_coverage=recommended_coverage,
                depends_on_inferred=depends_on_inferred,
            )
        )
    return _sorted_results(tuple(results), mode=mode, display_language=display_language)


def _sorted_results(
    results: tuple[NextCourseResult, ...],
    *,
    mode: NextCourseMode,
    display_language: DisplayLanguage,
) -> tuple[NextCourseResult, ...]:
    """Order one result tuple by the mode's ordering key."""

    def name_key(result: NextCourseResult) -> tuple[bool, str, str, str]:
        return name_code_key(result.course, official_display_name(result.course, display_language))

    if mode is NextCourseMode.AT_LEAST_ONE:
        return tuple(
            sorted(
                results,
                key=lambda result: (
                    -_ranked(result.required_coverage),
                    -_ranked(result.recommended_coverage),
                    name_key(result),
                ),
            )
        )
    return tuple(sorted(results, key=name_key))
