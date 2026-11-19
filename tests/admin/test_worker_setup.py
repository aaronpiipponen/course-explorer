"""Contracts for the explicit worker code update, readiness read and local install."""

from __future__ import annotations

import getpass
import hashlib
import os
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from course_explorer import administration
from course_explorer.config import (
    SETTINGS_SOURCE_MARKER,
    InferenceSettings,
    TranslationSettings,
    load_worker_connection_settings,
    load_worker_gpu_settings,
)
from course_explorer.configuration import parse_local_configuration
from course_explorer_admin import (
    compose,
    mechanics,
    provisioning,
    provisioning_worker,
    worker_install,
    working_files,
)
from tests.config._support import (
    WORKER_CONNECTION_KEYS,
    WORKER_GPU_KEYS,
    WORKER_SETUP_KEYS,
    set_administrative_controller_environment,
)
from tests.support import REPO_ROOT, write_local_conversion_translation

REVISION_A = "a" * 40
REVISION_B = "b" * 40
WORKER_ORIGIN = "git@github.com:example/worker.git"
WSL_DISTRIBUTION = "Ubuntu-24.04"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            message,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class LocalWorkerTransport:
    """Transport fake that executes recorded worker commands in a real local checkout."""

    def __init__(self, worker_root: Path) -> None:
        self.worker_root = worker_root
        self.calls: list[tuple[str, ...]] = []
        self.syncs: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None, timeout=None, **kwargs):
        command = tuple(argv)
        if command[:2] == ("uv", "sync"):
            # Record the locked worker dependency sync without executing it in this local
            # checkout. It targets the real worker environment, not the test repository.
            self.syncs.append(command)
            return b""
        self.calls.append(command)
        if command[:2] == ("bash", "-c"):
            completed = subprocess.run(
                ["bash", "-c", command[2]], cwd=self.worker_root, capture_output=True, check=True
            )
        else:
            completed = subprocess.run(
                list(command), cwd=self.worker_root, capture_output=True, check=True
            )
        return completed.stdout

    def scripts(self) -> list[str]:
        return [command[2] for command in self.calls if command[:2] == ("bash", "-c")]


@pytest.fixture()
def git_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (origin, server, worker) repos sharing one local bare origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    server = tmp_path / "server"
    subprocess.run(["git", "clone", str(origin), str(server)], check=True, capture_output=True)
    (server / "file.txt").write_text("one")
    _git(server, "add", "file.txt")
    _commit(server, "one")
    (server / "notes.txt").write_text("shared\n")
    _git(server, "add", "notes.txt")
    _commit(server, "notes")
    _git(server, "push", "origin", "main")
    _git(server, "checkout", "-b", "prerequisite-inference")
    (server / "file.txt").write_text("two")
    _git(server, "add", "file.txt")
    _commit(server, "two")
    _git(server, "push", "origin", "prerequisite-inference")
    _git(server, "checkout", "main")
    worker = tmp_path / "worker"
    subprocess.run(["git", "clone", str(origin), str(worker)], check=True, capture_output=True)
    return origin, server, worker


def _wire_setup(
    monkeypatch: pytest.MonkeyPatch,
    origin: Path,
    server: Path,
    worker: Path,
    transport: LocalWorkerTransport,
) -> None:
    monkeypatch.setattr(administration, "repository_root", lambda: server)
    monkeypatch.setattr(
        provisioning,
        "load_worker_setup_settings",
        lambda: SimpleNamespace(
            worker_setup_project_url=str(origin), worker_setup_timeout_seconds=60.0
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "load_worker_connection_settings",
        lambda: SimpleNamespace(worker_project_root=worker, host_kind="wsl"),
    )
    set_administrative_controller_environment(
        monkeypatch, INGESTION_CONTROLLER_STATE_ROOT="ingestion-state"
    )
    monkeypatch.setattr(provisioning, "build_worker_transport", lambda *args: transport)


def test_code_update_is_ff_only_and_preserves_user_work(
    monkeypatch: pytest.MonkeyPatch, git_pair: tuple[Path, Path, Path]
) -> None:
    """Code update is ff-only on the selected branch, notifies drift, and refuses unsafe states."""
    origin, server, worker = git_pair
    transport = LocalWorkerTransport(worker)
    _wire_setup(monkeypatch, origin, server, worker, transport)

    default = provisioning.update_worker_code(runner=mechanics.SubprocessRunner())
    assert default.branch == "main"
    assert default.server_origin_differs is False
    # A successful update syncs the locked environment exactly once, reinstalling the
    # repository's own editable project so the locked sync refreshes its install metadata.
    assert len(transport.syncs) == 1
    sync = transport.syncs[0]
    assert sync[:2] == ("uv", "sync")
    assert sync[sync.index("--reinstall-package") + 1] == provisioning._PROJECT_NAME

    # A local server commit that is not on its origin counterpart is a notice, not a blocker.
    (server / "file.txt").write_text("three")
    _git(server, "add", "file.txt")
    _commit(server, "three")
    drifted = provisioning.update_worker_code(runner=mechanics.SubprocessRunner())
    assert drifted.server_origin_differs is True

    # A tracked edit and an untracked file keep their bytes across the branch switch, and the
    # update succeeds.
    notes = worker / "notes.txt"
    notes.write_text("user edit\n")
    scratch = worker / "scratch.txt"
    scratch.write_text("work-in-progress")
    syncs_before = len(transport.syncs)
    preserved = provisioning.update_worker_code(
        branch="prerequisite-inference", runner=mechanics.SubprocessRunner()
    )
    assert preserved.branch == "prerequisite-inference"
    assert notes.read_text() == "user edit\n"
    assert scratch.read_text() == "work-in-progress"
    assert len(transport.syncs) == syncs_before + 1
    assert _git(worker, "branch", "--show-current").stdout.decode().strip() == (
        "prerequisite-inference"
    )

    # Git's own overwrite refusal protects a local edit to a file that differs between branches.
    edited_file = worker / "file.txt"
    original_file = edited_file.read_text()
    edited_file.write_text("local edit")
    head_before = _git(worker, "rev-parse", "HEAD").stdout.decode().strip()
    with pytest.raises(subprocess.CalledProcessError):
        provisioning.update_worker_code(branch="main", runner=mechanics.SubprocessRunner())
    assert _git(worker, "rev-parse", "HEAD").stdout.decode().strip() == head_before
    edited_file.write_text(original_file)

    # An update safely selects the independently requested origin branch: the worker switches
    # back to main while the server is main, and syncs once.
    syncs_before_switch = len(transport.syncs)
    switched = provisioning.update_worker_code(branch="main", runner=mechanics.SubprocessRunner())
    assert switched.branch == "main"
    assert _git(worker, "branch", "--show-current").stdout.decode().strip() == "main"
    assert switched.revision == _git(worker, "rev-parse", "HEAD").stdout.decode().strip()
    assert _git(server, "branch", "--show-current").stdout.decode().strip() == "main"
    assert len(transport.syncs) == syncs_before_switch + 1
    assert notes.read_text() == "user edit\n"
    assert scratch.read_text() == "work-in-progress"

    # A same-branch update fast-forwards without a checkout.
    synced = provisioning.update_worker_code(
        branch="prerequisite-inference", runner=mechanics.SubprocessRunner()
    )
    assert synced.branch == "prerequisite-inference"

    # No push, reset, clean, force, stash, bundle, or remote rewrite is ever emitted.
    joined = " ".join(" ".join(script.split()) for script in transport.scripts())
    for forbidden in ("push", "reset --hard", "clean -", "force", "stash", "bundle", "set-url"):
        assert forbidden not in joined

    # A local selected branch that is ahead of origin is refused without touching work.
    (worker / "file.txt").write_text("diverged")
    _git(worker, "add", "file.txt")
    _commit(worker, "diverged")
    diverged_head = _git(worker, "rev-parse", "HEAD").stdout.decode().strip()
    with pytest.raises(subprocess.CalledProcessError):
        provisioning.update_worker_code(
            branch="prerequisite-inference", runner=mechanics.SubprocessRunner()
        )
    assert _git(worker, "rev-parse", "HEAD").stdout.decode().strip() == diverged_head


class ScriptedWorkerRunner:
    """Worker process boundary scripted to one readiness read."""

    def __init__(
        self,
        *,
        missing_package: str | None = None,
        docker_ready: bool = True,
        db_image_prepared: bool = True,
        gpu_stack_ready: bool = True,
        rocm_target: str = "gfx1201",
        container_paths: frozenset[str] = frozenset({"/dev/kfd"}),
        runtime_library_missing: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.interactive_calls: list[tuple[str, ...]] = []
        self.missing_package = missing_package
        self.docker_ready = docker_ready
        self.db_image_prepared = db_image_prepared
        self.gpu_stack_ready = gpu_stack_ready
        self.rocm_target = rocm_target
        self.container_paths = container_paths
        self.runtime_library_missing = runtime_library_missing

    def run(self, argv, *, cwd=None, env=None, stdin_path=None, stdout_path=None, timeout=None):
        command = tuple(argv)
        self.calls.append(command)
        if command[:1] == ("sudo",):
            # A captured command holds no terminal, so an account outside the docker group fails
            # here rather than answering a password prompt.
            raise subprocess.CalledProcessError(1, list(command))
        if command[:1] == ("dpkg-query",):
            if command[-1] == self.missing_package:
                raise subprocess.CalledProcessError(1, list(command))
            return subprocess.CompletedProcess(list(command), 0, stdout=b"install ok installed\n")
        if command[:2] == ("test", "-e"):
            if command[2] not in self.container_paths:
                raise subprocess.CalledProcessError(1, list(command))
            return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        if command[:1] == ("ldd",):
            library = b"not found" if self.runtime_library_missing else b"/opt/lib/libhip.so"
            return subprocess.CompletedProcess(
                list(command), 0, stdout=b"libhip.so => " + library + b"\n"
            )
        if command[:1] == ("rocminfo",) or (
            len(command) == 3 and command[:2] == ("bash", "-lc") and "rocminfo" in command[2]
        ):
            if not self.gpu_stack_ready:
                raise subprocess.CalledProcessError(1, list(command))
            return subprocess.CompletedProcess(
                list(command), 0, stdout=f"  Name:                    {self.rocm_target}\n".encode()
            )
        if command == ("nvcc", "--version"):
            if not self.gpu_stack_ready:
                raise subprocess.CalledProcessError(1, list(command))
            return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        if command[:1] == ("nvidia-smi",) and "--query-gpu=compute_cap" in command:
            if not self.gpu_stack_ready:
                raise subprocess.CalledProcessError(1, list(command))
            # A host answers one line per card when the read names no card, and the configured card's
            # line alone when it does.
            selected = any(item.startswith("--id=") for item in command)
            return subprocess.CompletedProcess(
                list(command), 0, stdout=b"8.6\n" if selected else b"8.6\n9.0\n"
            )
        if command == ("docker", "info"):
            if not self.docker_ready:
                raise subprocess.CalledProcessError(1, list(command))
            return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        compose_prefix = mechanics.compose_prefix(interpreter=True)
        if command[: len(compose_prefix)] == compose_prefix:
            if command[-1] == "version":
                return subprocess.CompletedProcess(list(command), 0, stdout=b"")
            if "config" in command:
                return subprocess.CompletedProcess(
                    list(command), 0, stdout=b"course-explorer-synthetic-db\n"
                )
            if command[-2:] == ("build", "db"):
                self.db_image_prepared = True
                return subprocess.CompletedProcess(list(command), 0, stdout=b"")
            if command[-2:] == ("build", "worker"):
                return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        if command[:3] == ("docker", "image", "inspect"):
            if not self.db_image_prepared:
                return subprocess.CompletedProcess(list(command), 0, stdout=b"\n")
            return subprocess.CompletedProcess(
                list(command),
                0,
                stdout=f"{provisioning_worker._DB_EXTENSION_VERSION}\n".encode(),
            )
        if command[:2] == ("git", "-C"):
            directory = Path(command[2])
            if command[3:] == ("rev-parse", "HEAD"):
                revision = directory.name.removeprefix("runtime-").split("-", 1)[0]
                return subprocess.CompletedProcess(
                    list(command), 0, stdout=f"{revision}\n".encode()
                )
            if command[3:] == ("status", "--porcelain"):
                return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        raise AssertionError(f"unexpected check command: {command}")

    def run_interactive(self, argv, *, cwd=None, env=None, timeout=None):
        """Record one streamed helper mutation without executing it on this host."""
        command = tuple(argv)
        self.interactive_calls.append(command)
        if command[:2] == ("git", "clone") or (
            command[:2] == ("git", "-C") and command[3:5] == ("checkout", "--detach")
        ):
            return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        if command[:2] == ("bash", "-c") and "cmake" in command[2]:
            return subprocess.CompletedProcess(list(command), 0, stdout=b"")
        return self.run(command, cwd=cwd, env=env, timeout=timeout)


def _wire_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    translation: Path,
    generation: Path,
    rerank: Path,
    generation_url: str | None = "https://example.invalid/generation.gguf",
    fallback: Path | None = None,
    fallback_url: str = "https://example.invalid/generation-fallback.gguf",
    translation_url: str | None = None,
    artifact_sha256: str | None = None,
    gpu_backend: str = "rocm",
    gpu_target: str | None = None,
    host_kind: str = "wsl",
) -> None:
    monkeypatch.setattr(
        provisioning_worker,
        "load_translation_settings",
        lambda: SimpleNamespace(
            translation_artifact_path=translation,
            translation_artifact_sha256=artifact_sha256 or "1" * 64,
            translation_artifact_url=translation_url,
            translation_runtime_revision=REVISION_A,
        ),
    )
    digest = artifact_sha256 or "2" * 64
    first = SimpleNamespace(
        artifact_path=generation, artifact_sha256=digest, artifact_url=generation_url
    )
    second = (
        first
        if fallback is None
        else SimpleNamespace(
            artifact_path=fallback, artifact_sha256=digest, artifact_url=fallback_url
        )
    )
    monkeypatch.setattr(
        provisioning_worker,
        "load_inference_settings",
        lambda: SimpleNamespace(
            inference_local_variants=(first, second),
            inference_runtime_revision=REVISION_B,
            local_artifact_url=lambda variant: variant.artifact_url,
        ),
    )
    monkeypatch.setattr(
        provisioning_worker,
        "load_inference_rerank_settings",
        lambda: SimpleNamespace(
            inference_rerank_artifact_path=rerank,
            inference_rerank_artifact_sha256=artifact_sha256 or "3" * 64,
            rerank_artifact_url="https://example.invalid/rerank.gguf",
        ),
    )
    monkeypatch.setattr(
        provisioning_worker,
        "load_worker_setup_settings",
        lambda: SimpleNamespace(worker_setup_timeout_seconds=60.0),
    )
    monkeypatch.setattr(
        provisioning_worker,
        "load_worker_connection_settings",
        lambda: SimpleNamespace(host_kind=host_kind),
    )
    target = gpu_target or ("gfx1201" if gpu_backend == "rocm" else "86")
    monkeypatch.setattr(
        provisioning_worker,
        "load_worker_gpu_settings",
        lambda: SimpleNamespace(
            gpu_backend=gpu_backend,
            gpu_target=target,
            gpu_device_index=0,
        ),
    )

    def runtime_directory(revision, backend, build_target):
        return tmp_path / f"runtime-{revision}-{backend}-{build_target}"

    monkeypatch.setattr(mechanics, "runtime_checkout_directory", runtime_directory)
    monkeypatch.setattr(
        mechanics,
        "runtime_executable",
        lambda revision, backend, build_target: (
            runtime_directory(revision, backend, build_target) / "build" / "bin" / "llama-server"
        ),
    )


def _write_runtimes(root: Path, *, gpu_backend: str = "rocm", gpu_target: str = "gfx1201") -> None:
    """Create both executable dummy runtime checkouts under one root."""
    for revision in (REVISION_A, REVISION_B):
        executable = (
            root
            / f"runtime-{revision}-{gpu_backend}-{gpu_target}"
            / "build"
            / "bin"
            / "llama-server"
        )
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"")
        executable.chmod(0o700)


def _wire_present_resources(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, generation_present: bool = True
) -> None:
    """Wire one root whose configured artifacts and executable runtimes already exist."""
    digest = hashlib.sha256(b"tiny-artifact").hexdigest()
    translation = root / "translation.gguf"
    generation = root / "generation.gguf"
    rerank = root / "rerank.gguf"
    _wire_resources(
        monkeypatch,
        root,
        translation=translation,
        generation=generation,
        rerank=rerank,
        artifact_sha256=digest,
    )
    translation.write_bytes(b"tiny-artifact")
    rerank.write_bytes(b"tiny-artifact")
    if generation_present:
        generation.write_bytes(b"tiny-artifact")
    _write_runtimes(root)


def test_worker_check_is_read_only_and_reports_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check names fixed components without installing, downloading, starting or creating files."""
    shared = tmp_path / "check-shared"
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(shared))
    runner = ScriptedWorkerRunner(
        missing_package="cmake", docker_ready=False, gpu_stack_ready=False
    )
    _wire_resources(
        monkeypatch,
        tmp_path,
        translation=tmp_path / "translation.gguf",
        generation=tmp_path / "generation.gguf",
        rerank=tmp_path / "rerank.gguf",
    )

    report = provisioning_worker.check_worker_resources(runner=runner)

    assert report.ready is False
    assert report.missing == [
        "cmake",
        "gpu-stack",
        "docker",
        "translation-artifact",
        "generation-artifact",
        "rerank-artifact",
        "translation-runtime",
        "inference-runtime",
    ]
    assert all(
        command[0] not in {"curl", "apt-get", "git", "systemctl", "uv"} for command in runner.calls
    )
    # An account in the docker group reaches the socket without a prefix.
    assert not any(command[0] == "sudo" for command in runner.calls)

    # An all-present root with genuine tiny hashes and executable dummy runtimes is ready, and
    # check performs only captured read-only probes.
    present_root = tmp_path / "present"
    present_root.mkdir()
    _wire_present_resources(monkeypatch, present_root)
    present_runner = ScriptedWorkerRunner()
    present = provisioning_worker.check_worker_resources(runner=present_runner)
    assert present.ready is True
    assert present.missing == []
    assert present_runner.interactive_calls == []

    # A prepared root whose configured GPU stack does not answer reports that one component.
    stack_runner = ScriptedWorkerRunner(gpu_stack_ready=False)
    stack_missing = provisioning_worker.check_worker_resources(runner=stack_runner)
    assert stack_missing.ready is False
    assert stack_missing.missing == ["gpu-stack"]

    # An image without the extension capability is not ready. A read-only check reports the
    # missing image and never builds or pulls it.
    unprepared_runner = ScriptedWorkerRunner(db_image_prepared=False)
    unprepared = provisioning_worker.check_worker_resources(runner=unprepared_runner)
    assert unprepared.ready is False
    assert unprepared.missing == ["db-image"]
    assert unprepared_runner.interactive_calls == []
    assert not any(command[-2:] == ("build", "db") for command in unprepared_runner.calls)
    assert not any(command[:2] == ("docker", "pull") for command in unprepared_runner.calls)

    # A runtime file that exists but cannot be executed is not ready, and check never builds.
    inference_executable = (
        present_root / f"runtime-{REVISION_B}-rocm-gfx1201" / "build" / "bin" / "llama-server"
    )
    inference_executable.chmod(0o600)
    non_executable_runner = ScriptedWorkerRunner()
    non_executable = provisioning_worker.check_worker_resources(runner=non_executable_runner)
    assert non_executable.ready is False
    assert non_executable.missing == ["inference-runtime"]
    assert non_executable_runner.interactive_calls == []
    assert not shared.exists()

    # A socket this account cannot write reports the component instead of prompting: the read is a
    # captured plain Docker command, so it fails without a terminal, and nothing waits for a
    # password.
    unwritable_runner = ScriptedWorkerRunner(docker_ready=False)
    unwritable = provisioning_worker.check_worker_resources(runner=unwritable_runner)
    assert "docker" in unwritable.missing
    assert not any(command[0] == "sudo" for command in unwritable_runner.calls)
    assert not any("-n" in command for command in unwritable_runner.calls)
    assert unwritable_runner.interactive_calls == []

    # A same-machine readiness read inside the worker container reads the mounted device node and
    # the host-built runtime's libraries instead of the vendor tool.
    inference_executable = (
        present_root / f"runtime-{REVISION_B}-rocm-gfx1201" / "build" / "bin" / "llama-server"
    )
    inference_executable.chmod(0o700)
    translation_executable = (
        present_root / f"runtime-{REVISION_A}-rocm-gfx1201" / "build" / "bin" / "llama-server"
    )
    container_paths = frozenset(
        {"/dev/kfd", str(inference_executable), str(translation_executable)}
    )
    monkeypatch.setattr(
        provisioning_worker,
        "load_worker_connection_settings",
        lambda: SimpleNamespace(host_kind="same-machine"),
    )
    container_runner = ScriptedWorkerRunner(container_paths=container_paths)
    container = provisioning_worker.check_worker_resources(
        runner=container_runner, worker_root=present_root, in_container=True
    )
    assert container.ready is True
    assert ("test", "-e", "/dev/kfd") in container_runner.calls
    assert any(command[0] == "ldd" for command in container_runner.calls)
    assert not any(
        command[0] in {"rocminfo", "nvidia-smi", "dpkg-query"} for command in container_runner.calls
    )

    # An unresolved runtime library and an absent mounted device node each report the one stack
    # component.
    library_runner = ScriptedWorkerRunner(
        container_paths=container_paths, runtime_library_missing=True
    )
    library_missing = provisioning_worker.check_worker_resources(
        runner=library_runner, in_container=True
    )
    assert library_missing.missing == ["gpu-stack"]

    device_runner = ScriptedWorkerRunner(container_paths=frozenset())
    device_missing = provisioning_worker.check_worker_resources(
        runner=device_runner, in_container=True
    )
    assert device_missing.missing == ["gpu-stack"]
    assert not any(command[0] == "ldd" for command in device_runner.calls)

    # A CUDA worker's container read checks its mounted device node and the toolkit libraries its
    # runtime resolves, without the vendor tool.
    cuda_root = tmp_path / "container-cuda"
    cuda_root.mkdir()
    digest = hashlib.sha256(b"tiny-artifact").hexdigest()
    cuda_translation = cuda_root / "translation.gguf"
    cuda_generation = cuda_root / "generation.gguf"
    cuda_rerank = cuda_root / "rerank.gguf"
    _wire_resources(
        monkeypatch,
        cuda_root,
        translation=cuda_translation,
        generation=cuda_generation,
        rerank=cuda_rerank,
        artifact_sha256=digest,
        gpu_backend="cuda",
        gpu_target="86",
        host_kind="same-machine",
    )
    for path in (cuda_translation, cuda_generation, cuda_rerank):
        path.write_bytes(b"tiny-artifact")
    _write_runtimes(cuda_root, gpu_backend="cuda", gpu_target="86")
    cuda_executables = tuple(
        cuda_root / f"runtime-{revision}-cuda-86" / "build" / "bin" / "llama-server"
        for revision in (REVISION_A, REVISION_B)
    )
    cuda_paths = frozenset({"/dev/nvidia0", *(str(path) for path in cuda_executables)})
    cuda_runner = ScriptedWorkerRunner(container_paths=cuda_paths)
    cuda_report = provisioning_worker.check_worker_resources(
        runner=cuda_runner, worker_root=cuda_root, in_container=True
    )
    assert cuda_report.ready is True
    assert ("test", "-e", "/dev/nvidia0") in cuda_runner.calls
    assert any(command[0] == "ldd" for command in cuda_runner.calls)
    assert not any(command[:1] == ("nvidia-smi",) for command in cuda_runner.calls)

    cuda_library_runner = ScriptedWorkerRunner(
        container_paths=cuda_paths, runtime_library_missing=True
    )
    cuda_library_missing = provisioning_worker.check_worker_resources(
        runner=cuda_library_runner, in_container=True
    )
    assert cuda_library_missing.missing == ["gpu-stack"]

    # The install's host-side preparation of a same-machine host checks the vendor tool against the
    # configured target, so a card naming another target reports the stack component.
    prepare_root = tmp_path / "prepare-host"
    prepare_root.mkdir()
    _wire_present_resources(monkeypatch, prepare_root)
    monkeypatch.setattr(
        provisioning_worker,
        "load_worker_connection_settings",
        lambda: SimpleNamespace(host_kind="same-machine"),
    )
    prepare_runner = ScriptedWorkerRunner(rocm_target="gfx90a")
    prepared = provisioning_worker.prepare_worker_resources(
        runner=prepare_runner, worker_root=prepare_root
    )
    assert "gpu-stack" in prepared.missing
    assert any(command[:1] == ("rocminfo",) for command in prepare_runner.calls)
    assert not any(command[:2] == ("test", "-e") for command in prepare_runner.calls)
    # The capability read names the configured card, so another card's matching capability does not
    # report the configured card as ready.
    cuda_host = tmp_path / "cuda-host"
    cuda_host.mkdir()
    cuda_digest = hashlib.sha256(b"tiny-artifact").hexdigest()
    cuda_files = [
        cuda_host / name for name in ("translation.gguf", "generation.gguf", "rerank.gguf")
    ]
    _wire_resources(
        monkeypatch,
        cuda_host,
        translation=cuda_files[0],
        generation=cuda_files[1],
        rerank=cuda_files[2],
        artifact_sha256=cuda_digest,
        gpu_backend="cuda",
        gpu_target="90",
        host_kind="linux",
    )
    for path in cuda_files:
        path.write_bytes(b"tiny-artifact")
    _write_runtimes(cuda_host, gpu_backend="cuda", gpu_target="90")
    mismatched_card = provisioning_worker.check_worker_resources(
        runner=ScriptedWorkerRunner(), worker_root=cuda_host
    )
    assert mismatched_card.missing == ["gpu-stack"]


class _ComposeExecRunner:
    """Compose-exec boundary scripted to one same-machine preflight read."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None, stdin_path=None, stdout_path=None, timeout=None):
        command = tuple(argv)
        self.commands.append(command)
        script = command[-1]
        if "uv sync --locked --check" in script:
            # The worker image carries no development group and no installed project, so the plain
            # check reports the image environment as outdated.
            if "--no-dev --no-install-project" not in script:
                return subprocess.CompletedProcess(
                    list(command),
                    1,
                    stdout=b"",
                    stderr=b"\nCOURSE_EXPLORER_TRANSPORT_RESULT command 1\n",
                )
            return subprocess.CompletedProcess(
                list(command),
                0,
                stdout=b"",
                stderr=b"\nCOURSE_EXPLORER_TRANSPORT_RESULT command 0\n",
            )
        if "course_explorer_admin.provisioning_worker" in script:
            return subprocess.CompletedProcess(
                list(command),
                0,
                stdout=provisioning_worker.WorkerResourcesReport(ready=True, missing=[])
                .model_dump_json()
                .encode(),
                stderr=b"\nCOURSE_EXPLORER_TRANSPORT_RESULT command 0\n",
            )
        raise AssertionError(f"unexpected preflight command: {command}")


def test_same_machine_preflight_checks_the_image_environment_and_reports_server_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-machine preflight checks the worker image environment and reports the server code.

    The worker's code identity is the server checkout's, because the image is built from it, and
    the dependency check names the image environment that holds no development group and no
    installed project. The resource read runs only after that check passes. A same-machine
    preflight run outside the administration container refuses before any Compose exec.
    """
    monkeypatch.setenv("WORKER_HOST_KIND", "same-machine")
    monkeypatch.delenv("WORKER_SSH_TARGET", raising=False)
    monkeypatch.delenv("WORKER_WSL_DISTRIBUTION", raising=False)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(administration.repository_root()))
    monkeypatch.setattr(
        provisioning.mechanics,
        "read_checkout_identity",
        lambda run: ("main", REVISION_A),
    )
    monkeypatch.setattr(provisioning, "_require_matching_origin", lambda *args: None)
    monkeypatch.setattr(provisioning, "_server_origin_differs", lambda *args, **kwargs: False)
    runner = _ComposeExecRunner()

    report = provisioning.preflight_worker(runner=runner)

    assert report.code.branch == "main"
    assert report.code.revision == REVISION_A
    assert report.code.server_origin_differs is False
    assert report.resources is not None
    assert report.resources.ready is True
    assert report.resources.missing == []
    scripts = [command[-1] for command in runner.commands]
    assert any("course_explorer_admin.provisioning_worker check" in script for script in scripts)
    assert any("--in-container" in script for script in scripts)

    # A host-run same-machine preflight reads the container paths as local paths, so it refuses
    # before it runs any Compose exec.
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/app")
    refused_runner = _ComposeExecRunner()
    with pytest.raises(ValueError):
        provisioning.preflight_worker(runner=refused_runner)
    assert refused_runner.commands == []


def test_artifact_preparation_verifies_before_publish_and_preserves_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prepare creates the shared layout, leaves a mismatching installed artifact in place and
    refuses a bad download.
    """
    shared = tmp_path / "worker-shared"
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(shared))
    installed = tmp_path / "translation.gguf"
    installed.write_bytes(b"installed-mismatch")
    runner = ScriptedWorkerRunner()
    _wire_resources(
        monkeypatch,
        tmp_path,
        translation=installed,
        generation=tmp_path / "generation.gguf",
        rerank=tmp_path / "rerank.gguf",
    )

    with pytest.raises(ValueError):
        provisioning_worker.prepare_worker_resources(runner=runner)
    assert installed.read_bytes() == b"installed-mismatch"
    assert sorted(path.name for path in shared.iterdir()) == ["locks", "models"]
    assert oct((shared / "models").stat().st_mode & 0o777) == "0o700"
    assert oct((shared / "locks").stat().st_mode & 0o777) == "0o700"

    class DownloadRunner(ScriptedWorkerRunner):
        def __init__(
            self,
            *,
            download_bytes: bytes = b"bad-download",
            transfer_failure: bool = False,
            **kwargs,
        ) -> None:
            super().__init__(**kwargs)
            self.download_bytes = download_bytes
            self.transfer_failure = transfer_failure

        def run(self, argv, **kwargs):
            command = tuple(argv)
            if command[0] == "curl":
                self.calls.append(command)
                Path(command[command.index("--output") + 1]).write_bytes(b"partial-transfer")
                if self.transfer_failure:
                    raise subprocess.CalledProcessError(22, list(command))
                Path(command[command.index("--output") + 1]).write_bytes(self.download_bytes)
                return subprocess.CompletedProcess(list(command), 0, stdout=b"")
            return super().run(argv, **kwargs)

    download_runner = DownloadRunner()
    download_root = tmp_path / "download"
    download_root.mkdir()
    owner_scratch = download_root / "generation.gguf.download"
    owner_scratch.write_bytes(b"owner-bytes")
    _wire_resources(
        monkeypatch,
        download_root,
        translation=download_root / "translation.gguf",
        generation=download_root / "generation.gguf",
        rerank=download_root / "rerank.gguf",
    )
    with pytest.raises(ValueError):
        provisioning_worker.prepare_worker_resources(runner=download_runner)
    assert not (download_root / "generation.gguf").exists()
    assert owner_scratch.read_bytes() == b"owner-bytes"
    assert sorted(path.name for path in download_root.iterdir()) == ["generation.gguf.download"]
    assert any(command[0] == "curl" for command in download_runner.calls)

    # A broken transfer leaves neither a destination nor its invocation temporary behind.
    transfer_root = tmp_path / "transfer"
    transfer_root.mkdir()
    _wire_resources(
        monkeypatch,
        transfer_root,
        translation=transfer_root / "translation.gguf",
        generation=transfer_root / "generation.gguf",
        rerank=transfer_root / "rerank.gguf",
    )
    transfer_runner = DownloadRunner(transfer_failure=True)
    with pytest.raises(subprocess.CalledProcessError):
        provisioning_worker.prepare_worker_resources(runner=transfer_runner)
    assert list(transfer_root.iterdir()) == []

    # A valid download publishes to its configured target alone and leaves owner scratch untouched.
    success_root = tmp_path / "success"
    success_root.mkdir()
    success_scratch = success_root / "generation.gguf.download"
    success_scratch.write_bytes(b"owner-bytes")
    _wire_present_resources(monkeypatch, success_root, generation_present=False)
    success_runner = DownloadRunner(download_bytes=b"tiny-artifact", db_image_prepared=False)
    prepared = provisioning_worker.prepare_worker_resources(runner=success_runner)
    assert prepared.ready is True
    assert prepared.missing == []
    assert any(command[-2:] == ("build", "db") for command in success_runner.interactive_calls)
    assert not any(command[:2] == ("docker", "pull") for command in success_runner.calls)
    published = success_root / "generation.gguf"
    assert published.read_bytes() == b"tiny-artifact"
    assert (published.stat().st_mode & 0o777) == 0o644
    assert success_scratch.read_bytes() == b"owner-bytes"
    assert not any(path.name.startswith(".generation.gguf-") for path in success_root.iterdir())

    # A second key with its own file is checked and downloaded as the fallback component, while two
    # keys that share one file need one download.
    digest = hashlib.sha256(b"tiny-artifact").hexdigest()
    fallback_root = tmp_path / "fallback"
    fallback_root.mkdir()
    _wire_resources(
        monkeypatch,
        fallback_root,
        translation=fallback_root / "translation.gguf",
        generation=fallback_root / "generation.gguf",
        rerank=fallback_root / "rerank.gguf",
        fallback=fallback_root / "generation-fallback.gguf",
        artifact_sha256=digest,
    )
    for name in ("translation.gguf", "generation.gguf", "rerank.gguf"):
        (fallback_root / name).write_bytes(b"tiny-artifact")
    _write_runtimes(fallback_root)
    missing_fallback = provisioning_worker.check_worker_resources(runner=ScriptedWorkerRunner())
    assert missing_fallback.missing == ["generation-artifact-fallback"]
    fallback_runner = DownloadRunner(download_bytes=b"tiny-artifact")
    prepared_fallback = provisioning_worker.prepare_worker_resources(runner=fallback_runner)
    assert prepared_fallback.ready is True
    fallback_downloads = [command for command in fallback_runner.calls if command[0] == "curl"]
    assert len(fallback_downloads) == 1
    assert fallback_downloads[0][-1] == "https://example.invalid/generation-fallback.gguf"
    assert (fallback_root / "generation-fallback.gguf").read_bytes() == b"tiny-artifact"

    # A published translation source downloads and publishes a missing file at mode 0644, while a
    # local conversion is reported without a download.
    published_root = tmp_path / "published-translation"
    published_root.mkdir()
    _wire_resources(
        monkeypatch,
        published_root,
        translation=published_root / "translation.gguf",
        generation=published_root / "generation.gguf",
        rerank=published_root / "rerank.gguf",
        translation_url="https://example.invalid/translation.gguf",
        artifact_sha256=digest,
    )
    for name in ("generation.gguf", "rerank.gguf"):
        (published_root / name).write_bytes(b"tiny-artifact")
    _write_runtimes(published_root)
    published_runner = DownloadRunner(download_bytes=b"tiny-artifact")
    prepared_translation = provisioning_worker.prepare_worker_resources(runner=published_runner)
    assert prepared_translation.ready is True
    installed_translation = published_root / "translation.gguf"
    assert installed_translation.read_bytes() == b"tiny-artifact"
    assert (installed_translation.stat().st_mode & 0o777) == 0o644
    assert any(
        command[0] == "curl" and command[-1] == "https://example.invalid/translation.gguf"
        for command in published_runner.calls
    )

    local_root = tmp_path / "local-translation"
    local_root.mkdir()
    _wire_resources(
        monkeypatch,
        local_root,
        translation=local_root / "translation.gguf",
        generation=local_root / "generation.gguf",
        rerank=local_root / "rerank.gguf",
        artifact_sha256=digest,
    )
    for name in ("generation.gguf", "rerank.gguf"):
        (local_root / name).write_bytes(b"tiny-artifact")
    _write_runtimes(local_root)
    local_runner = DownloadRunner(download_bytes=b"tiny-artifact")
    local = provisioning_worker.prepare_worker_resources(runner=local_runner)
    assert local.ready is False
    assert local.missing == ["translation-artifact"]
    assert not any(command[0] == "curl" for command in local_runner.calls)
    assert not (local_root / "translation.gguf").exists()


def test_runtime_build_flags_follow_the_gpu_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each backend clones its own runtime directory and configures its own build flags."""
    digest = hashlib.sha256(b"tiny-artifact").hexdigest()
    for backend, target, build_flags in (
        (
            "rocm",
            "gfx1201",
            (
                'ROCM_PATH="$(hipconfig -R)"',
                "-DLLAMA_USE_PREBUILT_UI=OFF",
                "-DGGML_HIP=ON -DGPU_TARGETS=gfx1201",
            ),
        ),
        (
            "cuda",
            "86",
            ("-DLLAMA_USE_PREBUILT_UI=OFF", "-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86"),
        ),
    ):
        root = tmp_path / backend
        root.mkdir()
        monkeypatch.setenv("ADMIN_SHARED_ROOT", str(root / "shared"))
        _wire_resources(
            monkeypatch,
            root,
            translation=root / "translation.gguf",
            generation=root / "generation.gguf",
            rerank=root / "rerank.gguf",
            artifact_sha256=digest,
            gpu_backend=backend,
            gpu_target=target,
        )
        for name in ("translation.gguf", "generation.gguf", "rerank.gguf"):
            (root / name).write_bytes(b"tiny-artifact")
        runner = ScriptedWorkerRunner()

        provisioning_worker.prepare_worker_resources(runner=runner)

        clones = {
            command[3] for command in runner.interactive_calls if command[:2] == ("git", "clone")
        }
        assert clones == {
            str(root / f"runtime-{REVISION_A}-{backend}-{target}"),
            str(root / f"runtime-{REVISION_B}-{backend}-{target}"),
        }
        scripts = [
            command[2] for command in runner.interactive_calls if command[:2] == ("bash", "-c")
        ]
        assert any(all(flag in script for flag in build_flags) for script in scripts)


_LINUX_RELEASE = {
    "ID": "linuxmint",
    "ID_LIKE": "ubuntu",
    "VERSION_ID": "22.3",
    "VERSION_CODENAME": "zena",
    "UBUNTU_CODENAME": "noble",
}
_WSL_RELEASE = {
    "ID": "ubuntu",
    "VERSION_ID": "24.04",
    "VERSION_CODENAME": "noble",
    "UBUNTU_CODENAME": "noble",
}
# The group ids the install's own group check and its generated override read.
_GROUP_IDS = {"docker": 998, "render": 992, "video": 44}


def _joined_group_ids(gpu_backend: str, *, same_machine: bool) -> list[int]:
    """Return the group ids the install's own required-group rule names for one answer."""
    names = worker_install._required_groups(gpu_backend, same_machine=same_machine)
    return [_GROUP_IDS[name] for name in names]


class RecordingRunner:
    """Install runner that answers the host-check reads and records every argv."""

    def __init__(
        self,
        root: Path,
        *,
        missing_docker: bool = False,
        worker_running: bool = False,
        systemd: bool = True,
    ) -> None:
        self.root = root
        self.missing_docker = missing_docker
        self.worker_running = worker_running
        self.systemd = systemd
        self.calls: list[tuple[str, ...]] = []
        self.interactive_calls: list[tuple[str, ...]] = []
        self.recreated_services: list[str] = []
        self.installed_text: str | None = None

    def run(self, argv, *, cwd=None, env=None, stdin_path=None, stdout_path=None, timeout=None):
        command = tuple(argv)
        self.calls.append(command)
        if command == ("git", "rev-parse", "--show-toplevel"):
            return self._completed(command, f"{self.root}\n")
        if command == ("git", "branch", "--show-current"):
            return self._completed(command, "main\n")
        if command == ("git", "remote", "get-url", "origin"):
            return self._completed(command, f"{WORKER_ORIGIN}\n")
        if command == ("ps", "-p", "1", "-o", "comm="):
            return self._completed(command, "systemd\n" if self.systemd else "init\n")
        if command[:1] == ("dpkg-query",):
            if self.missing_docker and command[-1] == "docker-ce":
                raise subprocess.CalledProcessError(1, list(command))
            return self._completed(command, "install ok installed\n")
        if command == ("dpkg", "--print-architecture"):
            return self._completed(command, "amd64\n")
        if command == ("hipconfig", "-R"):
            return self._completed(command, "/opt/rocm-7.2.1\n")
        if command == ("ldconfig", "-p"):
            libraries = self.root / "cuda-libs"
            lines = [
                f"\t{name} (libc6,x86-64) => {libraries / name}"
                for name in ("libcudart.so.12", "libcublas.so.12", "libcublasLt.so.12")
                if (libraries / name).exists()
            ]
            return self._completed(command, "\n".join(lines) + "\n")
        if command[:2] == ("docker", "info"):
            return self._completed(command, '{"runc": {}, "nvidia": {}}\n')
        compose_prefix = mechanics.compose_prefix(interpreter=True)
        if command[: len(compose_prefix)] == compose_prefix:
            if "ps" in command and "--services" in command:
                return self._completed(command, "worker\n" if self.worker_running else "")
            if "up" in command and "--force-recreate" in command:
                self.recreated_services.append(command[-1])
                return self._completed(command, "")
        raise AssertionError(f"unexpected install command: {command}")

    def run_interactive(self, argv, *, cwd=None, env=None, timeout=None):
        command = tuple(argv)
        self.interactive_calls.append(command)
        if command[:2] == ("sudo", "install") and Path(command[-2]).is_file():
            # A rendered file is a temporary one that the install removes after this step.
            self.installed_text = Path(command[-2]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(list(command), 0)

    @staticmethod
    def _completed(command: tuple[str, ...], stdout: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(command), 0, stdout=stdout.encode())


def _install_checkout(tmp_path: Path, name: str) -> Path:
    """Return a real Git checkout that holds the tracked examples tree, built once per name."""
    repo = tmp_path / name
    if not repo.exists():
        repo.mkdir()
        shutil.copytree(REPO_ROOT / "examples", repo / "examples")
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        _git(repo, "remote", "add", "origin", WORKER_ORIGIN)
        _git(repo, "add", "-A")
        _commit(repo, "examples")
    return repo


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    name: str,
    host_kind: str = "linux",
    missing_docker: bool = False,
    install_rule: bool = False,
    groups_effective: bool = True,
    preset: str | None = None,
    same_machine: bool = False,
    gpu_backend: str = "rocm",
    gpu_target: str = "gfx1201",
    worker_running: bool = False,
    systemd: bool = True,
    prepare: Callable[[Path], None] | None = None,
    release: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Run one install against a fresh checkout wired to its own host kind and runner."""
    repo = _install_checkout(tmp_path, name)
    if prepare is not None:
        prepare(repo)
    runner = RecordingRunner(
        repo, missing_docker=missing_docker, worker_running=worker_running, systemd=systemd
    )
    monkeypatch.setattr(administration, "repository_root", lambda: repo)
    monkeypatch.setattr(compose, "docker_needs_sudo", lambda: False)
    monkeypatch.setattr(
        worker_install.platform,
        "freedesktop_os_release",
        (
            (lambda: release)
            if release is not None
            else (lambda: _WSL_RELEASE if host_kind == "wsl" else _LINUX_RELEASE)
        ),
    )
    if host_kind == "wsl":
        monkeypatch.setenv("WSL_DISTRO_NAME", WSL_DISTRIBUTION)
        monkeypatch.setattr(
            worker_install.platform, "release", lambda: "6.6.87.2-microsoft-standard-WSL2"
        )
    else:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        monkeypatch.setattr(worker_install.platform, "release", lambda: "6.17.0-19-generic")
    # The install's own group verdict reports whether this login session already holds the ids the
    # usermod step and the generated override name.
    monkeypatch.setattr(
        worker_install.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=_GROUP_IDS[name]),
    )
    effective = (
        _joined_group_ids(gpu_backend, same_machine=same_machine) if groups_effective else []
    )
    monkeypatch.setattr(os, "getgroups", lambda: effective)
    prepared_runner: list[mechanics.ProcessRunner] = []
    monkeypatch.setattr(
        provisioning_worker,
        "prepare_worker_resources",
        lambda *, runner, worker_root: (
            prepared_runner.append(runner)
            or provisioning_worker.WorkerResourcesReport(ready=True, missing=[])
        ),
    )
    writes: list[str] = []
    prompts: list[str] = []
    confirms: list[str] = []
    report: provisioning_worker.WorkerResourcesReport | None = None
    error: ValueError | None = None
    try:
        report = worker_install.install_worker(
            worker_install.WorkerInstallChoices(
                ssh_target=None if same_machine else "worker-host",
                same_machine=same_machine,
                gpu_backend=gpu_backend,
                gpu_target=gpu_target,
                gpu_device_index=0,
                preset=preset,
            ),
            runner=runner,
            read=lambda prompt: prompts.append(prompt) or "",
            confirm=lambda question: confirms.append(question) or install_rule,
            write=writes.append,
        )
    except ValueError as exc:
        error = exc
    return SimpleNamespace(
        repo=repo,
        runner=runner,
        report=report,
        error=error,
        writes=writes,
        prompts=prompts,
        confirms=confirms,
        prepared_runner=prepared_runner,
    )


def _load_written_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, object]:
    """Load the written worker file through the settings owner with no environment value."""
    monkeypatch.delenv(SETTINGS_SOURCE_MARKER, raising=False)
    for alias in WORKER_CONNECTION_KEYS | WORKER_GPU_KEYS | WORKER_SETUP_KEYS:
        monkeypatch.delenv(alias, raising=False)
    return load_worker_connection_settings(), load_worker_gpu_settings()


def _load_written_models(
    written_inference: dict[str, object], written_translation: dict[str, object]
) -> tuple[InferenceSettings, TranslationSettings]:
    """Load one install's written model files through the settings owner."""
    return InferenceSettings(**written_inference), TranslationSettings(**written_translation)


def test_worker_install_materializes_the_working_files_and_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An install creates every absent working file and generates the environment file once."""
    fresh = _install(monkeypatch, tmp_path, name="install-fresh")

    for _, destination in working_files.template_pairs(fresh.repo):
        assert (fresh.repo / destination).is_file()
    environment = fresh.repo / ".env"
    assert environment.stat().st_mode & 0o777 == 0o600
    generated = parse_local_configuration(environment.read_text(encoding="utf-8"))
    for key in ("POSTGRES_PASSWORD", "FEEDBACK_POSTGRES_PASSWORD"):
        assert generated[key] != "replace-with-local-secret"
        assert len(generated[key] or "") >= 32
    assert fresh.error is None
    assert fresh.report is not None and fresh.report.ready is True

    def keep(repo: Path) -> None:
        (repo / "docker-compose.yml").write_text("name: kept\n", encoding="utf-8")
        (repo / ".env").write_text(
            "POSTGRES_PASSWORD='kept'\nFEEDBACK_POSTGRES_PASSWORD='kept'\n", encoding="utf-8"
        )

    kept = _install(monkeypatch, tmp_path, name="install-kept", prepare=keep)
    assert (kept.repo / "docker-compose.yml").read_text(encoding="utf-8") == "name: kept\n"
    assert (kept.repo / ".env").read_text(encoding="utf-8") == (
        "POSTGRES_PASSWORD='kept'\nFEEDBACK_POSTGRES_PASSWORD='kept'\n"
    )


def test_worker_install_writes_the_worker_identity_for_the_detected_host_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each host kind writes its own identity keys and leaves a loadable worker file."""
    native = _install(monkeypatch, tmp_path, name="install-native")
    written = tomllib.loads((native.repo / "config" / "worker.toml").read_text(encoding="utf-8"))
    assert written["WORKER_HOST_KIND"] == "linux"
    assert written["WORKER_SSH_TARGET"] == "worker-host"
    assert written["WORKER_PROJECT_ROOT"] == str(native.repo.resolve())
    assert written["WORKER_SETUP_PROJECT_URL"] == WORKER_ORIGIN
    assert written["WORKER_GPU_BACKEND"] == "rocm"
    assert written["WORKER_GPU_TARGET"] == "gfx1201"
    assert written["WORKER_GPU_DEVICE_INDEX"] == 0
    assert "WORKER_WSL_DISTRIBUTION" not in written
    assert native.error is None
    # The final print names the server-side identity block the server's file must carry, and a
    # native Linux worker prints no WSL distribution.
    assert 'WORKER_HOST_KIND = "linux"' in native.writes
    assert f'WORKER_PROJECT_ROOT = "{native.repo.resolve()}"' in native.writes
    assert 'WORKER_GPU_TARGET = "gfx1201"' in native.writes
    assert not any(line.startswith("WORKER_WSL_DISTRIBUTION") for line in native.writes)

    # A second run on the same checkout writes the same identity: the distribution removal finds
    # the key already absent and leaves it absent.
    repeat = _install(monkeypatch, tmp_path, name="install-native")
    assert repeat.error is None
    assert (
        tomllib.loads((native.repo / "config" / "worker.toml").read_text(encoding="utf-8"))
        == written
    )

    connection, gpu = _load_written_identity(monkeypatch)
    assert connection.host_kind == "linux"
    assert connection.wsl_distribution is None
    assert gpu.gpu_backend == "rocm"

    distro = _install(monkeypatch, tmp_path, name="install-distro", host_kind="wsl")
    assert distro.error is None
    written = tomllib.loads((distro.repo / "config" / "worker.toml").read_text(encoding="utf-8"))
    assert written["WORKER_HOST_KIND"] == "wsl"
    assert written["WORKER_WSL_DISTRIBUTION"] == WSL_DISTRIBUTION
    assert distro.error is None
    assert _load_written_identity(monkeypatch)[0].wsl_distribution == WSL_DISTRIBUTION
    # A WSL install prints the distribution after the five shared keys.
    assert f'WORKER_WSL_DISTRIBUTION = "{WSL_DISTRIBUTION}"' in distro.writes


def test_same_machine_install_writes_the_container_identity_and_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A same-machine answer writes the container identity and the generated backend override.

    The ROCm answer adds the device nodes and the render and video group ids, the CUDA answer adds
    the NVIDIA runtime and the three resolved toolkit sonames, and a WSL host refuses the answer. A
    same-machine install creates the checkout's models directory that the worker service binds. A
    fresh account stops at the group check before any override, identity or directory exists, a
    second run with the groups in effect writes both files, and a running worker service is
    recreated with the new override after the image is prepared, while a stopped service is left to
    the first stage reservation.
    """
    gate = "install-same-machine-gated"
    stopped = _install(monkeypatch, tmp_path, name=gate, same_machine=True, groups_effective=False)
    assert stopped.error is not None
    assert not (stopped.repo / "docker-compose.worker.yml").exists()
    assert not (stopped.repo / "config" / "worker.toml").exists()
    assert not (stopped.repo / "models").exists()

    completed = _install(monkeypatch, tmp_path, name=gate, same_machine=True)
    assert completed.error is None
    assert (completed.repo / "docker-compose.worker.yml").is_file()
    assert (completed.repo / "config" / "worker.toml").is_file()
    # The worker service binds the checkout's models directory, so the install creates it.
    models_directory = completed.repo / "models"
    assert models_directory.is_dir()
    assert models_directory.stat().st_mode & 0o777 == 0o700
    override = (completed.repo / "docker-compose.worker.yml").read_text(encoding="utf-8")
    assert "/dev/kfd" in override
    assert "- /dev/dri" in override
    assert '"992"' in override
    assert '"44"' in override
    assert 'source: "/opt/rocm-7.2.1"' in override
    assert 'target: "/opt/rocm-7.2.1"' in override
    assert "create_host_path: false" in override

    rocm = _install(monkeypatch, tmp_path, name="install-same-machine-rocm", same_machine=True)
    assert rocm.error is None
    written = tomllib.loads((rocm.repo / "config" / "worker.toml").read_text(encoding="utf-8"))
    assert written["WORKER_HOST_KIND"] == "same-machine"
    assert "WORKER_SSH_TARGET" not in written
    assert "WORKER_WSL_DISTRIBUTION" not in written
    assert written["WORKER_PROJECT_ROOT"] == "/app"
    override_path = rocm.repo / "docker-compose.worker.yml"
    override = override_path.read_text(encoding="utf-8")
    assert override_path.stat().st_mode & 0o777 == 0o644
    assert "/dev/kfd" in override
    assert "- /dev/dri" in override
    assert '"992"' in override
    assert '"44"' in override
    assert 'source: "/opt/rocm-7.2.1"' in override
    assert 'target: "/opt/rocm-7.2.1"' in override
    assert "create_host_path: false" in override
    assert rocm.runner.recreated_services == []
    assert rocm.confirms == []
    assert rocm.prepared_runner == [rocm.runner]
    # The printed block names the five shared keys and no reach identity or WSL distribution.
    for key in (
        "WORKER_HOST_KIND",
        "WORKER_PROJECT_ROOT",
        "WORKER_GPU_BACKEND",
        "WORKER_GPU_TARGET",
        "WORKER_GPU_DEVICE_INDEX",
    ):
        assert any(line.startswith(f"{key} = ") for line in rocm.writes)
    assert not any(line.startswith("WORKER_SSH_TARGET") for line in rocm.writes)
    assert not any(line.startswith("WORKER_WSL_DISTRIBUTION") for line in rocm.writes)
    connection, _gpu = _load_written_identity(monkeypatch)
    assert connection.host_kind == "same-machine"
    assert connection.ssh_target is None
    assert connection.worker_project_root == Path("/app")

    def prepare_cuda(repo: Path) -> None:
        libraries = repo / "cuda-libs"
        libraries.mkdir()
        for name in ("libcudart.so.12", "libcublas.so.12", "libcublasLt.so.12"):
            (libraries / f"{name}.9").write_bytes(b"")
            (libraries / name).symlink_to(f"{name}.9")

    cuda = _install(
        monkeypatch,
        tmp_path,
        name="install-same-machine-cuda",
        same_machine=True,
        gpu_backend="cuda",
        gpu_target="86",
        worker_running=True,
        prepare=prepare_cuda,
    )
    assert cuda.error is None
    override = (cuda.repo / "docker-compose.worker.yml").read_text(encoding="utf-8")
    assert "runtime: nvidia" in override
    assert 'NVIDIA_VISIBLE_DEVICES: "0"' in override
    assert "create_host_path: false" in override
    for name in ("libcudart.so.12", "libcublas.so.12", "libcublasLt.so.12"):
        entry = cuda.repo / "cuda-libs" / name
        assert f'source: "{entry}.9"' in override
        assert f'target: "{entry}"' in override
    assert cuda.runner.recreated_services == ["worker"]

    distro = _install(
        monkeypatch,
        tmp_path,
        name="install-same-machine-wsl",
        host_kind="wsl",
        same_machine=True,
    )
    assert distro.error is not None
    assert not (distro.repo / "config").exists()


def test_worker_install_applies_the_selected_model_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each tracked preset writes both model files, and keep leaves operator values in place."""
    for name in ("12gb", "16gb", "8gb"):
        installed = _install(monkeypatch, tmp_path, name=f"install-preset-{name}", preset=name)
        assert installed.error is None
        assert installed.report is not None and installed.report.ready is True
        assert any(f"model preset: applied {name}" in line for line in installed.writes)

        preset_root = installed.repo / "examples" / "config" / "presets" / name
        written_inference = tomllib.loads(
            (installed.repo / "config" / "inference.toml").read_text(encoding="utf-8")
        )
        preset_inference = tomllib.loads(
            (preset_root / "inference.toml").read_text(encoding="utf-8")
        )
        written_translation = tomllib.loads(
            (installed.repo / "config" / "translation.toml").read_text(encoding="utf-8")
        )
        preset_translation = tomllib.loads(
            (preset_root / "translation.toml").read_text(encoding="utf-8")
        )
        assert (
            written_inference["INFERENCE_LOCAL_VARIANTS"]
            == preset_inference["INFERENCE_LOCAL_VARIANTS"]
        )
        for key, value in preset_translation.items():
            assert written_translation[key] == value

        inference, translation = _load_written_models(written_inference, written_translation)
        first, second = inference.inference_local_variants
        main, fallback = preset_inference["INFERENCE_LOCAL_VARIANTS"]
        for variant, fragment in ((first, main), (second, fallback)):
            assert inference.local_model_identity(variant) == (
                f"{fragment['model_repository']}@{fragment['model_revision']}"
                f";artifact={fragment['artifact_repository']}@{fragment['artifact_revision']}"
                f"/{fragment['artifact_file']}"
                f";quant={fragment['artifact_quant']}"
                f";sha256={fragment['artifact_sha256']}"
            )
            assert str(inference.local_artifact_url(variant)) == (
                f"https://huggingface.co/{fragment['artifact_repository']}"
                f"/resolve/{fragment['artifact_revision']}/{fragment['artifact_file']}"
            )
            assert (variant.context_size, variant.achieved_max_context) == (
                fragment["context_size"],
                fragment["achieved_max_context"],
            )
        # Every tracked preset gives its fallback a different file at a larger context.
        assert first.artifact_path != second.artifact_path
        assert first.artifact_sha256 != second.artifact_sha256
        assert second.context_size > first.context_size
        # The 16 GB main key is the only tracked key with MTP speculation.
        assert (first.mtp_draft_n_max is not None) == (name == "16gb")
        assert second.mtp_draft_n_max is None
        assert str(translation.translation_artifact_url) == (
            f"https://huggingface.co/{preset_translation['TRANSLATION_ARTIFACT_REPOSITORY']}"
            f"/resolve/{preset_translation['TRANSLATION_ARTIFACT_REVISION']}"
            f"/{preset_translation['TRANSLATION_ARTIFACT_FILE']}"
        )
        assert translation.provenance_model == (
            f"{preset_translation['TRANSLATION_MODEL_REPOSITORY']}"
            f"@{preset_translation['TRANSLATION_MODEL_REVISION']}"
            f";{preset_translation['TRANSLATION_ARTIFACT_QUANT']}"
            f";sha256:{preset_translation['TRANSLATION_ARTIFACT_SHA256']}"
        )

    # The committed example is the 16 GB preset's pair, so a fresh clone materializes it.
    example_inference = tomllib.loads(
        (REPO_ROOT / "examples" / "config" / "inference.toml").read_text(encoding="utf-8")
    )
    example_translation = tomllib.loads(
        (REPO_ROOT / "examples" / "config" / "translation.toml").read_text(encoding="utf-8")
    )
    sixteen_inference = tomllib.loads(
        (REPO_ROOT / "examples" / "config" / "presets" / "16gb" / "inference.toml").read_text(
            encoding="utf-8"
        )
    )
    sixteen_translation = tomllib.loads(
        (REPO_ROOT / "examples" / "config" / "presets" / "16gb" / "translation.toml").read_text(
            encoding="utf-8"
        )
    )
    assert (
        example_inference["INFERENCE_LOCAL_VARIANTS"]
        == sixteen_inference["INFERENCE_LOCAL_VARIANTS"]
    )
    for key, value in sixteen_translation.items():
        assert example_translation[key] == value

    # An install that answers keep leaves an operator-edited working file byte for byte.
    edited: list[str] = []

    def edit_inference(repo: Path) -> None:
        (repo / "config").mkdir()
        text = (repo / "examples" / "config" / "inference.toml").read_text(encoding="utf-8")
        text = text.replace('"Qwen/Qwen3.8-27B-FP8"', '"example/Operator-Model"')
        (repo / "config" / "inference.toml").write_text(text, encoding="utf-8")
        edited.append(text)

    kept = _install(monkeypatch, tmp_path, name="install-keep", prepare=edit_inference)
    assert kept.error is None
    assert (kept.repo / "config" / "inference.toml").read_text(encoding="utf-8") == edited[0]
    assert any("model preset: kept" in line for line in kept.writes)

    # An unknown preset refuses before any working file is written.
    unknown = _install(monkeypatch, tmp_path, name="install-unknown", preset="missing")
    assert unknown.error is not None
    assert "unknown model preset" in str(unknown.error)
    assert not (unknown.repo / "config").exists()
    assert not (unknown.repo / ".env").exists()

    # A preset the working files refuse stops before the generated passwords and the identity, and
    # the refusal names the preset. The working translation file names a local conversion, so the
    # preset's artifact source keys are absent from it.
    def local_conversion(repo: Path) -> None:
        write_local_conversion_translation(repo / "config" / "translation.toml")

    refused = _install(
        monkeypatch,
        tmp_path,
        name="install-preset-refused",
        preset="16gb",
        prepare=local_conversion,
    )
    assert refused.error is not None
    assert "16gb" in str(refused.error)
    assert (refused.repo / ".env").read_bytes() == (
        refused.repo / "examples" / ".env.example"
    ).read_bytes()
    assert (
        tomllib.loads((refused.repo / "config" / "worker.toml").read_text(encoding="utf-8"))[
            "WORKER_SETUP_PROJECT_URL"
        ]
        == "replace-with-project-url"
    )


def test_worker_install_runs_its_commands_without_passwordless_sudo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The install reaches its privileged steps through the interactive sudo form alone and reports
    a failed external step as one line."""
    native = _install(
        monkeypatch, tmp_path, name="install-privileges", missing_docker=True, install_rule=False
    )
    assert native.error is None
    recorded = native.runner.calls + native.runner.interactive_calls
    assert not any("-n" in command for command in recorded)
    interactive = native.runner.interactive_calls

    # Every privileged program is reached through an interactive sudo command, and the plain Docker
    # and package reads never elevate.
    privileged = {"apt-get", "install", "systemctl", "usermod", "visudo"}
    assert not any(command[0] in privileged for command in interactive)
    assert ("sudo", "apt-get", "update") in interactive
    assert any(
        command[:5] == ("sudo", "apt-get", "install", "-y", "--no-install-recommends")
        for command in interactive
    )
    destinations = [command[-1] for command in interactive if command[:2] == ("sudo", "install")]
    assert "/etc/apt/keyrings" in destinations
    assert "/etc/apt/keyrings/docker.asc" in destinations
    assert "/etc/apt/sources.list.d/docker.list" in destinations
    assert any(command[:1] == ("curl",) for command in interactive)
    assert ("sudo", "systemctl", "enable", "--now", "docker") in interactive
    usermod = next(command for command in interactive if command[:2] == ("sudo", "usermod"))
    assert set(usermod[3].split(",")) == {"docker", "render", "video"}
    assert native.prepared_runner == [native.runner]

    # The rule is offered on a native Linux host and written only on a yes.
    assert len(native.confirms) == 1
    assert "/etc/sudoers.d/course-explorer-worker-reboot" not in destinations
    assert not any(command[:2] == ("sudo", "visudo") for command in interactive)

    ruled = _install(
        monkeypatch, tmp_path, name="install-rule", missing_docker=False, install_rule=True
    )
    ruled_interactive = ruled.runner.interactive_calls
    # The rule is the one rendered file a sudo install publishes here, and the check of that file
    # follows it.
    rule_install = next(
        command
        for command in ruled_interactive
        if command[:2] == ("sudo", "install")
        and command[-1] == "/etc/sudoers.d/course-explorer-worker-reboot"
    )
    assert ruled.runner.installed_text is not None
    rule_line = next(
        line
        for line in ruled.runner.installed_text.splitlines()
        if "/usr/bin/systemctl reboot" in line and not line.startswith("#")
    )
    assert "WORKER_ACCOUNT" not in rule_line
    assert rule_line.startswith(f"{getpass.getuser()} ")
    assert "NOPASSWD: /usr/bin/systemctl reboot" in rule_line
    visudo_commands = [
        command for command in ruled_interactive if command[:3] == ("sudo", "visudo", "-c")
    ]
    assert len(visudo_commands) == 1
    assert ruled_interactive.index(visudo_commands[0]) > ruled_interactive.index(rule_install)

    # A WSL host writes no rule and prints the memory block for the operator to set.
    distro = _install(
        monkeypatch, tmp_path, name="install-distro-rule", host_kind="wsl", install_rule=True
    )
    assert distro.confirms == []
    assert not any(command[:2] == ("sudo", "visudo") for command in distro.runner.interactive_calls)
    assert not any(
        command[-1] == "/etc/sudoers.d/course-explorer-worker-reboot"
        for command in distro.runner.interactive_calls
    )
    assert len(distro.prompts) == 1
    assert {"[wsl2]", "memory=8GB", "swap=8GB"} <= set(distro.writes)

    # A WSL distribution without systemd refuses before the Docker service start and the group
    # join, so it writes nothing.
    non_systemd = _install(
        monkeypatch,
        tmp_path,
        name="install-wsl-non-systemd",
        host_kind="wsl",
        systemd=False,
    )
    assert non_systemd.error is not None
    assert not any(
        command[:3] == ("sudo", "systemctl", "enable") or command[:2] == ("sudo", "usermod")
        for command in non_systemd.runner.interactive_calls
    )
    assert not (non_systemd.repo / "config").exists()
    assert not (non_systemd.repo / ".env").exists()

    # A host without an Ubuntu lineage or an Ubuntu codename refuses with the one message before
    # any write.
    for name, unsupported in (
        ("install-debian", {"ID": "debian", "UBUNTU_CODENAME": "bookworm"}),
        ("install-lmde", {"ID": "linuxmint", "ID_LIKE": "debian"}),
        ("install-fedora", {"ID": "fedora", "VERSION_CODENAME": "40"}),
    ):
        refused = _install(monkeypatch, tmp_path, name=name, release=unsupported)
        assert refused.error is not None
        assert not (refused.repo / "config").exists()
        assert not (refused.repo / ".env").exists()

    # An Ubuntu derivative installs from the codename its release file reports, and Ubuntu reports
    # that codename in either key.
    for name, accepted_release, codename in (
        (
            "install-mint-21",
            {"ID": "linuxmint", "ID_LIKE": "ubuntu", "UBUNTU_CODENAME": "jammy"},
            "jammy",
        ),
        (
            "install-pop-os",
            {"ID": "pop", "ID_LIKE": "ubuntu debian", "UBUNTU_CODENAME": "noble"},
            "noble",
        ),
        (
            "install-ubuntu-version-codename",
            {"ID": "ubuntu", "VERSION_CODENAME": "oracular"},
            "oracular",
        ),
    ):
        accepted = _install(
            monkeypatch,
            tmp_path,
            name=name,
            missing_docker=True,
            install_rule=False,
            release=accepted_release,
        )
        assert accepted.error is None
        assert accepted.runner.installed_text is not None
        assert (
            f"https://download.docker.com/linux/ubuntu {codename} stable"
            in accepted.runner.installed_text
        )

    # The default Mint 22 host publishes the Ubuntu codename its release file reports.
    assert native.runner.installed_text is not None
    assert "https://download.docker.com/linux/ubuntu noble stable" in native.runner.installed_text

    # The group step stops a fresh account once: a running process holds its login groups, so the
    # stack preparation waits for the next login and no rule is offered before the stack exists.
    stopped = _install(monkeypatch, tmp_path, name="install-groups", groups_effective=False)
    assert stopped.error is not None
    assert stopped.prepared_runner == []
    assert not any(
        command[:2] == ("sudo", "visudo") for command in stopped.runner.interactive_calls
    )
    assert not any(
        command[-1] == "/etc/sudoers.d/course-explorer-worker-reboot"
        for command in stopped.runner.interactive_calls
    )

    # An external step that exits nonzero is one line that names the child program, not a traceback,
    # and the command leaves the rerun to the operator.
    def failed_command(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(100, ["sudo", "apt-get", "update"])

    monkeypatch.setattr(worker_install, "install_worker", failed_command)
    answers = iter(("", "worker-host", "rocm", "gfx1201", "0", "16gb"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert worker_install.main([]) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1
    assert "apt-get" in captured.err
    assert "sudo failed" not in captured.err

    # A prompt the command refuses, an ended input and an interrupt each report one line.
    answers = iter(("", "worker-host", "rocm", "gfx1201", "first"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert worker_install.main([]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1

    answers = iter(("", "worker-host", "vulkan"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert worker_install.main([]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def ended(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", ended)
    assert worker_install.main([]) == 0
    assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert worker_install.main([]) == 130
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_same_machine_code_update_is_refused_before_any_transport_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-machine code update is the server deployment, so it builds no transport."""
    monkeypatch.setattr(
        provisioning,
        "load_worker_connection_settings",
        lambda: SimpleNamespace(host_kind="same-machine"),
    )

    def unexpected_transport(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused code update builds no transport")

    monkeypatch.setattr(provisioning, "build_worker_transport", unexpected_transport)

    with pytest.raises(ValueError, match="a same-machine worker updates with the server deploy"):
        provisioning.update_worker_code(runner=mechanics.SubprocessRunner())


def test_preset_application_refuses_before_writing_either_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A preset the working files refuse leaves both model files byte-identical.

    The translation working file names a local conversion, so it carries no artifact source keys
    and the preset's source keys are absent from it.
    """
    repo = _install_checkout(tmp_path, "preset-refusal")
    monkeypatch.setattr(administration, "repository_root", lambda: repo)
    working_files.copy_example_files(repo)
    write_local_conversion_translation(repo / "config" / "translation.toml")
    shutil.copyfile(
        repo / "examples" / "config" / "inference.toml", repo / "config" / "inference.toml"
    )
    before = {
        name: (repo / "config" / name).read_bytes()
        for name in ("inference.toml", "translation.toml")
    }

    with pytest.raises(
        ValueError, match="configuration key is not present in config/translation.toml"
    ):
        worker_install.apply_model_preset(repo, "16gb")

    assert {
        name: (repo / "config" / name).read_bytes()
        for name in ("inference.toml", "translation.toml")
    } == before
