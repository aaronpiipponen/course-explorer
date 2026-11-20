"""Contracts for the unified administrative commands and the engine dispatcher.

The tests load ``course_explorer_admin.cli`` and drive its parser, dispatch and safe failures
through ``main``.
"""

from __future__ import annotations

import getpass
import importlib.util
import io
import json
import sys
import warnings
from pathlib import Path
from uuid import UUID

import pytest

from course_explorer_admin import commands
from course_explorer_admin.provisioning import ProvisioningReport, WorkerCodeReport
from course_explorer_admin.provisioning_worker import WorkerResourcesReport

_CLI_SPEC = importlib.util.spec_from_file_location(
    "admin_cli", Path(__file__).parents[2] / "src" / "course_explorer_admin" / "cli.py"
)
assert _CLI_SPEC is not None and _CLI_SPEC.loader is not None
_CLI_MODULE = importlib.util.module_from_spec(_CLI_SPEC)
_CLI_SPEC.loader.exec_module(_CLI_MODULE)


_FEEDBACK_ID = "3f1c2a64-5b7d-4e29-8c10-6a9d2b4e7f01"
_ATTACHMENT_ID = "8e2d4b17-9c63-4a52-b0f8-1d7c3e5a9b24"


class _Sentinel:
    """One JSON-renderable owner result standing in for a real DTO."""

    def __init__(self, label: str) -> None:
        self.label = label

    def model_dump_json(self) -> str:
        return json.dumps({"label": self.label})


@pytest.mark.parametrize(
    "case",
    [
        "setup-preflight",
        "preflight-not-ready",
        "report-safe-failure",
        "config-safe-set-failure",
        "password-delegate",
        "password-mismatch",
        "password-hidden-input-failure",
        "password-safe-failure",
        "feedback-list-delegate",
        "feedback-attachment-delegate",
        "feedback-delete-delegate",
        "feedback-safe-failure",
        "dispose-nothing",
        "self-host-example",
        "self-host-real-database",
        "self-host-publish",
        "self-host-unpublish",
        "deploy",
    ],
)
def test_operator_commands_report_status_refuse_bad_input_and_map_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """Public input refusal, preflight status, hidden password entry, safe failures and ``nothing``.

    Each parametrized invocation drives one coherent public adapter behavior. Invalid dataset setup
    combinations refuse at the public command entry, a not-ready preflight exits 1 with its report,
    a failed report or configuration save prints only its class token, the password command reads
    hidden input, the publication verbs and the deploy command each delegate once, and
    ``dispose inference ... nothing`` maps to no disposition.
    """
    if case == "setup-preflight":
        updated = ProvisioningReport(
            code=WorkerCodeReport(branch="main", revision="a" * 40, server_origin_differs=False),
            resources=None,
        )
        preflight = _Sentinel("worker")
        enrollment = _Sentinel("enrollment")
        from course_explorer_admin import datasets as datasets_module

        monkeypatch.setattr(
            commands,
            "update_worker_code",
            lambda *, branch, runner: updated.code,
        )
        monkeypatch.setattr(commands, "preflight_worker", lambda *, runner: preflight)
        monkeypatch.setattr(
            datasets_module,
            "initialize_selected_dataset",
            lambda *, runner: enrollment,
        )

        # The worker choice updates the code and prepares no resource.
        assert commands.run_administrative_setup("worker", branch="feature") == updated
        # Invalid dataset setup combinations refuse at the public command entry.
        with pytest.raises(ValueError):
            commands.run_administrative_setup("datasets", branch="feature")
        return

    if case == "preflight-not-ready":
        # A not-ready preflight prints its report and exits 1.
        not_ready = ProvisioningReport(
            code=WorkerCodeReport(branch="main", revision="a" * 40, server_origin_differs=True),
            resources=WorkerResourcesReport(ready=False, missing=["dependencies"]),
        )
        monkeypatch.setattr(_CLI_MODULE, "run_administrative_preflight", lambda: not_ready)
        assert _CLI_MODULE.main(["preflight"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.out)["resources"]["ready"] is False
        assert captured.err.strip()
        return

    if case == "report-safe-failure":
        # One synthetic unsafe exception produces only its class token, never the payload.

        def unsafe(kind, *, role):
            raise RuntimeError("secret-payload-value")

        monkeypatch.setattr(_CLI_MODULE, "run_administrative_coverage", unsafe)
        assert _CLI_MODULE.main(["coverage", "embedding", "--role", "working"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err.splitlines()[0]) == {"error": "RuntimeError"}
        assert "secret-payload-value" not in captured.err
        return

    if case == "config-safe-set-failure":
        # A failed configuration save prints only its class token, the guidance and no value.
        def failing_set(file, key, value, *, settings_families=()):
            raise RuntimeError("configuration-payload-value")

        monkeypatch.setattr(_CLI_MODULE, "run_administrative_config_set", failing_set)
        assert _CLI_MODULE.main(["config", "set", "config/web.toml", "WEB_PORT", "18026"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err.splitlines()[0]) == {"error": "RuntimeError"}
        assert "configuration-payload-value" not in captured.err
        return

    if case in {"password-delegate", "password-mismatch", "password-hidden-input-failure"}:
        # The password command reads twice through the supplied hidden reader and delegates once.
        report = commands.PasswordSetReport()
        delegated: list[dict] = []
        monkeypatch.setattr(
            commands,
            "set_admin_password",
            lambda *, username, password: (
                delegated.append({"username": username, "password": password}) or report
            ),
        )
        if case == "password-hidden-input-failure":
            # An entry the terminal cannot hide is an error rather than an echoed fallback, and
            # the one shared hidden reader owns that refusal for both password callers.
            def warning_reader(prompt: str) -> str:
                warnings.warn("input will be echoed", getpass.GetPassWarning)
                return "synthetic-entry"

            with pytest.raises(getpass.GetPassWarning):
                commands.run_administrative_password(
                    username="synthetic-operator", read_password=warning_reader
                )
            with pytest.raises(getpass.GetPassWarning):
                commands._read_hidden_secret("Prompt: ", read_password=warning_reader)
            assert (
                commands._read_hidden_secret(
                    "Prompt: ", read_password=lambda prompt: "shared-entry"
                )
                == "shared-entry"
            )
            assert delegated == []
            return
        answers = ["first-entry", "first-entry"]
        if case == "password-mismatch":
            answers = ["first-entry", "other-entry"]

        def reader(_prompt: str) -> str:
            return answers.pop(0)

        if case == "password-mismatch":
            with pytest.raises(ValueError):
                commands.run_administrative_password(
                    username="synthetic-operator", read_password=reader
                )
            assert delegated == []
            return
        assert (
            commands.run_administrative_password(
                username="synthetic-operator", read_password=reader
            )
            == report
        )
        assert len(delegated) == 1
        assert delegated[0]["username"] == "synthetic-operator"
        assert delegated[0]["password"].get_secret_value() == "first-entry"
        assert "first-entry" not in report.model_dump_json()
        return

    if case == "password-safe-failure":
        # A failed password command prints only its class token and the hidden-entry guidance.

        def failing_password(*, username):
            raise RuntimeError("password-payload-value")

        monkeypatch.setattr(_CLI_MODULE, "run_administrative_password", failing_password)
        assert _CLI_MODULE.main(["password", "--username", "synthetic-operator"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err.splitlines()[0]) == {"error": "RuntimeError"}
        assert "password-payload-value" not in captured.err
        return

    if case in {
        "feedback-list-delegate",
        "feedback-attachment-delegate",
        "feedback-delete-delegate",
    }:
        # Each feedback command parses its transport input and delegates once to its own owner.
        report = _Sentinel(case)
        if case == "feedback-list-delegate":
            list_calls: list[dict] = []
            monkeypatch.setattr(
                _CLI_MODULE,
                "run_administrative_feedback_list",
                lambda **kwargs: list_calls.append(kwargs) or report,
            )
            assert (
                _CLI_MODULE.main(
                    ["feedback", "list", "--query=needle", "--filters", '{"role": "student"}']
                )
                == 0
            )
            assert list_calls == [{"query": "needle", "filters": {"role": "student"}}]
        elif case == "feedback-attachment-delegate":
            attachment_calls: list[tuple[UUID, UUID]] = []
            monkeypatch.setattr(
                _CLI_MODULE,
                "run_administrative_feedback_attachment",
                lambda feedback_id, attachment_id: (
                    attachment_calls.append((feedback_id, attachment_id)) or report
                ),
            )
            assert _CLI_MODULE.main(["feedback", "attachment", _FEEDBACK_ID, _ATTACHMENT_ID]) == 0
            assert attachment_calls == [(UUID(_FEEDBACK_ID), UUID(_ATTACHMENT_ID))]
        else:
            delete_calls: list[UUID] = []
            monkeypatch.setattr(
                _CLI_MODULE,
                "run_administrative_feedback_delete",
                lambda feedback_id: delete_calls.append(feedback_id) or report,
            )
            assert _CLI_MODULE.main(["feedback", "delete", _FEEDBACK_ID]) == 0
            assert delete_calls == [UUID(_FEEDBACK_ID)]
        captured = capsys.readouterr()
        assert json.loads(captured.out)["label"] == case
        return

    if case == "feedback-safe-failure":
        # A failed feedback command prints only its class token and the feedback guidance.
        def failing_feedback(feedback_id):
            raise RuntimeError("feedback-payload-value")

        monkeypatch.setattr(_CLI_MODULE, "run_administrative_feedback_delete", failing_feedback)
        assert _CLI_MODULE.main(["feedback", "delete", _FEEDBACK_ID]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err.splitlines()[0]) == {"error": "RuntimeError"}
        assert "feedback-payload-value" not in captured.err
        assert "feedback definition" in captured.err
        # A malformed filter transport is a safe error too, and its text is not echoed.
        assert _CLI_MODULE.main(["feedback", "list", "--filters", "not-json"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err.splitlines()[0]) == {"error": "FeedbackValidationError"}
        assert "not-json" not in captured.err
        return

    if case in {"self-host-example", "self-host-real-database"}:
        # A terminal standard input reaches a workflow's own prompts, and a piped standard input
        # reads one JSON object. Either path delegates once, prints the report and writes fixed
        # progress to the error stream, and neither secret reaches the report or the progress.
        called: list[object] = []
        report = _Sentinel(case)

        def example_delegate(options, *, write):
            called.append(options)
            write("progress-step")
            return report

        def real_delegate(options, *, write):
            called.append(options)
            write("progress-step")
            return report

        if case == "self-host-example":

            class _Tty:
                def isatty(self) -> bool:
                    return True

            entered: list[object] = []

            def interactive_entry(*, read, write):
                entered.append(read)
                write("progress-step")
                return report

            monkeypatch.setattr(_CLI_MODULE, "run_example_setup_interactive", interactive_entry)
            monkeypatch.setattr(sys, "stdin", _Tty())
            assert _CLI_MODULE.main(["self-host", "example"]) == 0
            assert len(entered) == 1
            assert entered[0] is input
            captured = capsys.readouterr()
            assert json.loads(captured.out)["label"] == case
            assert "progress-step" in captured.err

            monkeypatch.setattr(_CLI_MODULE, "run_administrative_example_setup", example_delegate)
            payload = {
                "serve_mode": "local",
                "instance_key": "example",
                "web_port": 18037,
                "admin_web_port": 18039,
                "postgres_port": 15433,
                "embedding_port": 18081,
                "username": "synthetic-operator",
                "password": "synthetic-password",
                "publication_token": "synthetic-token",
                "confirm_download": True,
            }
            called.clear()
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
            assert _CLI_MODULE.main(["self-host", "example"]) == 0
            assert len(called) == 1
            options = called[0]
            assert isinstance(options, commands.ExampleSetupInput)
            assert options.password.get_secret_value() == "synthetic-password"
            assert options.publication_token is not None
            assert "password" not in options.model_dump()
            assert "publication_token" not in options.model_dump()
            assert "synthetic-password" not in repr(options)
            captured = capsys.readouterr()
            assert json.loads(captured.out)["label"] == case
            assert "progress-step" in captured.err
            assert "synthetic-password" not in captured.err
            assert "synthetic-token" not in captured.err

            # A workflow failure at a terminal input reaches the handler, which returns 1.
            def failing_interactive(*, read, write):
                raise RuntimeError("terminal-payload-value")

            monkeypatch.setattr(_CLI_MODULE, "run_example_setup_interactive", failing_interactive)
            monkeypatch.setattr(sys, "stdin", _Tty())
            assert _CLI_MODULE.main(["self-host", "example"]) == 1
            captured = capsys.readouterr()
            assert json.loads(captured.err.splitlines()[0]) == {"error": "RuntimeError"}
            assert "terminal-payload-value" not in captured.err
            assert "check the container host" in captured.err
            return

        monkeypatch.setattr(_CLI_MODULE, "run_administrative_real_database_setup", real_delegate)
        payload = {"database_name": "course_explorer_real"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert _CLI_MODULE.main(["self-host", "real-database"]) == 0
        assert len(called) == 1
        assert isinstance(called[0], commands.RealDatabaseSetupInput)
        assert called[0].database_name == "course_explorer_real"
        captured = capsys.readouterr()
        assert json.loads(captured.out)["label"] == case
        assert "progress-step" in captured.err

        # A safe owner failure prints only the class token and the workflow guidance.
        def failing_real(options, *, write):
            raise RuntimeError("self-host-payload-value")

        monkeypatch.setattr(_CLI_MODULE, "run_administrative_real_database_setup", failing_real)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert _CLI_MODULE.main(["self-host", "real-database"]) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err.splitlines()[0]) == {"error": "RuntimeError"}
        assert "self-host-payload-value" not in captured.err
        assert "example workflow first" in captured.err
        return

    if case in {"self-host-publish", "self-host-unpublish"}:
        # A publication verb delegates once, prints its report and writes its fixed step name.
        report = _Sentinel(case)

        def publication(*, write):
            write("publication-step")
            return report

        delegate = (
            "run_administrative_publish"
            if case == "self-host-publish"
            else "run_administrative_unpublish"
        )
        monkeypatch.setattr(_CLI_MODULE, delegate, publication)
        verb = "publish" if case == "self-host-publish" else "unpublish"
        assert _CLI_MODULE.main(["self-host", verb]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["label"] == case
        assert "publication-step" in captured.err
        return

    if case == "deploy":
        # The deploy command runs the deployment owner and writes its fixed step names.
        def containers(*, runner, write):
            write("deploy-step")

        monkeypatch.setattr(_CLI_MODULE.deploy, "deploy_containers", containers)
        assert _CLI_MODULE.main(["deploy"]) == 0
        captured = capsys.readouterr()
        assert "deploy-step" in captured.err
        return

    # The explicit `dispose inference ... nothing` machine command maps to no disposition.
    disposition_calls: list[tuple[str, object]] = []
    disposition_report = _Sentinel("disposition")
    monkeypatch.setattr(
        _CLI_MODULE,
        "set_inference_target_disposition",
        lambda target, choice: disposition_calls.append((target, choice)) or disposition_report,
    )
    assert _CLI_MODULE.main(["dispose", "inference", "disp-target", "nothing"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["label"] == "disposition"
    assert disposition_calls == [("disp-target", None)]


def test_progress_prefixes_each_line_when_a_transcript_marker_is_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A configured prefix reaches every deploy progress line on the error stream."""
    monkeypatch.setenv("COURSE_EXPLORER_PROGRESS_PREFIX", "::notice::")
    _CLI_MODULE._progress("deploy-step")
    assert capsys.readouterr().err == "::notice::deploy-step\n"
