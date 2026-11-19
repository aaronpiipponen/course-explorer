"""Sole explicit worker code-update and checkout-identity owner.

This module owns the one server-side operation that brings the worker checkout onto a selected
GitHub-origin branch with ``git pull --ff-only`` and installs its locked project environment, and
the read-only preflight that reads the checkout identity and the worker resources. The worker's
packages, services, model files and runtimes are installed on the worker itself, so no server
operation runs a system package command. The update runs through the captured,
completion-reported transport, and the preflight never elevates. Code sync is separate from the
recurring translation/inference lifecycle, which validates worker schema and actual resources
before work. A same-machine worker's image is built from the server checkout, so its preflight
reports the server checkout's identity and its update is the server deployment. A same-machine
operation reads the worker container's paths as local paths, so it runs inside the administration
container and refuses elsewhere with one clear error.
"""

from __future__ import annotations

import shlex
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from course_explorer import administration
from course_explorer.config import (
    WorkerConnectionSettings,
    WorkerSetupSettings,
    load_worker_connection_settings,
    load_worker_setup_settings,
)
from course_explorer_admin import mechanics
from course_explorer_admin.provisioning_worker import WorkerResourcesReport
from course_explorer_admin.transport import (
    WorkerTransport,
    build_worker_transport,
)

_SSH_BATCH_COMMAND = "core.sshCommand=ssh -o BatchMode=yes"


def _repository_project_name() -> str:
    """Read this repository's own distribution name from its tracked pyproject metadata.

    The locked sync reinstalls this editable project so a pyproject-only change refreshes the
    installed distribution, which uv's editable-install freshness otherwise ignores.
    """
    metadata = tomllib.loads(
        (administration.repository_root() / "pyproject.toml").read_text(encoding="utf-8")
    )
    return metadata["project"]["name"]


_PROJECT_NAME = _repository_project_name()


class WorkerCodeReport(BaseModel):
    """Ordinary frozen report of one explicit worker code update or checkout-identity read."""

    model_config = ConfigDict(frozen=True)

    branch: str
    revision: str
    server_origin_differs: bool


class ProvisioningReport(BaseModel):
    """Ordinary frozen report of one setup/update/preflight operation."""

    model_config = ConfigDict(frozen=True)

    code: WorkerCodeReport
    resources: WorkerResourcesReport | None


def update_worker_code(
    *, branch: str = "main", runner: mechanics.ProcessRunner
) -> WorkerCodeReport:
    """Fast-forward the worker checkout to one origin branch, then sync its locked environment.

    The dependency sync installs only the locked project environment. The remote update is the
    fast-forward pull alone, and the worker's packages, model files and runtimes belong to the
    install that runs on the worker. A same-machine worker's image is built from the server
    checkout, so its code update is the server deployment.
    """
    connection = load_worker_connection_settings()
    require_code_update_supported(connection)
    transport = build_worker_transport(runner, connection)
    report = _update_worker_code(transport, connection, branch=branch, runner=runner)
    setup = load_worker_setup_settings()
    _sync_worker_dependencies(transport, connection, setup)
    return report


def require_code_update_supported(settings: WorkerConnectionSettings) -> None:
    """Refuse a code update of a same-machine worker, whose code is the server checkout."""
    if settings.host_kind == "same-machine":
        raise ValueError("a same-machine worker updates with the server deploy")


def _update_worker_code(
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    *,
    branch: str,
    runner: mechanics.ProcessRunner,
) -> WorkerCodeReport:
    """Update the worker checkout body."""
    root = administration.repository_root()
    _validate_branch(runner, root, branch)
    setup = load_worker_setup_settings()
    server_branch, server_revision = mechanics.read_checkout_identity(
        lambda argv: runner.run(tuple(argv), cwd=root).stdout
    )
    _require_matching_origin(runner, root, setup.worker_setup_project_url)
    server_origin_differs = _server_origin_differs(
        runner, root, branch=server_branch, revision=server_revision, fetch=True
    )
    worker_branch, revision = _update_worker_checkout(
        transport,
        connection,
        branch=branch,
        project_url=setup.worker_setup_project_url,
    )
    return WorkerCodeReport(
        branch=worker_branch, revision=revision, server_origin_differs=server_origin_differs
    )


def preflight_worker(*, runner: mechanics.ProcessRunner) -> ProvisioningReport:
    """Read the checkout identity and the worker resources.

    The checkout identity is the branch and HEAD. The read installs, builds, downloads and starts
    nothing, and it never elevates.
    """
    root = administration.repository_root()
    setup = load_worker_setup_settings()
    branch, revision = mechanics.read_checkout_identity(
        lambda argv: runner.run(tuple(argv), cwd=root).stdout
    )
    _require_matching_origin(runner, root, setup.worker_setup_project_url)
    server_origin_differs = _server_origin_differs(
        runner, root, branch=branch, revision=revision, fetch=False
    )
    connection = load_worker_connection_settings()
    transport = build_worker_transport(runner, connection)
    if connection.host_kind == "same-machine":
        # The worker container is built from this checkout, so the server identity is the worker
        # code identity and no checkout lives on another machine to read.
        worker_branch, worker_revision = branch, revision
    else:
        worker_branch, worker_revision = _read_worker_checkout(transport, connection)
    code = WorkerCodeReport(
        branch=worker_branch, revision=worker_revision, server_origin_differs=server_origin_differs
    )
    if not _worker_dependencies_ready(transport, connection):
        return ProvisioningReport(
            code=code, resources=WorkerResourcesReport(ready=False, missing=["dependencies"])
        )
    return ProvisioningReport(code=code, resources=_read_worker_resources(transport, connection))


def _sync_worker_dependencies(
    transport: WorkerTransport, connection: WorkerConnectionSettings, setup: WorkerSetupSettings
) -> None:
    """Install the locked project environment on the already-pulled worker checkout.

    This sync needs no terminal, so it uses the same captured, completion-reported transport path
    as the read-only locked preflight check. It also reinstalls this repository's own editable
    project so a pyproject-only code update refreshes its installed distribution for the preflight
    check.
    """
    transport.run(
        ("uv", "sync", "--locked", "--reinstall-package", _PROJECT_NAME),
        cwd=PurePosixPath(str(connection.worker_project_root)),
        timeout=setup.worker_setup_timeout_seconds,
    )


def _validate_branch(runner: mechanics.ProcessRunner, root: Path, branch: str) -> None:
    try:
        runner.run(("git", "check-ref-format", "--branch", branch), cwd=root)
    except subprocess.CalledProcessError as exc:
        raise ValueError("worker setup branch is not a valid Git branch name") from exc


def _require_matching_origin(runner: mechanics.ProcessRunner, root: Path, project_url: str) -> None:
    actual = runner.run(("git", "remote", "get-url", "origin"), cwd=root).stdout.decode().strip()
    if actual != project_url:
        raise ValueError("configured origin does not match WORKER_SETUP_PROJECT_URL")


def _server_origin_differs(
    runner: mechanics.ProcessRunner,
    root: Path,
    *,
    branch: str,
    revision: str,
    fetch: bool,
) -> bool:
    """Report whether the local checkout HEAD differs from its origin branch.

    With ``fetch`` the origin branch is read from the remote; otherwise from the stored tracking
    ref. The remote read writes nothing, because the administration container mounts the
    checkout read-only.
    """
    if fetch:
        listed = runner.run(
            ("git", "-c", _SSH_BATCH_COMMAND, "ls-remote", "origin", f"refs/heads/{branch}"),
            cwd=root,
        ).stdout.decode()
        tracked = listed.split("\t", 1)[0].strip()
    else:
        tracked = (
            runner.run(
                ("git", "for-each-ref", "--format=%(objectname)", f"refs/remotes/origin/{branch}"),
                cwd=root,
            )
            .stdout.decode()
            .strip()
        )
    return tracked != revision


def _update_worker_checkout(
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    *,
    branch: str,
    project_url: str,
) -> tuple[str, str]:
    worker_root = connection.worker_project_root
    # The worker's current branch selects the requested origin branch before the
    # fetch/branch-switch/pull script. The server and worker branch names may differ.
    current_branch = (
        transport.run(
            ("git", "branch", "--show-current"),
            cwd=PurePosixPath(str(worker_root)),
        )
        .decode()
        .strip()
    )
    if not current_branch:
        raise ValueError("checkout identity requires a non-detached Git branch")
    script = _worker_update_script(
        worker_root,
        project_url,
        branch,
        current_branch=current_branch,
    )
    transport.run(
        ("bash", "-c", script),
        cwd=PurePosixPath(str(worker_root)),
    )
    return _read_worker_checkout(transport, connection)


def _read_worker_checkout(
    transport: WorkerTransport, connection: WorkerConnectionSettings
) -> tuple[str, str]:
    worker_root = connection.worker_project_root

    def run(argv: Sequence[str]) -> bytes:
        return transport.run(tuple(argv), cwd=PurePosixPath(str(worker_root)))

    return mechanics.read_checkout_identity(run)


def _worker_update_script(
    worker_root: Path,
    project_url: str,
    branch: str,
    *,
    current_branch: str,
) -> str:
    root = shlex.quote(str(worker_root))
    url = shlex.quote(project_url)
    name = shlex.quote(branch)
    local_ref = shlex.quote(f"refs/heads/{branch}")
    remote_ref = shlex.quote(f"refs/remotes/origin/{branch}")
    tracking = shlex.quote(f"origin/{branch}")
    ssh_config = shlex.quote(_SSH_BATCH_COMMAND)
    if current_branch == branch:
        selection = ""
    else:
        selection = (
            f"if git show-ref --verify --quiet {local_ref}; then "
            f"  git checkout {name}; "
            "else "
            f"  git checkout -b {name} --track {tracking}; "
            "fi; "
        )
    return (
        "set -euo pipefail; "
        f"cd {root}; "
        f'test "$(pwd -P)" = {root}; '
        f'test "$(git rev-parse --show-toplevel)" = {root}; '
        f'test "$(git remote get-url origin)" = {url}; '
        f"git -c {ssh_config} fetch origin {name}; "
        f"if git show-ref --verify --quiet {local_ref}; then "
        f"  if ! git merge-base --is-ancestor {local_ref} {remote_ref}; then "
        '    echo "worker local branch is ahead of or diverged from origin" >&2; '
        "    exit 1; "
        "  fi; "
        "fi; "
        f"{selection}"
        f"git -c {ssh_config} pull --ff-only origin {name}; "
        f'test "$(git branch --show-current)" = {name}; '
        f'test "$(git rev-parse HEAD)" = "$(git rev-parse {remote_ref})"'
    )


def _read_worker_resources(
    transport: WorkerTransport, connection: WorkerConnectionSettings
) -> WorkerResourcesReport:
    argv = [
        "uv",
        "run",
        "--no-sync",
        "--offline",
        "--no-python-downloads",
        "python",
        "-m",
        "course_explorer_admin.provisioning_worker",
        "check",
    ]
    if connection.host_kind == "same-machine":
        # The worker container's image owns the host packages, so the read checks the mounted device.
        argv.append("--in-container")
    payload = transport.run(
        tuple(argv),
        cwd=PurePosixPath(str(connection.worker_project_root)),
    )
    return WorkerResourcesReport.model_validate_json(payload.decode("utf-8"))


def _worker_dependencies_ready(
    transport: WorkerTransport, connection: WorkerConnectionSettings
) -> bool:
    try:
        transport.run(
            transport.dependency_check_arguments(),
            cwd=PurePosixPath(str(connection.worker_project_root)),
        )
    except subprocess.CalledProcessError:
        return False
    return True
