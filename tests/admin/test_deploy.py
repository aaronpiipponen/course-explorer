"""Contract for the container deployment owner and its stage-idle guard.

A busy inference lifecycle record proves that the guard runs before any build, migration or service
start, and a same-machine configuration adds the worker image and container to the deployment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from course_explorer import administration
from course_explorer_admin import deploy
from course_explorer_admin.lifecycle import InferenceProgress, LifecycleState, write_lifecycle_state
from tests.config._support import set_administrative_controller_environment


class _Runner:
    """Record every command the deployment would run and answer each one successfully."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.interactive_calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None, stdin_path=None, stdout_path=None, timeout=None):
        command = tuple(argv)
        self.calls.append(command)
        return subprocess.CompletedProcess(list(command), 0, stdout=b"", stderr=b"")

    def run_interactive(self, argv, *, cwd=None, env=None, timeout=None):
        command = tuple(argv)
        self.interactive_calls.append(command)
        self.run(command, cwd=cwd, env=env, timeout=timeout)


def test_deploy_refuses_busy_stages_before_mutation_and_builds_the_same_machine_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A busy inference stage stops the deployment before any build, migration or start, and a
    same-machine configuration builds and starts the worker once, with the site group."""
    root = tmp_path / "checkout"
    root.mkdir()
    admin_state = "data/admin-tests/deploy-guard"
    set_administrative_controller_environment(
        monkeypatch,
        ADMIN_STATE_ROOT=admin_state,
        INFERENCE_CONTROLLER_STATE_ROOT=f"{admin_state}/inference",
    )
    monkeypatch.setattr(administration, "repository_root", lambda: root)
    stage_root = root / admin_state / "inference" / "current"
    stage_root.mkdir(parents=True)
    write_lifecycle_state(
        stage_root,
        LifecycleState(
            session_id="e" * 32,
            lifecycle="running",
            requested_action="run",
            progress=InferenceProgress(kind="inference"),
            work_complete=False,
            active_seconds=0.0,
        ),
    )
    runner = _Runner()

    with pytest.raises(ValueError, match="stage inference is not idle"):
        deploy.deploy_containers(runner=runner, write=lambda message: None)

    assert runner.calls == []
    assert runner.interactive_calls == []

    # The same configuration builds and starts the site, administration and sender services and
    # the same-machine worker, so the server images recreate the worker container once.
    monkeypatch.setattr(deploy.datasets, "require_stages_idle", lambda: None)
    monkeypatch.setattr(
        deploy.commands,
        "run_administrative_dataset_ensure",
        lambda *, runner: SimpleNamespace(mode="enrolled"),
    )
    monkeypatch.setattr(deploy.commands, "run_administrative_migration", lambda **_: None)
    monkeypatch.setattr(deploy.commands, "run_administrative_serving_artifacts", lambda **_: None)
    monkeypatch.setattr(deploy.self_host, "prove_shells", lambda: None)
    monkeypatch.setattr(
        deploy.mechanics,
        "running_compose_services",
        lambda run, interpreter=False: {"db", "embedding", "sender"},
    )
    monkeypatch.setenv("WORKER_HOST_KIND", "same-machine")
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/app")
    monkeypatch.delenv("WORKER_SSH_TARGET", raising=False)
    monkeypatch.delenv("WORKER_WSL_DISTRIBUTION", raising=False)
    working = _Runner()
    lines: list[str] = []

    deploy.deploy_containers(runner=working, write=lines.append)

    first_build = next(
        call for call in working.interactive_calls if "build" in call and "db" in call
    )
    assert first_build[-2:] == ("db", "embedding")
    first_up = next(
        call
        for call in working.calls
        if "up" in call and "db" in call and "--force-recreate" in call
    )
    assert first_up[-2:] == ("db", "embedding")
    second_build = next(
        call for call in working.interactive_calls if "build" in call and "admin" in call
    )
    assert second_build.count("worker") == 1
    assert second_build[-1] == "worker"
    second_up = next(
        call
        for call in working.calls
        if "up" in call and "admin" in call and "--force-recreate" in call
    )
    assert second_up.count("worker") == 1
    assert second_up[-2:] == ("sender", "worker")
    # The step lines name the services this deployment builds and starts.
    assert "build web, admin, sender, worker images" in lines
    assert "start web, admin, sender, worker services" in lines
