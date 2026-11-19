# A Windows WSL worker

This page installs and runs the worker stages on a Windows host that runs its checkout in WSL.

## What the install needs

- Windows with WSL 2 and an Ubuntu-based distribution. Ubuntu 24.04 is the tested distribution.
- `systemd=true` in `/etc/wsl.conf`, and a distribution that runs systemd as its first process.
- An AMD card exposed to that distribution at the configured compute target for `rocm`, or an NVIDIA card whose driver and CUDA toolkit let `nvcc` and `nvidia-smi` answer for `cuda`. A WSL CUDA worker's toolkit is installed inside the distribution, and only the driver is on Windows.
- For `rocm`, the ROCDXG device runtime as the `rocdxg-roct` release package from [librocdxg](https://github.com/ROCm/librocdxg/releases). No apt source carries it.
- A checkout of this repository at the configured `WORKER_PROJECT_ROOT`.
- Git, `curl` and the account's own `sudo` password.
- SSH access from the server to the Windows host.
- Disk space under the account's home directory for the two llama.cpp checkouts under `~/.local/src/` and for up to four model artifacts under `models/`.

The install runs on the worker in its own terminal. The code update and the preflight run on the server. The worker identity lives in the `config/worker.toml` of the server and of the worker checkout. [The worker connection values](/docs/operations/worker-transport.md#the-worker-connection-values) lists the keys, and [what the worker must provide](/docs/operations/worker-transport.md#what-the-worker-must-provide) states what each requirement follows from.

## Install the worker

Run the command from the top of the worker checkout:

```bash
bash scripts/install-worker.sh
```

The command asks the questions that [Installing a worker](/docs/getting-started/worker-install.md#the-install-prompts) lists, in that order, and it asks for the `sudo` password when a privileged step needs it. This host answers **N** to the server-stack question, because it is a remote worker.

[Installing a worker](/docs/getting-started/worker-install.md#the-install-steps) states the install steps, the resource report the command prints and its refusals, and [the WSL memory block](#the-wsl-memory-block) below is the host's last step.

## The WSL memory block

The install prints this block and waits until it is set in the Windows user's `.wslconfig`:

```text
[wsl2]
memory=8GB
swap=8GB
```

The block is sized for the published 16 GB configuration and leaves the rest to Windows. A machine with more RAM raises `memory`. The install writes nothing on the Windows side.

## The GPU backend

`WORKER_GPU_BACKEND` selects the backend, and [Installing a worker](/docs/getting-started/worker-install.md#the-gpu-backend) states the build, the vendor source and the readiness read. A ROCm worker installs the package `rocm`, and a ROCm worker inside WSL adds the `rocdxg-roct` device runtime and launches the model children with `HSA_ENABLE_DXG_DETECTION=1`, which this device path needs. A CUDA worker installs no GPU package, because its toolkit is installed inside the distribution and only the driver is on Windows.

## Model presets

The install offers the three tracked presets, and [Model presets](/docs/getting-started/model-presets.md) states each preset and how the install applies one.

## The Windows reboot

A WSL worker restarts Windows, then proves a changed Windows boot and a healthy WSL before the session resumes. [The periodic reboot](/docs/operations/worker-sessions.md#the-periodic-reboot) states the clock, the checkpoint and the resume.
