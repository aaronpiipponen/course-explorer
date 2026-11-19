# Worker sessions

A worker session is one run of a remote stage on the worker, from the prepared copy of the Working dataset to the verified return of the result.

## What a session binds

The translation and inference stages are [remote stages](/docs/reference/glossary.md#administration). Each run of one of them is a [worker session](/docs/reference/glossary.md#administration), and one session binds the identity of the source dataset, the stage, the derived worker database, and the full input snapshot of the Working dataset.

The worker database name is derived from the session id with `ADMIN_WORKER_DATABASE_PREFIX`, and the name of the returned server database with `ADMIN_DATASET_DATABASE_PREFIX`. Both prefixes are in `config/worker.toml`. The session evidence lives under `ADMIN_STATE_ROOT`, which is in `config/database.toml`. A remote worker holds the same evidence under its own copy of that path, and a same-machine session reads that path from the server and from the worker container as one mounted directory.

Each read of the session record, the control record, and the worker state checks the session identity. A mismatch fails before any model or command work.

## Explicit setup and the recurring start

The worker is installed, updated and checked by three explicit operations. The install runs on the worker and prepares its stack and resources, `setup worker` fast-forwards its checkout to one origin branch and installs its locked environment, and `preflight` reads the prepared facts without changing anything. A same-machine worker's image is built from the server checkout, so one deployment updates it and `setup worker` refuses with that guidance. [A Windows WSL worker](/docs/getting-started/worker-wsl.md#install-the-worker) and [a Linux worker](/docs/getting-started/worker-linux.md#install-a-remote-worker) state what each host needs, and [Installing a worker](/docs/getting-started/worker-install.md#the-install-steps) states the install command, its prompts and its steps. The preflight and the code update belong to [Setup and preflight](/docs/operations/setup-and-preflight.md).

A session start never fetches, installs or downloads. It checks the installed locked environment without syncing it and runs every worker helper without dependency resolution or download, so the prepared worker is a prerequisite of the first remote stage start.

## Running a stage

The translation and inference stages run through the administration container, and each command prints one JSON report. `PROJECT` is the instance's Compose project.

```bash
docker exec PROJECT-admin-1 python scripts/admin.py translation start
docker exec PROJECT-admin-1 python scripts/admin.py translation status
docker exec PROJECT-admin-1 python scripts/admin.py translation stop
docker exec PROJECT-admin-1 python scripts/admin.py inference start
docker exec PROJECT-admin-1 python scripts/admin.py inference status
docker exec PROJECT-admin-1 python scripts/admin.py inference stop
docker exec PROJECT-admin-1 python scripts/admin.py dispose inference TARGET_ID {nothing|done|failed}
```

A start captures the session, brings the worker up and launches one detached run. A status reads the recorded state and reaches no worker. A stop publishes the stop request, finalizes the retained output, returns the worker's copy of the dataset and returns the stage to idle. The dispose command records one operator decision for one target course, and it requires every stage idle, a verified returned or copied Working dataset, and the current schema head. [The stage tools](/docs/operations/operator-menu.md#the-stage-tools) lists the same entries in the numbered menu, and [the dataset return](/docs/operations/datasets.md#the-return-and-the-working-role) and [rotation](/docs/operations/datasets.md#rotation) state the receipt and the guards of a returned copy.

## Starting a session

A start captures a session when the stage holds none. The administration command dumps the Working dataset, including the embedding table data, into the session directory, records the digest of that dump and a verification of the Working database, and publishes the session record. A session that already holds its evidence is refused before any dump.

Preparation brings the worker up in order:

1. A WSL worker starts one keepalive process that holds the session's environment open. A native Linux worker and a same-machine worker start none.
2. A remote worker starts its own database service when it is not already running and records that it did. A remote database service that is already running without recorded ownership refuses the session. A same-machine session requires the instance's `db` service to be running, records no ownership of it, and starts and stops nothing.
3. The remote session directory is created once. A same-machine session uses the canonical session directory that the capture created, because the server path and the container path are one mounted directory.
4. The session record, the stage policy, and the input dump are uploaded. An upload reuses a byte-identical file already on the worker, and a same-machine upload whose two paths are one file copies nothing.

The worker then restores the input dump into its derived database and checks the restored database against the input snapshot. A mismatch fails the session.

A worker run requires a received database at the migration head of the worker code. A mismatch fails before any model or command work and changes no state.

The launch runs exactly one detached worker helper, which writes its log beside the session. A lost launcher response is reconciled from the worker status within the recovery bound, and the launcher is never run a second time. A fresh preparation is one-shot for the same reason, so a lost acknowledgement fails loudly rather than repeating a restore.

While the session runs, the stage mirrors the worker status into its own durable progress. The mirror is read-only, and it forwards no control to the worker.

## The worker reservation

A translation or inference session holds the machine-wide worker reservation from before the dataset hand-off through the verified return. The file lives in the worker's shared directory and names the instance key, the stage and the session id, so two instances that use one worker never run those stages at the same time. A start takes it, and the finalized stop releases it after the worker returned the dataset. A pause, a worker reconnect and the periodic reboot release no reservation, so an automatic worker restart never makes another instance treat the reserved worker as free. Ingestion and embedding take the server reservation instead and never reserve the worker.

The checkout reservation is held beside it for the same session, and [Lifecycle core](/docs/operations/lifecycle-core.md#locks-intent-and-checkpoints) states the three reservations.

## Cooperative control and checkpoints

The worker reads one control record. It names the session and one requested action: run, pause, or stop. The record is published atomically under a per-session lock.

A stop always writes stop. A pause never overwrites a pending stop, and nothing clears a pending pause or stop except an explicit re-arm, which only a locked resume performs.

The worker stage reads the control at its own checkpoints. Translation takes a pause or a stop between two cells, and inference takes one after a completed unit of work. [Stage lifecycle](/docs/operations/stage-lifecycle.md#pause-and-stop) lists the boundary of each stage. A control request therefore takes effect at the next checkpoint, and no run ends inside a unit of work.

A checkpoint records the worker state and the completed count, and it keeps the retained output. When a pause or a stop ends the run at a checkpoint, the stage unloads the model runtime it started and publishes the terminal lifecycle for the control request it read. That retained output is the progress of the session, so a resumed session carries the work the session finished. [Translation](/docs/backend/text-and-vectors/translation.md#the-output-it-publishes) describes the retained file and the import that writes it into the worker's copy.

A stage status reads the durable state and the owned process, and it writes nothing. A starting or running record whose owned runner process is not alive is reported as interrupted, and that projection writes no state.

## Pause, stop, and the return

A pause publishes a pause request, waits for the worker to exit, and releases the resources the session started. The session record, the state, the retained output, and the other evidence stay, so a resume can re-arm the work.

A stop is the authoritative end of a session. It publishes a stop request, waits for the worker to exit, and finalizes the session through its owning writer. The worker import writes the retained output into the worker's own copy of the dataset, and the inference stage publishes the accepted proposals and equivalence groups there. A session that already holds an import receipt is never imported a second time. A same-machine stop returns the dataset through the same path and leaves the instance's `worker` container running under its restart policy.

The worker exports its database as a verified return snapshot, and the return downloads or copies it, restores it into the derived server database, verifies it, and only then publishes it as Working. A same-machine return copies the dump from the mounted session directory. The worker cleanup drops the worker's own database after the import receipt and the returned dataset receipt exist. [The dataset return](/docs/operations/datasets.md#the-return-and-the-working-role) and [rotation](/docs/operations/datasets.md#rotation) state the receipt, and [failure outcomes](/docs/operations/datasets.md#failure-outcomes) states the cleanup of the worker copy.

The dataset path of one session runs from the Working snapshot through the worker's own copy to the worker database cleanup.

![The Working dataset is snapshotted into a verified input copy that the worker restores into its own database, where the worker stage runs with checkpoints. The exported return snapshot is returned, restored and verified as the Working dataset, and the worker database cleanup drops the worker's copy after the receipts exist.](/docs/diagrams/worker-session-return.svg)

> [!CAUTION]
> A stop that fails before the return completes leaves the selection in place and keeps the worker copy, the retained output, and the session evidence. The next stop verifies the recorded state again and continues from there.


### Resume and stop of a session that is not running

A resume acts on a session that is prepared, paused, interrupted, stopped, or failed, and a stop acts on one that is prepared, interrupted, or paused as well as a live one. A resume re-arms the control to run, records starting and launches the worker again. A stop launches nothing.

| Situation | Resume | Stop |
|---|---|---|
| The session is cleaned | Refused | Refused |
| The session holds an import receipt | The imported status is returned, and nothing is re-armed | Not applicable, the session is finalized |
| The session holds return evidence without an import | Refused | Not applicable |
| A runner process is alive when the lock is taken | Refused | Refused |
| The run ended on its own in the meantime | The real status is returned as it is | The real status is returned as it is |
| Otherwise | The model runtime the session started is stopped, and the control is re-armed to run | The model runtime the session started is stopped, and the session is recorded as stopped with its retained count |

Both wait for an exiting runner first and then decide under the session lock. A resume also requires a received database at the migration head before it re-arms.

## Owned resources only

A session records only the resources it created or started: on a WSL worker its keepalive process and the boot it ran in, whether it started or attempted to start the worker database service, whether it created the remote session directory, and whether the worker was prepared. Release touches only those. A native Linux session and a same-machine session have no keepalive to stop, and a same-machine session records no database service.

A database service that another run started before the session is left alone. A remote worker database service that is already running without recorded ownership refuses the session, so release never acts on a resource the session did not create. A same-machine session requires the instance's `db` service and leaves it running.

## Lost connection and recovery

Each worker step gets one ordinary invocation. A transient availability loss admits retries inside one bounded recovery episode whose budget is the configured remote-stage stop timeout, `WORKER_STOP_TIMEOUT_SECONDS` in `config/translation.toml`, which the example sets to 660 seconds. The retries are spaced by `WORKER_HEALTH_INTERVAL_SECONDS`, which the example sets to 2 seconds.

A completed worker helper that reports a nonzero status is a genuine failure and not an availability loss. It propagates at once, and its class and integer status are recorded in the stage failure record together with the step that was executing. A missing or malformed configuration value fails the same way.

An invocation that never reaches a reportable completion and does not recover inside the budget fails the step as a lost connection, and the available status is recorded. The session evidence, the retained output, and the owned resources stay, so a stop can verify the real state and return what the worker holds.

Every retry rechecks whether the run is wanted, so a pause or a stop that arrives during a recovery episode cancels the remaining retries instead of starting one more attempt. One recovery budget covers the initial bring-up and launch, and each mirrored status iteration gets its own budget.

The stage reports the content-free failure record on every status read, and a completed stop clears it. The record holds the executing step, the exception class, and an available integer status. [Stage lifecycle](/docs/operations/stage-lifecycle.md#failures-and-recovery) lists the steps.

## The periodic reboot

A [periodic reboot](/docs/reference/glossary.md#administration) restarts the worker after a configured amount of model run time. The clock of the session counts only the time a local model is loaded on the worker, it accumulates across model switches and pauses, and it samples at a completed unit of work and at every recorded failed generation attempt. Repeated local failures therefore reach the restart as well.

`WORKER_GPU_REBOOT_ENABLED` in `config/worker.toml` turns the restart on, and the example leaves it off. `WORKER_GPU_REBOOT_INTERVAL_MINUTES` in the same file sets the interval, and the example sets 120 minutes.

When a sample reaches the interval, the worker publishes a paused state with the restart due. The administration command releases the resources the session owns, restarts the worker once, proves that the worker came back, brings the session's resources up again, and launches the same session with its pending work. A WSL worker proves a changed Windows boot and a healthy WSL. A native Linux worker proves a changed kernel boot id through the one reboot-only sudoers rule. The session identity, the retained output, and the receipt do not change, and neither do the session's reservations: the reboot releases no reservation and rewrites no reservation file.

A same-machine worker requests the restart through one fixed privileged one-shot over its worker container's Docker socket, and the changed kernel boot id is the proof. The site and the console are down for the boot. The rebooting session cannot observe its own return, so the administration backend's startup resume reads the stage state once and calls the resume action only for a `starting` or `running` stage whose requested action is `run` and whose recorded process belongs to an earlier boot. An operator pause or stop, a same-boot process, a fresh state and a remote worker are not resumed. A failed resume prints one class line and the console serves the readable stage state.

A pause or a stop that arrives before the restart issues no restart. The clock record belongs to one session and one worker boot, and a record from another boot reports not due and is not rewritten until a real checkpoint. [Inference scheduling and disposition](/docs/backend/inference/inference-scheduling.md#the-periodic-reboot) states the timing rule for the inference stage.

The stage status carries the last mirrored `reboot_due` value inside the progress member's worker status, and that value is unknown while the progress member carries no worker status. It is the last observation the session recorded rather than a current read of the worker, and a status read never turns it into a worker request.

## Status and report scope

`docker exec PROJECT-admin-1 python scripts/admin.py <stage> status` reads the durable state and the owned process. It starts no service and reaches no worker, so a remote stage reports the last status its run recorded.

A remote stage's status therefore refreshes from server state alone, while its two worker logs are read on demand. `docker exec PROJECT-admin-1 python scripts/admin.py logs read inference SESSION_ID worker-runner --lines N` and the same command with `worker-runtime` read the worker's own `runner.log` and `runtime.log` over the transport, and `keepalive` reads the shared session directory's `keepalive.log`. [The fixed log reads](/docs/backend/interfaces/administration-commands.md#the-fixed-log-reads) lists every source and its refusals.

The server-side logs live beside the session receipt. A local stage's detached worker writes `runner.out` and `runner.err` there, and the keepalive log of a remote session sits in the shared session directory. The inference stage launches its server process with its output sent to the null device, so its two runner streams are reported as unavailable rather than written. A stop removes the two runner streams and the keepalive log with the rest of the session's scratch.

The coverage reports and the summaries read the dataset role each command names. The progress of a remote stage that has not returned is read from that stage's status instead, and the [Operator menu](/docs/operations/operator-menu.md#coverage-and-summaries) states that scope for every report entry.

## What a session keeps

A session keeps its evidence on both sides until its cleanup runs.

| Evidence | What it holds |
|---|---|
| Session record | The identity of the session, the source dataset, the stage, and the input snapshot |
| Stage policy | The configuration identity the stage binds to the session |
| Input snapshot and dump | The verified copy of the Working dataset that the worker restored |
| Control record | The requested action of the session |
| Worker state | The lifecycle, the completed count, and the measured active time of the run |
| Ownership records | The runner and runtime processes the session owns |
| Retained output | The finished work of the session, which its import writes into the worker's copy |
| Return snapshot and dump | The verified export of the worker database at the return |
| Import receipt | The proof that the retained output reached the worker's copy |
| Returned dataset receipt | The proof that the return passed the server verification |

### The inference policy identity

The inference stage binds its policy to an identity that covers the generation, embedding runtime, context, selection, rerank and rerank-input settings and the prompt content hash. A session whose recorded identity differs from the running settings refuses at the worker with `canonical policy configuration does not match current settings`, and a session prepared from the running settings runs.
