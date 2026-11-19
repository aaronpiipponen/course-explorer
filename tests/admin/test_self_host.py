"""Contracts for the two self-host setup workflows and the absent-template copy.

Each test drives the workflow owner. The process runner, the HTTP reads, the pinned artifact check,
the container-host preflight, the artifact builder, the table-name and feedback-store helpers, the
migration and verification helpers are substituted where the test does not exercise them. Template
copy, the exact-file edits and credential creation run as product code.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from course_explorer import administration, configuration
from course_explorer.config import (
    SETTINGS_SOURCE_MARKER,
    load_admin_web_settings,
    load_instance_settings,
    load_raw_database_settings,
    load_shared_storage_settings,
    load_web_settings,
)
from course_explorer.datasets import (
    DatasetVerification,
    read_dataset_role,
    resolve_dataset_role,
)
from course_explorer.instance import UNIVERSITIES
from course_explorer.serving_artifacts import (
    CATALOGUE_FILE,
    catalogue_body,
    serving_artifact_root,
    serving_key,
)
from course_explorer_admin import compose, mechanics, self_host, working_files
from course_explorer_admin import configuration as editor
from course_explorer_admin import datasets as dataset_owner
from course_explorer_admin.self_host import (
    ExampleSetupInput,
    RealDatabaseSetupInput,
    setup_example,
    setup_real_database,
)
from tests.config._support import (
    WORKER_CONNECTION_KEYS,
    set_administrative_controller_environment,
)
from tests.datasets._support import sample_verification
from tests.support import REPO_ROOT, write_example_config_tree

_ADMIN_PORT = 18039
_WEB_PORT = 18037
_REAL_HTTP_CLIENT = httpx.Client


def _clear_written_port_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the fixture port overrides so the workflow's own file writes supply the values."""
    for key in (
        "WEB_PORT",
        "ADMIN_WEB_PORT",
        "POSTGRES_PORT",
        "FEEDBACK_POSTGRES_PORT",
        "EMBEDDING_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    configuration.clear_configuration_caches()


def _checkout(tmp_path: Path, name: str = "checkout") -> Path:
    """Materialize one temporary checkout with the tracked example assets and the fictional
    fixture tree."""
    root = tmp_path / name
    shutil.copytree(REPO_ROOT / "examples", root / "examples")
    shutil.copytree(REPO_ROOT / "data" / "mock", root / "data" / "mock")
    return root


def _prepare_environment(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Bind the product owners to a temporary checkout and free loopback ports."""
    set_administrative_controller_environment(
        monkeypatch,
        POSTGRES_PORT="15433",
        FEEDBACK_POSTGRES_PORT="15433",
        EMBEDDING_PORT="18081",
        WEB_PORT=str(_WEB_PORT),
        ADMIN_WEB_PORT=str(_ADMIN_PORT),
    )
    monkeypatch.setattr(administration, "repository_root", lambda: root)
    monkeypatch.delenv(SETTINGS_SOURCE_MARKER, raising=False)
    configuration.clear_configuration_caches()


def _write_catalogue(root: Path, settings, payload: bytes) -> None:
    """Write one fixed catalogue artifact for the supplied database settings."""
    family = serving_artifact_root(root / settings.admin_state_root, serving_key(settings))
    current = family / "current"
    current.mkdir(parents=True, exist_ok=True)
    (current / CATALOGUE_FILE).write_bytes(payload)


class _Runner:
    """Scripted local process boundary over the workflow's exact commands."""

    def __init__(self, *, services: tuple[str, ...] = ("db", "embedding")) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.runs: list[tuple[tuple[str, ...], Path | None, dict[str, str] | None]] = []
        self.interactive_calls: list[tuple[str, ...]] = []
        self.services = set(services)
        self.created: set[str] = set()
        self.dropped: set[str] = set()

    def run(self, argv, *, cwd=None, env=None, stdin_path=None, stdout_path=None, timeout=None):
        command = tuple(argv)
        self.calls.append(command)
        self.runs.append((command, cwd, env))
        output = b""
        if "ps" in command and "--services" in command and "--status" in command:
            output = "".join(f"{service}\n" for service in sorted(self.services)).encode()
        elif "psql" in command:
            query = command[-1]
            output = "".join(
                f"{name}\n" for name in sorted(self.created) if f"'{name}'" in query
            ).encode()
        elif "createdb" in command:
            self.created.add(command[-1])
        elif "dropdb" in command:
            self.dropped.add(command[-1])
        elif command[-1:] == ("version",):
            output = b"1.0.0\n"
        return subprocess.CompletedProcess(list(command), 0, stdout=output, stderr=b"")

    def run_interactive(self, argv, *, cwd=None, env=None, timeout=None):
        command = tuple(argv)
        self.interactive_calls.append(command)
        self.run(command, cwd=cwd, env=env, timeout=timeout)


def _bind_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    artifact: list[bytes],
    identities: list[str],
    events: list[str],
    web_port: int = _WEB_PORT,
    admin_port: int = _ADMIN_PORT,
    site_running: Callable[[], bool] | None = None,
) -> None:
    """Route both workflow proofs through one fixed HTTP boundary and record each read.

    ``site_running`` models a stopped site container: while it answers false, the feature read
    refuses the connection exactly as one that serves nothing does.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("http")
        html = {"content-type": "text/html; charset=utf-8"}
        if request.url.path == "/" and request.url.port == web_port:
            return httpx.Response(200, headers=html, text="<!doctype html><title>feature</title>")
        if request.url.path == "/" and request.url.port == admin_port:
            return httpx.Response(200, headers=html, text="<!doctype html><title>console</title>")
        if request.url.path == "/api/features":
            if site_running is not None and not site_running():
                raise httpx.ConnectError("the site container is not running")
            identity = identities[0]
            events.append(f"identity:{identity}")
            return httpx.Response(
                200,
                json={
                    "embeddings_enabled": False,
                    "inference_enabled": False,
                    "dataset_identity": identity,
                },
            )
        if request.url.path == "/api/catalogue":
            return httpx.Response(
                200, content=catalogue_body(artifact[0], identities[0]), headers={}
            )
        raise AssertionError(f"unexpected serving read: {request.url}")

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_HTTP_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def test_example_files_are_copied_only_when_absent(tmp_path: Path) -> None:
    """Absent templates are created, present working files keep their bytes, and links refuse."""
    first = _checkout(tmp_path, "first")
    (first / "config").mkdir()
    present = {
        "config/web.toml": b"# present web settings\n",
        ".env": b"CUSTOM_SECRET=kept\n",
        "docker-compose.yml": b"name: kept\n",
    }
    for relative, payload in present.items():
        path = first / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    assert working_files.copy_example_files(first) is False
    for relative, payload in present.items():
        assert (first / relative).read_bytes() == payload
    assert (first / "config" / "database.toml").read_bytes() == (
        first / "examples" / "config" / "database.toml"
    ).read_bytes()
    assert (first / "config" / "wiki.json").is_file()
    assert (first / "config" / "feedback-form.json").is_file()

    second = _checkout(tmp_path, "second")
    assert working_files.copy_example_files(second) is True
    environment = second / ".env"
    assert environment.read_bytes() == (second / "examples" / ".env.example").read_bytes()
    assert environment.stat().st_mode & 0o777 == 0o600

    third = _checkout(tmp_path, "third")
    (third / "config").mkdir()
    target = tmp_path / "outside.toml"
    target.write_text("outside\n", encoding="utf-8")
    (third / "config" / "curriculum.toml").symlink_to(target)
    with pytest.raises(ValueError):
        working_files.copy_example_files(third)
    assert target.read_text(encoding="utf-8") == "outside\n"


def test_example_workflow_refuses_a_host_the_preflight_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An account that cannot write the Docker socket stops before any build or download, a domain
    address with a port is refused, and a domain-mode run refuses a public port a listener holds on
    the configured bind address or the wildcard address while a listener on another address
    passes."""
    root = _checkout(tmp_path, "socket")
    _prepare_environment(monkeypatch, root)
    runner = _Runner()

    with pytest.raises(ValueError, match="without a port"):
        ExampleSetupInput(
            serve_mode="domain",
            app_url="https://course-explorer.test:443/",
            instance_key="example",
            web_port=_WEB_PORT,
            admin_web_port=_ADMIN_PORT,
            postgres_port=15433,
            embedding_port=18081,
            username="synthetic-operator",
            password=SecretStr("synthetic-password"),
        )

    monkeypatch.setattr(compose, "docker_needs_sudo", lambda: True)
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(tmp_path / "shared-socket"))
    configuration.clear_configuration_caches()

    with pytest.raises(ValueError, match="docker group"):
        setup_example(
            ExampleSetupInput(
                serve_mode="local",
                instance_key="example",
                web_port=_WEB_PORT,
                admin_web_port=_ADMIN_PORT,
                postgres_port=15433,
                embedding_port=18081,
                username="synthetic-operator",
                password=SecretStr("synthetic-password"),
            ),
            runner=runner,
            write=lambda _message: None,
        )

    # Only the three container-host checks ran, so no build, download or Compose command ran.
    socket_calls = list(runner.calls)
    assert [command[:2] for command in socket_calls] == [
        ("docker", "--version"),
        ("docker", "compose"),
        ("docker", "info"),
    ]
    assert not any("build" in command or "up" in command for command in socket_calls)

    # A domain-mode run refuses a public port held by a process other than the proxy.
    monkeypatch.setattr(compose, "docker_needs_sudo", lambda: False)
    monkeypatch.setattr(
        self_host.proxy, "listening_sockets", lambda: frozenset({("192.0.2.10", 80)})
    )
    monkeypatch.setattr(self_host.proxy, "running", lambda runner, *, shared_root: False)

    def run_domain() -> None:
        setup_example(
            ExampleSetupInput(
                serve_mode="domain",
                app_url="https://course-explorer.test/",
                instance_key="example",
                web_port=_WEB_PORT,
                admin_web_port=_ADMIN_PORT,
                postgres_port=15433,
                embedding_port=18081,
                username="synthetic-operator",
                password=SecretStr("synthetic-password"),
            ),
            runner=runner,
            write=lambda _message: None,
        )

    with pytest.raises(ValueError, match="feature HTTP port 80 is used by another process"):
        run_domain()

    # A listener outside the configured bind address passes the port check, while a wildcard
    # listener on the same port blocks the specific bind.
    (tmp_path / "shared-socket").mkdir(exist_ok=True)
    monkeypatch.setenv("PROXY_BIND_ADDRESS", "192.0.2.10")
    configuration.clear_configuration_caches()
    monkeypatch.setattr(
        self_host.proxy, "listening_sockets", lambda: frozenset({("192.0.2.20", 443)})
    )
    with pytest.raises(ValueError, match="the pinned embedding model is absent"):
        run_domain()
    monkeypatch.setattr(self_host.proxy, "listening_sockets", lambda: frozenset({("0.0.0.0", 443)}))
    with pytest.raises(ValueError, match="feature HTTPS port 443 is used by another process"):
        run_domain()

    assert runner.interactive_calls == []
    assert not any("build" in command or "up" in command for command in runner.calls)


@pytest.mark.parametrize(
    ("serve_mode", "app_url", "publication", "proxy_bind_address"),
    [
        ("local", f"http://localhost:{_WEB_PORT}/", False, None),
        ("domain", "https://course-explorer.test/", False, None),
        ("domain", "https://course-explorer.test/", True, None),
        ("domain", "https://course-explorer.test/", False, "192.0.2.10"),
    ],
)
def test_example_workflow_reaches_bootstrap_serving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    serve_mode: str,
    app_url: str,
    publication: bool,
    proxy_bind_address: str | None,
) -> None:
    """The example workflow builds both image sets, starts the services publication needs, publishes
    a domain-mode site through the machine-wide proxy with its configured bind address, and proves
    both shells."""
    root = _checkout(tmp_path, "checkout-bootstrap")
    _prepare_environment(monkeypatch, root)
    _clear_written_port_overrides(monkeypatch)
    if proxy_bind_address is not None:
        monkeypatch.setenv("PROXY_BIND_ADDRESS", proxy_bind_address)
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    monkeypatch.setattr(compose, "DOCKER_SOCKET_PATH", socket_path)
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(tmp_path / "shared-bootstrap"))
    configuration.clear_configuration_caches()
    if publication:
        # A non-example destination lets the publication branch run; no token is real and no
        # repository write happens.
        (root / "config").mkdir()
        feedback = (REPO_ROOT / "examples" / "config" / "feedback.toml").read_text(encoding="utf-8")
        (root / "config" / "feedback.toml").write_text(
            feedback.replace(
                'FEEDBACK_GITHUB_REPOSITORY = "example-owner/example-repository"',
                'FEEDBACK_GITHUB_REPOSITORY = "OWNER/REPOSITORY"',
            ),
            encoding="utf-8",
        )
    runner = _Runner()
    events: list[str] = []
    artifact = [b'{"courses":[]}']
    identities = ["bootstrap"]
    _bind_http(
        monkeypatch,
        artifact=artifact,
        identities=identities,
        events=events,
    )

    artifact_paths: list[Path] = []

    def artifact_present(path: Path, digest: str) -> bool:
        artifact_paths.append(Path(path))
        return True

    monkeypatch.setattr(self_host.artifacts, "artifact_present", artifact_present)
    monkeypatch.setattr(self_host, "_existing_table_names", lambda settings: ())
    monkeypatch.setattr(
        self_host, "_require_host_prerequisites", lambda root, runner, write, *, serve_mode: None
    )

    def prepare_feedback(*, runner) -> None:
        events.append("feedback")

    monkeypatch.setattr(self_host, "_prepare_feedback_store", prepare_feedback)

    def write_artifact(*_args, **_kwargs) -> None:
        events.append("artifact")
        _write_catalogue(root, load_raw_database_settings(), artifact[0])

    monkeypatch.setattr(
        dataset_owner,
        "migrate_configured_database",
        lambda *, runner: events.append("migrate") or sample_verification(),
    )

    # The fake runner writes the bundle artifact when the bundle writer action runs.
    original_run = runner.run

    def run(argv, **kwargs):
        if tuple(argv)[-2:] == ("scripts/mock_data.py", "write-artifacts"):
            write_artifact()
        return original_run(argv, **kwargs)

    runner.run = run

    report = setup_example(
        ExampleSetupInput(
            serve_mode=serve_mode,
            app_url=app_url,
            instance_key="example",
            web_port=_WEB_PORT,
            admin_web_port=_ADMIN_PORT,
            postgres_port=15433,
            embedding_port=18081,
            username="synthetic-operator",
            password=SecretStr("synthetic-password"),
            confirm_download=False,
            publication_token=SecretStr("synthetic-token") if publication else None,
        ),
        runner=runner,
        write=events.append,
    )

    assert report.app_url == app_url
    assert report.dataset_id is None
    assert read_dataset_role(root / "data" / "admin", "selected") is None
    assert "identity:bootstrap" in events
    assert editor.read_configuration_file("config/web.toml").values["INSTANCE_KEY"] == "example"
    assert editor.read_configuration_file("config/web.toml").values["WEB_PORT"] == _WEB_PORT
    assert editor.read_configuration_file("config/admin_web.toml").values["ADMIN_WEB_PORT"] == (
        _ADMIN_PORT
    )
    assert editor.read_configuration_file("config/database.toml").values["POSTGRES_PORT"] == 15433
    assert (
        editor.read_configuration_file("config/feedback.toml").values["FEEDBACK_POSTGRES_PORT"]
        == 15433
    )
    assert editor.read_configuration_file("config/search.toml").values["EMBEDDING_PORT"] == 18081
    assert sorted(path.name for path in (tmp_path / "shared-bootstrap").iterdir()) == (
        ["locks", "models", "proxy"] if serve_mode == "domain" else ["locks", "models"]
    )
    assert artifact_paths == [tmp_path / "shared-bootstrap" / "models" / "bge-m3-q8_0.gguf"]

    commands = runner.calls
    bridge = mechanics.compose_prefix(interpreter=True)
    steps = [command[len(bridge) :] for command in commands if command[: len(bridge)] == bridge]
    build_services = ["web", "admin", *(["sender"] if publication else [])]
    tail = ["sender"] if publication else []
    assert steps == [
        ("build", "db", "embedding"),
        ("up", "-d", "--wait", "--no-build", "db", "embedding"),
        ("build", *build_services),
        ("up", "-d", "--wait", "--no-build", "web", "admin", *tail),
    ]
    assert runner.interactive_calls == [
        (*bridge, "build", "db", "embedding"),
        (*bridge, "build", *build_services),
    ]
    proxy_root = tmp_path / "shared-bootstrap" / "proxy"
    proxy_prefix = (
        *compose.docker_prefix(non_interactive=True),
        "compose",
        "--file",
        str(proxy_root / "docker-compose.yml"),
    )
    proxy_steps = [
        command[len(proxy_prefix) :]
        for command in commands
        if command[: len(proxy_prefix)] == proxy_prefix
    ]
    # No image build names the proxy in either mode.
    assert not any("build" in command and "proxy" in command for command in commands)
    if serve_mode == "domain":
        templates = REPO_ROOT / "examples" / "deploy" / "proxy"
        assert (proxy_root / "Caddyfile").read_bytes() == (templates / "Caddyfile").read_bytes()
        assert (proxy_root / "docker-compose.yml").read_bytes() == (
            templates / "docker-compose.yml"
        ).read_bytes()
        bind_line = f"\tbind {proxy_bind_address}\n" if proxy_bind_address is not None else ""
        assert (proxy_root / "sites" / "example.caddy").read_text(encoding="utf-8") == (
            "# Public site for the configured domain, forwarded to this instance's loopback "
            "site port.\n"
            "https://course-explorer.test {\n"
            f"{bind_line}"
            f"\treverse_proxy 127.0.0.1:{_WEB_PORT}\n"
            "}\n"
        )
        assert proxy_steps == [
            ("up", "-d", "--wait", "proxy"),
            (
                "exec",
                "-T",
                "proxy",
                "caddy",
                "reload",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
                "--address",
                "unix//tmp/caddy-admin.sock",
            ),
        ]
    else:
        assert not proxy_root.exists()
        assert proxy_steps == []
    load = next(
        index
        for index, command in enumerate(commands)
        if command[-2:] == ("scripts/mock_data.py", "load")
    )
    artifacts = next(
        index
        for index, command in enumerate(commands)
        if command[-2:] == ("scripts/mock_data.py", "write-artifacts")
    )
    embedding_start = commands.index(
        (*bridge, "up", "-d", "--wait", "--no-build", "db", "embedding")
    )
    site_build = next(
        index
        for index, command in enumerate(commands)
        if command[len(bridge) :] == ("build", *build_services)
    )
    # The example import runs after both services start and before the site image build.
    assert embedding_start < load < artifacts
    assert artifacts < site_build
    # No host build runs and no unit is installed.
    assert not any(command[0] in {"npm", "systemctl", "loginctl"} for command in commands)
    assert not any("build_native.py" in command for command in commands)
    assert events.index("migrate") < events.index("artifact")
    assert events.index("artifact") < events.index("feedback")
    assert events.index("feedback") < events.index("http")


def test_example_workflow_builds_and_starts_the_same_machine_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The example workflow builds and starts the profile-gated worker on a same-machine host.

    The worker joins the site group in the build and the start, so the first-run path starts the
    same services that the configured stack starts on that host.
    """
    root = _checkout(tmp_path, "checkout-same-machine")
    _prepare_environment(monkeypatch, root)
    _clear_written_port_overrides(monkeypatch)
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(tmp_path / "shared-worker"))
    working_files.copy_example_files(root)
    (root / "config" / "worker.toml").write_text(
        'WORKER_HOST_KIND = "same-machine"\n'
        'WORKER_PROJECT_ROOT = "/app"\n'
        "WORKER_SSH_CONNECT_TIMEOUT_SECONDS = 10\n",
        encoding="utf-8",
    )
    # The ordinary-test fixture injects the example worker aliases into the process environment,
    # and a process value wins over the subsystem file, so the file is the only source here.
    for alias in WORKER_CONNECTION_KEYS:
        monkeypatch.delenv(alias, raising=False)
    configuration.clear_configuration_caches()
    runner, events = _stub_example_host(monkeypatch, root)

    report = setup_example(
        ExampleSetupInput(
            serve_mode="local",
            instance_key="example",
            web_port=_WEB_PORT,
            admin_web_port=_ADMIN_PORT,
            postgres_port=15433,
            embedding_port=18081,
            username="synthetic-operator",
            password=SecretStr("synthetic-password"),
        ),
        runner=runner,
        write=events.append,
    )

    assert report.app_url == f"http://localhost:{_WEB_PORT}/"
    bridge = mechanics.compose_prefix(interpreter=True)
    steps = [command[len(bridge) :] for command in runner.calls if command[: len(bridge)] == bridge]
    assert steps == [
        ("build", "db", "embedding"),
        ("up", "-d", "--wait", "--no-build", "db", "embedding"),
        ("build", "web", "admin", "worker"),
        ("up", "-d", "--wait", "--no-build", "web", "admin", "worker"),
    ]


def _stub_example_host(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    web_port: int = _WEB_PORT,
    admin_port: int = _ADMIN_PORT,
) -> tuple[_Runner, list[str]]:
    """Bind one example workflow's external boundaries to a temporary checkout."""
    runner = _Runner()
    events: list[str] = []
    artifact = [b'{"courses":[]}']
    identities = ["bootstrap"]
    _bind_http(
        monkeypatch,
        artifact=artifact,
        identities=identities,
        events=events,
        web_port=web_port,
        admin_port=admin_port,
    )
    monkeypatch.setattr(self_host.artifacts, "artifact_present", lambda path, digest: True)
    monkeypatch.setattr(self_host, "_existing_table_names", lambda settings: ())
    monkeypatch.setattr(
        self_host, "_require_host_prerequisites", lambda root, runner, write, *, serve_mode: None
    )
    monkeypatch.setattr(self_host, "_prepare_feedback_store", lambda *, runner: None)
    monkeypatch.setattr(
        dataset_owner,
        "migrate_configured_database",
        lambda *, runner: sample_verification(),
    )

    original_run = runner.run

    def run(argv, **kwargs):
        if tuple(argv)[-2:] == ("scripts/mock_data.py", "write-artifacts"):
            _write_catalogue(root, load_raw_database_settings(), artifact[0])
        return original_run(argv, **kwargs)

    runner.run = run
    return runner, events


def test_setup_instances_use_independent_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two setups prepare their own settings and Compose project names."""
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    monkeypatch.setattr(compose, "DOCKER_SOCKET_PATH", socket_path)
    monkeypatch.setenv(compose.HOST_ROOT_ALIAS, str(tmp_path / "host-checkout"))
    monkeypatch.setenv(compose.SSH_DIRECTORY_ALIAS, str(tmp_path / "host-ssh"))
    assert compose.host_checkout_root() == tmp_path / "host-checkout"
    assert compose.host_ssh_directory() == tmp_path / "host-ssh"
    monkeypatch.delenv(compose.HOST_ROOT_ALIAS)
    monkeypatch.delenv(compose.SSH_DIRECTORY_ALIAS)
    instances = (
        ("example", _WEB_PORT, _ADMIN_PORT, 15433, 18081),
        ("diak", 18077, 18079, 15473, 18121),
    )
    # The self-host script runs the workflow inside the setup container, whose account home is
    # `/home/course`, and names the Docker host's home through its own alias.
    host_home = tmp_path / "host-home"
    monkeypatch.setenv("HOME", "/home/course")
    monkeypatch.setenv("COURSE_EXPLORER_HOST_HOME", str(host_home))
    projections: dict[str, dict[str, str]] = {}
    for key, web_port, admin_port, postgres_port, embedding_port in instances:
        root = _checkout(tmp_path, f"instance-{key}")
        _prepare_environment(monkeypatch, root)
        _clear_written_port_overrides(monkeypatch)
        monkeypatch.setenv("ADMIN_SHARED_ROOT", f"~/.local/share/course-explorer-{key}")
        configuration.clear_configuration_caches()
        # A Peppi PSG entry maps the periods its source lists, so its checkout carries the last
        # listed year in CURRENT_ACADEMIC_YEAR before the setup writes the instance key.
        psg_block = UNIVERSITIES[key].peppi_psg
        if psg_block is not None:
            write_example_config_tree(root)
            curriculum = root / "config" / "curriculum.toml"
            curriculum.write_text(
                curriculum.read_text(encoding="utf-8").replace(
                    'CURRENT_ACADEMIC_YEAR = "2026-2027"',
                    f'CURRENT_ACADEMIC_YEAR = "{psg_block.periods[-1].id}"',
                ),
                encoding="utf-8",
            )
        runner, events = _stub_example_host(
            monkeypatch,
            root,
            web_port=web_port,
            admin_port=admin_port,
        )
        report = setup_example(
            ExampleSetupInput(
                serve_mode="local",
                instance_key=key,
                web_port=web_port,
                admin_web_port=admin_port,
                postgres_port=postgres_port,
                embedding_port=embedding_port,
                username="synthetic-operator",
                password=SecretStr("synthetic-password"),
            ),
            runner=runner,
            write=events.append,
        )

        assert report.app_url == f"http://localhost:{web_port}/"
        assert load_instance_settings().instance_key == key
        assert load_web_settings().web_port == web_port
        assert load_admin_web_settings().admin_web_port == admin_port
        assert load_admin_web_settings().admin_web_origin == f"https://localhost:{admin_port}"
        assert load_raw_database_settings().postgres_port == postgres_port
        site = editor.read_configuration_file("config/admin_web.toml").values
        assert not [name for name in site if name.startswith("ADMIN_SITE_")]
        web_values = editor.read_configuration_file("config/web.toml").values
        assert web_values["WEB_PREREQUISITES_ENABLED"] is (
            UNIVERSITIES[key].source_family == "sisu"
        )
        if key in {"diak", "metropolia"}:
            assert UNIVERSITIES[key].source_family == "guide"
            assert web_values["WEB_PREREQUISITES_ENABLED"] is False
        projections[key] = compose.compose_environment()

    assert projections["example"]["COURSE_EXPLORER_PROJECT_NAME"] == "course-explorer-example"
    assert projections["diak"]["COURSE_EXPLORER_PROJECT_NAME"] == "course-explorer-diak"
    assert projections["example"]["POSTGRES_PORT"] == "15433"
    assert projections["diak"]["POSTGRES_PORT"] == "15473"
    assert projections["example"]["EMBEDDING_PORT"] == "18081"
    assert projections["diak"]["EMBEDDING_PORT"] == "18121"
    for key, _, _, _, _ in instances:
        projection = projections[key]
        assert projection["COURSE_EXPLORER_UID"] == str(os.getuid())
        assert projection["COURSE_EXPLORER_GID"] == str(os.getgid())
        assert projection["COURSE_EXPLORER_DOCKER_GID"] == str(socket_path.stat().st_gid)
        assert projection["COURSE_EXPLORER_HOST_ROOT"] == str(tmp_path / f"instance-{key}")
        assert projection["COURSE_EXPLORER_SSH_DIR"] == str(Path.home() / ".ssh")
        assert projection["ADMIN_SHARED_ROOT"] == str(
            host_home / f".local/share/course-explorer-{key}"
        )
        assert projection["ADMIN_STATE_ROOT"] == "data/admin"


def test_setup_adopts_the_configured_shared_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured existing shared directory keeps its file and gains the two layout directories."""
    root = _checkout(tmp_path, "adopt")
    _prepare_environment(monkeypatch, root)
    _clear_written_port_overrides(monkeypatch)
    shared = tmp_path / "existing-shared"
    shared.mkdir()
    sentinel = shared / "model.gguf"
    sentinel.write_bytes(b"synthetic model bytes")
    # The self-host script passes the shared directory in the settings environment, so the workflow
    # adopts the configured value and writes it to the database settings.
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(shared))
    configuration.clear_configuration_caches()
    runner, events = _stub_example_host(monkeypatch, root)

    setup_example(
        ExampleSetupInput(
            serve_mode="local",
            instance_key="example",
            web_port=_WEB_PORT,
            admin_web_port=_ADMIN_PORT,
            postgres_port=15433,
            embedding_port=18081,
            username="synthetic-operator",
            password=SecretStr("synthetic-password"),
        ),
        runner=runner,
        write=events.append,
    )

    assert sentinel.read_bytes() == b"synthetic model bytes"
    assert sorted(path.name for path in shared.iterdir()) == ["locks", "model.gguf", "models"]
    assert oct((shared / "models").stat().st_mode & 0o777) == "0o700"
    assert oct((shared / "locks").stat().st_mode & 0o777) == "0o700"
    assert load_shared_storage_settings().shared_root == shared
    assert editor.read_configuration_file("config/database.toml").values[
        "ADMIN_SHARED_ROOT"
    ] == str(shared)


def test_stack_up_starts_the_configured_services_and_the_feedback_sender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`up` starts the four core services and adds the sender and the same-machine worker.

    A checkout whose environment file carries only the example placeholders starts no sender, a
    configured token adds it, and a same-machine host adds the profile-gated worker. One step line
    names every started service.
    """
    root = _checkout(tmp_path, "stack-up")
    _prepare_environment(monkeypatch, root)
    working_files.copy_example_files(root)

    def write_worker(host_kind: str, project_root: str, ssh_target: str | None) -> None:
        lines = [
            f'WORKER_HOST_KIND = "{host_kind}"',
            f'WORKER_PROJECT_ROOT = "{project_root}"',
            "WORKER_SSH_CONNECT_TIMEOUT_SECONDS = 10",
        ]
        if ssh_target is not None:
            lines.insert(1, f'WORKER_SSH_TARGET = "{ssh_target}"')
        (root / "config" / "worker.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        # The ordinary-test fixture injects the example worker aliases into the process environment,
        # and a process value wins over the subsystem file, so the file is the only source here.
        for alias in WORKER_CONNECTION_KEYS:
            monkeypatch.delenv(alias, raising=False)
        configuration.clear_configuration_caches()

    write_worker("linux", "/home/synthetic/course-explorer", "worker-host")

    def services(command: tuple[str, ...], verb: str) -> list[str]:
        index = command.index(verb)
        return [item for item in command[index + 1 :] if not item.startswith("-")]

    core = ["db", "embedding", "web", "admin"]

    local = _Runner()
    events: list[str] = []
    self_host.up_configured_stack(runner=local, write=events.append)

    assert events == [f"start {', '.join(core)}"]
    (local_build,) = [command for command in local.interactive_calls if "build" in command]
    (local_start,) = [entry[0] for entry in local.runs if "up" in entry[0]]
    assert services(local_build, "build") == core
    assert services(local_start, "up") == core

    (root / ".env").write_text(
        "POSTGRES_PASSWORD='synthetic-password'\n"
        "FEEDBACK_POSTGRES_PASSWORD='synthetic-password'\n"
        "FEEDBACK_GITHUB_TOKEN='synthetic-sender-token'\n",
        encoding="utf-8",
    )
    write_worker("same-machine", "/app", None)

    published = _Runner()
    published_events: list[str] = []
    self_host.up_configured_stack(runner=published, write=published_events.append)

    expected = [*core, "sender", "worker"]
    assert published_events == [f"start {', '.join(expected)}"]
    (published_build,) = [command for command in published.interactive_calls if "build" in command]
    (published_start,) = [entry[0] for entry in published.runs if "up" in entry[0]]
    assert services(published_build, "build") == expected
    assert services(published_start, "up") == expected


def test_real_database_workflow_serves_an_empty_initial_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The named owner creates and enrolls one empty database, builds before serving, and refuses a
    repeat."""
    root = _checkout(tmp_path, "checkout-real")
    _prepare_environment(monkeypatch, root)
    working_files.copy_example_files(root)
    runner = _Runner()
    events: list[str] = []
    artifact = [b'{"courses":[]}']
    identities = ["selected"]
    # The site container is stopped before the re-create and answers after it, so the workflow
    # records no previous identity and proves the new selection.
    sites: list[bool] = [False]
    _bind_http(
        monkeypatch,
        artifact=artifact,
        identities=identities,
        events=events,
        site_running=lambda: sites[0],
    )
    original_run = runner.run

    def run(argv, **kwargs):
        recreated = (
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--no-build",
            "web",
        )
        command = tuple(argv)
        if command[-len(recreated) :] == recreated:
            sites[0] = True
        return original_run(argv, **kwargs)

    runner.run = run
    # The artifact builder, the migration upgrade and the verification read are the substituted
    # boundaries.
    monkeypatch.setattr(
        self_host,
        "build_serving_artifacts",
        lambda settings, *, embedding: _build(settings),
    )
    monkeypatch.setattr(
        dataset_owner, "_run_migration_upgrade", lambda name: events.append("migrate")
    )
    monkeypatch.setattr(
        dataset_owner, "read_dataset_verification", lambda settings: sample_verification()
    )

    def _build(settings) -> None:
        events.append("build")
        _write_catalogue(root, settings, artifact[0])
        return None

    name = "course_explorer_real_proof"
    report = setup_real_database(
        RealDatabaseSetupInput(database_name=name),
        runner=runner,
        write=events.append,
    )
    verification = dataset_owner.migrate_dataset("selected", runner=runner)
    assert isinstance(verification, DatasetVerification)

    assert report.dataset_id == "0" * 32
    assert load_raw_database_settings().postgres_db == "course_explorer"
    assert name in runner.created
    assert runner.dropped == set()
    selected = resolve_dataset_role(root / "data" / "admin", "selected")
    assert selected.dataset_id == "0" * 32
    assert selected.database_name == name
    assert read_dataset_role(root / "data" / "admin", "working") is None
    assert events.index("build") < events.index("identity:selected")
    assert "identity:selected" in events
    # The one site re-create runs through the Compose bridge and names its service alone.
    bridge = mechanics.compose_prefix(non_interactive=True, interpreter=True)
    recreated = ("up", "-d", "--wait", "--force-recreate", "--no-build", "web")

    def recreated_commands() -> list[tuple[str, ...]]:
        return [command for command in runner.calls if command[len(bridge) :] == recreated]

    assert len(recreated_commands()) == 1

    created = len(runner.created)
    builds = events.count("build")
    with pytest.raises(ValueError):
        setup_real_database(
            RealDatabaseSetupInput(database_name=name),
            runner=runner,
            write=events.append,
        )
    assert len(runner.created) == created
    assert events.count("build") == builds
    # The repeat refuses inside the enrollment owner, so it re-creates nothing.
    assert len(recreated_commands()) == 1


def test_listening_sockets_reports_a_bound_loopback_port() -> None:
    """A loopback address and port bound in the listen state appears in the listen table read."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert ("127.0.0.1", port) in self_host.proxy.listening_sockets()


def test_unpublish_removes_only_its_site_file_and_reloads_a_running_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One instance's site file is removed, the other keeps its bytes, and a running proxy reloads."""
    root = _checkout(tmp_path, "unpublish")
    _prepare_environment(monkeypatch, root)
    working_files.copy_example_files(root)
    shared = tmp_path / "shared"
    sites = shared / "proxy" / "sites"
    sites.mkdir(parents=True)
    removed = sites / "diak.caddy"
    kept = sites / "metropolia.caddy"
    removed.write_text("https://course-explorer-one.test {\n}\n", encoding="utf-8")
    kept.write_text("https://course-explorer-two.test {\n}\n", encoding="utf-8")
    runner = _Runner()
    monkeypatch.setattr(self_host.proxy, "running", lambda process_runner, *, shared_root: True)

    self_host.proxy.unpublish(runner, shared_root=shared, instance_key="diak")

    reload = (
        "exec",
        "-T",
        "proxy",
        "caddy",
        "reload",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
        "--address",
        "unix//tmp/caddy-admin.sock",
    )
    assert not removed.exists()
    assert kept.read_text(encoding="utf-8") == "https://course-explorer-two.test {\n}\n"
    assert [command for command in runner.calls if command[-len(reload) :] == reload]

    # A stopped project serves nothing, so the second removal runs no Compose command.
    monkeypatch.setattr(self_host.proxy, "running", lambda process_runner, *, shared_root: False)
    calls = len(runner.calls)
    self_host.proxy.unpublish(runner, shared_root=shared, instance_key="diak")
    assert len(runner.calls) == calls
