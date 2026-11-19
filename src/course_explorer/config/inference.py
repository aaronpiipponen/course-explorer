"""Validated settings for the local inference runtime, its context, selection, rerank
and controller policy."""

import json
import math
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    _ANY_HTTP_URL_ADAPTER,
    _GGUF_FILE_PATTERN,
    _GPU_LAYERS_PATTERN,
    _REPOSITORY_PATTERN,
    ConfiguredSettings,
    _require_artifact_sha256,
    _require_decimal_int,
    _require_int_bounds,
    _require_loopback_host,
    _require_relative_path,
    _require_revision,
    _require_tcp_port,
)
from course_explorer.config.worker import WorkerConnectionSettings, WorkerRuntimeSettings
from course_explorer.enums import OfficialTextField, TeachingTextField
from course_explorer.instance import UniversityMetadata, validate_prompt_fields
from course_explorer.validation import (
    require_nonblank_text as _require_nonblank_text,
)

_INFERENCE_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_INFERENCE_QUANTIZATION_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*$")


class InferenceLocalVariantSettings(BaseModel):
    """One ordered local fit key: its source model and artifact, loopback port, context, KV,
    batch, and MTP.

    Each key names one measured fit key and the model and artifact it serves, so two keys of one
    configuration may serve two different files or share one. ``mtp_draft_n_max`` belongs to the
    first key alone and only when that key's ``speculative_type`` names ``draft-mtp``: the runtime
    takes no separate draft model, so a preset may run two keys without MTP speculation.
    ``achieved_max_context`` is the largest context the measurement achieved for that key.
    ``artifact_path`` is absolute or relative to the worker checkout, and its basename equals
    ``artifact_file``. These are validated configuration, never provider mechanics.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    alias: str
    port: int
    context_size: int
    kv_cache_type: Literal["q4_0"]
    batch_size: int
    ubatch_size: int
    speculative_type: str
    mtp_draft_n_max: int | None = None
    achieved_max_context: int
    model_repository: str
    model_revision: str
    artifact_repository: str
    artifact_revision: str
    artifact_file: str
    artifact_path: str
    artifact_sha256: str
    artifact_quant: str

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        """Require one short request alias without whitespace or path characters."""
        if _INFERENCE_ALIAS_PATTERN.fullmatch(value) is None:
            raise ValueError("inference local variant alias must be a short ASCII alias")
        return value

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        """Require a valid loopback TCP port."""
        return _require_tcp_port(value, setting_name="INFERENCE_LOCAL_VARIANTS port")

    @field_validator("context_size", "batch_size", "ubatch_size", "achieved_max_context")
    @classmethod
    def require_positive(cls, value: int) -> int:
        """Require positive context and physical batch geometry."""
        if value <= 0:
            raise ValueError("inference local variant context and batch values must be positive")
        return value

    @field_validator("mtp_draft_n_max")
    @classmethod
    def validate_mtp_draft(cls, value: int | None) -> int | None:
        """Require a positive MTP draft count when the key carries MTP speculation."""
        if value is not None and value <= 0:
            raise ValueError("inference local variant MTP draft count must be positive")
        return value

    @field_validator("speculative_type")
    @classmethod
    def validate_speculative_type(cls, value: str) -> str:
        """Require one nonblank speculative-decoding type string without rewriting it."""
        return _require_nonblank_text(
            value, message="inference local variant speculative type must not be blank"
        )

    @field_validator("model_repository", "artifact_repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Require one ordered public ``namespace/name`` repository identity."""
        if _REPOSITORY_PATTERN.fullmatch(value) is None:
            raise ValueError("INFERENCE_LOCAL_VARIANTS repositories must match namespace/name")
        return value

    @field_validator("model_revision", "artifact_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """Require a full lowercase pinned commit identity."""
        return _require_revision(
            value,
            message="INFERENCE_LOCAL_VARIANTS revisions must be 40 lowercase hexadecimal characters",
        )

    @field_validator("artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        """Require a full lowercase SHA-256 artifact identity."""
        return _require_artifact_sha256(
            value, setting_name="INFERENCE_LOCAL_VARIANTS artifact_sha256"
        )

    @field_validator("artifact_file")
    @classmethod
    def validate_artifact_file(cls, value: str) -> str:
        """Require one GGUF artifact file name without directories."""
        if _GGUF_FILE_PATTERN.fullmatch(value) is None:
            raise ValueError("INFERENCE_LOCAL_VARIANTS artifact_file must be a GGUF file name")
        return value

    @field_validator("artifact_quant")
    @classmethod
    def validate_artifact_quant(cls, value: str) -> str:
        """Require one quantization token of alphanumeric groups joined by ``_`` or ``-``."""
        if _INFERENCE_QUANTIZATION_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "INFERENCE_LOCAL_VARIANTS artifact_quant must be alphanumeric groups joined by "
                "'_' or '-'"
            )
        return value

    @field_validator("artifact_path", mode="before")
    @classmethod
    def validate_artifact_path(cls, value: object) -> object:
        """Require a raw nonblank artifact path before Pydantic coerces it to ``str``."""
        _require_nonblank_text(
            str(value), message="INFERENCE_LOCAL_VARIANTS artifact_path must not be blank"
        )
        return value

    @model_validator(mode="after")
    def validate_variant_bounds(self) -> Self:
        """Require ubatch not to exceed batch and context not to exceed its achieved maximum."""
        if self.ubatch_size > self.batch_size:
            raise ValueError("inference local variant ubatch must not exceed batch")
        if self.context_size > self.achieved_max_context:
            raise ValueError(
                "inference local variant context must not exceed its achieved maximum context"
            )
        return self

    @model_validator(mode="after")
    def validate_artifact_path_matches_file(self) -> Self:
        """Require the configured artifact path to name exactly the configured artifact file."""
        if Path(self.artifact_path).name != self.artifact_file:
            raise ValueError(
                "INFERENCE_LOCAL_VARIANTS artifact_path basename must equal artifact_file"
            )
        return self


# The fixed implementation-contract label of the deterministic generation-attempt schedule:
# one greedy attempt per cycle-target calculation, then deterministic-seed retries driven by the
# durable lifetime counter. A different schedule carries a different label, so recorded evidence
# names the schedule that produced it.
_GENERATION_ATTEMPT_SCHEDULE = "canonical-target-counter-calculation-first-v1"


class InferenceSettings(WorkerRuntimeSettings):
    """Validated loopback contract for the solo fit-only MIX inference runtime.

    The pinned llama.cpp runtime serves two measured ordered local fit keys, and each key names
    its own source model and artifact. The first key is the request tokenizer authority and may
    carry MTP speculation when its type names it. The second key is the larger no-MTP fallback and
    carries no draft count. A packet that fits neither
    configured key has no configured route and fails as one target capacity failure, unless a
    caller injects its own explicit oversize route. The three closed policy keys accept one literal
    each: ``INFERENCE_ROUTING_POLICY`` is ``solo_fit_only``, ``INFERENCE_RECOMMENDATION_POLICY`` is
    ``include``, and ``INFERENCE_FINDING_FILTER_POLICY`` is ``none``.
    `INFERENCE_PROPOSAL_VERSION` is the separate result proposal composition version that binds one
    proposal identity together with the official snapshot and its source attempt.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    inference_host: str = Field(validation_alias="INFERENCE_HOST")
    inference_local_variants: tuple[InferenceLocalVariantSettings, ...] = Field(
        validation_alias="INFERENCE_LOCAL_VARIANTS"
    )
    inference_runtime_revision: str = Field(validation_alias="INFERENCE_RUNTIME_REVISION")
    inference_parallel: int = Field(validation_alias="INFERENCE_PARALLEL", gt=0)
    inference_gpu_layers: str = Field(validation_alias="INFERENCE_GPU_LAYERS")
    inference_flash_attention: Literal["on"] = Field(validation_alias="INFERENCE_FLASH_ATTENTION")
    inference_request_timeout_seconds: float = Field(
        validation_alias="INFERENCE_REQUEST_TIMEOUT_SECONDS",
        gt=0,
        le=600,
        allow_inf_nan=False,
    )
    inference_max_attempts: int = Field(validation_alias="INFERENCE_MAX_ATTEMPTS", ge=1, le=5)
    inference_retry_backoff_seconds: float = Field(
        validation_alias="INFERENCE_RETRY_BACKOFF_SECONDS", ge=0, le=10, allow_inf_nan=False
    )
    inference_retry_max_backoff_seconds: float = Field(
        validation_alias="INFERENCE_RETRY_MAX_BACKOFF_SECONDS", gt=0, le=600, allow_inf_nan=False
    )
    inference_retry_poll_seconds: float = Field(
        validation_alias="INFERENCE_RETRY_POLL_SECONDS", gt=0, le=1, allow_inf_nan=False
    )
    inference_max_tokens: int = Field(validation_alias="INFERENCE_MAX_TOKENS", gt=0)
    inference_fit_headroom_tokens: int = Field(
        validation_alias="INFERENCE_FIT_HEADROOM_TOKENS", gt=0
    )
    inference_temperature: float = Field(
        validation_alias="INFERENCE_TEMPERATURE", ge=0, le=0, allow_inf_nan=False
    )
    inference_retry_temperature: float = Field(
        validation_alias="INFERENCE_RETRY_TEMPERATURE", gt=0, le=2, allow_inf_nan=False
    )
    inference_seed: int = Field(validation_alias="INFERENCE_SEED", ge=0)
    inference_chat_template_kwargs: dict[str, bool] = Field(
        validation_alias="INFERENCE_CHAT_TEMPLATE_KWARGS"
    )
    inference_routing_policy: Literal["solo_fit_only"] = Field(
        validation_alias="INFERENCE_ROUTING_POLICY"
    )
    inference_recommendation_policy: Literal["include"] = Field(
        validation_alias="INFERENCE_RECOMMENDATION_POLICY"
    )
    inference_finding_filter_policy: Literal["none"] = Field(
        validation_alias="INFERENCE_FINDING_FILTER_POLICY"
    )
    inference_prompt_version: Literal["v9"] = Field(validation_alias="INFERENCE_PROMPT_VERSION")
    inference_schema_version: Literal["v2"] = Field(validation_alias="INFERENCE_SCHEMA_VERSION")
    inference_pipeline_version: Literal["v1"] = Field(validation_alias="INFERENCE_PIPELINE_VERSION")
    inference_proposal_version: Literal["v1"] = Field(validation_alias="INFERENCE_PROPOSAL_VERSION")

    @field_validator("inference_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Permit only the loopback address the worker runtime may bind."""
        return _require_loopback_host(value, setting_name="INFERENCE_HOST")

    @field_validator("inference_runtime_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """Require a full lowercase pinned commit identity."""
        return _require_revision(
            value, message="inference revisions must be 40 lowercase hexadecimal characters"
        )

    @field_validator("inference_gpu_layers")
    @classmethod
    def validate_gpu_layers(cls, value: str) -> str:
        """Require full offload or one nonnegative layer count."""
        if _GPU_LAYERS_PATTERN.fullmatch(value) is None:
            raise ValueError("INFERENCE_GPU_LAYERS must be 'all' or a nonnegative integer")
        return value

    @field_validator("inference_chat_template_kwargs")
    @classmethod
    def validate_chat_template_kwargs(cls, value: dict[str, bool]) -> dict[str, bool]:
        """Require the non-thinking chat-template flag set."""
        if value != {"enable_thinking": False}:
            raise ValueError(
                'INFERENCE_CHAT_TEMPLATE_KWARGS must be exactly {"enable_thinking": false}'
            )
        return value

    @model_validator(mode="after")
    def validate_local_variants(self) -> Self:
        """Require two ordered unique loopback keys, the request tokenizer key first.

        The first key is the request tokenizer authority and may carry MTP speculation when its
        type names ``draft-mtp``, so a no-MTP first key is valid. The second key is the larger
        fallback and never carries a draft count.
        """
        variants = self.inference_local_variants
        if len(variants) != 2:
            raise ValueError("INFERENCE_LOCAL_VARIANTS must configure exactly two ordered keys")
        if len({variant.alias for variant in variants}) != 2:
            raise ValueError("INFERENCE_LOCAL_VARIANTS aliases must be unique")
        if len({variant.port for variant in variants}) != 2:
            raise ValueError("INFERENCE_LOCAL_VARIANTS ports must be unique")
        if variants[0].context_size >= variants[1].context_size:
            raise ValueError("INFERENCE_LOCAL_VARIANTS must be ordered by increasing context")
        if variants[1].mtp_draft_n_max is not None:
            raise ValueError(
                "INFERENCE_LOCAL_VARIANTS must not give the fallback key a draft count"
            )
        for variant in variants:
            names_draft_mtp = "draft-mtp" in variant.speculative_type
            if names_draft_mtp != (variant.mtp_draft_n_max is not None):
                raise ValueError(
                    "INFERENCE_LOCAL_VARIANTS must carry a draft count exactly when its "
                    "speculative type names draft-mtp"
                )
        # Two keys that name one artifact path serve one file, so they must name one digest.
        digests: dict[str, str] = {}
        for variant in variants:
            previous = digests.setdefault(variant.artifact_path, variant.artifact_sha256)
            if previous != variant.artifact_sha256:
                raise ValueError(
                    "INFERENCE_LOCAL_VARIANTS keys that share artifact_path must share "
                    "artifact_sha256"
                )
        return self

    @model_validator(mode="after")
    def validate_output_budget_below_local_contexts(self) -> Self:
        """Require the output cap plus fit margin to fit strictly inside every local context."""
        budget = self.inference_max_tokens + self.inference_fit_headroom_tokens
        if any(budget >= variant.context_size for variant in self.inference_local_variants):
            raise ValueError(
                "INFERENCE_MAX_TOKENS plus INFERENCE_FIT_HEADROOM_TOKENS must be smaller than "
                "every INFERENCE_LOCAL_VARIANTS context"
            )
        return self

    @model_validator(mode="after")
    def validate_retry_backoff_bounds(self) -> Self:
        """Require the local base and poll interval to stay inside the configured retry cap.

        The capped backoff grows from the base toward the cap, and the cooperative poll chunk is
        bounded by the same cap, so a base or poll larger than the cap would silently change the
        retry timing. Both are validated once here rather than at each sleep site.
        """
        if self.inference_retry_backoff_seconds > self.inference_retry_max_backoff_seconds:
            raise ValueError(
                "INFERENCE_RETRY_BACKOFF_SECONDS must not exceed "
                "INFERENCE_RETRY_MAX_BACKOFF_SECONDS"
            )
        if self.inference_retry_poll_seconds > self.inference_retry_max_backoff_seconds:
            raise ValueError(
                "INFERENCE_RETRY_POLL_SECONDS must not exceed INFERENCE_RETRY_MAX_BACKOFF_SECONDS"
            )
        return self

    def local_generation_parameters(
        self, attempt_ordinal: int, *, first_attempt: bool
    ) -> tuple[float, int]:
        """Return the local sampling pair for one reserved generation-attempt ordinal.

        The cycle-target calculation's first attempt is greedy at the configured initial zero
        temperature. Every subsequent attempt samples at the configured retry temperature. The seed
        is the configured base advanced by the durable lifetime ordinal (one-based), so a consumed
        ordinal never re-draws a seed. The reservation owner guarantees the positive ordinal and the
        boolean.
        """
        temperature = (
            self.inference_temperature if first_attempt else self.inference_retry_temperature
        )
        return temperature, self.inference_seed + attempt_ordinal - 1

    def local_base_url(self, variant: InferenceLocalVariantSettings) -> AnyHttpUrl:
        """Derive one variant's loopback base URL from the shared host and configured port."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(f"http://{self.inference_host}:{variant.port}")

    def local_artifact_url(self, variant: InferenceLocalVariantSettings) -> AnyHttpUrl:
        """Derive one key's immutable public resolve URL from its configured artifact identity."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(
            f"https://huggingface.co/{variant.artifact_repository}"
            f"/resolve/{variant.artifact_revision}"
            f"/{variant.artifact_file}"
        )

    def local_model_identity(self, variant: InferenceLocalVariantSettings) -> str:
        """Return one key's immutable source-model plus artifact identity persisted with every run."""
        return (
            f"{variant.model_repository}"
            f"@{variant.model_revision}"
            f";artifact={variant.artifact_repository}"
            f"@{variant.artifact_revision}"
            f"/{variant.artifact_file}"
            f";quant={variant.artifact_quant}"
            f";sha256={variant.artifact_sha256}"
        )

    def local_runtime_identity(self, variant: InferenceLocalVariantSettings) -> str:
        """Return one selected local variant's immutable runtime/generation identity.

        The identity binds the pinned llama.cpp revision, the selected key's alias/context/KV and
        MTP speculation, the shared physical batch/offload values, the generation thread geometry,
        and the common generation settings. Changing any of these records different execution
        provenance. It is not scheduling permission and never invalidates retained work.
        """
        chat_template_kwargs = json.dumps(
            self.inference_chat_template_kwargs, sort_keys=True, separators=(",", ":")
        )
        mtp_draft_n_max = (
            "none" if variant.mtp_draft_n_max is None else str(variant.mtp_draft_n_max)
        )
        return (
            f"llama.cpp@{self.inference_runtime_revision}"
            f";alias={variant.alias}"
            f";context={variant.context_size}"
            f";parallel={self.inference_parallel}"
            f";gpu_layers={self.inference_gpu_layers}"
            f";kv_cache={variant.kv_cache_type}"
            f";flash_attention={self.inference_flash_attention}"
            f";batch={variant.batch_size}"
            f";ubatch={variant.ubatch_size}"
            f";speculative={variant.speculative_type}"
            f";mtp_draft_n_max={mtp_draft_n_max}"
            f";threads={self.worker_runtime_threads}"
            f";threads_batch={self.worker_runtime_threads_batch}"
            f";max_tokens={self.inference_max_tokens}"
            f";generation_attempts=unbounded"
            f";initial_temperature={self.inference_temperature}"
            f";first_attempt_scope=cycle-target-calculation"
            f";retry_temperature={self.inference_retry_temperature}"
            f";seed_base={self.inference_seed}"
            f";seed_schedule={_GENERATION_ATTEMPT_SCHEDULE}"
            f";retry_backoff_seconds={self.inference_retry_backoff_seconds}"
            f";retry_max_backoff_seconds={self.inference_retry_max_backoff_seconds}"
            f";retry_poll_seconds={self.inference_retry_poll_seconds}"
            f";chat_template_kwargs={chat_template_kwargs}"
        )


def load_inference_settings() -> InferenceSettings:
    """Read inference settings through the automatic subsystem-file and environment-file
    composition."""
    return InferenceSettings()


# The canonical multiview field order, derived from the neutral ``TeachingTextField`` enum, which
# carries no ORM or database dependency. Settings serialize these wire values as plain strings.
_INFERENCE_MULTIVIEW_FIELD_ORDER: tuple[str, ...] = tuple(
    field.value for field in TeachingTextField
)


class InferenceContextSettings(ConfiguredSettings):
    """Validated label-blind multiview field-view, fusion and prompt-field settings for live selection.

    The two multiview values own the accepted field views and the reciprocal-rank fusion offset
    consumed by the operational cross-encoder selection. The two prompt-field values are optional
    overrides of the configured university map entry's target and pool defaults. They are operations
    selection and prompt policy apart from the runtime contract in ``InferenceSettings``. Loading
    composes the subsystem files under ``config/`` and the device-local ignored ``.env``, and no code
    default applies to the required values.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    inference_multiview_evaluation_fields: tuple[Literal["name", "content", "outcomes"], ...] = (
        Field(validation_alias="INFERENCE_MULTIVIEW_EVALUATION_FIELDS")
    )
    inference_multiview_fusion_rank_offset: int = Field(
        validation_alias="INFERENCE_MULTIVIEW_FUSION_RANK_OFFSET"
    )
    inference_target_fields: (
        tuple[Literal["name", "content", "outcomes", "prerequisite_text", "additional_info"], ...]
        | None
    ) = Field(default=None, validation_alias="INFERENCE_TARGET_FIELDS")
    inference_pool_fields: (
        tuple[Literal["name", "content", "outcomes", "prerequisite_text", "additional_info"], ...]
        | None
    ) = Field(default=None, validation_alias="INFERENCE_POOL_FIELDS")

    @field_validator("inference_multiview_evaluation_fields")
    @classmethod
    def validate_multiview_fields(
        cls, value: tuple[Literal["name", "content", "outcomes"], ...]
    ) -> tuple[Literal["name", "content", "outcomes"], ...]:
        """Require a nonempty, unique field tuple in canonical name/content/outcomes order.

        The canonical order is the neutral ``TeachingTextField`` enum's declaration order, which
        carries no ORM dependency. The evaluation service converts these validated wire strings to
        ``TeachingTextField`` before a field-view query.
        """
        if not value:
            raise ValueError("INFERENCE_MULTIVIEW_EVALUATION_FIELDS must not be empty")
        for item in value:
            if item not in _INFERENCE_MULTIVIEW_FIELD_ORDER:
                raise ValueError(
                    "INFERENCE_MULTIVIEW_EVALUATION_FIELDS entries must be one of "
                    f"{list(_INFERENCE_MULTIVIEW_FIELD_ORDER)}"
                )
        if tuple(sorted(value, key=_INFERENCE_MULTIVIEW_FIELD_ORDER.index)) != value:
            raise ValueError(
                "INFERENCE_MULTIVIEW_EVALUATION_FIELDS must follow canonical name/content/"
                "outcomes order"
            )
        if len(set(value)) != len(value):
            raise ValueError("INFERENCE_MULTIVIEW_EVALUATION_FIELDS must not repeat a value")
        return value

    @field_validator("inference_target_fields", "inference_pool_fields")
    @classmethod
    def validate_prompt_fields(
        cls,
        value: tuple[
            Literal["name", "content", "outcomes", "prerequisite_text", "additional_info"], ...
        ]
        | None,
    ) -> (
        tuple[Literal["name", "content", "outcomes", "prerequisite_text", "additional_info"], ...]
        | None
    ):
        """Require an absent or nonempty, unique field tuple in canonical official-field order."""
        if value is None:
            return None
        converted = tuple(OfficialTextField(item) for item in value)
        validate_prompt_fields(converted, "inference prompt fields")
        return value

    @field_validator("inference_multiview_fusion_rank_offset")
    @classmethod
    def validate_multiview_fusion_rank_offset(cls, value: int) -> int:
        """Bound the positive reciprocal-rank fusion offset used for live multiview selection."""
        return _require_int_bounds(
            value,
            setting_name="INFERENCE_MULTIVIEW_FUSION_RANK_OFFSET",
            minimum=1,
            maximum=1000,
        )

    def resolved_target_fields(
        self, university: UniversityMetadata
    ) -> tuple[OfficialTextField, ...]:
        """Return the configured target-field override or the university map's target defaults."""
        if self.inference_target_fields is None:
            return university.inference_target_fields
        return tuple(OfficialTextField(field) for field in self.inference_target_fields)

    def resolved_pool_fields(self, university: UniversityMetadata) -> tuple[OfficialTextField, ...]:
        """Return the configured pool-field override or the university map's pool defaults."""
        if self.inference_pool_fields is None:
            return university.inference_pool_fields
        return tuple(OfficialTextField(field) for field in self.inference_pool_fields)

    def prompt_field_policy(self, university: UniversityMetadata) -> dict[str, object]:
        """Return the prompt-field selection only when it differs from the teaching-field baseline.

        The baseline is the name, content and outcomes prompt field set for the target and for the
        pool. A selection equal to the baseline produces an empty mapping, so its compatibility
        identity equals one built with no override. A nondefault selection carries both the target
        and pool fields.
        """
        target = self.resolved_target_fields(university)
        pool = self.resolved_pool_fields(university)
        baseline = (
            OfficialTextField.NAME,
            OfficialTextField.CONTENT,
            OfficialTextField.OUTCOMES,
        )
        if target == baseline and pool == baseline:
            return {}
        return {
            "target": [field.value for field in target],
            "pool": [field.value for field in pool],
        }


def load_inference_context_settings() -> InferenceContextSettings:
    """Read live multiview field-view/fusion settings through the automatic composition."""
    return InferenceContextSettings()


class InferenceSelectionSettings(ConfiguredSettings):
    """Validated pool and final-candidate policy for the label-free selection pipeline.

    ``INFERENCE_SELECTION_POOL_K`` is the embedding pool depth the live operational selection
    consumes. ``INFERENCE_SELECTION_FINAL_K`` is the candidate count of the CE-only packet, which
    the operational selection path requires to equal its fixed ceiling.
    Both are separate from ``InferenceContextSettings``, whose multiview field views and fusion
    offset are selection mechanics. Loading composes the subsystem files under ``config/`` and the
    device-local ignored ``.env`` automatically. There is no code default.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    inference_selection_pool_k: int = Field(validation_alias="INFERENCE_SELECTION_POOL_K")
    inference_selection_final_k: int = Field(validation_alias="INFERENCE_SELECTION_FINAL_K")

    @field_validator("inference_selection_pool_k")
    @classmethod
    def validate_selection_pool_k(cls, value: int) -> int:
        """Bound the positive embedding pool depth."""
        return _require_int_bounds(
            value,
            setting_name="INFERENCE_SELECTION_POOL_K",
            minimum=1,
            maximum=5000,
        )

    @field_validator("inference_selection_final_k")
    @classmethod
    def validate_selection_final_k(cls, value: int) -> int:
        """Bound the hard maximum of final candidates the whole-target packet may receive."""
        return _require_int_bounds(
            value,
            setting_name="INFERENCE_SELECTION_FINAL_K",
            minimum=1,
            maximum=50,
        )


def load_inference_selection_settings() -> InferenceSelectionSettings:
    """Read pool selection settings through the automatic subsystem-file and environment-file
    composition."""
    return InferenceSelectionSettings()


class InferenceRerankSettings(ConfiguredSettings):
    """Validated loopback contract for the one pinned multilingual cross-encoder runtime.

    The ``rerank`` slot owns a distinct, separately pinned public GGUF and its own loopback
    llama.cpp serving contract (embedding/rank-pooling/reranking). It is separate from
    ``InferenceSettings``: the local generation keys carry their own quant, request and generation
    values, and this cross-encoder has no chat template, sampling,
    or generation semantics. The llama.cpp runtime revision is shared with the generation runtimes
    through the ``INFERENCE_RUNTIME_REVISION`` key so one worker build serves both, and
    the source-model and public-quantization artifact identities stay separate so a ranked output
    is always bound to one known configuration. The single batch-geometry value drives both physical
    llama.cpp flags and is part of the derived runtime identity, because the pinned ``--embedding``
    clamp otherwise reduces the physical batch below what a document of
    ``INFERENCE_RERANK_DOCUMENT_MAX_CHARACTERS`` characters needs.
    The single request timeout bounds one cross-encoder request and is likewise part of the derived
    runtime identity, so changing it invalidates rerank inputs and evidence.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    inference_rerank_host: str = Field(validation_alias="INFERENCE_RERANK_HOST")
    inference_rerank_port: int = Field(validation_alias="INFERENCE_RERANK_PORT")
    inference_rerank_model_alias: str = Field(validation_alias="INFERENCE_RERANK_MODEL_ALIAS")
    inference_rerank_model_repository: str = Field(
        validation_alias="INFERENCE_RERANK_MODEL_REPOSITORY"
    )
    inference_rerank_model_revision: str = Field(validation_alias="INFERENCE_RERANK_MODEL_REVISION")
    inference_rerank_artifact_repository: str = Field(
        validation_alias="INFERENCE_RERANK_ARTIFACT_REPOSITORY"
    )
    inference_rerank_artifact_revision: str = Field(
        validation_alias="INFERENCE_RERANK_ARTIFACT_REVISION"
    )
    inference_rerank_artifact_file: str = Field(validation_alias="INFERENCE_RERANK_ARTIFACT_FILE")
    inference_rerank_artifact_path: Path = Field(validation_alias="INFERENCE_RERANK_ARTIFACT_PATH")
    inference_rerank_artifact_sha256: str = Field(
        validation_alias="INFERENCE_RERANK_ARTIFACT_SHA256"
    )
    inference_rerank_artifact_quant: Literal["Q8_0"] = Field(
        validation_alias="INFERENCE_RERANK_ARTIFACT_QUANT"
    )
    inference_runtime_revision: str = Field(validation_alias="INFERENCE_RUNTIME_REVISION")
    inference_rerank_context_size: int = Field(
        validation_alias="INFERENCE_RERANK_CONTEXT_SIZE", ge=1, le=8192
    )
    inference_rerank_batch_size: int = Field(validation_alias="INFERENCE_RERANK_BATCH_SIZE")
    inference_rerank_request_timeout_seconds: float = Field(
        validation_alias="INFERENCE_RERANK_REQUEST_TIMEOUT_SECONDS"
    )

    @field_validator("inference_rerank_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Permit only the loopback address the worker runtime may bind."""
        return _require_loopback_host(value, setting_name="INFERENCE_RERANK_HOST")

    @field_validator("inference_rerank_port")
    @classmethod
    def validate_rerank_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="INFERENCE_RERANK_PORT")

    @field_validator("inference_rerank_model_alias")
    @classmethod
    def validate_model_alias(cls, value: str) -> str:
        """Require a short request alias without whitespace or path characters."""
        if _INFERENCE_ALIAS_PATTERN.fullmatch(value) is None:
            raise ValueError("INFERENCE_RERANK_MODEL_ALIAS must be a short ASCII alias")
        return value

    @field_validator("inference_rerank_model_repository", "inference_rerank_artifact_repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Require one ordered public ``namespace/name`` repository identity."""
        if _REPOSITORY_PATTERN.fullmatch(value) is None:
            raise ValueError("rerank repositories must match namespace/name")
        return value

    @field_validator(
        "inference_rerank_model_revision",
        "inference_rerank_artifact_revision",
        "inference_runtime_revision",
    )
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """Require a full lowercase pinned commit identity."""
        return _require_revision(
            value, message="rerank revisions must be 40 lowercase hexadecimal characters"
        )

    @field_validator("inference_rerank_artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        """Require a full lowercase SHA-256 artifact identity."""
        return _require_artifact_sha256(value, setting_name="INFERENCE_RERANK_ARTIFACT_SHA256")

    @field_validator("inference_rerank_artifact_file")
    @classmethod
    def validate_artifact_file(cls, value: str) -> str:
        """Require one GGUF artifact file name without directories."""
        if _GGUF_FILE_PATTERN.fullmatch(value) is None:
            raise ValueError("INFERENCE_RERANK_ARTIFACT_FILE must be a GGUF file name")
        return value

    @field_validator("inference_rerank_artifact_path", mode="before")
    @classmethod
    def validate_artifact_path(cls, value: object) -> object:
        """Require a raw nonblank artifact path before Pydantic coerces it to `Path`."""
        _require_nonblank_text(
            str(value), message="INFERENCE_RERANK_ARTIFACT_PATH must not be blank"
        )
        return value

    @field_validator("inference_rerank_batch_size", mode="before")
    @classmethod
    def validate_rerank_batch_size(cls, value: object) -> int:
        """Require one non-boolean decimal batch geometry inside the serving-context range.

        Runs before Pydantic coercion so a boolean, a fractional string, or a float input is
        rejected instead of silently truncated or coerced to a within-range batch. This single
        value drives both ``--batch-size`` and ``--ubatch-size``.
        """
        return _require_decimal_int(
            value,
            setting_name="INFERENCE_RERANK_BATCH_SIZE",
            minimum=1,
            maximum=8192,
        )

    @field_validator("inference_rerank_request_timeout_seconds", mode="before")
    @classmethod
    def validate_rerank_request_timeout(cls, value: object) -> float:
        """Require one positive finite decimal timeout no greater than 60 seconds.

        Runs before Pydantic coercion so a boolean, a nonnumeric or formatted string, ``NaN``,
        ``Infinity``, or zero is rejected instead of being accepted as a finite in-range timeout.
        A bare integer or a decimal string is accepted because environment values always arrive as
        strings. A trailing unit or surrounding whitespace is not.
        """
        if isinstance(value, bool):
            raise ValueError("INFERENCE_RERANK_REQUEST_TIMEOUT_SECONDS must be a number")
        if isinstance(value, int):
            number = float(value)
        elif isinstance(value, float):
            number = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+(\.[0-9]+)?", value) is not None:
            number = float(value)
        else:
            raise ValueError("INFERENCE_RERANK_REQUEST_TIMEOUT_SECONDS must be a number")
        if not math.isfinite(number) or number <= 0:
            raise ValueError("INFERENCE_RERANK_REQUEST_TIMEOUT_SECONDS must be finite and positive")
        if number > 60:
            raise ValueError("INFERENCE_RERANK_REQUEST_TIMEOUT_SECONDS must not exceed 60")
        return number

    @model_validator(mode="after")
    def validate_artifact_path_matches_file(self) -> Self:
        """Require the artifact path to name the artifact file and batch to fit the serving
        context."""
        if self.inference_rerank_artifact_path.name != self.inference_rerank_artifact_file:
            raise ValueError(
                "INFERENCE_RERANK_ARTIFACT_PATH basename must equal INFERENCE_RERANK_ARTIFACT_FILE"
            )
        if self.inference_rerank_batch_size > self.inference_rerank_context_size:
            raise ValueError(
                "INFERENCE_RERANK_BATCH_SIZE must not exceed INFERENCE_RERANK_CONTEXT_SIZE"
            )
        return self

    @property
    def base_url(self) -> AnyHttpUrl:
        """Derive the sole rerank endpoint base URL from the configured loopback host and port."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(
            f"http://{self.inference_rerank_host}:{self.inference_rerank_port}"
        )

    @property
    def rerank_artifact_url(self) -> AnyHttpUrl:
        """Derive the immutable public resolve URL of the configured quantization artifact."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(
            f"https://huggingface.co/{self.inference_rerank_artifact_repository}"
            f"/resolve/{self.inference_rerank_artifact_revision}"
            f"/{self.inference_rerank_artifact_file}"
        )

    @property
    def rerank_model_identity(self) -> str:
        """Return the immutable source-model plus artifact identity for this cross-encoder."""
        return (
            f"{self.inference_rerank_model_repository}"
            f"@{self.inference_rerank_model_revision}"
            f";artifact={self.inference_rerank_artifact_repository}"
            f"@{self.inference_rerank_artifact_revision}"
            f"/{self.inference_rerank_artifact_file}"
            f";quant={self.inference_rerank_artifact_quant}"
            f";sha256={self.inference_rerank_artifact_sha256}"
        )

    @property
    def rerank_runtime_identity(self) -> str:
        """Return the immutable runtime and serving identity for the rerank endpoint."""
        return (
            f"llama.cpp@{self.inference_runtime_revision}"
            f";alias={self.inference_rerank_model_alias}"
            f";context={self.inference_rerank_context_size}"
            f";host={self.inference_rerank_host}"
            f";port={self.inference_rerank_port}"
            f";embedding=true"
            f";pooling=rank"
            f";reranking=true"
            f";batch={self.inference_rerank_batch_size}"
            f";ubatch={self.inference_rerank_batch_size}"
            f";timeout={self.inference_rerank_request_timeout_seconds:g}"
        )


def load_inference_rerank_settings() -> InferenceRerankSettings:
    """Read cross-encoder rerank settings through the automatic subsystem-file and environment-file
    composition."""
    return InferenceRerankSettings()


class InferenceRerankInputSettings(ConfiguredSettings):
    """Validated pair-text bounds and layout for one label-free cross-encoder input.

    These keys bound and order the query and document text a cross-encoder input may send to the
    operational reranker. They are separate from ``InferenceRerankSettings`` (the
    runtime/serving contract) and ``InferenceSelectionSettings`` (pool depth and final
    candidate ceiling). They are the shared label-free teaching-text construction. The layout is a
    closed teaching-text order and never reads prerequisite text. Loading composes the subsystem
    files under ``config/`` and the device-local ignored ``.env`` automatically. There is no code
    default.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    inference_rerank_query_max_characters: int = Field(
        validation_alias="INFERENCE_RERANK_QUERY_MAX_CHARACTERS"
    )
    inference_rerank_document_max_characters: int = Field(
        validation_alias="INFERENCE_RERANK_DOCUMENT_MAX_CHARACTERS"
    )
    inference_rerank_text_layout: Literal["content-first"] = Field(
        validation_alias="INFERENCE_RERANK_TEXT_LAYOUT"
    )

    @field_validator(
        "inference_rerank_query_max_characters",
        "inference_rerank_document_max_characters",
        mode="before",
    )
    @classmethod
    def validate_rerank_input_limit(cls, value: object, info: ValidationInfo) -> int:
        """Require one non-boolean decimal integer inside the inclusive bound range.

        Runs before Pydantic coercion, so a boolean, a fractional string, or a float input is
        rejected instead of silently truncated or coerced to a within-range integer.
        """
        setting_name = (info.field_name or "").upper()
        return _require_decimal_int(value, setting_name=setting_name, minimum=64, maximum=8192)


def load_inference_rerank_input_settings() -> InferenceRerankInputSettings:
    """Read rerank-input bounds through the automatic subsystem-file and environment-file
    composition."""
    return InferenceRerankInputSettings()


class InferenceControllerSettings(WorkerConnectionSettings):
    """Validated inference-controller state-root and health-attempt policy.

    This adds the inference controller's own repository-relative state root and bounded
    health attempt count on top of the neutral connection base. The derived worker database name
    comes from :class:`AdministrativeDatasetSettings`, so cleanup targets the exact canonical
    session database. All values reach this class through the automatic subsystem-file and
    environment-file composition.
    """

    inference_controller_state_root: Path = Field(
        validation_alias="INFERENCE_CONTROLLER_STATE_ROOT"
    )
    inference_controller_health_attempts: int = Field(
        validation_alias="INFERENCE_CONTROLLER_HEALTH_ATTEMPTS", ge=1, le=600
    )

    @field_validator("inference_controller_state_root", mode="before")
    @classmethod
    def validate_state_root(cls, value: object) -> object:
        """Require one nonblank repository-relative state root without dot or cmd-expand
        characters."""
        return _require_relative_path(
            value, setting_name="INFERENCE_CONTROLLER_STATE_ROOT", reject_percent=True
        )


def load_inference_controller_settings() -> InferenceControllerSettings:
    """Read inference-controller settings through the automatic subsystem-file and environment-file
    composition."""
    return InferenceControllerSettings()
