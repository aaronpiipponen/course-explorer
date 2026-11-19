"""Validated settings for the curriculum database, the shared directory and the dataset
roles."""

import ipaddress
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import URL

from course_explorer import administration
from course_explorer.config.base import (
    ConfiguredSettings,
    _require_nonblank_secret,
    _require_relative_path,
    _require_tcp_port,
)
from course_explorer.validation import (
    require_nonblank_text as _require_nonblank_text,
)

if TYPE_CHECKING:
    from course_explorer.datasets import DatasetRole


class DatabaseSettings(ConfiguredSettings):
    """Curriculum PostgreSQL connection components shared with the local Compose service."""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    postgres_host: str = Field(validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(validation_alias="POSTGRES_PORT")
    postgres_db: str = Field(validation_alias="POSTGRES_DB")
    example_postgres_db: str = Field(validation_alias="EXAMPLE_POSTGRES_DB")
    postgres_user: str = Field(validation_alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(validation_alias="POSTGRES_PASSWORD")
    admin_state_root: Path = Field(validation_alias="ADMIN_STATE_ROOT")

    @field_validator("postgres_host", "postgres_db", "postgres_user")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject missing meaningful connection components without normalizing them."""
        return _require_nonblank_text(
            value, message="database connection text values must not be blank"
        )

    @field_validator("example_postgres_db")
    @classmethod
    def validate_example_postgres_db(cls, value: str) -> str:
        """Require the configured example database to be one lowercase SQL identifier."""
        from pydantic import TypeAdapter

        from course_explorer.datasets import DatasetDatabaseName

        return TypeAdapter(DatasetDatabaseName).validate_python(value)

    @field_validator("admin_state_root", mode="before")
    @classmethod
    def validate_admin_state_root(cls, value: object) -> object:
        """Require one nonblank repository-relative admin state root without dot components."""
        return _require_relative_path(value, setting_name="ADMIN_STATE_ROOT", reject_percent=True)

    @field_validator("postgres_password")
    @classmethod
    def reject_blank_password(cls, value: SecretStr) -> SecretStr:
        """Reject blank passwords while keeping the secret wrapped."""
        return _require_nonblank_secret(value, message="postgres password must not be blank")

    @field_validator("postgres_port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="postgres port")

    @property
    def sqlalchemy_url(self) -> URL:
        """Build the curriculum database connection URL from shared components."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


def load_raw_database_settings() -> DatabaseSettings:
    """Read the configured database fields without resolving a dataset role."""
    return DatabaseSettings()


# The service-environment alias that carries the Docker host's home directory. A container resolves
# a home-prefixed shared root against this value, so the path names the host's directory.
HOST_HOME_ENVIRONMENT_ALIAS = "COURSE_EXPLORER_HOST_HOME"


def _host_home() -> Path:
    """Return the Docker host's home directory named by the container environment, or this home.

    A relative ``COURSE_EXPLORER_HOST_HOME`` fails: it prefixes one absolute shared root, so a
    relative value would resolve the root under the Compose project directory.
    """
    value = os.environ.get(HOST_HOME_ENVIRONMENT_ALIAS)
    home = Path(value) if value else Path.home()
    if not home.is_absolute():
        raise ValueError(f"{HOST_HOME_ENVIRONMENT_ALIAS} must be an absolute path")
    return home


class SharedStorageSettings(ConfiguredSettings):
    """The machine-wide shared directory and the machine-wide site proxy's optional bind address.

    ``ADMIN_SHARED_ROOT`` is one absolute path, or one beginning with ``~/``. A blank value, a
    relative value and any other home prefix fail the read. A home prefix expands against the Docker
    host's home directory, which the container environments name through
    :data:`HOST_HOME_ENVIRONMENT_ALIAS`, so the resolved path addresses the host and every Compose
    bind names the host's directory. The configured directory must exist on the host before a
    Compose command binds it, because every shared bind refuses a missing source. The setup workflows
    create it, and the stack verbs bind the configured value without creating it. A relative
    embedding artifact path resolves inside this directory. ``PROXY_BIND_ADDRESS`` names the one host
    address the machine-wide site proxy binds. Absent, the proxy listens on every host address.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    shared_root: Path = Field(validation_alias="ADMIN_SHARED_ROOT")
    proxy_bind_address: str | None = Field(default=None, validation_alias="PROXY_BIND_ADDRESS")

    @field_validator("shared_root", mode="before")
    @classmethod
    def validate_shared_root(cls, value: object) -> object:
        """Require an absolute or home-prefixed path and expand the home prefix on the host."""
        raw = str(value)
        if not raw.strip():
            raise ValueError("ADMIN_SHARED_ROOT must not be blank")
        if raw == "~":
            return _host_home()
        if raw.startswith("~/"):
            return _host_home() / raw[2:].lstrip("/")
        if raw.startswith("~"):
            raise ValueError("ADMIN_SHARED_ROOT must be an absolute or home-prefixed path")
        expanded = Path(raw)
        if not expanded.is_absolute():
            raise ValueError("ADMIN_SHARED_ROOT must be an absolute or home-prefixed path")
        return expanded

    @field_validator("proxy_bind_address", mode="before")
    @classmethod
    def validate_proxy_bind_address(cls, value: object) -> str | None:
        """Require one IPv4 or IPv6 literal and keep its normalized text.

        An absent value binds every host address. A non-string value, a blank string and any text
        that is not one address fail with the same message, so no parser text escapes.
        """
        message = "PROXY_BIND_ADDRESS must be an IPv4 or IPv6 address"
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(message)
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise ValueError(message) from exc

    @property
    def models_root(self) -> Path:
        """Return the machine shared model directory."""
        return self.shared_root / "models"

    @property
    def locks_root(self) -> Path:
        """Return the machine shared reservation directory."""
        return self.shared_root / "locks"

    def resolve_embedding_artifact(self, configured: Path) -> Path:
        """Resolve a relative embedding artifact path under the machine shared directory."""
        return configured if configured.is_absolute() else self.shared_root / configured


def load_shared_storage_settings() -> SharedStorageSettings:
    """Read the machine-wide shared directory through the automatic composition."""
    return SharedStorageSettings()


def load_database_settings_with_identity(
    *, role: DatasetRole = "selected"
) -> tuple[DatabaseSettings, str | None]:
    """Resolve one dataset role's connection together with its dataset identity.

    A Selected state root with neither pointer is bootstrap, so the raw configured database is
    returned with no identity. Otherwise the named role's receipt is resolved once through its
    repository-relative admin state root, and its database name replaces ``POSTGRES_DB``. Working
    requires a pointer, while a missing or inconsistent Selected pointer raises its own guidance.
    The raw database name never selects a role, and no dotenv or process-global value is mutated.
    """
    from course_explorer.datasets import read_dataset_role, resolve_dataset_role

    settings = DatabaseSettings()
    state_root = administration.repository_root() / settings.admin_state_root
    if role == "selected":
        receipt = read_dataset_role(state_root, "selected")
        if receipt is None:
            return settings, None
    else:
        receipt = resolve_dataset_role(state_root, role)
    resolved = settings.model_copy(update={"postgres_db": receipt.database_name})
    return resolved, receipt.dataset_id


def load_database_settings(*, role: DatasetRole = "selected") -> DatabaseSettings:
    """Read the resolved database settings of one dataset role, discarding its identity."""
    settings, _ = load_database_settings_with_identity(role=role)
    return settings


_CONTROLLER_DATABASE_PREFIX_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


_CONTROLLER_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _require_database_prefix(value: str, *, setting_name: str) -> str:
    """Require a lowercase SQL identifier prefix short enough for a 63-byte derived name."""
    if (
        _CONTROLLER_DATABASE_PREFIX_PATTERN.fullmatch(value) is None
        or len(value.encode("ascii")) > 30
    ):
        raise ValueError(
            f"{setting_name} must match [a-z_][a-z0-9_]* and be at most 30 ASCII bytes"
        )
    return value


class AdministrativeDatasetSettings(ConfiguredSettings):
    """Validated selected-dataset state root, derived database prefixes, and one operation bound.

    These values are portable policy: the repository-relative admin state root, the two
    distinct lowercase prefixes that derive one server or worker database name from a validated
    32-character hexadecimal id, and the single timeout every dataset dump/restore/rotation
    subprocess receives.
    The state root reuses the shared repository-relative predicate. Loading composes the
    subsystem files under ``config/`` and the device-local ignored ``.env`` automatically.
    There is no code default.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    admin_state_root: Path = Field(validation_alias="ADMIN_STATE_ROOT")
    dataset_database_prefix: str = Field(validation_alias="ADMIN_DATASET_DATABASE_PREFIX")
    worker_database_prefix: str = Field(validation_alias="ADMIN_WORKER_DATABASE_PREFIX")
    database_operation_timeout_seconds: float = Field(
        validation_alias="ADMIN_DATABASE_OPERATION_TIMEOUT_SECONDS",
        gt=0,
        allow_inf_nan=False,
    )

    @field_validator("admin_state_root", mode="before")
    @classmethod
    def validate_admin_state_root(cls, value: object) -> object:
        """Require one nonblank repository-relative admin state root without dot components."""
        return _require_relative_path(value, setting_name="ADMIN_STATE_ROOT", reject_percent=True)

    @field_validator("dataset_database_prefix", "worker_database_prefix")
    @classmethod
    def validate_database_prefix(cls, value: str) -> str:
        """Require a lowercase SQL identifier prefix short enough for a 63-byte derived name."""
        return _require_database_prefix(value, setting_name="ADMIN_*_DATABASE_PREFIX")

    @model_validator(mode="after")
    def validate_distinct_prefixes(self) -> Self:
        """Require the server and worker prefixes to derive different database names."""
        if self.dataset_database_prefix == self.worker_database_prefix:
            raise ValueError("admin dataset and worker database prefixes must be distinct")
        return self

    def database_name(self, dataset_id: str, *, worker: bool = False) -> str:
        """Derive the one server or worker database name from a validated 32-character
        hexadecimal id."""
        if _CONTROLLER_SESSION_ID_PATTERN.fullmatch(dataset_id) is None:
            raise ValueError("dataset id must be 32 lowercase hexadecimal characters")
        prefix = self.worker_database_prefix if worker else self.dataset_database_prefix
        derived = f"{prefix}_{dataset_id}"
        if len(derived.encode("ascii")) > 63:
            raise ValueError("derived dataset database name must be at most 63 ASCII bytes")
        return derived


def load_administrative_dataset_settings() -> AdministrativeDatasetSettings:
    """Read selected-dataset administrative settings through the automatic composition."""
    return AdministrativeDatasetSettings()
