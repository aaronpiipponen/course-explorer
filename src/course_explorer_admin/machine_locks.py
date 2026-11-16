"""The one owner of the machine-wide checkout, server and worker reservations and their files.

A reservation is one small strict record in the machine shared directory. It names the instance
and operation that hold the protected work, and the next reservation attempt reads it to refuse and
name the holder. A checkout reservation covers one instance's preparation stages, its Working
dataset operations and an app rebuild, a server reservation covers ingestion and embedding on this
server, and a worker reservation covers translation and inference on the worker.

The file is the reservation itself, not an audit record. No timestamp, process identity, boot
identity, lease or history is stored, and nothing expires a reservation.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from course_explorer.administration import administrative_json, load_private_model
from course_explorer.config import load_instance_settings, load_shared_storage_settings
from course_explorer.instance import UNIVERSITIES

type LockName = Literal["checkout", "server", "worker"]

_LOCK_NAMES: tuple[LockName, ...] = ("checkout", "server", "worker")


class Reservation(BaseModel):
    """One persistent reservation's owner identity and protected operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    instance_key: str
    operation: str
    session_id: str | None = None


def reservation_path(shared_root: Path, name: LockName, *, instance_key: str) -> Path:
    """Return one reservation's path under the machine shared directory."""
    if name == "checkout":
        return shared_root / "locks" / f"checkout-{instance_key}.lock"
    if name == "server":
        return shared_root / "locks" / "server.lock"
    return shared_root / "locks" / "worker.lock"


def _read_held(path: Path, name: LockName) -> Reservation:
    """Read one existing reservation, refusing an unreadable or incomplete file clearly.

    A process killed between the exclusive create and its write leaves a file without an owner, so
    the refusal names the lock and the operator action instead of an owner that no record holds.
    """
    try:
        return load_private_model(path, Reservation)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} reservation file is incomplete or unreadable; remove it after confirming "
            "its operation is gone"
        ) from exc


def acquire(
    shared_root: Path,
    name: LockName,
    *,
    instance_key: str,
    owner: Reservation,
    allow_existing: bool = False,
) -> bool:
    """Create one reservation and return whether this call created its file.

    The lock directory is created at mode 0700 and the file at mode 0600. An existing file is read
    strictly: a different owner raises, an identical owner with ``allow_existing=True`` returns
    ``False`` and leaves the file, and an identical owner without it raises because a one-shot
    reservation is never treated as free. A write failure unlinks the file this call created.
    """
    payload = administrative_json(owner)
    path = reservation_path(shared_root, name, instance_key=instance_key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = _read_held(path, name)
        if existing != owner:
            raise RuntimeError(
                f"{name} reservation is held by {existing.operation} in {existing.instance_key}"
            ) from None
        if allow_existing:
            return False
        raise RuntimeError(
            f"{name} reservation is already held by {owner.operation} in {owner.instance_key}"
        ) from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return True


def release(shared_root: Path, name: LockName, *, instance_key: str, owner: Reservation) -> None:
    """Remove one reservation this owner holds.

    An absent file is a no-op. A file held by a different owner raises and is left in place.
    """
    path = reservation_path(shared_root, name, instance_key=instance_key)
    if not path.exists() and not path.is_symlink():
        return
    existing = _read_held(path, name)
    if existing != owner:
        raise RuntimeError(
            f"{name} reservation is held by {existing.operation} in {existing.instance_key}"
        )
    path.unlink()


@contextmanager
def reservation(
    shared_root: Path, name: LockName, *, instance_key: str, owner: Reservation
) -> Iterator[None]:
    """Hold one one-shot reservation around a block of work.

    A pre-existing file refuses before the wrapped work, whatever its owner. Only the file this
    block created is released.
    """
    created = acquire(shared_root, name, instance_key=instance_key, owner=owner)
    try:
        yield
    finally:
        if created:
            release(shared_root, name, instance_key=instance_key, owner=owner)


def configured_owner(operation: str, session_id: str | None = None) -> Reservation:
    """Return the configured instance's owner record for one operation."""
    return Reservation(
        instance_key=load_instance_settings().instance_key,
        operation=operation,
        session_id=session_id,
    )


@contextmanager
def checkout_reservation(operation: str) -> Iterator[None]:
    """Hold the configured instance's checkout reservation around a block of work.

    A held reservation refuses before the wrapped work, and an absent file makes the release a
    no-op.
    """
    owner = configured_owner(operation)
    with reservation(
        load_shared_storage_settings().shared_root,
        "checkout",
        instance_key=owner.instance_key,
        owner=owner,
    ):
        yield


def read(shared_root: Path, name: LockName, *, instance_key: str) -> Reservation | None:
    """Read one reservation strictly, or return ``None`` when no file exists."""
    path = reservation_path(shared_root, name, instance_key=instance_key)
    if not path.exists() and not path.is_symlink():
        return None
    return _read_held(path, name)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one worker-side reservation command over the configured shared directory.

    The commands are ``acquire``, ``release`` and ``read``, each followed by the lock name, the
    instance key, the operation and an optional session id. ``read`` prints the held owner record,
    and the other two print nothing.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) not in {4, 5}:
        raise ValueError(
            "usage: machine_locks {acquire|release|read} {checkout|server|worker} "
            "INSTANCE_KEY OPERATION [SESSION_ID]"
        )
    command, raw_name, instance_key, operation = arguments[:4]
    if raw_name not in _LOCK_NAMES:
        raise ValueError("reservation name must be checkout, server or worker")
    name: LockName = raw_name
    if instance_key not in UNIVERSITIES:
        raise ValueError("reservation instance key must be a configured university key")
    session_id = arguments[4] if len(arguments) == 5 else None
    shared_root = load_shared_storage_settings().shared_root
    owner = Reservation(instance_key=instance_key, operation=operation, session_id=session_id)
    if command == "read":
        held = read(shared_root, name, instance_key=instance_key)
        if held is not None:
            sys.stdout.write(administrative_json(held))
        return 0
    if command == "acquire":
        acquire(shared_root, name, instance_key=instance_key, owner=owner, allow_existing=True)
        return 0
    if command == "release":
        release(shared_root, name, instance_key=instance_key, owner=owner)
        return 0
    raise ValueError("reservation command must be acquire, release or read")


if __name__ == "__main__":
    raise SystemExit(main())
