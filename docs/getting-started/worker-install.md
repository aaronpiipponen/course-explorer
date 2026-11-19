# Installing a worker

Every worker host runs one interactive install that prepares its packages, files, identity, preset and runtimes.

## The install command

```bash
bash scripts/install-worker.sh
```

The command runs on the worker in its own terminal, from the top of the worker checkout, and it asks for the `sudo` password when a privileged step needs it. The script installs `uv` when the account holds none, syncs the locked environment and runs the install. [A Windows WSL worker](/docs/getting-started/worker-wsl.md) and [a Linux worker](/docs/getting-started/worker-linux.md) state each host's prerequisites and the answers that differ for that host.

No argument runs the install. `-h` or `--help` prints `usage: scripts/install-worker.sh` and returns 0. Any other argument prints the same line to the error stream and returns 1.

Before the install starts, `scripts/install-worker.sh` refuses three conditions in the form `install-worker: ` followed by the message, and returns 1:

| Message | When it is printed |
|---|---|
| `Git is not available` | `git` is not on the command search path. |
| `curl is not available` | `curl` is not on the command search path. |
| `<path> is not a Git checkout` | The checkout path lies in no Git repository, where `<path>` is that path. |

## The install prompts

The install asks these questions in this order:

| Prompt | Answer |
|---|---|
| `This machine also runs the server stack? [y/N]: ` | **Y** for the same-machine worker, **N** for a worker on a separate host |
| `Worker SSH target: ` | The SSH target the server reaches. Asked for a worker on a separate host. |
| `GPU backend (rocm or cuda): ` | `rocm` or `cuda` |
| `GPU target: ` | The AMD compute target such as `gfx1201`, or the CUDA compute capability without the dot such as `86` |
| `GPU device index [0]: ` | The vendor index of the card the model servers use |
| `Model preset (12gb, 16gb, 8gb or keep) [keep]: ` | One preset name, or `keep` for the configuration already in the checkout |

An empty answer to a question that offers a default takes the value in the brackets. An empty answer to `Worker SSH target: ` or `GPU target: ` is refused, because those questions offer none.

After these questions, a native Linux worker on a separate host is offered `Install the reboot rule that allows /usr/bin/systemctl reboot? [y/N]: ` once the packages, the identity and the prepared stack are in place. A WSL host prints the Windows-side memory block and waits for it to be set.

## The install steps

1. Check the host. The command needs a Git checkout of this repository at the top of its working tree, on a branch with an origin remote, and an Ubuntu-based host whose release file reports an Ubuntu codename in `UBUNTU_CODENAME` or, on Ubuntu itself, `VERSION_CODENAME`.
2. Install the packages. Docker Engine comes from Docker's official apt source when one of its packages is missing, and the configured package list follows. A distribution package such as `docker.io`, `docker-compose` or `podman-docker` conflicts with Docker's own packages, so remove a conflicting package before the install.
3. Start Docker and join the groups. A WSL host first checks that the distribution's first process is systemd. The install enables and starts Docker, joins the account to the `docker` group and, on a ROCm remote worker, to the `render` and `video` groups. A running process holds the groups it started with, so a fresh account's first run stops after this step with no working file, identity or override written.
4. Write the working files, the identity and the preset. Every absent working file is materialized from `examples/`, a `.env` is created at mode 0600 with generated database passwords, and the worker identity is written to `config/worker.toml` in one validated save. The preset answer writes the chosen preset's model and artifact values into `config/inference.toml` and `config/translation.toml`, and `keep` leaves both files as they are. A same-machine worker writes its generated GPU override before its identity.
5. Prepare the stack. The shared directory's `models/` and `locks/` are created at mode 0700, the `db` service image is built, the configured artifacts are verified (a main and a distinct fallback generation file, the translation file and the rerank file), a missing artifact with a configured source is downloaded from it and verified by SHA-256, and the two llama.cpp runtimes are built for the configured backend and target under `~/.local/src/llama.cpp-<revision>-<backend>-<target>`. The install converts no file.
6. Run the host's last step. A native Linux worker on a separate host is offered the reboot rule, which [a Linux worker](/docs/getting-started/worker-linux.md#the-reboot-rule) states, and a WSL host is shown the Windows-side memory block, which [a Windows WSL worker](/docs/getting-started/worker-wsl.md#the-wsl-memory-block) states.

## Refusals

Each refusal prints one line to the error stream in the form `install-worker: ` followed by the message, and the command returns 1.

| Message | When it is printed | What to do |
|---|---|---|
| `Worker SSH target is required` | An empty answer to `Worker SSH target: ` for a worker on a separate host. | Answer with the SSH target the server reaches. |
| `GPU target is required` | An empty answer to `GPU target: `. | Answer with the AMD compute target or the CUDA compute capability. |
| `GPU backend (rocm or cuda) is required` | An empty answer to the backend question. | Answer `rocm` or `cuda`. |
| `GPU backend (rocm or cuda) must be one of: rocm, cuda` | An answer other than `rocm` or `cuda` to the backend question. | Answer `rocm` or `cuda`. |
| `GPU device index must be a whole number` | An answer to the device index question that is not a whole number. | Answer with the vendor index of the card. |
| `the install must run from the top of its own Git checkout` | The checkout lies inside a larger Git working tree, so the top level Git reports is not the checkout root. | Clone the repository outside the enclosing working tree, then run the command again. |
| `the worker checkout must be on a Git branch` | The checkout's HEAD is detached from every branch. | Check out the branch the server updates, then run the command again. |
| `the worker checkout must name its Git origin remote` | The checkout's origin remote reports no address. | Add the origin remote the server uses, then run the command again. |
| `the worker install supports an Ubuntu-based Linux distribution` | The release file names no Ubuntu lineage, or reports no Ubuntu codename. | Run the install on an Ubuntu-based distribution such as Ubuntu itself or Linux Mint. |
| `run this command from a new WSL login shell` | The command runs against the Windows kernel outside a WSL distribution, as from a Windows terminal. | Open a shell in the WSL distribution and run the command there. |
| `a same-machine worker runs on native Linux, so this WSL host cannot select it` | The same-machine answer on a WSL host. | Answer `N` for a remote worker, or run the install on the native Linux host that runs the server stack. |
| `one of the install answers is invalid; answer the prompts again` | An answer fails the install's own validation, such as a GPU target of the wrong form (`gfx1201` for `rocm` or `86` for `cuda`), or a device index outside 0 to 15. | Run the command again and answer each prompt with a value of the required form. |
| `unknown model preset: <name>` | The preset answer names no tracked preset directory that holds both fragments. | Run the command again and answer the preset question with one of the offered names or `keep`. |
| `the host reports no dpkg architecture` | `dpkg --print-architecture` returns nothing when the install writes Docker's apt source. | Check the host's `dpkg` installation, then run the command again. |
| `the worker package installation for the <backend> backend failed with exit status <status>; check the host's apt sources, the backend's package source, free disk space and a conflicting Docker package, then run the install again` | The package step exits nonzero. | Check the apt sources and the free disk space, remove a conflicting Docker package, then run the command again. |
| `this WSL distribution does not run systemd; set systemd=true in /etc/wsl.conf and restart the distribution` | The distribution's first process is not systemd. | Set `systemd=true` in `/etc/wsl.conf` and restart the distribution. |
| `the new group membership takes effect in a new login session. Sign in again and run this command once more to prepare the stack.` | The account was joined to a group during this run, and the running shell holds the groups it started with. | Sign in again and run the command once more, which prepares the stack. |
| `the ROCm install reports no root path through hipconfig` | A same-machine ROCm worker's `hipconfig -R` reports no root path, at the GPU override step, before the override file is written. | Check the ROCm installation, then run the command again. |
| `the CUDA worker requires Docker's nvidia runtime. Install the NVIDIA Container Toolkit, register the runtime with Docker and run the install again` | A same-machine CUDA worker's Docker daemon lists no `nvidia` runtime. | Install the NVIDIA Container Toolkit, register the runtime with Docker, then run the command again. |
| `the CUDA toolkit library <name> resolves to no regular file` | A loader-cache entry for one of the three CUDA toolkit library names points at no regular file, at the GPU override step, before the override file is written. | Check the CUDA toolkit installation, then run the command again. |
| `the CUDA toolkit library <name> is not in the loader cache` | One of the three CUDA toolkit library names is absent from `ldconfig -p`, at the GPU override step, before the override file is written. | Check the CUDA toolkit installation, then run the command again. |
| `the model preset <name> does not apply to this checkout: <reason>` | The preset's fragments name a key that a working file lacks, such as a `config/translation.toml` that names a local translation conversion and omits the three artifact source keys. | Answer `keep` at the preset question, or add the missing keys to the working file, then run the command again. |

An input that ends before every answer is read prints `install-worker: input ended before the install answers were complete` and returns 0, and an interrupt prints `install-worker: the install was interrupted` and returns 130. Any other command the install runs that exits nonzero prints `install-worker: <program> failed with exit status <status>. Resolve the failure and run the install again.` and returns 1.

## The resource report

The install prints one JSON resource report holding a `ready` flag and a `missing` list of fixed component names, and it returns 1 when that list is not empty. After the report it prints one sentence about the server's `config/worker.toml`, then the values that file must carry: the five shared keys `WORKER_HOST_KIND`, `WORKER_PROJECT_ROOT`, `WORKER_GPU_BACKEND`, `WORKER_GPU_TARGET` and `WORKER_GPU_DEVICE_INDEX`. A WSL host adds `WORKER_WSL_DISTRIBUTION`. The sentence states that a remote worker's `WORKER_SSH_TARGET` names this host as the server reaches it, and that a same-machine worker names no SSH target.

The command's last line states what the install wrote: the SSH target, the backend, the compute target and the device index for a remote worker, or the same-machine worker container with the same backend, target and device index.

## The GPU backend

`WORKER_GPU_BACKEND` selects the backend, `WORKER_GPU_TARGET` names its compute target and `WORKER_GPU_DEVICE_INDEX` names the card.

- `rocm` builds llama.cpp with HIP and launches the model children with `HIP_VISIBLE_DEVICES`.
- `cuda` builds llama.cpp with CUDA and launches the model children with `CUDA_VISIBLE_DEVICES`.

The operator installs the GPU package that each host's backend needs, and each host page states it. The install writes no vendor apt source, so the source comes from [AMD's ROCm installation instructions for Linux](https://rocm.docs.amd.com/en/latest/install/rocm.html) at the release the installed driver supports, or from [NVIDIA's CUDA installation guide for Linux](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/), which also provides `nvcc` and `nvidia-smi`. A failed package step stops with one message that names the configured backend, and a readiness check names a missing or mismatched stack as `gpu-stack`.

Each build lives in `~/.local/src/llama.cpp-<revision>-<backend>-<target>`, so the path is a function of the revision, the backend and the target. The install clones and builds both runtimes for the configured backend and target, and a stage reservation verifies the checkout revision, the executable and the model artifact before it starts a model. A directory built for another target sits beside the configured one under `~/.local/src/`, and a build writes inside its own directory alone. The install's own host-side check reads the vendor tool against the configured target before it builds a runtime, and the readiness read names a target that no card reports as `gpu-stack`.

## Model presets

The install offers the three tracked presets and writes the chosen pair into `config/inference.toml` and `config/translation.toml`. [Model presets](/docs/getting-started/model-presets.md) lists each preset's files, contexts and card size and states how the install applies one.

## The periodic reboot

`WORKER_GPU_REBOOT_ENABLED` in `config/worker.toml` turns the periodic reboot on, and the example leaves it off. `WORKER_GPU_REBOOT_INTERVAL_MINUTES` sets the loaded-model run time after which the worker restarts, 120 minutes in the example. [The periodic reboot](/docs/operations/worker-sessions.md#the-periodic-reboot) states the clock, the checkpoint and the resume, and [a Linux worker](/docs/getting-started/worker-linux.md#the-reboot-rule) and [a Windows WSL worker](/docs/getting-started/worker-wsl.md#the-windows-reboot) state how each host proves the restart.

## Update and check

The code update and the preflight run on the server, in the administration container:

```bash
docker exec PROJECT-admin-1 python scripts/admin.py setup worker --branch main
docker exec PROJECT-admin-1 python scripts/admin.py preflight
```

The code update fast-forwards the worker checkout to one origin branch and installs its locked environment. `--branch BRANCH` selects the origin branch, and the default is `main`. The preflight reads the prepared facts without changing anything and returns 1 when the locked environment or a resource is missing. A same-machine worker has no separate checkout, because its image is built from the server checkout, so the code update refuses it and one deployment updates it. [Setup and preflight](/docs/operations/setup-and-preflight.md) states both operations and the worker provisioning entries of the operator menu.

## Run the stages

The translation and inference stages start, report and stop through the administration container, and each command prints one JSON report. The prefix is `docker exec PROJECT-admin-1 python scripts/admin.py`. [Running a stage](/docs/operations/worker-sessions.md#running-a-stage) states the commands, and [the stage tools](/docs/operations/operator-menu.md#the-stage-tools) lists the menu entries behind them. A stop returns the verified copy of the Working dataset, and [the dataset return](/docs/operations/datasets.md#the-return-and-the-working-role) and [rotation](/docs/operations/datasets.md#rotation) state the receipt and the guards.
