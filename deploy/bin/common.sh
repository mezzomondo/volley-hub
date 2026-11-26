# Sourced by the server scripts. Settings come from /opt/volley-hub/.env.prod.
set -euo pipefail
REPO=/opt/volley-hub
ENV_FILE=$REPO/.env.prod
set -a; . "$ENV_FILE"; set +a
dc() { docker compose -f "$REPO/deploy/compose.yml" --env-file "$ENV_FILE" "$@"; }
