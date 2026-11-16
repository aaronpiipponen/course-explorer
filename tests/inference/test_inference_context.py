"""Contracts for label-free candidate context, fusion, and target packets."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from course_explorer.curriculum import (
    CatalogueCourse,
    CatalogueCourseVersion,
    CataloguePrerequisiteMember,
    CataloguePrerequisiteOption,
    CatalogueTeachingCourse,
)
from course_explorer.enums import (
    OfficialTextField,
    PrerequisiteMemberType,
    PrerequisiteRequirementKind,
    TeachingTextField,
    TeachingTextOrigin,
)
from course_explorer.hashing import canonical_json_sha256
from course_explorer.inference.context import (
    InferenceCandidateMatch,
    InferenceCourseEmbeddingIdentity,
    InferenceCourseReference,
    InferenceMatchedChunk,
    InferenceRankedCandidate,
    InferenceSelectionReferences,
    catalogue_identity_sha256,
    course_reference,
    fuse_candidate_views,
    processing_text_sha256,
    selection_references_sha256,
)
from course_explorer.inference.failures import TargetSelectionError
from course_explorer.inference.operational import (
    OperationalCatalogue,
    materialize_selection_packet,
    prepare_operational_catalogue,
)
from course_explorer.inference.operational_selection import (
    render_rerank_course_text,
    represented_identities,
)
from course_explorer.inference.prompts import load_prompt_pack
from course_explorer.inference.scheduling import processing_cycle_input_sha256
from course_explorer.inference.target_pass import render_target_request
from course_explorer.instance import (
    UNIVERSITIES,
    inference_language_policy_overrides,
    language_policy_overrides,
)
from course_explorer.teaching_text.service import (
    EffectiveTeachingTextCatalogue,
    TeachingTextService,
    teaching_text_sha256,
)
from tests.embeddings._repository_support import embedding_settings
from tests.support import (
    FakeCurriculum,
    FakeTranslations,
    course,
    derived_translation,
    settings_factory,
)
from tests.support import (
    version as catalogue_version,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def version(
    code: str,
    *,
    source_id: str | None = None,
    name_fi: str | None = None,
    name_en: str | None = None,
    content_fi: str | None = None,
    content_en: str | None = None,
    outcomes_fi: str | None = None,
    outcomes_en: str | None = None,
    prerequisite_options: tuple[CataloguePrerequisiteOption, ...] = (),
) -> CatalogueCourseVersion:
    """Build one detached official representative fixture with the canonical test period and URL."""
    return catalogue_version(
        source_id or f"source-{code}",
        code=code,
        name_fi=name_fi,
        name_en=name_en,
        content_fi=content_fi,
        content_en=content_en,
        outcomes_fi=outcomes_fi,
        outcomes_en=outcomes_en,
    ).model_copy(
        update={
            "official_url": f"https://example.test/{code}",
            "curriculum_period_ids": ("2026-2027",),
            "prerequisite_options": prerequisite_options,
        }
    )


def course_label_option(*group_ids: str) -> CataloguePrerequisiteOption:
    """Build one official option referencing course-typed prerequisite members."""
    return CataloguePrerequisiteOption(
        option_index=0,
        requirement_kind=PrerequisiteRequirementKind.COMPULSORY,
        members=tuple(
            CataloguePrerequisiteMember(
                member_index=index,
                member_type=PrerequisiteMemberType.COURSE,
                referenced_group_id=group_id,
            )
            for index, group_id in enumerate(group_ids)
        ),
    )


def effective_catalogue(courses: tuple[CatalogueCourse, ...]):
    """Resolve the effective teaching catalogue over fake seams."""
    return TeachingTextService(FakeCurriculum(courses), FakeTranslations()).build_catalogue()


def sample_courses() -> tuple[CatalogueCourse, ...]:
    """Return a coherent catalogue with active/inactive/unrepresented courses."""
    return (
        course("comp-a", version("COMP.A.100", name_fi="A", content_fi="Sisalto A")),
        course("comp-b", version("COMP.B.200", name_en="B", content_en="Content B")),
        course("math-c", version("MATH.C.300", name_fi="C", content_fi="Sisalto C")),
        course("data-d", version("DATA.D.400", name_fi="D", content_fi="Sisalto D")),
        course("other-e", version("OTHER.E.500", name_fi="E", content_fi="Sisalto E")),
        course("inactive-f", version("COMP.F.600", name_fi="F"), is_active=False),
        course("unrepresented-g", None),
    )


def match_for(
    group_id: str, score: float, *, source_id: str | None = None
) -> InferenceCandidateMatch:
    """Build one complete candidate match fixture for a synthetic identity."""
    return match_for_identity(
        InferenceCourseEmbeddingIdentity(
            group_id=group_id,
            source_version_id=source_id or f"source-{group_id}",
            representative_source_sha256=HASH_A,
        ),
        score,
    )


def match_for_identity(
    identity: InferenceCourseEmbeddingIdentity, score: float
) -> InferenceCandidateMatch:
    """Build one complete candidate match fixture for one exact represented identity."""
    chunk = InferenceMatchedChunk(
        field=TeachingTextField.CONTENT,
        language="fi",
        chunk_index=0,
        text_origin=TeachingTextOrigin.OFFICIAL,
        chunk_text_sha256=HASH_B,
    )
    return InferenceCandidateMatch(
        course=identity,
        score=score,
        target_chunk=chunk,
        candidate_chunk=chunk,
    )


def ranked(matches: tuple[InferenceCandidateMatch, ...]) -> tuple[InferenceRankedCandidate, ...]:
    """Wrap candidate matches in contiguous one-based ranks."""
    return tuple(
        InferenceRankedCandidate(rank=index, match=match)
        for index, match in enumerate(matches, start=1)
    )


def _operational_catalogue(
    courses: tuple[CatalogueCourse | CatalogueTeachingCourse, ...],
    effective: EffectiveTeachingTextCatalogue,
    courses_by_group: dict[str, CatalogueCourse | CatalogueTeachingCourse],
    *,
    prompt_target_fields: tuple[OfficialTextField, ...] | None = None,
    prompt_pool_fields: tuple[OfficialTextField, ...] | None = None,
    prompt_official_languages: tuple[str, ...] | None = None,
) -> OperationalCatalogue:
    """Build the live operational catalogue carrier over the fake seams."""
    university = UNIVERSITIES["example"]
    return OperationalCatalogue(
        courses_by_group={
            group_id: CatalogueTeachingCourse.model_validate(course, from_attributes=True)
            for group_id, course in courses_by_group.items()
        },
        effective_by_group={item.group_id: item for item in effective.courses},
        teaching_text_catalogue=effective,
        represented_courses=represented_identities(courses),
        catalogue_identity_sha256=catalogue_identity_sha256(
            courses, effective, embedding_configuration_hash=HASH_A
        ),
        embedding_configuration_sha256=HASH_A,
        prompt_target_fields=(
            university.inference_target_fields
            if prompt_target_fields is None
            else prompt_target_fields
        ),
        prompt_pool_fields=(
            university.inference_pool_fields if prompt_pool_fields is None else prompt_pool_fields
        ),
        prompt_official_languages=(
            tuple(
                language
                for language, origin in university.inference_languages
                if origin is TeachingTextOrigin.OFFICIAL
            )
            if prompt_official_languages is None
            else prompt_official_languages
        ),
    )


def test_packet_hash_binds_current_teaching_provenance_without_official_labels() -> None:
    """A materialized packet carries only effective teaching text/provenance. Compact refs carry no text."""
    courses = sample_courses()
    effective = effective_catalogue(courses)
    effective_by_group = {item.group_id: item for item in effective.courses}
    courses_by_group = {item.group_id: item for item in courses}

    # Compact references carry only identity and text-only digests, never teaching text.
    def reference(course: CatalogueCourse) -> InferenceCourseReference:
        representative = course.representative
        assert representative is not None
        effective_course = effective_by_group[course.group_id]
        return InferenceCourseReference(
            group_id=course.group_id,
            source_version_id=representative.source_version_id,
            representative_source_sha256=representative.source_hash,
            teaching_text_sha256=processing_text_sha256(
                tuple(
                    (cell.field.value, cell.language, cell.text) for cell in effective_course.texts
                )
            ),
        )

    references = InferenceSelectionReferences(
        target=reference(courses_by_group["comp-a"]),
        candidates=(reference(courses_by_group["comp-b"]),),
    )
    catalogue = _operational_catalogue(courses, effective, courses_by_group)
    packet = materialize_selection_packet(references, catalogue)
    assert packet.packet_version == "v1"
    assert packet.target.course_code == "COMP.A.100"
    assert packet.target.is_active is True
    assert packet.target.activity_as_of_academic_year_start == 2026
    assert packet.target.activity_window_years == 3
    assert packet.target.identity.representative_source_sha256 == HASH_A
    dumped = packet.model_dump_json()
    assert "prerequisite" not in dumped
    assert "official_label" not in dumped
    text = packet.target.texts[0]
    assert text.text == "A"
    assert text.origin is TeachingTextOrigin.OFFICIAL
    assert text.text_sha256 == text.source_text_sha256 == teaching_text_sha256("A")

    serialized = references.model_dump(mode="json")
    assert set(serialized["target"]) == {
        "group_id",
        "sisu_course_unit_id",
        "representative_source_sha256",
        "teaching_text_sha256",
    }
    assert "Sisalto" not in references.model_dump_json()
    assert selection_references_sha256(references) == canonical_json_sha256(serialized)

    # Official labels on the target representative never enter the packet.
    target = courses_by_group["comp-a"]
    assert target.representative is not None
    labeled_target = target.model_copy(
        update={
            "representative": target.representative.model_copy(
                update={"prerequisite_options": (course_label_option("data-d"),)}
            )
        }
    )
    labeled_catalogue = _operational_catalogue(
        courses, effective, {**courses_by_group, "comp-a": labeled_target}
    )
    assert materialize_selection_packet(references, labeled_catalogue) == packet

    # The teaching projection yields the identical packet and catalogue digest.
    projected = FakeCurriculum(courses).list_teaching_courses()
    projected_by_group = {item.group_id: item for item in projected}
    projected_catalogue = _operational_catalogue(projected, effective, projected_by_group)
    assert materialize_selection_packet(references, projected_catalogue) == packet
    assert catalogue_identity_sha256(
        projected, effective, embedding_configuration_hash=HASH_A
    ) == catalogue_identity_sha256(courses, effective, embedding_configuration_hash=HASH_A)


def fused_ranked(
    group_ids: tuple[str, ...], scores: tuple[float, ...]
) -> tuple[InferenceRankedCandidate, ...]:
    """Wrap explicit group IDs and scores in contiguous one-based ranks."""
    return ranked(
        tuple(match_for(group_id, score) for group_id, score in zip(group_ids, scores, strict=True))
    )


def test_multiview_fusion_is_label_blind_deterministic_and_preserves_cosine_provenance() -> None:
    """Reciprocal field ranks fuse deterministically. Cosine provenance is on the match."""
    universe = ("comp-b", "math-c", "data-d")
    all_field = fused_ranked(universe, (0.90, 0.80, 0.70))
    name_view = fused_ranked(("data-d", "comp-b", "math-c"), (0.95, 0.60, 0.50))
    content_view = fused_ranked(("comp-b", "data-d", "math-c"), (0.91, 0.85, 0.40))
    outcomes_view = fused_ranked(("math-c", "data-d", "comp-b"), (0.99, 0.55, 0.30))
    views = (
        (None, all_field),
        (TeachingTextField.NAME, name_view),
        (TeachingTextField.CONTENT, content_view),
        (TeachingTextField.OUTCOMES, outcomes_view),
    )
    fused = fuse_candidate_views(views, rank_offset=60)
    assert [entry.rank for entry in fused] == [1, 2, 3]
    by_group = {entry.match.course.group_id: entry for entry in fused}
    expected = {
        "comp-b": 1 / 61 + 1 / 62 + 1 / 61 + 1 / 63,
        "math-c": 1 / 62 + 1 / 63 + 1 / 63 + 1 / 61,
        "data-d": 1 / 63 + 1 / 61 + 1 / 62 + 1 / 62,
    }
    assert set(by_group) == set(expected)
    for group_id, score in expected.items():
        assert by_group[group_id].fusion_score == pytest.approx(score)
    assert [entry.match.course.group_id for entry in fused] == sorted(
        expected, key=lambda group_id: (-expected[group_id], group_id)
    )
    # view_ranks are canonical-ordered, unique, and carry the actual per-view source ranks.
    for entry in fused:
        keys = [key for key, _ in entry.view_ranks]
        assert keys == [None, "name", "content", "outcomes"]
        assert all(view_rank >= 1 for _, view_rank in entry.view_ranks)
        assert len(set(keys)) == len(keys)
    # The all-field canonical cosine and matched chunks are the source of truth.
    assert by_group["comp-b"].match == all_field[0].match
    assert by_group["comp-b"].match.score == pytest.approx(0.90)
    # Deterministic: identical inputs produce identical output.
    assert fuse_candidate_views(views, rank_offset=60) == fused
    # A single all-field view is valid and reduces to its own ordering.
    only_all = fuse_candidate_views(((None, all_field),), rank_offset=60)
    assert [entry.match.course.group_id for entry in only_all] == ["comp-b", "math-c", "data-d"]
    assert only_all[0].view_ranks == ((None, 1),)
    # An empty field view (no compatible target chunk in that field) contributes no synthetic rank.
    empty_field = fuse_candidate_views(
        ((None, all_field), (TeachingTextField.NAME, ())), rank_offset=60
    )
    assert [entry.match.course.group_id for entry in empty_field] == [
        "comp-b",
        "math-c",
        "data-d",
    ]
    assert all(len(entry.view_ranks) == 1 for entry in empty_field)
    # A field view over a different candidate universe fails loudly.
    with pytest.raises(TargetSelectionError) as universe_error:
        fuse_candidate_views(
            (
                (None, all_field),
                (TeachingTextField.NAME, fused_ranked(("comp-b", "math-c"), (0.9, 0.8))),
            ),
            rank_offset=60,
        )
    assert universe_error.value.reason == "incomplete_views"


@pytest.mark.integration
def test_split_selection_matches_combined_and_refusal_never_completes_partial_rerank(
    database,
) -> None:
    """The split multiview/rerank path byte-matches the combined selector and never yields partial input."""
    import json

    import httpx
    from sqlalchemy import select

    from course_explorer.config import (
        InferenceContextSettings,
        InferenceRerankInputSettings,
        InferenceRerankSettings,
        InferenceSelectionSettings,
        InferenceSettings,
    )
    from course_explorer.curriculum_repository import CurriculumRepository
    from course_explorer.db.models import CourseVersion, OfficialCourseText
    from course_explorer.db.session import session_scope
    from course_explorer.enums import OfficialTextField
    from course_explorer.http_retry import RequestNotStarted
    from course_explorer.inference.context_repository import InferenceCandidateRepository
    from course_explorer.inference.operational import (
        prepare_operational_catalogue,
        rerank_operational_pool,
        select_operational_multiview_pool,
    )
    from course_explorer.inference.provider import RerankProvider
    from course_explorer.teaching_text.repository import DerivedTranslationRepository
    from tests.inference._http_support import InferenceHttpBoundary
    from tests.inference._job_support import embedding_settings, seed_embeddings
    from tests.inference._scheduling_support import seed_courses
    from tests.support import settings_factory

    factory = database[1]
    count = 60
    seed_courses(
        factory,
        [(f"g{index:03d}", f"CODE.{index}", False) for index in range(count)]
        + [("target", "TARGET", True)],
    )
    # Current teaching text includes outcomes plus prerequisite/additional cells that the renderer
    # must never read. Set them before the embeddings are derived from this exact text.
    with session_scope(factory) as session:
        version_codes = {
            version.id: version.code for version in session.scalars(select(CourseVersion))
        }
        for version_id, code in version_codes.items():
            for field, text in (
                (OfficialTextField.OUTCOMES, f"Outcomes {code}"),
                (OfficialTextField.PREREQUISITE_TEXT, "hidden prerequisite sentinel"),
                (OfficialTextField.ADDITIONAL_INFO, "hidden additional sentinel"),
            ):
                session.add(
                    OfficialCourseText(
                        course_version_id=version_id,
                        field=field,
                        language="fi",
                        text=text,
                    )
                )
    embedding = embedding_settings()
    seed_embeddings(factory, embedding)
    curriculum = CurriculumRepository(factory, university=UNIVERSITIES["example"])
    teaching_text = TeachingTextService(curriculum, DerivedTranslationRepository(factory))
    catalogue = prepare_operational_catalogue(curriculum, teaching_text, embedding)
    candidate_repository = InferenceCandidateRepository(factory)
    selection = settings_factory(
        InferenceSelectionSettings,
        INFERENCE_SELECTION_POOL_K=count,
        INFERENCE_SELECTION_FINAL_K=50,
    )
    context_settings = settings_factory(InferenceContextSettings)
    rerank_input = settings_factory(InferenceRerankInputSettings)
    rerank_settings = settings_factory(InferenceRerankSettings)
    boundary = InferenceHttpBoundary(settings_factory(InferenceSettings), prompt_tokens=1000)
    transport = httpx.MockTransport(boundary.handle)

    def reranker() -> RerankProvider:
        return RerankProvider(rerank_settings, transport=transport)

    pool = select_operational_multiview_pool(
        catalogue,
        "target",
        candidate_repository,
        selection=selection,
        context_settings=context_settings,
    )
    assert len(pool.references.candidates) == count
    assert pool.pool_sha256 == selection_references_sha256(pool.references)
    split = rerank_operational_pool(
        pool,
        reranker(),
        catalogue=catalogue,
        selection=selection,
        context_settings=context_settings,
        rerank_input=rerank_input,
        rerank_settings=rerank_settings,
    )
    assert len(split.references.candidates) == 50
    assert split.packet_sha256 == selection_references_sha256(split.references)

    rerank_requests = [
        request for request in boundary.requests if request.url.path.endswith("/rerank")
    ]
    assert len(rerank_requests) == count
    # No hidden prerequisite or additional-information cell may reach any rerank request.
    for request in rerank_requests:
        payload = json.loads(request.content)
        rendered_pair = payload["query"] + "".join(payload["documents"])
        assert "hidden prerequisite sentinel" not in rendered_pair
        assert "hidden additional sentinel" not in rendered_pair

    # A mid-pool admission refusal raises the fixed signal and never returns a partial target input.
    refusal = {"calls": 0}

    def flaky() -> bool:
        refusal["calls"] += 1
        return refusal["calls"] <= 3

    with pytest.raises(RequestNotStarted):
        rerank_operational_pool(
            pool,
            reranker(),
            catalogue=catalogue,
            selection=selection,
            context_settings=context_settings,
            rerank_input=rerank_input,
            rerank_settings=rerank_settings,
            before_request=flaky,
        )
    assert refusal["calls"] == 4


def test_inference_pair_policy_controls_packet_rerank_and_processing_input() -> None:
    """The allowed inference pairs filter packet/reference/rerank input while embeddings keep every
    configured effective cell, and a nondefault policy changes only the cycle compatibility payload."""
    from course_explorer.config import (
        InferenceContextSettings,
        InferenceRerankInputSettings,
        InferenceRerankSettings,
        InferenceSelectionSettings,
    )

    policy = replace(
        UNIVERSITIES["hanken"],
        official_languages=("sv", "fi"),
        display_fallback={"fi": ("fi", "sv"), "en": ("sv", "fi")},
        translation_priority={"sv": ("fi",), "fi": ("sv",)},
        inference_languages=(
            ("sv", TeachingTextOrigin.OFFICIAL),
            ("fi", TeachingTextOrigin.OFFICIAL),
        ),
    )
    target_version = catalogue_version("source-target").model_copy(
        update={"texts": {"name": {"sv": "Svenska"}}}
    )
    candidate_version = catalogue_version("source-candidate").model_copy(
        update={"texts": {"name": {"sv": "Kandidat"}}}
    )
    rows = (
        # A current Finnish translation from the official Swedish target cell. It is effective
        # text for embedding, but the official-only inference policy excludes it from the packet.
        derived_translation(
            "source-target",
            TeachingTextField.NAME,
            "fi",
            "sv",
            "Svenska",
            "Suomi",
        ),
    )
    courses = (
        course("target", target_version),
        course("candidate", candidate_version),
    )
    curriculum = FakeCurriculum(courses)
    teaching_text = TeachingTextService(curriculum, FakeTranslations(rows), university=policy)
    embedding = embedding_settings()
    catalogue = prepare_operational_catalogue(
        curriculum, teaching_text, embedding, university=policy
    )

    # Every configured effective cell drives the embedding chunks.
    from course_explorer.embeddings.generation import build_course_embedding_chunks

    desired = build_course_embedding_chunks(catalogue.teaching_text_catalogue, embedding)
    assert {chunk.language for chunk in desired} == {"sv", "fi"}
    # The inference projection carries only official Swedish for the target and candidate.
    target_effective = catalogue.effective_by_group["target"]
    assert [cell.language for cell in target_effective.texts] == ["sv"]
    target_reference = InferenceCourseReference(
        group_id="target",
        source_version_id="source-target",
        representative_source_sha256=target_version.source_hash,
        teaching_text_sha256=processing_text_sha256(
            tuple((cell.field.value, cell.language, cell.text) for cell in target_effective.texts)
        ),
    )
    candidate_effective = catalogue.effective_by_group["candidate"]
    candidate_reference = InferenceCourseReference(
        group_id="candidate",
        source_version_id="source-candidate",
        representative_source_sha256=candidate_version.source_hash,
        teaching_text_sha256=processing_text_sha256(
            tuple(
                (cell.field.value, cell.language, cell.text) for cell in candidate_effective.texts
            )
        ),
    )
    packet = materialize_selection_packet(
        InferenceSelectionReferences(target=target_reference, candidates=(candidate_reference,)),
        catalogue,
    )
    rendered = render_rerank_course_text(packet.target, max_characters=1000)
    assert "sv: Svenska" in rendered
    assert "fi: Suomi" not in rendered
    assert len(packet.target.texts) == 1

    selection = settings_factory(InferenceSelectionSettings)
    context_settings = settings_factory(InferenceContextSettings)
    rerank_input = settings_factory(InferenceRerankInputSettings)
    rerank_settings = settings_factory(InferenceRerankSettings)
    arguments = {
        "selection": selection,
        "context_settings": context_settings,
        "rerank_input": rerank_input,
        "rerank_settings": rerank_settings,
    }
    baseline = processing_cycle_input_sha256(HASH_A, HASH_B, **arguments)
    # An empty prompt field policy leaves the cycle payload identical.
    assert processing_cycle_input_sha256(HASH_A, HASH_B, **arguments, prompt_fields={}) == baseline
    # The default Finnish/English policy omits every override and leaves the payload identical.
    assert (
        processing_cycle_input_sha256(
            HASH_A,
            HASH_B,
            **arguments,
            language_policy=inference_language_policy_overrides(UNIVERSITIES["example"]),
        )
        == baseline
    )
    # Inference owns the official-language list and allowed inference pairs only.
    inference_policy = inference_language_policy_overrides(policy)
    assert "translation_priority" not in inference_policy
    assert (
        processing_cycle_input_sha256(
            HASH_A,
            HASH_B,
            **arguments,
            language_policy=inference_policy,
        )
        != baseline
    )
    translation_only = replace(
        UNIVERSITIES["example"],
        translation_priority={"fi": ("en",)},
    )
    assert language_policy_overrides(translation_only) == {
        "translation_priority": translation_only.translation_priority
    }
    assert inference_language_policy_overrides(translation_only) == {}
    assert (
        processing_cycle_input_sha256(
            HASH_A,
            HASH_B,
            **arguments,
            language_policy=inference_language_policy_overrides(translation_only),
        )
        == baseline
    )


def test_prompt_field_selection_controls_prompt_and_policy_identity() -> None:
    """Selected official prompt fields enter the prompt while retrieval stays teaching-only, and the
    prompt selection changes the cycle identity only when it differs from the teaching baseline."""
    from course_explorer.config import (
        InferenceContextSettings,
        InferenceRerankInputSettings,
        InferenceRerankSettings,
        InferenceSelectionSettings,
    )

    def prompt_version(code: str, source_id: str) -> CatalogueCourseVersion:
        return version(code, source_id=source_id).model_copy(
            update={
                "texts": {
                    OfficialTextField.NAME: {"fi": f"name {code}"},
                    OfficialTextField.CONTENT: {
                        "fi": f"content {code}",
                        "en": f"content-en {code}",
                    },
                    OfficialTextField.OUTCOMES: {"fi": f"outcomes {code}"},
                    OfficialTextField.PREREQUISITE_TEXT: {
                        "fi": f"prerequisite {code}",
                        "en": f"prerequisite-en {code}",
                    },
                    OfficialTextField.ADDITIONAL_INFO: {
                        "fi": f"additional {code}",
                        "en": f"additional-en {code}",
                    },
                }
            }
        )

    target_source = prompt_version("TARGET.1", "source-target")
    candidate_source = prompt_version("CAND.1", "source-candidate")
    courses = (
        course("target", target_source),
        course("candidate", candidate_source),
    )
    effective = TeachingTextService(FakeCurriculum(courses), FakeTranslations()).build_catalogue(
        courses=courses
    )
    catalogue = _operational_catalogue(
        courses,
        effective,
        {item.group_id: item for item in courses},
        prompt_target_fields=(
            OfficialTextField.NAME,
            OfficialTextField.CONTENT,
            OfficialTextField.PREREQUISITE_TEXT,
        ),
        prompt_pool_fields=(
            OfficialTextField.NAME,
            OfficialTextField.CONTENT,
            OfficialTextField.ADDITIONAL_INFO,
        ),
    )
    references = InferenceSelectionReferences(
        target=course_reference(
            catalogue.courses_by_group["target"], catalogue.effective_by_group["target"]
        ),
        candidates=(
            course_reference(
                catalogue.courses_by_group["candidate"],
                catalogue.effective_by_group["candidate"],
            ),
        ),
    )
    assert [reference.group_id for reference in (references.target, *references.candidates)] == [
        "target",
        "candidate",
    ]
    packet = materialize_selection_packet(references, catalogue)

    # An inference list that excludes one official language keeps the teaching cells and drops every
    # non-teaching cell in that language.
    finnish_only = _operational_catalogue(
        courses,
        effective,
        {item.group_id: item for item in courses},
        prompt_target_fields=(
            OfficialTextField.NAME,
            OfficialTextField.CONTENT,
            OfficialTextField.PREREQUISITE_TEXT,
        ),
        prompt_pool_fields=(
            OfficialTextField.NAME,
            OfficialTextField.CONTENT,
            OfficialTextField.ADDITIONAL_INFO,
        ),
        prompt_official_languages=("fi",),
    )
    finnish_packet = materialize_selection_packet(references, finnish_only)
    assert finnish_packet.target.texts == packet.target.texts
    assert {
        cell.language
        for cell in finnish_packet.target.prompt_texts
        if cell.field is OfficialTextField.PREREQUISITE_TEXT
    } == {"fi"}
    assert {
        cell.language
        for cell in finnish_packet.candidates[0].course.prompt_texts
        if cell.field is OfficialTextField.ADDITIONAL_INFO
    } == {"fi"}

    # The target carries the selected teaching cells and its prerequisite text.
    # The candidate carries the selected teaching cells and its additional information.
    assert {cell.field for cell in packet.target.prompt_texts} == {
        OfficialTextField.NAME,
        OfficialTextField.CONTENT,
        OfficialTextField.PREREQUISITE_TEXT,
    }
    assert {cell.field for cell in packet.candidates[0].course.prompt_texts} == {
        OfficialTextField.NAME,
        OfficialTextField.CONTENT,
        OfficialTextField.ADDITIONAL_INFO,
    }
    # The packet's teaching cells and the rerank renderer stay teaching-only.
    for packet_course in (packet.target, packet.candidates[0].course):
        assert {cell.field for cell in packet_course.texts} <= set(TeachingTextField)
        rendered = render_rerank_course_text(packet_course, max_characters=1000)
        assert "prerequisite" not in rendered
        assert "additional" not in rendered

    # The rendered user prompt carries the selected extra fields, and the system prompt adds one
    # guidance paragraph that states the official structure is never supplied.
    request = render_target_request(packet, load_prompt_pack("v9"), "v2")
    body = json.loads(request.user_prompt)
    assert {entry["field"] for entry in body["target"]["texts"]} == {
        "name",
        "content",
        "prerequisite_text",
    }
    assert {entry["field"] for entry in body["candidates"][0]["texts"]} == {
        "name",
        "content",
        "additional_info",
    }
    assert "official prerequisite structure is never supplied" in request.system_prompt

    # The catalogue identity reads only identity, activity and effective teaching cells, so the
    # extra official cells do not change its bytes.
    teaching_fields = (
        OfficialTextField.NAME,
        OfficialTextField.CONTENT,
        OfficialTextField.OUTCOMES,
    )

    def stripped(representative: CatalogueCourseVersion) -> CatalogueCourseVersion:
        return representative.model_copy(
            update={
                "texts": {
                    field: cells
                    for field, cells in representative.texts.items()
                    if field in teaching_fields
                }
            }
        )

    stripped_courses = (
        course("target", stripped(target_source)),
        course("candidate", stripped(candidate_source)),
    )
    assert catalogue_identity_sha256(
        courses, effective, embedding_configuration_hash=HASH_A
    ) == catalogue_identity_sha256(stripped_courses, effective, embedding_configuration_hash=HASH_A)

    # A nondefault prompt selection changes the cycle identity, and the baseline leaves it.
    overridden = settings_factory(
        InferenceContextSettings,
        INFERENCE_TARGET_FIELDS=["name", "content", "prerequisite_text"],
        INFERENCE_POOL_FIELDS=["name", "content", "additional_info"],
    )
    baseline_context = settings_factory(
        InferenceContextSettings,
        INFERENCE_TARGET_FIELDS=["name", "content", "outcomes"],
        INFERENCE_POOL_FIELDS=["name", "content", "outcomes"],
    )
    university = UNIVERSITIES["example"]
    policy = overridden.prompt_field_policy(university)
    assert policy == {
        "target": ["name", "content", "prerequisite_text"],
        "pool": ["name", "content", "additional_info"],
    }
    assert baseline_context.prompt_field_policy(university) == {}
    arguments = {
        "selection": settings_factory(InferenceSelectionSettings),
        "context_settings": overridden,
        "rerank_input": settings_factory(InferenceRerankInputSettings),
        "rerank_settings": settings_factory(InferenceRerankSettings),
    }
    baseline = processing_cycle_input_sha256(HASH_A, HASH_B, **arguments)
    assert processing_cycle_input_sha256(HASH_A, HASH_B, **arguments, prompt_fields={}) == baseline
    assert (
        processing_cycle_input_sha256(HASH_A, HASH_B, **arguments, prompt_fields=policy) != baseline
    )
