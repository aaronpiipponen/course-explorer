# A Linux worker

This page installs and runs the worker stages on a native Linux host, reached over SSH or on the server machine itself.

## What the install needs

- An Ubuntu-based Linux distribution, such as Ubuntu itself or a derivative whose release file names Ubuntu in `ID_LIKE` (Linux Mint, Pop!_OS), on a remote host reached over SSH or on the server machine for a same-machine worker. Linux Mint 22 is the tested native host.
- For `rocm`, an AMD card exposed at the configured compute target, and the `/dev/kfd` and `/dev/dri` device nodes. The install joins a remote worker's account to the `render` and `video` groups that own them.
- For `cuda`, an NVIDIA card whose driver and CUDA toolkit let `nvcc` and `nvidia-smi` answer. A same-machine CUDA worker also needs the NVIDIA Container Toolkit installed and its `nvidia` runtime registered with Docker, which `docker info` lists.
- A checkout of this repository at `WORKER_PROJECT_ROOT` on a remote worker, or the server checkout at its own host path for a same-machine worker.
- Git, `curl` and the account's own `sudo` password.
- Disk space under the account's home directory for the two llama.cpp checkouts under `~/.local/src/` and for up to four model artifacts under `models/`.

The install runs on the worker in its own terminal. The code update and the preflight run on the server. The worker identity lives in the `config/worker.toml` of the server and of the worker checkout. [The worker connection values](/docs/operations/worker-transport.md#the-worker-connection-values) lists the keys, and [what the worker must provide](/docs/operations/worker-transport.md#what-the-worker-must-provide) states what each requirement follows from.

## Install a remote worker

Run the command from the top of the worker checkout:

```bash
bash scripts/install-worker.sh
```

The command asks the questions that [Installing a worker](/docs/getting-started/worker-install.md#the-install-prompts) lists, in that order, and it asks for the `sudo` password when a privileged step needs it. This host answers **N** to the server-stack question, because it is a remote worker.

[Installing a worker](/docs/getting-started/worker-install.md#the-install-steps) states the install steps, the resource report the command prints and its refusals, and a remote worker's last step offers the rule that [the reboot rule](#the-reboot-rule) states.

## Install a same-machine worker

A same-machine worker is the instance's own worker container, and a native Linux host that runs the server stack selects it. This host answers **Y** to the server-stack question and is asked no SSH target, and the remaining questions and answers are those [Installing a worker](/docs/getting-started/worker-install.md#the-install-prompts) lists.

A same-machine answer removes `WORKER_SSH_TARGET` and `WORKER_WSL_DISTRIBUTION` from `config/worker.toml`, writes `/app` as the project root, and joins the account to the `docker` group alone. After the group check, the install creates the checkout's `models/` directory at mode 0700, because the `worker` service binds it with host-path creation turned off. The install then writes `docker-compose.worker.yml` at mode 0644 from the detected host: the ROCm device nodes with the `render` and `video` group ids, or the NVIDIA runtime with the three CUDA toolkit sonames. A missing NVIDIA runtime and a missing CUDA library each stop with one message. A CUDA container exposes the configured card as ordinal zero, so its model children select it with `CUDA_VISIBLE_DEVICES=0`.

Preparation builds the `worker` service image as well as the other resources, because the stage runs its helpers in that container. When the service is already running, the install recreates it with the generated GPU override, and a stopped service is left for the first stage reservation to start with the current override. [The same-machine worker service](/docs/operations/server-containers.md#the-same-machine-worker-service) states the container and the override.

## The GPU backend

`WORKER_GPU_BACKEND` selects the backend, and [Installing a worker](/docs/getting-started/worker-install.md#the-gpu-backend) states the build, the launch environment and the vendor source. A native Linux ROCm worker installs the package `rocm` alone, because the ROCDXG runtime belongs to a WSL device path. A CUDA worker installs no GPU package, because its driver and CUDA toolkit are installed on the host.

## Model presets

The install offers the three tracked presets, and [Model presets](/docs/getting-started/model-presets.md) states each preset and how the install applies one.

## The reboot rule

The install offers the one reboot-only sudoers rule, and a native Linux worker reached over SSH proves a changed kernel boot id through it. A declined rule is installed with this command, after replacing `@USER@` with the worker account:

```bash
sed '/^WORKER_ACCOUNT /s/WORKER_ACCOUNT/@USER@/' \
  examples/deploy/course-explorer-worker-reboot.sudoers.in \
  | sudo tee /etc/sudoers.d/course-explorer-worker-reboot >/dev/null
sudo chmod 0440 /etc/sudoers.d/course-explorer-worker-reboot
sudo visudo -c -f /etc/sudoers.d/course-explorer-worker-reboot
```

A same-machine worker restarts its host through one fixed privileged one-shot over the worker container's Docker socket, and the changed kernel boot id is the proof. That restart takes the site and the console down for the boot, and the administration backend's startup resume relaunches the interrupted session when the stack returns. [The periodic reboot](/docs/operations/worker-sessions.md#the-periodic-reboot) states the clock, the checkpoint and the resume.
