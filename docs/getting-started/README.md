# Getting started

The getting-started pages install a copy, fill its database and prepare a worker.

## The quick path

A newcomer clones the checkout, runs `scripts/self-host.sh example`, and opens the address the run prints. The workflow asks for a shared directory and a university instance, writes the working configuration, builds the images and starts the stack, and the served site presents the invented example dataset. [Installation](/docs/getting-started/installation.md) states the requirements, the two commands and the stop and start commands.

## The worker is optional

The worker is optional: ingestion and embedding run on the server, and [Installation](/docs/getting-started/installation.md#the-worker-is-optional) states what a site without a worker serves and how the three feature switches decide its features.

## Model presets

The worker install offers three model presets, and [Model presets](/docs/getting-started/model-presets.md) lists each preset's files, contexts and card size.

## Tested hardware and presets

The table names the hardware and worker combinations that a run exercises, and the model presets whose files are measured.

| Area | What it covers |
|---|---|
| Server containers | Both image builds run on an ARM64 server and on an x86-64 Linux Mint 22 virtual machine. The example setup runs on the ARM64 server. The example setup from a fresh clone, and two instances side by side, run on the x86-64 virtual machine. The machine-wide site proxy runs on the x86-64 virtual machine. A site that serves real university data runs on the ARM64 server. |
| WSL and ROCm | RX 9070 XT: the worker preflight, one translation slice, a Windows reboot and resume, and an install on a fresh WSL distribution. |
| WSL and CUDA | RTX 3060: the worker preflight and one translation slice. |
| Native Linux and ROCm | RX 9070 XT: the worker preflight, one translation slice, a remote reboot and resume, and an install in a fresh account. |
| Native Linux and CUDA | RTX 3060: the worker preflight and one translation slice. |
| Worker install | Ubuntu 24.04 under WSL and Linux Mint 22 on a native host. Another Ubuntu-based distribution installs from the codename its release file reports and is untested. |
| Same-machine ROCm | RX 9070 XT: a translation cycle and an inference cycle, and a reboot that resumes the inference session. |
| Same-machine CUDA | RTX 3060: one translation slice and the worker preflight. |
| 12 GB preset | The preset is measured and run on the RTX 3060. |
| 8 GB preset | The preset is measured on the RTX 3060. |
| 16 GB preset | Its main key is the key the example configuration carries. Its translation file is the published Q5_K_M conversion of the same TranslateGemma 12B model that the stored translations name. Its fallback is the 12 GB preset's main file. Its main key and its fallback each return the stage's result format on the RX 9070 XT, and its install passes a worker preflight. |

The untested combinations are an 8 GB card, a machine with 16 GB of system RAM, a second NVIDIA card index on one host, a second GPU target on one backend, another Ubuntu-based distribution, and a WSL distribution without systemd.

## In reading order

1. [Installation](/docs/getting-started/installation.md) takes a clone to a served example site through one command, and states the stop and start commands.
2. [Self-hosting](/docs/getting-started/self-hosting.md) runs a copy: its stack, its two serve modes, the machine-wide site proxy, several instances on one server and the update steps.
3. [The example dataset](/docs/getting-started/example-dataset.md) describes the loader that fills the configured database from the invented fixture, the artifact action and the explicit rewrite of the bundled vectors.
4. [Preparing a dataset from the curriculum source](/docs/getting-started/data-preparation.md) copies the Selected dataset to Working, runs ingestion and embedding, reads the coverage and summary reports, and migrates a dataset role.
5. [Installing a worker](/docs/getting-started/worker-install.md) states the install command, its prompts, its refusals, the GPU backend and the update and check that every worker shares.
6. [A Windows WSL worker](/docs/getting-started/worker-wsl.md) installs and runs the worker stages on Windows with a ROCm or CUDA card.
7. [A Linux worker](/docs/getting-started/worker-linux.md) installs a remote or same-machine worker on native Linux with a ROCm or CUDA card.
8. [Model presets](/docs/getting-started/model-presets.md) states the three worker model presets, the files each names and how the install writes one.
