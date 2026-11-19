"""Contracts for the shared product-file administrative primitives."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from course_explorer.administration import (
    administrative_json,
    atomic_write_private_text,
    load_private_model,
    rewrite_private_text_in_place,
    session_directory,
)


class _Probe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    count: int


def test_administrative_json_and_model_round_trip(tmp_path: Path) -> None:
    """A strict loader round-trips a canonical write and rejects a foreign schema and a symlink."""
    probe = _Probe(name="alpha", count=2)
    payload = administrative_json(probe)

    target = tmp_path / "probe.json"
    atomic_write_private_text(target, payload)
    assert load_private_model(target, _Probe) == probe

    foreign = tmp_path / "foreign.json"
    atomic_write_private_text(foreign, '{"count":2,"name":"alpha","extra":1}\n')
    with pytest.raises(ValueError):
        load_private_model(foreign, _Probe)

    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        load_private_model(link, _Probe)


def test_atomic_write_is_mode_0600_and_rejects_missing_parent(tmp_path: Path) -> None:
    """Publication is atomic, mode 0600, and refuses an absent parent."""
    directory = tmp_path / "directory"
    directory.mkdir()
    target = directory / "state.json"
    atomic_write_private_text(target, "payload\n")
    assert target.read_text(encoding="utf-8") == "payload\n"
    assert oct(target.stat().st_mode & 0o777) == "0o600"

    atomic_write_private_text(target, "replacement\n")
    assert target.read_text(encoding="utf-8") == "replacement\n"
    assert list(directory.iterdir()) == [target]

    with pytest.raises(ValueError):
        atomic_write_private_text(directory / "missing" / "state.json", "payload\n")


def test_in_place_rewrite_keeps_the_inode_mode_and_truncates(tmp_path: Path) -> None:
    """The mounted-file rewrite keeps the file's own inode, applies mode 0600 and truncates it.

    A symlink target and a blank payload are refused.
    """
    # The path avoids the temporary repository root's linked checkout entries.
    target = tmp_path / "mounted.env"
    target.write_text("POSTGRES_PASSWORD='synthetic-longer-password'\n", encoding="utf-8")
    target.chmod(0o644)
    inode = target.stat().st_ino

    rewrite_private_text_in_place(target, "POSTGRES_PASSWORD='synthetic-password'\n")

    assert target.stat().st_ino == inode
    assert target.read_text(encoding="utf-8") == "POSTGRES_PASSWORD='synthetic-password'\n"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".mounted.env-")]

    with pytest.raises(ValueError):
        rewrite_private_text_in_place(target, "")

    link = tmp_path / "mounted-link.env"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        rewrite_private_text_in_place(link, "payload\n")


def test_session_directory_rejects_invalid_or_escaping_identity(tmp_path: Path) -> None:
    """Only a 32-lowercase-hex identity inside the state root produces a session path."""
    root = tmp_path / "state"
    root.mkdir()
    session_id = "0123456789abcdef0123456789abcdef"
    assert session_directory(root, session_id) == root / session_id

    with pytest.raises(ValueError):
        session_directory(root, "../" + session_id)

    # A symlinked state root is resolved to its real directory. The identity itself is the
    # only thing that could escape and it is restricted to hex.
    real = tmp_path / "outside"
    real.mkdir()
    linked = tmp_path / "state-link"
    linked.symlink_to(real)
    assert session_directory(linked, session_id) == real / session_id
