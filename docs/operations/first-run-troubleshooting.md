# First-run troubleshooting

This page states the refusals of the first-run workflows and the stack verbs, and the work a failed
run leaves in place.

## Refusals and messages

| Refusal or message | Cause | Fix |
|---|---|---|
| `self-host: Docker is not available; install Docker Engine`, `self-host: Docker Compose is not available; install the Compose plugin` or `self-host: the Docker daemon does not answer; start it and retry` | A container-host check failed. The bootstrap prefixes each of these three refusals with `self-host: ` on standard error. | Install the named item, start the Docker daemon and retry. |
| `this account cannot write /var/run/docker.sock` | The account is not in the `docker` group. | Add the account to the group and start a new login session. |
| `Git is not available, or <the printed checkout path> is not a Git checkout` | Git is absent, or the printed checkout is not a Git repository. | Install Git, or run the bootstrap from the checkout. |
| `the shared directory must be an absolute path` or `the shared directory must be outside the checkout` | The supplied shared directory is relative or lies inside the checkout. | Choose an absolute path outside the checkout. |
| `the shared directory PATH must be a writable directory` | The shared directory is absent or not writable. | Create it, or grant this account write access, then retry. |
| `the example workflow requires POSTGRES_DB to equal EXAMPLE_POSTGRES_DB` | The configured database name differs from the example name. | Set both keys to the example name in `config/database.toml`. |
| `the example workflow requires a state root with no dataset pointers or records` | The state root holds a pointer or an enrollment record. | Use the real-database workflow, or clear the dataset state explicitly. |
| `an existing environment file keeps its feedback sender credential` | Publication was requested while `.env` holds its own token. | Set the token with `docker exec PROJECT-admin-1 python scripts/admin.py config set .env FEEDBACK_GITHUB_TOKEN '"TOKEN"'`, or start from an absent `.env`. |
| `publication needs a real feedback repository` | `FEEDBACK_GITHUB_REPOSITORY` names the example repository. | Set the destination with `docker exec PROJECT-admin-1 python scripts/admin.py config set config/feedback.toml FEEDBACK_GITHUB_REPOSITORY '"OWNER/REPOSITORY"'`. |
| `the configured feature web port PORT is used by another process` or `the configured administration port PORT is used by another process` | Another listener holds the port. | Stop that listener, or set another port in `config/web.toml` or `config/admin_web.toml`. |
| `the configured database port PORT is used by another process` or `the configured embedding port PORT is used by another process` | A process other than this project's Compose service holds the port. | Stop that process, or point the setting at a free port. |
| `the configured feature HTTP port 80 is used by another process` or `the configured feature HTTPS port 443 is used by another process` | A domain-mode run found a listener on the public port on the configured bind address or the wildcard address, or on any address when none is configured, that the machine-wide proxy does not hold. | Stop that listener, or start the proxy project that owns the port. |
| `domain mode needs the site's HTTPS address` | A domain-mode run or the `publish` command supplied no site address. The interactive form writes `domain mode needs the site's HTTPS address; nothing was written`. | Supply the domain's HTTPS root address. |
| `domain mode needs a site address outside loopback` | The site address names `localhost`, `127.0.0.1` or `::1`. | Supply the domain's address. |
| `domain mode needs the site's HTTPS address without a port` | The site address carries an explicit port. | Supply the address without a port. |
| `proxy path NAME must be a regular non-symlink file` | A proxy project path or a site file is a symlink or not a regular file. | Replace that path with a regular file, then retry. |
| `serve mode must be 1 or 2; nothing was written` | The interactive form received another answer at the serve-mode menu. | Answer **1** for a local site or **2** for a domain. |
| `the four setup ports must be distinct` | Two of the supplied ports are equal. | Choose four different ports. |
| `the setup image IMAGE is missing; run the example, real-database, up or deploy verb first` | `down`, `publish` or `unpublish` ran on a checkout that never built the setup image. | Run `example`, `real-database`, `up` or `deploy` once, which builds and tags it. |
| `the shared directory PATH could not be created` | The bootstrap could not create the shared directory. | Create that path as this account, or grant write access to its parent, then retry. |
| `the SSH directory PATH could not be created` | The bootstrap could not create the account's `.ssh` directory. | Create that directory at mode 0700 as this account, then retry. |
| `publish needs --shared-root or COURSE_EXPLORER_SHARED_ROOT` | A publication verb ran with no shared directory. | Pass `--shared-root PATH` or set `COURSE_EXPLORER_SHARED_ROOT`. |
| Docker's `invalid mount config ... bind source path does not exist` on `up` or `deploy` | The configured shared directory is absent on the host, and every shared bind refuses a missing source. | Create the directory `ADMIN_SHARED_ROOT` names, or run a verb that passes and prepares it. |
| Docker's `invalid mount config ... bind source path does not exist` on `up` | The checkout holds its working files but none of the data directories, and `up` creates no bind source. | Run the `example` workflow first, which creates the usage and attachment directories. |
| `ADMIN_SHARED_ROOT must not be blank` | The configured shared directory is empty. | Set a nonblank path in `config/database.toml`. |
| `ADMIN_SHARED_ROOT must be an absolute or home-prefixed path` | The configured shared directory is relative. | Use an absolute path or one beginning with `~/`. |
| `the shared directory must be outside this checkout` | The resolved shared directory lies inside the checkout. | Choose a path outside the checkout. |
| Docker's `invalid mount config ... bind source path does not exist` on `up`, naming the configured state-root path | The configured state root is absent, and `up` creates no bind source. | Create the root under `data/` before `up`. |
| `the pinned embedding model is absent` | The model file is absent and the download was not confirmed. | Confirm the download in the interactive run or set `confirm_download` in the JSON input. |
| `the embedding artifact download does not report its length` | The download address reports no `Content-Length`. | Use an address that reports its length. |
| `the embedding artifact download must end at an HTTPS address` | The download address redirects to a non-HTTPS address. | Use an address that ends at HTTPS. |
| `the embedding download needs BYTES bytes and BYTES bytes are free` | The target filesystem has less free space than the download. | Free space and retry. |
| `the example database already holds tables` | The example database was imported once. | Use the real-database workflow, or clear the example database explicitly. |
| `the feedback store must be on the same local database cluster as the curriculum database` | The feedback host or port differs from the curriculum host or port. | Point both settings at the same local cluster. |
| `the feedback database name must differ from the curriculum database name` | Both names are equal. | Give the feedback application store its own database name in `config/feedback.toml`. |
| `the feedback database role could not be created` or `the feedback database could not be created` | The role or database creation statement failed. | Check the database service and the administration credentials, then retry. |
| `self-host setup needs the prepared working file PATH` | The real-database workflow ran before the working files existed. | Run the example workflow first. |
| `working path PATH must be a regular non-symlink file` | A working file the workflow materializes is a symlink or not a regular file. | Remove that path or replace it with a regular file, then retry. |
| `administration credential state must be a regular non-symlink file` | The administration credential path is a symlink or not a regular file. | Remove that path or replace it with a regular file, then retry. |
| `the database and embedding services must already be running` | The real-database workflow found a stopped service. | Start both services with `scripts/self-host.sh up`, then retry. |
| `stage STAGE is not idle` | A pipeline stage holds a live state, and the deployment refuses before it builds any service image. | Ordinary-stop that stage, then retry. |
| `the requested initial dataset database already exists` | The named database exists in the container. | Choose another name, or remove that database explicitly. |
| `named initial enrollment requires a state root with no dataset pointers or records` | The state root holds a pointer or an enrollment record. | Use the enrolled dataset owners for a second dataset. |
| `dataset database name must be a lowercase SQL identifier of at most 63 characters` | The supplied database name is not a lowercase SQL identifier of at most 63 characters. | Enter a lowercase name of letters, digits and underscores. |
| `the configured example database cannot be enrolled as a dataset` | The chosen name equals `EXAMPLE_POSTGRES_DB`. | Choose another name. |
| `refuse to drop a protected database` | The chosen name is a protected database. | Choose another name. |
| `installed artifact checksum does not match configuration` | The file at `EMBEDDING_ARTIFACT_PATH` does not match `EMBEDDING_ARTIFACT_SHA256`. | Replace the file with the pinned artifact. |
| `downloaded artifact checksum does not match configuration` | The downloaded bytes do not match `EMBEDDING_ARTIFACT_SHA256`. | Repeat the download and the checksum check. |
| `serving artifacts are missing at ...` | The current artifact build is absent. | On a bootstrap site that serves the example database, run `docker exec PROJECT-admin-1 python scripts/mock_data.py write-artifacts`. On a site with an enrolled Selected dataset, run `docker exec PROJECT-admin-1 python scripts/admin.py serving-artifacts`. |
| `the received database does not match the executing checkout's single Alembic head` | A database is not at the checkout's head revision. | Run `docker exec PROJECT-admin-1 python scripts/admin.py migrate selected` for the served dataset, `migrate working` for Working, or `migrate database` on a bootstrap site. [The schema head and migration](/docs/operations/datasets.md#the-schema-head-and-migration) states the guards. |

A held public port or an unwritable shared directory fails the proxy start or the reload with the reported error. The publication verbs run no port preflight of their own.

## Failures that leave work in place

A failed example import leaves the copied working files, the generated environment file, the administration account and the migrated example schema in place. Clear the example database through the database service before a retry. A failed real-database build or proof leaves the named database, its initial receipt, its dump, its artifacts and the Selected pointer, which `docker exec PROJECT-admin-1 python scripts/admin.py datasets list` names and the dataset owners maintain explicitly.

A failed deployment stops at its first failing step. The enrollment it completed is recorded, and the steps after the failure change nothing.
