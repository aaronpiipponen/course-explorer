"""Opt-in bounded live contract test for one public Kori curriculum period."""

import json
import os

import pytest

from course_explorer.config import KoriSettings
from course_explorer.ingestion.kori import KoriSource

pytestmark = pytest.mark.live


def test_public_kori_enumerates_one_period_with_exact_batches_and_group_versions() -> None:
    """Exercise complete one-period enumeration, exact batches, and group discovery."""
    if os.environ.get("COURSE_EXPLORER_RUN_LIVE_KORI_TESTS") != "1":
        pytest.skip("set COURSE_EXPLORER_RUN_LIVE_KORI_TESTS=1 to enable public Kori test")
    settings = KoriSettings()

    with KoriSource(settings) as source:
        # The test bounds itself to the highest year of the mapped academic-year interval.
        periods = source.get_curriculum_periods().value
        assert periods
        period_id = periods[-1].id

        course_responses = tuple(source.iter_course_version_refs([period_id]))
        degree_responses = tuple(source.iter_module_version_refs([period_id], "DegreeProgramme"))
        study_responses = tuple(source.iter_module_version_refs([period_id], "StudyModule"))
        families = {
            "courses": [ref.id for result in course_responses for ref in result.value],
            "degrees": [ref.id for result in degree_responses for ref in result.value],
            "study_modules": [ref.id for result in study_responses for ref in result.value],
        }
        for ids in families.values():
            assert ids

        fetched = {
            "courses": tuple(
                item
                for result in source.iter_course_versions(sorted(set(families["courses"]))[:2])
                for item in result.value
            ),
            "degrees": tuple(
                item
                for result in source.iter_module_versions(sorted(set(families["degrees"]))[:2])
                for item in result.value
            ),
            "study_modules": tuple(
                item
                for result in source.iter_module_versions(
                    sorted(set(families["study_modules"]))[:2]
                )
                for item in result.value
            ),
        }
        for family, objects in fetched.items():
            assert {item["id"] for item in objects} == set(sorted(set(families[family]))[:2])

        course_group = fetched["courses"][0]["groupId"]
        module_group = fetched["study_modules"][0]["groupId"]
        course_group_result = source.get_course_group_versions(course_group)
        module_group_result = source.get_module_group_versions(module_group)
        assert course_group_result.value
        assert module_group_result.value
        assert {item["groupId"] for item in course_group_result.value} == {course_group}
        assert {item["groupId"] for item in module_group_result.value} == {module_group}

    print(
        json.dumps(
            {
                "period_id": period_id,
                "enumerated": {family: len(ids) for family, ids in families.items()},
                "fetched": {family: len(objects) for family, objects in fetched.items()},
                "course_group_versions": len(course_group_result.value),
                "module_group_versions": len(module_group_result.value),
            },
            sort_keys=True,
        )
    )
