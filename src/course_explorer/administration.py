"""The file, clock and path primitives shared by the administration and serving code.

Long-running administrative controllers need one set of file, clock and path primitives: canonical
one-line JSON, atomic publication for a file this process owns, an in-place rewrite for a file that
is a single-file bind mount, strict regular-file state loading, confined session directories and one
shared UTC clock default. Those primitives live here so every caller keeps its own closed DTOs and
lifecycle decisions, while process ownership, Compose orchestration, action locking and subprocess
transport live in the sibling ``course_explorer_admin`` package. Every primitive here takes its
environment and state locations from the caller.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from course_explorer.hashing import canonical_json_bytes

_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time as the shared default clock."""
    return datetime.now(UTC)


def administrative_json(model: BaseModel) -> str:
    """Render one canonical single-line UTF-8 JSON document plus a trailing newline."""
    return (
        canonical_json_bytes(model.model_dump(mode="json"), ensure_ascii=False).decode("utf-8")
        + "\n"
    )


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of one file's exact bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repository_root() -> Path:
    """Return the repository root that owns ignored local controller state."""
    return Path(__file__).resolve().parents[2]


def session_directory(state_root: Path, session_id: str) -> Path:
    """Derive one session directory without creating it or escaping the state root."""
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("session id must be 32 lowercase hexadecimal characters")
    root = state_root.resolve()
    candidate = (root / session_id).resolve()
    if candidate != root / session_id or root not in candidate.parents:
        raise ValueError("session directory must stay inside the state root")
    return candidate


def load_private_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Strictly load one JSON model from a regular non-symlink file."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"state file must be a regular non-symlink file: {path.name}")
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"state file is not readable UTF-8: {path.name}") from exc
    try:
        return model_type.model_validate_json(payload)
    except ValueError as exc:
        raise ValueError(f"state file does not match its closed schema: {path.name}") from exc


def rewrite_private_text_in_place(path: Path, payload: str, *, mode: int = 0o600) -> None:
    """Rewrite one text file through its own inode, then apply its owner-only permission bits.

    A subsystem file this process owns is published atomically by :func:`atomic_write_private_text`.
    An environment file can be a single-file bind mount, which can be neither replaced nor renamed
    over, so the atomic directory write cannot publish it. The caller compares the file's device
    with its directory's device and rewrites the file through its own inode: opened, written,
    truncated to the payload's length and synced. The rewrite takes no lock, so one operator and
    one writer keep it consistent, and a crash during the rewrite can leave a truncated file. The
    target must be a regular non-symlink file, the payload must be nonblank, and a write failure
    propagates.
    """
    if not isinstance(payload, str) or not payload:
        raise ValueError("payload must be a nonblank string")
    if path.is_symlink() or not path.is_file():
        raise ValueError("target file must be a regular non-symlink file")
    encoded = payload.encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.ftruncate(stream.fileno(), len(encoded))
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def atomic_write_private_text(path: Path, payload: str, *, mode: int = 0o600) -> None:
    """Publish one complete UTF-8 file atomically in its own directory.

    The permission bits default to 0600. A caller publishing a file that
    carries its own permission contract passes that mode. The parent directory must already exist,
    and the replacement is a same-directory temporary file that the call removes when it fails.
    """
    if not isinstance(payload, str) or not payload:
        raise ValueError("payload must be a nonblank string")
    if not path.parent.is_dir():
        raise ValueError("target directory must already exist")
    descriptor, temporary_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise
