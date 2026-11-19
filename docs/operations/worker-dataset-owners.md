# Worker dataset owners

This page states the worker session lifecycle seam, the worker commands, the dataset owners and predicates, the checkpoint clock and the model runtime.

The transport that carries these calls is described in [Worker transport](/docs/operations/worker-transport.md), and [Worker sessions](/docs/operations/worker-sessions.md) and [Datasets](/docs/operations/datasets.md) state the behavior of a session and of a dataset role.

## The session lifecycle seam

`WorkerStage` in `src/course_explorer_admin/worker_session.py` is the seam a worker stage implements. Its methods are `connection`, `load_session`, `load_state`, `require_policy`, `publish_prepared`, `run`, `read_import_receipt`, `finalize`, `status`, `stop_updates`, and `require_cleanup_evidence`, and its attributes name the stage, the controller lock name, and the artifacts that cleanup removes.

| Function | Owns |
|---|---|
| `prepare_worker_session(stage, session, *, runner)` | The canonical prepare and the already-prepared acknowledgement |
| `run_worker_session(stage, session_id, *, runner, ...)` | One session run under the session instance lock |
| `request_worker_action(stage, session_id, action, *, runner, ...)` | Pause, resume, and stop, and their acknowledgement |
| `finalize_worker_session(stage, session_id, *, runner)` | The one import and publication of a terminal session |
| `cleanup_worker_session(stage, session_id, *, runner)` | The receipt-gated drop and evidence removal |
| `read_worker_status(stage, session_id, *, clock)` | The read-only operator status |
| `project_worker_lifecycle(state, *, directory)` | The interrupted projection of a vanished runner |
| `await_worker_quiescence(...)` and `require_worker_quiescence(state, directory)` | The bounded exit wait and the locked re-proof |
| `require_worker_database_schema(session)` | The received-database schema-head precondition |
| `worker_main(stage)` | The prepare, run, pause, resume, status, finalize, cleanup, and stop subcommands |

The server-side adapter is `remote_stages.RemoteStage`. `run(context)` captures the session, brings the worker up, launches the run, and mirrors progress until a terminal status. `release(context)` is the pause path, and `finalize(context)` is the stop path that finalizes, returns, cleans, and releases.

`_bring_up(context, transport, connection, session, *, recovery)` starts the keepalive on a WSL worker, then the worker database service, the remote session directory, the uploads, the check of the locked environment, and the worker restore. `_launch(...)` re-arms a retained session and starts the detached run, and `_await_launch_acknowledgment(...)` resolves a lost launcher response from read-only status.

`_mutation_with_reconcile(operation, read_status, accepted)` reads the real status before a retry, and the closed accepted sets are `_RESUME_ACCEPTED`, `_PAUSE_ACCEPTED`, `_STOP_ACCEPTED`, `_FINALIZE_ACCEPTED`, and `_CLEANUP_ACCEPTED`. `remote_stages._transport(runner, connection)` is the single construction point and returns `build_worker_transport(runner, connection)`, and `_worker_helper(...)` wraps every worker invocation in `uv run --no-sync --offline --no-python-downloads`.

Where to change what:

| Behavior | Owner |
|---|---|
| Adding a remote stage | A `WorkerStage` implementation plus a `RemoteStage` built in `remote_stages.py` |
| The control vocabulary | `WorkerControl` in `remote.py` and `publish_worker_control` in `worker_session.py` |
| The worker subcommand set | `worker_main` in `worker_session.py` |
| The transport or its recovery budget | `transport.py` |
| The session identity fields | `WorkerDatasetSession` in `remote.py` |

## Worker commands

These commands run on the worker. The first two act on the session directory of one prepared session, and the third reads the worker's resources and takes no session.

```bash
uv run --no-sync --offline --no-python-downloads python -m course_explorer_admin.translation.controller_worker status SESSION_ID
uv run --no-sync --offline --no-python-downloads python -m course_explorer_admin.remote prepare SESSION_ID
uv run --no-sync --offline --no-python-downloads python -m course_explorer_admin.provisioning_worker check
```

`course_explorer_admin.remote` exposes `prepare`, `export`, and `cleanup`. The stage worker modules expose `prepare`, `run`, `pause`, `resume`, `status`, `finalize`, `cleanup`, and `stop` through the shared shell. `course_explorer_admin.provisioning_worker` exposes `check` as its one action, and `course_explorer_admin.worker_install` is the local install that runs in the worker's terminal.

## Dataset owners and predicates

`src/course_explorer/datasets.py` owns the DTOs and the read-only checks. `DatasetReceipt.validate_kind_identity` enforces the initial, returned and copied receipt shapes, `DatasetPointer` is the strict pointer, `DatabaseSnapshot` binds a dump digest to a session and a verification, and `DatasetVerification` carries the migration head, the table row counts, the catalogue summary, and the coverage reads.

One public dataset read applies the state-tree guard once and passes the checked root down, so one listing does not walk the tree once per pointer and receipt.

`read_dataset_role(state_root, role)` reads one role and decides bootstrap, `resolve_dataset_role(state_root, role)` raises the role guidance, `resolve_selected_dataset(state_root)` delegates to that resolver, `read_dataset_receipt(state_root, dataset_id)` reads one confined receipt after the state guard, `dataset_state_refusal(state_root)` reports a redirected state tree, `require_selected_dataset_root(state_root)` refuses one, `require_database_schema(connection)` compares the current migration head and raises `DatabaseSchemaMismatchError`, and `read_dataset_verification(settings)` opens one disposable engine for the verification read.

`src/course_explorer_admin/datasets.py` owns every write. `admin_state_path(settings)` returns the repository-relative state root, `admin_state_root(settings)` resolves it and refuses the backups boundary, `initialize_selected_dataset(*, runner)` enrolls the initial dataset, `ensure_selected_dataset(*, runner)` applies the example-database exception for deploy, `migrate_dataset(role, *, runner)` applies the head to the named role's database, where a Working migration takes the checkout reservation under the `migrate_working` operation and requires every stage idle and a Selected migration does neither, `copy_to_working(source_dataset_id, *, runner)` creates a verified copy and publishes Working, and `retain_returned_dataset(session, snapshot, dump, *, runner)` restores, verifies, writes the returned receipt, publishes Working, and rotates.

The receipt predicate and the path helpers sit beside those writes. `require_returned_dataset_receipt(receipt, session, *, server_database)` is the receipt predicate, `read_optional_dataset_receipt(root, dataset_id)` tells an absent receipt from a malformed one, `require_droppable_database(database_name)` refuses a protected name, `_retain_dump(source, destination)` copies a dump into its confined path, `_walk_chain(receipts, start)` validates one chain through the action's receipt index, and `_rotate_locked(settings, root, runner)` drops the databases outside both role pairs.

`save_activity_configuration(file, key, value, *, settings_families)` saves one activity policy key and refreshes the stored activity of the Selected and Working databases, or of the configured database when no Selected pointer exists, with every stage idle under the datasets action lock. `run_administrative_config_set` routes `CURRENT_ACADEMIC_YEAR` and `ACTIVE_ACADEMIC_YEAR_WINDOW` of `config/curriculum.toml` to it, and every other key takes the file save alone.

`src/course_explorer_admin/remote.py` owns the session-side dataset work. `create_worker_dataset_session(stage, *, session_id, course_limit=None, runner)` snapshots the Working dataset, `prepare_worker_database(session, *, runner)` restores the uploaded input dump and checks it against the snapshot digest, `export_worker_database(session, *, runner)` verifies and exports the return snapshot, `return_worker_database(session, *, transport, runner)` downloads and retains it, and `cleanup_returned_worker_database(session, receipt, *, runner)` drops the exact worker database after the recorded session, the receipt and the return snapshot agree on their session and database identity. `worker_dataset_session_directory(session_id)` and `remote_worker_session_directory(connection, session_id)` own the two session path forms.

`src/course_explorer_admin/snapshots.py` owns the process commands. `dump_database(*, runner, root, settings, destination, include_embeddings, timeout=None)` dumps into a fresh temporary, publishes it at mode 0600 and returns its producer digest, `restore_database(...)` creates or verifies a target and restores into it, checks a supplied expected digest once, and compares the restored database's verification with the supplied snapshot verification, and `database_dump_contains_embedding_data(listing)` matches `_EMBEDDING_TABLE_DATA_PATTERN` for the embedding-data policy. A transferred or moved dump is restored with its snapshot digest, while a same-operation copy passes no separate digest and relies on the producer digest stored in the receipt and the restore verification.

Where to change what:

| Behavior | Owner |
|---|---|
| The receipt, snapshot, or verification shape | `src/course_explorer/datasets.py` |
| Enrollment, copy, return, or rotation | `src/course_explorer_admin/datasets.py` |
| The dump and restore commands | `src/course_explorer_admin/snapshots.py` |
| The derived database names | `AdministrativeDatasetSettings.dataset_database_prefix` and `worker_database_prefix` in `src/course_explorer/config/database.py` |

## The checkpoint clock and the model runtime

`reboot.WorkerGpuClock(directory, *, session_id, settings, monotonic=time.monotonic)` counts loaded seconds only. `loaded()` opens one segment, `unloaded()` closes it, `checkpoint()` samples the open segment and publishes `WorkerGpuCheckpoint` as `gpu-clock.json`, and `due` reports the last published sample while `WORKER_GPU_REBOOT_ENABLED` is on.

`worker_gpu_reboot_due(directory, *, session_id, settings)` is the read-only due check, and `reboot_worker(transport, connection, *, run_allowed, recovery=None, sleep=..., monotonic=...)` issues one restart request and proves the boot change of the configured host kind before it returns. A WSL worker proves the native Windows boot changed and then a parsed WSL kernel boot id, a native Linux worker proves the kernel boot id changed, and a same-machine worker proves the same change through its worker container's exec.

A transport availability loss during the handshake is the same severed observation as exit 255 or a timeout, and only the changed boot claims success.

`runtime.OwnedWorkerRuntime(runner, *, root, directory, sleep=...)` owns one model child through the `runtime.json` ownership record. `start(spec, *, attempts, interval, timeout)` reuses a live identical record or spawns after `provisioning_worker.require_prepared_runtime`, and merges the spec's configured GPU variables into the closed child environment. `stop(*, timeout, interval)` retires only a proved-owned process. `WorkerInferenceRuntime` adds `activate`, `stop`, `start_rerank`, `stop_rerank`, and `request_scope` over that owner, and the inference stage passes its `WorkerGpuClock` in so a mode switch closes and opens the loaded segment.

## Tests that pin this behavior

| Test module | Covers |
|---|---|
| `tests/admin/test_remote_lifecycle.py` | The due reboot and the same-session resume, the pending-pause suppression, the bounded mirror failure and its diagnostics, the stop recovery, the failed return, the genuine helper error, the same-machine transport over the canonical session directory, the reserve guard and the instance db refusal, and the host-run reserve refusal before any Compose command |
| `tests/admin/test_datasets.py` | Enrollment idempotency, the pointer-only interruption, Working assignment on return, copy and deletion authorities, both role pairs and rotation, the redirected collection, listing availability, Working deletion, and the serialized worker export |
| `tests/admin/test_dataset_database.py` | The real dump, restore, copy and return round trip, vector and verification preservation, role-pair rotation, and the guarded role migration |
| `tests/admin/test_checkpoint_reboot.py` | The clock arithmetic and the toggle, the changed-boot plus healthy-environment requirement, the native Linux handshake, and the same-machine one-shot with its severed read |
| `tests/admin/test_runtime.py` | The configured runtime builders, the ownership record, the occupied port, the reused foreign PID, and the record from another boot |
| `tests/datasets/test_datasets.py` | The pointer resolution, the presence-based roles, the cached-alias guard, and the failure shapes |
