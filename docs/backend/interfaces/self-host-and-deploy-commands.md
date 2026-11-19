# Self-host and deploy commands

The self-host workflows, the stack verbs, the deploy command and the serving artifact command build and serve one checkout.

Each command belongs to the [administration command](/docs/backend/interfaces/administration-commands.md), which states the shared failure line and the exit status of every machine command.

## The start list

The `site_services` owner of the administration package returns the start list, the services that one command builds and starts: `web`, then `admin`, then `sender` when its caller asks for one, then the `worker` service behind the `worker` Compose profile when `WORKER_HOST_KIND` in `config/worker.toml` is `same-machine`.

Each caller decides the sender. `self-host example` asks for one on a run that carries a publication token, `self-host up` asks for one when the checkout's local environment file holds a feedback token beyond the example placeholder, and `deploy` asks for one when the `sender` service already runs. Every command that builds and starts the site therefore names the same services in the same order.

## The self-host setup commands

`self-host example` and `self-host real-database` run the two setup workflows inside the administration image, `self-host up` builds and starts the configured stack there, and the host bootstrap `scripts/self-host.sh` is the entry that runs them beside its own `down` verb. A terminal standard input reaches each workflow's own prompts, and standard input that is not a terminal holds one JSON object that validates as the workflow's closed input model. Fixed step names go to standard error, and the one report goes to standard output. [The example prompts](/docs/operations/first-run-setup.md#the-example-prompts) lists the prompts and [the machine input](/docs/operations/first-run-setup.md#the-machine-input) states the JSON input.

| Verb | Input fields |
|---|---|
| `example` | `serve_mode`, `instance_key`, `web_port`, `admin_web_port`, `postgres_port`, `embedding_port`, `app_url`, `admin_origin`, `username`, `password`, `confirm_download`, `publication_token` |
| `real-database` | `database_name` |
| `up` | None |
| `down` | None |
| `publish` | None |
| `unpublish` | None |

`self-host up` builds the images of `db`, `embedding` and the start list and starts all of them through the Compose bridge. It writes one step line naming that whole list, such as `start db, embedding, web, admin, sender, worker`. The build is unbounded, the start is bounded by `ADMIN_DATABASE_OPERATION_TIMEOUT_SECONDS`, and the command returns no report.

`down` is the bootstrap's own verb: it stops and removes the stack's containers and its network through the Compose bridge and leaves every volume and database in place. It runs no administration subcommand, so it reads no workflow input and writes no report.

`serve_mode` is `local` or `domain`. A domain-mode run writes the instance's site file into the shared site proxy project and starts and reloads that project, and a local-mode run writes no site file and starts no proxy. `instance_key` selects one entry of the finite university map, which lists the implemented source families. The four ports must differ, and each validates as a TCP port.

`app_url` is the feature site's external root address. In local mode an absent value derives `http://localhost:<web_port>/`, and in domain mode it is required and is an HTTPS address outside loopback without an explicit port. `admin_origin` is the administration backend's exact HTTPS origin, and an absent value derives `https://localhost:<admin_web_port>`. A domain-mode run refuses an absent address, a loopback address and an address that carries a port.

The shared directory is always the configured `ADMIN_SHARED_ROOT`. `password` and `publication_token` are hidden from the input model's representation and its serialized form, and a validation error never echoes their inputs. The two commands take no secret flag.

The two commands accept one closed input contract each, with a process runner and a writer. The report is transient: the address to open and the Selected dataset id, which is `null` for the example path.

`self-host example` copies the absent working files and applies the supplied instance key, ports, origins, feature-switch defaults and shared directory. It generates the two database passwords when it creates the environment file, creates the administration account when its leaf is absent, and preflights the container host. It then prepares the pinned artifact, builds and starts the database and embedding services, migrates and loads the example dataset, and prepares the feedback application store. The last steps build the start list's images, start those services, and prove both HTML shells and the bootstrap catalogue. A publication run adds the sender to that list, and a same-machine host adds the worker.

The workflow writes the embedding and inference switches off, and it writes the prerequisites switch on when the configured entry's [source family](/docs/reference/glossary.md#configuration) is `sisu` and off for every other family. A domain-mode run then writes the instance's site file in the shared proxy project's `sites/` directory, starts that project when it is absent and reloads the running Caddy.

`self-host real-database` enrolls the named empty database as Selected, builds its artifacts, re-creates the site container, and proves the Selected catalogue and identity. Both resolve the process runner once.

`publish` and `unpublish` read the configured instance key, `WEB_APP_URL`, the loopback site target and `ADMIN_SHARED_ROOT` through their owning loaders. `publish` writes the instance's site file, starts the proxy project when it is absent and reloads it, and `unpublish` removes that one site file and reloads a running project. Both refuse a proxy path that is not a regular file, `publish` refuses an absent or loopback site address and an address that carries a port, as a domain-mode setup does, and each prints one transient `PublicSiteReport(site_address, published)`.

The record of a self-host run is its output: the fixed step names on standard error and the one report on standard output. The bootstrap verbs run from `scripts/self-host.sh`, and the administration console offers no self-host action.

## The deploy command

```bash
python scripts/admin.py deploy
```

`deploy` takes no argument and runs one deployment of this checkout to its containers. The stage-idle guard runs first, so a busy stage refuses the command before it changes anything. The command then writes one step line per step to standard error, in this order:

- `check that every stage is idle`
- `build the database and embedding images`
- `start the database and embedding services`
- `enroll the served dataset`
- `migrate the selected dataset and build its serving artifacts`, or `migrate the configured database and build the example artifacts` on a bootstrap state root
- `build web, admin images`, which names the services the deployment builds
- `start web, admin services`, which names the same services
- `prove both HTML shells`
- `prune the superseded artifact builds`

Both site lines name the [start list](#the-start-list). They read `build web, admin images` and `start web, admin services`, and `sender` is appended when a sender runs, with `worker` last on a same-machine worker configuration. With a running sender and a same-machine worker the lines read `build web, admin, sender, worker images` and `start web, admin, sender, worker services`. A same-machine worker's image is built from this checkout, so the deployment builds and re-creates its container once, with the site and the administration services.

The enrollment step enrolls the configured database as Selected unless it is the example database, and the mode it returns selects the migration step. An enrolled site migrates the served dataset and builds its serving artifacts, and a bootstrap state root migrates the configured database and writes its example artifacts in place of the Selected steps.

The command takes no commit argument: the calling workflow admits the checkout at the tested commit before the body runs, and `deploy/deploy.sh` prints `deployed <commit>` after the owner returns. A failure stops the sequence and prints the last step line, the content-free error and the deploy guidance. The deployment's own record is the step lines it prints.

## The serving artifact command

```bash
python scripts/admin.py serving-artifacts [--dataset ID] [--prune]
```

`serving-artifacts` targets the Selected dataset, which is the configured database in bootstrap. `--dataset ID` targets a receipt-bearing dataset. The build and the prune take the same target options.

A build writes the catalogue artifact and the study fragments into one versioned build directory under the configured administrative state root and switches the artifact family's `current` build. On a site that serves the embedding features it also writes the hub pair and the shared stores into the family's embedding directory for the running configuration and replaces the dataset's five derived tables in the same transaction. `--prune` is the separate housekeeping action, and it runs after a restart. [Serving artifact build](/docs/backend/text-and-vectors/serving-artifact-build.md#the-artifact-family-the-build-directory-and-the-embedding-directory) states the files and tables of each embedding mode and the prune's own retention rule.

A failed command prints the content-free JSON line with `operator_guidance("serving-artifacts")` to standard error and returns exit status 1. That guidance names the embedding stage, the server database service, the retry and the site container re-create that a completed build needs. The error output carries the exception class name and the fixed guidance alone.

The build report holds `embeddings_enabled`, the catalogue and study-fragment paths, the seven study-fragment values and, on a site that serves the embedding features, the five embedding paths, both chunk counts, the centroid and neighbour counts, and the packed search group and block counts. Every embedding path and count is `null` on a site with the features off. A prune report names the retained build or a `null` active build and every path it deleted.

The embedding tool's `6` entry, **Build serving artifacts**, and the console preparation action call the same owner. The ingestion, translation and inference tools offer no artifact entry.

The files, the table transaction, the recovery of a failed build and the prune's own rules are described in [Serving artifact build](/docs/backend/text-and-vectors/serving-artifact-build.md). The supervisor opens the build once before it forks its workers, and on a site that serves the embedding features every similarity read takes its hub pair from that opened set. [Web supervisor](/docs/backend/interfaces/web-supervisor.md#startup-and-validation) states that opening and the supervisor lifetime, and [Operations](/docs/operations/README.md#derived-files-served-data-and-the-web-restart) states the restart a completed build needs and the prune that follows it.
