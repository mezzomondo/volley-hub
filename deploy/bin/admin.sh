#!/usr/bin/env bash
# Run a volleyhub admin command (create-user, list-users, revoke, rotate-token).
. "$(dirname "$0")/common.sh"
dc exec -T api volleyhub "$@"
