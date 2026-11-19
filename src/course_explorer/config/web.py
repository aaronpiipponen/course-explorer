"""Validated settings for the university instance, the web app address, the serving
process, the admission policy and usage retention."""

from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from course_explorer.config.base import (
    ConfiguredSettings,
    _require_exact_origin,
    _require_loopback_host,
    _require_tcp_port,
)
from course_explorer.instance import UNIVERSITIES, UniversityMetadata, validate_display_label


class InstanceSettings(ConfiguredSettings):
    """The one configured university instance and its metadata.

    ``instance_key`` selects one entry of the finite university map, so an instance presents one
    university and one curriculum source. ``instance_display_label`` is an optional lowercase brand
    label that overrides the map entry's label. The read-only ``university`` property returns that
    map entry, and the read-only ``display_label`` property returns the override or the map label.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    instance_key: str = Field(validation_alias="INSTANCE_KEY")
    instance_display_label: str | None = Field(
        default=None, validation_alias="INSTANCE_DISPLAY_LABEL"
    )

    @field_validator("instance_key")
    @classmethod
    def validate_instance_key(cls, value: str) -> str:
        """Require one configured university key without echoing the value."""
        if value not in UNIVERSITIES:
            raise ValueError("INSTANCE_KEY must name one configured university")
        return value

    @field_validator("instance_display_label")
    @classmethod
    def validate_instance_display_label(cls, value: str | None) -> str | None:
        """Validate an optional override through the shared display-label owner."""
        if value is None:
            return None
        return validate_display_label(value)

    @property
    def university(self) -> UniversityMetadata:
        """Return the configured university's map entry."""
        return UNIVERSITIES[self.instance_key]

    @property
    def display_label(self) -> str:
        """Return the override label or the configured university's map label."""
        if self.instance_display_label is not None:
            return self.instance_display_label
        return self.university.display_label


def load_instance_settings() -> InstanceSettings:
    """Read the configured instance key through the automatic composition."""
    return InstanceSettings()


class WebAppSettings(ConfiguredSettings):
    """The one external web app address shared by the administration App link and the feedback
    exact-site boundary.

    ``web_app_url`` is the site's root address, at the root and without credentials, wildcard,
    whitespace, query or fragment. It is kept as it is spelled, so the console renders the
    configured address. ``web_app_origin`` is that address without its optional root slash, which is
    the one exact origin a feedback submission's Host and Origin must equal. HTTPS is the only
    scheme unless the hostname is ``localhost``, ``127.0.0.1`` or ``::1``.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    web_app_url: str = Field(validation_alias="WEB_APP_URL")

    @field_validator("web_app_url")
    @classmethod
    def validate_web_app_url(cls, value: str) -> str:
        """Require one root app address and validate the origin without its optional root slash."""
        origin = value[:-1] if value.endswith("/") else value
        _require_exact_origin(origin, setting_name="WEB_APP_URL", allow_loopback_http=True)
        return value

    @property
    def web_app_origin(self) -> str:
        """Return the exact origin, which is the address without its optional root slash."""
        return self.web_app_url[:-1] if self.web_app_url.endswith("/") else self.web_app_url


class WebSettings(WebAppSettings, InstanceSettings):
    """Validated loopback bind, process count, malloc arena cap, child respawn limits, drawing,
    served-feature and retained-cache region settings for HTTP serving.

    The class inherits the one external app address from :class:`WebAppSettings` and the configured
    instance key and display label from :class:`InstanceSettings`."""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    web_host: str = Field(validation_alias="WEB_HOST")
    web_port: int = Field(validation_alias="WEB_PORT")
    web_workers: int = Field(validation_alias="WEB_WORKERS", gt=0)
    web_malloc_arena_max: int = Field(validation_alias="WEB_MALLOC_ARENA_MAX", gt=0)
    web_respawn_exit_limit: int = Field(validation_alias="WEB_RESPAWN_EXIT_LIMIT", gt=0)
    web_respawn_window_seconds: int = Field(validation_alias="WEB_RESPAWN_WINDOW_SECONDS", gt=0)
    prerequisite_graph_max_elements: int = Field(
        validation_alias="PREREQUISITE_GRAPH_MAX_ELEMENTS", gt=0
    )
    embeddings_enabled: bool = Field(validation_alias="WEB_EMBEDDINGS_ENABLED")
    inference_enabled: bool = Field(validation_alias="WEB_INFERENCE_ENABLED")
    prerequisites_enabled: bool = Field(validation_alias="WEB_PREREQUISITES_ENABLED")
    web_next_course_report_cache_bytes: int = Field(
        validation_alias="WEB_NEXT_COURSE_REPORT_CACHE_BYTES", gt=0
    )
    web_query_embedding_cache_bytes: int = Field(
        validation_alias="WEB_QUERY_EMBEDDING_CACHE_BYTES", gt=0
    )
    web_request_max_course_items: int = Field(validation_alias="WEB_REQUEST_MAX_COURSE_ITEMS", gt=0)
    web_request_max_identifier_length: int = Field(
        validation_alias="WEB_REQUEST_MAX_IDENTIFIER_LENGTH", gt=0
    )
    web_request_max_query_length: int = Field(validation_alias="WEB_REQUEST_MAX_QUERY_LENGTH", gt=0)
    web_request_body_max_bytes: int = Field(validation_alias="WEB_REQUEST_BODY_MAX_BYTES", gt=0)

    @field_validator("web_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Require a loopback host."""
        return _require_loopback_host(value, setting_name="WEB_HOST")

    @field_validator("web_port")
    @classmethod
    def validate_web_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        return _require_tcp_port(value, setting_name="WEB_PORT")


def load_web_settings() -> WebSettings:
    """Read web bind, child supervision, drawing, served-feature and retained-cache budget settings
    through the automatic composition.
    """
    return WebSettings()


class AdmissionSettings(ConfiguredSettings):
    """The front router's admission policy and its counter lifetimes.

    Every cost value is total server core-seconds, covering the serving process, the database and
    the embedding service, and a graph's cost is its measured elapsed seconds. The load regime
    follows one score over the configured window: the sum of the whole host's CPU utilisation
    times its weight and the light and heavy admissions counted in that window times their
    weights. The ``web_workers`` field reads the same ``WEB_WORKERS`` value as the web settings,
    and the policy caps the heavy slots at one fewer than it. The router holds the policy's
    counters, activity timestamps, tickets and graph costs in process memory. The identity
    lifetime is the seconds of idleness after which one cookie's light and heavy counters become
    removable, while the cookie's own max-age is a separate value. A cookie that runs a request or
    holds a ticket keeps its counters past that mark, and the front router sweeps the counters for
    expiry once per lifetime.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    web_workers: int = Field(validation_alias="WEB_WORKERS", gt=0)
    admission_enabled: bool = Field(validation_alias="ADMISSION_ENABLED")
    admission_identity_ttl_seconds: int = Field(
        validation_alias="ADMISSION_IDENTITY_TTL_SECONDS", gt=0
    )
    admission_cookie_max_age_seconds: int = Field(
        validation_alias="ADMISSION_COOKIE_MAX_AGE_SECONDS", gt=0
    )
    admission_retry_after_seconds: int = Field(
        validation_alias="ADMISSION_RETRY_AFTER_SECONDS", gt=0
    )
    admission_window_seconds: int = Field(validation_alias="ADMISSION_WINDOW_SECONDS", gt=0)
    admission_load_window_seconds: int = Field(
        validation_alias="ADMISSION_LOAD_WINDOW_SECONDS", gt=0
    )
    admission_score_cpu_weight: float = Field(
        validation_alias="ADMISSION_SCORE_CPU_WEIGHT", ge=0, allow_inf_nan=False
    )
    admission_score_light_weight: float = Field(
        validation_alias="ADMISSION_SCORE_LIGHT_WEIGHT", ge=0, allow_inf_nan=False
    )
    admission_score_heavy_weight: float = Field(
        validation_alias="ADMISSION_SCORE_HEAVY_WEIGHT", ge=0, allow_inf_nan=False
    )
    admission_score_headroom: float = Field(
        validation_alias="ADMISSION_SCORE_HEADROOM", gt=0, allow_inf_nan=False
    )
    admission_score_saturation: float = Field(
        validation_alias="ADMISSION_SCORE_SATURATION", gt=0, allow_inf_nan=False
    )
    admission_score_release: float = Field(
        validation_alias="ADMISSION_SCORE_RELEASE", ge=0, allow_inf_nan=False
    )
    admission_light_load_floor: float = Field(
        validation_alias="ADMISSION_LIGHT_LOAD_FLOOR", gt=0, le=1
    )
    admission_light_operations_per_minute: float = Field(
        validation_alias="ADMISSION_LIGHT_OPERATIONS_PER_MINUTE", gt=0
    )
    admission_light_burst: int = Field(validation_alias="ADMISSION_LIGHT_BURST", gt=0)
    admission_light_concurrency: int = Field(validation_alias="ADMISSION_LIGHT_CONCURRENCY", gt=0)
    admission_heavy_budget_core_seconds: float = Field(
        validation_alias="ADMISSION_HEAVY_BUDGET_CORE_SECONDS", gt=0
    )
    admission_heavy_budget_saturated_core_seconds: float = Field(
        validation_alias="ADMISSION_HEAVY_BUDGET_SATURATED_CORE_SECONDS", gt=0
    )
    admission_heavy_slots: int = Field(validation_alias="ADMISSION_HEAVY_SLOTS", gt=0)
    admission_heavy_queue_headroom_limit: int = Field(
        validation_alias="ADMISSION_HEAVY_QUEUE_HEADROOM_LIMIT", gt=0
    )
    admission_heavy_queue_middle_limit: int = Field(
        validation_alias="ADMISSION_HEAVY_QUEUE_MIDDLE_LIMIT", gt=0
    )
    admission_heavy_queue_saturated_limit: int = Field(
        validation_alias="ADMISSION_HEAVY_QUEUE_SATURATED_LIMIT", gt=0
    )
    admission_recommendation_active_heavy_basis: int = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ACTIVE_HEAVY_BASIS", gt=0
    )
    admission_recommendation_inactive_heavy_basis: int = Field(
        validation_alias="ADMISSION_RECOMMENDATION_INACTIVE_HEAVY_BASIS", gt=0
    )
    admission_next_courses_active_heavy_basis: int = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ACTIVE_HEAVY_BASIS", gt=0
    )
    admission_next_courses_inactive_heavy_basis: int = Field(
        validation_alias="ADMISSION_NEXT_COURSES_INACTIVE_HEAVY_BASIS", gt=0
    )
    admission_graph_heavy_seconds: float = Field(
        validation_alias="ADMISSION_GRAPH_HEAVY_SECONDS", gt=0
    )
    admission_search_inactive_cost_core_seconds: float = Field(
        validation_alias="ADMISSION_SEARCH_INACTIVE_COST_CORE_SECONDS", gt=0
    )
    admission_recommendation_origin_basis: int = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ORIGIN_BASIS", ge=0
    )
    admission_recommendation_active_knee_basis: int = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ACTIVE_KNEE_BASIS", gt=0
    )
    admission_recommendation_inactive_knee_basis: int = Field(
        validation_alias="ADMISSION_RECOMMENDATION_INACTIVE_KNEE_BASIS", gt=0
    )
    admission_recommendation_active_origin_cost: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ACTIVE_ORIGIN_COST", ge=0
    )
    admission_recommendation_active_slope: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ACTIVE_SLOPE", ge=0
    )
    admission_recommendation_active_tail_cost: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ACTIVE_TAIL_COST", ge=0
    )
    admission_recommendation_active_tail_slope: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_ACTIVE_TAIL_SLOPE", ge=0
    )
    admission_recommendation_inactive_origin_cost: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_INACTIVE_ORIGIN_COST", ge=0
    )
    admission_recommendation_inactive_slope: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_INACTIVE_SLOPE", ge=0
    )
    admission_recommendation_inactive_tail_cost: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_INACTIVE_TAIL_COST", ge=0
    )
    admission_recommendation_inactive_tail_slope: float = Field(
        validation_alias="ADMISSION_RECOMMENDATION_INACTIVE_TAIL_SLOPE", ge=0
    )
    admission_next_courses_origin_basis: int = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ORIGIN_BASIS", ge=0
    )
    admission_next_courses_active_knee_basis: int = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ACTIVE_KNEE_BASIS", gt=0
    )
    admission_next_courses_inactive_knee_basis: int = Field(
        validation_alias="ADMISSION_NEXT_COURSES_INACTIVE_KNEE_BASIS", gt=0
    )
    admission_next_courses_active_origin_cost: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ACTIVE_ORIGIN_COST", ge=0
    )
    admission_next_courses_active_slope: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ACTIVE_SLOPE", ge=0
    )
    admission_next_courses_active_tail_cost: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ACTIVE_TAIL_COST", ge=0
    )
    admission_next_courses_active_tail_slope: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_ACTIVE_TAIL_SLOPE", ge=0
    )
    admission_next_courses_inactive_origin_cost: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_INACTIVE_ORIGIN_COST", ge=0
    )
    admission_next_courses_inactive_slope: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_INACTIVE_SLOPE", ge=0
    )
    admission_next_courses_inactive_tail_cost: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_INACTIVE_TAIL_COST", ge=0
    )
    admission_next_courses_inactive_tail_slope: float = Field(
        validation_alias="ADMISSION_NEXT_COURSES_INACTIVE_TAIL_SLOPE", ge=0
    )

    @model_validator(mode="after")
    def require_consistent_policy(self) -> Self:
        """Refuse a policy whose thresholds contradict its shape or that leaves no spare worker."""
        if self.admission_enabled and self.web_workers < 2:
            raise ValueError(
                "ADMISSION_ENABLED requires WEB_WORKERS of at least 2, because one worker is "
                "always kept free of heavy work"
            )
        if not (
            self.admission_score_headroom
            <= self.admission_score_release
            < self.admission_score_saturation
        ):
            raise ValueError(
                "ADMISSION_SCORE_HEADROOM must be at most ADMISSION_SCORE_RELEASE and "
                "ADMISSION_SCORE_RELEASE must be below ADMISSION_SCORE_SATURATION"
            )
        for curve in ("RECOMMENDATION", "NEXT_COURSES"):
            origin = getattr(self, f"admission_{curve.lower()}_origin_basis")
            for scope in ("ACTIVE", "INACTIVE"):
                knee = getattr(self, f"admission_{curve.lower()}_{scope.lower()}_knee_basis")
                if knee <= origin:
                    raise ValueError(
                        f"ADMISSION_{curve}_{scope}_KNEE_BASIS must be above "
                        f"ADMISSION_{curve}_ORIGIN_BASIS"
                    )
        return self


def load_admission_settings() -> AdmissionSettings:
    """Read the admission policy through the automatic subsystem-file and environment-file
    composition."""
    return AdmissionSettings()


class UsageSettings(ConfiguredSettings):
    """The usage report's retention window for the per-request records.

    Retention is report policy: the report owner keeps records from the current hour back this
    many days and refuses a requested window longer than that. The serving process never reads it.
    """

    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    usage_retention_days: int = Field(validation_alias="USAGE_RETENTION_DAYS", gt=0)


def load_usage_settings() -> UsageSettings:
    """Read usage retention through the automatic subsystem-file and environment-file
    composition."""
    return UsageSettings()
