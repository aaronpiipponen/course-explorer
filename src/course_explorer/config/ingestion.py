"""Validated settings for raw capture, the public source request policy and
normalization."""

from pathlib import Path
from typing import Self

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    _ANY_HTTP_URL_ADAPTER,
    ConfiguredSettings,
    _require_relative_path,
)
from course_explorer.config.web import InstanceSettings
from course_explorer.instance import (
    UNIVERSITIES,
    SisuSourceMetadata,
    academic_year_start,
)


class CaptureSettings(ConfiguredSettings):
    """Repository-relative location and checkpoint policy for raw capture evidence."""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    raw_data_root: Path = Field(validation_alias="RAW_DATA_ROOT")
    capture_checkpoint_response_count: int = Field(
        validation_alias="CAPTURE_CHECKPOINT_RESPONSE_COUNT", ge=1, le=1000
    )

    @field_validator("raw_data_root", mode="before")
    @classmethod
    def validate_raw_data_root(cls, value: Path | str) -> Path:
        """Require one non-empty relative path without traversal components."""
        return Path(_require_relative_path(value, setting_name="RAW_DATA_ROOT"))


def load_capture_settings() -> CaptureSettings:
    """Read capture settings through the automatic subsystem-file and environment-file
    composition."""
    return CaptureSettings()


class KoriSettings(InstanceSettings):
    """Validated anonymous public source policy for the configured university instance.

    The class and its ``KORI_*`` request-policy keys serve every implemented HTTP source family.
    The Sisu client's base and university root identity come from the instance's Sisu source
    block, and the first academic year comes from the configured family's source block, so a
    change of the configured university changes the source and no constructor or environment value
    overrides those facts. The remaining fields are operator policy shared by every family.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    current_academic_year: str = Field(validation_alias="CURRENT_ACADEMIC_YEAR")
    kori_search_page_size: int = Field(validation_alias="KORI_SEARCH_PAGE_SIZE", ge=1, le=1000)
    kori_batch_size: int = Field(validation_alias="KORI_BATCH_SIZE", ge=1, le=100)
    kori_request_timeout_seconds: float = Field(
        validation_alias="KORI_REQUEST_TIMEOUT_SECONDS", gt=0, le=120
    )
    kori_max_attempts: int = Field(validation_alias="KORI_MAX_ATTEMPTS", ge=1, le=5)
    kori_retry_backoff_seconds: float = Field(
        validation_alias="KORI_RETRY_BACKOFF_SECONDS", ge=0, le=10
    )
    kori_concurrency: int = Field(validation_alias="KORI_CONCURRENCY", ge=1, le=8)
    kori_max_partition_depth: int = Field(validation_alias="KORI_MAX_PARTITION_DEPTH", ge=1, le=12)
    kori_request_interval_seconds: float = Field(
        validation_alias="KORI_REQUEST_INTERVAL_SECONDS", gt=0, allow_inf_nan=False
    )

    @field_validator("current_academic_year")
    @classmethod
    def validate_academic_year(cls, value: str) -> str:
        """Require consecutive four-digit academic-year bounds."""
        academic_year_start(value)
        return value

    @model_validator(mode="after")
    def validate_academic_year_order(self) -> Self:
        """Reject a mapped first academic year after the configured current year."""
        if self.first_academic_year_start > self.current_academic_year_start:
            raise ValueError("first academic year must not exceed current academic year")
        return self

    def _require_sisu_source(self) -> SisuSourceMetadata:
        """Return the configured university's Sisu source block or raise one clear error."""
        source = self.university.sisu
        if source is None:
            raise ValueError("the configured university has no Sisu source block")
        return source

    @property
    def kori_base_url(self) -> AnyHttpUrl:
        """Return the mapped anonymous HTTPS Kori base of the configured university."""
        return _ANY_HTTP_URL_ADAPTER.validate_python(self._require_sisu_source().kori_base_url)

    @property
    def university_id(self) -> str:
        """Return the mapped university root identity the source requests name."""
        return self._require_sisu_source().university_id

    @property
    def kori_first_academic_year(self) -> str:
        """Return the mapped earliest academic year whose curriculum periods are captured."""
        return self._require_sisu_source().first_academic_year

    @property
    def first_academic_year_start(self) -> int:
        """Return the mapped first academic-year start of the configured source family."""
        return self.university.first_academic_year_start

    @property
    def current_academic_year_start(self) -> int:
        """Return the configured current academic-year start."""
        return academic_year_start(self.current_academic_year)


def load_kori_settings() -> KoriSettings:
    """Read public Kori settings through the automatic subsystem-file and environment-file
    composition."""
    return KoriSettings()


class NormalizationSettings(InstanceSettings):
    """Validated normalization policy for the configured university instance.

    The course-guide base comes from the configured instance's source block, so a change of the
    configured university changes the official page address and no constructor or environment
    value overrides it.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    current_academic_year: str = Field(validation_alias="CURRENT_ACADEMIC_YEAR")
    active_academic_year_window: int = Field(
        validation_alias="ACTIVE_ACADEMIC_YEAR_WINDOW", ge=1, le=100
    )

    @field_validator("current_academic_year")
    @classmethod
    def validate_current_academic_year(cls, value: str) -> str:
        """Require consecutive four-digit academic-year bounds."""
        academic_year_start(value)
        return value

    @property
    def current_academic_year_start(self) -> int:
        """Return configured current academic-year start."""
        return academic_year_start(self.current_academic_year)

    @property
    def course_guide_base_url(self) -> AnyHttpUrl:
        """Return the mapped course-guide base of the configured university."""
        return validate_course_guide_base_url(self.university.course_guide_base_url)


# The canonical course-guide bases of the finite university map.
_COURSE_GUIDE_BASE_URLS = frozenset(
    metadata.course_guide_base_url for metadata in UNIVERSITIES.values()
)


def validate_course_guide_base_url(value: object) -> AnyHttpUrl:
    """Validate and return one canonical HTTPS course-guide base of the university map."""
    url = _ANY_HTTP_URL_ADAPTER.validate_python(value)
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or url.username is not None
        or url.password is not None
        or url.query is not None
        or url.fragment is not None
        or f"https://{url.host}{url.path.removesuffix('/')}" not in _COURSE_GUIDE_BASE_URLS
    ):
        raise ValueError("course-guide base must be one canonical university course-guide base")
    return url


def load_normalization_settings() -> NormalizationSettings:
    """Read normalization settings through the automatic subsystem-file and environment-file
    composition."""
    return NormalizationSettings()
