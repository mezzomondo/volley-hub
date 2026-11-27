#!/usr/bin/env bash
# Load one season of the drill library: s3://$BUCKET/library/<season>/ -> $DATA_DIR/library/<season>/, then import.
#   import-library.sh 2025-26
. "$(dirname "$0")/common.sh"
season=$1
mkdir -p "$DATA_DIR/library/$season"
aws s3 sync --only-show-errors --delete "s3://$BUCKET/library/$season/" "$DATA_DIR/library/$season/"
dc exec -T api volleyhub import-library "/data/library/$season/bundle.json"
