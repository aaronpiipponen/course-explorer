"""One generated Compose environment bridge for the Docker Compose stack.

The bridge projects the validated raw database, embedding runtime, instance, shared-storage and
feedback settings onto the Compose interpolation aliases, writes them into an invocation-owned
mode-0700 temporary directory as a mode-0600 file, runs ``docker compose`` with that file, and
removes the directory when the subprocess completes. It adds the three account identity aliases and
the four host-path aliases, whose values are the Docker host's absolute paths, so a command run
inside the administration container names the same containers, volumes and build contexts as the
same command run on the host. It also projects the repository-relative ``ADMIN_STATE_ROOT`` and the
feedback application store's password for the site service. A caller-supplied Compose project name
is forwarded as an explicit ``--project-name``. The account's Docker socket access decides whether
the command needs a ``sudo`` prefix, and a ``same-machine`` worker configuration adds the ``worker``
profile and the generated ``docker-compose.worker.yml`` override to every command on that host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from course_explorer import administration
from course_explorer.config import (
    HOST_HOME_ENVIRONMENT_ALIAS,
    load_embedding_runtime_settings,
    load_feedback_settings,
    load_instance_settings,
    load_raw_database_settings,
    load_shared_storage_settings,
    load_worker_connection_settings,
)

# Docker Compose's own process-level project selector. When the caller supplies it, the bridge
# forwards it as the explicit ``--project-name`` so one ephemeral namespace reaches initial setup,
# detached generation and cleanup without relying on ``sudo`` preserving the variable.
COMPOSE_PROJECT_NAME_ALIAS = "COMPOSE_PROJECT_NAME"

# The Docker daemon socket whose ownership decides whether this account needs ``sudo``.
DOCKER_SOCKET_PATH: Path = Path("/var/run/docker.sock")
# The service-environment alias that carries the Docker host's absolute checkout path.
HOST_ROOT_ALIAS = "COURSE_EXPLORER_HOST_ROOT"
# The service-environment alias that carries the Docker host's absolute SSH directory.
SSH_DIRECTORY_ALIAS = "COURSE_EXPLORER_SSH_DIR"
# The Docker host's home directory, which holds the host-built runtime tree the worker container
# mounts and which a home-prefixed shared root expands against.
HOST_HOME_ALIAS = HOST_HOME_ENVIRONMENT_ALIAS
# The profile and the generated override that add the same-machine worker service to the stack.
WORKER_PROFILE = "worker"
WORKER_OVERRIDE_FILENAME = "docker-compose.worker.yml"


def docker_needs_sudo() -> bool:
    """Report whether this account cannot write the Docker socket itself."""
    return not os.access(DOCKER_SOCKET_PATH, os.W_OK)


def docker_prefix(*, non_interactive: bool) -> tuple[str, ...]:
    """Return the Docker command prefix this account's socket access requires.

    A socket the account can write needs no prefix. Otherwise the command runs through ``sudo``,
    and ``non_interactive`` selects ``sudo -n``, which never prompts and fails when the account
    holds no passwordless grant.
    """
    if not docker_needs_sudo():
        return ("docker",)
    return ("sudo", "-n", "docker") if non_interactive else ("sudo", "docker")


def host_checkout_root() -> Path:
    """Return the Docker host's absolute checkout path.

    Inside a container the value comes from the service environment, and a host process derives it
    from its own checkout.
    """
    value = os.environ.get(HOST_ROOT_ALIAS)
    return Path(value) if value else administration.repository_root()


def host_ssh_directory() -> Path:
    """Return the Docker host's absolute SSH directory.

    Inside a container the value comes from the service environment, and a host process derives it
    from the account's own home directory.
    """
    value = os.environ.get(SSH_DIRECTORY_ALIAS)
    return Path(value) if value else Path.home() / ".ssh"


def host_home() -> Path:
    """Return the Docker host's home directory.

    Inside a container the value comes from the service environment, and a host process derives it
    from the account's own home directory.
    """
    value = os.environ.get(HOST_HOME_ALIAS)
    return Path(value) if value else Path.home()


def compose_environment() -> dict[str, str]:
    """Project the raw database, embedding, instance, shared-storage and feedback settings onto
    Compose interpolation aliases, together with the account identities and the Docker host's paths.
    """
    database = load_raw_database_settings()
    embedding = load_embedding_runtime_settings()
    instance = load_instance_settings()
    shared = load_shared_storage_settings()
    feedback = load_feedback_settings()
    artifact = shared.resolve_embedding_artifact(embedding.embedding_artifact_path)
    return {
        "COURSE_EXPLORER_PROJECT_NAME": f"course-explorer-{instance.instance_key}",
        "POSTGRES_DB": database.postgres_db,
        "POSTGRES_USER": database.postgres_user,
        "POSTGRES_PASSWORD": database.postgres_password.get_secret_value(),
        "POSTGRES_PORT": str(database.postgres_port),
        "EMBEDDING_SERVER_IMAGE": embedding.embedding_server_image,
        "EMBEDDING_RUNTIME_REVISION": embedding.embedding_runtime_revision,
        "EMBEDDING_ARTIFACT_SHA256": embedding.embedding_artifact_sha256,
        "EMBEDDING_MODEL": embedding.embedding_model,
        "EMBEDDING_HOST": embedding.embedding_host,
        "EMBEDDING_PORT": str(embedding.embedding_port),
        "EMBEDDING_THREADS": str(embedding.embedding_threads),
        "EMBEDDING_CONTEXT_SIZE": str(embedding.embedding_context_size),
        "EMBEDDING_POOLING": embedding.embedding_pooling,
        "EMBEDDING_NORMALIZATION": str(embedding.embedding_normalization),
        "EMBEDDING_ARTIFACT_PATH": str(artifact),
        "COURSE_EXPLORER_UID": str(os.getuid()),
        "COURSE_EXPLORER_GID": str(os.getgid()),
        "COURSE_EXPLORER_DOCKER_GID": str(DOCKER_SOCKET_PATH.stat().st_gid),
        HOST_ROOT_ALIAS: str(host_checkout_root()),
        HOST_HOME_ALIAS: str(host_home()),
        SSH_DIRECTORY_ALIAS: str(host_ssh_directory()),
        "ADMIN_SHARED_ROOT": str(shared.shared_root),
        "ADMIN_STATE_ROOT": str(database.admin_state_root),
        "FEEDBACK_POSTGRES_PASSWORD": feedback.feedback_postgres_password.get_secret_value(),
    }


def _quote_bridge_value(key: str, value: str) -> str:
    """Render one value in Compose dotenv literal single-quote syntax.

    A value carrying a line break cannot be represented in the file syntax, so it fails with a
    key-only error that renders no value text.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} must not contain a line break")
    return "'" + value.replace("'", "\\'") + "'"


def _render_bridge(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={_quote_bridge_value(key, value)}\n" for key, value in values.items())


def _write_bridge(directory: Path, values: Mapping[str, str]) -> Path:
    bridge = directory / "compose.env"
    descriptor = os.open(bridge, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(_render_bridge(values))
    return bridge


def _split_arguments(argv: Sequence[str]) -> tuple[bool, list[str]]:
    """Split the optional non-interactive flag from the forwarded Compose arguments after ``--``."""
    arguments = list(argv)
    non_interactive = False
    if arguments and arguments[0] == "--non-interactive":
        non_interactive = True
        arguments.pop(0)
    if not arguments or arguments[0] != "--":
        raise ValueError("compose bridge requires -- before the forwarded Compose arguments")
    return non_interactive, arguments[1:]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one Compose command with a locally generated temporary environment-file bridge.

    Docker Compose's process-level ``COMPOSE_PROJECT_NAME`` override, when the caller supplies one,
    is forwarded as an explicit ``--project-name`` before the caller's own arguments. The project
    directory and the Compose file come from the Docker host's checkout path, so the command reaches
    the same project from a host process and from inside a container. A ``same-machine`` worker
    configuration adds the ``worker`` profile and the generated worker override, so every command on
    that host sees the worker service.
    """
    arguments = sys.argv[1:] if argv is None else argv
    non_interactive, compose_arguments = _split_arguments(arguments)
    root = host_checkout_root()
    same_machine = load_worker_connection_settings().host_kind == "same-machine"
    project_name = os.environ.get(COMPOSE_PROJECT_NAME_ALIAS)
    directory = Path(tempfile.mkdtemp(prefix="course-explorer-compose-"))
    os.chmod(directory, 0o700)
    try:
        bridge = _write_bridge(directory, compose_environment())
        command = (
            *docker_prefix(non_interactive=non_interactive),
            "compose",
            "--project-directory",
            str(root),
            "--file",
            str(root / "docker-compose.yml"),
            *(("--file", str(root / WORKER_OVERRIDE_FILENAME)) if same_machine else ()),
            *(() if not project_name else ("--project-name", project_name)),
            "--env-file",
            str(bridge),
            *(("--profile", WORKER_PROFILE) if same_machine else ()),
            *compose_arguments,
        )
        completed = subprocess.run(command, check=False)
        return completed.returncode
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
