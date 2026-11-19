"""Translation and inference remote stage adapters over the shared lifecycle core.

This module owns the one server-side remote algorithm the translation and inference stages share.
Each adapter extends the shared five-action lifecycle core with a worker bring-up (keepalive, owned
Compose ``db``, remote session directory, the three uploads and the prepared acknowledgement), a
detached worker-run launch, a progress poll that forwards no control, a foreground release hook for
pause and a foreground finalize hook for stop. The whole worker database is returned, verified and
published as Working through the dataset owner, then dropped only after the server receipt exists.
Stage DTO logic lives in the two policy/status seams, and every transport construction goes through
``_transport``. Each adapter takes the checkout reservation on this machine and the worker
reservation on the worker. A same-machine worker first starts the instance's own worker service, then
reuses the canonical session directory and the instance's database service instead of creating a
copy or owning a database service. The module also owns the administration backend's
startup resume, which relaunches the same-machine stage a host reboot interrupted.
"""

from __future__ import annotations

import os
import shlex
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from course_explorer import administration
from course_explorer.administration import (
    administrative_json,
    atomic_write_private_text,
    utc_now,
)
from course_explorer.config import (
    WorkerConnectionSettings,
    load_administrative_dataset_settings,
    load_inference_controller_settings,
    load_instance_settings,
    load_translation_controller_settings,
    load_worker_connection_settings,
    load_worker_gpu_reboot_settings,
)
from course_explorer_admin import machine_locks, mechanics, reboot, remote
from course_explorer_admin.datasets import admin_state_root
from course_explorer_admin.inference.controller import (
    InferenceControllerPolicy,
    InferenceControllerStatus,
    inference_configuration_sha256,
    load_inference_dataset_session,
)
from course_explorer_admin.lifecycle import (
    InferenceOptions,
    InferenceProgress,
    LifecycleContext,
    LifecycleStatus,
    StageName,
    StageOptions,
    TranslationOptions,
    TranslationProgress,
    WorkerResources,
    read_lifecycle_state,
    run_lifecycle_command,
    stage_state_root,
)
from course_explorer_admin.local_stages import (
    require_working_database_schema,
    reserve_local_stage,
    stage_identity_sha256,
    unreserve_local_stage,
)
from course_explorer_admin.remote import (
    WorkerControllerStatus,
    WorkerDatasetSession,
    remote_worker_session_directory,
    worker_dataset_session_directory,
)
from course_explorer_admin.translation.controller import (
    TranslationControllerPolicy,
    TranslationControllerStatus,
    load_translation_dataset_session,
    translation_configuration_sha256,
)
from course_explorer_admin.transport import (
    WorkerAvailabilityError,
    WorkerConnectionLost,
    WorkerRecovery,
    WorkerRecoveryCancelled,
    WorkerTransport,
    build_worker_transport,
    require_same_machine_container,
)

_STAGE = Literal["translation", "inference"]

# A prepared worker is re-armed before its run is launched. A running/completed worker needs no
# resume, and an imported/cleaned worker can only be stopped.
_RESUMABLE = {"prepared", "paused", "interrupted", "stopped", "failed"}

# Statuses reconciled for an ambiguous worker mutation acknowledgment. Resume accepts a session that
# a lost response already re-armed or finished. Pause accepts an unloaded or terminal session. Stop
# and finalize accept a terminal/imported/cleaned result. Cleanup accepts only cleaned.
_RESUME_ACCEPTED = {"starting", "running", "completed", "imported", "cleaned"}
_PAUSE_ACCEPTED = {"paused", "stopped", "completed", "failed", "imported", "cleaned"}
_STOP_ACCEPTED = {"stopped", "completed", "failed", "imported", "cleaned"}
_FINALIZE_ACCEPTED = {"imported", "cleaned"}
_CLEANUP_ACCEPTED = {"cleaned"}


def _admin_settings():
    return load_administrative_dataset_settings()


def _session_database(session_id: str) -> str:
    """Derive the configured canonical worker database name for one session id."""
    return _admin_settings().database_name(session_id, worker=True)


def _worker_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment overrides every worker helper needs to resolve one session."""
    reboot_settings = load_worker_gpu_reboot_settings()
    overrides = {
        "ADMIN_STATE_ROOT": str(_admin_settings().admin_state_root),
        "WORKER_GPU_REBOOT_ENABLED": (
            "true" if reboot_settings.worker_gpu_reboot_enabled else "false"
        ),
        "WORKER_GPU_REBOOT_INTERVAL_MINUTES": str(
            reboot_settings.worker_gpu_reboot_interval_minutes
        ),
    }
    if extra:
        overrides.update(extra)
    return overrides


def _session_environment(session_id: str) -> dict[str, str]:
    """Return the id-derived database override every canonical worker helper needs."""
    return {"POSTGRES_DB": _session_database(session_id)}


def _transport(
    runner: mechanics.ProcessRunner, connection: WorkerConnectionSettings
) -> WorkerTransport:
    """Construct the one worker transport the configured host kind selects."""
    return build_worker_transport(runner, connection)


def _ensure_same_machine_worker(
    runner: mechanics.ProcessRunner,
    connection: WorkerConnectionSettings,
    *,
    timeout: float,
) -> None:
    """Start the instance's worker container before the first same-machine transport call.

    The container-entry guard runs first, so a host process refuses before it issues any Compose
    command. The service is instance-owned and profile-gated, so a remote-worker host runs no Compose
    command here and the stage records no ownership of it.
    """
    require_same_machine_container(connection)
    if connection.host_kind != "same-machine":
        return
    mechanics.ensure_prepared_services(
        lambda argv: runner.run(argv, cwd=administration.repository_root(), timeout=timeout).stdout,
        ("worker",),
        interpreter=True,
    )


def _remote_helper(
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    argv: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
):
    """Run one worker helper from the configured project root with only the given overrides."""
    return transport.run(
        argv,
        cwd=PurePosixPath(str(connection.worker_project_root)),
        env=_worker_environment(env),
        timeout=connection.stop_timeout_seconds if timeout is None else timeout,
    )


def _worker_helper(
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    worker_module: str,
    argv: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
):
    """Run one worker helper through ``uv run --no-sync`` so nothing is installed or synced."""
    return _remote_helper(
        transport,
        connection,
        (
            "uv",
            "run",
            "--no-sync",
            "--offline",
            "--no-python-downloads",
            "python",
            "-m",
            worker_module,
            *argv,
        ),
        env=env,
        timeout=timeout,
    )


def _parse_status[StatusT: WorkerControllerStatus](
    status_type: type[StatusT], payload: bytes
) -> StatusT:
    return status_type.model_validate_json(payload.decode("utf-8"))


def _validate_status(status: WorkerControllerStatus, session: WorkerDatasetSession) -> None:
    if status.session_id != session.session_id:
        raise ValueError("worker status identity does not match the session")
    if status.course_limit != session.course_limit:
        raise ValueError("worker status course limit does not match the session")


def _ensure_keepalive(
    context: LifecycleContext,
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    resources: WorkerResources,
    *,
    recovery: WorkerRecovery,
    health_attempts: int,
    sleep: Callable[[float], None],
) -> WorkerResources:
    """Reuse or restart the one detached keepalive a WSL worker owns for this session.

    A native Linux worker owns no keepalive, so its recorded worker resources pass through
    unchanged. For a WSL worker, a live exact handle is a local no-op. A recorded but
    dead/previous-boot handle is cleared and its exact named remote orphan is terminated through
    the idempotent owner before one local replacement is started and recorded. Readiness
    re-observes the recorded handle, so a keepalive that dies during the wait is restarted. The
    health attempt count bounds successful not-ready reads.
    """
    if not transport.keepalive_required:
        return context.read_state().worker or resources
    session_id = context.session.session_id

    def observe(candidate: WorkerResources) -> bool:
        pid = candidate.keepalive_pid
        if pid is None or candidate.keepalive_boot_id != mechanics.current_boot_id():
            return False
        return transport.keepalive_is_alive(session_id, pid)

    def start(candidate: WorkerResources) -> WorkerResources:
        directory = worker_dataset_session_directory(session_id)
        if context.session.stage == "inference":
            sink = Path(os.devnull)
        else:
            sink = directory / "keepalive.log"
        pid = transport.start_keepalive(
            session_id,
            stdout_path=sink,
            stderr_path=sink,
        )
        updated = candidate.model_copy(
            update={"keepalive_pid": pid, "keepalive_boot_id": mechanics.current_boot_id()}
        )
        context.record_worker(updated)
        return updated

    candidate = context.read_state().worker or resources
    if observe(candidate):
        return candidate
    if candidate.keepalive_pid is not None:
        # A dead or foreign-boot record is not ownership: clear it without signalling and let the
        # exact session-named remote orphan be terminated by the idempotent owner.
        candidate = candidate.model_copy(update={"keepalive_pid": None, "keepalive_boot_id": None})
        context.record_worker(candidate)
    recovery.call(
        lambda: transport.terminate_remote_keepalive(
            session_id, timeout=_admin_settings().database_operation_timeout_seconds
        )
    )
    candidate = start(candidate)

    def read_ready() -> bool:
        current = context.read_state().worker or candidate
        if not observe(current):
            current = current.model_copy(update={"keepalive_pid": None, "keepalive_boot_id": None})
            context.record_worker(current)
            start(current)
            return False
        recovery.call(lambda: _remote_helper(transport, connection, ("printf", "ready")))
        return True

    mechanics.await_state(
        read_ready,
        lambda alive: alive,
        attempts=health_attempts,
        interval=connection.poll_interval_seconds,
        sleep=sleep,
    )
    return context.read_state().worker or candidate


def _stop_owned_worker_database(
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    resources: WorkerResources,
    *,
    timeout: float,
    recovery: WorkerRecovery,
) -> None:
    """Stop and remove the worker ``db`` service only when this session started or attempted it."""
    if not transport.owns_worker_database_service:
        return
    if not (resources.worker_database_started or resources.worker_database_start_attempted):
        return

    def run(argv: tuple[str, ...]) -> bytes:
        return recovery.call(
            lambda: _remote_helper(
                transport,
                connection,
                argv,
                timeout=timeout,
            )
        )

    prefix = mechanics.compose_prefix()
    recovery.call(
        lambda: _remote_helper(
            transport,
            connection,
            (*prefix, "stop", "db"),
            timeout=timeout,
        )
    )
    recovery.call(
        lambda: _remote_helper(
            transport,
            connection,
            (*prefix, "rm", "-f", "db"),
            timeout=timeout,
        )
    )
    if "db" in recovery.call(lambda: mechanics.running_compose_services(run)):
        raise RuntimeError("worker database service did not stop")


def _ensure_worker_database(
    context: LifecycleContext,
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    resources: WorkerResources,
    *,
    timeout: float,
    recovery: WorkerRecovery,
) -> WorkerResources:
    """Start the prepared worker Compose ``db`` service while recording only real ownership."""

    def run(argv: tuple[str, ...]) -> bytes:
        return recovery.call(
            lambda: _remote_helper(
                transport,
                connection,
                argv,
                timeout=timeout,
            )
        )

    running = recovery.call(lambda: mechanics.running_compose_services(run))
    if not transport.owns_worker_database_service:
        if "db" not in running:
            raise ValueError("the instance db service is not running")
        return resources
    owned = resources.worker_database_started or resources.worker_database_start_attempted
    if "db" in running:
        if not owned:
            raise ValueError(
                "worker database service is already running without recorded ownership"
            )
        return resources
    if not resources.worker_database_start_attempted:
        resources = resources.model_copy(update={"worker_database_start_attempted": True})
        context.record_worker(resources)
    started = recovery.call(lambda: mechanics.ensure_prepared_services(run, ("db",)))
    updated = resources.model_copy(
        update={"worker_database_started": (resources.worker_database_started or "db" in started)}
    )
    context.record_worker(updated)
    return updated


def _release_worker_resources(
    context: LifecycleContext,
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    resources: WorkerResources,
    *,
    recovery: WorkerRecovery,
    sleep: Callable[[float], None],
) -> WorkerResources:
    """Release only recorded owned worker resources without dropping or returning anything."""
    session_id = context.session.session_id
    _stop_owned_worker_database(
        transport,
        connection,
        resources,
        timeout=_admin_settings().database_operation_timeout_seconds,
        recovery=recovery,
    )
    if not transport.keepalive_required:
        return resources
    pid = resources.keepalive_pid
    if pid is not None and resources.keepalive_boot_id == mechanics.current_boot_id():
        transport.stop_keepalive(session_id, pid, sleep=sleep)
        if transport.keepalive_is_alive(session_id, pid):
            raise RuntimeError("owned keepalive process did not exit")
    # The local SSH client alone leaves the remote ``sleep infinity`` running, so the one owner
    # also kills the exact named remote process (a genuinely absent one is a no-op).
    recovery.call(
        lambda: transport.terminate_remote_keepalive(
            session_id, timeout=_admin_settings().database_operation_timeout_seconds
        )
    )
    updated = resources.model_copy(update={"keepalive_pid": None, "keepalive_boot_id": None})
    context.record_worker(updated)
    return updated


def _create_remote_session_directory(
    context: LifecycleContext,
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    resources: WorkerResources,
    *,
    remote_directory: PurePosixPath,
    recovery: WorkerRecovery,
) -> WorkerResources:
    """Create the exact remote session directory once, or verify the recorded one."""
    if transport.shares_worker_filesystem:
        # The server's canonical session directory and the worker path are one mounted directory,
        # which the capture creates. This branch verifies the directory and records no created bit,
        # because only the non-sharing branch reads that bit.
        script = f"set -euo pipefail; test -d {shlex.quote(str(remote_directory))}"
        recovery.call(lambda: _remote_helper(transport, connection, ("bash", "-c", script)))
        return resources
    if resources.remote_session_created:
        script = f"set -euo pipefail; test -d {shlex.quote(str(remote_directory))}"
        recovery.call(lambda: _remote_helper(transport, connection, ("bash", "-c", script)))
        return resources
    # Fresh creation is not replayed after an ambiguous started operation: a single attempt, so a
    # half-created directory fails loudly rather than being silently re-created.
    script = (
        "set -euo pipefail; "
        f"mkdir -m 0700 -p {shlex.quote(str(remote_directory.parent))}; "
        f"test ! -e {shlex.quote(str(remote_directory))}; "
        f"mkdir -m 0700 {shlex.quote(str(remote_directory))}"
    )
    _remote_helper(transport, connection, ("bash", "-c", script))
    updated = resources.model_copy(update={"remote_session_created": True})
    context.record_worker(updated)
    return updated


def _check_worker_dependencies(
    transport: WorkerTransport, connection: WorkerConnectionSettings
) -> None:
    """Validate the already-provisioned worker environment. Never installs or syncs."""
    _remote_helper(transport, connection, transport.dependency_check_arguments())


def _launch_worker_run(
    transport: WorkerTransport,
    connection: WorkerConnectionSettings,
    worker_module: str,
    session: WorkerDatasetSession,
) -> None:
    """Launch the detached canonical worker run helper with mode-0600 logs and the stop bound."""
    remote_session = remote_worker_session_directory(connection, session.session_id)
    transport.start_detached(
        (
            "uv",
            "run",
            "--no-sync",
            "--offline",
            "--no-python-downloads",
            "python",
            "-m",
            worker_module,
            "run",
            session.session_id,
        ),
        cwd=PurePosixPath(str(connection.worker_project_root)),
        env=_worker_environment(_session_environment(session.session_id)),
        output_path=remote_session / "runner.log",
        timeout=connection.stop_timeout_seconds,
    )


class RemoteStage:
    """One remote stage adapter over the shared lifecycle core.

    It owns only the translation/inference identity and policy seams plus the worker bring-up,
    launch, poll, release and finalize sequence. The shared core owns reservation, intent, the
    detached server child and the state/lock layout.
    """

    services: tuple[str, ...] = ("db",)
    temporary_services: tuple[str, ...] = ()
    retains_after_stop: bool = False

    def __init__(
        self,
        *,
        name: _STAGE,
        worker_module: str,
        status_type: type[WorkerControllerStatus],
        load_connection: Callable[[], WorkerConnectionSettings],
        startup_attempts: int,
        load_session: Callable[[str], tuple[WorkerDatasetSession, object, Path]],
        make_policy: Callable[[str], object],
        configuration_sha256: Callable[[], str],
        server_worker_module: str = "course_explorer_admin.worker",
    ) -> None:
        self.name = name
        self.worker_module = worker_module
        self.server_worker_module = server_worker_module
        self.status_type = status_type
        self._load_connection = load_connection
        self.startup_attempts = startup_attempts
        self._load_session = load_session
        self._make_policy = make_policy
        self._configuration_sha256 = configuration_sha256

    def connection(self) -> WorkerConnectionSettings:
        return self._load_connection()

    def select_options(self) -> StageOptions:
        if self.name == "translation":
            return TranslationOptions(kind="translation")
        return InferenceOptions(kind="inference")

    def identity_sha256(self, options: StageOptions) -> str:
        return stage_identity_sha256(self.name, self._configuration_sha256(), options)

    def initial_progress(self, options: StageOptions) -> TranslationProgress | InferenceProgress:
        if self.name == "translation":
            return TranslationProgress(kind="translation")
        return InferenceProgress(kind="inference")

    def require_prepared(self) -> None:
        """Worker readiness is checked during bring-up."""

    def require_database(self) -> None:
        require_working_database_schema()

    def reserve(self, session_id: str, *, runner: mechanics.ProcessRunner) -> None:
        """Take the local checkout reservation and the machine-wide worker reservation.

        The worker read renders the held owner in the refusal, and the worker acquire is the atomic
        check. Any worker-side failure releases the checkout reservation only when this call created
        it.
        """
        instance_key = load_instance_settings().instance_key
        checkout_created = reserve_local_stage(self.name, session_id, ("checkout",))
        owner = machine_locks.configured_owner(self.name, session_id)
        try:
            connection = self.connection()
            _ensure_same_machine_worker(runner, connection, timeout=connection.stop_timeout_seconds)
            transport = _transport(runner, connection)
            payload = _worker_helper(
                transport,
                connection,
                "course_explorer_admin.machine_locks",
                ("read", "worker", instance_key, self.name, session_id),
            )
            held = (
                machine_locks.Reservation.model_validate_json(payload) if payload.strip() else None
            )
            if held is not None and held != owner:
                raise RuntimeError(
                    f"worker reservation is held by {held.operation} in {held.instance_key}"
                )
            _worker_helper(
                transport,
                connection,
                "course_explorer_admin.machine_locks",
                ("acquire", "worker", instance_key, self.name, session_id),
            )
        except Exception:
            if checkout_created:
                unreserve_local_stage(self.name, session_id, ("checkout",))
            raise

    def unreserve(self, session_id: str, *, runner: mechanics.ProcessRunner) -> None:
        """Release the worker reservation, then the local checkout reservation."""
        connection = self.connection()
        transport = _transport(runner, connection)
        instance_key = load_instance_settings().instance_key
        _worker_helper(
            transport,
            connection,
            "course_explorer_admin.machine_locks",
            ("release", "worker", instance_key, self.name, session_id),
        )
        unreserve_local_stage(self.name, session_id, ("checkout",))

    def _progress(self, status: WorkerControllerStatus) -> TranslationProgress | InferenceProgress:
        if self.name == "translation":
            return TranslationProgress(kind="translation", worker=status)  # type: ignore[arg-type]
        preparation: Literal["snapshot", "transfer", "restore", "job"] | None = (
            "job" if status.lifecycle in {"prepared", "starting"} else None
        )
        return InferenceProgress(  # type: ignore[arg-type]
            kind="inference", worker=status, preparation=preparation
        )

    def _checkpoint_preparation(
        self,
        context: LifecycleContext,
        preparation: Literal["snapshot", "transfer", "restore", "job"],
    ) -> None:
        """Publish one inference preparation checkpoint before the mirrored worker starts."""
        if self.name != "inference":
            return
        context.checkpoint(InferenceProgress(kind="inference", preparation=preparation))

    def _read_status(
        self,
        transport: WorkerTransport,
        connection: WorkerConnectionSettings,
        session: WorkerDatasetSession,
    ) -> WorkerControllerStatus:
        status = _parse_status(
            self.status_type,
            _worker_helper(
                transport,
                connection,
                self.worker_module,
                ("status", session.session_id),
                env=_session_environment(session.session_id),
            ),
        )
        _validate_status(status, session)
        return status

    def _status_callable(
        self,
        transport: WorkerTransport,
        connection: WorkerConnectionSettings,
        session: WorkerDatasetSession,
        subcommand: str,
        *,
        timeout: float | None = None,
    ) -> Callable[[], WorkerControllerStatus]:
        """Return a callable that runs one worker status subcommand and validates it."""

        def call() -> WorkerControllerStatus:
            status = _parse_status(
                self.status_type,
                _worker_helper(
                    transport,
                    connection,
                    self.worker_module,
                    (subcommand, session.session_id),
                    env=_session_environment(session.session_id),
                    timeout=timeout,
                ),
            )
            _validate_status(status, session)
            return status

        return call

    @staticmethod
    def _mutation_with_reconcile(
        operation: Callable[[], WorkerControllerStatus],
        read_status: Callable[[], WorkerControllerStatus],
        accepted: set[str],
    ) -> Callable[[], WorkerControllerStatus]:
        """First attempt runs the helper. A retry first reconciles an already-accepted real status.

        A lost response may have already applied the mutation, so a second attempt reads and
        validates the real status first and returns it when it is an accepted outcome. Otherwise it
        invokes the idempotent owner again. Both the read and the helper share the caller's
        single recovery budget.
        """
        attempts = 0

        def call() -> WorkerControllerStatus:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                current = read_status()
                if current.lifecycle in accepted:
                    return current
            return operation()

        return call

    def _capture(self, context: LifecycleContext) -> WorkerDatasetSession:
        """Snapshot the Working dataset once and publish the policy if it is absent."""
        session_id = context.session.session_id
        directory = worker_dataset_session_directory(session_id)
        session_path = directory / "session.json"
        if not session_path.exists() and not session_path.is_symlink():
            session = remote.create_worker_dataset_session(
                self.name,
                session_id=session_id,
                course_limit=None,
                runner=context.runner,
            )
        else:
            session = remote.load_worker_dataset_session(session_id)
        policy_path = directory / "policy.json"
        if not policy_path.exists() and not policy_path.is_symlink():
            atomic_write_private_text(
                policy_path,
                administrative_json(self._make_policy(session.session_id)),
            )
        return session

    def _bring_up(
        self,
        context: LifecycleContext,
        transport: WorkerTransport,
        connection: WorkerConnectionSettings,
        session: WorkerDatasetSession,
        *,
        recovery: WorkerRecovery,
    ) -> WorkerResources | None:
        """Bring one session's canonical remote resources up. Return ``None`` when denied."""
        context.failure_step = "bring_up"
        state = context.read_state()
        resources = state.worker or WorkerResources()
        timeout = _admin_settings().database_operation_timeout_seconds
        _ensure_same_machine_worker(
            context.runner, connection, timeout=connection.stop_timeout_seconds
        )
        resources = _ensure_keepalive(
            context,
            transport,
            connection,
            resources,
            recovery=recovery,
            health_attempts=self.startup_attempts,
            sleep=time.sleep,
        )
        if not context.admit():
            return None
        resources = _ensure_worker_database(
            context, transport, connection, resources, timeout=timeout, recovery=recovery
        )
        if not context.admit():
            return None
        remote_session = remote_worker_session_directory(connection, session.session_id)
        resources = _create_remote_session_directory(
            context,
            transport,
            connection,
            resources,
            remote_directory=remote_session,
            recovery=recovery,
        )
        if not context.admit():
            return None
        directory = worker_dataset_session_directory(session.session_id)
        self._checkpoint_preparation(context, "transfer")
        for source, destination in (
            (directory / "session.json", remote_session / "session.json"),
            (directory / "policy.json", remote_session / "policy.json"),
            (directory / "input.dump", remote_session / "input.dump"),
        ):
            if not context.admit():
                return None
            recovery.call(
                lambda source=source, destination=destination: transport.upload(
                    source, destination, reuse_matching=True, timeout=timeout
                )
            )
        if not context.admit():
            return None
        recovery.call(lambda: _check_worker_dependencies(transport, connection))
        if not context.admit():
            return None
        if not resources.worker_prepared:
            self._checkpoint_preparation(context, "restore")
            # Fresh preparation is one-shot: the worker may already have restored a database before
            # publishing state, so a lost acknowledgment must fail loudly rather than replay it.
            status = self._status_callable(
                transport, connection, session, "prepare", timeout=timeout
            )()
            if status.lifecycle != "prepared":
                raise ValueError("worker session must report the prepared lifecycle")
            resources = resources.model_copy(update={"worker_prepared": True})
            context.record_worker(resources)
        self._checkpoint_preparation(context, "job")
        return resources

    def _launch(
        self,
        context: LifecycleContext,
        transport: WorkerTransport,
        connection: WorkerConnectionSettings,
        session: WorkerDatasetSession,
        *,
        recovery: WorkerRecovery,
    ) -> None:
        """Resume a retained worker session if needed and launch its detached run."""
        context.failure_step = "status"
        status = recovery.call(lambda: self._read_status(transport, connection, session))
        lifecycle = status.lifecycle
        if lifecycle in {"starting", "running", "completed"}:
            return
        if lifecycle in {"imported", "cleaned"}:
            raise ValueError("worker session is already finalized; use stop")
        if lifecycle not in _RESUMABLE:
            raise ValueError("retained session has no resumable outstanding work")
        armed = recovery.call(
            self._mutation_with_reconcile(
                self._status_callable(transport, connection, session, "resume"),
                lambda: self._read_status(transport, connection, session),
                _RESUME_ACCEPTED,
            )
        )
        if armed.lifecycle in {"starting", "running", "completed"}:
            return
        if armed.lifecycle in {"imported", "cleaned"}:
            raise ValueError("worker session is already finalized; use stop")
        if not context.admit():
            raise WorkerRecoveryCancelled()
        context.failure_step = "launch"
        try:
            # Exactly one detached launch. A lost response is reconciled read-only below, never by
            # calling the launcher again.
            _launch_worker_run(transport, connection, self.worker_module, session)
        except WorkerAvailabilityError as exc:
            recovery.last_returncode = exc.returncode
            recovery.begin()
            self._await_launch_acknowledgment(context, transport, connection, session, recovery)

    def _await_launch_acknowledgment(
        self,
        context: LifecycleContext,
        transport: WorkerTransport,
        connection: WorkerConnectionSettings,
        session: WorkerDatasetSession,
        recovery: WorkerRecovery,
    ) -> None:
        """Boundedly acknowledge a lost launcher response from read-only real status."""
        while True:
            if not context.admit():
                raise WorkerRecoveryCancelled()
            remaining = recovery.remaining()
            if remaining <= 0:
                raise WorkerConnectionLost(recovery.last_returncode) from None
            # The whole acknowledgment is the launch step: an unproved launch records
            # stage `launch`.
            status = recovery.call(lambda: self._read_status(transport, connection, session))
            if status.lifecycle in {"starting", "running", "completed"}:
                return
            if status.lifecycle == "failed":
                raise RuntimeError("worker run failed")
            time.sleep(min(connection.poll_interval_seconds, max(remaining, 0.0)))

    def _release_resources(
        self,
        context: LifecycleContext,
        transport: WorkerTransport,
        connection: WorkerConnectionSettings,
        *,
        recovery: WorkerRecovery,
    ) -> None:
        context.failure_step = "release"
        state = context.read_state()
        if state.worker is None:
            return
        _release_worker_resources(
            context, transport, connection, state.worker, recovery=recovery, sleep=time.sleep
        )

    def run(self, context: LifecycleContext) -> bool:
        """Capture, bring the worker up, launch its run and mirror progress until terminal."""
        context.failure_step = "transport"
        # One transport per detached run so its retained keepalive handle is the one released.
        connection = self.connection()
        transport = _transport(context.runner, connection)
        context.failure_step = "capture"
        self._checkpoint_preparation(context, "snapshot")
        session = self._capture(context)
        if not context.admit():
            return False
        # One budget for the initial bring-up/launch observation.
        recovery = WorkerRecovery(connection, run_allowed=context.admit)
        try:
            resources = self._bring_up(context, transport, connection, session, recovery=recovery)
            if resources is None:
                return False
            self._launch(context, transport, connection, session, recovery=recovery)
            while True:
                if not context.admit():
                    return False
                # A fresh budget per mirror iteration. A successful ordinary iteration ends that
                # episode and the next ordinary poll is a distinct one.
                iteration = WorkerRecovery(connection, run_allowed=context.admit)
                state = context.read_state()
                resources = state.worker or WorkerResources()
                context.failure_step = "status"
                resources = _ensure_keepalive(
                    context,
                    transport,
                    connection,
                    resources,
                    recovery=iteration,
                    health_attempts=self.startup_attempts,
                    sleep=time.sleep,
                )
                status = iteration.call(lambda: self._read_status(transport, connection, session))
                context.checkpoint(self._progress(status))
                lifecycle = status.lifecycle
                if lifecycle == "completed":
                    self._release_resources(context, transport, connection, recovery=iteration)
                    return True
                if lifecycle == "failed":
                    self._release_resources(context, transport, connection, recovery=iteration)
                    raise RuntimeError("worker run failed")
                if lifecycle == "interrupted":
                    raise RuntimeError("worker run interrupted")
                if lifecycle in {"starting", "running"}:
                    time.sleep(connection.poll_interval_seconds)
                    continue
                if lifecycle in {"imported", "cleaned"}:
                    raise ValueError("worker session is already finalized; use stop")
                if lifecycle == "paused" and status.reboot_due:
                    self._release_resources(context, transport, connection, recovery=iteration)
                    context.failure_step = "reboot"
                    rebooted = reboot.reboot_worker(
                        transport, connection, run_allowed=context.admit, recovery=iteration
                    )
                    if not rebooted or not context.admit():
                        return False
                    resources = self._bring_up(
                        context, transport, connection, session, recovery=iteration
                    )
                    if resources is None:
                        return False
                    self._launch(context, transport, connection, session, recovery=iteration)
                    continue
                raise RuntimeError("worker left the run unexpectedly")
        except WorkerRecoveryCancelled:
            # A pending pause/stop withdrew run intent. Preserve the live intent/progress and let
            # the detached terminal publisher and foreground pause/stop owner proceed.
            return False

    def release(self, context: LifecycleContext) -> None:
        """Foreground pause: forward the unloaded pause/stop and release owned resources."""
        state = context.read_state()
        if state.worker is None:
            return
        resources = state.worker
        context.failure_step = "transport"
        connection = self.connection()
        transport = _transport(context.runner, connection)
        # No run-admission callback: a requested pause must finish unloading, not cancel itself.
        recovery = WorkerRecovery(connection)
        context.failure_step = "status"
        if resources.worker_prepared:
            session = self._load_session(context.session.session_id)[0]
            status = recovery.call(lambda: self._read_status(transport, connection, session))
            if status.lifecycle in {"starting", "running"}:
                acknowledged = recovery.call(
                    self._mutation_with_reconcile(
                        self._status_callable(transport, connection, session, "pause"),
                        lambda: self._read_status(transport, connection, session),
                        _PAUSE_ACCEPTED,
                    )
                )
            elif status.lifecycle in {"prepared", "interrupted"}:
                acknowledged = recovery.call(
                    self._mutation_with_reconcile(
                        self._status_callable(transport, connection, session, "stop"),
                        lambda: self._read_status(transport, connection, session),
                        _STOP_ACCEPTED,
                    )
                )
            else:
                acknowledged = status
            context.checkpoint(self._progress(acknowledged))
            resources = context.read_state().worker or resources
        context.failure_step = "release"
        _release_worker_resources(
            context, transport, connection, resources, recovery=recovery, sleep=time.sleep
        )

    def finalize(self, context: LifecycleContext) -> None:
        """Foreground stop: stop/finalize, return the whole dataset, clean and release."""
        context.failure_step = "finalize"
        state = context.read_state()
        session_id = context.session.session_id
        directory = worker_dataset_session_directory(session_id)
        session_path = directory / "session.json"
        context.failure_step = "transport"
        connection = self.connection()
        # No run-admission callback: a requested stop must finish its safe owners and return.
        recovery = WorkerRecovery(connection)
        if (
            (not session_path.exists() and not session_path.is_symlink())
            or state.worker is None
            or not state.worker.worker_prepared
        ):
            if state.worker is not None:
                transport = _transport(context.runner, connection)
                context.failure_step = "release"
                _release_worker_resources(
                    context,
                    transport,
                    connection,
                    state.worker,
                    recovery=recovery,
                    sleep=time.sleep,
                )
            context.failure_step = "local_cleanup"
            self._unlink_local_scratch(context)
            return
        _ensure_same_machine_worker(
            context.runner, connection, timeout=connection.stop_timeout_seconds
        )
        transport = _transport(context.runner, connection)
        session = self._load_session(session_id)[0]
        timeout = _admin_settings().database_operation_timeout_seconds
        context.failure_step = "bring_up"
        resources = _ensure_keepalive(
            context,
            transport,
            connection,
            state.worker,
            recovery=recovery,
            health_attempts=self.startup_attempts,
            sleep=time.sleep,
        )
        resources = _ensure_worker_database(
            context, transport, connection, resources, timeout=timeout, recovery=recovery
        )
        context.failure_step = "worker_finalize"
        status = recovery.call(lambda: self._read_status(transport, connection, session))
        if status.lifecycle in {"prepared", "starting", "running", "paused", "interrupted"}:
            status = recovery.call(
                self._mutation_with_reconcile(
                    self._status_callable(transport, connection, session, "stop"),
                    lambda: self._read_status(transport, connection, session),
                    _STOP_ACCEPTED,
                )
            )
        if status.lifecycle in {"stopped", "completed", "failed"}:
            status = recovery.call(
                self._mutation_with_reconcile(
                    self._status_callable(
                        transport, connection, session, "finalize", timeout=timeout
                    ),
                    lambda: self._read_status(transport, connection, session),
                    _FINALIZE_ACCEPTED,
                )
            )
        if status.lifecycle not in {"imported", "cleaned"}:
            raise ValueError("worker session did not reach an imported or cleaned status")
        acknowledged = status
        context.checkpoint(self._progress(acknowledged))
        if acknowledged.lifecycle == "imported":
            context.failure_step = "return"
            recovery.call(
                lambda: remote.return_worker_database(
                    session, transport=transport, runner=context.runner
                )
            )
            admin = _admin_settings()
            root = admin_state_root(admin)
            remote_session = remote_worker_session_directory(connection, session_id)
            context.failure_step = "worker_cleanup"
            recovery.call(
                lambda: transport.upload(
                    root / "datasets" / session_id / "receipt.json",
                    remote_session / "dataset-receipt.json",
                    reuse_matching=True,
                    timeout=timeout,
                )
            )
            cleaned = recovery.call(
                self._mutation_with_reconcile(
                    self._status_callable(
                        transport, connection, session, "cleanup", timeout=timeout
                    ),
                    lambda: self._read_status(transport, connection, session),
                    _CLEANUP_ACCEPTED,
                )
            )
            if cleaned.lifecycle != "cleaned":
                raise ValueError("worker cleanup must report the cleaned lifecycle")
            context.checkpoint(self._progress(cleaned))
        context.failure_step = "release"
        resources = context.read_state().worker or resources
        _release_worker_resources(
            context, transport, connection, resources, recovery=recovery, sleep=time.sleep
        )
        context.failure_step = "local_cleanup"
        self._unlink_local_scratch(context)

    @staticmethod
    def _unlink_local_scratch(context: LifecycleContext) -> None:
        """Remove only this session's local transfer scratch after a completed stop."""
        directory = worker_dataset_session_directory(context.session.session_id)
        (directory / "input.dump").unlink(missing_ok=True)
        (directory / "keepalive.log").unlink(missing_ok=True)


def translation_stage() -> RemoteStage:
    """Build the one translation remote adapter."""
    connection = load_translation_controller_settings()
    return RemoteStage(
        name="translation",
        worker_module="course_explorer_admin.translation.controller_worker",
        status_type=TranslationControllerStatus,
        load_connection=load_translation_controller_settings,
        startup_attempts=connection.translation_controller_health_attempts,
        load_session=load_translation_dataset_session,
        make_policy=lambda session_id: TranslationControllerPolicy(
            schema_version=3,
            session_id=session_id,
            configuration_sha256=translation_configuration_sha256(),
        ),
        configuration_sha256=translation_configuration_sha256,
    )


def inference_stage(
    *,
    worker_module: str = "course_explorer_admin.inference.controller_worker",
    server_worker_module: str = "course_explorer_admin.worker",
) -> RemoteStage:
    """Build the one inference remote adapter over the supplied bootstrap module pair.

    ``worker_module`` is the detached worker-host helper this adapter launches, and
    ``server_worker_module`` is the server-side lifecycle worker that in turn runs this adapter, so
    a caller that owns an additional explicit route supplies its own pair.
    """
    connection = load_inference_controller_settings()
    return RemoteStage(
        name="inference",
        worker_module=worker_module,
        status_type=InferenceControllerStatus,
        load_connection=load_inference_controller_settings,
        startup_attempts=connection.inference_controller_health_attempts,
        load_session=load_inference_dataset_session,
        make_policy=lambda session_id: InferenceControllerPolicy(
            schema_version=2,
            session_id=session_id,
            configuration_sha256=inference_configuration_sha256(),
        ),
        configuration_sha256=inference_configuration_sha256,
        server_worker_module=server_worker_module,
    )


def run_translation_lifecycle_command(
    action,
    *,
    runner: mechanics.ProcessRunner | None = None,
    clock: Callable[[], datetime] = utc_now,
    session_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    sleep: Callable[[float], None] = time.sleep,
) -> LifecycleStatus:
    """Run exactly one translation lifecycle action through the shared core."""
    return run_lifecycle_command(
        action,
        stage=translation_stage(),
        root=stage_state_root("translation"),
        runner=runner,
        clock=clock,
        session_id_factory=session_id_factory,
        sleep=sleep,
    )


def run_inference_lifecycle_command(
    action,
    *,
    runner: mechanics.ProcessRunner | None = None,
    clock: Callable[[], datetime] = utc_now,
    session_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    sleep: Callable[[float], None] = time.sleep,
    stage_factory: Callable[[], RemoteStage] | None = None,
) -> LifecycleStatus:
    """Run exactly one inference lifecycle action through the shared core.

    ``stage_factory`` is the caller's explicit adapter builder, resolved at call time. Absent, the
    default local-only adapter is used.
    """
    stage = inference_stage() if stage_factory is None else stage_factory()
    return run_lifecycle_command(
        action,
        stage=stage,
        root=stage_state_root("inference"),
        runner=runner,
        clock=clock,
        session_id_factory=session_id_factory,
        sleep=sleep,
    )


def resume_reboot_interrupted_stages(
    *,
    runner: mechanics.ProcessRunner,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, ...]:
    """Resume the remote stages a same-machine host reboot interrupted.

    Only a same-machine worker is considered, because a remote worker's restart is issued by the
    session that waits for it. A stage is resumed only when its current state is a ``starting`` or
    ``running`` run whose recorded process belongs to an earlier boot and the operator's requested
    action is ``run``. A pending pause or stop, a fresh state, a same-boot process and a remote
    worker are not resumed. The resume action takes the stage's reservations and launches its
    detached child. The returned names are the resumed stages in stage order.
    """
    connection = load_worker_connection_settings()
    if connection.host_kind != "same-machine":
        return ()
    boot_id = mechanics.current_boot_id()
    resumed: list[str] = []
    stages: tuple[tuple[StageName, Callable[..., LifecycleStatus]], ...] = (
        ("translation", run_translation_lifecycle_command),
        ("inference", run_inference_lifecycle_command),
    )
    for name, command in stages:
        state = read_lifecycle_state(stage_state_root(name))
        if state is None:
            continue
        if state.lifecycle not in {"starting", "running"}:
            continue
        if state.requested_action != "run":
            continue
        if state.process is None or state.process.boot_id == boot_id:
            continue
        command("resume", runner=runner, clock=clock, sleep=sleep)
        resumed.append(name)
    return tuple(resumed)
