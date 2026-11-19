"""Validated settings for the administration backend."""

from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    _require_exact_origin,
    _require_loopback_host,
    _require_tcp_port,
)
from course_explorer.config.web import InstanceSettings, WebAppSettings


class AdminWebSettings(WebAppSettings, InstanceSettings):
    """Validated loopback bind, exact site origin and session and log bounds of the administration
    backend.

    ``admin_web_origin`` is the external HTTPS origin the administration backend answers for, not
    the plain-HTTP socket the backend itself binds. It is compared exactly by scheme, host and
    port, so a request whose Host or supplied Origin differs is refused. The session lifetimes bound
    one server-side session, and the command-log limit bounds the entries the one log file retains.
    The anonymous session limit bounds the fresh anonymous sessions one process creates in the
    configured window of seconds, across every client of that process. The lock path and the
    failed-attempt limit are code constants.

    ``admin_web_poll_seconds`` is the interval the console waits after one completed panel read
    before the next read of that panel. The console's App link reads the inherited ``web_app_url``,
    which the feature site owns. ``admin_web_repository_url`` is the repository address of the
    console's GitHub link.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    admin_web_host: str = Field(validation_alias="ADMIN_WEB_HOST")
    admin_web_port: int = Field(validation_alias="ADMIN_WEB_PORT")
    admin_web_origin: str = Field(validation_alias="ADMIN_WEB_ORIGIN")
    admin_web_session_idle_minutes: int = Field(
        validation_alias="ADMIN_WEB_SESSION_IDLE_MINUTES", gt=0
    )
    admin_web_session_max_hours: int = Field(validation_alias="ADMIN_WEB_SESSION_MAX_HOURS", gt=0)
    admin_web_anonymous_session_limit: int = Field(
        validation_alias="ADMIN_WEB_ANONYMOUS_SESSION_LIMIT", gt=0
    )
    admin_web_anonymous_session_window_seconds: int = Field(
        validation_alias="ADMIN_WEB_ANONYMOUS_SESSION_WINDOW_SECONDS", gt=0
    )
    admin_web_command_log_limit: int = Field(validation_alias="ADMIN_WEB_COMMAND_LOG_LIMIT", gt=0)
    admin_web_poll_seconds: int = Field(validation_alias="ADMIN_WEB_POLL_SECONDS", gt=0)
    admin_web_repository_url: str = Field(validation_alias="ADMIN_WEB_REPOSITORY_URL")

    @field_validator("admin_web_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Require the loopback address the administration backend may bind."""
        return _require_loopback_host(value, setting_name="ADMIN_WEB_HOST")

    @field_validator("admin_web_port")
    @classmethod
    def validate_admin_web_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="ADMIN_WEB_PORT")

    @field_validator("admin_web_origin")
    @classmethod
    def validate_admin_web_origin(cls, value: str) -> str:
        """Require one exact HTTPS origin without credentials, path, query or fragment.

        The value is kept as it is spelled, so the request comparison reads one exact string. A
        wildcard, whitespace, another scheme, a port outside the TCP range, an empty host, user
        information and every URL decoration each fail the read. An invalid port raises the parser's
        own failure, which ``hide_input_in_errors`` keeps free of the value.
        """
        return _require_exact_origin(value, setting_name="ADMIN_WEB_ORIGIN")

    @field_validator("admin_web_repository_url")
    @classmethod
    def validate_admin_web_repository_url(cls, value: str) -> str:
        """Require one absolute HTTPS GitHub owner and repository address.

        The console renders this address on its GitHub link. A wildcard, whitespace, another
        scheme or host, user information, a query or fragment marker, and any path other than one
        owner and one repository each fail the read.
        """
        if not value or any(character.isspace() for character in value) or "*" in value:
            raise ValueError("ADMIN_WEB_REPOSITORY_URL must be one absolute repository address")
        parts = urlsplit(value)
        segments = [segment for segment in parts.path.split("/") if segment]
        if (
            parts.scheme != "https"
            or parts.netloc != "github.com"
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or len(segments) != 2
        ):
            raise ValueError(
                "ADMIN_WEB_REPOSITORY_URL must be one HTTPS GitHub owner/repository address"
            )
        return value


def load_admin_web_settings() -> AdminWebSettings:
    """Read administration bind, origin, session, anonymous-grant budget and log bounds.

    The administration keys compose automatically from ``config/admin_web.toml``, and the inherited
    app address composes from ``config/web.toml``.
    """
    return AdminWebSettings()
