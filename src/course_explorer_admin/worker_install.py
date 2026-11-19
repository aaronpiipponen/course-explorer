"""One interactive worker install that materializes the worker's working files and prepares its
stack.

The install runs on the worker host in its own terminal. An SSH worker writes its reach identity,
and a same-machine worker instead writes the container project root that the instance's worker
container runs at, joins only the Docker socket group and generates the GPU override that adds the
host's device nodes or NVIDIA runtime to that container. The packages and the account's groups are
installed before any working file, identity or override is written, so the first run of a fresh
account stops at the group check with nothing written and the next login's run completes the same
install.
"""

from __future__ import annotations

import grp
import json
import os
import platform
import pwd
import secrets
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from course_explorer import administration
from course_explorer.config import (
    WorkerGpuSettings,
    load_worker_setup_settings,
)
from course_explorer.configuration import configuration_candidate
from course_explorer_admin import (
    compose,
    configuration,
    mechanics,
    provisioning_worker,
    working_files,
)

# The install reaches the Docker Engine repository from Docker's own Ubuntu publication, whatever
# the host distribution calls itself, because its packages are published for Ubuntu releases.
_DOCKER_KEY_URL = "https://download.docker.com/linux/ubuntu/gpg"
_DOCKER_KEYRING = "/etc/apt/keyrings/docker.asc"
_DOCKER_SOURCE_PATH = "/etc/apt/sources.list.d/docker.list"
_REBOOT_RULE_PATH = "/etc/sudoers.d/course-explorer-worker-reboot"
_REBOOT_TEMPLATE = Path("examples") / "deploy" / "course-explorer-worker-reboot.sudoers.in"
# The ROCm device nodes belong to these groups. An account outside them cannot open /dev/kfd or
# /dev/dri, so rocminfo reports no device and the model servers cannot reach the card.
_ROCM_DEVICE_GROUPS = ("render", "video")
# The three CUDA toolkit libraries a model child loads, and the Docker runtime it needs.
_CUDA_LIBRARY_SONAMES = ("libcudart.so.12", "libcublas.so.12", "libcublasLt.so.12")
_NVIDIA_RUNTIME_GUIDANCE = (
    "the CUDA worker requires Docker's nvidia runtime. Install the NVIDIA Container Toolkit, "
    "register the runtime with Docker and run the install again"
)
# The container path of the worker image root, which a same-machine project root names.
_WORKER_CONTAINER_ROOT = "/app"
_WSL_MEMORY_BLOCK = ("[wsl2]", "memory=8GB", "swap=8GB")
_SUPPORTED_DISTRIBUTIONS = "an Ubuntu-based Linux distribution"
# The one refusal both answer checks report, because a settings dump is longer than one guidance line.
_INVALID_ANSWERS = "one of the install answers is invalid; answer the prompts again"


class WorkerInstallChoices(BaseModel):
    """The operator's worker install answers, validated before any file changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ssh_target: str | None = None
    same_machine: bool = False
    gpu_backend: Literal["rocm", "cuda"]
    gpu_target: str
    gpu_device_index: int
    preset: str | None = None

    @model_validator(mode="after")
    def validate_same_machine(self) -> Self:
        """Require one reach form: an SSH target alone or the same-machine answer alone."""
        if self.same_machine and self.ssh_target is not None:
            raise ValueError("a same-machine worker install names no SSH target")
        if not self.same_machine and self.ssh_target is None:
            raise ValueError("a remote worker install requires an SSH target")
        return self


class _HostFacts(BaseModel):
    """The detected host kind, its WSL distribution, its Docker codename and its origin URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host_kind: Literal["wsl", "linux"]
    wsl_distribution: str | None
    docker_codename: str
    origin: str


def install_worker(
    choices: WorkerInstallChoices,
    *,
    runner: mechanics.ProcessRunner,
    read: Callable[[str], str],
    confirm: Callable[[str], bool],
    write: Callable[[str], None],
) -> provisioning_worker.WorkerResourcesReport:
    """Install the worker stack on this host and return the prepared resource report.

    The host is checked before any write, the answers are validated through the worker GPU settings
    before the package install, and the package, service and rule steps run through the interactive
    runner, so a ``sudo`` password prompt reaches the terminal. The packages and the account's
    groups come before every written file, so a fresh account's first run stops at the group check
    with no working file, identity or override on disk. The Docker service start belongs to this
    install alone, and the readiness read that follows never starts it.
    """
    root = administration.repository_root()
    write("check the worker host")
    facts = _host_facts(root, runner)
    if choices.same_machine and facts.host_kind == "wsl":
        raise ValueError(
            "a same-machine worker runs on native Linux, so this WSL host cannot select it"
        )

    # The answer check reads no working file: the candidate block holds no stored value, so the
    # settings class validates the three answers alone and a bad answer refuses before any write.
    try:
        with configuration_candidate(documents={}, local={}):
            validated = WorkerGpuSettings(
                WORKER_GPU_BACKEND=choices.gpu_backend,
                WORKER_GPU_TARGET=choices.gpu_target,
                WORKER_GPU_DEVICE_INDEX=choices.gpu_device_index,
            )
    except ValidationError as exc:
        raise ValueError(_INVALID_ANSWERS) from exc

    if choices.preset is not None:
        # Parse the preset fragment pair before the packages, so an unknown preset refuses at once.
        preset_values = _preset_values(root, choices.preset)
    else:
        preset_values = None

    write("install the worker packages")
    _install_packages(
        root,
        runner,
        write,
        facts=facts,
        gpu_backend=validated.gpu_backend,
    )

    write("start Docker and join the group")
    account = pwd.getpwuid(os.getuid()).pw_name
    if facts.host_kind == "wsl":
        _require_systemd(root, runner)
    _join_groups(
        account=account,
        chosen_backend=validated.gpu_backend,
        same_machine=choices.same_machine,
        runner=runner,
        root=root,
    )
    if not _groups_effective(validated.gpu_backend, same_machine=choices.same_machine):
        raise ValueError(
            "the new group membership takes effect in a new login session. Sign in again and run "
            "this command once more to prepare the stack."
        )

    if choices.same_machine:
        # The worker service binds this checkout's models directory with no host-path creation, so
        # the directory exists before that container starts.
        (root / "models").mkdir(mode=0o700, parents=True, exist_ok=True)
        # The override belongs to the worker container, so it is written before the identity that
        # selects that container.
        write("write the generated worker compose override")
        _write_worker_compose_override(
            root,
            gpu_backend=validated.gpu_backend,
            gpu_target=validated.gpu_target,
            gpu_device_index=validated.gpu_device_index,
            runner=runner,
        )

    write("write the worker working files and identity")
    created_environment = working_files.copy_example_files(root)
    if preset_values is not None:
        # The chosen preset is refused before any generated password or identity reaches a file, so a
        # preset the working files do not accept leaves the credentials, identity and preset unwritten.
        _validate_preset(choices.preset, preset_values)
    if created_environment:
        for key in ("POSTGRES_PASSWORD", "FEEDBACK_POSTGRES_PASSWORD"):
            configuration.set_configuration_value(
                ".env", key, json.dumps(secrets.token_urlsafe(32))
            )
    identity = {
        "WORKER_HOST_KIND": "same-machine" if choices.same_machine else facts.host_kind,
        "WORKER_SSH_TARGET": choices.ssh_target,
        "WORKER_PROJECT_ROOT": (
            _WORKER_CONTAINER_ROOT if choices.same_machine else str(root.resolve())
        ),
        "WORKER_SETUP_PROJECT_URL": facts.origin,
        "WORKER_GPU_BACKEND": validated.gpu_backend,
        "WORKER_GPU_TARGET": validated.gpu_target,
        "WORKER_GPU_DEVICE_INDEX": validated.gpu_device_index,
        "WORKER_WSL_DISTRIBUTION": (None if choices.same_machine else facts.wsl_distribution),
    }
    configuration.apply_configuration_values("config/worker.toml", identity)
    if choices.preset is None:
        write("model preset: kept the current configuration values")
    else:
        apply_model_preset(root, choices.preset, values=preset_values)
        write(f"model preset: applied {choices.preset}")
    timeout = load_worker_setup_settings().worker_setup_timeout_seconds

    write("prepare the worker stack")
    report = provisioning_worker.prepare_worker_resources(runner=runner, worker_root=root)
    if choices.same_machine:
        _recreate_running_worker(root, runner, timeout=timeout)
    if facts.host_kind == "linux" and not choices.same_machine:
        _install_reboot_rule(
            root, account, runner=runner, confirm=confirm, write=write, timeout=timeout
        )
    elif facts.host_kind == "wsl":
        _show_wsl_memory_block(read=read, write=write)
    write(report.model_dump_json())
    if choices.same_machine:
        write(
            "the server's config/worker.toml must carry the same values, and a same-machine worker "
            "names no WORKER_SSH_TARGET:"
        )
    else:
        write(
            "the server's config/worker.toml must carry the same values, and its WORKER_SSH_TARGET "
            "names this host as the server reaches it:"
        )
    keys = [
        "WORKER_HOST_KIND",
        "WORKER_PROJECT_ROOT",
        "WORKER_GPU_BACKEND",
        "WORKER_GPU_TARGET",
        "WORKER_GPU_DEVICE_INDEX",
    ]
    if facts.host_kind == "wsl":
        keys.append("WORKER_WSL_DISTRIBUTION")
    for key in keys:
        value = identity[key]
        write(f"{key} = {json.dumps(value) if isinstance(value, str) else value}")
    return report


def _available_presets(root: Path) -> tuple[str, ...]:
    """Return the sorted tracked preset names that carry both configuration fragments."""
    directory = root / "examples" / "config" / "presets"
    if not directory.is_dir():
        return ()
    return tuple(
        path.name
        for path in sorted(directory.iterdir())
        if (path / "inference.toml").is_file() and (path / "translation.toml").is_file()
    )


def _preset_values(root: Path, preset: str) -> tuple[dict[str, object], dict[str, object]]:
    """Parse one tracked preset's inference and translation fragments."""
    if preset not in _available_presets(root):
        raise ValueError(f"unknown model preset: {preset}")
    directory = root / "examples" / "config" / "presets" / preset
    return (
        tomllib.loads((directory / "inference.toml").read_text(encoding="utf-8")),
        tomllib.loads((directory / "translation.toml").read_text(encoding="utf-8")),
    )


def _validate_preset(preset: str, values: tuple[dict[str, object], dict[str, object]]) -> None:
    """Validate both preset fragments against the working files and name the preset on a refusal."""
    inference_values, translation_values = values
    try:
        configuration.validate_configuration_values("config/inference.toml", inference_values)
        configuration.validate_configuration_values("config/translation.toml", translation_values)
    except ValueError as exc:
        raise ValueError(
            f"the model preset {preset} does not apply to this checkout: {exc}"
        ) from exc


def apply_model_preset(
    root: Path,
    preset: str,
    *,
    values: tuple[dict[str, object], dict[str, object]] | None = None,
) -> None:
    """Write one tracked preset's two fragments to the working files.

    ``values`` is an already parsed and validated pair. An absent value parses the tracked
    fragments and validates them here. Both fragments are written in the same call, and each
    working file is validated by :func:`configuration.apply_configuration_values` before that file
    is written.
    """
    if values is None:
        pair = _preset_values(root, preset)
        _validate_preset(preset, pair)
    else:
        # The caller validated the already parsed pair before any credential or identity write.
        pair = values
    inference_values, translation_values = pair
    configuration.apply_configuration_values("config/inference.toml", inference_values)
    configuration.apply_configuration_values("config/translation.toml", translation_values)


def _host_facts(root: Path, runner: mechanics.ProcessRunner) -> _HostFacts:
    """Check the checkout, the distribution and the host kind before any write."""
    top = runner.run(("git", "rev-parse", "--show-toplevel"), cwd=root).stdout.decode().strip()
    if not top or Path(top).resolve() != root.resolve():
        raise ValueError("the install must run from the top of its own Git checkout")
    branch = runner.run(("git", "branch", "--show-current"), cwd=root).stdout.decode().strip()
    if not branch:
        raise ValueError("the worker checkout must be on a Git branch")
    origin = runner.run(("git", "remote", "get-url", "origin"), cwd=root).stdout.decode().strip()
    if not origin:
        raise ValueError("the worker checkout must name its Git origin remote")

    release = platform.freedesktop_os_release()
    identifier = release.get("ID", "")
    # Docker publishes its Ubuntu packages per Ubuntu release. A derivative names the Ubuntu
    # codename in UBUNTU_CODENAME, and Ubuntu reports it in UBUNTU_CODENAME or VERSION_CODENAME.
    codename = release.get("UBUNTU_CODENAME", "")
    if not codename and identifier == "ubuntu":
        codename = release.get("VERSION_CODENAME", "")
    ubuntu_based = identifier == "ubuntu" or "ubuntu" in release.get("ID_LIKE", "").split()
    if not ubuntu_based or not codename:
        raise ValueError(f"the worker install supports {_SUPPORTED_DISTRIBUTIONS}")

    distribution = os.environ.get("WSL_DISTRO_NAME")
    if distribution:
        return _HostFacts(
            host_kind="wsl",
            wsl_distribution=distribution,
            docker_codename=codename,
            origin=origin,
        )
    if "microsoft" in platform.release().lower():
        raise ValueError("run this command from a new WSL login shell")
    return _HostFacts(
        host_kind="linux", wsl_distribution=None, docker_codename=codename, origin=origin
    )


def _install_packages(
    root: Path,
    runner: mechanics.ProcessRunner,
    write: Callable[[str], None],
    *,
    facts: _HostFacts,
    gpu_backend: str,
) -> None:
    """Install Docker from its official source when absent, then the configured package list.

    These apt steps run before the checkout holds its working files, so they carry the operator's
    own terminal and no configured bound.
    """
    runner.run_interactive(("sudo", "apt-get", "update"), cwd=root)
    if provisioning_worker.missing_packages(runner, root, provisioning_worker.DOCKER_PACKAGES):
        write("write Docker's official apt source")
        _write_docker_source(root, runner, codename=facts.docker_codename)
        runner.run_interactive(("sudo", "apt-get", "update"), cwd=root)
    packages = provisioning_worker.system_packages(
        host_kind=facts.host_kind, gpu_backend=gpu_backend
    )
    try:
        runner.run_interactive(
            ("sudo", "apt-get", "install", "-y", "--no-install-recommends", *packages),
            cwd=root,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"the worker package installation for the {gpu_backend} backend failed with exit "
            f"status {exc.returncode}; check the host's apt sources, the backend's package "
            "source, free disk space and a conflicting Docker package, then run the install again"
        ) from exc


def _write_docker_source(root: Path, runner: mechanics.ProcessRunner, *, codename: str) -> None:
    """Install Docker's apt key and repository line from its own publication."""
    architecture = runner.run(("dpkg", "--print-architecture"), cwd=root).stdout.decode().strip()
    if not architecture:
        raise ValueError("the host reports no dpkg architecture")
    source = (
        f"deb [arch={architecture} signed-by={_DOCKER_KEYRING}] "
        f"https://download.docker.com/linux/ubuntu {codename} stable\n"
    )
    with tempfile.TemporaryDirectory(prefix="course-explorer-worker-install-") as directory:
        scratch = Path(directory)
        key = scratch / "docker.asc"
        listing = scratch / "docker.list"
        listing.write_text(source, encoding="utf-8")
        runner.run_interactive(
            (
                "curl",
                "--disable",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--output",
                str(key),
                _DOCKER_KEY_URL,
            ),
            cwd=root,
        )
        runner.run_interactive(
            ("sudo", "install", "-m", "0755", "-d", "/etc/apt/keyrings"), cwd=root
        )
        runner.run_interactive(
            ("sudo", "install", "-m", "0644", str(key), _DOCKER_KEYRING),
            cwd=root,
        )
        runner.run_interactive(
            ("sudo", "install", "-m", "0644", str(listing), _DOCKER_SOURCE_PATH),
            cwd=root,
        )


def _required_groups(gpu_backend: str, *, same_machine: bool = False) -> tuple[str, ...]:
    """Return the socket group and, on a host-side ROCm worker, the two device groups.

    A same-machine host runs no model server itself, so its account joins the Docker socket group
    alone and the container receives the device group ids in the generated override.
    """
    if same_machine:
        return ("docker",)
    return ("docker", *(_ROCM_DEVICE_GROUPS if gpu_backend == "rocm" else ()))


def _require_systemd(root: Path, runner: mechanics.ProcessRunner) -> None:
    """Refuse a WSL distribution whose first process is not systemd.

    The Docker service is enabled through ``systemctl``, which exists only in a distribution that
    runs systemd as its init process.
    """
    command = runner.run(("ps", "-p", "1", "-o", "comm="), cwd=root).stdout.decode().strip()
    if command != "systemd":
        raise ValueError(
            "this WSL distribution does not run systemd; set systemd=true in /etc/wsl.conf and "
            "restart the distribution"
        )


def _join_groups(
    account: str,
    *,
    chosen_backend: str,
    same_machine: bool = False,
    runner: mechanics.ProcessRunner,
    root: Path,
) -> None:
    """Start the Docker service and join the account to its socket and device groups."""
    runner.run_interactive(("sudo", "systemctl", "enable", "--now", "docker"), cwd=root)
    runner.run_interactive(
        (
            "sudo",
            "usermod",
            "-aG",
            ",".join(_required_groups(chosen_backend, same_machine=same_machine)),
            account,
        ),
        cwd=root,
    )


def _groups_effective(gpu_backend: str, *, same_machine: bool = False) -> bool:
    """Report whether this process already holds the groups the install joined.

    A supplementary group reaches a running process only at login, so a fresh account's first
    install run stops after the group step and the second run prepares the stack.
    """
    required = {
        grp.getgrnam(name).gr_gid
        for name in _required_groups(gpu_backend, same_machine=same_machine)
    }
    return required <= set(os.getgroups())


def _write_worker_compose_override(
    root: Path,
    *,
    gpu_backend: str,
    gpu_target: str,
    gpu_device_index: int,
    runner: mechanics.ProcessRunner,
) -> None:
    """Write the generated worker GPU override from the detected host facts.

    The file adds what only the host knows to the profile-gated worker service: the device nodes and
    their numeric group ids on a ROCm host, or the NVIDIA runtime and the three CUDA toolkit
    sonames on a CUDA host. It carries paths, group ids and the device index alone, and it is
    rewritten from the detected facts on a re-run. A missing device group, runtime or library fails
    loudly before the file is written.
    """
    lines = [
        f"# Generated worker GPU override for the {gpu_backend} build target {gpu_target} "
        f"on device {gpu_device_index}.",
        "services:",
        "  worker:",
    ]
    if gpu_backend == "rocm":
        rocm_root = (
            runner.run(("hipconfig", "-R"), cwd=root)
            .stdout.decode("utf-8", errors="strict")
            .strip()
        )
        if not rocm_root:
            raise ValueError("the ROCm install reports no root path through hipconfig")
        gids = [str(grp.getgrnam(name).gr_gid) for name in _ROCM_DEVICE_GROUPS]
        lines.extend(
            [
                "    devices:",
                "      - /dev/kfd",
                "      - /dev/dri",
                "    group_add:",
                *(f"      - {json.dumps(gid)}" for gid in gids),
                "    volumes:",
                "      - type: bind",
                f"        source: {json.dumps(rocm_root)}",
                f"        target: {json.dumps(rocm_root)}",
                "        read_only: true",
                "        bind:",
                "          create_host_path: false",
            ]
        )
    else:
        _require_nvidia_runtime(root, runner)
        lines.extend(
            [
                "    runtime: nvidia",
                "    environment:",
                f"      NVIDIA_VISIBLE_DEVICES: {json.dumps(str(gpu_device_index))}",
                "    volumes:",
            ]
        )
        for soname in _CUDA_LIBRARY_SONAMES:
            resolved, entry = _resolve_shared_library(root, runner, soname)
            lines.extend(
                [
                    "      - type: bind",
                    f"        source: {json.dumps(resolved)}",
                    f"        target: {json.dumps(entry)}",
                    "        read_only: true",
                    "        bind:",
                    "          create_host_path: false",
                ]
            )
    administration.atomic_write_private_text(
        root / compose.WORKER_OVERRIDE_FILENAME, "\n".join(lines) + "\n", mode=0o644
    )


def _recreate_running_worker(
    root: Path, runner: mechanics.ProcessRunner, *, timeout: float
) -> None:
    """Recreate an already-running worker service with the generated GPU override.

    A running service keeps the container it was created with, so a backend change does not reach it
    through the stage reservation. The install recreates only a running service, without a build and
    without a pull, because the worker image is already prepared. An absent or stopped service is
    left to the first stage reservation, which starts it with the generated override.
    """
    running = mechanics.running_compose_services(
        lambda argv: runner.run(argv, cwd=root, timeout=timeout).stdout,
        interpreter=True,
    )
    if "worker" not in running:
        return
    runner.run(
        (
            *mechanics.compose_prefix(interpreter=True),
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--no-build",
            "--pull",
            "never",
            "worker",
        ),
        cwd=root,
        timeout=timeout,
    )


def _require_nvidia_runtime(root: Path, runner: mechanics.ProcessRunner) -> None:
    """Require the registered Docker NVIDIA runtime a CUDA worker container boots with."""
    try:
        output = runner.run(
            (
                *compose.docker_prefix(non_interactive=False),
                "info",
                "--format",
                "{{json .Runtimes}}",
            ),
            cwd=root,
        ).stdout
        runtimes = json.loads(output.decode("utf-8", errors="strict"))
    except (subprocess.CalledProcessError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(_NVIDIA_RUNTIME_GUIDANCE) from exc
    if not isinstance(runtimes, dict) or "nvidia" not in runtimes:
        raise ValueError(_NVIDIA_RUNTIME_GUIDANCE)


def _resolve_shared_library(
    root: Path, runner: mechanics.ProcessRunner, soname: str
) -> tuple[str, str]:
    """Return one CUDA soname's resolved regular file and its loader-cache entry path."""
    listing = runner.run(("ldconfig", "-p"), cwd=root).stdout.decode("utf-8", errors="strict")
    for line in listing.splitlines():
        _description, separator, path = line.partition("=>")
        if not separator:
            continue
        entry = Path(path.strip())
        if entry.name != soname:
            continue
        resolved = entry.resolve()
        if not resolved.is_file():
            raise ValueError(f"the CUDA toolkit library {soname} resolves to no regular file")
        return str(resolved), str(entry)
    raise ValueError(f"the CUDA toolkit library {soname} is not in the loader cache")


def _install_reboot_rule(
    root: Path,
    account: str,
    *,
    runner: mechanics.ProcessRunner,
    confirm: Callable[[str], bool],
    write: Callable[[str], None],
    timeout: float,
) -> None:
    """Offer the one reboot-only sudoers rule and install it on a yes."""
    if not confirm("Install the reboot rule that allows /usr/bin/systemctl reboot?"):
        write("The reboot rule is not installed. Run the install again to be offered the rule.")
        return
    template = (root / _REBOOT_TEMPLATE).read_text(encoding="utf-8")
    rendered = "".join(
        f"{line.replace('WORKER_ACCOUNT', account, 1)}\n"
        if line.startswith("WORKER_ACCOUNT ")
        else f"{line}\n"
        for line in template.splitlines()
    )
    with tempfile.TemporaryDirectory(prefix="course-explorer-worker-install-") as directory:
        rule = Path(directory) / "course-explorer-worker-reboot"
        rule.write_text(rendered, encoding="utf-8")
        runner.run_interactive(
            (
                "sudo",
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0440",
                str(rule),
                _REBOOT_RULE_PATH,
            ),
            cwd=root,
            timeout=timeout,
        )
    # The check reads the installed rule alone, so an unrelated sudoers file on the host cannot
    # fail this install's rule step.
    runner.run_interactive(
        ("sudo", "visudo", "-c", "-f", _REBOOT_RULE_PATH), cwd=root, timeout=timeout
    )


def _show_wsl_memory_block(*, read: Callable[[str], str], write: Callable[[str], None]) -> None:
    """Print the Windows-side memory block and wait for the operator to set it."""
    write(
        "The published configuration assumes 16 GB of system RAM, and this block leaves the rest "
        "to Windows. Raise memory on a machine with more RAM, then set the block in the Windows "
        "user's .wslconfig."
    )
    for line in _WSL_MEMORY_BLOCK:
        write(line)
    read("Press Enter when the block is set in the Windows user's .wslconfig. ")


def _ask(read: Callable[[str], str], label: str, *, default: str | None = None) -> str:
    """Read one nonblank answer, falling back to the offered default."""
    suffix = f" [{default}]" if default is not None else ""
    answer = read(f"{label}{suffix}: ").strip()
    if answer:
        return answer
    if default is not None:
        return default
    raise ValueError(f"{label} is required")


def _ask_confirm(read: Callable[[str], str], question: str) -> bool:
    """Read one yes or no confirmation, with no as the default answer."""
    return read(f"{question} [y/N]: ").strip().lower() in {"y", "yes"}


def _ask_int(read: Callable[[str], str], label: str, *, default: int) -> int:
    """Read one whole-number answer, falling back to the offered default."""
    answer = _ask(read, label, default=str(default))
    try:
        return int(answer, 10)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number") from exc


def _ask_choice(read: Callable[[str], str], label: str, choices: tuple[str, ...]) -> str:
    """Read one answer that must be one of the listed choices."""
    answer = _ask(read, label)
    if answer not in choices:
        raise ValueError(f"{label} must be one of: {', '.join(choices)}")
    return answer


def main(argv: list[str] | None = None) -> int:
    """Prompt for the worker answers, run the install and report the prepared resources."""
    del argv
    presets = _available_presets(administration.repository_root())
    preset_options = ", ".join(presets)
    preset_label = f"Model preset ({preset_options} or keep)" if presets else "Model preset (keep)"
    try:
        same_machine = _ask_confirm(input, "This machine also runs the server stack?")
        ssh_target = None if same_machine else _ask(input, "Worker SSH target")
        gpu_backend = _ask_choice(input, "GPU backend (rocm or cuda)", ("rocm", "cuda"))
        gpu_target = _ask(input, "GPU target")
        gpu_device_index = _ask_int(input, "GPU device index", default=0)
        preset_answer = _ask(input, preset_label, default="keep")
        try:
            choices = WorkerInstallChoices(
                ssh_target=ssh_target,
                same_machine=same_machine,
                gpu_backend=gpu_backend,
                gpu_target=gpu_target,
                gpu_device_index=gpu_device_index,
                preset=None if preset_answer == "keep" else preset_answer,
            )
        except ValidationError as exc:
            # The answers reach a closed settings model, whose dump is longer than one line of
            # guidance.
            raise ValueError(_INVALID_ANSWERS) from exc
        report = install_worker(
            choices,
            runner=mechanics.SubprocessRunner(),
            read=input,
            confirm=lambda question: _ask_confirm(input, question),
            write=print,
        )
    except EOFError:
        # End of input is an ordinary way for a piped prompt sequence to end, so it is not a
        # failure.
        print(
            "install-worker: input ended before the install answers were complete", file=sys.stderr
        )
        return 0
    except KeyboardInterrupt:
        print("install-worker: the install was interrupted", file=sys.stderr)
        return 130
    except ValueError as exc:
        # The refusals this command raises are one sentence of guidance, so the operator reads that
        # sentence instead of a traceback.
        print(f"install-worker: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        # An external step that exits nonzero is the operator's next action, so the command names
        # the program and its status and leaves the rerun to the operator.
        command = tuple(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else (exc.cmd,)
        # A privileged step runs through ``sudo``, so the program the operator acts on is its child.
        failed = command[1:] if command[:1] == ("sudo",) else command
        program = failed[0] if failed else str(exc.cmd)
        print(
            f"install-worker: {program} failed with exit status {exc.returncode}. "
            "Resolve the failure and run the install again.",
            file=sys.stderr,
        )
        return 1
    if choices.same_machine:
        print(
            f"worker install wrote the same-machine worker container for the "
            f"{choices.gpu_backend} backend targeting {choices.gpu_target} on device "
            f"{choices.gpu_device_index}"
        )
    else:
        print(
            f"worker install wrote {choices.ssh_target} for the {choices.gpu_backend} backend "
            f"targeting {choices.gpu_target} on device {choices.gpu_device_index}"
        )
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
