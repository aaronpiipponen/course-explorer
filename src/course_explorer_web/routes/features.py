"""POST adapter for the site's served feature switches, dataset identity, display label,
university names, curriculum source attribution, assumed course credits and study-plan import
configuration.
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from course_explorer.example_data import EXAMPLE_SOURCE_ATTRIBUTION
from course_explorer_web.serving_cache import preloaded_serving_settings

router = APIRouter()


class FeaturesRequest(BaseModel):
    """The closed empty feature-report request."""

    model_config = ConfigDict(extra="forbid")


class WebFeaturesReport(BaseModel):
    """The served feature switches, the served dataset identity, the site's display label, the
    configured university's names, its curriculum source attribution, the configured
    assumed course credit value and the configured study-plan import script and formats.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    embeddings_enabled: bool
    inference_enabled: bool
    prerequisites_enabled: bool
    dataset_identity: str
    display_label: str
    university_names: dict[str, str]
    source_attribution: dict[str, str]
    assumed_course_credits: str
    study_plan_script: str | None
    study_plan_formats: tuple[str, ...]


@router.post("/api/features")
def features(request: FeaturesRequest) -> Response:
    """Return the configured feature switches and identity with the site's display label,
    university names, source attribution, assumed course credits and study-plan import
    configuration.
    """
    bundle = preloaded_serving_settings()
    university = bundle.web.university
    report = WebFeaturesReport(
        embeddings_enabled=bundle.web.embeddings_enabled,
        inference_enabled=bundle.web.inference_enabled,
        prerequisites_enabled=bundle.web.prerequisites_enabled,
        dataset_identity=bundle.dataset.identity,
        display_label=bundle.web.display_label,
        university_names={"en": university.name_en, "fi": university.name_fi},
        source_attribution=(
            EXAMPLE_SOURCE_ATTRIBUTION
            if bundle.dataset.example
            else {
                "en": university.source_attribution_en,
                "fi": university.source_attribution_fi,
            }
        ),
        assumed_course_credits=str(bundle.search.assumed_course_credits),
        study_plan_script=university.study_plan_script,
        study_plan_formats=university.study_plan_formats,
    )
    return Response(content=report.model_dump_json(), media_type="application/json")
