"""Worker-side readiness read and resource preparation helper.

This module runs on the worker host (invoked as
``python -m course_explorer_admin.provisioning_worker check``, or ``check --in-container`` from the
same-machine worker container) and owns the one readiness read plus the one mutation of missing
pinned worker resources: the configured worker packages, which follow the GPU backend and the host
kind, the Docker Compose ``db`` image and a same-machine worker's ``worker`` image, the pinned GGUFs,
and the pinned llama.cpp runtimes built for the configured backend and target. ``check`` performs
only the readiness read, and the local worker install calls the preparation once. The install owns the
Docker service start, so neither read starts it. ``_inspect_worker_resources`` loads the
translation, inference and rerank settings to inspect the pinned resources. Both functions reach
Compose through the shared ``course_explorer_admin.compose`` bridge, which consumes the validated
database credentials into a mode-0600 temporary file removed on exit, and they reach Docker through
its plain command, so neither needs password-free ``sudo``. Its report carries fixed component names
alone. The caller states where the read runs. Inside a same-machine worker container the image owns
the host packages, so the container read checks the mounted device node and each host-built runtime's
resolved libraries instead of the vendor tool. The install's own host-side preparation names the host
explicitly and probes the vendor tool against the configured target before it builds anything.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from course_explorer import administration
from course_explorer.config import (
    WorkerGpuSettings,
    load_inference_rerank_settings,
    load_inference_settings,
    load_shared_storage_settings,
    load_translation_settings,
    load_worker_connection_settings,
    load_worker_gpu_settings,
    load_worker_setup_settings,
)
from course_explorer_admin import mechanics
from course_explorer_admin.artifacts import artifact_present, download_artifact

# The Docker Engine packages the worker installs from Docker's official apt source.
DOCKER_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
# The base ordered package-name list every worker installs.
_BASE_SYSTEM_PACKAGES = (
    "build-essential",
    "ca-certificates",
    "cmake",
    "curl",
    "git",
    "iproute2",
    "libcurl4-openssl-dev",
    "pkg-config",
    *DOCKER_PACKAGES,
)
# The versioned ROCm apt source owns its pin, so the ROCm names carry no version.
_ROCM_PACKAGES = ("rocm",)
_WSL_ROCM_PACKAGES = ("rocdxg-roct",)


def system_packages(*, host_kind: str, gpu_backend: str) -> tuple[str, ...]:
    """Return the fixed worker package list the host kind and GPU backend require.

    A CUDA worker installs no GPU package: its driver and CUDA toolkit are host-provided.
    """
    if gpu_backend != "rocm":
        return _BASE_SYSTEM_PACKAGES
    if host_kind == "wsl":
        return (*_BASE_SYSTEM_PACKAGES, *_ROCM_PACKAGES, *_WSL_ROCM_PACKAGES)
    return (*_BASE_SYSTEM_PACKAGES, *_ROCM_PACKAGES)


_RUNTIME_CLONE_URL = "https://github.com/ggml-org/llama.cpp.git"
_TRANSLATION_GUIDANCE = (
    "missing translation artifact: the configuration names a local conversion, and no automated "
    "converter exists"
)
_TRANSLATION_DOWNLOAD_GUIDANCE = (
    "missing translation artifact: run the worker install to download the configured file"
)
# The database image carries the serving extension only when the final image stage declares this
# capability. Readiness reads the label of the image the effective Compose project names, so a
# matching tag alone is not readiness.
_DB_EXTENSION_LABEL = "org.course-explorer.db-extension"
_DB_EXTENSION_VERSION = "1.0"


class WorkerResourcesReport(BaseModel):
    """Ordinary frozen readiness report naming only fixed component tokens."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    missing: list[str]


def check_worker_resources(
    *, runner: mechanics.ProcessRunner, worker_root: Path | None = None, in_container: bool = False
) -> WorkerResourcesReport:
    """Read worker readiness only, mutating nothing and reporting fixed component names.

    ``worker_root`` is the checkout the reads run against, and an absent value is the current
    directory. ``in_container`` states that the read runs inside the same-machine worker container.
    """
    root = Path.cwd() if worker_root is None else worker_root
    return _inspect_worker_resources(runner, root, prepare=False, in_container=in_container)


def prepare_worker_resources(
    *, runner: mechanics.ProcessRunner, worker_root: Path | None = None
) -> WorkerResourcesReport:
    """Verify or acquire the configured packages, db image, artifacts, and runtimes.

    ``worker_root`` is the checkout the reads and the builds run against, and an absent value is the
    current directory. The machine shared model and reservation directories are created before the
    inspection, so a worker that stores its additional models in the shared directory has the layout
    prepared. The caller starts the Docker service first, because the install owns that one service
    start.
    """
    root = Path.cwd() if worker_root is None else worker_root
    shared = load_shared_storage_settings()
    shared.models_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    shared.locks_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _inspect_worker_resources(runner, root, prepare=True, in_container=False)


def _inspect_worker_resources(
    runner: mechanics.ProcessRunner, worker_root: Path, *, prepare: bool, in_container: bool
) -> WorkerResourcesReport:
    translation = load_translation_settings()
    inference = load_inference_settings()
    rerank = load_inference_rerank_settings()
    connection = load_worker_connection_settings()
    gpu = load_worker_gpu_settings()
    timeout = load_worker_setup_settings().worker_setup_timeout_seconds

    missing: list[str] = []
    if not in_container:
        # The worker image owns the package names when the readiness read runs inside the container.
        missing.extend(
            missing_packages(
                runner,
                worker_root,
                system_packages(host_kind=connection.host_kind, gpu_backend=gpu.gpu_backend),
            )
        )
    gpu_missing = _gpu_stack_missing(
        runner,
        worker_root,
        host_kind=connection.host_kind,
        gpu=gpu,
        in_container=in_container,
        runtimes=(
            mechanics.runtime_executable(
                translation.translation_runtime_revision, gpu.gpu_backend, gpu.gpu_target
            ),
            mechanics.runtime_executable(
                inference.inference_runtime_revision, gpu.gpu_backend, gpu.gpu_target
            ),
        ),
    )
    missing.extend(gpu_missing)

    if not _docker_ready(runner, worker_root):
        missing.append("docker")
    elif not _docker_compose_ready(runner, worker_root):
        missing.append("docker-compose")
    else:
        image = _db_image_name(runner, worker_root)
        if not _db_image_present(runner, worker_root, image):
            if prepare:
                runner.run_interactive(
                    (*mechanics.compose_prefix(interpreter=True), "build", "db"),
                    cwd=worker_root,
                    timeout=timeout,
                )
            if not _db_image_present(runner, worker_root, image):
                missing.append("db-image")
        if prepare and connection.host_kind == "same-machine":
            # The worker container runs the same checkout, so its image belongs to this preparation
            # and a build failure is one loud error rather than a missing readiness component.
            runner.run_interactive(
                (*mechanics.compose_prefix(interpreter=True), "build", "worker"),
                cwd=worker_root,
                timeout=timeout,
            )

    translation_url = translation.translation_artifact_url
    artifacts = [
        (
            worker_root / translation.translation_artifact_path,
            translation.translation_artifact_sha256,
            "translation-artifact",
            str(translation_url) if translation_url is not None else None,
        )
    ]
    seen_generation: set[tuple[Path, str]] = set()
    for index, variant in enumerate(inference.inference_local_variants):
        path = worker_root / variant.artifact_path
        identity = (path, variant.artifact_sha256)
        if identity in seen_generation:
            continue
        seen_generation.add(identity)
        artifacts.append(
            (
                path,
                variant.artifact_sha256,
                "generation-artifact" if index == 0 else "generation-artifact-fallback",
                str(inference.local_artifact_url(variant)),
            )
        )
    artifacts.append(
        (
            worker_root / rerank.inference_rerank_artifact_path,
            rerank.inference_rerank_artifact_sha256,
            "rerank-artifact",
            str(rerank.rerank_artifact_url),
        )
    )
    for path, digest, token, url in artifacts:
        if artifact_present(path, digest):
            continue
        if prepare and url is not None:
            download_artifact(runner, worker_root, path, digest, url, timeout)
            continue
        missing.append(token)

    runtimes: dict[str, list[str]] = {}
    runtimes.setdefault(translation.translation_runtime_revision, []).append("translation-runtime")
    runtimes.setdefault(inference.inference_runtime_revision, []).append("inference-runtime")
    for revision, tokens in runtimes.items():
        missing.extend(
            _runtime_missing(
                runner,
                worker_root,
                revision,
                tokens,
                prepare=prepare and "gpu-stack" not in gpu_missing,
                timeout=timeout,
                gpu=gpu,
            )
        )

    return WorkerResourcesReport(ready=not missing, missing=missing)


def missing_packages(
    runner: mechanics.ProcessRunner, worker_root: Path, packages: tuple[str, ...]
) -> list[str]:
    """Return the configured package names the host does not report as installed.

    A host without ``dpkg-query`` reports that one command as the missing component, not every
    configured package.
    """
    missing: list[str] = []
    for package in packages:
        try:
            status = (
                runner.run(("dpkg-query", "-W", "-f=${Status}", package), cwd=worker_root)
                .stdout.decode("utf-8", errors="replace")
                .strip()
            )
        except FileNotFoundError:
            return ["dpkg-query"]
        except subprocess.CalledProcessError:
            missing.append(package)
        else:
            if status != "install ok installed":
                missing.append(package)
    return missing


def _gpu_stack_missing(
    runner: mechanics.ProcessRunner,
    worker_root: Path,
    *,
    host_kind: str,
    gpu: WorkerGpuSettings,
    in_container: bool,
    runtimes: tuple[Path, ...],
) -> list[str]:
    """Report the one fixed component when the configured GPU stack does not answer.

    A ROCm host worker needs ``rocminfo`` to name the configured compute target, and a WSL worker
    needs the ROCDXG device flag for that read. A CUDA host worker needs ``nvcc`` and a card whose
    compute capability equals the configured target. The install's host-side preparation of a
    same-machine host reads those host tools, because the runtime is built on the host. A
    same-machine readiness read runs inside the worker container, whose image carries no vendor
    tool, so it reads the mounted device node and the loader's view of each host-built runtime
    instead.
    """
    if in_container:
        return _container_gpu_stack_missing(runner, worker_root, gpu=gpu, runtimes=runtimes)
    probe_kind = "wsl" if host_kind == "wsl" else "linux"
    if gpu.gpu_backend == "rocm":
        probe = (
            ("bash", "-lc", "HSA_ENABLE_DXG_DETECTION=1 rocminfo")
            if probe_kind == "wsl"
            else ("rocminfo",)
        )
        try:
            output = runner.run(probe, cwd=worker_root).stdout.decode("utf-8", errors="replace")
        except subprocess.CalledProcessError, FileNotFoundError:
            return ["gpu-stack"]
        if re.search(rf"Name:\s+{re.escape(gpu.gpu_target)}\b", output) is None:
            return ["gpu-stack"]
        return []
    try:
        runner.run(("nvcc", "--version"), cwd=worker_root)
    except subprocess.CalledProcessError, FileNotFoundError:
        return ["gpu-stack"]
    return _nvidia_capability_missing(runner, worker_root, gpu)


def _container_gpu_stack_missing(
    runner: mechanics.ProcessRunner,
    worker_root: Path,
    *,
    gpu: WorkerGpuSettings,
    runtimes: tuple[Path, ...],
) -> list[str]:
    """Read the container's mounted device node and the host-built runtime's libraries.

    An absent device node or an unresolved runtime library is the one ``gpu-stack`` component. An
    absent runtime executable is the runtime component's own missing report, so it is skipped here.
    The container carries no vendor tool, so the device node and the loader's view of each mounted
    runtime are its whole evidence, and no vendor command runs.
    """
    device = "/dev/kfd" if gpu.gpu_backend == "rocm" else "/dev/nvidia0"
    if not _path_present(runner, worker_root, Path(device)):
        return ["gpu-stack"]
    for executable in runtimes:
        if not _path_present(runner, worker_root, executable):
            continue
        try:
            output = runner.run(("ldd", str(executable)), cwd=worker_root).stdout.decode(
                "utf-8", errors="replace"
            )
        except subprocess.CalledProcessError, FileNotFoundError:
            return ["gpu-stack"]
        if "not found" in output:
            return ["gpu-stack"]
    return []


def _path_present(runner: mechanics.ProcessRunner, worker_root: Path, path: Path) -> bool:
    """Report whether one container path exists, through the worker's own command runner."""
    try:
        runner.run(("test", "-e", str(path)), cwd=worker_root)
    except subprocess.CalledProcessError, FileNotFoundError:
        return False
    return True


def _nvidia_capability_missing(
    runner: mechanics.ProcessRunner, worker_root: Path, gpu: WorkerGpuSettings
) -> list[str]:
    """Report whether the configured card's compute capability equals the configured CUDA target."""
    try:
        output = runner.run(
            (
                "nvidia-smi",
                f"--id={gpu.gpu_device_index}",
                "--query-gpu=compute_cap",
                "--format=csv,noheader",
            ),
            cwd=worker_root,
        ).stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError, FileNotFoundError:
        return ["gpu-stack"]
    capability = f"{gpu.gpu_target[:-1]}.{gpu.gpu_target[-1]}"
    return [] if capability in {line.strip() for line in output.splitlines()} else ["gpu-stack"]


def _docker_ready(runner: mechanics.ProcessRunner, worker_root: Path) -> bool:
    """Report whether this account reaches the Docker socket.

    The read uses the plain Docker command, because a socket this account cannot write is the
    ``docker`` component and not an elevation. A captured command holds no terminal, so the
    command fails and the report names ``docker`` alone.
    """
    try:
        runner.run(("docker", "info"), cwd=worker_root)
    except subprocess.CalledProcessError, FileNotFoundError:
        return False
    return True


def _docker_compose_ready(runner: mechanics.ProcessRunner, worker_root: Path) -> bool:
    """Report whether the Compose plugin answers through the plain Compose command."""
    try:
        runner.run((*mechanics.compose_prefix(interpreter=True), "version"), cwd=worker_root)
    except subprocess.CalledProcessError, FileNotFoundError:
        return False
    return True


def _db_image_name(runner: mechanics.ProcessRunner, worker_root: Path) -> str:
    output = runner.run(
        (*mechanics.compose_prefix(interpreter=True), "config", "--images", "db"), cwd=worker_root
    ).stdout.decode("utf-8")
    image = output.strip()
    if not image:
        raise ValueError("configured db service image is blank")
    return image


def _db_image_present(runner: mechanics.ProcessRunner, worker_root: Path, image: str) -> bool:
    """Report whether the effective project's database image carries the serving extension.

    The read-only inspection reads the final image's extension label and compares it with the
    internal capability version. A missing image or label, an absent Docker command, or an
    unreadable image is not ready.
    """
    try:
        output = runner.run(
            (
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{_DB_EXTENSION_LABEL}" }}}}',
                image,
            ),
            cwd=worker_root,
        ).stdout.decode("utf-8")
    except subprocess.CalledProcessError, FileNotFoundError:
        return False
    return output.strip() == _DB_EXTENSION_VERSION


def _runtime_executable_ready(executable: Path) -> bool:
    """Report whether one prepared runtime executable exists and can actually be executed."""
    return executable.is_file() and os.access(executable, os.X_OK)


def _runtime_missing(
    runner: mechanics.ProcessRunner,
    worker_root: Path,
    revision: str,
    tokens: list[str],
    *,
    prepare: bool,
    timeout: float,
    gpu: WorkerGpuSettings,
) -> list[str]:
    executable = mechanics.runtime_executable(revision, gpu.gpu_backend, gpu.gpu_target)
    directory = mechanics.runtime_checkout_directory(revision, gpu.gpu_backend, gpu.gpu_target)
    if directory.exists():
        require_runtime_checkout(runner, directory, revision, timeout)
        if _runtime_executable_ready(executable):
            return []
        if not prepare:
            return list(tokens)
        _build_runtime(runner, directory, timeout, gpu=gpu)
        return [] if _runtime_executable_ready(executable) else list(tokens)
    if not prepare:
        return list(tokens)
    runner.run_interactive(
        ("git", "clone", _RUNTIME_CLONE_URL, str(directory)),
        cwd=worker_root,
        timeout=timeout,
    )
    runner.run_interactive(
        ("git", "-C", str(directory), "checkout", "--detach", revision),
        cwd=worker_root,
        timeout=timeout,
    )
    _build_runtime(runner, directory, timeout, gpu=gpu)
    return [] if _runtime_executable_ready(executable) else list(tokens)


def require_runtime_checkout(
    runner: mechanics.ProcessRunner, directory: Path, revision: str, timeout: float
) -> None:
    """Require a runtime checkout at the exact clean configured revision."""
    head = (
        runner.run(
            ("git", "-C", str(directory), "rev-parse", "HEAD"), cwd=directory, timeout=timeout
        )
        .stdout.decode("utf-8")
        .strip()
    )
    if head != revision:
        raise ValueError("runtime checkout revision does not match configuration")
    dirty = runner.run(
        ("git", "-C", str(directory), "status", "--porcelain"), cwd=directory, timeout=timeout
    ).stdout.decode("utf-8")
    if dirty.strip():
        raise ValueError("runtime checkout must be clean")


def require_prepared_runtime(
    *,
    runner: mechanics.ProcessRunner,
    revision: str,
    artifact: Path,
    artifact_sha256: str,
    timeout: float,
    gpu_backend: str,
    gpu_target: str,
) -> None:
    """Require an already-prepared pinned runtime and artifact.

    This is the recurring fresh-launch readiness read: it reuses the one checkout convention and the
    executable and pinned artifact-SHA-256 checks, so every fresh launch verifies the
    authoritative checkout revision, executable, and artifact hash. A live same-spec reuse in
    ``OwnedWorkerRuntime.start`` reuses the running process. The explicit setup/update path owns
    every download, clone, build, and install. A missing component raises fixed setup/update
    guidance. A present but mismatching component fails through the checkout/checksum errors.
    """
    executable = mechanics.runtime_executable(revision, gpu_backend, gpu_target)
    directory = mechanics.runtime_checkout_directory(revision, gpu_backend, gpu_target)
    if not directory.exists():
        raise ValueError("worker runtime is not prepared; run explicit setup/update")
    require_runtime_checkout(runner, directory, revision, timeout)
    if not _runtime_executable_ready(executable):
        raise ValueError("worker runtime executable is not ready; run explicit setup/update")
    if not artifact_present(artifact, artifact_sha256):
        raise ValueError("worker model artifact is not prepared; run explicit setup/update")


def _build_runtime(
    runner: mechanics.ProcessRunner,
    directory: Path,
    timeout: float,
    *,
    gpu: WorkerGpuSettings,
) -> None:
    # The model servers run with ``--no-webui``, so the build embeds no web UI and fetches no
    # prebuilt UI assets.
    if gpu.gpu_backend == "rocm":
        # A ROCm install may place its root in a versioned directory, so the same hipconfig read
        # supplies the prefix CMake's HIP lookup uses.
        configure = (
            'ROCM_PATH="$(hipconfig -R)" HIPCXX="$(hipconfig -l)/clang" '
            'HIP_PATH="$(hipconfig -R)" '
            "cmake -B build -S . -DCMAKE_BUILD_TYPE=Release -DLLAMA_USE_PREBUILT_UI=OFF "
            "-DGGML_HIP=ON "
            f"-DGPU_TARGETS={gpu.gpu_target}"
        )
    else:
        configure = (
            "cmake -B build -S . -DCMAKE_BUILD_TYPE=Release -DLLAMA_USE_PREBUILT_UI=OFF "
            "-DGGML_CUDA=ON "
            f"-DCMAKE_CUDA_ARCHITECTURES={gpu.gpu_target}"
        )
    script = (
        "set -euo pipefail; "
        f"cd {shlex.quote(str(directory))}; "
        f"{configure}; "
        'cmake --build build --config Release --target llama-server -j "$(nproc)"'
    )
    runner.run_interactive(("bash", "-c", script), cwd=directory, timeout=timeout)


def _print_translation_guidance(report: WorkerResourcesReport) -> None:
    if "translation-artifact" in report.missing:
        guidance = (
            _TRANSLATION_GUIDANCE
            if load_translation_settings().translation_artifact_url is None
            else _TRANSLATION_DOWNLOAD_GUIDANCE
        )
        print(guidance, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Expose the ``check`` action and its container statement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check"])
    parser.add_argument(
        "--in-container",
        action="store_true",
        help="state that the read runs inside the same-machine worker container",
    )
    arguments = parser.parse_args(argv)
    report = check_worker_resources(
        runner=mechanics.SubprocessRunner(),
        worker_root=administration.repository_root(),
        in_container=arguments.in_container,
    )
    _print_translation_guidance(report)
    print(report.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
