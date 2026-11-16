"""Selector contracts for the translation-only worker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from course_explorer.enums import (
    OfficialTextField,
    TeachingTextField,
    TeachingTextOrigin,
    TranslationMethod,
)
from course_explorer.instance import UNIVERSITIES, UniversityMetadata
from tests.translation._service_support import (
    counts_by_cell,
    course,
    keys,
    row,
    select,
    version,
)


def _language_policy(
    official: tuple[str, ...], priority: dict[str, tuple[str, ...]]
) -> UniversityMetadata:
    """Build one three-language policy over the shared map entry for a selector proof."""
    return replace(
        UNIVERSITIES["hanken"],
        official_languages=official,
        display_fallback={"fi": official, "en": tuple(reversed(official))},
        translation_priority=priority,
        inference_languages=tuple((language, TeachingTextOrigin.OFFICIAL) for language in official),
    )


def _cells(**texts: dict[str, str]):
    """Return keyed official teaching cells for a name-only selector fixture."""
    return {OfficialTextField.NAME: texts}


def test_selector_partitions_every_representative_cell_and_orders_pending_work() -> None:
    """Every cell is classified once and pending work follows course, field, then language order."""
    courses = (
        course("course-a", version("course-a-v1", name_fi="Nimi")),
        course("course-b", version("course-b-v1", name_fi="Toinen")),
    )
    selection = select(courses)

    assert selection.canonical_course_count == 2
    assert selection.representative_course_count == 2
    assert selection.no_representative_course_count == 0
    assert selection.active_representative_count == 2
    assert selection.inactive_representative_count == 0
    for cell_counts in counts_by_cell(selection).values():
        assert sum(cell_counts) == 2
    assert keys(selection) == [
        ("course-a-v1", "name", "en"),
        ("course-b-v1", "name", "en"),
    ]
    counts = counts_by_cell(selection)
    assert counts[(TeachingTextField.NAME, "fi")] == (2, 0, 0, 0)
    assert counts[(TeachingTextField.NAME, "en")] == (0, 0, 2, 0)
    assert counts[(TeachingTextField.CONTENT, "fi")] == (0, 0, 0, 2)
    assert sum(count.pending_count for count in selection.fields) == len(selection.items)


def test_selector_includes_inactive_name_only_and_both_directions_but_not_cross_field_sources() -> (
    None
):
    """Inactive courses participate, listed Swedish fallback is ordered, and derived text is never
    a source."""
    policy = _language_policy(
        ("fi", "sv", "en"),
        {"fi": ("sv", "en"), "en": ("fi", "sv")},
    )
    swedish = version("course-sv-v1").model_copy(
        update={"texts": {OfficialTextField.NAME: {"sv": "Svenska"}}}
    )
    english = version("course-en-v1").model_copy(
        update={"texts": {OfficialTextField.NAME: {"en": "English"}}}
    )
    rows = (
        row(
            "course-en-v1",
            TeachingTextField.NAME,
            "fi",
            "sv",
            "Derived source",
            "Johdettu",
        ),
    )
    selection = select(
        (course("course-sv", swedish, is_active=False), course("course-en", english)),
        rows,
        university=policy,
    )

    assert selection.active_representative_count == 1
    assert selection.inactive_representative_count == 1
    # Swedish is unmapped, so it never becomes a target. Both mapped targets use the first
    # populated listed source, and a stored derived Swedish row is never selected as a source.
    assert keys(selection) == [
        ("course-sv-v1", "name", "fi"),
        ("course-sv-v1", "name", "en"),
        ("course-en-v1", "name", "fi"),
    ]
    by_key = {(item.source_version_id, item.language): item for item in selection.items}
    assert by_key[("course-sv-v1", "fi")].source_language == "sv"
    assert by_key[("course-sv-v1", "en")].source_language == "sv"
    assert by_key[("course-en-v1", "fi")].source_language == "en"
    counts = counts_by_cell(selection)
    assert counts[(TeachingTextField.NAME, "fi")] == (0, 0, 2, 0)
    assert counts[(TeachingTextField.NAME, "en")] == (1, 0, 1, 0)
    assert (TeachingTextField.NAME, "sv") not in counts
    # A name-only course never invents content/outcomes from its name.
    assert all(item.field is TeachingTextField.NAME for item in selection.items)


def test_selector_skips_current_derived_and_requeues_stale_or_mismatched_derived() -> None:
    """A later listed current manual row is reused. Unlisted pairs and wrong-source/model rows requeue."""
    policy = _language_policy(("fi", "sv", "en"), {"en": ("fi", "sv")})
    representative = version(
        "course-a-v1", name_fi="Nimi", content_fi="Sisältö", outcomes_fi="Osat"
    ).model_copy(
        update={
            "texts": {
                OfficialTextField.NAME: {"fi": "Nimi", "sv": "Svenska"},
                OfficialTextField.CONTENT: {"fi": "Sisältö"},
                OfficialTextField.OUTCOMES: {"fi": "Osat"},
            }
        }
    )
    rows = (
        # A manual row from the later listed current source is reused despite the earlier source.
        row(
            "course-a-v1",
            TeachingTextField.NAME,
            "en",
            "sv",
            "Svenska",
            "Name",
            method=TranslationMethod.MANUAL,
            translation_model=None,
        ),
        # A model row from the later listed source is not the first-source machine item.
        row(
            "course-a-v1",
            TeachingTextField.CONTENT,
            "en",
            "sv",
            "Sisältö",
            "Content",
        ),
        # A manual row from an unlisted source is not current, so the machine item requeues.
        row(
            "course-a-v1",
            TeachingTextField.OUTCOMES,
            "en",
            "de",
            "Osat",
            "Outcomes",
            method=TranslationMethod.MANUAL,
            translation_model=None,
        ),
    )
    selection = select((course("course-a", representative),), rows, university=policy)

    counts = counts_by_cell(selection)
    assert counts[(TeachingTextField.NAME, "en")] == (0, 1, 0, 0)
    assert counts[(TeachingTextField.CONTENT, "en")] == (0, 0, 1, 0)
    assert counts[(TeachingTextField.OUTCOMES, "en")] == (0, 0, 1, 0)
    assert keys(selection) == [
        ("course-a-v1", "content", "en"),
        ("course-a-v1", "outcomes", "en"),
    ]
    assert all(item.source_language == "fi" for item in selection.items)


def test_selector_applies_positive_course_limit_before_representative_filtering() -> None:
    """The limit narrows stable canonical order first, even when the first course has no representative."""
    courses = (
        course("course-a", None),
        course("course-b", version("course-b-v1", name_fi="Nimi")),
    )
    selection = select(courses, course_limit=1)

    assert selection.canonical_course_count == 1
    assert selection.representative_course_count == 0
    assert selection.no_representative_course_count == 1
    assert selection.items == ()

    # A nonpositive public limit is refused before any read or provider work.
    with pytest.raises(ValueError):
        select(courses, course_limit=0)
    with pytest.raises(ValueError):
        select(courses, course_limit=-1)
