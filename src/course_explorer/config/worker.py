"""Validated settings for the worker runtime, its checkpoint reboot, GPU, connection and
setup."""

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    ConfiguredSettings,
    _require_decimal_int,
)
from course_explorer.validation import (
    require_nonblank_text as _require_nonblank_text,
)


class WorkerRuntimeSettings(ConfiguredSettings):
    """The portable generation CPU geometry shared by every local llama.cpp generation launch.

    These two values are the measured launcher geometry (`-t`/`--threads-batch`) recorded as
    execution provenance. They never gate scheduling, invalidate retained work, or appear in the
    cross-encoder runtime identity. They are device-independent, so they live in
    ``config/worker.toml`` rather than the device-local ignored ``.env``.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    worker_runtime_threads: int = Field(validation_alias="WORKER_RUNTIME_THREADS")
    worker_runtime_threads_batch: int = Field(validation_alias="WORKER_RUNTIME_THREADS_BATCH")

    @field_validator("worker_runtime_threads", "worker_runtime_threads_batch", mode="before")
    @classmethod
    def validate_runtime_threads(cls, value: object, info: ValidationInfo) -> int:
        """Require one non-boolean decimal thread count inside the inclusive serving bound.

        Runs before Pydantic coercion, so a boolean, a fractional string, or a float input is
        rejected instead of silently truncated. An environment value is a decimal string.
        """
        setting_name = (info.field_name or "").upper()
        return _require_decimal_int(value, setting_name=setting_name, minimum=1, maximum=256)


class WorkerGpuRebootSettings(ConfiguredSettings):
    """The one validated portable checkpoint-reboot toggle and interval shared by every worker GPU
    mode.

    ``WORKER_GPU_REBOOT_ENABLED`` turns the periodic restart on. A worker GPU mode (translation
    generation and inference cross-encoder scoring and whole-target generation) stops at a
    completed atomic unit or a committed failed generation attempt once its loaded-GPU time
    reaches ``WORKER_GPU_REBOOT_INTERVAL_MINUTES``, and restarts the worker host. The values are
    device-independent policy owned by ``config/worker.toml``, and every worker GPU mode uses
    them. Loading composes the subsystem files under ``config/`` and the device-local ignored
    ``.env`` automatically. There is no code default.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    worker_gpu_reboot_enabled: bool = Field(validation_alias="WORKER_GPU_REBOOT_ENABLED")
    worker_gpu_reboot_interval_minutes: int = Field(
        validation_alias="WORKER_GPU_REBOOT_INTERVAL_MINUTES", ge=1
    )


def load_worker_gpu_reboot_settings() -> WorkerGpuRebootSettings:
    """Read the shared checkpoint-reboot toggle and interval through the automatic subsystem-file
    and environment-file composition."""
    return WorkerGpuRebootSettings()


_AMD_GPU_TARGET_PATTERN = re.compile(r"gfx[0-9a-f]{1,4}")
_CUDA_GPU_TARGET_PATTERN = re.compile(r"[0-9]{2,3}")


class WorkerGpuSettings(ConfiguredSettings):
    """Validated worker GPU backend, build target and model-card index.

    ``WORKER_GPU_BACKEND`` selects the llama.cpp build and the model child's device variable.
    ``WORKER_GPU_TARGET`` is a ROCm compute target such as ``gfx1201`` for ``rocm`` and a CUDA
    compute capability without the dot such as ``86`` for ``cuda``. ``WORKER_GPU_DEVICE_INDEX`` is
    the vendor device index the model servers use. These are non-secret worker machine identity
    read from ``config/worker.toml`` through the automatic subsystem-file and environment-file
    composition.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    gpu_backend: Literal["rocm", "cuda"] = Field(validation_alias="WORKER_GPU_BACKEND")
    gpu_target: str = Field(validation_alias="WORKER_GPU_TARGET")
    gpu_device_index: int = Field(validation_alias="WORKER_GPU_DEVICE_INDEX", ge=0, le=15)

    @model_validator(mode="after")
    def validate_gpu_target(self) -> Self:
        """Require the target form the configured backend builds for."""
        if self.gpu_backend == "rocm":
            if _AMD_GPU_TARGET_PATTERN.fullmatch(self.gpu_target) is None:
                raise ValueError("WORKER_GPU_TARGET must be a ROCm compute target such as gfx1201")
        elif _CUDA_GPU_TARGET_PATTERN.fullmatch(self.gpu_target) is None:
            raise ValueError("WORKER_GPU_TARGET must be a CUDA compute capability such as 86")
        return self


def load_worker_gpu_settings() -> WorkerGpuSettings:
    """Read the worker GPU settings through the automatic subsystem-file and environment-file
    composition."""
    return WorkerGpuSettings()


_WORKER_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class WorkerConnectionSettings(ConfiguredSettings):
    """Validated worker host kind, neutral worker transport and lifecycle-bounding policy.

    ``WORKER_HOST_KIND`` selects the transport: ``wsl`` for a Windows host that runs the stage
    inside a WSL distribution, ``linux`` for a native Linux host reached over SSH, and
    ``same-machine`` for a worker container of this instance's own stack, reached with no SSH hop.
    ``WORKER_SSH_TARGET``, ``WORKER_WSL_DISTRIBUTION`` and ``WORKER_PROJECT_ROOT`` are non-secret
    worker machine identity read from ``config/worker.toml``, together with the connect timeout. A
    ``wsl`` or ``linux`` worker requires ``WORKER_SSH_TARGET``, and a ``same-machine`` worker refuses
    it. A ``wsl`` worker requires ``WORKER_WSL_DISTRIBUTION`` and the other kinds refuse it. A
    ``same-machine`` worker's ``WORKER_PROJECT_ROOT`` is the path inside its container. The health
    interval and stop timeout are shared wait values that are assigned once, in
    ``config/translation.toml``. All reach this class through the automatic subsystem-file and
    environment-file composition.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    host_kind: Literal["wsl", "linux", "same-machine"] = Field(validation_alias="WORKER_HOST_KIND")
    ssh_target: str | None = Field(default=None, validation_alias="WORKER_SSH_TARGET")
    ssh_connect_timeout_seconds: int = Field(
        validation_alias="WORKER_SSH_CONNECT_TIMEOUT_SECONDS", ge=1, le=120
    )
    wsl_distribution: str | None = Field(default=None, validation_alias="WORKER_WSL_DISTRIBUTION")
    worker_project_root: Path = Field(validation_alias="WORKER_PROJECT_ROOT")
    poll_interval_seconds: float = Field(
        validation_alias="WORKER_HEALTH_INTERVAL_SECONDS", gt=0, le=30, allow_inf_nan=False
    )
    stop_timeout_seconds: float = Field(
        validation_alias="WORKER_STOP_TIMEOUT_SECONDS", gt=0, le=3600, allow_inf_nan=False
    )

    @field_validator("ssh_target")
    @classmethod
    def validate_ssh_target(cls, value: str | None) -> str | None:
        """Require a short ASCII SSH alias when one is configured."""
        if value is None:
            return None
        if _WORKER_IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "WORKER_SSH_TARGET must be 1-64 ASCII letters, digits, underscore, dot, or hyphen"
            )
        return value

    @field_validator("wsl_distribution")
    @classmethod
    def validate_wsl_distribution(cls, value: str | None) -> str | None:
        """Require a short ASCII WSL distribution name when one is configured."""
        if value is None:
            return None
        if _WORKER_IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "WORKER_WSL_DISTRIBUTION must be 1-64 ASCII letters, digits, underscore, "
                "dot, or hyphen"
            )
        return value

    @field_validator("worker_project_root")
    @classmethod
    def validate_worker_project_root(cls, value: Path) -> Path:
        """Require an absolute POSIX worker checkout path without dot components."""
        raw = str(value)
        if (
            not raw.strip()
            or not value.is_absolute()
            or any(part in {".", ".."} for part in value.parts)
        ):
            raise ValueError("WORKER_PROJECT_ROOT must be an absolute path without dot components")
        return value

    @model_validator(mode="after")
    def validate_host_kind(self) -> Self:
        """Require each host kind's own identity and refuse another kind's values."""
        if self.host_kind == "wsl":
            if self.ssh_target is None:
                raise ValueError("a WSL worker requires WORKER_SSH_TARGET")
            if self.wsl_distribution is None:
                raise ValueError("a WSL worker requires WORKER_WSL_DISTRIBUTION")
            if "%" in str(self.worker_project_root):
                raise ValueError(
                    "a WSL worker requires WORKER_PROJECT_ROOT without percent characters"
                )
            return self
        if self.host_kind == "linux" and self.ssh_target is None:
            raise ValueError("a Linux worker requires WORKER_SSH_TARGET")
        if self.host_kind == "same-machine" and self.ssh_target is not None:
            raise ValueError("a same-machine worker refuses WORKER_SSH_TARGET")
        if self.wsl_distribution is not None:
            raise ValueError("WORKER_WSL_DISTRIBUTION belongs to a WSL worker")
        return self


def load_worker_connection_settings() -> WorkerConnectionSettings:
    """Read the neutral worker connection settings through the automatic subsystem-file and
    environment-file composition."""
    return WorkerConnectionSettings()


class WorkerSetupSettings(ConfiguredSettings):
    """Validated explicit worker setup/update identity and bound.

    Two portable values drive explicit worker setup/update: the GitHub origin URL the worker
    checkout must match, and one long setup timeout that a real install/download/build may use
    without borrowing the short model-request or control timeout. Loading composes the
    subsystem files under ``config/`` and the device-local ignored ``.env`` automatically.
    There is no code default.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    worker_setup_project_url: str = Field(validation_alias="WORKER_SETUP_PROJECT_URL")
    worker_setup_timeout_seconds: float = Field(
        validation_alias="WORKER_SETUP_TIMEOUT_SECONDS", gt=0, allow_inf_nan=False
    )

    @field_validator("worker_setup_project_url")
    @classmethod
    def validate_project_url(cls, value: str) -> str:
        """Require one nonblank configured origin URL without normalizing it."""
        return _require_nonblank_text(value, message="WORKER_SETUP_PROJECT_URL must not be blank")


def load_worker_setup_settings() -> WorkerSetupSettings:
    """Read explicit worker setup/update settings through the automatic subsystem-file and
    environment-file composition."""
    return WorkerSetupSettings()
