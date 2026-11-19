"""Contracts that the subsystem files and the device-local .env compose automatically."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from course_explorer import administration, configuration
from course_explorer.config import (
    SETTINGS_SOURCE_MARKER,
    AdminWebSettings,
    DatabaseSettings,
    EmbeddingRuntimeSettings,
    FeedbackIntakeSettings,
    FeedbackSenderSettings,
    FeedbackSettings,
    InferenceContextSettings,
    KoriSettings,
    NormalizationSettings,
    SearchSettings,
    SharedStorageSettings,
    UsageSettings,
    WebSettings,
    WorkerConnectionSettings,
    WorkerGpuSettings,
    load_inference_context_settings,
)
from course_explorer.enums import OfficialTextField
from course_explorer.instance import UNIVERSITIES
from course_explorer_admin import compose
from tests.config._support import FIXTURE_VALUES, set_administrative_controller_environment
from tests.support import write_example_config_tree


def _bind_checkout(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point the file-source owner at one temporary checkout and drop cached reads."""
    monkeypatch.delenv(SETTINGS_SOURCE_MARKER, raising=False)
    for alias in FIXTURE_VALUES:
        monkeypatch.delenv(alias, raising=False)
    monkeypatch.setattr(administration, "repository_root", lambda: root)
    configuration.clear_configuration_caches()


def test_loads_automatic_configuration_and_applies_override_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The example TOML values load without sourcing, the environment file supplies the database
    password, an explicit process value wins, and an instance-key override selects that
    university's mapped source facts.
    """
    root = tmp_path / "checkout"
    write_example_config_tree(root)
    (root / ".env").write_text(
        "POSTGRES_PASSWORD=synthetic-secret\n"
        "FEEDBACK_POSTGRES_PASSWORD=synthetic-feedback-secret\n"
        "UNRELATED_SECRET=synthetic-unrelated-secret\n",
        encoding="utf-8",
    )
    (root / ".env").chmod(0o600)
    _bind_checkout(monkeypatch, root)

    # Example TOML values load with no sourcing, and the local .env supplies the secrets.
    database = DatabaseSettings()
    assert database.postgres_db == "course_explorer"
    assert database.postgres_password.get_secret_value() == "synthetic-secret"
    assert database.example_postgres_db == "course_explorer"
    assert WebSettings().web_port == 8037
    assert WebSettings().embeddings_enabled is True
    assert WebSettings().inference_enabled is True
    assert WebSettings().prerequisites_enabled is True
    # The one configured instance key selects the site's university metadata.
    assert WebSettings().instance_key == "example"
    assert WebSettings().display_label == UNIVERSITIES["example"].display_label
    assert WebSettings().university.name_en == UNIVERSITIES["example"].name_en
    assert WebSettings().university.name_fi == UNIVERSITIES["example"].name_fi
    # The worker host kind and the worker identities load from their owning subsystem file.
    worker = WorkerConnectionSettings()
    assert worker.host_kind == "wsl"
    assert worker.ssh_target == "replace-with-ssh-target"
    assert worker.wsl_distribution == "replace-with-wsl-distribution"
    assert worker.worker_project_root == Path("/home/replace-with-user/course-explorer")
    # The worker GPU backend, its build target and the card index load from the same file.
    gpu = WorkerGpuSettings()
    assert gpu.gpu_backend == "rocm"
    assert gpu.gpu_target == "gfx1201"
    assert gpu.gpu_device_index == 0
    # One app address supplies both consumers, and the feedback origin drops its root slash.
    assert WebSettings().web_app_url == "http://localhost:8037/"
    assert AdminWebSettings().web_app_url == "http://localhost:8037/"
    feedback = FeedbackSettings()
    assert feedback.feedback_postgres_db == "course_explorer_feedback"
    assert feedback.feedback_postgres_port == 5433
    assert feedback.feedback_attachments_root == Path("data/attachments")
    assert feedback.feedback_postgres_password.get_secret_value() == "synthetic-feedback-secret"
    # The shared directory is one machine-wide path, and a home-prefixed value expands.
    shared = SharedStorageSettings()
    assert shared.shared_root == Path.home() / ".local/share/course-explorer"
    assert shared.models_root == shared.shared_root / "models"
    assert shared.locks_root == shared.shared_root / "locks"
    # A container expands the home prefix against the Docker host's home it names, so the resolved
    # path addresses the host's directory rather than the container account's.
    host_home = tmp_path / "host-home"
    with monkeypatch.context() as container:
        container.setenv("HOME", "/home/course")
        container.setenv("COURSE_EXPLORER_HOST_HOME", str(host_home))
        assert SharedStorageSettings().shared_root == (host_home / ".local/share/course-explorer")
    relative = Path("models/bge-m3-q8_0.gguf")
    assert shared.resolve_embedding_artifact(relative) == shared.shared_root / relative
    absolute = Path("/srv/models/bge-m3-q8_0.gguf")
    assert shared.resolve_embedding_artifact(absolute) == absolute
    intake = FeedbackIntakeSettings()
    assert intake.web_app_url == "http://localhost:8037/"
    assert intake.web_app_origin == "http://localhost:8037"
    assert intake.feedback_github_repository == "example-owner/example-repository"
    assert intake.feedback_github_visibility == "public"
    assert intake.feedback_browser_submissions_per_hour == 5
    assert intake.feedback_site_submissions_per_hour == 30
    assert intake.feedback_max_request_bytes == 25165824
    # The intake family reads the destination without a sender credential, while the full sender
    # refuses its absence.
    with pytest.raises(ValidationError):
        FeedbackSenderSettings()
    monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "synthetic-sender-token")
    sender = FeedbackSenderSettings()
    assert sender.feedback_github_repository == "example-owner/example-repository"
    assert sender.feedback_github_visibility == "public"
    assert sender.feedback_github_timeout_seconds == 120.0

    # An explicit process override wins over both files.
    monkeypatch.setenv("EMBEDDING_PORT", "18080")
    monkeypatch.setenv("COMPARISON_OVERLAP_CEILING", "0.5")
    monkeypatch.setenv("SEARCH_EMBEDDING_REQUEST_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("SEARCH_EMBEDDING_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("WEB_PORT", "18028")
    monkeypatch.setenv("WEB_APP_URL", "http://localhost:18037/")
    monkeypatch.setenv("INSTANCE_DISPLAY_LABEL", "custom-label")
    monkeypatch.setenv("WEB_EMBEDDINGS_ENABLED", "0")
    monkeypatch.setenv("WEB_INFERENCE_ENABLED", "0")
    monkeypatch.setenv("WEB_PREREQUISITES_ENABLED", "0")
    monkeypatch.setenv("INFERENCE_TARGET_FIELDS", '["name","content"]')
    monkeypatch.setenv("INFERENCE_POOL_FIELDS", '["name","outcomes"]')
    monkeypatch.setenv("USAGE_RETENTION_DAYS", "7")
    monkeypatch.setenv("FEEDBACK_POSTGRES_PORT", "15432")
    monkeypatch.setenv("FEEDBACK_BROWSER_SUBMISSIONS_PER_HOUR", "9")
    monkeypatch.setenv("FEEDBACK_GITHUB_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("ADMIN_SHARED_ROOT", "/synthetic/shared")
    monkeypatch.setenv("ADMIN_WEB_PORT", "18027")
    monkeypatch.setenv("ADMIN_WEB_SESSION_IDLE_MINUTES", "5")
    monkeypatch.setenv("ADMIN_WEB_POLL_SECONDS", "3")
    assert EmbeddingRuntimeSettings().embedding_port == 18080
    assert SearchSettings().comparison_overlap_ceiling == 0.5
    assert SearchSettings().search_embedding_request_timeout_seconds == 7
    assert SearchSettings().search_embedding_max_attempts == 2
    assert WebSettings().web_port == 18028
    assert WebSettings().web_app_url == "http://localhost:18037/"
    assert WebSettings().display_label == "custom-label"
    assert AdminWebSettings().web_app_url == "http://localhost:18037/"
    assert AdminWebSettings().display_label == "custom-label"
    assert FeedbackIntakeSettings().web_app_origin == "http://localhost:18037"
    assert WebSettings().embeddings_enabled is False
    assert WebSettings().inference_enabled is False
    assert WebSettings().prerequisites_enabled is False
    # The two optional prompt-field keys resolve through the same composition on the class and
    # its loader, and an override differs from the teaching-field baseline.
    context = InferenceContextSettings()
    loaded = load_inference_context_settings()
    university = WebSettings().university
    for family in (context, loaded):
        assert family.resolved_target_fields(university) == (
            OfficialTextField.NAME,
            OfficialTextField.CONTENT,
        )
        assert family.resolved_pool_fields(university) == (
            OfficialTextField.NAME,
            OfficialTextField.OUTCOMES,
        )
        assert family.prompt_field_policy(university) == {
            "target": ["name", "content"],
            "pool": ["name", "outcomes"],
        }
    assert UsageSettings().usage_retention_days == 7
    assert FeedbackSettings().feedback_postgres_port == 15432
    assert FeedbackIntakeSettings().feedback_browser_submissions_per_hour == 9
    assert FeedbackSenderSettings().feedback_github_timeout_seconds == 45.0
    assert SharedStorageSettings().shared_root == Path("/synthetic/shared")
    assert AdminWebSettings().admin_web_port == 18027
    assert AdminWebSettings().admin_web_session_idle_minutes == 5
    assert AdminWebSettings().admin_web_poll_seconds == 3

    # An instance-key override selects that university's mapped source facts, so the Kori and
    # normalization families read one source base, root, first year and guide base.
    monkeypatch.setenv("INSTANCE_KEY", "aalto")
    assert WebSettings().instance_key == "aalto"
    aalto_sisu = UNIVERSITIES["aalto"].sisu
    assert aalto_sisu is not None
    assert str(KoriSettings().kori_base_url) == aalto_sisu.kori_base_url
    assert KoriSettings().university_id == aalto_sisu.university_id
    assert KoriSettings().kori_first_academic_year == aalto_sisu.first_academic_year
    assert str(NormalizationSettings().course_guide_base_url) == (aalto_sisu.course_guide_base_url)

    # A Peppi legacy university reads its own first year and guide base, and its missing Sisu block
    # refuses the Kori read.
    monkeypatch.setenv("INSTANCE_KEY", "turku")
    assert WebSettings().instance_key == "turku"
    turku_legacy = UNIVERSITIES["turku"].peppi_legacy
    assert turku_legacy is not None
    assert KoriSettings().first_academic_year_start == turku_legacy.periods[0].start_year
    assert str(NormalizationSettings().course_guide_base_url) == (f"{turku_legacy.guide_base_url}/")
    with pytest.raises(ValueError, match="has no Sisu source block"):
        KoriSettings().kori_base_url

    # A guide university reads its own first year, guide base and fetch variant.
    monkeypatch.setenv("INSTANCE_KEY", "diak")
    assert WebSettings().instance_key == "diak"
    diak_guide = UNIVERSITIES["diak"].guide
    assert diak_guide is not None
    assert KoriSettings().first_academic_year_start == diak_guide.periods[0].start_year
    assert str(NormalizationSettings().course_guide_base_url) == (f"{diak_guide.guide_base_url}/")
    assert diak_guide.fetch_variant == "plain_http"
    monkeypatch.delenv("INSTANCE_KEY")

    # A candidate configuration supplies its own WebSettings port in front of both files and in
    # front of the process override the handoff marker would otherwise admit.
    web_toml = (root / "config" / "web.toml").read_text(encoding="utf-8")
    candidate = web_toml.replace("WEB_PORT = 8037", "WEB_PORT = 18099")
    documents = configuration.read_configuration_documents(
        root, replacements={"config/web.toml": candidate}
    )
    with configuration.configuration_candidate(documents=documents, local={}):
        assert WebSettings().web_port == 18099
    # Leaving the candidate restores the ordinary source precedence.
    assert WebSettings().web_port == 18028

    # A candidate value the owning validator rejects fails despite the valid process override.
    invalid = web_toml.replace("WEB_PORT = 8037", "WEB_PORT = 70000")
    with pytest.raises(ValidationError):
        with configuration.configuration_candidate(
            documents=configuration.read_configuration_documents(
                root, replacements={"config/web.toml": invalid}
            ),
            local={},
        ):
            WebSettings()


def test_public_configuration_rejects_duplicate_alias(tmp_path: Path) -> None:
    """A duplicate top-level alias across two subsystem files fails before any settings succeed."""
    root = tmp_path / "checkout"
    write_example_config_tree(root)
    for relative, value in (("database.toml", "first"), ("curriculum.toml", "second")):
        path = root / "config" / relative
        path.write_text(
            f'DUPLICATE_ALIAS = "{value}"\n' + path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    configuration.clear_configuration_caches()

    with pytest.raises(ValueError, match="DUPLICATE_ALIAS"):
        configuration.read_public_configuration(root)


def test_compose_bridge_carries_interpolation_aliases_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Compose command writes a Compose environment file in a temporary directory and removes
    that directory.
    """
    database = SimpleNamespace(
        postgres_db="synthetic-db",
        postgres_user="synthetic-user",
        postgres_password=SecretStr("syn'thetic-password"),
        postgres_port=5555,
        admin_state_root=Path("data/synthetic-admin"),
    )
    embedding = SimpleNamespace(
        embedding_server_image="synthetic-image@sha256:" + "a" * 64,
        embedding_runtime_revision="b" * 40,
        embedding_artifact_sha256="c" * 64,
        embedding_model="synthetic/model",
        embedding_host="127.0.0.1",
        embedding_port=8080,
        embedding_threads=2,
        embedding_context_size=2048,
        embedding_pooling="cls",
        embedding_normalization=2,
        embedding_artifact_path=Path("models/synthetic.gguf"),
    )
    instance = SimpleNamespace(instance_key="example")
    feedback = SimpleNamespace(feedback_postgres_password=SecretStr("synthetic-feedback-password"))
    monkeypatch.setattr(compose, "load_raw_database_settings", lambda: database)
    monkeypatch.setattr(compose, "load_embedding_runtime_settings", lambda: embedding)
    monkeypatch.setattr(compose, "load_instance_settings", lambda: instance)
    monkeypatch.setattr(compose, "load_feedback_settings", lambda: feedback)
    monkeypatch.setattr(administration, "repository_root", lambda: tmp_path)
    set_administrative_controller_environment(monkeypatch)
    monkeypatch.setenv("ADMIN_SHARED_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv("WORKER_HOST_KIND", "linux")
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/home/test-user/course-explorer")
    monkeypatch.delenv("WORKER_WSL_DISTRIBUTION", raising=False)
    monkeypatch.delenv(compose.COMPOSE_PROJECT_NAME_ALIAS, raising=False)
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    monkeypatch.setattr(compose, "DOCKER_SOCKET_PATH", socket_path)
    monkeypatch.delenv(compose.HOST_ROOT_ALIAS, raising=False)
    monkeypatch.delenv(compose.HOST_HOME_ALIAS, raising=False)
    monkeypatch.delenv(compose.SSH_DIRECTORY_ALIAS, raising=False)

    captures: list[dict[str, object]] = []

    def fake_run(command, check):
        bridge = Path(command[command.index("--env-file") + 1])
        captures.append(
            {
                "command": tuple(command),
                "content": bridge.read_text(encoding="utf-8"),
                "file_mode": bridge.stat().st_mode & 0o777,
                "directory": bridge.parent,
                "directory_mode": bridge.parent.stat().st_mode & 0o777,
            }
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    assert compose.main(["--", "config", "--quiet"]) == 0
    # The noninteractive flag before the final `--` is accepted and the call exits 0.
    assert compose.main(["--non-interactive", "--", "version"]) == 0
    # Docker Compose's own process-level project override is forwarded as an explicit argv option,
    # so one ephemeral namespace carries through sudo and the default
    # project name in docker-compose.yml is untouched.
    monkeypatch.setenv(compose.COMPOSE_PROJECT_NAME_ALIAS, "synthetic-proof-project")
    assert compose.main(["--", "version"]) == 0

    first, second, third = captures
    project_index = third["command"].index("--project-name")
    assert third["command"][project_index + 1] == "synthetic-proof-project"
    assert project_index < third["command"].index("--env-file")
    assert "COMPOSE_PROJECT_NAME" not in str(third["content"])

    def bridge_values(content: object) -> dict[str, str]:
        rendered: dict[str, str] = {}
        for line in str(content).splitlines():
            key, _, quoted = line.partition("=")
            rendered[key] = quoted
        return rendered

    values = bridge_values(first["content"])
    assert values["POSTGRES_PASSWORD"] == "'syn\\'thetic-password'"
    assert values["FEEDBACK_POSTGRES_PASSWORD"] == "'synthetic-feedback-password'"
    assert values["COURSE_EXPLORER_PROJECT_NAME"] == "'course-explorer-example'"
    assert values["EMBEDDING_ARTIFACT_PATH"] == (
        "'" + str(tmp_path / "shared" / "models" / "synthetic.gguf") + "'"
    )
    assert values["COURSE_EXPLORER_UID"] == f"'{os.getuid()}'"
    assert values["COURSE_EXPLORER_GID"] == f"'{os.getgid()}'"
    assert values["COURSE_EXPLORER_DOCKER_GID"] == f"'{socket_path.stat().st_gid}'"
    assert values["COURSE_EXPLORER_HOST_ROOT"] == f"'{tmp_path}'"
    assert values["COURSE_EXPLORER_HOST_HOME"] == f"'{Path.home()}'"
    assert values["COURSE_EXPLORER_SSH_DIR"] == f"'{Path.home() / '.ssh'}'"
    assert values["ADMIN_SHARED_ROOT"] == f"'{tmp_path / 'shared'}'"
    assert values["ADMIN_STATE_ROOT"] == "'data/synthetic-admin'"
    assert first["file_mode"] == 0o600
    assert first["directory_mode"] == 0o700
    assert first["directory"] is not second["directory"]
    assert not first["directory"].exists() and not second["directory"].exists()

    # The host home alias supplies COURSE_EXPLORER_HOST_HOME independently of the SSH directory.
    host_home = tmp_path / "host-home"
    monkeypatch.setenv(compose.HOST_HOME_ALIAS, str(host_home))
    assert compose.main(["--", "config", "--quiet"]) == 0
    home_values = bridge_values(captures[3]["content"])
    assert home_values["COURSE_EXPLORER_HOST_HOME"] == f"'{host_home}'"
    assert home_values["COURSE_EXPLORER_SSH_DIR"] == f"'{Path.home() / '.ssh'}'"

    # A remote worker host runs the stack without the worker profile or its generated override.
    assert "--profile" not in first["command"]
    assert first["command"].count("--file") == 1

    # A same-machine host adds both the profile and the generated override to every command.
    monkeypatch.setenv("WORKER_HOST_KIND", "same-machine")
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/app")
    monkeypatch.delenv("WORKER_SSH_TARGET", raising=False)
    assert compose.main(["--", "config", "--quiet"]) == 0
    same_machine = captures[4]["command"]
    assert str(tmp_path / "docker-compose.yml") in same_machine
    assert str(tmp_path / "docker-compose.worker.yml") in same_machine
    assert same_machine[same_machine.index("--profile") + 1] == "worker"
    assert same_machine.index("--profile") < same_machine.index("config")


def test_unsourced_cli_loads_policy(tmp_path: Path) -> None:
    """The shell action exports the composed example non-secret values, and credential aliases do
    not appear in the exports.
    """
    root = tmp_path / "checkout"
    write_example_config_tree(root)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from course_explorer import administration, configuration; "
            "administration.repository_root = lambda: Path(sys.argv[1]); "
            "raise SystemExit(configuration.main(['shell']))",
            str(root),
        ],
        check=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")},
        text=True,
    )

    exported: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, _, quoted = line.removeprefix("export ").partition("=")
        exported[key] = shlex.split(quoted)[0]

    assert exported["POSTGRES_DB"] == "course_explorer"
    assert exported["EMBEDDING_PORT"] == "8081"
    assert exported["SEARCH_TIER_PRIORITY"] == '["exact_code","substring","fuzzy_name","semantic"]'
    assert exported["WORKER_GPU_REBOOT_ENABLED"] == "0"
    assert exported["WORKER_GPU_REBOOT_INTERVAL_MINUTES"] == "120"
    assert exported["EXAMPLE_POSTGRES_DB"] == "course_explorer"
    assert exported["WEB_APP_URL"] == "http://localhost:8037/"
    # The worker host kind and identity are non-secret subsystem values, so the shell exports them.
    assert exported["WORKER_HOST_KIND"] == "wsl"
    assert exported["WORKER_SSH_TARGET"] == "replace-with-ssh-target"
    assert exported["WORKER_WSL_DISTRIBUTION"] == "replace-with-wsl-distribution"
    assert exported["WORKER_PROJECT_ROOT"] == "/home/replace-with-user/course-explorer"
    assert exported["WORKER_GPU_BACKEND"] == "rocm"
    assert exported["WORKER_GPU_TARGET"] == "gfx1201"
    assert exported["WORKER_GPU_DEVICE_INDEX"] == "0"
    assert (
        not {
            "POSTGRES_PASSWORD",
            "FEEDBACK_POSTGRES_PASSWORD",
            "FEEDBACK_GITHUB_TOKEN",
            "UNRELATED_SECRET",
        }
        & exported.keys()
    )


def test_worker_host_kind_requires_its_own_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each host kind requires its own reach values and refuses another kind's."""
    set_administrative_controller_environment(monkeypatch)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/home/test-user/course-explorer")

    monkeypatch.setenv("WORKER_HOST_KIND", "wsl")
    monkeypatch.delenv("WORKER_WSL_DISTRIBUTION")
    monkeypatch.delenv("WORKER_SSH_TARGET")
    with pytest.raises(ValidationError):
        WorkerConnectionSettings()

    monkeypatch.setenv("WORKER_SSH_TARGET", "worker-host")
    with pytest.raises(ValidationError):
        WorkerConnectionSettings()

    monkeypatch.setenv("WORKER_WSL_DISTRIBUTION", "Ubuntu-24.04")
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/home/test-user/100%checkout")
    with pytest.raises(ValidationError):
        WorkerConnectionSettings()

    monkeypatch.setenv("WORKER_HOST_KIND", "linux")
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/home/test-user/course-explorer")
    with pytest.raises(ValidationError):
        WorkerConnectionSettings()

    monkeypatch.delenv("WORKER_WSL_DISTRIBUTION")
    loaded = WorkerConnectionSettings()
    assert loaded.host_kind == "linux"
    assert loaded.wsl_distribution is None
    assert loaded.ssh_target == "worker-host"

    # A same-machine worker refuses the SSH target it never uses, and needs none.
    monkeypatch.setenv("WORKER_HOST_KIND", "same-machine")
    with pytest.raises(ValidationError):
        WorkerConnectionSettings()

    monkeypatch.delenv("WORKER_SSH_TARGET")
    monkeypatch.setenv("WORKER_PROJECT_ROOT", "/app")
    same_machine = WorkerConnectionSettings()
    assert same_machine.host_kind == "same-machine"
    assert same_machine.ssh_target is None
    assert same_machine.worker_project_root == Path("/app")

    # A Linux worker requires its SSH target.
    monkeypatch.setenv("WORKER_HOST_KIND", "linux")
    with pytest.raises(ValidationError):
        WorkerConnectionSettings()


def test_gpu_backend_requires_its_own_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ROCm worker needs a gfx target and a CUDA worker a compute capability."""
    set_administrative_controller_environment(monkeypatch)
    monkeypatch.setenv("WORKER_GPU_DEVICE_INDEX", "0")

    monkeypatch.setenv("WORKER_GPU_BACKEND", "rocm")
    monkeypatch.setenv("WORKER_GPU_TARGET", "86")
    with pytest.raises(ValidationError):
        WorkerGpuSettings()

    monkeypatch.setenv("WORKER_GPU_BACKEND", "cuda")
    monkeypatch.setenv("WORKER_GPU_TARGET", "gfx1201")
    with pytest.raises(ValidationError):
        WorkerGpuSettings()

    monkeypatch.setenv("WORKER_GPU_BACKEND", "rocm")
    monkeypatch.setenv("WORKER_GPU_TARGET", "gfx1201")
    rocm = WorkerGpuSettings()
    assert rocm.gpu_backend == "rocm"
    assert rocm.gpu_target == "gfx1201"

    monkeypatch.setenv("WORKER_GPU_BACKEND", "cuda")
    monkeypatch.setenv("WORKER_GPU_TARGET", "120")
    cuda = WorkerGpuSettings()
    assert cuda.gpu_backend == "cuda"
    assert cuda.gpu_target == "120"
