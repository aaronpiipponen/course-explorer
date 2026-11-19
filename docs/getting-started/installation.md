# Installation

Installation takes a fresh checkout to a served example site through one host command.

## What the copy needs

- Docker Engine with the Docker Compose plugin, and an account that writes `/var/run/docker.sock`. The account joins the `docker` group and starts a new login session once.
- Git.
- A checkout of this repository. The example workflow writes `INSTANCE_KEY`, the one university instance the site presents, into `config/web.toml`, and [the university map](/docs/operations/first-run-setup.md#the-university-map) states the table it selects from and the resources the key derives.
- Access to the image registries the server images and the pinned database and embedding images build from, and to the model file at the download address that `EMBEDDING_ARTIFACT_URL` names.

The site image carries Python, the locked dependencies and the `libpq` and `libstdc++` libraries, and the administration image carries those and the browser. The host needs no interpreter, Node installation, compiler or native library to operate a copy. Scripts, tests and a source checkout use the development toolchain in [Local checks](/docs/testing/local-checks.md).

## Clone the checkout

```bash
git clone REPOSITORY_ADDRESS course-explorer
cd course-explorer
```

The folder name carries no meaning. The Compose project `course-explorer-<instance key>`, every container name and the volume namespace derive from the instance key the workflow writes, and the instance also owns its four ports. Two instances on one host choose different instance keys and different ports.

## Run the example workflow

```bash
scripts/self-host.sh example
```

The run asks `Shared directory [PATH]: ` first when its input and its output are a terminal, and an empty answer accepts the value in the brackets. A run without a terminal asks nothing and uses the value it already holds. [The bootstrap](/docs/operations/first-run-setup.md#the-bootstrap) states the order the shared directory resolves in and the path rules it must meet.

The workflow copies the absent working files, writes the instance and device values, generates both database passwords, creates the administration account, preflights the container host, prepares the pinned embedding model, builds the database and embedding images, starts those two services, migrates and loads the example dataset, prepares the feedback application store, builds and starts the site and administration services, and proves both HTML shells and the bootstrap catalogue. Those services are `web` and `admin`, with the feedback sender on a publication run and the worker on a same-machine host. It prints one report whose `app_url` is the address to open.

The example data is invented. Its courses, modules, prerequisites and structure rules are not university data, and its embedding vectors are bundled with it, so the import runs no embedding stage.

A copy that serves real curriculum data runs `scripts/self-host.sh real-database` instead, which creates one empty Selected database, builds its serving artifacts, re-creates the site container and proves the selected catalogue. [The real-database workflow](/docs/operations/first-run-setup.md#the-real-database-workflow) states its prompt and its input.

## Stop and start the copy

The copy starts with `scripts/self-host.sh up` and stops with `scripts/self-host.sh down`, which [self-hosting](/docs/getting-started/self-hosting.md#the-stack) states.

## The worker is optional

Ingestion and embedding are server stages, and a copy reaches a served site without a worker. Translation and inference run on the worker, so a site with no worker holds no translated teaching text and no inferred prerequisite, and its semantic match reads official text alone.

The three feature switches in `config/web.toml` decide which features the site serves:

| Key | Decides | Example workflow |
|---|---|---|
| `WEB_EMBEDDINGS_ENABLED` | Comparison, recommendations and semantic search | off |
| `WEB_INFERENCE_ENABLED` | Inferred prerequisites | off |
| `WEB_PREREQUISITES_ENABLED` | The prerequisite graph and Next courses | on for a Sisu instance, off for every other source family |

The example workflow turns the embedding and the inference switch off and writes the prerequisites switch from the instance's source family, so its site serves the official catalogue, search and the structure of studies, with the prerequisite graph and Next courses on a Sisu instance and off for every other source family. Comparison, recommendations and semantic match appear when `WEB_EMBEDDINGS_ENABLED` is on and the serving artifacts carry the embedding files, and inferred prerequisites appear when `WEB_INFERENCE_ENABLED` is on. [A Windows WSL worker](/docs/getting-started/worker-wsl.md) and [a Linux worker](/docs/getting-started/worker-linux.md) install the stages that fill both, and [Preparing a dataset from the curriculum source](/docs/getting-started/data-preparation.md) runs the stages around it.

## Where the reference is

- [First-run installation reference](/docs/operations/first-run-setup.md) states every prompt, the JSON input, the working files, the instance resources and the addresses, and [First-run troubleshooting](/docs/operations/first-run-troubleshooting.md) states the refusals.
- [Server containers](/docs/operations/server-containers.md) states the images, the mounts and the Compose bridge.
- [Machine-wide site proxy](/docs/operations/site-proxy.md) states the proxy project, its container and its reload.
- [Datasets](/docs/operations/datasets.md) states the Selected and Working roles, the receipts and the promotion.
- [Self-hosting](/docs/getting-started/self-hosting.md) states the running copy, its two serve modes and its update steps.
