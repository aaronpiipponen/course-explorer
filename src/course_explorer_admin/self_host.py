"""The two self-host setup workflows and the configured publication path.

``setup_example`` takes a fresh checkout from absent working files to a site that serves the bundled
example data without an enrolled dataset. ``setup_real_database`` then creates one named empty
database, enrolls it as Selected and serves it. ``publish_configured_site`` and
``unpublish_configured_site`` write or remove the configured instance's site file of the
machine-wide proxy. Every workflow accepts its choices in memory and emits fixed coarse step names
through the supplied writer. One workflow copy owns absent template materialization, generated
credentials, the pinned embedding artifact, the container-host preflight, the Compose image builds
and the Compose starts. The services run under the Compose restart policy, so the workflow starts
them and returns. A domain-mode setup or the publish command writes the site file and starts
or reloads the proxy project, and a local-mode setup starts no proxy.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import inspect

from course_explorer import administration
from course_explorer import configuration as file_configuration
from course_explorer.config import (
    AdminWebSettings,
    DatabaseSettings,
    EmbeddingRuntimeSettings,
    InstanceSettings,
    WebAppSettings,
    WebSettings,
    embedding_configuration_for_site,
    load_admin_web_settings,
    load_administrative_dataset_settings,
    load_embedding_runtime_settings,
    load_feedback_settings,
    load_instance_settings,
    load_local_lifecycle_settings,
    load_raw_database_settings,
    load_shared_storage_settings,
    load_web_settings,
    load_worker_connection_settings,
)
from course_explorer.datasets import (
    DatasetDatabaseName,
    DatasetId,
    DatasetReceipt,
    dataset_pointer_path,
    has_dataset_enrollment_records,
)
from course_explorer.db.session import create_database_engine
from course_explorer.instance import UNIVERSITIES
from course_explorer.serving_build import build_serving_artifacts, dataset_settings
from course_explorer.usage import usage_root
from course_explorer_admin import (
    artifacts,
    compose,
    configuration,
    credentials,
    mechanics,
    proxy,
    rebuild,
)
from course_explorer_admin import datasets as dataset_owner
from course_explorer_admin.working_files import copy_example_files, template_pairs

# The three loopback hosts a loopback URL may name.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_public_site_address(app_url: str | None) -> None:
    """Require the site's HTTPS address outside loopback without an explicit port.

    A domain-mode setup and the publish command both call it, so neither publishes an address the
    machine-wide proxy cannot serve. The proxy supplies the HTTPS port itself.
    """
    if app_url is None:
        raise ValueError("domain mode needs the site's HTTPS address")
    parts = urlsplit(app_url)
    if parts.hostname in _LOOPBACK_HOSTS:
        raise ValueError("domain mode needs a site address outside loopback")
    if parts.port is not None:
        raise ValueError("domain mode needs the site's HTTPS address without a port")


class _SelfHostModel(BaseModel):
    """Frozen, closed DTO base for the self-host setup contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExampleSetupInput(_SelfHostModel):
    """Everything one example workflow needs, supplied in memory without a prompt.

    ``serve_mode`` decides whether the workflow publishes the site through the machine-wide proxy
    project or starts no proxy at all. ``instance_key`` selects one entry of the finite university
    map. The four ports are independent listeners, so the workflow requires them to differ.
    ``app_url`` is the feature site's external root address, and ``admin_origin`` is the
    administration backend's exact external HTTPS origin. Either null value derives its default
    from the matching port. In domain mode ``app_url`` is required, and it is an HTTPS address
    outside loopback without an explicit port. The shared directory is the configured
    ``ADMIN_SHARED_ROOT``.
    Both credentials are hidden from the model's representation and its serialized form, and
    validation errors never echo their inputs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    serve_mode: Literal["local", "domain"]
    app_url: str | None = None
    admin_origin: str | None = None
    instance_key: str
    web_port: int
    admin_web_port: int
    postgres_port: int
    embedding_port: int
    username: str
    password: SecretStr = Field(exclude=True, repr=False)
    confirm_download: bool = False
    publication_token: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_choices(self) -> ExampleSetupInput:
        """Validate every supplied choice through its owning settings family."""
        InstanceSettings.validate_instance_key(self.instance_key)
        WebSettings.validate_web_port(self.web_port)
        AdminWebSettings.validate_admin_web_port(self.admin_web_port)
        DatabaseSettings.validate_port(self.postgres_port)
        EmbeddingRuntimeSettings.validate_embedding_port(self.embedding_port)
        if len({self.web_port, self.admin_web_port, self.postgres_port, self.embedding_port}) != 4:
            raise ValueError("the four setup ports must be distinct")
        if self.app_url is not None:
            WebAppSettings.validate_web_app_url(self.app_url)
        if self.serve_mode == "domain":
            _require_public_site_address(self.app_url)
        if self.admin_origin is not None:
            AdminWebSettings.validate_admin_web_origin(self.admin_origin)
        return self


class RealDatabaseSetupInput(_SelfHostModel):
    """Everything one real-database workflow needs: the name of the new database."""

    database_name: DatasetDatabaseName


class SelfHostReport(_SelfHostModel):
    """One transient workflow result: the address to open and the Selected dataset it enrolled."""

    app_url: str
    dataset_id: DatasetId | None


class PublicSiteReport(_SelfHostModel):
    """One transient publish or unpublish result: the configured site address and its state."""

    site_address: str
    published: bool


def _require_working_files(root: Path) -> None:
    """Require every prepared working file a deployment keeps beside its tracked examples."""
    for _, destination_relative in template_pairs(root):
        destination = root / destination_relative
        if destination.is_symlink() or not destination.is_file():
            raise ValueError(
                f"self-host setup needs the prepared working file {destination_relative}; "
                "run the example workflow first"
            )


def _port_is_free(host: str, port: int) -> bool:
    """Report whether one configured loopback port can be bound by this process."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((host, port))
        except OSError:
            return False
    return True


def _running_services(runner: mechanics.ProcessRunner, root: Path) -> set[str]:
    """Return the Compose services this project reports running."""
    return mechanics.running_compose_services(
        lambda argv: runner.run(tuple(argv), cwd=root).stdout, interpreter=True
    )


def _require_host_prerequisites(
    root: Path,
    runner: mechanics.ProcessRunner,
    write: Callable[[str], None],
    *,
    serve_mode: Literal["local", "domain"],
) -> None:
    """Preflight the container host, the free ports and the two working roots.

    The contract is the container host: Docker with Docker Compose, a daemon that answers, and an
    account that writes the Docker socket. The two images supply the interpreter, the dependencies,
    the browser and the native libraries. This check refuses a missing command, a socket the
    account cannot write, a loopback port held by another process, and a shared directory that is
    absent or is not writable. A database or embedding port already served by this project's own
    Compose service is allowed. Domain mode also checks the two public ports, and the machine-wide
    proxy is their one allowed holder. A public port counts as held when a listener is on the
    configured bind address, on the wildcard address, or on any address when none is configured,
    because a wildcard listener blocks a specific bind. That read is the kernel's listen table
    rather than a bind probe, because this account holds no capability that binds a port below
    1024.
    """
    for argv, label in (
        (("docker", "--version"), "Docker"),
        (("docker", "compose", "version"), "Docker Compose"),
        (("docker", "info"), "a running Docker daemon"),
    ):
        try:
            runner.run(argv, cwd=root)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise ValueError(f"{label} is not available; install it and retry") from exc
    if compose.docker_needs_sudo():
        raise ValueError(
            f"this account cannot write {compose.DOCKER_SOCKET_PATH}; add it to the "
            "docker group and start a new login session"
        )

    database = load_raw_database_settings()
    embedding = load_embedding_runtime_settings()
    web = load_web_settings()
    admin = load_admin_web_settings()
    shared = load_shared_storage_settings()
    shared_root = shared.shared_root.resolve()
    services = _running_services(runner, root)
    for label, host, port, service in (
        ("feature web", web.web_host, web.web_port, None),
        ("administration", admin.admin_web_host, admin.admin_web_port, None),
        ("database", database.postgres_host, database.postgres_port, "db"),
        ("embedding", embedding.embedding_host, embedding.embedding_port, "embedding"),
    ):
        if not _port_is_free(host, port) and service not in services:
            raise ValueError(
                f"the configured {label} port {port} is used by another process; "
                "stop that listener and retry"
            )
    if serve_mode == "domain":
        listening = proxy.listening_sockets()
        for label, port in (("feature HTTP", 80), ("feature HTTPS", 443)):
            held = any(
                bound_port == port
                and (
                    shared.proxy_bind_address is None
                    or address in {shared.proxy_bind_address, "0.0.0.0", "::"}
                )
                for address, bound_port in listening
            )
            if held and not proxy.running(runner, shared_root=shared_root):
                raise ValueError(
                    f"the configured {label} port {port} is used by another process; "
                    "stop that listener and retry"
                )
    if not shared_root.is_dir() or not os.access(shared_root, os.W_OK):
        raise ValueError(f"the shared directory {shared_root} must be a writable directory")
    write(
        f"ports: web {web.web_host}:{web.web_port}, "
        f"administration {admin.admin_web_host}:{admin.admin_web_port}, "
        f"database {database.postgres_host}:{database.postgres_port}, "
        f"embedding {embedding.embedding_host}:{embedding.embedding_port}"
    )
    write(f"disk: {shutil.disk_usage(root).free} bytes free at {root}")
    write(f"disk: {shutil.disk_usage(shared_root).free} bytes free at {shared_root}")


def _require_download_space(path: Path, url: str, write: Callable[[str], None]) -> None:
    """Display and check the reported download length against the target filesystem's free space."""
    timeout = load_local_lifecycle_settings().stop_timeout_seconds
    response = httpx.head(url, follow_redirects=True, timeout=timeout)
    response.raise_for_status()
    if response.url.scheme != "https":
        raise ValueError("the embedding artifact download must end at an HTTPS address")
    length = response.headers.get("content-length")
    if length is None:
        raise ValueError(
            "the embedding artifact download does not report its length; an unbounded download "
            "is unsupported"
        )
    required = int(length)
    directory = path.parent
    while not directory.exists():
        directory = directory.parent
    available = shutil.disk_usage(directory).free
    write(f"embedding download: {required} bytes required, {available} bytes available")
    if required > available:
        raise ValueError(
            f"the embedding download needs {required} bytes and {available} bytes are free"
        )


def _prepare_embedding_artifact(
    root: Path,
    runner: mechanics.ProcessRunner,
    *,
    confirm_download: bool,
    write: Callable[[str], None],
) -> None:
    """Check once for the pinned artifact, then download it only after an explicit confirmation."""
    runtime = load_embedding_runtime_settings()
    path = load_shared_storage_settings().resolve_embedding_artifact(
        runtime.embedding_artifact_path
    )
    if artifacts.artifact_present(path, runtime.embedding_artifact_sha256):
        return
    if not confirm_download:
        raise ValueError(
            "the pinned embedding model is absent; confirm the download before running the workflow"
        )
    _require_download_space(path, str(runtime.embedding_artifact_url), write)
    write(f"download the pinned embedding model to {path}")
    artifacts.download_artifact(
        runner,
        root,
        path,
        runtime.embedding_artifact_sha256,
        str(runtime.embedding_artifact_url),
        None,
    )


# The migration owner's own bookkeeping table, which is not a user table.
_VERSION_TABLE = "alembic_version"


def _existing_table_names(settings: DatabaseSettings) -> tuple[str, ...]:
    """Return the user tables of one short-lived database read."""
    engine = create_database_engine(settings)
    try:
        return tuple(name for name in inspect(engine).get_table_names() if name != _VERSION_TABLE)
    finally:
        engine.dispose()


def _conninfo(
    *, host: str, port: int, database: str, user: str, password: str, timeout: float
) -> str:
    """Build one psycopg connection string in memory from validated components."""
    return psycopg.conninfo.make_conninfo(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=max(1, int(timeout)),
    )


def _prepare_site_usage_directory() -> None:
    """Create the site's usage directory before the site process starts writing records."""
    usage_root(administration.repository_root()).mkdir(mode=0o700, parents=True, exist_ok=True)


def _prepare_feedback_store(*, runner: mechanics.ProcessRunner) -> None:
    """Create the absent feedback role and database, prove the connection, then migrate the chain.

    Both connections must name the same local cluster as the curriculum settings, and the two
    database names must differ. An existing role or database is never reassigned, dropped or given a
    new password. Identifiers are composed through psycopg's SQL identifier and value quoting, and a
    failed creation reports one fixed error that carries no statement or credential.
    """
    raw = load_raw_database_settings()
    feedback = load_feedback_settings()
    if (feedback.feedback_postgres_host, feedback.feedback_postgres_port) != (
        raw.postgres_host,
        raw.postgres_port,
    ):
        raise ValueError(
            "the feedback store must be on the same local database cluster as the curriculum "
            "database"
        )
    if feedback.feedback_postgres_db == raw.postgres_db:
        raise ValueError("the feedback database name must differ from the curriculum database name")
    timeout = load_administrative_dataset_settings().database_operation_timeout_seconds
    role = feedback.feedback_postgres_user
    password = feedback.feedback_postgres_password.get_secret_value()
    admin_conninfo = _conninfo(
        host=raw.postgres_host,
        port=raw.postgres_port,
        database="postgres",
        user=raw.postgres_user,
        password=raw.postgres_password.get_secret_value(),
        timeout=timeout,
    )
    with psycopg.connect(admin_conninfo, autocommit=True) as connection:
        role_present = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if role_present is None:
            try:
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            except psycopg.Error:
                raise ValueError("the feedback database role could not be created") from None
        database_present = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (feedback.feedback_postgres_db,)
        ).fetchone()
        if database_present is None:
            try:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(feedback.feedback_postgres_db), sql.Identifier(role)
                    )
                )
            except psycopg.Error:
                raise ValueError("the feedback database could not be created") from None

    feedback_conninfo = _conninfo(
        host=feedback.feedback_postgres_host,
        port=feedback.feedback_postgres_port,
        database=feedback.feedback_postgres_db,
        user=role,
        password=password,
        timeout=timeout,
    )
    with psycopg.connect(feedback_conninfo) as connection:
        connection.execute("SELECT 1")

    attachments = feedback.feedback_attachments_root.expanduser()
    if not attachments.is_absolute():
        attachments = administration.repository_root() / attachments
    attachments.mkdir(mode=0o700, parents=True, exist_ok=True)
    runner.run(
        (
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic_feedback.ini",
            "upgrade",
            "head",
        ),
        cwd=administration.repository_root(),
        timeout=timeout,
    )


def _loopback_url(host: str, port: int) -> str:
    """Return one loopback HTTP base URL, bracketing an IPv6 host."""
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _prove_html(host: str, port: int, host_header: str) -> None:
    """Prove one running backend answers its HTML shell at its configured Host.

    One short-lived client retries only a connection that is not ready yet and requires status 200
    with a ``text/html`` content type.
    """
    lifecycle = load_local_lifecycle_settings()

    def read(client: httpx.Client) -> bool | None:
        try:
            response = client.get(
                f"{_loopback_url(host, port)}/",
                headers={"Accept-Encoding": "identity", "Host": host_header},
            )
        except httpx.ConnectError, httpx.ReadTimeout:
            return None
        return response.status_code == 200 and response.headers.get("content-type", "").startswith(
            "text/html"
        )

    with httpx.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=lifecycle.stop_timeout_seconds,
    ) as client:
        mechanics.await_state(
            lambda: read(client),
            lambda accepted: accepted is True,
            attempts=lifecycle.startup_attempts,
            interval=lifecycle.poll_interval_seconds,
            sleep=time.sleep,
        )


def prove_web_shell() -> None:
    """Prove the running site backend answers its HTML shell at its configured Host."""
    web = load_web_settings()
    _prove_html(web.web_host, web.web_port, urlsplit(web.web_app_url).netloc)


def prove_shells() -> None:
    """Prove both running backends answer their HTML shell at their configured Host."""
    admin = load_admin_web_settings()
    prove_web_shell()
    _prove_html(
        admin.admin_web_host,
        admin.admin_web_port,
        urlsplit(admin.admin_web_origin).netloc,
    )


def publish_configured_site(runner: mechanics.ProcessRunner) -> str:
    """Publish the configured instance's site through the machine-wide proxy.

    It reads the instance key, the site address, the loopback site target, the optional bind address
    and the shared directory through their owning loaders, refuses an address the proxy cannot
    serve, writes the site file, starts the project when it is absent and reloads it. It returns the
    configured site address.
    """
    web = load_web_settings()
    shared = load_shared_storage_settings()
    instance = load_instance_settings()
    _require_public_site_address(web.web_app_url)
    proxy.publish(
        runner,
        checkout_root=administration.repository_root(),
        shared_root=shared.shared_root,
        instance_key=instance.instance_key,
        site_address=web.web_app_url,
        host=web.web_host,
        port=web.web_port,
        bind_address=shared.proxy_bind_address,
    )
    return web.web_app_url


def unpublish_configured_site(runner: mechanics.ProcessRunner) -> str:
    """Remove the configured instance's site through the machine-wide proxy.

    It reads the instance key and the shared directory through their owning loaders, removes only
    that instance's site file and reloads a running project. It returns the configured site address.
    """
    web = load_web_settings()
    shared = load_shared_storage_settings()
    instance = load_instance_settings()
    proxy.unpublish(
        runner,
        shared_root=shared.shared_root,
        instance_key=instance.instance_key,
    )
    return web.web_app_url


def _show_origins(write: Callable[[str], None], *, serve_mode: Literal["local", "domain"]) -> None:
    """Show both configured origins and the serve mode's own proxy arrangement."""
    web = load_web_settings()
    admin = load_admin_web_settings()
    write(f"feature origin: {web.web_app_origin}")
    write(f"administration origin: {admin.admin_web_origin}")
    if serve_mode == "local":
        write(
            "local mode: no public proxy is started; the site and the console answer on their "
            "loopback ports and any external address belongs to the operator's own proxy."
        )
    else:
        write(
            f"domain mode: the machine-wide proxy publishes the site at {web.web_app_origin}; "
            "the console stays loopback and is reached through the operator's own private HTTPS "
            "proxy."
        )


def _require_publication_destination(root: Path) -> None:
    """Refuse publication while the feedback destination is still the example placeholder."""
    configured = configuration.read_configuration_file("config/feedback.toml").values[
        "FEEDBACK_GITHUB_REPOSITORY"
    ]
    example = file_configuration.read_public_configuration(
        root / "examples", aliases=("FEEDBACK_GITHUB_REPOSITORY",)
    )["FEEDBACK_GITHUB_REPOSITORY"]
    if configured == example:
        raise ValueError(
            "publication needs a real feedback repository; set it with `python scripts/"
            "admin.py config set config/feedback.toml FEEDBACK_GITHUB_REPOSITORY OWNER/REPOSITORY`"
        )


def _apply_instance_choices(options: ExampleSetupInput, root: Path) -> None:
    """Write the instance key, ports, origins, feature defaults and shared root, then create it.

    Every save validates one candidate through its owning settings family. Embedding and inference
    start off, and the prerequisites switch follows the selected map entry's source family. The
    shared directory is the configured ``ADMIN_SHARED_ROOT``, created or adopted outside the
    checkout with its ``models`` and ``locks`` directories.
    """
    configuration.set_configuration_value(
        "config/web.toml", "INSTANCE_KEY", json.dumps(options.instance_key)
    )
    configuration.set_configuration_value(
        "config/web.toml", "WEB_PORT", json.dumps(options.web_port)
    )
    app_url = options.app_url or f"http://localhost:{options.web_port}/"
    configuration.set_configuration_value("config/web.toml", "WEB_APP_URL", json.dumps(app_url))
    configuration.set_configuration_value(
        "config/admin_web.toml", "ADMIN_WEB_PORT", json.dumps(options.admin_web_port)
    )
    admin_origin = options.admin_origin or f"https://localhost:{options.admin_web_port}"
    configuration.set_configuration_value(
        "config/admin_web.toml", "ADMIN_WEB_ORIGIN", json.dumps(admin_origin)
    )
    configuration.set_configuration_value(
        "config/database.toml", "POSTGRES_PORT", json.dumps(options.postgres_port)
    )
    configuration.set_configuration_value(
        "config/search.toml", "EMBEDDING_PORT", json.dumps(options.embedding_port)
    )
    configuration.set_configuration_value(
        "config/feedback.toml", "FEEDBACK_POSTGRES_PORT", json.dumps(options.postgres_port)
    )
    configuration.set_configuration_value(
        "config/web.toml", "WEB_EMBEDDINGS_ENABLED", json.dumps(False)
    )
    configuration.set_configuration_value(
        "config/web.toml", "WEB_INFERENCE_ENABLED", json.dumps(False)
    )
    configuration.set_configuration_value(
        "config/web.toml",
        "WEB_PREREQUISITES_ENABLED",
        json.dumps(UNIVERSITIES[options.instance_key].source_family == "sisu"),
    )
    shared = load_shared_storage_settings()
    configuration.set_configuration_value(
        "config/database.toml", "ADMIN_SHARED_ROOT", json.dumps(str(shared.shared_root))
    )
    shared_root = shared.shared_root.resolve()
    if shared_root.is_relative_to(root.resolve()):
        raise ValueError("the shared directory must be outside this checkout")
    shared_root.mkdir(parents=True, exist_ok=True)
    shared.models_root.resolve().mkdir(mode=0o700, parents=True, exist_ok=True)
    shared.locks_root.resolve().mkdir(mode=0o700, parents=True, exist_ok=True)


def site_services(*, sender: bool) -> list[str]:
    """Return the site, administration, optional sender and same-machine worker services.

    Every workflow that builds and starts the site group asks this owner, so a same-machine host
    adds its profile-gated worker service once and in the same order.
    """
    services = ["web", "admin"]
    if sender:
        services.append("sender")
    if load_worker_connection_settings().host_kind == "same-machine":
        services.append("worker")
    return services


def setup_example(
    options: ExampleSetupInput,
    *,
    runner: mechanics.ProcessRunner,
    write: Callable[[str], None],
) -> SelfHostReport:
    """Take a fresh checkout to a site that serves the bundled example data.

    Absent templates are materialized first. The enrollment and example preconditions are checked
    before any supplied key edit or fresh credential is written. The instance key, ports, origins,
    feature-switch defaults and shared directory are then applied, and every later step reads the
    edited configuration. The host preflight, the pinned model, both image builds, the Compose
    starts, the example import, the feedback store and the two serving proofs follow in that order.
    Publication adds the feedback sender to the build and the start, and a same-machine worker host
    adds the profile-gated worker. A domain-mode run writes its site file into the shared proxy
    project, starts that project and reloads it, and a local-mode run writes no site file and
    starts no proxy. The workflow runs inside the administration image and starts the services
    under the Compose restart policy, so it returns once the proofs pass. No dataset is enrolled.
    """
    root = administration.repository_root()
    write("copy absent working files")
    created_environment = copy_example_files(root)
    raw = load_raw_database_settings()
    state_root = root / raw.admin_state_root
    if raw.postgres_db != raw.example_postgres_db:
        raise ValueError(
            "the example workflow requires POSTGRES_DB to equal EXAMPLE_POSTGRES_DB in "
            "config/database.toml"
        )
    if (
        has_dataset_enrollment_records(state_root)
        or (dataset_pointer_path(state_root, "selected")).exists()
        or (dataset_pointer_path(state_root, "selected")).is_symlink()
        or (dataset_pointer_path(state_root, "working")).exists()
        or (dataset_pointer_path(state_root, "working")).is_symlink()
    ):
        raise ValueError(
            "the example workflow requires a state root with no dataset pointers or records; "
            "this installation already holds dataset state"
        )

    write("apply device settings")
    _apply_instance_choices(options, root)
    if created_environment:
        for key in ("POSTGRES_PASSWORD", "FEEDBACK_POSTGRES_PASSWORD"):
            configuration.set_configuration_value(
                ".env", key, json.dumps(secrets.token_urlsafe(32))
            )
    publication = options.publication_token is not None
    if publication:
        if not created_environment:
            raise ValueError(
                "an existing environment file keeps its feedback sender credential; set it with "
                "`python scripts/admin.py config set .env FEEDBACK_GITHUB_TOKEN '\"TOKEN\"'`"
            )
        _require_publication_destination(root)
        configuration.set_configuration_value(
            ".env",
            "FEEDBACK_GITHUB_TOKEN",
            json.dumps(options.publication_token.get_secret_value()),
        )

    write("create the administration account")
    credential_path = root / credentials.ADMIN_WEB_STATE_ROOT / credentials.PASSWORD_FILENAME
    if credential_path.is_symlink() or (credential_path.exists() and not credential_path.is_file()):
        raise ValueError("administration credential state must be a regular non-symlink file")
    if not credential_path.exists():
        credentials.set_admin_password(username=options.username, password=options.password)

    write("preflight the container host")
    _require_host_prerequisites(root, runner, write, serve_mode=options.serve_mode)
    _show_origins(write, serve_mode=options.serve_mode)

    write("prepare the pinned embedding model")
    _prepare_embedding_artifact(
        root, runner, confirm_download=options.confirm_download, write=write
    )

    write("build and start the database and embedding services")
    timeout = load_administrative_dataset_settings().database_operation_timeout_seconds
    runner.run_interactive(
        (*mechanics.compose_prefix(interpreter=True), "build", "db", "embedding"),
        cwd=root,
        timeout=None,
    )
    runner.run(
        (
            *mechanics.compose_prefix(interpreter=True),
            "up",
            "-d",
            "--wait",
            "--no-build",
            "db",
            "embedding",
        ),
        cwd=root,
        timeout=timeout,
    )
    write("prepare the example database")
    if _existing_table_names(load_raw_database_settings()):
        raise ValueError(
            "the example database already holds tables; use the real-database workflow or clear "
            "the example database explicitly"
        )

    write("migrate and load the example dataset")
    dataset_owner.migrate_configured_database(runner=runner)
    runner.run((sys.executable, "scripts/mock_data.py", "load"), cwd=root, timeout=None)
    runner.run((sys.executable, "scripts/mock_data.py", "write-artifacts"), cwd=root, timeout=None)

    write("prepare the feedback store")
    _prepare_feedback_store(runner=runner)
    _prepare_site_usage_directory()

    write("build and start the site and the administration backend")
    services = site_services(sender=publication)
    runner.run_interactive(
        (*mechanics.compose_prefix(interpreter=True), "build", *services),
        cwd=root,
        timeout=None,
    )
    runner.run(
        (
            *mechanics.compose_prefix(interpreter=True),
            "up",
            "-d",
            "--wait",
            "--no-build",
            *services,
        ),
        cwd=root,
        timeout=timeout,
    )

    if options.serve_mode == "domain":
        write("publish the site through the machine-wide proxy")
        publish_configured_site(runner)

    write("prove the site and the administration backend")
    prove_shells()
    dataset_owner.prove_serving_database(
        load_raw_database_settings(),
        previous_identity=None,
        port=load_web_settings().web_port,
    )
    return SelfHostReport(app_url=load_web_settings().web_app_url, dataset_id=None)


def setup_real_database(
    options: RealDatabaseSetupInput,
    *,
    runner: mechanics.ProcessRunner,
    write: Callable[[str], None],
) -> SelfHostReport:
    """Create one named empty database, enroll it as Selected, and serve and prove it.

    Prepared working files, both Compose services and idle stages are required. The configured port
    and the site's served identity are read before any mutation, and the running site container is
    re-created through the shared re-create owner after the pointer switch. A repeat refuses inside
    the named enrollment owner before another database, migration or server start.
    """
    root = administration.repository_root()
    _require_working_files(root)
    web = load_web_settings()
    if not {"db", "embedding"} <= _running_services(runner, root):
        raise ValueError(
            "the database and embedding services must already be running; run the example "
            "workflow first"
        )
    dataset_owner.require_stages_idle()

    lifecycle = load_local_lifecycle_settings()
    port = web.web_port
    with httpx.Client(
        trust_env=False,
        timeout=lifecycle.stop_timeout_seconds,
        follow_redirects=False,
    ) as client:
        # A stopped site container serves nothing to compare, as in a promotion.
        try:
            previous_identity = dataset_owner.read_serving_identity(client, port)
        except httpx.ConnectError:
            previous_identity = None

    write("create the named empty database")
    receipt: DatasetReceipt = dataset_owner.create_empty_selected_dataset(
        options.database_name, runner=runner
    )

    write("build the selected serving artifacts")
    build_serving_artifacts(
        dataset_settings(receipt.dataset_id),
        embedding=embedding_configuration_for_site(web),
    )

    write("start and prove the selected site")
    rebuild.recreate_web(runner=runner)
    prove_shells()
    dataset_owner.prove_serving_dataset(receipt, previous_identity=previous_identity, port=port)
    return SelfHostReport(app_url=web.web_app_url, dataset_id=receipt.dataset_id)


def _publication_configured(root: Path) -> bool:
    """Report whether this checkout holds a feedback token beyond the example placeholder."""
    token = file_configuration.read_local_configuration(root).get("FEEDBACK_GITHUB_TOKEN")
    placeholder = file_configuration.parse_local_configuration(
        (root / "examples" / ".env.example").read_text(encoding="utf-8")
    ).get("FEEDBACK_GITHUB_TOKEN")
    return token is not None and token != placeholder


def up_configured_stack(*, runner: mechanics.ProcessRunner, write: Callable[[str], None]) -> None:
    """Build and start the configured stack, adding the services this host runs.

    The four core services always start together. A local environment that carries a feedback token
    beyond the example placeholder adds the sender, and a same-machine worker host adds the
    profile-gated worker. The build is not bounded, and the start is bounded by the configured
    database operation timeout.
    """
    root = administration.repository_root()
    services = ["db", "embedding", *site_services(sender=_publication_configured(root))]
    write(f"start {', '.join(services)}")
    runner.run_interactive(
        (*mechanics.compose_prefix(interpreter=True), "build", *services),
        cwd=root,
        timeout=None,
    )
    runner.run(
        (
            *mechanics.compose_prefix(interpreter=True),
            "up",
            "-d",
            "--wait",
            "--no-build",
            *services,
        ),
        cwd=root,
        timeout=load_administrative_dataset_settings().database_operation_timeout_seconds,
    )


__all__ = [
    "ExampleSetupInput",
    "PublicSiteReport",
    "RealDatabaseSetupInput",
    "SelfHostReport",
    "prove_shells",
    "prove_web_shell",
    "publish_configured_site",
    "setup_example",
    "setup_real_database",
    "site_services",
    "unpublish_configured_site",
    "up_configured_stack",
]
