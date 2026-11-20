#!/usr/bin/env bash
# Run the self-host workflows, the container stack maintenance and the admitted deployment through
# one Docker-only entry.
#
# Usage:
#   scripts/self-host.sh example [--shared-root PATH]
#   scripts/self-host.sh real-database [--shared-root PATH]
#   scripts/self-host.sh up [--shared-root PATH]
#   scripts/self-host.sh down [--shared-root PATH]
#   scripts/self-host.sh publish [--shared-root PATH]
#   scripts/self-host.sh unpublish [--shared-root PATH]
#   scripts/self-host.sh deploy [--shared-root PATH]
#
# `example` and `real-database` run one setup workflow inside the administration image and start
# the services. `up` builds and starts the configured stack, and `down` stops it and keeps every
# volume and dataset. `publish` and `unpublish` write or remove the configured instance's site file
# of the machine-wide proxy, and `deploy` runs one container deployment of the checkout.
#
# The setup image is tagged per checkout, so two checkouts on one daemon never share it. The
# `example`, `real-database`, `up` and `deploy` verbs build it, and `down`, `publish` and `unpublish`
# use the image the checkout already has. The script creates the account's `~/.ssh` directory at
# mode 0700 when it is absent. `publish` and `unpublish` require `--shared-root` or
# `COURSE_EXPLORER_SHARED_ROOT`. The two workflows offer the option, and it is an explicit override
# on `up`, `down` and `deploy`, where an absent value leaves the configured `ADMIN_SHARED_ROOT` in
# charge.

set -euo pipefail

readonly SOCKET="/var/run/docker.sock"

CHECKOUT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="course-explorer-setup:$(printf '%s' "$CHECKOUT" | sha256sum | cut -c1-16)"
SHARED_ROOT="${COURSE_EXPLORER_SHARED_ROOT:-$HOME/.local/share/course-explorer}"
SHARED_ROOT_GIVEN=0
if [[ -n "${COURSE_EXPLORER_SHARED_ROOT:-}" ]]; then
    SHARED_ROOT_GIVEN=1
fi
VERBS="example real-database up down publish unpublish deploy"

usage() {
    printf 'usage: scripts/self-host.sh {%s} [--shared-root PATH]\n' "${VERBS// /|}" >&2
}

refuse() {
    printf 'self-host: %s\n' "$1" >&2
    exit 1
}

verb="${1:-}"
if [[ -z "$verb" ]]; then
    usage
    exit 1
fi
if [[ "$verb" == "-h" || "$verb" == "--help" ]]; then
    usage
    exit 0
fi
case " $VERBS " in
    *" $verb "*) ;;
    *)
        usage
        exit 1
        ;;
esac
shift
while [[ $# -gt 0 ]]; do
    case "$1" in
        --shared-root)
            [[ $# -ge 2 ]] || refuse "--shared-root needs a path"
            SHARED_ROOT="$2"
            SHARED_ROOT_GIVEN=1
            shift 2
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

if ! docker --version >/dev/null 2>&1; then
    refuse "Docker is not available; install Docker Engine"
fi
if ! docker compose version >/dev/null 2>&1; then
    refuse "Docker Compose is not available; install the Compose plugin"
fi
if ! docker info >/dev/null 2>&1; then
    refuse "the Docker daemon does not answer; start it and retry"
fi
if ! git -C "$CHECKOUT" rev-parse --git-dir >/dev/null 2>&1; then
    refuse "Git is not available, or $CHECKOUT is not a Git checkout"
fi
if [[ ! -w "$SOCKET" ]]; then
    refuse "this account cannot write $SOCKET; add it to the docker group and start a new login session"
fi

validate_shared_root() {
    case "$SHARED_ROOT" in
        /*) ;;
        *) refuse "the shared directory must be an absolute path" ;;
    esac
    case "$SHARED_ROOT/" in
        "$CHECKOUT"/*) refuse "the shared directory must be outside the checkout" ;;
    esac
}

prepare_shared_root() {
    validate_shared_root
    mkdir -p -- "$SHARED_ROOT" || refuse "the shared directory $SHARED_ROOT could not be created"
}

# `example`, `real-database`, `publish` and `unpublish` pass the shared directory into the setup
# image and mount it. The other verbs read the configured value unless one is supplied.
case "$verb" in
    example|real-database|publish|unpublish) USES_SHARED_ROOT=1 ;;
    *) USES_SHARED_ROOT=0 ;;
esac

if [[ "$USES_SHARED_ROOT" == 1 && -t 0 && -t 1 && ( "$verb" == "example" || "$verb" == "real-database" ) ]]; then
    printf 'Shared directory [%s]: ' "$SHARED_ROOT"
    IFS= read -r answer || answer=""
    if [[ -n "$answer" ]]; then
        SHARED_ROOT="$answer"
    fi
fi

if [[ "$USES_SHARED_ROOT" == 1 ]]; then
    if [[ "$SHARED_ROOT_GIVEN" == 0 && ( "$verb" == "publish" || "$verb" == "unpublish" ) ]]; then
        refuse "$verb needs --shared-root or COURSE_EXPLORER_SHARED_ROOT"
    fi
    prepare_shared_root
    SHARED_ROOT_GIVEN=1
elif [[ "$SHARED_ROOT_GIVEN" == 1 ]]; then
    validate_shared_root
fi

SSH_DIR="${HOME}/.ssh"
if [[ ! -d "$SSH_DIR" ]]; then
    mkdir -m 700 -- "$SSH_DIR" || refuse "the SSH directory $SSH_DIR could not be created"
fi

# The setup image names the account it runs as, as the stack's server images do.
build_image() {
    docker build --target admin \
        --build-arg COURSE_EXPLORER_UID="$(id -u)" --build-arg COURSE_EXPLORER_GID="$(id -g)" \
        -f "$CHECKOUT/docker/server/Dockerfile" -t "$IMAGE" "$CHECKOUT"
}

require_image() {
    docker image inspect "$IMAGE" >/dev/null 2>&1 ||
        refuse "the setup image $IMAGE is missing; run the example, real-database, up or deploy verb first"
}

run_image() {
    local tty=()
    local shared_env=()
    local progress_env=()
    local shared_mount=()
    if [[ -n "${COURSE_EXPLORER_PROGRESS_PREFIX:-}" ]]; then
        progress_env=(--env "COURSE_EXPLORER_PROGRESS_PREFIX=$COURSE_EXPLORER_PROGRESS_PREFIX")
    fi
    if [[ -t 0 && -t 1 ]]; then
        tty=(--tty)
    fi
    if [[ "$SHARED_ROOT_GIVEN" == 1 ]]; then
        shared_env=(--env "ADMIN_SHARED_ROOT=$SHARED_ROOT")
    fi
    if [[ "$USES_SHARED_ROOT" == 1 ]]; then
        shared_mount=(--volume "$SHARED_ROOT:$SHARED_ROOT")
    fi
    docker run \
        --rm \
        --interactive \
        ${tty[@]+"${tty[@]}"} \
        --network host \
        --user "$(id -u):$(id -g)" \
        --group-add "$(stat -c '%g' "$SOCKET")" \
        --env HOME=/home/course \
        --env "PYTHONPATH=$CHECKOUT/src" \
        ${shared_env[@]+"${shared_env[@]}"} \
        ${progress_env[@]+"${progress_env[@]}"} \
        --env "COURSE_EXPLORER_HOST_ROOT=$CHECKOUT" \
        --env "COURSE_EXPLORER_HOST_HOME=$HOME" \
        --env "COURSE_EXPLORER_SSH_DIR=$SSH_DIR" \
        --volume "$CHECKOUT:$CHECKOUT" \
        ${shared_mount[@]+"${shared_mount[@]}"} \
        --volume "$SOCKET:$SOCKET" \
        --workdir "$CHECKOUT" \
        --entrypoint python \
        "$IMAGE" "$@"
}

case "$verb" in
    example|real-database|up|deploy)
        build_image
        ;;
    down|publish|unpublish)
        require_image
        ;;
esac

case "$verb" in
    example|real-database|publish|unpublish)
        run_image scripts/admin.py self-host "$verb"
        ;;
    up)
        run_image scripts/admin.py self-host up
        ;;
    deploy)
        run_image scripts/admin.py deploy
        ;;
    down)
        run_image -m course_explorer_admin.compose --non-interactive -- down
        ;;
esac
