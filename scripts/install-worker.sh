#!/usr/bin/env bash
# Install the worker stack on this host from one interactive command.
#
# Usage:
#   scripts/install-worker.sh
#
# `-h` or `--help` prints the usage line, and any other argument is refused. Run it from the checkout
# that serves as the worker project root. It installs the packages, Docker, the GPU vendor stack for
# the configured backend, the pinned runtimes and the worker's working files, and it offers the
# reboot rule on a native Linux host.

set -euo pipefail

usage() {
    printf 'usage: scripts/install-worker.sh\n'
}

case "${1:-}" in
    "") ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac

readonly UV_INSTALL_URL="https://astral.sh/uv/install.sh"

CHECKOUT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

refuse() {
    printf 'install-worker: %s\n' "$1" >&2
    exit 1
}

if ! command -v git >/dev/null 2>&1; then
    refuse "Git is not available"
fi
if ! command -v curl >/dev/null 2>&1; then
    refuse "curl is not available"
fi
if ! git -C "$CHECKOUT" rev-parse --git-dir >/dev/null 2>&1; then
    refuse "$CHECKOUT is not a Git checkout"
fi

if ! command -v uv >/dev/null 2>&1; then
    curl --disable --fail --location --proto '=https' --tlsv1.2 "$UV_INSTALL_URL" \
        | UV_NO_MODIFY_PATH=1 sh
fi
export PATH="$HOME/.local/bin:$PATH"

cd -- "$CHECKOUT"
uv sync --locked
exec uv run --no-sync --offline --no-python-downloads \
    python -m course_explorer_admin.worker_install
