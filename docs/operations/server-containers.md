# Server containers

The site backend, the administration backend and the feedback sender run from two images of one
multi-stage Dockerfile, and a same-machine worker runs from a third target.

## The image targets

Three Dockerfiles build the images an instance stack runs.

| Dockerfile | Compose service | What it produces |
|---|---|---|
| `docker/server/Dockerfile` | `web`, `admin`, `sender`, `worker` | The site, administration and worker images of its three final targets |
| `docker/db/Dockerfile` | `db` | PostgreSQL 18 with the `course_embedding_search` extension and its capability label |
| `docker/embedding/Dockerfile` | `embedding` | The pinned llama.cpp server image with its checking entrypoint |

`docker/server/Dockerfile` has three final targets, `site`, `admin` and `worker`, over shared build
stages. The site and administration targets start from the same `python-dependencies` stage, so
they carry the same locked Python environment.

| Stage | Base image | What it produces |
|---|---|---|
| `python-runtime` | `python:3.14-slim-trixie` | `libpq5`, `libstdc++6`, `ca-certificates`, the `uv` and `uvx` binaries, the interpreter capability check, `WORKDIR /app`, `PYTHONPATH=/app/src`, `UV_PROJECT_ENVIRONMENT=/app/.venv` and the writable image home directory |
| `python-dependencies` | `python-runtime` | `/app/.venv` from `uv sync --locked --no-dev --no-install-project --no-managed-python --python /usr/local/bin/python3` |
| `native-build` | `python-runtime` | The three serving libraries from `native/`, compiled with `g++`, `make` and `libpq-dev` |
| `web-build` | `node:22.22.2-slim` | `web/dist`, built from `web/`, `docs/` and the checkout's `config/wiki.json` |
| `admin-build` | `node:22.22.2-slim` | `admin-web/dist` |
| `site` | `python-dependencies` | The site backend image |
| `admin` | `python-dependencies` | The administration backend image |
| `worker` | `python-dependencies` | The same-machine worker image |

The interpreter check runs `os.memfd_create` with `MFD_CLOEXEC` in the runtime stage, because the
shared caches map an anonymous memory file. A base image whose interpreter lacks that capability
fails the build.

### The database image

`docker/db/Dockerfile` builds the PostgreSQL image in two stages that both start from the
`pgvector/pgvector:0.8.6-pg18-trixie` image pinned by tag and digest. The first installs `g++`,
`make` and `postgresql-server-dev-18`, compiles `docker/db/extension` and installs it. The second
copies the `course_embedding_search` shared library, control file and SQL file into the PostgreSQL
18 library and extension directories, and labels the result
`org.course-explorer.db-extension="1.0"`, the capability label the worker readiness read compares
with its capability version.

`examples/docker-compose.yml` builds `db` from that file with the checkout as its context, publishes
it on `127.0.0.1:POSTGRES_PORT`, stores `postgres_data` at `/var/lib/postgresql`, probes it with
`pg_isready` over TCP and gives it 1 GiB of shared memory for the index build an
embedding-inclusive restore performs.

### The embedding image

`docker/embedding/Dockerfile` wraps the official llama.cpp server image named by
`EMBEDDING_SERVER_IMAGE` and records `EMBEDDING_RUNTIME_REVISION` in the
`org.opencontainers.image.revision` label. A build layer re-checks the `@sha256` digest pin that
Compose expands in `FROM`, and the image installs `docker/embedding/entrypoint.sh` as its
entrypoint.

The entrypoint requires the embedding environment values and refuses with one message per defect:
`EMBEDDING_HOST` other than `127.0.0.1`, a port outside 1 to 65535, a thread or context count below
1, a pooling value other than `cls`, `mean` or `last`, a normalization value other than `-1` or a
non-negative integer, an artifact digest that is not 64 lowercase hexadecimal characters, and a
runtime revision that is not 40. It checks `/models/embedding.gguf` against
`EMBEDDING_ARTIFACT_SHA256`, reads `/app/llama-server --version` and requires the commit prefix of
the configured revision, then replaces itself with `llama-server` on that artifact, bound to the
configured host and port, with the configured threads, context size, pooling and normalization,
one parallel slot, embedding mode on, no GPU device, offline mode and no web UI.

Compose passes the two build arguments, runs the service in the host network namespace, mounts the
artifact read-only at `/models/embedding.gguf` with no host-path creation, drops every capability,
sets `no-new-privileges` and probes its `/health` endpoint.

## What each image carries

The `site` target copies `universities.toml`, `src/course_explorer/`, `src/course_explorer_web/`,
`alembic_feedback.ini`, `migrations_feedback/`, the three native libraries into `/app/native/` and
`web/dist` into `/app/web/dist`. The feedback service reads the feedback migration chain on the
first submission it accepts. The image's importable packages are `course_explorer` and
`course_explorer_web`, and the CI verify job builds the image and checks that `course_explorer_admin`
does not resolve and that no `docker`, `ssh`, `gh` or `git` command is on its path.

The `admin` target installs `git`, `openssh-client`, `gh`, Docker's `docker-ce-cli`,
`docker-buildx-plugin` and `docker-compose-plugin`, and a headless Chromium at `/opt/ms-playwright`
through `playwright install --with-deps --only-shell chromium`. Compose builds the stack's images
with the BuildKit builder, so the client carries the plugin that provides it. It copies the whole
`src/` tree, so the image carries every package the checkout implements, together with `scripts/`,
`examples/`, `universities.toml`, `pyproject.toml`, `alembic.ini`, `alembic_feedback.ini`,
`migrations/`, `migrations_feedback/`, the three native libraries and `admin-web/dist`.

The `worker` target installs `git`, `iproute2`, `util-linux`, Docker's `docker-ce-cli`,
`docker-buildx-plugin` and `docker-compose-plugin` packages, and the runtime libraries
`libdrm-amdgpu1`, `libdrm2`, `libelf1`, `libgomp1` and `libnuma1`. It
copies `alembic.ini`, `migrations/`, `universities.toml`, `src/` and `examples/`, and its command is
`sleep infinity`, so a stage runs its helpers as commands in the running container.

The images carry code and build outputs alone. Every secret, model file, SSH key and runtime file
arrives as a mount, the administration image's `examples/` templates hold placeholder values only,
and the build copies no directory that could sweep the checkout.

## The host network namespace

The embedding service and the four server services `web`, `admin`, `sender` and `worker` run with
`network_mode: host`. Each container therefore shares the host's network namespace, and a bind to a
loopback address inside the container is a bind on the host's loopback. The site reaches the
database at `POSTGRES_HOST:POSTGRES_PORT` and the embedding service at
`EMBEDDING_HOST:EMBEDDING_PORT` as `config/` configures them, and the administration console binds
`ADMIN_WEB_HOST:ADMIN_WEB_PORT` on the host.

A Caddy container outside every instance stack publishes the sites on the host's ports 80 and 443,
and [Machine-wide site proxy](/docs/operations/site-proxy.md) states that project.

## Mounts

Every bind sets `bind.create_host_path: false`, so a missing source fails the container start
instead of becoming an empty directory.

| Service | Mount | Container path | Access |
|---|---|---|---|
| `web` | `./config` | `/app/config` | read-only |
| `web` | `./${ADMIN_STATE_ROOT}` | `/app/${ADMIN_STATE_ROOT}` | read-only |
| `web` | `./data/usage` | `/app/data/usage` | read-write |
| `web` | `./data/attachments` | `/app/data/attachments` | read-write |
| `admin` | `./config` | `/app/config` | read-write |
| `admin` | `./.env` | `/app/.env` | read-write |
| `admin` | `./data` | `/app/data` | read-write |
| `admin` | `${COURSE_EXPLORER_HOST_ROOT}` | the same absolute path | read-only |
| `admin` | `${ADMIN_SHARED_ROOT}` | the same absolute path | read-write |
| `admin` | `./.git` | `/app/.git` | read-only |
| `admin` | `${COURSE_EXPLORER_SSH_DIR}` | `$HOME/.ssh` | read-only |
| `admin` | `/var/run/docker.sock` | `/var/run/docker.sock` | read-write |
| `sender` | `./config` | `/app/config` | read-only |
| `sender` | `./.env` | `/app/.env` | read-only |
| `sender` | `./data` | `/app/data` | read-only |
| `worker` | `${COURSE_EXPLORER_HOST_ROOT}` | the same absolute path | read-only |
| `worker` | `./config` | `/app/config` | read-only |
| `worker` | `./.env` | `/app/.env` | read-only |
| `worker` | `./data` | `/app/data` | read-write |
| `worker` | `./models` | `/app/models` | read-only |
| `worker` | `${ADMIN_SHARED_ROOT}` | the same absolute path | read-write |
| `worker` | `${COURSE_EXPLORER_HOST_HOME}/.local/src` | the same absolute path | read-only |
| `worker` | `/var/run/docker.sock` | `/var/run/docker.sock` | read-write |

The site container reads the dataset pointer, its receipt and the serving artifacts under the
configured `ADMIN_STATE_ROOT`, which the Compose bridge projects from `config/database.toml`, and it
writes one request record per request into `data/usage/requests` and the feedback attachments into
`data/attachments`. Those four mounts are the only checkout content the site sees. It mounts no
environment file, no `data/admin-web` and no other `data/` subtree, and it receives only
`POSTGRES_PASSWORD` and `FEEDBACK_POSTGRES_PASSWORD` through the generated Compose environment. The
site merges every subsystem file, `config/worker.toml` included, and each settings family it
builds reads only the keys its own settings class declares. The service carries a
4 GiB `mem_limit` and `memswap_limit`.

`ADMIN_STATE_ROOT` names a repository-relative path, and the deployment sets it under `data/`. The
site is the only service that receives that configured mount, and the administration and worker
containers mount `data/` alone, so a state root outside `data/` has no writable mount in those two
containers.

The administration container edits configuration, the environment file and its own state under
`data/`, which is why those three mounts are read-write. The checkout at its host absolute path
gives the console the two Git reads of the status report, the files a rebuild reads and the
relative sources of the Compose file it runs. The shared directory holds the embedding model
and the machine-wide reservation files. The `.ssh` mount is the account's whole SSH directory, with
every key and known-host entry in it, and it gives the console the account's device-local SSH
authentication for the configured worker. The Docker socket gives the console the host's Docker
control. Both are the operator capability of the administration surface, and neither is a feature
of the site.

The mounted `.git` directory belongs to the operator while the image's `/app` belongs to root, so
the service declares `/app` a Git safe directory through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and
`GIT_CONFIG_VALUE_0`. The status report's two Git reads and a stage's recorded revision then resolve
the mounted repository.

The feedback sender runs the administration image with `python -m course_explorer.feedback.sender`
in a loop and 60 seconds between one completed attempt and the next. Its three read-only
working-path mounts supply the destination settings and the attachments, and it reaches the
feedback application store over the network. It mounts nothing else from the checkout. It declares
`restart: unless-stopped` and depends on the healthy database, and the setup starts it only when
the operator requested publication.

## The same-machine worker service

The `worker` service runs under the `worker` profile, so a remote-worker instance does not start it.
The container shares the host network namespace, runs as the invoking account and joins the
socket's group. Its Docker socket mount is the same full Docker control the worker account holds on
its host, and its runtime-tree mount is the host-built llama.cpp checkout tree at the host home path.
Its `data/` mount is read-write because a stage writes its session evidence there, and its `models/`
and `.env` mounts are read-only.

The checkpoint reboot uses that socket for one fixed `--rm --privileged --pid=host` one-shot of the
running container's own image. The one-shot enters the host's namespaces and runs the host's
`systemctl reboot`. It adds no mount, rule, unit or persistent container.

The install writes `docker-compose.worker.yml` with the host's ROCm device nodes and the `render`
and `video` group ids, or the NVIDIA runtime and the three CUDA toolkit sonames. The Compose bridge
adds that override and the `worker` profile to every command on a same-machine host. A same-machine
install that rewrites the override recreates a running `worker` service with it, and a stopped
service is started by the first stage reservation. [Worker
transport](/docs/operations/worker-transport.md) describes the transport that reaches the service.

## Why the checkout and the shared directory keep their host paths

A Compose command resolves its project directory, every relative bind source and every build
context against paths the Docker daemon reads on the host. The administration container runs such
commands when it prepares a stage and when an operator starts, stops or inspects the stack.

The checkout and the shared directory are therefore mounted at their host absolute paths, and the
bridge names that path as the project directory. A Compose command issued inside the container
names the same containers, volumes and build contexts as the same command on the host, and a
relative `./config` source resolves to the host checkout's `config/`.

## The bridge aliases and the Docker privilege

`src/course_explorer_admin/compose.py` owns the generated environment file that Compose reads. It
projects the database, embedding, instance, shared-storage and feedback settings onto the Compose
interpolation aliases. It adds the three account identity aliases and the four Docker host-path
aliases, and it projects `ADMIN_STATE_ROOT` and the feedback application store's password. The three
are `COURSE_EXPLORER_UID`, `COURSE_EXPLORER_GID` and `COURSE_EXPLORER_DOCKER_GID`, and the four are
`COURSE_EXPLORER_HOST_ROOT`, `COURSE_EXPLORER_HOST_HOME`, `COURSE_EXPLORER_SSH_DIR` and
`ADMIN_SHARED_ROOT`. `COURSE_EXPLORER_PROJECT_NAME` comes from the instance key, so the table below
names nine aliases, and the password reaches the site as `FEEDBACK_POSTGRES_PASSWORD`.

| Alias | Source |
|---|---|
| `COURSE_EXPLORER_PROJECT_NAME` | The instance key's derived project name |
| `COURSE_EXPLORER_UID` | The invoking process's user id |
| `COURSE_EXPLORER_GID` | The invoking process's group id |
| `COURSE_EXPLORER_DOCKER_GID` | The Docker socket's group id |
| `COURSE_EXPLORER_HOST_ROOT` | The Docker host's absolute checkout path |
| `COURSE_EXPLORER_HOST_HOME` | The Docker host's home directory, independent of its SSH directory |
| `COURSE_EXPLORER_SSH_DIR` | The Docker host's absolute SSH directory |
| `ADMIN_SHARED_ROOT` | The configured shared directory, whose home prefix resolves against the host home |
| `ADMIN_STATE_ROOT` | The configured repository-relative state root the site mounts read-only |

The three identity aliases set the `user` and `group_add` values of the services that run as the
invoking account: `web`, `admin`, `sender` and `worker`. The four host-path aliases carry the Docker
host's paths. A host process derives `COURSE_EXPLORER_HOST_ROOT` from its own checkout,
`COURSE_EXPLORER_HOST_HOME` from its own home directory and `COURSE_EXPLORER_SSH_DIR` from the
account's home directory. The administration service receives those three aliases from its own
environment. The host home is its own alias rather than a value derived from the SSH directory, and
a home-prefixed shared root resolves against it. `ADMIN_STATE_ROOT` is the repository-relative root
the site container mounts read-only.

Every stack command reads the values it projects from the checkout's `config/*.toml` and `.env`,
`ADMIN_STATE_ROOT` included. A non-secret value exported in the operator's shell changes a
projection or a mount but not what the container serves, because the container reads the same files.
Set every non-secret container setting in `config/*.toml`. The two database passwords are the
exception: the site reads no `.env` and receives them through the generated Compose environment, so
an exported password is the value the site receives.

The account decides the Docker privilege. `compose.docker_needs_sudo` reports whether the account
can write `/var/run/docker.sock`, and `compose.docker_prefix` runs `docker` directly when it can.
Otherwise the command runs through `sudo`, with `-n` for a fixed command that cannot answer a
prompt. The server-status container read uses the same prefix.

## Stage children and the restart contract

A local stage child runs inside the administration container as
`python -m <stage worker module> <stage> <session id>`. Its durable lifecycle state lives in
`data/`, and each atomic unit ends at a checkpoint.

Restarting the administration container ends its stage child. The durable session state lives in
`data/`, and the operator resumes the sole compatible retained job with the same fixed `start`
command. A container restart starts no stage. At startup the administration backend resumes each
same-machine stage that a host boot interrupted: a `starting` or `running` stage whose requested
action is `run` and whose recorded process belongs to an earlier boot. That resume is the only
automatic stage start, and a stop reaches idle the same way it does for a host process.

## Building and starting by hand

The three targets build on `linux/amd64` and `linux/arm64` from the checkout root. Each command
below builds one target under its own tag, so the image is available under that tag outside the
stack.

```bash
docker build --target site -f docker/server/Dockerfile -t course-explorer-site .
docker build --target admin -f docker/server/Dockerfile -t course-explorer-admin .
docker build --target worker -f docker/server/Dockerfile -t course-explorer-worker .
```

The stack runs the images Compose builds from the same targets, named after the effective project
and service, such as `course-explorer-example-web`. `examples/docker-compose.yml` declares no
`image:` key, so the bootstrap's `up` builds every service image under that project name before it
starts the services, through the one Compose bridge that writes the environment file and names the
project directory.

The stack is the four core services, the sender of a publication-configured checkout and the
same-machine worker of a same-machine host.

```bash
scripts/self-host.sh up
```

The same bridge is reachable inside the administration container, and one command there starts the
four core services. `PROJECT` is the instance's Compose project, for example `course-explorer-example`.

```bash
docker exec PROJECT-admin-1 python -m course_explorer_admin.compose --non-interactive -- up -d --wait db embedding web admin
```

One feedback dispatch runs from the administration image.

```bash
docker exec PROJECT-admin-1 python -m course_explorer.feedback.sender
```

## Starting, stopping and updating a copy

The host bootstrap runs every container action inside the administration image, so the host needs
Docker, Docker Compose and Git alone. [Self-hosting](/docs/getting-started/self-hosting.md#update-a-running-copy)
states the operator's update procedure. `up` builds and starts `db`, `embedding`, `web` and `admin`.
It adds the `sender` when `self_host._publication_configured` finds a feedback token in the
checkout's environment beyond the example placeholder, and it adds the profile-gated `worker` when
`WORKER_HOST_KIND` is `same-machine`. `down` stops and removes the instance's containers and its
network and leaves every volume and dataset in place.

Start and stop the sender alone with Docker, and re-create one service through the administration
container's Compose bridge:

```bash
docker start PROJECT-sender-1
docker stop PROJECT-sender-1
docker exec PROJECT-admin-1 python -m course_explorer_admin.compose --non-interactive -- up -d --wait --force-recreate --no-build web
```

The last command replaces only the named service's container from its current image and waits for
the service to run. The site and administration rebuilds, the selected-dataset promotion and the
container status read all act on these containers. Every service declares `restart: unless-stopped`,
so the Docker daemon starts the stack with the host and returns a failed service.
