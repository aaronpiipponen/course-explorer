"""The fixed short rebuilds of this checkout and the queued re-create of its administration service.

This module owns the operator's rebuilds. ``rebuild_site`` builds the site image from the checkout,
re-creates the database and embedding containers from their prepared images, re-creates the site
container and proves the configured loopback site answers with its HTML shell. ``recreate_web`` is
the one site re-create owner shared by the setup, the rebuild and the dataset promotion.
``rebuild_admin`` builds the administration image alone, and ``restart_admin`` starts a detached
one-off that re-creates this checkout's own administration service. The steps run in one fixed
order, each bounded by the configured stop timeout except the two image builds, which are not, and
the first failure stops the command without a further step, a rollback, a retry or a success report.
``recreate_admin_queued`` returns once the one-off container is started, so the caller is not the
process being replaced. ``rebuild_site`` holds the checkout reservation for its whole body, so it
refuses while a preparation stage or a Working operation holds it. The administration build and the
re-create take no reservation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from course_explorer import administration
from course_explorer.config import load_local_lifecycle_settings, load_web_settings
from course_explorer_admin import machine_locks, mechanics

# The exact ordered steps a completed rebuild names, as its transient acknowledgment.
REBUILD_STEPS: tuple[str, ...] = ("image", "containers", "web", "bind")

# The one step the administration build names: its own image.
ADMIN_REBUILD_STEPS: tuple[str, ...] = ("image",)

# The two services a rebuild re-creates, in order. The volumes and images stay in place.
REBUILD_SERVICES: tuple[str, ...] = ("db", "embedding")


class RebuildReport(BaseModel):
    """One completed rebuild: its steps, and the configured web bind it verified."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    steps: tuple[str, ...]
    host: str
    port: int


class AdminRebuildReport(BaseModel):
    """One completed administration build: the ordered steps it ran.

    The report holds the steps. The build writes the administration image, and a separate
    ``restart_admin`` invocation starts the re-create of the administration service.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    steps: tuple[str, ...]


class AdminRestartReport(BaseModel):
    """One acknowledged administration re-create request.

    ``restart_queued`` is always true, because the report is returned only after the detached
    one-off container is started.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    restart_queued: Literal[True] = True


def recreate_web(*, runner: mechanics.ProcessRunner) -> None:
    """Re-create the site container from its prepared image and wait for it to answer.

    The service is re-created without a build or a pull, so the command reuses the image the site
    build wrote, and the wait returns only once the container is healthy. It is bounded by the
    configured stop timeout.
    """
    root = administration.repository_root()
    runner.run(
        (
            *mechanics.compose_prefix(non_interactive=True, interpreter=True),
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--no-build",
            "web",
        ),
        cwd=root,
        timeout=load_local_lifecycle_settings().stop_timeout_seconds,
    )


def recreate_admin_queued(*, runner: mechanics.ProcessRunner) -> None:
    """Start a detached one-off that re-creates the administration service.

    The one-off container performs the re-create of the administration service, so it survives the
    replacement it performs. It is bounded by the configured stop timeout.
    """
    root = administration.repository_root()
    runner.run(
        (
            *mechanics.compose_prefix(non_interactive=True, interpreter=True),
            "run",
            "--rm",
            "-d",
            "--no-deps",
            "admin",
            "python",
            "-m",
            "course_explorer_admin.compose",
            "--non-interactive",
            "--",
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--no-build",
            "admin",
        ),
        cwd=root,
        timeout=load_local_lifecycle_settings().stop_timeout_seconds,
    )


def rebuild_site(*, runner: mechanics.ProcessRunner) -> RebuildReport:
    """Build the site image, re-create the containers, re-create the site and prove its bind.

    The checkout reservation is held for the whole rebuild, so a preparation stage or a Working
    operation refuses before any image, container or serving change. Every step is checked. The
    image build runs from the checkout and is not bounded, the database and embedding containers
    are re-created from their prepared images without a build or a download, the site container is
    re-created through the shared owner, and the site must answer on the configured loopback bind.
    """
    with machine_locks.checkout_reservation("rebuild_site"):
        root = administration.repository_root()
        web = load_web_settings()
        timeout = load_local_lifecycle_settings().stop_timeout_seconds

        runner.run_interactive(
            (*mechanics.compose_prefix(interpreter=True), "build", "web"),
            cwd=root,
            timeout=None,
        )
        runner.run(
            (
                *mechanics.compose_prefix(non_interactive=True, interpreter=True),
                "up",
                "-d",
                "--wait",
                "--force-recreate",
                "--no-build",
                *REBUILD_SERVICES,
            ),
            cwd=root,
            timeout=timeout,
        )
        recreate_web(runner=runner)
        # The self-host module imports this one, so the proof owner is resolved at call time.
        from course_explorer_admin import self_host

        self_host.prove_web_shell()
        return RebuildReport(steps=REBUILD_STEPS, host=web.web_host, port=web.web_port)


def rebuild_admin(*, runner: mechanics.ProcessRunner) -> AdminRebuildReport:
    """Build the administration image of this checkout.

    The command builds the administration target from the checkout and is not bounded. A build
    failure propagates, and a separate ``restart_admin`` invocation starts the re-create.
    """
    root = administration.repository_root()

    runner.run_interactive(
        (*mechanics.compose_prefix(interpreter=True), "build", "admin"),
        cwd=root,
        timeout=None,
    )
    return AdminRebuildReport(steps=ADMIN_REBUILD_STEPS)


def restart_admin(*, runner: mechanics.ProcessRunner) -> AdminRestartReport:
    """Start the detached one-off that re-creates this checkout's administration service.

    The report acknowledges the started one-off alone, so the console answers again once the
    re-create completes.
    """
    recreate_admin_queued(runner=runner)
    return AdminRestartReport()
