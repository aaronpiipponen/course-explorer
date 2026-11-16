"""Per-worker libpq fact-handle lifecycle for the native prerequisite compiler.

The native library keeps one loaded fact bundle per open handle, so a serving worker opens one
handle for its serving database and reuses it for the same libpq connection string built from the
connection settings. This module owns that handle and the connection string it was opened from.
"""

from __future__ import annotations

from course_explorer.config import DatabaseSettings, load_instance_settings
from course_explorer.instance import UniversityMetadata
from course_explorer.prerequisite.native_compiler import _NATIVE_LOCK, _load

_WORKER_HANDLE: int | None = None
_WORKER_CONNINFO: str | None = None


def database_conninfo(settings: DatabaseSettings) -> str:
    """Build one libpq connection string from the owning database settings."""

    def quoted(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return (
        f"host={quoted(settings.postgres_host)} port={settings.postgres_port} "
        f"dbname={quoted(settings.postgres_db)} user={quoted(settings.postgres_user)} "
        f"password={quoted(settings.postgres_password.get_secret_value())}"
    )


def _text_array(values: tuple[str, ...]) -> str:
    """Serialize one validated official-language sequence as a PostgreSQL text array literal."""
    return "{" + ",".join(values) + "}"


def worker_handle(settings: DatabaseSettings) -> int:
    """Return this worker's one cached native fact handle for the serving database."""
    global _WORKER_HANDLE, _WORKER_CONNINFO
    conninfo = database_conninfo(settings)
    with _NATIVE_LOCK:
        if _WORKER_HANDLE is not None and _WORKER_CONNINFO == conninfo:
            return _WORKER_HANDLE
        reset_worker_handle()
        _WORKER_HANDLE = open_database(conninfo)
        _WORKER_CONNINFO = conninfo
        return _WORKER_HANDLE


def open_database(conninfo: str, *, university: UniversityMetadata | None = None) -> int:
    """Open one per-worker database fact handle with the configured display policy.

    The supplied university, or the configured one, contributes its two validated fallback
    sequences as PostgreSQL text-array parameters consumed by the scoped name reads.
    """
    policy = load_instance_settings().university if university is None else university
    display_fi = _text_array(policy.display_fallback["fi"])
    display_en = _text_array(policy.display_fallback["en"])
    with _NATIVE_LOCK:
        handle = _load().course_prerequisite_open(
            conninfo.encode(), display_fi.encode(), display_en.encode()
        )
        if handle < 0:
            message = _load().course_prerequisite_last_error()
            raise ValueError(message.decode() if message else "native database loader failed")
        return handle


def close_database(handle: int) -> None:
    """Close one per-worker database fact handle."""
    with _NATIVE_LOCK:
        _load().course_prerequisite_close(handle)


def reset_worker_handle() -> None:
    """Close and forget this process's cached fact handle after its database facts change."""
    global _WORKER_HANDLE, _WORKER_CONNINFO
    with _NATIVE_LOCK:
        if _WORKER_HANDLE is not None:
            close_database(_WORKER_HANDLE)
        _WORKER_HANDLE = None
        _WORKER_CONNINFO = None
