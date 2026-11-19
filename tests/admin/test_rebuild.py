"""Contracts for the fixed rebuild of the site image, the containers, the site and its bind, and for
the administration image build and its own re-create.

One small process-boundary double answers the fixed Git and Compose reads, so no real image build or
container change ever runs. Every parameter performs exactly one rebuild through the one entry point
and asserts the executed bridge commands, the first-failure cutoff and that nothing beyond the fixed
prepared steps was reached. The administration commands are proven separately. One invocation
performs only its own build or re-create of this checkout's own administration service.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from course_explorer import administration
from course_explorer_admin import machine_locks, mechanics, rebuild
from tests.admin._rebuild_support import (
    _BUILD_ADMIN,
    _BUILD_SITE,
    _RECREATE_ADMIN,
    _RECREATE_CONTAINERS,
    _RECREATE_WEB,
    _SENTINEL,
    _WEB_HOST,
    _WEB_PORT,
    _WEB_WORKERS,
    _RebuildProcessBoundary,
)

_STOP_TIMEOUT_SECONDS = "77"
_STARTUP_ATTEMPTS = "3"
_POLL_INTERVAL_SECONDS = "0.01"
_BUILD_BRIDGE = mechanics.compose_prefix(interpreter=True)
_FIXED_BRIDGE = mechanics.compose_prefix(non_interactive=True, interpreter=True)


def _bind_rebuild_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Bind the site bind, the timing policy and the checkout."""
    monkeypatch.setenv("WEB_HOST", _WEB_HOST)
    monkeypatch.setenv("WEB_PORT", str(_WEB_PORT))
    monkeypatch.setenv("WEB_WORKERS", str(_WEB_WORKERS))
    monkeypatch.setenv("WEB_APP_URL", f"http://localhost:{_WEB_PORT}/")
    monkeypatch.setenv("ADMIN_STOP_TIMEOUT_SECONDS", _STOP_TIMEOUT_SECONDS)
    monkeypatch.setenv("ADMIN_STARTUP_ATTEMPTS", _STARTUP_ATTEMPTS)
    monkeypatch.setenv("ADMIN_POLL_INTERVAL_SECONDS", _POLL_INTERVAL_SECONDS)
    monkeypatch.setenv("INSTANCE_KEY", "hanken")
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(tmp_path / "shared"))
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(administration, "repository_root", lambda: root)
    return root


def _bind_web(monkeypatch: pytest.MonkeyPatch, *, served: bool) -> None:
    """Route the site proof's one HTTP read through a fixed response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        if served:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<!doctype html><title>feature</title>",
            )
        return httpx.Response(503, headers={"content-type": "text/html; charset=utf-8"}, text="")

    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _commands(boundary: _RebuildProcessBoundary) -> list[tuple[str, ...]]:
    return [command for command, _ in boundary.calls]


@pytest.mark.parametrize(
    ("case", "fail_step"),
    [
        ("success", None),
        ("build-failure", "image"),
        ("container-failure", "containers"),
        ("web-failure", "web"),
        ("bind-failure", None),
    ],
)
def test_rebuild_runs_only_the_remaining_fixed_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    fail_step: str | None,
) -> None:
    """One rebuild stops at its first failure and otherwise proves its own loopback bind."""
    root = _bind_rebuild_roots(monkeypatch, tmp_path)
    boundary = _RebuildProcessBoundary(fail_step=fail_step)
    _bind_web(monkeypatch, served=case != "bind-failure")

    if case == "success":
        report = rebuild.rebuild_site(runner=boundary)
        assert report.steps == ("image", "containers", "web", "bind")
        assert (report.host, report.port) == (_WEB_HOST, _WEB_PORT)
    elif case == "bind-failure":
        with pytest.raises(RuntimeError):
            rebuild.rebuild_site(runner=boundary)
    else:
        with pytest.raises(subprocess.CalledProcessError) as failed:
            rebuild.rebuild_site(runner=boundary)
        assert failed.value.returncode == 1
        assert _SENTINEL not in str(failed.value)

    expected = {
        "success": [
            (*_BUILD_BRIDGE, *_BUILD_SITE),
            (*_FIXED_BRIDGE, *_RECREATE_CONTAINERS),
            (*_FIXED_BRIDGE, *_RECREATE_WEB),
        ],
        "build-failure": [(*_BUILD_BRIDGE, *_BUILD_SITE)],
        "container-failure": [
            (*_BUILD_BRIDGE, *_BUILD_SITE),
            (*_FIXED_BRIDGE, *_RECREATE_CONTAINERS),
        ],
        "web-failure": [
            (*_BUILD_BRIDGE, *_BUILD_SITE),
            (*_FIXED_BRIDGE, *_RECREATE_CONTAINERS),
            (*_FIXED_BRIDGE, *_RECREATE_WEB),
        ],
        "bind-failure": [
            (*_BUILD_BRIDGE, *_BUILD_SITE),
            (*_FIXED_BRIDGE, *_RECREATE_CONTAINERS),
            (*_FIXED_BRIDGE, *_RECREATE_WEB),
        ],
    }[case]
    assert _commands(boundary) == expected

    # The two image builds are unbounded, and each container step is bounded by the configured
    # foreground subprocess ceiling.
    timeouts = dict(boundary.calls)
    assert timeouts[(*_BUILD_BRIDGE, *_BUILD_SITE)] is None
    for command in expected:
        if command != (*_BUILD_BRIDGE, *_BUILD_SITE):
            assert timeouts[command] == float(_STOP_TIMEOUT_SECONDS)

    # No unit, listener, npm or volume command runs.
    assert all(
        command[0] not in {"systemctl", "ss", "npm", "sudo", "docker", "du", "pkill"}
        for command in _commands(boundary)
    )
    assert all(
        not (set(command) & {"volume", "down", "rm", "prune"}) for command in _commands(boundary)
    )

    # The rebuild retains no receipt, state file or any other file in the checkout.
    assert list(root.rglob("*")) == []


def test_rebuild_is_refused_while_a_preparation_holds_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A held checkout reservation refuses the rebuild before any image or Compose command."""
    _bind_rebuild_roots(monkeypatch, tmp_path)
    boundary = _RebuildProcessBoundary()
    _bind_web(monkeypatch, served=True)
    shared = tmp_path / "shared"
    holder = machine_locks.Reservation(
        instance_key="hanken", operation="ingestion", session_id="c" * 32
    )
    assert machine_locks.acquire(shared, "checkout", instance_key="hanken", owner=holder) is True
    try:
        with pytest.raises(RuntimeError, match="held by ingestion in hanken"):
            rebuild.rebuild_site(runner=boundary)
        assert boundary.calls == []
    finally:
        machine_locks.release(shared, "checkout", instance_key="hanken", owner=holder)

    # The release removes the lock file, and the rebuild runs its four steps.
    assert not (shared / "locks" / "checkout-hanken.lock").exists()
    report = rebuild.rebuild_site(runner=boundary)
    assert report.steps == ("image", "containers", "web", "bind")


@pytest.mark.parametrize("action", ["build", "restart"])
@pytest.mark.parametrize("case", ["success", "step-failure"])
def test_admin_maintenance_commands_act_only_on_their_own_image_and_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, case: str
) -> None:
    """One administration build or re-create mutates this checkout's own image and service alone."""
    root = _bind_rebuild_roots(monkeypatch, tmp_path)
    boundary = _RebuildProcessBoundary(
        fail_admin_build=case == "step-failure" and action == "build",
        fail_admin_restart=case == "step-failure" and action == "restart",
    )
    command = rebuild.rebuild_admin if action == "build" else rebuild.restart_admin

    if case == "success":
        report = command(runner=boundary)
        if action == "build":
            assert report.steps == ("image",)
            assert report.schema_version == 1
        else:
            assert report.restart_queued is True
            assert report.schema_version == 1
    else:
        with pytest.raises(subprocess.CalledProcessError) as failed:
            command(runner=boundary)
        assert failed.value.returncode == 1
        assert _SENTINEL not in str(failed.value)

    expected = {
        "build": [(*_BUILD_BRIDGE, *_BUILD_ADMIN)],
        "restart": [(*_FIXED_BRIDGE, *_RECREATE_ADMIN)],
    }[action]
    assert _commands(boundary) == expected

    if action == "build":
        assert boundary.interactive_calls == [(*_BUILD_BRIDGE, *_BUILD_ADMIN)]
        assert dict(boundary.calls)[(*_BUILD_BRIDGE, *_BUILD_ADMIN)] is None
    else:
        assert boundary.interactive_calls == []
        assert dict(boundary.calls)[(*_FIXED_BRIDGE, *_RECREATE_ADMIN)] == float(
            _STOP_TIMEOUT_SECONDS
        )

    # Neither command reads a unit, a listener or Git metadata.
    assert all(
        command[0]
        not in {
            "systemctl",
            "ss",
            "git",
            "du",
            "ssh",
            "wsl.exe",
            "docker",
            "pkill",
            "sudo",
            "uv",
            "npm",
        }
        for command in _commands(boundary)
    )

    # The administration commands retain no receipt, state file or any other file in the checkout.
    assert list(root.rglob("*")) == []
