#!/usr/bin/env bash
# Pull the latest code and restart the stack.
. "$(dirname "$0")/common.sh"
git -C "$REPO" fetch --quiet origin
git -C "$REPO" checkout --quiet "${REPO_REF:-main}"
git -C "$REPO" pull --quiet --ff-only || true
# Continue with the version just pulled (this file may have changed).
[ -z "${VH_UPDATE_REEXEC:-}" ] && VH_UPDATE_REEXEC=1 exec "$REPO/deploy/bin/update.sh"
dc up -d --build --remove-orphans
dc --profile tools build ingest   # the ingest image bakes in the R scripts
dc ps
