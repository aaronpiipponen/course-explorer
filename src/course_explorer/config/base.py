"""The configured settings base, the shared validators its families use and the
settings-source markers."""

import os
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from course_explorer import administration
from course_explorer.validation import require_sha256

_ANY_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


def _require_nonblank_secret(value: SecretStr, *, message: str) -> SecretStr:
    """Reject a blank secret while keeping it wrapped and never rendering it."""
    if not value.get_secret_value().strip():
        raise ValueError(message)
    return value


def _require_tcp_port(value: int, *, setting_name: str) -> int:
    """Require a valid inclusive TCP port."""
    if not 1 <= value <= 65535:
        raise ValueError(f"{setting_name} must be between 1 and 65535")
    return value


def _require_loopback_host(value: str, *, setting_name: str) -> str:
    """Permit only the loopback address a local service may bind."""
    if value != "127.0.0.1":
        raise ValueError(f"{setting_name} must be 127.0.0.1")
    return value


# The hostnames a plain-HTTP origin may name. Every other origin requires HTTPS.
_LOOPBACK_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_exact_origin(
    value: str, *, setting_name: str, allow_loopback_http: bool = False
) -> str:
    """Require one exact origin without credentials, path, query or fragment.

    HTTPS is the only scheme unless ``allow_loopback_http`` admits plain HTTP for a loopback
    hostname. The value is kept as it is spelled, so a request comparison reads one exact string. A
    wildcard, whitespace, an empty host, user information, a port outside the TCP range and every
    URL decoration each fail the read. An invalid port raises the parser's own failure, which
    ``hide_input_in_errors`` keeps free of the value.
    """
    if not value or any(character.isspace() for character in value) or "*" in value:
        raise ValueError(f"{setting_name} must be one exact origin without wildcards")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{setting_name} must carry a valid TCP port") from exc
    allowed_scheme = parts.scheme == "https" or (
        allow_loopback_http and parts.scheme == "http" and parts.hostname in _LOOPBACK_ORIGIN_HOSTS
    )
    if (
        not allowed_scheme
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or value != f"{parts.scheme}://{parts.netloc}"
    ):
        raise ValueError(f"{setting_name} must be one exact origin")
    if port is not None:
        _require_tcp_port(port, setting_name=f"{setting_name} port")
    return value


# Process-handoff marker. When a short-lived launcher hands a closed environment to a
# long-lived child (or a test supplies synthetic input), this marker restricts the settings layer
# to the constructor and process sources.
SETTINGS_SOURCE_MARKER = "COURSE_EXPLORER_SETTINGS_SOURCE"


# Marker value that restricts a settings read to the constructor and process sources.
ENVIRONMENT_ONLY_SETTINGS_SOURCE = "environment"


def _project_public_values(
    settings_cls: type[BaseSettings], public: dict[str, object]
) -> dict[str, object]:
    """Project merged subsystem-file values onto the requested settings class's aliases only."""
    values: dict[str, object] = {}
    for name, field in settings_cls.model_fields.items():
        alias = field.validation_alias if isinstance(field.validation_alias, str) else name
        if alias in public:
            values[alias] = public[alias]
    return values


def _settings_aliases(
    settings_cls: type[BaseSettings], *, credentials_only: bool = False
) -> tuple[str, ...]:
    """Return the validation aliases the requested class reads from merged subsystem-file values.

    ``credentials_only`` keeps the secret fields alone, which are the values the ignored environment
    file owns.
    """
    return tuple(
        field.validation_alias if isinstance(field.validation_alias, str) else name
        for name, field in settings_cls.model_fields.items()
        if not credentials_only or field.annotation is SecretStr
    )


class ConfiguredSettings(BaseSettings):
    """Base for every settings family: the subsystem files plus the device-local ignored ``.env``.

    The standard Pydantic source hook composes, in descending precedence: native constructor
    inputs, intentional process-environment overrides, the device-local ignored ``.env``, and the
    subsystem files, over each class's own code defaults. Setting the
    ``COURSE_EXPLORER_SETTINGS_SOURCE=environment`` marker returns only the constructor and process
    sources for an isolated child or a synthetic test. Configuration
    errors hide their inputs so a secret can never be rendered by a validation failure.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="", hide_input_in_errors=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource | Callable[[], dict[str, object]], ...]:
        """Compose automatic file-backed sources, candidate sources, or only constructor/process.

        A candidate configuration block supplies its own local and subsystem-file mappings, and
        those sources replace the file reads so an edited candidate validates on its own values. The
        process-handoff marker restricts the sources to the constructor and the process environment
        when no candidate block is active. Otherwise every subsystem file and the environment file
        compose automatically.
        """
        from course_explorer import configuration

        candidate = configuration.candidate_settings_sources(settings_cls)
        if candidate is not None:
            return (init_settings, *candidate)
        if os.environ.get(SETTINGS_SOURCE_MARKER) == ENVIRONMENT_ONLY_SETTINGS_SOURCE:
            return (init_settings, env_settings)

        def public_values() -> dict[str, object]:
            return _project_public_values(
                settings_cls,
                configuration.read_public_configuration(
                    administration.repository_root(),
                    aliases=_settings_aliases(settings_cls),
                ),
            )

        return (
            init_settings,
            env_settings,
            configuration.LocalConfigurationSource(settings_cls),
            public_values,
        )


def _require_relative_path(
    value: object, *, setting_name: str, reject_percent: bool = False
) -> object:
    """Require one nonblank repository-relative path without dot components.

    Runs before Pydantic converts to ``Path`` because ``pathlib`` collapses ``.`` components
    during construction, which would hide the raw input segments this contract must reject. The
    raw value is returned as-is so every artifact path and state root shares one
    predicate. State roots additionally refuse cmd-unsafe percent characters.
    """
    raw = str(value)
    path = Path(raw)
    if (
        not raw.strip()
        or not path.parts
        or path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or any(part in {".", ".."} for part in raw.split("/"))
        or (reject_percent and "%" in raw)
    ):
        raise ValueError(
            f"{setting_name} must be a non-empty repository-relative path without dot components"
        )
    return value


def _require_artifact_sha256(value: str, *, setting_name: str) -> str:
    """Require a full lowercase SHA-256 artifact identity."""
    return require_sha256(value, setting_name)


_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


_GPU_LAYERS_PATTERN = re.compile(r"^(?:all|[0-9]+)$")


# A public repository identity is one ``namespace/name`` pair without separators.
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


# A model artifact is one GGUF file name without directories.
_GGUF_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$")


def _require_revision(value: str, *, message: str) -> str:
    """Require a full lowercase pinned commit identity."""
    if _REVISION_PATTERN.fullmatch(value) is None:
        raise ValueError(message)
    return value


def _require_decimal_int(value: object, *, setting_name: str, minimum: int, maximum: int) -> int:
    """Coerce one non-boolean decimal integer input once and bound it inclusively.

    Runs before Pydantic coercion, so a boolean, a fractional string, or a float input is
    rejected instead of silently truncated. An environment value is a decimal string.
    """
    if isinstance(value, bool):
        raise ValueError(f"{setting_name} must be an integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is not None:
        number = int(value)
    else:
        raise ValueError(f"{setting_name} must be an integer")
    return _require_int_bounds(number, setting_name=setting_name, minimum=minimum, maximum=maximum)


def _require_int_bounds(value: int, *, setting_name: str, minimum: int, maximum: int) -> int:
    """Require one integer setting inside an inclusive configured range."""
    if not minimum <= value <= maximum:
        raise ValueError(f"{setting_name} must be between {minimum} and {maximum}")
    return value
