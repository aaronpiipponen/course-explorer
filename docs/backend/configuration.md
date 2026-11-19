# Configuration

course-explorer reads non-secret values from the subsystem files and credentials from the environment file, and fails a settings read when a required value is missing or malformed.

The repository holds one subsystem file per configuration subsystem under `config/`. Credentials live outside those files, in the environment file at the repository root. One [settings owner](/docs/reference/glossary.md#configuration) composes both, so an ordinary command reads its configuration with no manual sourcing step.

## Where values live

The ten subsystem files under `config/` each own the non-secret settings of one subsystem. `config/wiki.json` selects which pages the documentation wiki renders, and the feedback definition lives in `config/feedback-form.json`. The embedding hub vectors are files of a serving artifact build, not configuration.

| File | Owns |
|---|---|
| `config/curriculum.toml` | Raw capture location and checkpoint policy, the shared source request and normalization policy, and the local ingestion controller state root |
| `config/database.toml` | PostgreSQL connection components, the configured example database name, the administrative state root, the machine-wide shared directory, and the optional machine-wide site proxy bind address |
| `config/search.toml` | The embedding runtime and generation contract, the local embedding controller state root, and the search, comparison, recommendation, and route policy |
| `config/translation.toml` | The translation runtime and provenance contract, the published artifact source or local conversion, the translation controller state root and health attempts, and the shared worker health interval and stop timeout |
| `config/inference.toml` | The inference runtime contract, the per-key model and artifact pins of the local fit keys, the candidate selection and rerank policy, and the inference controller policy |
| `config/worker.toml` | The worker host kind, SSH target, WSL distribution and checkout path, the GPU backend, target and card index, and the worker connection, setup, dataset, and local lifecycle policy |
| `config/admin_web.toml` | The Administration backend's loopback bind, exact HTTPS origin, session lifetimes, anonymous session creation limit, command-log entry limit, console poll interval, and the GitHub repository address |
| `config/feedback.toml` | The feedback application store connection, the attachment root, the destination repository and visibility, the GitHub attempt timeout, and the submission allowances and body bound |
| `config/web.toml` | The configured university instance key and optional display label, the external app address shared by the administration App link and the feedback exact-site boundary, the web bind, process count and malloc arena cap, child respawn bounds, browser-drawing limit, the three served feature switches, the two retained-cache byte regions, the four request-input bounds, and the usage-record retention |
| `config/admission.toml` | Admission control of the read-only API: the serving switch, the load score that places the site in a regime, the counter lifetimes, the per-user light and heavy limits, the heavy queue caps, and the cost curves |

The tracked templates for these paths live under `examples/`. The `self-host example` workflow copies every absent template from `examples/` into its working path and keeps every present file as it is, and an operator edits the resulting working files through the configuration command in the administration container. Git ignores these working copies, so each checkout carries its own settings without changing the templates. The tracked model presets under `examples/config/presets/` are `8gb`, `12gb` and `16gb`, and each holds the model and artifact values that the worker install writes into a working `config/inference.toml` and `config/translation.toml`. Each preset names two generation files. The 16 GB preset names the Qwen3.8-27B IQ3-MIX file and the Qwen3.8-27B UD-IQ2_S file. The 12 GB preset names the Qwen3.8-27B UD-IQ2_S file and the Gemma 4 12B Q3_K_M file. The 8 GB preset names the Gemma 4 12B Q3_K_M file and the Gemma 4 12B Q3_K_S file.

The 16 GB and 12 GB presets name the published `Q5_K_M` translation file, and the 8 GB preset names the published `Q2_K` file. The preset values are tabled in [Model presets](/docs/getting-started/model-presets.md), and the quick path is described in [Installation](/docs/getting-started/installation.md).

The `self-host example` workflow copies every absent template and leaves every present working file as it is at that step. It writes the instance key, the four ports, the feature app address, the administration origin, the embedding and inference switches off, the prerequisites switch from the selected source family and the resolved shared directory. It runs inside the administration image, which is why it needs no host interpreter. It writes the optional `FEEDBACK_GITHUB_TOKEN` when publication is requested. When it creates the environment file, it generates `POSTGRES_PASSWORD` and `FEEDBACK_POSTGRES_PASSWORD` with `secrets.token_urlsafe(32)` and publishes the file at mode 0600. Every other value comes from the copied example file. A supplied publication token refuses when the environment file already exists, because that file owns its sender credential.

The `self-host real-database` workflow copies nothing. It refuses when a prepared working file is absent, so the example workflow prepares the working files first.

Each subsystem file holds top-level keys whose names are the settings the code reads. A key defined in two files is rejected before any setting loads. Environment keys compare case-insensitively, matching the runtime composition, so one key written in two cases is refused as one key assigned twice.

![The example files are copied at setup into the subsystem files and the environment file. The settings owner reads the subsystem files, the environment file, whose values the process environment overrides, and the university map loaded from the university table at package load, and hands settings to the web server, the stages and the Compose environment file, which feeds Docker Compose. The Administration backend loads its own family from `config/admin_web.toml` in its own process, and a serving artifact build writes the hub pair that the web server reads.](/docs/diagrams/configuration-sources.svg)

The Administration backend loads its own family from `config/admin_web.toml` in its own process. No administration key reaches the serving web process, whose launcher composes its own families alone.

`INSTANCE_KEY` in `config/web.toml` selects the one university the site presents. The tracked [university table](/docs/reference/glossary.md#configuration) at the repository root is the sole owner of each key's English and Finnish display names, its display label, its curriculum source attribution statements, its source family and source block, its language policy, and its study-plan import script and the formats the site offers. The language policy holds the ordered official languages, the display fallback for each interface language, the translation priority map and the inference language and origin list.

An entry may also set `inference_target_fields` and `inference_pool_fields`, each an array of official text field names in canonical order, to override the prompt fields its inference runs carry. An entry that omits them uses the name, content, outcomes and prerequisite-text target and the name, content and outcomes pool.

The table is shared product data rather than a configuration subsystem file, so every site reads the same one. The loader in `src/course_explorer/instance.py` reads it at package load and stops with one clear error naming the file, the entry key and the field when a required value is missing or malformed. An entry names its source family in `source_family` and carries exactly one source block for it, either the Sisu facts, the Peppi legacy facts, the Peppi PSG facts or the guide facts. A value outside the table fails the settings read.

`WebSettings`, `AdminWebSettings`, `KoriSettings` and `NormalizationSettings` inherit the instance family, so both serving processes and the ingestion source report the same key, and the Compose project derives from it. Each of those settings reads resolves the configured key through the map loaded once per process. [Settings loading](/docs/backend/settings-loading.md) names those classes and the source of each value.

The key is an ordinary key of the configuration editor. The source block and the language policy are not keys of any subsystem file, while `config/curriculum.toml` holds the operator's request and normalization policy. `INSTANCE_DISPLAY_LABEL` is an optional lowercase brand label, and the read-only `display_label` property returns that override or the configured university's map label. Both apps render `course-explorer/<display label>`. The Compose project and every container name carry the configured key.

## Deciding between a subsystem file and the environment file

One test decides where a value belongs. The ignored environment file holds credentials only: passwords, tokens and other actual secrets. Every non-secret value, including machine and site identity, goes in the subsystem file that owns the value.

The ignored environment file holds the two database passwords and any provider credential the site uses. The tracked template `examples/.env.example` carries credential placeholders:

| Key | Value |
|---|---|
| `POSTGRES_PASSWORD` | The curriculum database password |
| `FEEDBACK_POSTGRES_PASSWORD` | The password of the feedback application store |
| `FEEDBACK_GITHUB_TOKEN` | The GitHub credential that only the feedback sender reads |

Worker identity is non-secret subsystem configuration. `config/worker.toml` holds `WORKER_HOST_KIND`, `WORKER_SSH_TARGET`, `WORKER_WSL_DISTRIBUTION` and `WORKER_PROJECT_ROOT`: the host kind, how the administration command reaches the worker, the WSL distribution that runs the stage, and the worker checkout location. `WORKER_HOST_KIND` is `wsl` for a Windows host that runs the stage inside a WSL distribution, `linux` for a native Linux host reached over SSH, or `same-machine` for the instance's worker container. A `wsl` worker requires `WORKER_WSL_DISTRIBUTION`, and the other kinds refuse it. A `wsl` or `linux` worker requires `WORKER_SSH_TARGET`, and a `same-machine` worker refuses it. The target and the distribution are each 1 to 64 ASCII letters, digits, underscore, dot or hyphen. The project root is an absolute POSIX path without dot components, a WSL worker's path carries no percent characters, and a same-machine worker's path is its container path, `/app` in the worker image.

The same file holds the worker's GPU values. `WORKER_GPU_BACKEND` selects the llama.cpp build and the model child's device variable, `WORKER_GPU_TARGET` is its build target, and `WORKER_GPU_DEVICE_INDEX` is the vendor index of the card the model servers use. `rocm` requires a ROCm compute target such as `gfx1201`, `cuda` requires a two- or three-digit compute capability such as `86`, and the index is an integer from 0 to 15.

The two rebuild scopes apply a saved file differently. The App rebuild builds the site image and re-creates the site container, so a save that changes a value of the serving process takes effect at that step. The administration rebuild builds the administration image and starts the detached one-off that re-creates the administration service, so a save that changes a value of that backend takes effect at that step. A saved file also reaches a process when its container restarts for any other reason.

> [!WARNING]
> The database passwords belong in the ignored environment file. Subsystem files hold non-secret settings, and their tracked templates must be credential-free.

Every other value is non-secret. The two embedding hubs are not settings: a similarity read opens the current serving artifact build and reads the pair it carries. A read that finds the build or a hub file missing or malformed fails. The pages linked below name the keys a reader is most likely to change.

## Automatic loading

A process reads the subsystem files and the environment file when it builds its settings, so no shell export step is needed. When the same key is set in more than one place, the process environment takes precedence over the environment file, and the environment file takes precedence over the subsystem files. A missing required value or an invalid value fails the read with a clear error.

Every settings family validates its own values before it is returned. A bind address other than `127.0.0.1`, an academic year bound that is not two consecutive four-digit years, a port outside 1 to 65535, a worker path that is not absolute, a worker host kind without its required distribution or with a refused one, a GPU target that does not match the configured backend's form, a state root that is not repository-relative, an artifact path that is blank or whose basename differs from its configured file, a translation artifact source with only some of its three keys set, a shared directory that is blank or relative, an administration origin that is not one exact `https://` origin without credentials, path, query or fragment, or an app address with credentials, a path other than the root, a wildcard, whitespace, query or fragment all fail at load.

The inference variant and rerank artifact paths accept an absolute path or a checkout-relative path, while state roots are repository-relative. The settings classes and the composition order are described in [Settings loading](/docs/backend/settings-loading.md).

## The administration backend's own file

`config/admin_web.toml` holds the 10 administration keys of the Administration backend. Every key is validated and required, so a site states its own bind and origin. The console's App link reads `WEB_APP_URL` from `config/web.toml`.

| Key | File | Effect |
|---|---|---|
| `ADMIN_WEB_HOST` | `config/admin_web.toml` | The loopback address the backend binds. The example sets `127.0.0.1`. |
| `ADMIN_WEB_PORT` | `config/admin_web.toml` | The valid TCP port the backend binds. The example sets 8039. |
| `ADMIN_WEB_ORIGIN` | `config/admin_web.toml` | The exact external HTTPS origin the backend answers for, compared by scheme, host and port. The example sets `https://localhost:8039`. |
| `ADMIN_WEB_SESSION_IDLE_MINUTES` | `config/admin_web.toml` | The idle lifetime of one server-side session. The example sets 30. |
| `ADMIN_WEB_SESSION_MAX_HOURS` | `config/admin_web.toml` | The total lifetime of one session from its creation. The example sets 12. |
| `ADMIN_WEB_ANONYMOUS_SESSION_LIMIT` | `config/admin_web.toml` | The fresh anonymous sessions one backend process admits in the window, as a positive integer count. The example sets 10. |
| `ADMIN_WEB_ANONYMOUS_SESSION_WINDOW_SECONDS` | `config/admin_web.toml` | The length of that server-wide sliding window in seconds, as a positive integer. The example sets 60. |
| `ADMIN_WEB_COMMAND_LOG_LIMIT` | `config/admin_web.toml` | The entries the command log retains. The example sets 200. |
| `ADMIN_WEB_POLL_SECONDS` | `config/admin_web.toml` | The interval the console waits after one completed panel read before its next read of that panel. The example sets 60. |
| `ADMIN_WEB_REPOSITORY_URL` | `config/admin_web.toml` | The HTTPS GitHub owner and repository address the console's GitHub link opens. |

`ADMIN_WEB_ANONYMOUS_SESSION_LIMIT` and `ADMIN_WEB_ANONYMOUS_SESSION_WINDOW_SECONDS` set the anonymous session creation limit for a backend process, shared by every client. A fresh anonymous session granted at `GET /auth/session` consumes one count, and a live anonymous or signed-in session consumes none. The counted creation times live in the process's memory, so a backend restart clears the count. [Console access and records](/docs/operations/admin-console/access-and-records.md) states the response when the limit is reached.

The Administration backend reads this family through the same settings owner, and the course-exploration launcher composes its own families alone, so no administration key reaches the serving web process. The Administration lock path and its failed-attempt limit are fixed in code rather than configuration, as [Console access and records](/docs/operations/admin-console/access-and-records.md) states.

## The feedback application store

`config/feedback.toml` holds the feedback application store connection, the attachment root, the destination metadata, the GitHub attempt timeout and the intake policy. The connection names a separate database from the curriculum store, so dataset replacement, reset, import, worker round trips and rotation never reach feedback records. `FEEDBACK_POSTGRES_HOST`, `FEEDBACK_POSTGRES_PORT`, `FEEDBACK_POSTGRES_DB`, `FEEDBACK_POSTGRES_USER`, `FEEDBACK_ATTACHMENTS_ROOT`, `FEEDBACK_GITHUB_REPOSITORY`, `FEEDBACK_GITHUB_VISIBILITY`, `FEEDBACK_GITHUB_TIMEOUT_SECONDS`, `FEEDBACK_BROWSER_SUBMISSIONS_PER_HOUR`, `FEEDBACK_SITE_SUBMISSIONS_PER_HOUR` and `FEEDBACK_MAX_REQUEST_BYTES` are non-secret. `FEEDBACK_POSTGRES_PASSWORD` and `FEEDBACK_GITHUB_TOKEN` belong in the ignored environment file.

| Key | Effect |
|---|---|
| `FEEDBACK_POSTGRES_HOST` | The host of the feedback application store. The example sets `127.0.0.1`. |
| `FEEDBACK_POSTGRES_PORT` | The valid TCP port of the feedback application store. The example sets 5433. |
| `FEEDBACK_POSTGRES_DB` | The name of the feedback application store. |
| `FEEDBACK_POSTGRES_USER` | The user of the feedback application store. |
| `FEEDBACK_ATTACHMENTS_ROOT` | The directory that holds stored image files. A relative path resolves against the checkout root, an absolute path is used as written, and a leading `~/` resolves against the home directory. |
| `FEEDBACK_GITHUB_REPOSITORY` | The destination in `owner/repository` form, never a URL. Feedback intake reports it and the sender publishes to it. |
| `FEEDBACK_GITHUB_VISIBILITY` | The configured destination visibility: `public` or `private`. |
| `FEEDBACK_GITHUB_TIMEOUT_SECONDS` | The bound of one GitHub issue creation attempt, as a positive finite second count, 120 in the example. |
| `FEEDBACK_BROWSER_SUBMISSIONS_PER_HOUR` | The admitted submission attempts of one browser in one rolling hour. A positive count, 5 in the example. |
| `FEEDBACK_SITE_SUBMISSIONS_PER_HOUR` | The admitted submission attempts of the whole site in one rolling hour. A positive count, 30 in the example. |
| `FEEDBACK_MAX_REQUEST_BYTES` | The bound of one submission request body, covering a declared and a streamed body. A positive byte count, 25165824 in the example. |

The front router compares a submission's single Host and Origin with the exact origin derived from `WEB_APP_URL` in `config/web.toml`, without the address's optional root slash. The feedback definition read reports the configured repository, visibility and body bound.

The feedback destination family owns the repository and visibility for both feedback families. The intake family adds the intake policy and the inherited app address, and needs no sender credential. The sender credential family owns `FEEDBACK_GITHUB_TOKEN` and `FEEDBACK_GITHUB_TIMEOUT_SECONDS`, and the token is a required nonblank secret. The sender family composes the destination and the credential for the sender process, so a full sender load validates both, while a destination save, a feedback definition read and an intake load need no token. [Settings loading](/docs/backend/settings-loading.md) names the classes and the composition order. The serving launcher composes neither sender family and forwards no token. A serving process receives no destination token, administration key or worker key.

`config/feedback-form.json` holds the feedback definition: the bilingual labels and options, each select option's optional `publicIssueByDefault`, the field kinds and limits, the unchosen select placeholder and the GitHub issue mapping. The array order is the authority for field order, and the feedback definition is read fresh when the feedback service is composed. The file is edited directly and is not part of the one-key TOML editor. Each stored image is written beneath `<attachments-root>/<field-id>/<feedback-id>/<generated-filename>`, and the feedback service reads and removes those files. The submitted display filename is kept in the attachment row alone and never names the stored file.

The same feedback definition drives the operator reads. Every field of kind `select` provides one listing filter in the feedback definition's order, and a supplied filter must name one of that field's current option values. Every field of kind `text` or `textarea` is searched identically, as one case-insensitive literal substring, and the feedback definition's order sets which matching field groups a submission first. A field the feedback definition does not carry provides no filter and no search, and stored answers of another field ID are outside both reads.

[Tables and migrations](/docs/backend/catalogue/tables-and-migrations.md#the-feedback-application-store-schema) describes the feedback tables, their own metadata and the separate migration chain.

## Reading and writing a file

The administration command reads one configuration file and saves one key of it. A read of a subsystem file returns the values that file holds, so a TOML table is returned as that table rather than resolved. A read of the environment file returns every assignment in plain text, as the file spells it.

A save names one configuration file, one key, and one JSON literal, such as setting `WEB_PORT` of `config/web.toml` to `18026`. A subsystem file takes only a key it already carries, while the environment file also accepts an absent credential alias that a settings family owns. `CURRENT_ACADEMIC_YEAR` in `config/curriculum.toml` is an ordinary key of that editor, and so are `INSTANCE_KEY`, `WEB_APP_URL`, `WEB_EMBEDDINGS_ENABLED`, `WEB_INFERENCE_ENABLED`, `WEB_PREREQUISITES_ENABLED` and `USAGE_RETENTION_DAYS` of `config/web.toml`, the four worker identity keys of `config/worker.toml`, the admission keys of `config/admission.toml`, and the ten administration keys of `config/admin_web.toml`.

The literal matches the broad type the key holds: a boolean, an integer, a number, a string, an array or a table. An unlisted file, a key a subsystem file does not carry, a key the environment file does not carry and no settings family owns, JSON `null`, a non-finite number, a value of the wrong broad type and a value the owning settings family refuses all fail before the file changes. A malformed environment file, and an assignment repeated in it under another case, fail the save the same way.

A save renders only the edited key and validates the whole candidate through the settings families that own that key, so a value those families refuse changes no file. A subsystem file is published atomically, and an environment-file write is mode 0600. An environment file that is a single-file bind mount inside the administration container is rewritten through its own inode, so a crash during that rewrite can leave a truncated file. [Settings loading](/docs/backend/settings-loading.md#the-configuration-editor) describes the writers, the candidate sources, the environment-file read and the validation order.

An environment-file save preserves the spelling, whitespace, comments and line endings the file holds. A credential key that a settings family owns is writable whether or not the environment file already carries it, and an absent key is appended as one quoted assignment in the file's first line-ending style. A key that a settings family owns and that is not a credential is refused when the file does not carry it. A key of the environment file that no settings family owns is writable only when the file already carries it.

A `config set` invocation writes one key, and the operator menu's configuration entry does the same, so a file with several edits takes one save per key through the command. [The configuration editor](/docs/operations/admin-console/settings.md#configuration-editor) states how the console saves several changed keys.

A save of `CURRENT_ACADEMIC_YEAR` or `ACTIVE_ACADEMIC_YEAR_WINDOW` also refreshes the stored activity context of every course in every present dataset role. The save takes the datasets action lock and requires every stage idle before it writes the file. It then reloads the normalization settings and updates Selected and Working when present. A deployment with no Selected role updates its configured bootstrap database. The refresh creates no course, membership, import, result, pointer or receipt. A file save the settings family refuses writes no database. A database failure after the file is written propagates and leaves the saved key in place, and repeating the same save applies the idempotent refresh again. A running process uses the saved policy after the serving-artifact build and web restart that any saved value needs. An operator who edits the file directly applies the same value once through this command before rebuilding or restarting.

A file value and the effective value of a running process are different things. A process composes its settings when it builds them, and the process environment wins over the environment file, which wins over the subsystem files. A saved file reaches a process when that process next builds its settings, so restart a serving copy after a save that changes a bind, a port, a credential or a served feature switch.

The App rebuild runs four steps in order: it builds the site image, re-creates the database and embedding containers, re-creates the site container, and proves that container's loopback bind. A save that changes a serving-process value takes effect at the site container's re-create, and the bind proof follows it. The administration rebuild applies a save of the administration backend's own values when it restarts that backend.

## The Compose environment file

The administration command runs Docker Compose with a Compose environment file. The file maps the raw database values, the embedding runtime values and the instance key onto the keys the Compose file interpolates, and it is written to a mode-0700 directory as a mode-0600 file. The derived `COURSE_EXPLORER_PROJECT_NAME` is `course-explorer-<instance key>`, and the example Compose file takes its project name from it. Each Compose run gets its own file, and the command deletes the file and its directory when it returns. The administration side of each machine generates its own file, so the connection values reach only the machine that runs Compose. [Settings loading](/docs/backend/settings-loading.md#the-compose-environment-file) describes the generator and the quoting of the values.

A non-secret container setting lives in `config/*.toml`, never in an exported shell value, because the container reads the file while the bridge projects it. The two database passwords reach the site through the bridge, which projects the effective value, so an exported password is the value the site receives.

## What the web server receives

The web server process receives only the settings it needs: the raw database settings, the normalization settings, the search settings, the web settings, the admission settings and the feedback connection and intake settings, with the full similarity embedding settings added only when the site serves embedding features. Its own process environment holds `HOME`, `PATH`, `LANG`, `PYTHONPATH` with the `src` directory that holds the loaded package, and `MALLOC_ARENA_MAX` from the arena setting. It holds no administration or worker value, and the process reads no settings file. A feedback request reads `config/feedback-form.json` through the feedback service, because the feedback definition is not a settings source. The separation between the administration side and the web server is described in [System boundaries](/docs/architecture/boundaries.md).

## Subsystem keys

The keys a reader most often changes select data, set what the app shows, and bound the serving processes.

Admission costs are total server core-seconds of the serving process, the database and the embedding service together, and a prerequisite graph's cost is its measured elapsed seconds. The load score is a weighted sum of the whole host's CPU utilisation and the light and heavy requests counted in one window, and it places the site in one of the three regimes.

| Key | File | Effect |
|---|---|---|
| `CURRENT_ACADEMIC_YEAR` | `config/curriculum.toml` | Sets which curriculum periods count and refreshes every present role's stored activity on save. |
| `ACTIVE_ACADEMIC_YEAR_WINDOW` | `config/curriculum.toml` | Sets the number of academic years inside which a listed version makes a course active, and refreshes every present role's stored activity on save. |
| `KORI_REQUEST_INTERVAL_SECONDS` | `config/curriculum.toml` | The seconds between source request starts across every concurrent request, as a positive finite number. |
| `EXAMPLE_POSTGRES_DB` | `config/database.toml` | Names the configured database that deploy enrollment leaves unenrolled for the example dataset. |
| `ADMIN_SHARED_ROOT` | `config/database.toml` | The machine-wide directory outside the checkout that the example workflow creates or adopts. An absolute path, or one beginning with `~/`, which expands against the home directory. It holds `models/` for model files, `locks/` for the reservation files and `proxy/` for the machine-wide site proxy project that a domain-mode setup creates, and a relative `EMBEDDING_ARTIFACT_PATH` resolves inside it. |
| `PROXY_BIND_ADDRESS` | `config/database.toml` | One IPv4 or IPv6 literal the machine-wide site proxy binds. Absent, the proxy listens on every host address. Each published site file carries the configured address in a `bind` line. |
| `SEARCH_TIER_PRIORITY` | `config/search.toml` | Sets the order of the four search methods in the merged results. |
| `COMPARISON_HEAVY_OVERLAP_PERCENT` | `config/search.toml` | Sets the overlap threshold of the **Heavily overlapping** verdict. |
| `COMPARISON_PARTIAL_OVERLAP_PERCENT` | `config/search.toml` | Sets the overlap threshold of the **Partly overlapping** verdict. |
| `COMPARISON_SAME_AREA_PERCENT` | `config/search.toml` | Sets the overall-relatedness threshold of the **Same area, different content** verdict. |
| `COMPARISON_LOOSE_RELATED_PERCENT` | `config/search.toml` | Sets the overall-relatedness threshold of the **Loosely related** verdict. |
| `RECOMMENDATION_MIN_RELATEDNESS` | `config/search.toml` | Sets the relatedness floor of the Recommended list. |
| `RECOMMENDATION_RELATEDNESS_CEILING` | `config/search.toml` | Sets the raw relatedness that reads as 100 in the displayed **Similarity** meter, so the floor keeps its own percentage and a close match reads high. |
| `RECOMMENDATION_NEAR_DUPLICATE_PERCENT` | `config/search.toml` | Sets the overlap share above which a candidate is a near-duplicate and left out of recommendations. |
| `ASSUMED_COURSE_CREDITS` | `config/search.toml` | Sets the positive credit value used for a course with no official minimum in every route, graph satisfaction, next-course and aggregate credit calculation. |
| `SEARCH_HNSW_ITERATIVE_SCAN` | `config/search.toml` | Sets the iterative-scan mode of the serving artifact build's nearest query to `relaxed_order` or `strict_order`, so the scan collects further candidates until enough rows survive the build's field and language filters. A recommendation read filters the stored rows by activity and by the excluded courses. |
| `SEARCH_HNSW_MAX_SCAN_TUPLES` | `config/search.toml` | Sets the greatest number of HNSW candidates one nearest-neighbour scan of the serving artifact build reads. |
| `SEARCH_NEIGHBOR_TABLE_ROWS_PER_CHUNK` | `config/search.toml` | Sets the nearest-neighbour rows the serving artifact build stores per query chunk. A recommendation read filters those rows and keeps its own top-K. |
| `INSTANCE_KEY` | `config/web.toml` | The university instance the site presents, as one key of the finite metadata map, which supplies the site's display names, its curriculum source attribution, its one source block and its language policy. [Configuration](/docs/backend/configuration.md#where-values-live) states the policy parts. |
| `WEB_APP_URL` | `config/web.toml` | The external root address of this site. The administration console's App link opens it, and the feedback exact Host and Origin boundary is this address without its optional root slash. HTTPS is required unless the hostname is a loopback host. A domain-mode setup or the publish command writes the instance's site file in the shared site proxy project from this address, and every proxy setting lives in that project's own files. |
| `WEB_HOST` | `config/web.toml` | The loopback address the web server binds. |
| `WEB_PORT` | `config/web.toml` | The TCP port the web server binds. |
| `WEB_WORKERS` | `config/web.toml` | The number of worker processes behind the front router. The router serves the configured loopback port, and each worker process listens on an ephemeral loopback port. The admission settings read the same key. |
| `WEB_MALLOC_ARENA_MAX` | `config/web.toml` | The glibc malloc arenas per web process, as a positive integer. The launcher passes it to the supervisor, and the front router and every forked worker inherit it. Fewer arenas cap the free heap each worker retains under concurrency. The example sets 2. |
| `WEB_RESPAWN_EXIT_LIMIT` | `config/web.toml` | The exits of one role, the router or one worker slot, inside the respawn window that stop the web process instead of starting another replacement. The example sets 3. |
| `WEB_RESPAWN_WINDOW_SECONDS` | `config/web.toml` | The length of the sliding window in seconds over which the supervisor counts the exits of one role. The example sets 60. |
| `WEB_NEXT_COURSE_REPORT_CACHE_BYTES` | `config/web.toml` | The bytes of the shared next-course report region. Every worker reads and writes the same region, and clock eviction keeps it under the bound. The example sets 100663296. |
| `WEB_QUERY_EMBEDDING_CACHE_BYTES` | `config/web.toml` | The bytes of the shared query-embedding region. On a site that serves the embedding features every worker reads and writes the same region, and clock eviction keeps it under the bound. The example sets 50331648. |
| `WEB_REQUEST_MAX_COURSE_ITEMS` | `config/web.toml` | The identities one course list of a request may carry, and the entries of one `module_versions` map, as a positive count. A request over it answers status 422. The example sets 500. |
| `WEB_REQUEST_MAX_IDENTIFIER_LENGTH` | `config/web.toml` | The characters of one identity, official code, map key or module-version value, as a positive count. A request over it answers status 422. The example sets 254, above the longest stored identity of the supported sources. |
| `WEB_REQUEST_MAX_QUERY_LENGTH` | `config/web.toml` | The characters of one search query text, as a positive count. A request over it answers status 422. The example sets 500. |
| `WEB_REQUEST_BODY_MAX_BYTES` | `config/web.toml` | The bytes of one request body the front router reads, as a positive byte count. A larger body answers status 413 before any worker process or admission decision. One request inside the per-input bounds can carry 500 identities of 254 characters in `root_group_ids` and 500 key/value pairs of 254 characters in `module_versions`, about 385 KB before JSON overhead, so the example sets 524288. |
| `WEB_EMBEDDINGS_ENABLED` | `config/web.toml` | Decides whether the site serves semantic matching, the Compare feature and the Recommended list, and which artifacts the serving artifact build writes and the supervisor opens. [Serving artifact build](/docs/backend/text-and-vectors/serving-artifact-build.md#the-embedding-switch) states each mode. The example sets `true`. |
| `WEB_INFERENCE_ENABLED` | `config/web.toml` | Serves inferred data and its controls on every surface. The inference stage has no serving artifact and no coverage gate, so partial coverage is served while the switch is on. The example sets `true`. |
| `WEB_PREREQUISITES_ENABLED` | `config/web.toml` | Serves the prerequisite graph and Next courses, and hides Discover when the embeddings switch is also off. The example sets `true`. |
| `USAGE_RETENTION_DAYS` | `config/web.toml` | The days of request records the usage report retains. A requested window in hours may not exceed that retention. |
| `ADMISSION_ENABLED` | `config/admission.toml` | Serve through the front router's admission control. With it off only the classification is skipped, and the request body bound applies. When it is true the configuration requires `WEB_WORKERS` of at least 2. The example sets `true`. |
| `ADMISSION_IDENTITY_TTL_SECONDS`, `ADMISSION_COOKIE_MAX_AGE_SECONDS`, `ADMISSION_RETRY_AFTER_SECONDS`, `ADMISSION_WINDOW_SECONDS` | `config/admission.toml` | The idle lifetime of a per-user counter, the browser cookie's life, the seconds a refusal states, and the heavy budget window. The example sets 3600, 2592000, 5 and 60. |
| `ADMISSION_LOAD_WINDOW_SECONDS`, `ADMISSION_SCORE_CPU_WEIGHT`, `ADMISSION_SCORE_LIGHT_WEIGHT`, `ADMISSION_SCORE_HEAVY_WEIGHT` | `config/admission.toml` | The seconds of one-second host CPU samples and admitted light and heavy activity that the load score measures, the score per unit of whole-host CPU utilisation, and the score per light and per heavy request counted in that window. The example sets 60, 1.0, 0.012 and 0.004. |
| `ADMISSION_SCORE_HEADROOM`, `ADMISSION_SCORE_SATURATION`, `ADMISSION_SCORE_RELEASE`, `ADMISSION_LIGHT_LOAD_FLOOR` | `config/admission.toml` | The score at or below which the site is in the headroom regime, the score at or above which it is in the saturated regime, the score below which a saturated site returns to the middle regime, and the fraction of the headroom light limits that applies at saturation. `ADMISSION_SCORE_HEADROOM` must be at most `ADMISSION_SCORE_RELEASE`, and `ADMISSION_SCORE_RELEASE` must be below `ADMISSION_SCORE_SATURATION`. The example sets `ADMISSION_SCORE_HEADROOM` to 0.60, `ADMISSION_SCORE_SATURATION` to 1.05, `ADMISSION_SCORE_RELEASE` to 0.90 and `ADMISSION_LIGHT_LOAD_FLOOR` to 0.25. |
| `ADMISSION_LIGHT_OPERATIONS_PER_MINUTE`, `ADMISSION_LIGHT_BURST`, `ADMISSION_LIGHT_CONCURRENCY` | `config/admission.toml` | The per-user light rate, burst and concurrency at headroom. The example sets 120, 20 and 8. |
| `ADMISSION_HEAVY_BUDGET_CORE_SECONDS`, `ADMISSION_HEAVY_BUDGET_SATURATED_CORE_SECONDS`, `ADMISSION_HEAVY_SLOTS`, `ADMISSION_HEAVY_QUEUE_HEADROOM_LIMIT`, `ADMISSION_HEAVY_QUEUE_MIDDLE_LIMIT`, `ADMISSION_HEAVY_QUEUE_SATURATED_LIMIT` | `config/admission.toml` | The per-user heavy budget per window at headroom and at saturation, the global heavy slots at headroom, and the waiting tickets the queue admits at headroom, in the middle regime and at saturation. The slot arithmetic caps the heavy slots at one fewer than `WEB_WORKERS`, so one worker process is always free of heavy work. The example sets 120, 40, 3, 64, 32 and 16. |
| `ADMISSION_RECOMMENDATION_ACTIVE_HEAVY_BASIS`, `ADMISSION_RECOMMENDATION_INACTIVE_HEAVY_BASIS`, `ADMISSION_NEXT_COURSES_ACTIVE_HEAVY_BASIS`, `ADMISSION_NEXT_COURSES_INACTIVE_HEAVY_BASIS`, `ADMISSION_GRAPH_HEAVY_SECONDS`, `ADMISSION_SEARCH_INACTIVE_COST_CORE_SECONDS` | `config/admission.toml` | The basis size from which a recommendation and a next-courses request are heavy in each scope, the elapsed seconds from which a graph is heavy, and the fixed cost of a search that includes inactive courses. The example sets 92, 59, 317, 121, 1.0 and 0.22689. |
| `ADMISSION_RECOMMENDATION_ORIGIN_BASIS`, `ADMISSION_RECOMMENDATION_ACTIVE_KNEE_BASIS`, `ADMISSION_RECOMMENDATION_INACTIVE_KNEE_BASIS` | `config/admission.toml` | The recommendation curve's origin basis and the knee basis of each scope. Each knee must be above the origin. The example sets 3, 181 and 244. |
| `ADMISSION_RECOMMENDATION_ACTIVE_ORIGIN_COST`, `ADMISSION_RECOMMENDATION_ACTIVE_SLOPE`, `ADMISSION_RECOMMENDATION_ACTIVE_TAIL_COST`, `ADMISSION_RECOMMENDATION_ACTIVE_TAIL_SLOPE` | `config/admission.toml` | The active-only recommendation curve: origin cost, slope, tail cost and tail slope. The example sets 0.28268, 0.00808, 1.72143 and 0.00499. |
| `ADMISSION_RECOMMENDATION_INACTIVE_ORIGIN_COST`, `ADMISSION_RECOMMENDATION_INACTIVE_SLOPE`, `ADMISSION_RECOMMENDATION_INACTIVE_TAIL_COST`, `ADMISSION_RECOMMENDATION_INACTIVE_TAIL_SLOPE` | `config/admission.toml` | The recommendation curve that includes inactive courses, with the same four values. The example sets 0.10363, 0.01625, 4.01941 and 0.00817. |
| `ADMISSION_NEXT_COURSES_ORIGIN_BASIS`, `ADMISSION_NEXT_COURSES_ACTIVE_KNEE_BASIS`, `ADMISSION_NEXT_COURSES_INACTIVE_KNEE_BASIS` | `config/admission.toml` | The next-courses curve's origin basis and the knee basis of each scope. Each knee must be above the origin. The example sets 3, 230 and 398. |
| `ADMISSION_NEXT_COURSES_ACTIVE_ORIGIN_COST`, `ADMISSION_NEXT_COURSES_ACTIVE_SLOPE`, `ADMISSION_NEXT_COURSES_ACTIVE_TAIL_COST`, `ADMISSION_NEXT_COURSES_ACTIVE_TAIL_SLOPE` | `config/admission.toml` | The active-only next-courses curve. The example sets 0.10883, 0.00358, 0.92217 and 0.0009. |
| `ADMISSION_NEXT_COURSES_INACTIVE_ORIGIN_COST`, `ADMISSION_NEXT_COURSES_INACTIVE_SLOPE`, `ADMISSION_NEXT_COURSES_INACTIVE_TAIL_COST`, `ADMISSION_NEXT_COURSES_INACTIVE_TAIL_SLOPE` | `config/admission.toml` | The next-courses curve that includes inactive courses. The example sets 0.05998, 0.00803, 3.23171 and 0.00061. |

The administration keys have their own table, with their effects and example values, in [The administration backend's own file](#the-administration-backends-own-file).

Only the usage report reads the retention key at runtime, and the configuration editor lists and saves it in `config/web.toml`. A serving process holds no retention value.

Further keys of each subsystem are documented with the behavior they set:

- Ingestion and normalization: [Ingestion](/docs/backend/catalogue/ingestion.md)
- The data model: [Data model](/docs/backend/catalogue/data-model.md)
- Teaching text and translation: [Translation](/docs/backend/text-and-vectors/translation.md)
- Embedding generation and hubs: [Embeddings](/docs/backend/text-and-vectors/embeddings.md)
- Search, comparison, recommendations, and the route policy: [Search](/docs/backend/search-and-discovery/search.md), [Comparison](/docs/backend/search-and-discovery/comparison.md), [Recommendations](/docs/backend/search-and-discovery/recommendations.md), [Minimum route](/docs/backend/prerequisites/minimum-route.md)
- Inference runtime, generation, selection and rerank: [Inference](/docs/backend/inference/inference.md), [Inference scheduling and disposition](/docs/backend/inference/inference-scheduling.md)
- The read API: [Web API](/docs/backend/interfaces/web-api.md)
- Administration and worker policy: [Operator menu](/docs/operations/operator-menu.md), [Worker sessions](/docs/operations/worker-sessions.md)
