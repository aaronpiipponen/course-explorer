"""Shared process double and synthetic site constants for the rebuild and promotion proofs.

The double answers every fixed Git and Compose read, and records each command in order. The site
re-create and the administration re-create are decided by their own flags, so one double serves the
site rebuild, the administration build and the administration re-create. A promotion proof opts
into forwarding the real Compose commands, and it can inject a bounded number of site re-create
failures so the caller's rollback re-create runs against the same boundary.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from course_explorer_admin import datasets as datasets_module
from course_explorer_admin import mechanics

_WEB_HOST = "127.0.0.1"
_WEB_PORT = 18026
_WEB_WORKERS = 2
_SENTINEL = "synthetic-failure-output"

# The forwards each rebuild command places after the Compose bridge prefix.
_BUILD_SITE = ("build", "web")
_BUILD_ADMIN = ("build", "admin")
_RECREATE_CONTAINERS = (
    "up",
    "-d",
    "--wait",
    "--force-recreate",
    "--no-build",
    "db",
    "embedding",
)
_RECREATE_WEB = ("up", "-d", "--wait", "--force-recreate", "--no-build", "web")
_RECREATE_ADMIN = (
    "run",
    "--rm",
    "-d",
    "--no-deps",
    "admin",
    "python",
    "-m",
    "course_explorer_admin.compose",
    "--non-interactive",
    "--",
    "up",
    "-d",
    "--wait",
    "--force-recreate",
    "--no-build",
    "admin",
)

_COMPOSE_PREFIXES = (
    mechanics.compose_prefix(non_interactive=True, interpreter=True),
    mechanics.compose_prefix(interpreter=True),
)


class _RebuildProcessBoundary:
    """Answer the fixed rebuild process reads and record every command in order."""

    def __init__(
        self,
        *,
        fail_step: str | None = None,
        fail_admin_build: bool = False,
        fail_admin_restart: bool = False,
        restart_failures: int = 0,
        real_compose: bool = False,
    ) -> None:
        self.fail_step = fail_step
        self.fail_admin_build = fail_admin_build
        self.fail_admin_restart = fail_admin_restart
        self.restart_failures = restart_failures
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        self.interactive_calls: list[tuple[str, ...]] = []
        self.restarted = False
        self.admin_recreate_started = False
        self._delegate = mechanics.SubprocessRunner() if real_compose else None

    @property
    def recreate_commands(self) -> list[tuple[str, ...]]:
        """Return every site re-create command this boundary attempted, in order."""
        return [command for command, _ in self.calls if self._forwarded(command) == _RECREATE_WEB]

    def run(
        self,
        argv: Any,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = tuple(argv)
        self.calls.append((command, timeout))
        forwarded = self._forwarded(command)
        if forwarded is None:
            raise AssertionError(f"the rebuild ran an unexpected process command: {command[0]}")
        if forwarded == _RECREATE_WEB:
            self._maybe_fail_recreate(command)
            self.restarted = True
            return self._completed(command, b"")
        if forwarded == _RECREATE_ADMIN:
            if self.fail_admin_restart or self.fail_step == "web":
                self._fail(command)
            self.admin_recreate_started = True
            return self._completed(command, b"")
        if forwarded == _BUILD_SITE:
            if self.fail_step == "image":
                self._fail(command)
            return self._completed(command, b"")
        if forwarded == _BUILD_ADMIN:
            if self.fail_admin_build:
                self._fail(command)
            return self._completed(command, b"")
        if forwarded == _RECREATE_CONTAINERS:
            if self.fail_step == "containers":
                self._fail(command)
            return self._completed(command, b"")
        if self._delegate is not None:
            return self._delegate.run(
                command,
                cwd=cwd,
                env=env,
                stdin_path=stdin_path,
                stdout_path=stdout_path,
                timeout=timeout,
            )
        raise AssertionError(f"the rebuild ran an unexpected Compose command: {command}")

    def run_interactive(
        self,
        argv: Any,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        command = tuple(argv)
        self.interactive_calls.append(command)
        self.run(command, cwd=cwd, env=env, timeout=timeout)

    def _forwarded(self, command: tuple[str, ...]) -> tuple[str, ...] | None:
        for prefix in _COMPOSE_PREFIXES:
            if command[: len(prefix)] == prefix:
                return command[len(prefix) :]
        return None

    def _maybe_fail_recreate(self, command: tuple[str, ...]) -> None:
        """Fail one site re-create the caller already recognized as that step."""
        if self.fail_step == "web":
            self._fail(command)
        if self.restart_failures > 0:
            self.restart_failures -= 1
            self._fail(command)

    def _fail(self, command: tuple[str, ...]) -> None:
        raise subprocess.CalledProcessError(
            1, list(command), output=_SENTINEL.encode(), stderr=_SENTINEL.encode()
        )

    def _completed(
        self, command: tuple[str, ...], stdout: bytes
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(command), 0, stdout=stdout, stderr=b"")


def bind_promotion_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Route the dataset owner's HTTP client through one mock transport.

    The saved constructor keeps every argument the production helper supplies, including its
    timeout and its trust-environment choice. Only the transport is added.
    """
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(datasets_module.httpx, "Client", factory)
