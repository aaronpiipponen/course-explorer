# Setup and preflight

The setup and preflight entries prepare and maintain this copy, its worker, its containers, its configuration and its administration account.

First-run setup runs through `scripts/self-host.sh`, not through this menu. The **Setup/preflight** entry holds the dataset enrollment, the copy to Working, the read-only worker preflight, the role migration, the two cleanup entries, the configuration editor, the two rebuild scopes and the account password. Every machine command runs inside the administration container, which is the host's command surface.

| Choice | Entry | Machine command |
|---|---|---|
| 1 | Enroll/select dataset | `docker exec PROJECT-admin-1 python scripts/admin.py setup datasets` |
| 2 | Copy dataset to Working | `docker exec PROJECT-admin-1 python scripts/admin.py datasets copy-to-working SOURCE_DATASET_ID` |
| 3 | Worker code update | `docker exec PROJECT-admin-1 python scripts/admin.py setup worker` |
| 4 | Read-only worker preflight | `docker exec PROJECT-admin-1 python scripts/admin.py preflight` |
| 5 | Migrate dataset | `docker exec PROJECT-admin-1 python scripts/admin.py migrate selected` or `migrate working` |
| 6 | Dataset cleanup | `docker exec PROJECT-admin-1 python scripts/admin.py datasets list` and `datasets delete DATASET_ID [--confirm-working]` |
| 7 | Backup cleanup | `docker exec PROJECT-admin-1 python scripts/admin.py backups list` and `backups delete NAME` |
| 8 | Configuration | `docker exec PROJECT-admin-1 python scripts/admin.py config list`, `config read FILE` and `config set FILE KEY VALUE` |
| 9 | Rebuild app | `docker exec PROJECT-admin-1 python scripts/admin.py rebuild` |
| 10 | Administration password | `docker exec -it PROJECT-admin-1 python scripts/admin.py password --username USERNAME` |
| 11 | Rebuild administration console | `docker exec PROJECT-admin-1 python scripts/admin.py rebuild-admin` and `restart-admin` |

`PROJECT` is the instance's Compose project, `course-explorer-<instance key>`. The menu opens inside the same container:

```bash
docker exec -it PROJECT-admin-1 python scripts/admin.py menu
```

The dataset enrollment starts the server database service, migrates the configured database to this checkout's migration head and enrolls it as the Selected initial dataset. An already-enrolled dataset is reused and returned as it is.

The copy lists every receipt-bearing dataset and asks for one source id. It confirms the exact dataset before it copies, requires every stage idle, creates the verified copy and publishes it as Working. The confirmation names the dataset and its database.

The worker code update fast-forwards the worker checkout to one origin branch and installs its locked project environment. It prepares no resource and installs no system package. The worker's stack is installed on the worker itself by its own command, which also prepares the configured artifacts and runtimes and writes a chosen model preset into the worker's model configuration files. [A Windows WSL worker](/docs/getting-started/worker-wsl.md#install-the-worker) and [a Linux worker](/docs/getting-started/worker-linux.md#install-a-remote-worker) state what each host needs, and [Installing a worker](/docs/getting-started/worker-install.md#the-install-steps) states that command, its prompts and its steps.

The menu reads the server checkout's branch first and asks for the origin branch only when that branch is not `main`. The question shows the server branch as its default, and an empty answer accepts it. The machine command sets the same branch with `--branch BRANCH`, and the default is `main`. Its report holds the worker branch, its revision, whether the server checkout's branch differs from its origin counterpart, and no resource readiness. A same-machine worker has no separate checkout. Its image is built from the server checkout, so one deployment updates it, and this entry refuses with that guidance.

The preflight reads the branch and HEAD of the server checkout and of the worker checkout, checks the worker's locked environment, and reads the worker resources when that check passes. On a same-machine worker it reports the server checkout's branch and revision, because the worker image is built from that checkout. On a same-machine worker it reads the running worker container, because the first stage reservation starts the worker service. A preflight before that reservation reports the safe preflight failure.

It installs, downloads and starts nothing, and it never elevates. The socket, daemon and image reads use Docker's plain command, and the Compose reads use the shared bridge, so an account outside the `docker` group reports the `docker` component instead of prompting. A same-machine operation runs inside the administration container, and a run outside it refuses with that one rule. A missing generation file is named `generation-artifact`, a second distinct generation file `generation-artifact-fallback`, and the translation artifact `translation-artifact` whether it is a published file or a local conversion. A configured GPU stack that does not answer is named `gpu-stack`, and each other missing resource carries its own fixed name. A GPU stack is host-provided: a ROCm worker's stack comes from its configured package source, and a CUDA worker's driver and toolkit are installed on the host. The machine command returns 1 when the locked environment or a resource is not ready.

The migration asks for Selected or Working, then applies the migration head to that role's database as explicit maintenance. A Working migration requires every stage to be idle. The server database service must already be running.

The worker code update and the preflight add one fixed notice when the server checkout's branch differs from its origin counterpart. The notice states that pushing is a separate manual step. The menu prints it after the report, and the machine command prints it to standard error.

## First-run setup

The example workflow and the real-database workflow run from the host bootstrap, which builds the administration image and runs the workflow inside it with the checkout mounted at its own path. The host needs Docker, Docker Compose and Git alone.

```bash
scripts/self-host.sh example
scripts/self-host.sh real-database
```

[First-run installation reference](/docs/operations/first-run-setup.md) states the working files, the instance resources and the addresses, and [First-run troubleshooting](/docs/operations/first-run-troubleshooting.md) states the refusals. [The bootstrap](/docs/operations/first-run-setup.md#the-bootstrap) lists the verbs and the shared directory rules, and [the example prompts](/docs/operations/first-run-setup.md#the-example-prompts) states every prompt and the JSON input the non-interactive form accepts.

## Cleanup of datasets and backups

Two entries delete one target the operator names.

**Dataset cleanup** lists every receipt-bearing dataset and deletes one dataset. The machine commands are `docker exec PROJECT-admin-1 python scripts/admin.py datasets list` and `docker exec PROJECT-admin-1 python scripts/admin.py datasets delete DATASET_ID [--confirm-working]`.

**Backup cleanup** lists the files under `data/backups` and deletes one of them. The machine commands are `docker exec PROJECT-admin-1 python scripts/admin.py backups list` and `docker exec PROJECT-admin-1 python scripts/admin.py backups delete NAME`.

Both entries print the listing before they ask for a target, and `0` at the target prompt cancels the entry. An empty listing and a failed listing both return to the calling menu without a target prompt. An answer outside the printed listing, Selected and any dataset without a receipt in the listing are refused with one fixed line and delete nothing.

A target that the listing contains draws one confirmation titled with that exact target. A non-role target and a backup use `Delete dataset <id> (<database name>)?` and `Delete backup <name>?`. A Working target uses `Delete Working dataset <id> (<database name>)? Working becomes absent.` The confirmation holds **1 Delete** and **0 Cancel**, and the menu passes the Working confirmation only after that titled choice. Only the delete choice runs the deletion, and that owner's report is printed as it returns. A cancel and any other answer delete nothing and return to the calling menu. An end of input and an interrupt delete nothing, stop no running work, and leave the whole menu with exit status 0 and 130.

The two listing commands create no state. The dataset listing reads every receipt and the role pointers, then probes every known database name once. A stopped service produces a typed unavailable presence rather than a failed listing. `backups list` requires access to the backup directory and reports an empty listing when that directory is absent. A dataset deletion requires a Selected dataset, a target from its listing and the running server database service. A Working target also requires every stage idle. A backup deletion requires a regular file from its listing and access to it. [Datasets](/docs/operations/datasets.md#manual-cleanup) states every refusal and what each deletion retains.

> [!CAUTION]
> A cleanup deletion is irreversible. The listing names each target, and the confirmation names the exact dataset or backup file that the deletion removes.

## Configuration

Choice 8 reads one configuration file and saves one key of it. The machine commands are `docker exec PROJECT-admin-1 python scripts/admin.py config list`, `config read FILE` and `config set FILE KEY VALUE`.

The entry lists the registered subsystem files in composition order with the environment file last, prints the listing, and asks for a file the listing carries. It then prints every key of the chosen file as that file holds it and asks for a key the report carries. The value prompt takes one JSON literal that matches the broad type the key holds, and the entry does not strip or echo it.

A save draws one confirmation titled `Save configuration <file> key <key>?` with **1 Save** and **0 Cancel**. The title names the file and the key and no value. Only **1 Save** runs the save, and the writer's report is printed as it returns. `0` at the file prompt or the key prompt cancels the entry, an answer outside either prompt writes nothing, and a value the owning settings family refuses prints the shared safe failure and guidance.

One save writes one key, so a file with several edits takes one save per key. The entry edits the keys the read displays, so it writes a key the chosen file already carries and appends nothing. The machine command accepts one more case: `config set` appends an absent environment alias that a settings family owns for that file as one quoted assignment that leaves every other byte as it is, and it refuses an absent subsystem-file key or an absent environment key that no settings family owns.

A save of `CURRENT_ACADEMIC_YEAR` or `ACTIVE_ACADEMIC_YEAR_WINDOW` in `config/curriculum.toml` also refreshes the stored activity of every course in every present dataset role and, when no Selected dataset is enrolled, of the configured bootstrap database. Such a save is refused while any preparation stage runs.

> [!CAUTION]
> The read prints every value of the chosen file in plain text, the environment file included.

A saved file reaches a running process when that process next builds its settings, which is when its container starts again. [Configuration](/docs/backend/configuration.md#reading-and-writing-a-file) states the precedence between the files.

## The two rebuild scopes

Choice 9 rebuilds the app. Choice 11 rebuilds the administration console. The two scopes are separate, each has its own confirmation, and neither reaches the other's process or its resources.

Choice 9 builds the site image, recreates the database and embedding containers, re-creates the site container and proves its bind. The machine command is `docker exec PROJECT-admin-1 python scripts/admin.py rebuild`.

The entry asks one confirmation, `Rebuild the site image, re-create the database and embedding containers, re-create the site container and prove its bind?`, and holds **1 Rebuild** and **0 Cancel**. Only the rebuild choice runs the command. A cancel and any other answer run nothing and return to the calling menu, and an end of input and an interrupt run nothing and leave the whole menu with exit status 0 and 130.

The command runs four steps in this order, and the first failure stops it.

1. The site image build, `python -m course_explorer_admin.compose -- build web`, in the checkout. It builds the site target from `docker/server/Dockerfile` and the mounted checkout.
2. The two prepared containers, `python -m course_explorer_admin.compose --non-interactive -- up -d --wait --force-recreate --no-build db embedding`. The step uses the prepared images, and the data volumes and databases are left as they are.
3. The site container re-create, `up -d --wait --force-recreate --no-build web` through the same bridge. It reuses the image the first step wrote and waits for the container to become healthy.
4. The bind proof: a loopback `GET /` with the configured `Host` must answer status 200 and a `text/html` content type within `ADMIN_STARTUP_ATTEMPTS` polls spaced by `ADMIN_POLL_INTERVAL_SECONDS`.

The two image builds are not bounded by a timeout. The container steps are bounded by `ADMIN_STOP_TIMEOUT_SECONDS`, and the bind wait by its poll count and interval. The report names the four steps and the verified bind, and it is the transient acknowledgment of the run. A failed step prints the shared safe failure and the rebuild guidance, and no further step runs. The rebuild leaves the checkout's Git state as it is, reaches no worker, and touches only the instance's own containers.

The command holds the checkout reservation for its whole run, so it refuses before its first step while a preparation stage or a Working dataset operation holds the checkout. [Administration commands](/docs/backend/interfaces/administration-commands.md#the-rebuild-commands) states each command's report and failure presentation.

Choice 11 builds the administration image alone and then starts the detached re-create of this checkout's own administration service. The machine commands are `docker exec PROJECT-admin-1 python scripts/admin.py rebuild-admin` and `restart-admin`, and the entry runs the first and then the second.

The entry asks one confirmation, `Rebuild the administration console? This signs all operators out.`, and holds **1 Rebuild** and **0 Cancel**. The warning states the sign-out because replacing that backend container ends every session it holds in memory. Only the rebuild choice runs the sequence. A cancel and any other answer run nothing and return to the calling menu, and an end of input and an interrupt run nothing and leave the whole menu with exit status 0 and 130.

The sequence runs two commands in this order, and a failed build stops it before the re-create.

1. `rebuild-admin` builds the administration image with `python -m course_explorer_admin.compose -- build admin`, from the mounted checkout. The image build is not bounded by a timeout. It recreates no container, opens no database and reaches no worker.
2. `restart-admin` starts one detached one-off container that runs `up -d --wait --force-recreate --no-build admin`, and returns once that container is started. The one-off runs as its own container, so it outlives the replacement of the service it performs.

The report the entry prints is the re-create command's own acknowledgment that the one-off is started. It states no observed restart and no verified bind, so the console's own restart and the following sign-in are what show the outcome.

## The administration password

Choice 10 stores the site's one administration account. The machine command is `docker exec -it PROJECT-admin-1 python scripts/admin.py password --username USERNAME`.

The entry asks `Username (0 to cancel): ` through the ordinary menu input, and `0` cancels it. It then asks for the password twice at the hidden prompts `Password: ` and `Repeat password: `, which the command itself owns, so no password is read from or written to the menu's own streams. Two different entries, a blank entry, an end of input and an interrupt each store nothing, and an end of input and an interrupt leave the whole menu with exit status 0 and 130.

```bash
docker exec -it PROJECT-admin-1 python scripts/admin.py password --username OPERATOR
```

The command prints one acknowledgment line and stores the account's username and an argon2id hash in `data/admin-web/password`. [Administration commands](/docs/backend/interfaces/administration-commands.md#the-administration-password) states the command, the file, and the failure presentation.

> [!IMPORTANT]
> The entry is a terminal provisioning step, and the account's own site authenticates with it. Each site holds its own account, so each deployment sets its own username and password.
