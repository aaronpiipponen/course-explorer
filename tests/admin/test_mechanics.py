"""Contracts for the administrative request timer and shared process owners."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from course_explorer_admin import machine_locks
from course_explorer_admin.mechanics import (
    SubprocessRunner,
    active_seconds,
    exclusive_action_lock,
    model_request_scope,
)


def test_model_request_scope_expires_and_restores_the_alarm() -> None:
    """A stalled body raises the safe transport timeout and leaves no armed timer behind."""
    with pytest.raises(httpx.ReadTimeout):
        with model_request_scope(0.05):
            time.sleep(1.0)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

    # A normal request block disarms the timer before further work runs.
    with model_request_scope(1.0):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_exclusive_action_lock_rejects_concurrent_action(tmp_path: Path) -> None:
    """A second concurrent action fails. ``create=False`` never initializes state but contends."""
    root = tmp_path / "state"
    with exclusive_action_lock(root, controller_name="embedding"):
        assert root.is_dir()
        assert oct(root.stat().st_mode & 0o777) == "0o700"
        assert (root / "controller.lock").is_file()
        with pytest.raises(RuntimeError):
            with exclusive_action_lock(root, controller_name="embedding"):
                pytest.fail("second lock must not be acquired")

    with exclusive_action_lock(root, controller_name="embedding"):
        pass

    absent = tmp_path / "absent"
    with exclusive_action_lock(absent, controller_name="embedding", create=False):
        pass
    assert not absent.exists()

    before = (root / "controller.lock").stat()
    assert oct(before.st_mode & 0o777) == "0o600"
    with exclusive_action_lock(root, controller_name="embedding", create=False):
        with pytest.raises(RuntimeError):
            with exclusive_action_lock(root, controller_name="embedding", create=False):
                pytest.fail("read-only lock must still contend on an existing lock")
    after = (root / "controller.lock").stat()
    assert (before.st_ino, before.st_size, oct(after.st_mode & 0o777)) == (
        after.st_ino,
        after.st_size,
        oct(before.st_mode & 0o777),
    )


def test_machine_reservations_are_persistent_and_owner_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file per reservation names its owner, refuses another owner and persists across calls."""
    shared = tmp_path / "shared"
    owner = machine_locks.Reservation(
        instance_key="hanken", operation="ingestion", session_id="a" * 32
    )
    other = machine_locks.Reservation(
        instance_key="humak", operation="embedding", session_id="b" * 32
    )
    checkout = shared / "locks" / "checkout-hanken.lock"

    assert machine_locks.reservation_path(shared, "checkout", instance_key="hanken") == checkout
    assert machine_locks.reservation_path(shared, "server", instance_key="hanken") == (
        shared / "locks" / "server.lock"
    )
    assert machine_locks.reservation_path(shared, "worker", instance_key="hanken") == (
        shared / "locks" / "worker.lock"
    )
    assert machine_locks.read(shared, "checkout", instance_key="hanken") is None
    assert not (shared / "locks").exists()

    assert machine_locks.acquire(shared, "checkout", instance_key="hanken", owner=owner) is True
    assert checkout.is_file()
    assert oct((shared / "locks").stat().st_mode & 0o777) == "0o700"
    assert oct(checkout.stat().st_mode & 0o777) == "0o600"
    assert machine_locks.read(shared, "checkout", instance_key="hanken") == owner

    # A same-session re-reserve keeps the file. A one-shot acquire never treats it as free.
    assert (
        machine_locks.acquire(
            shared, "checkout", instance_key="hanken", owner=owner, allow_existing=True
        )
        is False
    )
    assert machine_locks.read(shared, "checkout", instance_key="hanken") == owner
    with pytest.raises(RuntimeError, match="already held by ingestion in hanken"):
        machine_locks.acquire(shared, "checkout", instance_key="hanken", owner=owner)
    with pytest.raises(RuntimeError, match="held by ingestion in hanken"):
        machine_locks.acquire(shared, "checkout", instance_key="hanken", owner=other)

    # A one-shot reservation refuses a pre-existing file even for the same owner.
    with pytest.raises(RuntimeError, match="already held by ingestion in hanken"):
        with machine_locks.reservation(shared, "checkout", instance_key="hanken", owner=owner):
            pytest.fail("a pre-existing reservation must not be adopted")
    assert machine_locks.read(shared, "checkout", instance_key="hanken") == owner

    # A non-owner release refuses and leaves the file. The owner release removes it.
    with pytest.raises(RuntimeError, match="held by ingestion in hanken"):
        machine_locks.release(shared, "checkout", instance_key="hanken", owner=other)
    assert checkout.is_file()
    machine_locks.release(shared, "checkout", instance_key="hanken", owner=owner)
    assert not checkout.exists()
    machine_locks.release(shared, "checkout", instance_key="hanken", owner=owner)

    # The one-shot context manager releases only the file it created.
    with machine_locks.reservation(shared, "server", instance_key="hanken", owner=owner):
        assert machine_locks.read(shared, "server", instance_key="hanken") == owner
    assert machine_locks.read(shared, "server", instance_key="hanken") is None

    # A file left empty by a crash refuses clearly and is removed only by the operator.
    worker_lock = shared / "locks" / "worker.lock"
    worker_lock.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="incomplete or unreadable"):
        machine_locks.read(shared, "worker", instance_key="hanken")
    with pytest.raises(RuntimeError, match="incomplete or unreadable"):
        machine_locks.acquire(shared, "worker", instance_key="hanken", owner=owner)
    with pytest.raises(RuntimeError, match="incomplete or unreadable"):
        machine_locks.release(shared, "worker", instance_key="hanken", owner=owner)
    assert worker_lock.is_file()

    # The worker adapter refuses an argument that is not a configured university key and writes
    # nothing outside the locks directory.
    monkeypatch.setattr(
        machine_locks,
        "load_shared_storage_settings",
        lambda: SimpleNamespace(shared_root=shared),
    )
    before = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    with pytest.raises(
        ValueError, match="reservation instance key must be a configured university key"
    ):
        machine_locks.main(["read", "worker", "../escape", "translation"])
    assert sorted(path for path in tmp_path.rglob("*") if path.is_file()) == before


def test_subprocess_runner_popen_uses_devnull_mode_0600_logs_and_its_own_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A detached child gets DEVNULL stdin, mode-0600 append logs, and its own session."""
    stdout_path = tmp_path / "runner.out"
    stderr_path = tmp_path / "runner.err"
    script = "import os, sys; print(os.getsid(0) == os.getpid()); print(sys.stdin.read())"
    process = SubprocessRunner().popen(
        (sys.executable, "-c", script),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        start_new_session=True,
    )
    assert process.wait() == 0
    assert stdout_path.read_text(encoding="utf-8").splitlines() == ["True", ""]
    assert stderr_path.read_text(encoding="utf-8") == ""
    assert oct(stdout_path.stat().st_mode & 0o777) == "0o600"
    assert oct(stderr_path.stat().st_mode & 0o777) == "0o600"

    # A repeated launch appends to the same mode-0600 log rather than truncating it.
    SubprocessRunner().popen(
        (sys.executable, "-c", "print('line')"),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    ).wait()
    assert stdout_path.read_text(encoding="utf-8") == "True\n\nline\n"

    # A model-style launch with ``inherit_environment=False`` receives only the explicit mapping.
    monkeypatch.setenv("SYNTHETIC_PARENT_ONLY", "parent-value")
    env_stdout = tmp_path / "env.out"
    env_stderr = tmp_path / "env.err"
    env_script = (
        "import os; "
        "print(os.environ.get('SYNTHETIC_EXPLICIT', 'missing')); "
        "print(os.environ.get('SYNTHETIC_PARENT_ONLY', 'absent'))"
    )
    env_process = SubprocessRunner().popen(
        (sys.executable, "-c", env_script),
        env={"SYNTHETIC_EXPLICIT": "explicit-value"},
        stdout_path=env_stdout,
        stderr_path=env_stderr,
        inherit_environment=False,
    )
    assert env_process.wait() == 0
    assert env_stdout.read_text(encoding="utf-8").splitlines() == ["explicit-value", "absent"]
    assert oct(env_stdout.stat().st_mode & 0o777) == "0o600"

    # A null output path inherits this process's stream instead of opening a detached log file.
    inherited = SubprocessRunner().popen(
        (sys.executable, "-c", "print('inherited line')"),
        stdout_path=None,
        stderr_path=None,
    )
    assert inherited.wait() == 0
    assert "inherited line" in capfd.readouterr().out


def test_subprocess_runner_run_captures_output_and_honors_files(tmp_path: Path) -> None:
    """The foreground runner captures stdout and redirects it to a mode-0600 file on request."""
    completed = SubprocessRunner().run((sys.executable, "-c", "print('hello')"))
    assert completed.stdout == b"hello\n"

    destination = tmp_path / "captured.txt"
    SubprocessRunner().run((sys.executable, "-c", "print('redirected')"), stdout_path=destination)
    assert destination.read_text(encoding="utf-8") == "redirected\n"
    assert oct(destination.stat().st_mode & 0o777) == "0o600"

    with pytest.raises(subprocess.CalledProcessError):
        SubprocessRunner().run((sys.executable, "-c", "raise SystemExit(3)"))


def test_active_session_time_holds_at_zero_across_a_backward_clock_step() -> None:
    """A backward host clock step contributes no elapsed time, so the state stays publishable."""
    running_since = datetime(2026, 11, 18, 2, 48, 18, tzinfo=UTC)
    earlier = running_since - timedelta(seconds=5)
    assert active_seconds(10.0, running_since, running=True, now=earlier) == 10.0
    later = running_since + timedelta(seconds=2)
    assert active_seconds(10.0, running_since, running=True, now=later) == 12.0
