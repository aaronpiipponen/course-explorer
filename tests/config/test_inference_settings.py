"""Inference provenance identity tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from course_explorer.config import (
    InferenceRerankSettings,
    InferenceSettings,
    TranslationSettings,
    load_inference_settings,
)
from tests.config._support import (
    INFERENCE_KEYS,
    set_inference_environment,
    set_translation_environment,
)
from tests.support import settings_factory, write_local_conversion_translation

FIRST_KEY_IDENTITY = (
    "Qwen/Qwen3.8-27B-FP8@017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
    ";artifact=vmarcelo/Qwen3.8-27B-MIX_GGUF"
    "@f922f9d9bbe67ede1c49e364d71737b8f067e49b/Qwen3.8-27B-IQ3-MIX.gguf"
    ";quant=IQ3_MIX"
    ";sha256=3089d39401e60551a03e0858d775d6b33f966f6e6513f31f75a011cfc8f56309"
)


def _example_variants() -> list[dict[str, object]]:
    """Return the committed example local keys as one mutable list."""
    return json.loads(INFERENCE_KEYS["INFERENCE_LOCAL_VARIANTS"])


def test_inference_settings_require_two_key_mix_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two-key MIX and exact translation/model/runtime/rerank identities derive."""
    set_inference_environment(monkeypatch)
    settings = InferenceSettings()
    first, second = settings.inference_local_variants
    assert (first.port, first.context_size) == (18140, 131072)
    assert second.port == 18160
    assert second.context_size > first.context_size
    assert second.achieved_max_context == second.context_size
    assert first.mtp_draft_n_max == 2
    assert second.mtp_draft_n_max is None
    assert settings.local_model_identity(first) == FIRST_KEY_IDENTITY
    assert settings.local_runtime_identity(first) == (
        "llama.cpp@4ff829ec2e2f526aa6afba529eebbfb3ef1f95ec"
        ";alias=qwen3.8-27b-iq3-mix-128k-mtp"
        ";context=131072"
        ";parallel=1"
        ";gpu_layers=all"
        ";kv_cache=q4_0"
        ";flash_attention=on"
        ";batch=256"
        ";ubatch=256"
        ";speculative=draft-mtp,ngram-mod"
        ";mtp_draft_n_max=2"
        ";threads=8"
        ";threads_batch=16"
        ";max_tokens=8192"
        ";generation_attempts=unbounded"
        ";initial_temperature=0.0"
        ";first_attempt_scope=cycle-target-calculation"
        ";retry_temperature=0.3"
        ";seed_base=0"
        ";seed_schedule=canonical-target-counter-calculation-first-v1"
        ";retry_backoff_seconds=1.0"
        ";retry_max_backoff_seconds=10.0"
        ";retry_poll_seconds=0.1"
        ';chat_template_kwargs={"enable_thinking":false}'
    )
    # The local retry policy is recorded in the local identity only. The deterministic cross-encoder
    # identity is unaffected by it.
    assert load_inference_settings() == settings

    rerank = InferenceRerankSettings()
    assert rerank.rerank_model_identity == (
        "BAAI/bge-reranker-v2-m3@953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
        ";artifact=gpustack/bge-reranker-v2-m3-GGUF"
        "@3093af03b1a635e67b084b1d8c03c5f5e020fd05/bge-reranker-v2-m3-Q8_0.gguf"
        ";quant=Q8_0"
        ";sha256=a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3"
    )
    assert rerank.rerank_runtime_identity == (
        "llama.cpp@4ff829ec2e2f526aa6afba529eebbfb3ef1f95ec"
        ";alias=bge-reranker-v2-m3-q8"
        ";context=8192"
        ";host=127.0.0.1"
        ";port=18083"
        ";embedding=true"
        ";pooling=rank"
        ";reranking=true"
        ";batch=2048"
        ";ubatch=2048"
        ";timeout=60"
    )

    # A changed retry temperature changes the local execution identity but never the rerank one.
    changed = settings.model_copy(update={"inference_retry_temperature": 0.7})
    assert changed.local_runtime_identity(first) != settings.local_runtime_identity(first)
    assert InferenceRerankSettings().rerank_runtime_identity == rerank.rerank_runtime_identity

    small_variants = _example_variants()
    small_variants[0].update(
        {
            "artifact_file": "Qwen3-1.7B-Q8_0.gguf",
            "artifact_path": "models/qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf",
            "artifact_quant": "Q8_0",
        }
    )
    set_inference_environment(monkeypatch, INFERENCE_LOCAL_VARIANTS=json.dumps(small_variants))
    small = InferenceSettings()
    small_first, _ = small.inference_local_variants
    identity = small.local_model_identity(small_first)
    assert ";quant=Q8_0" in identity
    assert "Qwen3-1.7B-Q8_0.gguf" in identity

    set_translation_environment(monkeypatch)
    published = TranslationSettings()
    assert published.provenance_model == (
        "google/translategemma-12b-it@d1b225e1caa17f1ddc7e62065d8637d0923f34e2"
        ";Q5_K_M;sha256:be458d9ab099e0f418bc5687c5e97cf1555c3d32a012a196744db917656becbc"
    )
    assert str(published.translation_artifact_url) == (
        "https://huggingface.co/mradermacher/translategemma-12b-it-GGUF"
        "/resolve/41d7c8aa650b26791225ad9d0597e674e680da96"
        "/translategemma-12b-it.Q5_K_M.gguf"
    )

    # A local conversion names no artifact source: the three source keys are absent, the derived
    # URL is None, and the provenance keeps the digest the file records.
    local = tmp_path / "translation.toml"
    write_local_conversion_translation(local)
    tracked = tomllib.loads(local.read_text(encoding="utf-8"))
    for alias in (
        "TRANSLATION_ARTIFACT_REPOSITORY",
        "TRANSLATION_ARTIFACT_REVISION",
        "TRANSLATION_ARTIFACT_FILE",
    ):
        monkeypatch.delenv(alias, raising=False)
    local_conversion = TranslationSettings(**tracked)
    assert local_conversion.translation_artifact_repository is None
    assert local_conversion.translation_artifact_revision is None
    assert local_conversion.translation_artifact_file is None
    assert local_conversion.translation_artifact_url is None
    assert local_conversion.provenance_model == (
        f"{tracked['TRANSLATION_MODEL_REPOSITORY']}@{tracked['TRANSLATION_MODEL_REVISION']}"
        f";{tracked['TRANSLATION_ARTIFACT_QUANT']}"
        f";sha256:{tracked['TRANSLATION_ARTIFACT_SHA256']}"
    )
    with pytest.raises(ValueError):
        TranslationSettings(
            **{**tracked, "TRANSLATION_ARTIFACT_FILE": "translategemma-12b-it.Q5_K_M.gguf"}
        )


def test_each_local_key_names_its_own_model_and_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ordered key derives its own identity and URL from its own model and artifact."""
    set_inference_environment(monkeypatch)
    settings = InferenceSettings()
    first, _ = settings.inference_local_variants
    assert settings.local_model_identity(first) == FIRST_KEY_IDENTITY

    moved = _example_variants()
    moved[1].update(
        {
            "model_repository": "Qwen/Qwen3-1.7B",
            "model_revision": "1" * 40,
            "artifact_repository": "example/Qwen3-1.7B-GGUF",
            "artifact_revision": "2" * 40,
            "artifact_file": "Qwen3-1.7B-Q8_0.gguf",
            "artifact_path": "models/qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf",
            "artifact_sha256": "6" * 64,
            "artifact_quant": "Q8_0",
        }
    )
    paired = settings_factory(InferenceSettings, INFERENCE_LOCAL_VARIANTS=moved)
    paired_first, paired_second = paired.inference_local_variants
    assert paired.local_model_identity(paired_second) != paired.local_model_identity(paired_first)
    assert paired.local_artifact_url(paired_second) != paired.local_artifact_url(paired_first)
    assert str(paired.local_artifact_url(paired_second)) == (
        f"https://huggingface.co/example/Qwen3-1.7B-GGUF/resolve/{'2' * 40}/Qwen3-1.7B-Q8_0.gguf"
    )

    # One artifact path carries one digest, and an artifact path names exactly its file.
    shared = _example_variants()
    shared[1]["artifact_file"] = shared[0]["artifact_file"]
    shared[1]["artifact_path"] = shared[0]["artifact_path"]
    shared[1]["artifact_sha256"] = "7" * 64
    with pytest.raises(ValueError):
        settings_factory(InferenceSettings, INFERENCE_LOCAL_VARIANTS=shared)

    mismatched_name = _example_variants()
    mismatched_name[0]["artifact_file"] = "Qwen3.8-27B-IQ3-MIX-v2.gguf"
    with pytest.raises(ValueError):
        settings_factory(InferenceSettings, INFERENCE_LOCAL_VARIANTS=mismatched_name)

    # A draft count belongs to the first key alone and only when its speculation names it.
    fallback_count = _example_variants()
    fallback_count[1]["mtp_draft_n_max"] = 2
    with pytest.raises(ValueError):
        settings_factory(InferenceSettings, INFERENCE_LOCAL_VARIANTS=fallback_count)
    missing_count = _example_variants()
    missing_count[0].pop("mtp_draft_n_max")
    with pytest.raises(ValueError):
        settings_factory(InferenceSettings, INFERENCE_LOCAL_VARIANTS=missing_count)
    no_mtp = _example_variants()
    no_mtp[0]["speculative_type"] = "ngram-mod"
    no_mtp[0].pop("mtp_draft_n_max")
    plain = settings_factory(InferenceSettings, INFERENCE_LOCAL_VARIANTS=no_mtp)
    assert plain.inference_local_variants[0].mtp_draft_n_max is None
