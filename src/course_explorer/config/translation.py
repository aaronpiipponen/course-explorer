"""Validated settings for the offline translation runtime and its remote controller."""

from pathlib import Path
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    _ANY_HTTP_URL_ADAPTER,
    _GGUF_FILE_PATTERN,
    _GPU_LAYERS_PATTERN,
    _REPOSITORY_PATTERN,
    ConfiguredSettings,
    _require_artifact_sha256,
    _require_loopback_host,
    _require_relative_path,
    _require_revision,
    _require_tcp_port,
)
from course_explorer.config.worker import WorkerConnectionSettings
from course_explorer.validation import (
    require_nonblank_text as _require_nonblank_text,
)


class TranslationSettings(ConfiguredSettings):
    """Validated loopback contract for the one pinned offline translation runtime.

    The artifact has two valid shapes. The three ``TRANSLATION_ARTIFACT_REPOSITORY``,
    ``TRANSLATION_ARTIFACT_REVISION`` and ``TRANSLATION_ARTIFACT_FILE`` keys name a published GGUF,
    and the artifact URL derives from them. All three keys are absent when the artifact is a local
    conversion, which the runtime serves from its configured path and digest alone. Deployment,
    model, runtime, and request values stay out of provider and worker mechanics. The request
    alias, immutable model/runtime identities, and provenance model string are all derived or
    validated here so untrusted model output is bound to one known configuration.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    translation_host: str = Field(validation_alias="TRANSLATION_HOST")
    translation_port: int = Field(validation_alias="TRANSLATION_PORT")
    translation_model_alias: str = Field(validation_alias="TRANSLATION_MODEL_ALIAS")
    translation_model_repository: str = Field(validation_alias="TRANSLATION_MODEL_REPOSITORY")
    translation_model_revision: str = Field(validation_alias="TRANSLATION_MODEL_REVISION")
    translation_artifact_repository: str | None = Field(
        default=None, validation_alias="TRANSLATION_ARTIFACT_REPOSITORY"
    )
    translation_artifact_revision: str | None = Field(
        default=None, validation_alias="TRANSLATION_ARTIFACT_REVISION"
    )
    translation_artifact_file: str | None = Field(
        default=None, validation_alias="TRANSLATION_ARTIFACT_FILE"
    )
    translation_artifact_path: Path = Field(validation_alias="TRANSLATION_ARTIFACT_PATH")
    translation_artifact_sha256: str = Field(validation_alias="TRANSLATION_ARTIFACT_SHA256")
    translation_artifact_quant: Literal["Q5_K_M", "Q4_K_M", "Q2_K"] = Field(
        validation_alias="TRANSLATION_ARTIFACT_QUANT"
    )
    translation_runtime_revision: str = Field(validation_alias="TRANSLATION_RUNTIME_REVISION")
    translation_context_size: int = Field(validation_alias="TRANSLATION_CONTEXT_SIZE")
    translation_parallel: int = Field(validation_alias="TRANSLATION_PARALLEL")
    translation_gpu_layers: str = Field(validation_alias="TRANSLATION_GPU_LAYERS")
    translation_request_timeout_seconds: float = Field(
        validation_alias="TRANSLATION_REQUEST_TIMEOUT_SECONDS",
        gt=0,
        le=600,
        allow_inf_nan=False,
    )
    translation_max_attempts: int = Field(validation_alias="TRANSLATION_MAX_ATTEMPTS", ge=1, le=5)
    translation_retry_backoff_seconds: float = Field(
        validation_alias="TRANSLATION_RETRY_BACKOFF_SECONDS", ge=0, le=10, allow_inf_nan=False
    )
    translation_max_tokens: int = Field(validation_alias="TRANSLATION_MAX_TOKENS")
    translation_temperature: float = Field(
        validation_alias="TRANSLATION_TEMPERATURE", ge=0, le=2, allow_inf_nan=False
    )

    @field_validator("translation_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Permit only the loopback address the local runtime may bind."""
        return _require_loopback_host(value, setting_name="TRANSLATION_HOST")

    @field_validator("translation_port")
    @classmethod
    def validate_translation_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="TRANSLATION_PORT")

    @field_validator("translation_model_alias", "translation_model_repository")
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        """Require meaningful runtime and model identities without normalizing them."""
        return _require_nonblank_text(
            value, message="translation model alias and repository must not be blank"
        )

    @field_validator("translation_model_revision", "translation_runtime_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """Require a full lowercase pinned commit identity."""
        return _require_revision(
            value,
            message=(
                "translation model and runtime revisions must be 40 lowercase hexadecimal characters"
            ),
        )

    @field_validator("translation_artifact_path", mode="before")
    @classmethod
    def validate_artifact_path(cls, value: object) -> object:
        """Require a raw nonblank artifact path before Pydantic coerces it to `Path`."""
        _require_nonblank_text(str(value), message="TRANSLATION_ARTIFACT_PATH must not be blank")
        return value

    @field_validator("translation_artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        """Require a full lowercase SHA-256 identity."""
        return _require_artifact_sha256(value, setting_name="TRANSLATION_ARTIFACT_SHA256")

    @field_validator("translation_artifact_repository")
    @classmethod
    def validate_artifact_repository(cls, value: str | None) -> str | None:
        """Require one public ``namespace/name`` repository identity when a source is configured."""
        if value is not None and _REPOSITORY_PATTERN.fullmatch(value) is None:
            raise ValueError("TRANSLATION_ARTIFACT_REPOSITORY must match namespace/name")
        return value

    @field_validator("translation_artifact_revision")
    @classmethod
    def validate_artifact_revision(cls, value: str | None) -> str | None:
        """Require a full lowercase pinned commit identity when a source is configured."""
        if value is None:
            return None
        return _require_revision(
            value,
            message="TRANSLATION_ARTIFACT_REVISION must be 40 lowercase hexadecimal characters",
        )

    @field_validator("translation_artifact_file")
    @classmethod
    def validate_artifact_file(cls, value: str | None) -> str | None:
        """Require one GGUF file name without directories when a source is configured."""
        if value is not None and _GGUF_FILE_PATTERN.fullmatch(value) is None:
            raise ValueError("TRANSLATION_ARTIFACT_FILE must be a GGUF file name")
        return value

    @field_validator("translation_context_size", "translation_parallel", "translation_max_tokens")
    @classmethod
    def require_positive_integer(cls, value: int) -> int:
        """Require positive runtime and output counts."""
        if value <= 0:
            raise ValueError("translation context, parallel, and max tokens must be positive")
        return value

    @field_validator("translation_gpu_layers")
    @classmethod
    def validate_gpu_layers(cls, value: str) -> str:
        """Require full offload or one nonnegative layer count."""
        if _GPU_LAYERS_PATTERN.fullmatch(value) is None:
            raise ValueError("TRANSLATION_GPU_LAYERS must be 'all' or a nonnegative integer")
        return value

    @property
    def base_url(self) -> AnyHttpUrl:
        """Derive the sole provider base URL from configured loopback host and port."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(
            f"http://{self.translation_host}:{self.translation_port}"
        )

    @property
    def provenance_model(self) -> str:
        """Return the immutable model identity persisted on derived translation rows.

        The identity is the source model repository and revision, the quantization and the
        artifact digest. The published artifact source keys do not enter it, so two conversions of
        one model at one quantization and digest share one identity.
        """
        return (
            f"{self.translation_model_repository}@{self.translation_model_revision}"
            f";{self.translation_artifact_quant};sha256:{self.translation_artifact_sha256}"
        )

    @property
    def translation_artifact_url(self) -> AnyHttpUrl | None:
        """Derive the published artifact URL, or ``None`` for a local conversion."""
        if self.translation_artifact_repository is None:
            return None
        return _ANY_HTTP_URL_ADAPTER.validate_python(
            f"https://huggingface.co/{self.translation_artifact_repository}"
            f"/resolve/{self.translation_artifact_revision}"
            f"/{self.translation_artifact_file}"
        )

    @model_validator(mode="after")
    def validate_artifact_source(self) -> Self:
        """Require the three source keys together and an artifact path that names its file."""
        source = (
            self.translation_artifact_repository,
            self.translation_artifact_revision,
            self.translation_artifact_file,
        )
        present = [value is not None for value in source]
        if any(present) and not all(present):
            raise ValueError(
                "TRANSLATION_ARTIFACT_REPOSITORY, TRANSLATION_ARTIFACT_REVISION and "
                "TRANSLATION_ARTIFACT_FILE must be set together"
            )
        if (
            self.translation_artifact_file is not None
            and self.translation_artifact_path.name != self.translation_artifact_file
        ):
            raise ValueError(
                "TRANSLATION_ARTIFACT_PATH basename must equal TRANSLATION_ARTIFACT_FILE"
            )
        return self

    @model_validator(mode="after")
    def validate_max_tokens_below_context(self) -> Self:
        """Require the output cap to stay strictly below the served text context size."""
        if self.translation_max_tokens >= self.translation_context_size:
            raise ValueError("TRANSLATION_MAX_TOKENS must be smaller than TRANSLATION_CONTEXT_SIZE")
        return self


def load_translation_settings() -> TranslationSettings:
    """Read translation settings through the automatic subsystem-file and environment-file
    composition."""
    return TranslationSettings()


class TranslationControllerSettings(WorkerConnectionSettings):
    """Validated remote-controller state-root and health-attempt policy.

    This adds the translation controller's own local state root and health attempt count on top
    of the neutral connection base. The derived worker database name comes from
    :class:`AdministrativeDatasetSettings`, so cleanup targets the exact session database. All
    values reach this class through the automatic subsystem-file and environment-file composition.
    """

    translation_controller_state_root: Path = Field(
        validation_alias="TRANSLATION_CONTROLLER_STATE_ROOT"
    )
    translation_controller_health_attempts: int = Field(
        validation_alias="TRANSLATION_CONTROLLER_HEALTH_ATTEMPTS", ge=1, le=600
    )

    @field_validator("translation_controller_state_root", mode="before")
    @classmethod
    def validate_state_root(cls, value: object) -> object:
        """Require one nonblank repository-relative state root without dot or cmd-expand
        characters."""
        return _require_relative_path(
            value, setting_name="TRANSLATION_CONTROLLER_STATE_ROOT", reject_percent=True
        )


def load_translation_controller_settings() -> TranslationControllerSettings:
    """Read translation-controller settings through the automatic subsystem-file and
    environment-file composition."""
    return TranslationControllerSettings()
