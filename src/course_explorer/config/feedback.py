"""Validated settings for the feedback application store, its intake policy, its
destination and its sender."""

import math
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import URL

from course_explorer.config.base import (
    ConfiguredSettings,
    _require_nonblank_secret,
    _require_tcp_port,
)
from course_explorer.config.web import WebAppSettings
from course_explorer.validation import (
    require_nonblank_text as _require_nonblank_text,
)

_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


class FeedbackSettings(ConfiguredSettings):
    """Feedback application-store connection and attachment-root settings.

    The connection names a separate application database, so the curriculum dataset and its
    selected-dataset pointer never select the feedback store. The attachment root accepts a relative
    path, an absolute path or one beginning with ``~/``. It is expanded and resolved when an
    attachment store is built, not while settings load.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    feedback_postgres_host: str = Field(validation_alias="FEEDBACK_POSTGRES_HOST")
    feedback_postgres_port: int = Field(validation_alias="FEEDBACK_POSTGRES_PORT")
    feedback_postgres_db: str = Field(validation_alias="FEEDBACK_POSTGRES_DB")
    feedback_postgres_user: str = Field(validation_alias="FEEDBACK_POSTGRES_USER")
    feedback_postgres_password: SecretStr = Field(validation_alias="FEEDBACK_POSTGRES_PASSWORD")
    feedback_attachments_root: Path = Field(validation_alias="FEEDBACK_ATTACHMENTS_ROOT")

    @field_validator("feedback_postgres_host", "feedback_postgres_db", "feedback_postgres_user")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject missing meaningful connection components without normalizing them."""
        return _require_nonblank_text(
            value, message="feedback database connection text values must not be blank"
        )

    @field_validator("feedback_attachments_root", mode="before")
    @classmethod
    def validate_attachments_root(cls, value: object) -> object:
        """Require one nonblank path string before it is read as a path."""
        if not isinstance(value, str):
            raise ValueError("FEEDBACK_ATTACHMENTS_ROOT must be a path string")
        return _require_nonblank_text(value, message="FEEDBACK_ATTACHMENTS_ROOT must not be blank")

    @field_validator("feedback_postgres_password")
    @classmethod
    def reject_blank_password(cls, value: SecretStr) -> SecretStr:
        """Reject a blank password while keeping it wrapped."""
        return _require_nonblank_secret(
            value, message="feedback postgres password must not be blank"
        )

    @field_validator("feedback_postgres_port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="feedback postgres port")

    @property
    def sqlalchemy_url(self) -> URL:
        """Build the feedback database connection URL from its own components."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.feedback_postgres_user,
            password=self.feedback_postgres_password.get_secret_value(),
            host=self.feedback_postgres_host,
            port=self.feedback_postgres_port,
            database=self.feedback_postgres_db,
        )


def load_feedback_settings() -> FeedbackSettings:
    """Read the feedback application-store settings through the automatic composition."""
    return FeedbackSettings()


class FeedbackDestinationSettings(ConfiguredSettings):
    """Non-secret feedback destination metadata shared by intake and the sender.

    The repository names one owner and repository, never a URL, and the visibility states the
    destination the operator configured. The values carry no credential, so a destination edit
    validates from this base alone.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    feedback_github_repository: str = Field(validation_alias="FEEDBACK_GITHUB_REPOSITORY")
    feedback_github_visibility: Literal["public", "private"] = Field(
        validation_alias="FEEDBACK_GITHUB_VISIBILITY"
    )

    @field_validator("feedback_github_repository")
    @classmethod
    def validate_github_repository(cls, value: str) -> str:
        """Require one GitHub owner and repository name in owner/repository form."""
        if _GITHUB_REPOSITORY.fullmatch(value) is None:
            raise ValueError("FEEDBACK_GITHUB_REPOSITORY must name one repository")
        return value


class FeedbackIntakeSettings(WebAppSettings, FeedbackDestinationSettings):
    """Validated feedback intake policy and non-secret destination metadata.

    The inherited ``web_app_url`` supplies the exact external app origin the feedback pages answer
    for, through ``web_app_origin``. The other values are policy and destination metadata carried by
    ``config/feedback.toml``. The two hourly values are positive submission counts per rolling hour,
    and the body value is a positive byte count. The inherited destination values name the
    configured repository and visibility. No credential belongs to this family.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="", hide_input_in_errors=True)

    feedback_browser_submissions_per_hour: int = Field(
        validation_alias="FEEDBACK_BROWSER_SUBMISSIONS_PER_HOUR", gt=0
    )
    feedback_site_submissions_per_hour: int = Field(
        validation_alias="FEEDBACK_SITE_SUBMISSIONS_PER_HOUR", gt=0
    )
    feedback_max_request_bytes: int = Field(validation_alias="FEEDBACK_MAX_REQUEST_BYTES", gt=0)


def load_feedback_intake_settings() -> FeedbackIntakeSettings:
    """Read the feedback intake policy and destination metadata through the composed settings."""
    return FeedbackIntakeSettings()


class FeedbackSenderCredentialsSettings(ConfiguredSettings):
    """The GitHub publication credential and the bound of one external creation attempt.

    The token is a secret and never rendered. The timeout is a positive, finite number of seconds
    that bounds one ``gh`` invocation.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="", hide_input_in_errors=True)

    feedback_github_token: SecretStr = Field(validation_alias="FEEDBACK_GITHUB_TOKEN")
    feedback_github_timeout_seconds: float = Field(
        validation_alias="FEEDBACK_GITHUB_TIMEOUT_SECONDS", gt=0
    )

    @field_validator("feedback_github_token")
    @classmethod
    def reject_blank_token(cls, value: SecretStr) -> SecretStr:
        """Reject a blank token while keeping it wrapped and never rendering it."""
        return _require_nonblank_secret(value, message="feedback GitHub token must not be blank")

    @field_validator("feedback_github_timeout_seconds")
    @classmethod
    def require_finite_timeout(cls, value: float) -> float:
        """Require a finite second count."""
        if not math.isfinite(value):
            raise ValueError("FEEDBACK_GITHUB_TIMEOUT_SECONDS must be finite")
        return value


class FeedbackSenderSettings(FeedbackDestinationSettings, FeedbackSenderCredentialsSettings):
    """The sender's configured destination, credential and attempt timeout."""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="", hide_input_in_errors=True)


def load_feedback_sender_settings() -> FeedbackSenderSettings:
    """Read the sender destination, credential and timeout through the automatic composition."""
    return FeedbackSenderSettings()
