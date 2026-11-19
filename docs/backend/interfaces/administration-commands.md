# Administration commands

The administration command runs the operator menu and the machine commands that enroll a dataset, update the worker code, run the stages, report state and maintain the checkout.

## The administration command

`scripts/admin.py` delegates to the administrative dispatcher, which owns one parser and one JSON emitter. Without a subcommand, and with `menu`, it starts the numbered operator menu, which continues until Exit, an end of input, or an interrupt. [Operator menu](/docs/operations/operator-menu.md) describes the menu and the operations it drives.

Every command runs inside the administration container, which is the host's command surface:

```bash
docker exec PROJECT-admin-1 python scripts/admin.py server status
docker exec -it PROJECT-admin-1 python scripts/admin.py menu
```

The machine commands fall into nine groups. [Command adapters](/docs/backend/interfaces/command-adapters.md#the-administrative-dispatcher) holds the parser, the argument grammar and the exit statuses.

| Group | Commands |
|---|---|
| Menu | `menu`, and no subcommand |
| Setup | `setup datasets`, `setup worker`, `preflight`, `self-host example`, `self-host real-database`, `self-host up`, `self-host publish`, `self-host unpublish`, `deploy` |
| Stages | `ingestion`, `translation`, `embedding` and `inference`, each with `start`, `pause`, `resume`, `status` and `stop` |
| Datasets | `migrate selected`, `migrate working`, `migrate database`, `datasets list`, `datasets copy-to-working SOURCE_DATASET_ID`, `datasets copy-to-selected SOURCE_DATASET_ID --confirm-restart`, `datasets ensure-selected`, `datasets delete DATASET_ID [--confirm-working]` |
| Backups | `backups list`, `backups delete NAME` |
| Configuration | `config list`, `config read FILE`, `config set FILE KEY VALUE` |
| Reports | `coverage KIND --role selected\|working`, `summary KIND --role selected\|working`, `target inference TARGET_ID`, `server status`, `logs sessions`, `logs commands`, `logs read STAGE SESSION_ID SOURCE --lines N`, `logs embedding --lines N`, `usage --hours HOURS`, `serving-artifacts [--dataset ID] [--prune]` |
| Feedback | `feedback list [--query TEXT] [--filters JSON]`, `feedback attachment FEEDBACK_ID ATTACHMENT_ID`, `feedback delete FEEDBACK_ID` |
| Maintenance | `password --username USERNAME`, `dispose inference TARGET_ID DISPOSITION`, `rebuild`, `rebuild-admin`, `restart-admin` |

`coverage` takes `teaching-text`, `embedding` or `inference` as its kind, and `summary` takes `catalogue` or `inference`.

Seven of the machine commands build, deploy and serve a copy's containers and artifacts, and [Self-host and deploy commands](/docs/backend/interfaces/self-host-and-deploy-commands.md) describes them with the services they build and start.

The administration command differs from the read scripts in three ways.

- It reports the state of the dataset roles and of the stages, and some of its commands write to a dataset or to the worker checkout.
- A failure prints one content-free JSON line holding the exception's class name, plus fixed guidance, to standard error, and returns exit status 1. A checked subprocess failure adds its return code. An interrupt returns 130, and a not-ready `preflight` prints its report and returns 1.

### The guidance line of each operation

A failed command prints one content-free JSON line and then the fixed guidance of its operation.
The table lists the line each operation prints, and a configuration error prints the last row
whatever operation raised it.

| Operation | Guidance |
|---|---|
| `setup` | `Check the worker connection, then the checkout branch and the configured origin on this checkout and on the worker. A same-machine worker updates with the server deploy instead, run from the administration container with docker access.` |
| `preflight` | `Check the worker connection, start the same-machine worker container, check the checkout branch, the configured origin and the prepared worker resources, then run the worker install, the worker code update, or the server deploy on a same-machine host.` |
| `stage` | `A same-machine stage runs inside the administration container. Copy a dataset to Working with datasets copy-to-working if none is copied, inspect this tool's status, then retry its start, ordinary stop or resume as appropriate.` |
| `report` | `enroll a dataset or copy one to Working, ensure the server db is running at the current schema, then retry the report` |
| `migration` | `ordinary-stop all four stages, copy a dataset to Working first when migrating Working, ensure the server db is running, then retry` |
| `disposition` | `ordinary-stop all four stages, keep the verified returned or copied dataset as Working and the server db running at the current schema, then retry` |
| `target-read` | `copy a dataset to Working first if none is copied, ensure the server db is running at the current schema, then retry the target read` |
| `serving-artifacts` | ``complete the embedding stage on the served dataset, ensure the server db is running, then retry and re-create the site container with the App rebuild or `rebuild` after a successful build`` |
| `dataset-cleanup` | `enroll the Selected dataset, pick a non-selected target from the dataset listing printed by this entry, and ensure the server db is running` |
| `dataset-copy` | `pick a listed dataset whose database is present, stop every stage, ensure the server db is running, and retry` |
| `dataset-list` | `run setup datasets when the state root holds dataset records without a Selected pointer, check the dataset state files, then retry the listing` |
| `dataset-promotion` | `inspect the listed source, its database and serving artifacts with datasets list, then run Copy to Selected again with the confirmation once the reported step can succeed` |
| `backup-list` | `check that data/backups and every entry under it are a directory and regular files this account can read, with no symlink, then retry the listing` |
| `backup-cleanup` | `pick a regular file from the backup listing printed by this entry and check its access permissions` |
| `configuration` | `choose a listed configuration file and key, provide a valid JSON value, and check file permissions` |
| `server` | `check the site container, this checkout, and the running database and embedding services, then retry` |
| `logs` | `choose a session and source from the listed sessions, provide a positive line count, and check the worker connection for a worker read` |
| `rebuild` | `check the container host, the site and administration images, and the site container, then retry` |
| `usage` | `request a positive hour window no longer than the configured retention, and check the usage file permissions` |
| `password` | `provide a nonblank username, enter the same password twice at the hidden prompts, and check the file permissions of the administration state directory` |
| `feedback` | `check the configured feedback definition, the feedback database connection and the stored submission identity, then retry` |
| `self-host-example` | `check the container host and its ports, confirm the pinned model download, and run the workflow again after the reported step succeeds. A failed import leaves the copied working files, .env, the account and the example schema in place; clear the example database explicitly before a retry.` |
| `self-host-real-database` | `run the example workflow first, keep the database and embedding services running, and stop every stage before running this workflow again. A failed build or proof keeps the named database, its initial receipt and Selected; inspect them with `datasets list` and use the dataset owners for explicit maintenance before a retry.` |
| `self-host-up` | `check the container host, the ports and the configured publication credential, then retry` |
| `self-host-publish` | `check this account's Docker socket access, the configured site address and the machine-wide proxy project, then retry` |
| `self-host-unpublish` | `check this account's Docker socket access and the machine-wide proxy project, then retry` |
| `deploy` | `check the container host, the running services and the stage idle state, then retry` |
| any, on a configuration error | `check config/*.toml and this device's ignored .env, then retry the same command` |

`coverage` and `summary` also print a fixed note to standard error, stating that the reports read the named role and not unreturned worker progress.

## Dataset and backup cleanup

`datasets` and `backups` each take one nested verb, and each verb runs one delegate.

`migrate selected` and `migrate working` apply the migration head to the named role's database, and `migrate database` applies it to the configured database of a bootstrap state root. All three are explicit maintenance and print the verification read of the migrated database. A Working migration requires every stage idle with no live recorded process. The server `db` service must already run. `migrate database` refuses an enrolled deployment. [Datasets](/docs/operations/datasets.md#the-schema-head-and-migration) states the refusals and the verification report.

`datasets list` reports every receipt-bearing dataset, with the dataset id, the kind, the database name, the predecessor, the receipt's recorded migration head, the repository-relative retained dump path, the Selected and Working marks, the typed database presence and whether the dataset's artifact namespace exists. Role datasets are always listed, and any other dataset is listed when its database is present, its retained dump exists or its artifact namespace exists. One `psql` statement answers every known database name, and a bootstrap state root adds the configured database to that one probe without listing it as a dataset row. Actual service unavailability returns the typed `unavailable` presence with every known receipt listed. The report also carries the configured database of a bootstrap root and the four stage busy facts, so the console and the menu read one observation of each. The read creates no state or lock.

`datasets copy-to-working SOURCE_DATASET_ID` requires every stage idle, dumps the source, restores a fresh derived database, writes a copied receipt and publishes the Working pointer. `datasets copy-to-selected SOURCE_DATASET_ID --confirm-restart` copies and builds one enrolled source, publishes it as Selected, re-creates only the site container and proves the served catalogue and a changed identity. It refuses without the confirmation before any mutation, and a failed re-create or proof restores the previous Selected pointer and proves the previous catalogue. A failed build switches and re-creates nothing. [Datasets](/docs/operations/datasets.md#promotion-to-selected) states the full outcome and the preserved failed-copy resources.

The promotion owner reports each coarse step immediately before that step's work and reports the rollback once before it restores the previous pointer. The console streams those steps over its promotion route, the machine command prints the one final receipt, and the step reporting changes no output and retains nothing. `datasets ensure-selected` applies the deploy example-database exception and reports `bootstrap` or `enrolled`, and deploy reads that returned mode instead of probing the roles again.

`datasets delete DATASET_ID` drops one non-Selected dataset's server database, removes its retained dump and removes its derived artifact namespace. It refuses a target without a receipt, Selected, a receipt naming the Selected database, a database name on the fixed protected list, an unsafe dump or redirected artifact namespace before it changes a resource. Working additionally requires `--confirm-working` and every stage idle, and a successful Working deletion clears only the Working pointer. The other pointer, the receipt and every other database namespace stay in place. [Datasets](/docs/operations/datasets.md#manual-cleanup) states the whole outcome.

`backups list` reports every regular file under `data/backups` in lexical relative-path order with its size and its last modification time. `backups delete NAME` removes the one regular file that relative name identifies and refuses a name that leaves the root.

All of these commands print their owner's report and return exit status 0. A failure prints the content-free JSON line and the operation's fixed guidance to standard error, and returns exit status 1. A refused promotion prints the promotion guidance, which names the listed source, its database and serving artifacts, and the confirmed Copy to Selected. A refused cleanup prints the guidance of its cleanup kind. [Setup and preflight](/docs/operations/setup-and-preflight.md#cleanup-of-datasets-and-backups) holds the same cleanup operations behind a confirmation.

## Configuration reads and writes

`config` takes one nested verb, and each verb runs one delegate.

`config list` reports the registered subsystem files in composition order with the device-local environment file last, and it reads no file.

`config read FILE` reports every key of one listed file as that file holds it. A subsystem file is parsed as TOML, so a value is reported as that file holds it. The environment file is parsed into its assignments, a bare key reports no value, and every value is reported in plain text. The report is transient and is the one command output that carries file values.

`config set FILE KEY VALUE` replaces the one key of the one listed file with `VALUE`, which is one JSON literal over the broad type the key holds. The write renders only that key's text and leaves every other key with its comments, its order and its line endings. An environment-file save also appends one absent binding, and only when a settings family declares the key as one of its credential aliases. The appended assignment is one quoted line and leaves every other byte as it is. A subsystem TOML file takes no absent key, and the interactive operator menu edits the keys its read displays instead.

The save validates the whole candidate through the settings families that own the key, comparing the key and the aliases without case, reading the environment file freshly and parsing it strictly. A subsystem file is published atomically. The environment file is rewritten through its own inode, because inside the administration container it can be a single-file bind mount that no replacement can publish. That rewrite takes no lock, and a crash during it can leave a truncated file. A malformed assignment and a key assigned more than once in the environment file, ignoring case, each refuse the save. An environment-file save locates the stored binding without case and preserves its spelling. An environment-file write is mode 0600 and a subsystem file is published with its own permission bits. The acknowledgment holds the file and the key and no value.

```bash
python scripts/admin.py config list
python scripts/admin.py config read config/web.toml
python scripts/admin.py config set config/web.toml WEB_PORT 18026
python scripts/admin.py config set config/curriculum.toml CURRENT_ACADEMIC_YEAR '"2027-2028"'
```

Every example uses a synthetic value, and an environment-file read is the only command output that renders a device credential.

An unlisted file, a key the file does not carry outside that one append case, a JSON `null`, a non-finite number, a value of the wrong broad type, a value the owning settings family refuses, a value that is not one JSON literal, and a malformed or case-insensitively duplicated environment-file assignment each print the content-free JSON line and the configuration guidance to standard error and return exit status 1. [Setup and preflight](/docs/operations/setup-and-preflight.md#configuration) holds the same reads and the same save behind a confirmation.

## The administration password

`password --username USERNAME` stores the site's one administration account through one fixed command that runs in the administration command's own process. `--username` is required, and the password is read twice at the hidden terminal prompts `Password: ` and `Repeat password: `, so it never appears in the command line, in an argument, in an environment value or in a pipe. The credential owner hashes the entry with argon2id and publishes the credential file. An entry the terminal cannot hide is an error rather than an echoed fallback, two different entries are refused, and the acknowledgment names no credential.

```bash
python scripts/admin.py password --username OPERATOR
```

The command prints one acknowledgment line and returns exit status 0. It neither deletes the site's lock nor resets the failed-attempt count, and no configuration command can read or write the credential file, because administration state is not configuration. [Setup and preflight](/docs/operations/setup-and-preflight.md#the-administration-password) holds the same command behind Setup choice 10. A failed command prints the content-free JSON line and the password guidance to standard error and returns exit status 1.

## The feedback application store commands

`feedback` takes one nested verb. Each verb runs one delegate over the separate feedback application store, and each report is transient command output that no command saves.

`feedback list [--query TEXT] [--filters JSON]` reads the current feedback definition once and every submission the query and filters select. An absent or whitespace-only query searches nothing. Any other query is one literal, case-insensitive substring of every configured `text` and `textarea` answer, and `%` and `_` carry no wildcard meaning. Results are grouped by the earliest matching text field in definition order, then newest first, and one submission appears once. Without a query the listing is newest first. `--filters` is one JSON object mapping a configured select field ID to one of its option values, and defaults to `{}`. Every configured select may filter, every supplied pair must match, and an unknown field or option fails the command.

`feedback attachment FEEDBACK_ID ATTACHMENT_ID` reads one attachment only when both identities name the same stored file. The report holds the attachment metadata, including the submitted filename, and `content_base64`, the file's bytes encoded with standard base64. It holds no filesystem path. The bytes are transient command output, and the service stores no copy.

`feedback delete FEEDBACK_ID` deletes the named submission row and its attachment metadata in one database transaction, and removes the submission's stored files through the attachment store. The field directory and the attachment root belong to the store, and every other submission's rows and files are untouched. The command acts on the local store alone and reaches no remote issue or uploaded image. File removal and the database commit are not one transaction: a file failure can leave a partial removal, and a database failure after the file removal can leave the row. Both failures return exit status 1 instead of reporting a deletion.

```bash
python scripts/admin.py feedback list --query "login" --filters '{"role": "student"}'
python scripts/admin.py feedback attachment FEEDBACK_ID ATTACHMENT_ID
python scripts/admin.py feedback delete FEEDBACK_ID
```

A failure prints the content-free JSON line and the feedback guidance to standard error. [Configuration](/docs/backend/configuration.md#the-feedback-application-store) describes the feedback definition and the feedback application store.

## The target identity read and the summary policies

`target inference TARGET_ID` reads one canonical target's identity and its current durable decision without changing anything. It prints one JSON report that holds `dataset_id`, `target_group_id`, `course_code`, `course_name` and `disposition`, where `disposition` is `null` for an automatic target and one of `done` and `failed` for an explicit decision. The read requires the Working dataset and its live schema head. A decision additionally requires the verified returned or copied Working receipt and idle stages.

`dispose inference TARGET_ID {nothing|done|failed}` sets that decision, and `nothing` maps to `null` before the call, so the stored decision records an automatic target rather than a terminal one. The menu's **Dispose target** entry reads the identity before it asks for the decision, so the operator sees what the decision applies to. A guard that refuses either command prints the content-free JSON line and the fixed guidance line to standard error, and the command returns exit status 1. A refused read prints the target-read guidance, and a refused decision prints the disposition guidance.

```bash
python scripts/admin.py target inference TARGET_ID
python scripts/admin.py dispose inference TARGET_ID done
```

The detailed `summary inference` report holds, beside its coverage, publication and failure members, the three configured inference policy values `routing_policy`, `recommendation_policy` and `finding_filter_policy`, projected from the loaded generation settings rather than from a stored duplicate.

## Server status, log reads and rebuild

`server`, `logs`, `rebuild`, `rebuild-admin` and `restart-admin` each run one read or maintenance operation over this checkout.

### The server status report

`server` takes one nested verb, and the verb runs one delegate. `server status` prints one read-only report of the server the command runs on. It contacts no worker and retains nothing. The report holds six members.

| Member | Facts |
|---|---|
| `website` | The site container: its Compose state, health and image, its measured start instant and uptime, the configured loopback bind and process count, and the branch and revision of the checkout this command reads |
| `host` | The host's measured uptime, CPU usage sampled over a short interval, total and used memory, the filesystem total and free bytes, and the total bytes of `data`, `models` and `data/backups` |
| `database` | The database service's container state, image and health, the resolved database name, the database's own migration revision beside this checkout's expected head, its size, its connection count, and the container's shared-memory size |
| `embedding` | The embedding service's container state, image and health, and the configured model, resolved artifact path, bind, context size, thread count, pooling and normalization. The artifact path is the configured `EMBEDDING_ARTIFACT_PATH`, resolved under `ADMIN_SHARED_ROOT` when it is relative and used as written when it is absolute. |
| `serving` | The site's embedding feature switch, whether a Selected pointer exists, and the Selected receipt's id, kind, predecessor, pointer and recorded database head |
| `hubs` | On a site that serves the embedding features, both embedding hub files of the configured embedding directory: their name, their repository-relative path, their modification time, and whether each is newer than the measured web start. On a site with them off the member is empty and no artifact path is resolved. |

The report states which kind of fact each member carries: measured values from the container, the kernel and the database, configured values from the settings, the Selected receipt's facts, and the checkout identity of the command itself. The configured bind and process count are the values `WEB_HOST`, `WEB_PORT` and `WEB_WORKERS` set, and the report labels them as configured facts.

The `serving` member records the database head the selected receipt's verification carries, which is neither the live database's own revision nor this checkout's expected head. The branch and the revision are the identity of the checkout this command reads, and the hub file-time comparison is a restart hint rather than proof of the served process's values.

The database size, connection count and revision are reported while the database container runs, and a running container whose live read fails fails the whole command rather than reporting a guessed value. An optional fact the report finds none of is `null`: a service the Compose listing does not contain is `absent`, a configured embedding directory that does not hold a hub file has no modification time, a site with the embedding features off reports no hub entry, and every receipt fact is `null` while no Selected pointer exists.

A read, parse or stat failure, a hub path that does not name a regular file, and a Compose listing that carries no site service each fail the command instead of reporting a fact.

### The fixed log reads

`logs` takes one nested verb. `logs sessions` lists the sessions the four stage roots hold, newest first, with each session's stage, identity and receipt time. It reads the recorded receipts and creates nothing. Only a genuinely absent stage root contributes no entry, and a stage root behind a redirect is refused rather than reported as an empty listing.

`logs read STAGE SESSION_ID SOURCE --lines N` prints the last `N` lines of one fixed source of one listed session. `SOURCE` is one of the fixed sources, and `--lines` is required and takes a positive whole number.

| Source | What it reads |
|---|---|
| `server-stdout` | The stage's `runner.out` beside its session receipt |
| `server-stderr` | The stage's `runner.err` beside its session receipt |
| `keepalive` | The session's `keepalive.log` in the shared administrative session directory |
| `worker-runner` | The session's `runner.log` on the worker, read on demand |
| `worker-runtime` | The session's `runtime.log` on the worker, read on demand |

`server-stdout` and `server-stderr` belong to every stage, and the other three belong to the translation and inference stages alone. The stage and the source are closed sets that the parser checks, the session identity selects one recorded receipt of that stage, and a session identity that stage never recorded is refused. No argument carries a path, a filename or a shell command, so every read reaches one fixed basename beneath a derived directory.

Every directory from the filesystem root through the session directory must be a real directory, so a symlink or another redirect anywhere in that chain is refused before a log path is read.

A session that failed before it prepared its worker resources has no shared worker receipt, so a keepalive or worker read of that listed lifecycle-only session reports the log unavailable without loading worker settings or opening the transport. A malformed, symlinked or non-regular receipt is refused.

The remaining outcomes are:

- An absent log is reported as unavailable with `text` set to `null`.
- A log file that holds no lines is reported as available with empty text.
- A log that is a symlink or not a regular file is refused.

The `keepalive.log` of a translation session exists while that session's keepalive runs, and a stop removes it. The inference stage sends the keepalive output and the output of its server process to the null device, so both its keepalive log and its two runner streams are reported as unavailable.

The two worker sources read over the same worker transport the stages use, on demand. The read walks the derived path's ancestors on the worker and refuses a symlink or an unsearchable directory, and a path the worker does not hold at all is the one reported absence. The worker read installs nothing, prepares nothing and retries nothing. A read failure, an unreachable worker and a timeout print the content-free JSON line and the log guidance to standard error and return exit status 1, and no captured log text reaches that line.

`logs embedding --lines N` prints the last `N` lines of the embedding service's own container log. The read does not start the service, and a failed Compose command prints the content-free JSON line and the log guidance to standard error and returns exit status 1.

```bash
python scripts/admin.py server status
python scripts/admin.py logs sessions
python scripts/admin.py logs read inference SESSION_ID worker-runner --lines 50
python scripts/admin.py logs embedding --lines 100
```

### The command-log read

`logs commands` prints the retained entries of the sole administration command log, oldest first, each with its time, fixed command label, exit code, elapsed duration, outcome token and its one safe failure line when it has one. The read resolves this checkout and the configured `ADMIN_WEB_COMMAND_LOG_LIMIT`, reads the log owner's own file and creates no file, so an absent log prints an empty entry list and no invocation rewrites the file.

The `failed-login` and `lock` outcomes are the authentication events of the same file, and no entry holds captured output, a password, a key value or a free-form argument. An entry records one explicit command, one failed automatic read, or one of the sign-in, failed sign-in and lock events. A console read that the console repeats on its own timer and that succeeds records no entry, so the polls of an open administration console do not fill the record. [Administration backend](/docs/backend/interfaces/admin-backend.md#the-command-log) states the owner, the bound and the record shape.

```bash
python scripts/admin.py logs commands
```

### The rebuild commands

`rebuild` builds the site image, re-creates the database and embedding containers, re-creates the site container and proves its loopback bind. It works on the mounted checkout, uses the prepared database and embedding images, reaches no worker, and touches only this instance's containers. The report names the four steps and the verified bind, and the first failing step prints the content-free JSON line and the rebuild guidance and returns exit status 1.

`rebuild-admin` builds the administration image from this checkout alone, reports its single step and changes no running container, and the build is unbounded. `restart-admin` starts one detached one-off `admin` container that re-creates this checkout's own administration service, and the wait for the replacement service belongs to that one-off container. Both act on this checkout's own image and service, and the call that starts the one-off is bounded by the configured stop timeout.

A failed image build changes no running container, and it starts no re-create. A terminal or menu sequence runs the two commands in order and reaches `restart-admin` only after a successful build: `rebuild-admin` followed by `restart-admin`.

The one-off container outlives the service it performs, so `restart-admin` returns after the detached one-off is started, and its report acknowledges the request alone. It claims no observed restart, no restarted process and no verified bind. A failed build and a refused re-create each print the content-free JSON line and the rebuild guidance and return exit status 1.

```bash
python scripts/admin.py rebuild
python scripts/admin.py rebuild-admin
python scripts/admin.py restart-admin
```

[Setup and preflight](/docs/operations/setup-and-preflight.md#the-two-rebuild-scopes) holds both scopes behind their own confirmations.

## The request usage report

`usage --hours HOURS` takes one required option and prints one report of the recorded web requests, and it prunes the records past the configured retention. `--hours` takes a positive integer.

The window is whole hours back from the current rounded UTC hour inclusive. `--hours 1` covers the current hour, the window ends at the end of that hour, and the report holds `window_start`, `window_end`, `requests`, `mean_duration_seconds`, `p95_duration_seconds`, `p99_duration_seconds` and `routes`. Each route row holds its `route`, its `requests`, its `mean_duration_seconds`, its `p95_duration_seconds` and its `p99_duration_seconds`, and the rows sort by route. The mean is the arithmetic mean of the recorded elapsed serving seconds.

The two percentiles use the exact nearest-rank definition. For `n` recorded durations sorted ascending, p95 is the value at zero-based index `(95 * n + 99) // 100 - 1` and p99 at `(99 * n + 99) // 100 - 1`, so each value is one of the recorded durations and carries its full precision. One request takes its own duration at both percentiles. The overall values cover every recorded request and the route values cover that route alone. A window with no recorded request reports zero requests, null for the mean and both percentiles, and no route rows.

`USAGE_RETENTION_DAYS` of `config/web.toml` sets the retention that bounds the file. A count that is not positive, or that is longer than the retention in hours, is refused before the command opens the usage file, so a window longer than the retention is refused rather than shortened. The same read drops the records older than the retention in days and retains every newer record, including one outside the reported window. One invocation therefore reports a window and prunes the file.

[Front router](/docs/backend/interfaces/front-router.md#the-request-usage-record) describes the records the front router appends and the file they live in. The report and the serving append take one exclusive lock on the same file, so an append never interleaves with the report's rewrite and neither one loses a record. An absent usage file reports zero requests and creates no file, and a malformed line fails the command without rewriting the file and without putting any line content into the failure line.

```bash
python scripts/admin.py usage --hours 24
```

[Operator menu](/docs/operations/operator-menu.md#server-and-log-reads) holds the same report behind its hours prompt.

## The example data loader

`scripts/mock_data.py` is data preparation rather than a feature read. Its four actions import the invented example dataset, add the bundled vectors of that dataset, write the serving artifact set the site's switch selects, and rewrite the tracked bundled vector file. The loader refuses an enrolled state root, so it fills a bootstrap configured database, and it requires that database at the running code's migration head.

The two import actions print one report each, and the artifact action prints one JSON report of the built artifact paths and its counts. The artifact action builds artifacts alone and rewrites no tracked file, so a deployment never changes the checkout, and `write-bundle` is the explicit rewrite of `data/mock/embeddings.json`. `src/course_explorer/example_data.py` owns `EXAMPLE_SOURCE_ATTRIBUTION`, the attribution of the example dataset, which the loader in `src/course_explorer/mock_data.py` and the feature route in `src/course_explorer_web/routes/features.py` import. [The example dataset](/docs/getting-started/example-dataset.md) describes when the loader runs.

The stage actions, the dataset operations, and the worker operations are described in [Stage lifecycle](/docs/operations/stage-lifecycle.md), [Datasets](/docs/operations/datasets.md), and [Worker sessions](/docs/operations/worker-sessions.md).
