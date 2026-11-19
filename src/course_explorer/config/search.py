"""Validated settings for search, comparison, recommendation, the embedding runtime and
the local stage lifecycle."""

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    _ANY_HTTP_URL_ADAPTER,
    ConfiguredSettings,
    _require_artifact_sha256,
    _require_loopback_host,
    _require_relative_path,
    _require_revision,
    _require_tcp_port,
)
from course_explorer.config.web import WebSettings
from course_explorer.constants import COURSE_EMBEDDING_DIMENSION
from course_explorer.validation import (
    require_nonblank_text as _require_nonblank_text,
)


class LocalLifecycleSettings(ConfiguredSettings):
    """Validated state locations and cooperative lifecycle policy for the local admin stages.

    These values describe where the two local stage state roots live and how every
    local detached-worker startup, polling, and stop acknowledgement is bounded. They are portable
    policy: a different device keeps the same repository-relative state roots and the same timing
    bounds, so the roots live in ``config/curriculum.toml``/``config/search.toml`` and the shared
    timing keys live in ``config/worker.toml``. Loading composes the subsystem files under
    ``config/`` and the device-local ignored ``.env`` automatically. There is no code default.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    ingestion_state_root: Path = Field(validation_alias="INGESTION_CONTROLLER_STATE_ROOT")
    embedding_state_root: Path = Field(validation_alias="EMBEDDING_CONTROLLER_STATE_ROOT")
    startup_attempts: int = Field(validation_alias="ADMIN_STARTUP_ATTEMPTS", ge=1, le=600)
    poll_interval_seconds: float = Field(
        validation_alias="ADMIN_POLL_INTERVAL_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    stop_timeout_seconds: float = Field(
        validation_alias="ADMIN_STOP_TIMEOUT_SECONDS",
        gt=0,
        le=3600,
        allow_inf_nan=False,
    )

    @field_validator("ingestion_state_root", mode="before")
    @classmethod
    def validate_ingestion_state_root(cls, value: object) -> object:
        """Require one nonblank repository-relative state root without dot or cmd-expand
        characters."""
        return _require_relative_path(
            value, setting_name="INGESTION_CONTROLLER_STATE_ROOT", reject_percent=True
        )

    @field_validator("embedding_state_root", mode="before")
    @classmethod
    def validate_embedding_state_root(cls, value: object) -> object:
        """Require one nonblank repository-relative state root without dot or cmd-expand
        characters."""
        return _require_relative_path(
            value, setting_name="EMBEDDING_CONTROLLER_STATE_ROOT", reject_percent=True
        )


def load_local_lifecycle_settings() -> LocalLifecycleSettings:
    """Read local lifecycle settings through the automatic subsystem-file and environment-file
    composition."""
    return LocalLifecycleSettings()


_EMBEDDING_IMAGE_PATTERN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


class EmbeddingRuntimeSettings(ConfiguredSettings):
    """Validated loopback contract for the one pinned CPU embedding runtime and its generation.

    The one generation lifecycle, the Compose bridge, the similarity consumers and the web process
    read this family. Every similarity read takes its hub pair from the opened serving artifact set,
    which reads the family's configured embedding directory. Every consumer reads the same subsystem
    files and environment file through one settings owner.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    embedding_host: str = Field(validation_alias="EMBEDDING_HOST")
    embedding_port: int = Field(validation_alias="EMBEDDING_PORT")
    embedding_model: str = Field(validation_alias="EMBEDDING_MODEL")
    embedding_dimension: Literal[COURSE_EMBEDDING_DIMENSION] = COURSE_EMBEDDING_DIMENSION
    embedding_pooling: Literal["cls", "mean", "last"] = Field(validation_alias="EMBEDDING_POOLING")
    embedding_normalization: int = Field(validation_alias="EMBEDDING_NORMALIZATION", ge=-1)
    embedding_threads: int = Field(validation_alias="EMBEDDING_THREADS")
    embedding_context_size: int = Field(validation_alias="EMBEDDING_CONTEXT_SIZE")
    embedding_request_timeout_seconds: float = Field(
        validation_alias="EMBEDDING_REQUEST_TIMEOUT_SECONDS", gt=0, le=600, allow_inf_nan=False
    )
    embedding_max_attempts: int = Field(validation_alias="EMBEDDING_MAX_ATTEMPTS", ge=1, le=5)
    embedding_retry_backoff_seconds: float = Field(
        validation_alias="EMBEDDING_RETRY_BACKOFF_SECONDS", ge=0, le=10, allow_inf_nan=False
    )
    embedding_generation_batch_size: int = Field(
        validation_alias="EMBEDDING_GENERATION_BATCH_SIZE", ge=1, le=128
    )
    embedding_chunk_max_characters: int = Field(
        validation_alias="EMBEDDING_CHUNK_MAX_CHARACTERS", ge=2, le=8192
    )
    embedding_chunk_overlap_characters: int = Field(
        validation_alias="EMBEDDING_CHUNK_OVERLAP_CHARACTERS", ge=0
    )
    embedding_artifact_url: AnyHttpUrl = Field(validation_alias="EMBEDDING_ARTIFACT_URL")
    embedding_artifact_path: Path = Field(validation_alias="EMBEDDING_ARTIFACT_PATH")
    embedding_artifact_sha256: str = Field(validation_alias="EMBEDDING_ARTIFACT_SHA256")
    embedding_server_image: str = Field(validation_alias="EMBEDDING_SERVER_IMAGE")
    embedding_runtime_revision: str = Field(validation_alias="EMBEDDING_RUNTIME_REVISION")

    @field_validator("embedding_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Permit only the loopback address the local service may bind."""
        return _require_loopback_host(value, setting_name="EMBEDDING_HOST")

    @field_validator("embedding_port")
    @classmethod
    def validate_embedding_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="EMBEDDING_PORT")

    @field_validator("embedding_model")
    @classmethod
    def reject_blank_model(cls, value: str) -> str:
        """Require a meaningful model identity without normalizing it."""
        return _require_nonblank_text(value, message="EMBEDDING_MODEL must not be blank")

    @field_validator("embedding_threads", "embedding_context_size")
    @classmethod
    def require_positive_integer(cls, value: int) -> int:
        """Require positive thread and context-size counts."""
        if value <= 0:
            raise ValueError("embedding threads and context size must be positive")
        return value

    @field_validator("embedding_artifact_url")
    @classmethod
    def validate_artifact_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require a credential-free HTTPS artifact location without decorations."""
        if (
            value.scheme != "https"
            or value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "EMBEDDING_ARTIFACT_URL must be HTTPS without credentials, query, or fragment"
            )
        return value

    @field_validator("embedding_artifact_path", mode="before")
    @classmethod
    def validate_artifact_path(cls, value: object) -> object:
        """Require a raw nonblank artifact path before Pydantic coerces it to ``Path``."""
        _require_nonblank_text(str(value), message="EMBEDDING_ARTIFACT_PATH must not be blank")
        return value

    @field_validator("embedding_artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        """Require a full lowercase SHA-256 identity."""
        return _require_artifact_sha256(value, setting_name="EMBEDDING_ARTIFACT_SHA256")

    @field_validator("embedding_runtime_revision")
    @classmethod
    def validate_runtime_revision(cls, value: str) -> str:
        """Require a full lowercase pinned runtime commit identity."""
        return _require_revision(
            value, message="EMBEDDING_RUNTIME_REVISION must be 40 lowercase hexadecimal characters"
        )

    @field_validator("embedding_server_image")
    @classmethod
    def validate_server_image(cls, value: str) -> str:
        """Require an immutable digest-pinned OCI reference."""
        if _EMBEDDING_IMAGE_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "EMBEDDING_SERVER_IMAGE must be an OCI reference pinned by @sha256 digest"
            )
        return value

    @property
    def base_url(self) -> AnyHttpUrl:
        """Derive the sole provider base URL from configured loopback host and port."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(
            f"http://{self.embedding_host}:{self.embedding_port}"
        )

    @model_validator(mode="after")
    def validate_chunk_overlap_bound(self) -> Self:
        """Require deterministic forward chunk progress (overlap below the window)."""
        if self.embedding_chunk_overlap_characters >= self.embedding_chunk_max_characters:
            raise ValueError(
                "EMBEDDING_CHUNK_OVERLAP_CHARACTERS must be smaller than "
                "EMBEDDING_CHUNK_MAX_CHARACTERS"
            )
        return self


def load_embedding_runtime_settings() -> EmbeddingRuntimeSettings:
    """Read the runtime and generation embedding fields from the configured sources."""
    return EmbeddingRuntimeSettings()


def embedding_configuration_for_site(web: WebSettings) -> EmbeddingRuntimeSettings | None:
    """Resolve the site's embedding configuration from its serving switch.

    This is the one owner of the switch-to-configuration policy for the serving entry points: each
    resolves the mode once through it. With the switch off it returns ``None`` and never loads or
    validates the embedding family itself. An already resolved web settings object is passed in so
    the switch is read once per entry point.
    """
    if not web.embeddings_enabled:
        return None
    return load_embedding_runtime_settings()


# The four search methods a query matches a course by: exact code, substring, fuzzy search and
# semantic. Semantic runs only on a site that serves the embedding features.
SearchMethod = Literal["exact_code", "substring", "fuzzy_name", "semantic"]


_SEARCH_METHODS: tuple[SearchMethod, ...] = (
    "exact_code",
    "substring",
    "fuzzy_name",
    "semantic",
)


class SearchSettings(ConfiguredSettings):
    """Validated search, query-embedding request, comparison, recommendation, and route policy."""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    semantic_search_min_score: float = Field(
        validation_alias="SEMANTIC_SEARCH_MIN_SCORE",
        ge=-1,
        le=1,
        allow_inf_nan=False,
    )
    fuzzy_search_min_score: float = Field(
        validation_alias="FUZZY_SEARCH_MIN_SCORE",
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    fuzzy_token_match_weight: float = Field(
        validation_alias="FUZZY_TOKEN_MATCH_WEIGHT", gt=0, le=1, allow_inf_nan=False
    )
    fuzzy_score_tie_break_weight: float = Field(
        validation_alias="FUZZY_SCORE_TIE_BREAK_WEIGHT", ge=0, le=1, allow_inf_nan=False
    )
    search_embedding_request_timeout_seconds: float = Field(
        validation_alias="SEARCH_EMBEDDING_REQUEST_TIMEOUT_SECONDS",
        gt=0,
        allow_inf_nan=False,
    )
    search_embedding_max_attempts: int = Field(
        validation_alias="SEARCH_EMBEDDING_MAX_ATTEMPTS", ge=1
    )
    search_tier_priority: tuple[SearchMethod, ...] = Field(validation_alias="SEARCH_TIER_PRIORITY")
    comparison_name_weight: float = Field(
        validation_alias="COMPARISON_NAME_WEIGHT", gt=0, allow_inf_nan=False
    )
    comparison_content_weight: float = Field(
        validation_alias="COMPARISON_CONTENT_WEIGHT", gt=0, allow_inf_nan=False
    )
    comparison_outcomes_weight: float = Field(
        validation_alias="COMPARISON_OUTCOMES_WEIGHT", gt=0, allow_inf_nan=False
    )
    comparison_overlap_floor: float = Field(
        validation_alias="COMPARISON_OVERLAP_FLOOR", ge=0, le=1, allow_inf_nan=False
    )
    comparison_overlap_ceiling: float = Field(
        validation_alias="COMPARISON_OVERLAP_CEILING", ge=0, le=1, allow_inf_nan=False
    )
    comparison_heavy_overlap_percent: float = Field(
        validation_alias="COMPARISON_HEAVY_OVERLAP_PERCENT", ge=0, le=100, allow_inf_nan=False
    )
    comparison_partial_overlap_percent: float = Field(
        validation_alias="COMPARISON_PARTIAL_OVERLAP_PERCENT", ge=0, le=100, allow_inf_nan=False
    )
    comparison_same_area_percent: float = Field(
        validation_alias="COMPARISON_SAME_AREA_PERCENT", ge=0, le=100, allow_inf_nan=False
    )
    comparison_loose_related_percent: float = Field(
        validation_alias="COMPARISON_LOOSE_RELATED_PERCENT", ge=0, le=100, allow_inf_nan=False
    )
    prerequisite_path_max_compact_nodes: int = Field(
        validation_alias="PREREQUISITE_PATH_MAX_COMPACT_NODES", gt=0
    )
    assumed_course_credits: Decimal = Field(
        validation_alias="ASSUMED_COURSE_CREDITS",
        gt=0,
        max_digits=6,
        decimal_places=2,
    )
    recommendation_chunk_top_k: int = Field(validation_alias="RECOMMENDATION_CHUNK_TOP_K", gt=0)
    recommendation_min_relatedness: float = Field(
        validation_alias="RECOMMENDATION_MIN_RELATEDNESS",
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    recommendation_relatedness_ceiling: float = Field(
        validation_alias="RECOMMENDATION_RELATEDNESS_CEILING",
        gt=0,
        le=1,
        allow_inf_nan=False,
    )
    recommendation_near_duplicate_percent: float = Field(
        validation_alias="RECOMMENDATION_NEAR_DUPLICATE_PERCENT",
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    recommendation_overlap_penalty_weight: float = Field(
        validation_alias="RECOMMENDATION_OVERLAP_PENALTY_WEIGHT",
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    search_hnsw_iterative_scan: Literal["relaxed_order", "strict_order"] = Field(
        validation_alias="SEARCH_HNSW_ITERATIVE_SCAN"
    )
    search_hnsw_max_scan_tuples: int = Field(validation_alias="SEARCH_HNSW_MAX_SCAN_TUPLES", gt=0)
    search_neighbor_table_rows_per_chunk: int = Field(
        validation_alias="SEARCH_NEIGHBOR_TABLE_ROWS_PER_CHUNK", gt=0
    )

    @field_validator("search_tier_priority")
    @classmethod
    def validate_search_tier_priority(
        cls, value: tuple[SearchMethod, ...]
    ) -> tuple[SearchMethod, ...]:
        """Require one occurrence of every match method in the config-owned primary-tier order."""
        if len(value) != len(_SEARCH_METHODS) or set(value) != set(_SEARCH_METHODS):
            raise ValueError(
                "SEARCH_TIER_PRIORITY must contain each of exact_code, substring, fuzzy_name, "
                "and semantic exactly once"
            )
        return value

    @model_validator(mode="after")
    def validate_recommendation_scale(self) -> Self:
        """Require the displayed relatedness ceiling to sit above the recommendation floor."""
        if self.recommendation_relatedness_ceiling <= self.recommendation_min_relatedness:
            raise ValueError(
                "RECOMMENDATION_RELATEDNESS_CEILING must exceed RECOMMENDATION_MIN_RELATEDNESS"
            )
        return self

    @model_validator(mode="after")
    def validate_comparison_threshold_order(self) -> Self:
        """Require the partial and loose thresholds to stay below their upper neighbours."""
        if self.comparison_overlap_floor >= self.comparison_overlap_ceiling:
            raise ValueError("COMPARISON_OVERLAP_FLOOR must be below COMPARISON_OVERLAP_CEILING")
        if self.comparison_partial_overlap_percent >= self.comparison_heavy_overlap_percent:
            raise ValueError(
                "COMPARISON_PARTIAL_OVERLAP_PERCENT must be below COMPARISON_HEAVY_OVERLAP_PERCENT"
            )
        if self.comparison_loose_related_percent >= self.comparison_same_area_percent:
            raise ValueError(
                "COMPARISON_LOOSE_RELATED_PERCENT must be below COMPARISON_SAME_AREA_PERCENT"
            )
        return self


def load_search_settings() -> SearchSettings:
    """Read search settings through the automatic subsystem-file and environment-file
    composition."""
    return SearchSettings()
