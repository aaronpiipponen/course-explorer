"""One deployment of an admitted checkout to this server's containers.

The job that calls this owner admits the checkout at the tested commit before the deployment body
runs. The sequence refuses while a stage is busy, builds and starts the database and embedding
services, enrolls and migrates the served dataset and builds its artifacts, builds and starts the
site and administration containers, a sender that already runs and a same-machine worker, proves
both HTML shells and prunes the superseded artifact builds. The first failure stops the sequence
and propagates; the previous containers and images stay as they are where a step did not change
them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from course_explorer import administration
from course_explorer.config import load_administrative_dataset_settings
from course_explorer_admin import commands, datasets, mechanics, self_host


def deploy_containers(*, runner: mechanics.ProcessRunner, write: Callable[[str], None]) -> None:
    """Deploy this checkout's containers in the fixed order and stop at the first failure.

    A sender that already runs is rebuilt and started with the site and the administration
    services, and one that does not run is left absent. A same-machine worker's image is built from
    this checkout, so the deployment builds and recreates its container once, with the site and
    administration services. The owner takes no commit argument, because the caller's admission
    binds the checkout to the tested commit.
    """
    root = administration.repository_root()
    timeout = load_administrative_dataset_settings().database_operation_timeout_seconds
    base_services = ("db", "embedding")

    write("check that every stage is idle")
    datasets.require_stages_idle()
    write("build the database and embedding images")
    runner.run_interactive(
        (*mechanics.compose_prefix(interpreter=True), "build", *base_services),
        cwd=root,
        timeout=None,
    )
    write("start the database and embedding services")
    runner.run(
        (
            *mechanics.compose_prefix(interpreter=True),
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--no-build",
            *base_services,
        ),
        cwd=root,
        timeout=timeout,
    )

    write("enroll the served dataset")
    enrollment = commands.run_administrative_dataset_ensure(runner=runner)
    if enrollment.mode == "enrolled":
        write("migrate the selected dataset and build its serving artifacts")
        commands.run_administrative_migration(role="selected", runner=runner)
        commands.run_administrative_serving_artifacts()
    else:
        write("migrate the configured database and build the example artifacts")
        commands.run_administrative_database_migration(runner=runner)
        runner.run(
            (sys.executable, "scripts/mock_data.py", "write-artifacts"),
            cwd=root,
            timeout=None,
        )

    running = mechanics.running_compose_services(
        lambda argv: runner.run(tuple(argv), cwd=root).stdout, interpreter=True
    )
    services = self_host.site_services(sender="sender" in running)
    write(f"build {', '.join(services)} images")
    runner.run_interactive(
        (*mechanics.compose_prefix(interpreter=True), "build", *services),
        cwd=root,
        timeout=None,
    )
    write(f"start {', '.join(services)} services")
    runner.run(
        (
            *mechanics.compose_prefix(interpreter=True),
            "up",
            "-d",
            "--wait",
            "--force-recreate",
            "--no-build",
            *services,
        ),
        cwd=root,
        timeout=timeout,
    )

    write("prove both HTML shells")
    self_host.prove_shells()
    write("prune the superseded artifact builds")
    commands.run_administrative_serving_artifacts(prune=True)


__all__ = ["deploy_containers"]
