#!/usr/bin/env bash
# Deploy one admitted commit to this checkout's containers.
#
# Usage: deploy/deploy.sh <tested-commit>
# <tested-commit> is the full commit SHA the caller admitted and prints as deployed.
#
# The body runs the checkout's own deploy verb inside the administration image. That sequence
# refuses while a stage is busy, builds the database and embedding images, starts those services,
# enrolls the served dataset, migrates it and builds its serving artifacts, builds and starts the
# site and administration containers, a sender that already runs and the worker container on a
# same-machine host, proves both HTML shells and prunes the superseded artifact builds. The branch,
# revision and dirty-check admission belong to the workflow that calls this script, and a failure
# prints the deploy command's step line and its own content-free error and guidance.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: deploy/deploy.sh <tested-commit>" >&2
  exit 2
fi
tested_commit="$1"
checkout="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

"$checkout/scripts/self-host.sh" deploy

printf 'deployed %s\n' "$tested_commit"
