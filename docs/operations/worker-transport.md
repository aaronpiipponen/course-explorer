# Worker transport

This page maps the modules that carry one worker session over the transport boundary and states the connection values, the worker requirements and the fixed log read.

## Module owners

| Module | Owns |
|---|---|
| `src/course_explorer_admin/transport.py` | The one worker command and file transport seam with its WSL, native Linux and same-machine kinds, its recovery budget, and the transport error classes |
| `src/course_explorer_admin/logs.py` | The session listing, and the fixed bounded log reads the two worker sources reach through that transport |
| `src/course_explorer_admin/remote.py` | The worker session, control, and status DTOs, the worker-side prepare/export/cleanup helpers, and the worker dataset CLI |
| `src/course_explorer_admin/worker_session.py` | The shared worker-side lifecycle shell, its subcommands, and the identity, quiescence, and cleanup guards |
| `src/course_explorer_admin/remote_stages.py` | The server-side translation and inference adapters over the lifecycle core and the administration backend's startup resume |
| `src/course_explorer_admin/machine_locks.py` | The machine-wide checkout, server and worker reservations and their files |
| `src/course_explorer_admin/snapshots.py` | The dump and restore command mechanics and the embedding-data rule |
| `src/course_explorer_admin/datasets.py` | Receipt and role-pointer writes, enrollment, role migration, copy, return retention, and rotation |
| `src/course_explorer/datasets.py` | The dataset DTOs, pointer resolution, the schema-head check, and the verification read |
| `src/course_explorer_admin/provisioning.py` | Explicit worker code update and read-only preflight |
| `src/course_explorer_admin/provisioning_worker.py` | Worker-side resource readiness and preparation, the prepared-runtime check, the per-key generation components with their fallback, the published or local translation artifact and its derived source URL, and its one `check` action |
| `src/course_explorer_admin/worker_install.py` | The interactive worker install that writes the worker's working files, identity and selected model preset, prepares its stack, and recreates a running same-machine worker with its generated GPU override |
| `src/course_explorer_admin/working_files.py` | One owner for materializing a checkout's absent working files from the tracked examples |
| `src/course_explorer_admin/reboot.py` | The loaded-GPU checkpoint clock and the host restart handshakes of the WSL, native Linux and same-machine worker host kinds |
| `src/course_explorer_admin/runtime.py` | The owned model runtime shared by the two worker stages |

The behavior these owners implement is described in [Worker sessions](/docs/operations/worker-sessions.md) and [Datasets](/docs/operations/datasets.md). The session lifecycle seam, the dataset owners and the model runtime are described in [Worker dataset owners](/docs/operations/worker-dataset-owners.md).

## The reservation helper

`course_explorer_admin.machine_locks` is the one owner of the machine-wide reservations and the worker-side helper that reaches them. Its files live in the shared directory's `locks` directory, and a remote stage invokes the module on the worker for the worker reservation:

```bash
uv run python -m course_explorer_admin.machine_locks read worker INSTANCE_KEY OPERATION SESSION_ID
uv run python -m course_explorer_admin.machine_locks acquire worker INSTANCE_KEY OPERATION SESSION_ID
uv run python -m course_explorer_admin.machine_locks release worker INSTANCE_KEY OPERATION SESSION_ID
```

`INSTANCE_KEY` must name a configured university key. Any other value is refused before the shared root is read. The module reads the shared root through `load_shared_storage_settings` on the host that runs it. The session id argument is optional. `read` prints the held owner record when one exists and nothing otherwise, and `acquire` and `release` print nothing. The worker-side read renders the held owner in the operator message, and the acquire is the atomic check. [Worker sessions](/docs/operations/worker-sessions.md#the-worker-reservation) states when the reservation is held.

## The transport abstraction

`build_worker_transport(runner: mechanics.ProcessRunner, settings: WorkerConnectionSettings)` is the one construction owner. It returns `SshWslTransport` for `WORKER_HOST_KIND = "wsl"`, `SshLinuxTransport` for `WORKER_HOST_KIND = "linux"`, and `ComposeExecTransport` for `WORKER_HOST_KIND = "same-machine"`. Every kind extends `WorkerTransport`, which owns four boundaries.

| Method | Boundary |
|---|---|
| `run(argv, *, cwd, env=None, stdin_path=None, timeout=None)` | One command with captured stdout and a classified result |
| `start_detached(argv, *, cwd, env, output_path, timeout=None)` | One detached command and its log file |
| `upload(source, destination, *, reuse_matching=False, timeout=None)` | One file streamed to the worker and renamed into place, or a same-machine local copy beside its destination |
| `download(source, destination, *, timeout=None)` | One file streamed back to an atomic local mode-0600 file, or a same-machine local copy |

The WSL kind carries every command through `wsl.exe -d <distribution> -- bash -lc` with the cmd.exe metacharacter escaping that Windows OpenSSH requires. Its keepalive methods are `start_keepalive`, `keepalive_is_alive`, `stop_keepalive`, and `terminate_remote_keepalive`. The WSL kind reads the native Windows last-boot identity and restarts Windows, and the native Linux kind runs one `bash -lc` command over plain SSH, reads the kernel boot id and restarts through `sudo -n /usr/bin/systemctl reboot`. The native Linux kind refuses every keepalive operation with one fixed `ValueError`.

The same-machine kind runs every command as `docker compose exec -T worker bash -lc` through the server's Compose bridge, so the command path holds one Compose client and no SSH client. It reads the worker container's paths as this host's paths, so every same-machine operation runs inside the administration container and one run outside it refuses with that rule before any Compose command. The server and the worker container read the same mounted checkout, shared directory and session directory.

An upload whose two paths are one file returns without a transfer, and another upload is a local copy beside its destination. A same-machine stage uses the instance's own `db` service, so this kind owns no database service and records none. It starts no keepalive.

`read_host_boot_id` reads the kernel boot id through the container. `request_host_reboot` issues one fixed `--rm --privileged --pid=host` one-shot of the running container's own image, which enters the host's namespaces and runs the host's `systemctl reboot`. The worker container's Docker socket carries that authority. The host severs the exec connection, and the changed kernel boot id is the proof.

The handshake retries that transport availability loss inside the configured reboot budget, and the administration backend's startup resume continues the interrupted same-machine session.

The `worker` service belongs to the instance stack under the `worker` profile, and `up` starts it when the configured host kind is `same-machine`. The install writes `docker-compose.worker.yml` with the host's ROCm device nodes and group ids, or the NVIDIA runtime and the three CUDA toolkit sonames. The Compose bridge adds that file and the profile to every command on a same-machine host, so the service is built, started and inspected through the same bridge as the other services. A remote-worker host runs its Compose commands without either addition.

Every command body runs through one wrapper that installs an exit trap before the working directory change. The trap emits exactly one `COURSE_EXPLORER_TRANSPORT_RESULT <phase> <status>` record on standard error, where the phase is `bootstrap` before the payload starts and `command` after. `_classify` reads the process status and the records: a zero status must carry exactly one zero `command` record, a nonzero status with a valid nonzero record raises `WorkerCommandError`, and a nonzero status without a record raises `WorkerAvailabilityError`. Any other shape raises `ValueError`, so an unproven completion is never mistaken for a success. The wrapper rejects an environment override whose key is not on `_WORKER_ENVIRONMENT_ALLOWLIST`, which holds the stage state roots, the derived database name, and the checkpoint-reboot toggle and interval.

`WorkerRecovery` owns one retry-admission budget. `begin()` fixes one deadline of `connection.stop_timeout_seconds`, `remaining()` reports what is left of it, and `call(operation)` retries only `WorkerAvailabilityError` while the budget lasts and the optional `run_allowed` admission holds. `WorkerConnectionLost` is the expired-recovery subclass, and `WorkerRecoveryCancelled` is raised when live run intent is withdrawn.

The connection values name where the transport goes. They are non-secret worker identity values in `config/worker.toml`, so a machine change does not change shared code. [The worker connection values](#the-worker-connection-values) lists them and the rules that bind each host kind.

Transport protocols at the level the module fixes them: an SSH command is one SSH invocation against the configured worker target, and each command body runs inside the configured environment on the worker. A WSL command carries the `wsl.exe` wrapper, and a native Linux command is one `bash -lc` over SSH. A same-machine command is one `docker compose exec -T worker bash -lc` invocation through the server's bridge. The host boot read and the restart request use the same target without the wrapper on the SSH kinds. The WSL kind sends the native Windows commands, and the native Linux kind sends the kernel boot read and the one allowed restart command. The same-machine kind reads the kernel boot id through its exec, and its restart request is the one fixed privileged one-shot over the worker container's Docker socket. The WSL keepalive is one detached local SSH child that holds a remote sleep open for the session, and `terminate_remote_keepalive` stops the exact session-named remote process.

## The worker connection values

`WORKER_HOST_KIND` selects the kind. The `wsl` and `linux` kinds require the SSH target, and the `same-machine` kind refuses it. The `wsl` kind requires the distribution, and the other kinds refuse it. A same-machine worker's project root is its container path. `WORKER_SSH_CONNECT_TIMEOUT_SECONDS` bounds one connect attempt, `WORKER_STOP_TIMEOUT_SECONDS` is the recovery and stop bound, and `WORKER_HEALTH_INTERVAL_SECONDS` spaces the retries and polls.

| File | Key | Holds |
|---|---|---|
| `config/worker.toml` | `WORKER_HOST_KIND` | `wsl` reaches a Windows host through a WSL distribution, `linux` reaches a native Linux host over plain SSH, and `same-machine` reaches the instance's worker container with no SSH hop. The example file carries `wsl`. |
| `config/worker.toml` | `WORKER_SSH_TARGET` | The SSH target of a `wsl` or `linux` worker. A `same-machine` worker removes the key. The example file carries a placeholder value. |
| `config/worker.toml` | `WORKER_WSL_DISTRIBUTION` | The WSL distribution on that target that holds the checkout. A `linux` worker names no value. The example file carries a placeholder value. |
| `config/worker.toml` | `WORKER_PROJECT_ROOT` | The absolute path of the project checkout on the worker, or `/app` for a same-machine worker. The example file carries a placeholder value. |
| `config/worker.toml` | `WORKER_SETUP_PROJECT_URL` | The expected origin URL. The worker update compares both checkout origins against it, the preflight compares the server checkout origin against it, and the example file carries a placeholder value. On a same-machine worker the server checkout's origin is the worker's, because its image is built from that checkout. |
| `config/worker.toml` | `WORKER_SSH_CONNECT_TIMEOUT_SECONDS` | The bound on one connect attempt, 10 seconds in the example. |
| `config/worker.toml` | `WORKER_SETUP_TIMEOUT_SECONDS` | The bound on each remote install, download, or build step, four hours in the example. |
| `config/worker.toml` | `WORKER_GPU_REBOOT_ENABLED` | Turns the periodic checkpoint reboot on. The example leaves it off, and a deployment that wants the periodic restart turns it on. |
| `config/worker.toml` | `WORKER_GPU_REBOOT_INTERVAL_MINUTES` | The loaded-model run time after which the worker restarts, 120 minutes in the example. |
| `config/worker.toml` | `WORKER_GPU_BACKEND` | `rocm` builds llama.cpp with HIP and launches the model children with the ROCm device variables, and `cuda` builds it with CUDA and launches them with the CUDA device variable. The example file carries `rocm`. |
| `config/worker.toml` | `WORKER_GPU_TARGET` | The ROCm compute target such as `gfx1201` for `rocm`, and the CUDA compute capability without the dot such as `86` for `cuda`. The example file carries `gfx1201`. |
| `config/worker.toml` | `WORKER_GPU_DEVICE_INDEX` | The vendor index of the card the model servers use, from 0 to 15. The example file carries `0`. |
| `config/database.toml` | `ADMIN_SHARED_ROOT` | The machine-wide shared directory the worker preparation loads. Preparation creates its `models/` and `locks/` directories at mode 0700, so the key holds one value before preparation runs. |
| `config/translation.toml` | `WORKER_HEALTH_INTERVAL_SECONDS`, `WORKER_STOP_TIMEOUT_SECONDS` | The retry and poll interval, 2 seconds in the example, and the recovery and stop bound, 660 seconds in the example. |
| `config/translation.toml`, `config/inference.toml` | The artifact keys | The configured model files and runtimes the worker holds, named by `TRANSLATION_ARTIFACT_PATH`, `TRANSLATION_ARTIFACT_SHA256` and the translation source keys, each `INFERENCE_LOCAL_VARIANTS` entry's `artifact_path` and `artifact_sha256`, and the rerank artifact keys. A path is absolute or relative to the worker checkout, and a model file the worker shares with another checkout is stored under `ADMIN_SHARED_ROOT/models/` and named by its absolute path. A model file at that path in the checkout, holding its pinned bytes, satisfies the readiness read. A local translation conversion is installed at its path beforehand. |
| `config/translation.toml`, `config/inference.toml` | `TRANSLATION_CONTROLLER_STATE_ROOT`, `INFERENCE_CONTROLLER_STATE_ROOT` | The repository-relative roots that hold each controller's recorded state. |

The install writes the worker's own copy of these values from its answers and the detected host, and the server keeps its own `config/worker.toml` with the same connection values, because the code update and the preflight reach the worker through them. A server copy for a WSL worker also names the distribution. [A Windows WSL worker](/docs/getting-started/worker-wsl.md) and [a Linux worker](/docs/getting-started/worker-linux.md) state the install answers, and [Configuration](/docs/backend/configuration.md#subsystem-keys) states every key of the subsystem files.

## What the worker must provide

- A `wsl` or `linux` worker's target accepts an SSH connection without a password prompt, because every remote command runs with batch mode set. One SSH target serves either host kind.
- A `wsl` worker names the configured WSL distribution that exists on that target and runs every remote command inside it. A `linux` worker names no distribution and runs every remote command as `bash -lc` over plain SSH. A `same-machine` worker runs every command through `docker compose exec -T worker` in the instance's worker container.
- The SSH target and, for a WSL worker, the WSL distribution are each 1 to 64 ASCII letters, digits, underscore, dot, or hyphen.
- The project root is an absolute path without dot components, and a WSL worker's path carries no percent characters because Windows OpenSSH passes it through cmd.exe. A same-machine worker's project root is `/app`, the container path of the image root.
- A WSL worker starts one keepalive process per session. A native Linux worker and a same-machine worker start none.
- A `wsl` or `linux` worker's checkout exists at the configured project root on a non-detached Git branch with its origin remote set, because the code update reads it and applies a fast-forward pull. The update fetches the selected origin branch, refuses a local branch ahead of or diverged from the origin branch, switches the checkout onto the branch, and pulls the branch fast-forward. Git refuses an operation that would overwrite a local change. A same-machine worker's project root is the image's `/app`, and one deployment updates its image.
- The install runs `sudo` in the operator's terminal, so it prompts for the account's password when a privileged step needs it.
- The account reaches Docker through the `docker` group, because the stages run without a terminal and reach Docker through the Compose bridge. The group gives the account full control of the machine. The install starts the Docker service and joins the group, and the membership takes effect in a new login session.
- A native Linux worker on its own machine allows exactly `/usr/bin/systemctl reboot` through one installed sudoers rule, because the checkpoint reboot runs that command over the same transport. The install offers the rule after the other steps, replaces the template's `WORKER_ACCOUNT` token with the account on the rule line, and checks the installed file with `visudo -c -f`. A WSL host and a same-machine host receive no rule.
- A same-machine worker restarts its host at the checkpoint reboot through one fixed privileged one-shot over the worker container's Docker socket, so it needs no rule. The reboot takes the site and the console down for the boot, and the administration backend's startup resume relaunches the interrupted stage when the stack returns.
- On a native Linux ROCm worker the account is in the `render` and `video` groups, because `/dev/kfd` and the `/dev/dri` nodes belong to them and the model servers open those nodes. The install joins both groups for a ROCm worker. A same-machine host joins the `docker` group alone, and its worker container receives the two device group ids in the generated override.
- Docker and Docker Compose are available, because the prepared database image and the embedding image run through the Compose bridge. The database image build installs the serving extension from `docker/db/extension/`, and readiness requires its capability label.
- The configured GPU stack answers, because preparation reads it before the first build and the model servers need it. A ROCm worker needs `rocminfo` to name the configured compute target, and its build reads `hipconfig` for the ROCm prefix, the HIP compiler and the clang path. A CUDA worker needs `nvcc` to answer and a card whose compute capability, read through `nvidia-smi`, equals the configured target. A CUDA same-machine worker also needs the NVIDIA Container Toolkit installed and its `nvidia` runtime registered with Docker, which `docker info` lists.
- The package list follows the backend and the host kind, because the install runs it. A ROCm worker installs `rocm`, and a ROCm worker inside WSL adds `rocdxg-roct`. A CUDA worker installs no GPU package: its driver and CUDA toolkit are installed on the host.
- Each backend and compute target has its own runtime directory `~/.local/src/llama.cpp-<revision>-<backend>-<target>`, so one machine holds the ROCm and CUDA builds of a revision and the builds for two targets beside each other. Preparation clones and builds there, and the built runtime carries no web UI because every model child runs with `--no-webui`.
- A model child runs with `HIP_VISIBLE_DEVICES` set to the configured card index on a ROCm worker, and with `CUDA_VISIBLE_DEVICES` on a CUDA worker. A ROCm worker inside WSL also sets `HSA_ENABLE_DXG_DETECTION=1`, which its device path needs.

## The fixed worker log read

`course_explorer_admin.logs` owns one bounded read of a fixed session log, and it uses the same transport without the lifecycle helpers. `read_session_log(stage, session_id, source, *, lines, runner)` resolves the lifecycle session receipt of the chosen stage for every source. The `keepalive` source also resolves the recorded canonical worker session, and the `worker-runner` and `worker-runtime` sources derive the worker path server side from `remote_worker_session_directory`, so no caller supplies a path. A worker source runs exactly one `transport.run(("bash", "-c", script), cwd=PurePosixPath(str(connection.worker_project_root)), timeout=connection.stop_timeout_seconds)` with no environment override.

For a worker source, the script is one line, and it is the only worker command of that read. It walks the derived path's ancestors from the root to the log's parent, refusing a symlink before it checks existence and requiring each present ancestor to be a searchable directory, then it refuses a symlink at the log path itself and requires a regular file there, and it prints the last lines with `tail -n COUNT -- PATH`. Every ancestor path is quoted, each ancestor refusal exits 1, an absent ancestor or an absent log exits 3, and a tail failure exits 1.

Only a command-phase exit status of 3 is classified as an absent log, which the read reports as unavailable with no text. A read failure, a timeout, a connection or protocol failure and a decode failure propagate to the caller, and no captured output reaches the failure line.

This read installs nothing, prepares nothing, reads no worker status, starts no keepalive and retries nothing, so it needs no `uv` invocation, no dependency check, no prepared checkout and no recovery episode.

## Tests that pin this behavior

| Test module | Covers |
|---|---|
| `tests/admin/test_worker_setup.py` | The fast-forward code update, preservation of user work in a dirty checkout, the read-only resource check, the per-key artifact downloads with the fallback component, the published and local translation artifact, the install of each of the three model presets, the local install's working files, identity and privileged steps, the same-machine identity with its generated ROCm and CUDA override, and the same-machine preflight over the server code identity and the image environment |
| `tests/admin/test_log_reads.py` | Each fixed local source, a redirected session directory chain, the classified absent worker log beside a genuine read failure, the symlink refusals, and the one fixed worker command |
