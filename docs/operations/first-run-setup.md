# First-run installation reference

This page states the first-run workflows' prompts, inputs, working files, instance resources and
addresses.

The refusals of those workflows and of the stack verbs, with the work a failed run leaves in place,
are listed in [First-run troubleshooting](/docs/operations/first-run-troubleshooting.md).

## The university map

The example workflow writes `INSTANCE_KEY` into `config/web.toml`, the one university instance the site presents, from the tracked university table in `universities.toml` at the repository root, each container and volume derives from that key, and the four ports are chosen values that default to the tracked example files. The key also selects the university's public curriculum source and course-page address. The table holds 36 entries in four source families: 35 name a real university, and the last names the fictional Example University the example configuration presents.

- The Sisu family holds 10 entries. Nine read the Sisu Kori API: Aalto, Helsinki, Jyväskylä, LUT, Hanken, Tampere, Arcada, LAB and Åland. The tenth is the fictional Example University, which names a fictional Kori base.
- Eight read the Peppi legacy public JSON API: Turku, Eastern Finland, Oulu, Vaasa, Vaasa UAS, Åbo Akademi, Lapland and the University of the Arts Helsinki.
- Three read the Peppi Solenovo PSG public JSON API: Centria, HAMK and SAMK.
- The remaining 15 read the server-rendered Peppi guides, six of them over plain HTTP: Diak, Humak, JAMK, Kajaani University of Applied Sciences, Laurea and Novia. The nine others read the guides through a headless browser: Metropolia, Seinäjoki, TAMK, Lapland UAS, Oulu UAS, Haaga-Helia, Karelia, Savonia and Turku UAS.

## The bootstrap

`scripts/self-host.sh` is the one host entry. It needs Docker, Docker Compose and Git, and runs every workflow and every container action inside the administration image with the checkout mounted at its own absolute path.

```bash
scripts/self-host.sh example [--shared-root PATH]
scripts/self-host.sh real-database [--shared-root PATH]
scripts/self-host.sh up [--shared-root PATH]
scripts/self-host.sh down [--shared-root PATH]
scripts/self-host.sh publish [--shared-root PATH]
scripts/self-host.sh unpublish [--shared-root PATH]
scripts/self-host.sh deploy [--shared-root PATH]
```

The bootstrap checks Docker, Docker Compose, the Docker daemon, Git in the checkout and write access to `/var/run/docker.sock` before anything else, and before the model download, the image builds or any Compose command. A missing item refuses the run with one message that names it. A socket the account cannot write refuses with the `docker` group guidance. The bootstrap never runs `sudo`.

The shared directory is `--shared-root PATH`, otherwise `COURSE_EXPLORER_SHARED_ROOT`, otherwise `$HOME/.local/share/course-explorer`. An interactive `example` or `real-database` run asks `Shared directory [PATH]: ` and takes the default on an empty answer. Those two verbs and `publish` and `unpublish` prepare the resolved directory, mount it and pass it to the image. `publish` and `unpublish` require `--shared-root` or `COURSE_EXPLORER_SHARED_ROOT`. `up`, `down` and `deploy` pass a supplied value as an override without creating or mounting it, and with no supplied value they read `ADMIN_SHARED_ROOT` from `config/database.toml`. The bootstrap refuses a relative path and a path inside the checkout, and it creates the directory and its parents for the verbs that prepare it.

The bootstrap builds the administration target of `docker/server/Dockerfile` into a setup image whose tag carries a digest of the checkout path, so two checkouts on one daemon never share it. `example`, `real-database`, `up` and `deploy` build the image, and `down`, `publish` and `unpublish` use the image the checkout already has. The image runs with the host network namespace, the account's uid and gid, the Docker socket's group, the checkout mounted at its host path, the shared directory for the verbs that pass it, and the Docker socket. The bootstrap creates the account's `~/.ssh` directory at mode 0700 when it is absent, and passes it and the host home to the image. A Compose build of the same administration target reuses the image's layers.

Each verb runs one fixed command:

| Verb | Runs |
|---|---|
| `example` | `python scripts/admin.py self-host example` |
| `real-database` | `python scripts/admin.py self-host real-database` |
| `up` | `python scripts/admin.py self-host up`, which builds the four core services, any configured sender and a same-machine worker, then starts them through `course_explorer_admin.compose` |
| `down` | `down` through `course_explorer_admin.compose`, which leaves every volume and dataset in place |
| `publish` | `python scripts/admin.py self-host publish` |
| `unpublish` | `python scripts/admin.py self-host unpublish` |
| `deploy` | `python scripts/admin.py deploy` |

Every verb runs the bootstrap's Docker, Compose, daemon, Git and socket checks before the image starts.

### The deploy verb

`deploy` runs one container deployment of the checkout it runs in and takes no input. It refuses while a stage is not idle, before it builds any service image, and its first failing step stops the sequence and reports the failure, so a step that did not run changes no container and no image.

The deployment reuses a valid Selected dataset and enrolls the configured database on a bootstrap state root, unless that database is the configured example database. An enrolled site migrates the served dataset and builds its serving artifacts, and a bootstrap site migrates the configured database and writes the example artifacts. [The deploy command](/docs/backend/interfaces/self-host-and-deploy-commands.md#the-deploy-command) states the step sequence, the enrollment of the served dataset and the services the deployment builds and starts.

## What the workflows write

The example workflow writes the working files in the table below and, when it creates the environment file, both generated database passwords. It also writes the chosen instance key, the four ports, the two origin addresses, the feature-switch defaults and the resolved shared directory into their subsystem files. The real-database workflow requires those working files and the two running services, and it writes one named database, that database's initial Selected receipt and pointer, and its serving artifacts, then re-creates the site container. The copy starts from the tracked example assets under `examples/` and needs no manual file edit for the example path.

| Working path | Holds |
|---|---|
| `config/` | The registered subsystem files, the feedback definition and the wiki page selection, copied from `examples/config/` |
| `.env` | The generated curriculum database password, the generated feedback application store's database password, and the optional feedback sender token, published at mode 0600 |
| `docker-compose.yml` | The PostgreSQL database with pgvector, the embedding service, the site, the administration backend, the feedback sender and the same-machine worker |
| `ADMIN_SHARED_ROOT/proxy/` | The machine-wide site proxy project: its Compose file, its top Caddyfile and one site file per published instance, created by a domain-mode run or the `publish` command |
| `config/database.toml` | The resolved `ADMIN_SHARED_ROOT`, written beside the database port |
| `data/admin-web/password` | The administration account's username and argon2id hash, written only when the account is absent |
| `ADMIN_SHARED_ROOT/models/bge-m3-q8_0.gguf` | The pinned embedding model, checked against `EMBEDDING_ARTIFACT_SHA256` |
| `ADMIN_SHARED_ROOT/locks/` | The machine-wide checkout, server and worker reservation files |

Template materialization writes only an absent working file. The example workflow then writes its own keys into the subsystem files named in [The instance resources](#the-instance-resources). The embedding model, the reservation files and the site proxy project live in the machine-wide shared directory outside the checkout, and a relative `EMBEDDING_ARTIFACT_PATH` resolves inside it. A symlinked or non-regular working path is refused rather than followed. A present `config/` directory is an ordinary directory that receives the absent templates.

## The example prompts

```bash
scripts/self-host.sh example
```

The bootstrap asks for the shared directory, then the workflow asks its own questions. Each port defaults to the value in the tracked example files, and the four chosen values must differ.

| Prompt | Answer |
|---|---|
| `University` | One numbered entry of the implemented map, or `0` to cancel |
| `Feature web port [PORT]: ` | The feature site's loopback port |
| `Administration port [PORT]: ` | The administration backend's loopback port |
| `Database port [PORT]: ` | The PostgreSQL service's loopback port. The feedback application store uses the same port. |
| `Embedding port [PORT]: ` | The embedding service's loopback port |
| `Serve mode` | **1** **Local**, which serves the site on its loopback ports with no proxy, or **2** **Domain**, which publishes it through the machine-wide site proxy |
| `App URL [ADDRESS]: ` | The local site's external root address, or an empty answer for `http://localhost:<web_port>/`. Asked in local mode. |
| `Site HTTPS address: ` | The domain's HTTPS root address. Asked in domain mode, and it is an address outside loopback without an explicit port. |
| `Administration origin [ORIGIN]: ` | The administration backend's exact HTTPS origin, or an empty answer for `https://localhost:<admin_web_port>` |
| `Administration username (0 to cancel): ` | The administration account's username |
| `Download the pinned embedding model if it is absent?` | **1** to allow the download, **0** to refuse it |
| `Set up feedback publication?` | **1** to configure the sender, **0** for no publication |
| `Feedback publication token: ` | The sender token, read hidden, only when publication is chosen |
| `Administration password: ` and `Repeat administration password: ` | The account's password, read hidden twice |

The workflow copies the absent working files, writes the instance and device values, generates both database passwords, creates the administration account, preflights the container host, prepares the pinned model, builds the database and embedding images, starts those two services, migrates and loads the example dataset, prepares the feedback application store, builds and starts the site and administration services, and proves both HTML shells and the bootstrap catalogue. Those services are `web` and `admin`, with the feedback sender of a publication run and the worker of a same-machine host. A domain-mode run then writes the instance's site file into the shared proxy project, starts that project and reloads it.

Every service the workflow starts runs under the Compose restart policy, so it starts at boot with the Docker daemon and returns after a failure. [Starting, stopping and updating a copy](/docs/operations/server-containers.md#starting-stopping-and-updating-a-copy) lists the services that `up` starts and what `down` leaves in place. Fixed step names go to standard error and the one report goes to standard output.

## The machine input

The machine command reads one JSON object from standard input and takes no secret flag:

```bash
read -rsp 'Administration password: ' ADMIN_PASSWORD
printf '%s\n' "{\"serve_mode\":\"local\",\"instance_key\":\"example\",\"web_port\":8037,\"admin_web_port\":8039,\"postgres_port\":5433,\"embedding_port\":8081,\"username\":\"OPERATOR\",\"password\":\"${ADMIN_PASSWORD}\",\"confirm_download\":true}" | scripts/self-host.sh example --shared-root /srv/course-explorer
unset ADMIN_PASSWORD
```

| Field | Holds |
|---|---|
| `serve_mode` | `local` or `domain`. A domain run writes the instance's site file into the shared proxy project and starts and reloads that project, and a local run starts no proxy. |
| `instance_key` | The one university instance the site presents, as a key of the table in `universities.toml` |
| `web_port` | The feature site's loopback port |
| `admin_web_port` | The administration backend's loopback port |
| `postgres_port` | The PostgreSQL service's loopback port. The feedback application store uses the same port. |
| `embedding_port` | The embedding service's loopback port |
| `app_url` | The feature site's external root address. In local mode an absent value or `null` derives `http://localhost:<web_port>/`. Domain mode requires it, and it is an HTTPS address outside loopback without an explicit port. |
| `admin_origin` | The administration backend's exact HTTPS origin. An absent value or `null` derives `https://localhost:<admin_web_port>`. |
| `username` | The administration account's username |
| `password` | The administration account's password. It is hidden from the model's representation and its serialized form. |
| `confirm_download` | Whether an absent pinned model may be downloaded |
| `publication_token` | The optional feedback sender token. It is hidden from the model's representation and its serialized form. |

The four port values must be four distinct numbers. A value outside the TCP range, a key outside the university map and a malformed origin each fail the input validation before the workflow runs. A domain-mode run refuses an absent site address, a loopback site address and a site address that carries a port. A terminal standard input reaches the workflow's own prompts, and standard input that is not a terminal must hold the JSON object.

The container-host preflight refuses a loopback port that another process holds, except the database port or the embedding port of a container this project already runs. The browser and the native libraries come from the images, so the host check has no browser or library item.

The workflow refuses an example path whose `POSTGRES_DB` differs from `EXAMPLE_POSTGRES_DB`, and it refuses a state root that holds a dataset pointer or an enrollment record. It creates the account only when `data/admin-web/password` is absent, so a second run does not reset the account. The second run refuses at the example database's user-table check before it imports anything.

## The real-database workflow

This workflow creates one named empty database in the running container, enrolls it as the initial Selected dataset, builds its serving artifacts, re-creates the site container, and proves its identity and catalogue. It does not edit `POSTGRES_DB`, and the example database is left in the container.

```bash
scripts/self-host.sh real-database
```

| Prompt | Answer |
|---|---|
| `Database name [course_explorer_real]: ` | The named database, or an empty answer for the default |

```bash
printf '%s\n' '{"database_name":"course_explorer_real"}' | scripts/self-host.sh real-database
```

The workflow needs the working files, the running database and embedding services, and every stage idle. It reads the site's served identity before any mutation, and it re-creates the site container after the pointer switch.

The named enrollment requires a state root with no pointer and no enrollment record. The configured example name, a target database the service already holds and a protected name each refuse before mutation. A failed create or migration preserves whatever it created for explicit maintenance.

The empty database builds and serves with the feature switches the example workflow wrote: embedding and inference off, and prerequisites on only for a Sisu source. It holds no course, module or derived data, so its catalogue, search and study-structure results are empty. The same embedding switch turns the recommendation feature off, so `/api/recommendations` answers `403` with `Feature disabled`.

A repeated call refuses inside the named enrollment owner before it creates another database, migrates, builds or re-creates a server. The example database is not promoted, copied or pruned.

## The instance resources

The chosen key supplies the two university display names, and it derives the Compose project `course-explorer-<key>`, the volume namespace, the built database image and the container names. Compose names the built database image after the project and service, so the example key builds `course-explorer-example-db`. Each checkout owns its own Compose project, database volume, built database image, four ports, data directories and dataset state, so several instances run side by side without sharing a database or a checkout. [Self-hosting](/docs/getting-started/self-hosting.md#several-instances-on-one-server) states the separation.

The workflow writes the four ports to their owning subsystem files and writes the same PostgreSQL port to `FEEDBACK_POSTGRES_PORT`. It writes `WEB_EMBEDDINGS_ENABLED = false` and `WEB_INFERENCE_ENABLED = false`. It writes `WEB_PREREQUISITES_ENABLED` from the selected source family: on for Sisu, which publishes official prerequisites, and off for every other family, including the guide family. The example load imports the fixture's bundled vectors, and the operator turns a switch on when a site's data and serving artifacts exist.

### The shared directory

The shared directory is one machine-wide path outside the checkout. The workflow resolves the configured `ADMIN_SHARED_ROOT`, which accepts an absolute path, a bare `~` that resolves to the host home directory, or a path beginning with `~/`, writes the resolved value to `config/database.toml`, and creates the directory with its parents when it is absent, together with its `models/` and `locks/` directories at mode 0700. A present directory is adopted with its other contents and permissions as they are. A blank, relative or in-checkout path fails before the services start. The pinned embedding model is written into `models/` there, and the reservation files of the stages, the Working dataset owners and the site rebuild live in `locks/`. A domain-mode run creates the site proxy project in `proxy/` there.

## Editing a working file

The example workflow materializes the absent working templates in the table above from the tracked examples, writes the chosen instance and device values, and leaves any other present working file byte for byte. To change a value, save it with the configuration command from the administration container, which takes one JSON literal per key, and restart the container that reads it.

```bash
docker exec PROJECT-admin-1 python scripts/admin.py config set config/web.toml WEB_PORT 8037
docker restart PROJECT-web-1
```

`PROJECT` is the instance's Compose project, for example `course-explorer-example`. [Setup and preflight](/docs/operations/setup-and-preflight.md#configuration) states the command in full. A second run of the example workflow refuses, because the example database already holds tables.

The workflow creates the feedback role and database, applies the feedback chain and writes the attachment root at `FEEDBACK_ATTACHMENTS_ROOT`.

## The address and HTTPS

The serve mode decides the proxy arrangement. Local mode starts no proxy: the site answers on its loopback port, the administration backend holds its loopback bind and exact HTTPS origin, and any external site address belongs to the operator's own proxy. The example address is loopback HTTP, the only address at which the feature site accepts plain HTTP. A blank app address in local mode derives `http://localhost:<web_port>/`, and a blank administration origin derives `https://localhost:<admin_web_port>`.

Domain mode publishes the site through the machine-wide site proxy project in `ADMIN_SHARED_ROOT/proxy/`. The workflow writes the instance's site file from `app_url`, starts the project when it is absent and reloads the running proxy. Caddy supplies the domain's certificate and the HTTP-to-HTTPS redirect on ports 80 and 443. The instance's site file carries the proxy's configured bind address when `PROXY_BIND_ADDRESS` sets one. The console is loopback-only in both modes and is reached through the operator's own HTTPS proxy. [Self-hosting](/docs/getting-started/self-hosting.md#the-machine-wide-site-proxy) states the project and [Removing a public site](/docs/getting-started/self-hosting.md#removing-a-public-site) the removal.

A domain-mode address is an HTTPS address outside loopback without an explicit port. The preflight reads the host's listening table on the configured bind address and the wildcard address, or on every address when none is configured, and a listener on port 80 or 443 other than the running proxy refuses the run. Each external origin may name a proxy port that differs from its backend's loopback port.

## Check the running copy

Open `http://127.0.0.1:8037`, the address from `WEB_HOST` and `WEB_PORT` in `config/web.toml`. The top bar shows the tabs the site serves, with **Docs** beside them. `WEB_EMBEDDINGS_ENABLED = false` in `config/web.toml` hides the **Compare** tab and the comparison controls. `WEB_INFERENCE_ENABLED = false` hides the **Inferred** controls and mark. `WEB_PREREQUISITES_ENABLED = false` hides the **Prerequisites** tab and Next courses, and it hides **Discover** when embeddings are also off. **Docs** opens the documentation wiki inside the app, and a page the configuration does not select opens on GitHub.

The browser, console and Python checks are listed in [Local checks](/docs/testing/local-checks.md).

