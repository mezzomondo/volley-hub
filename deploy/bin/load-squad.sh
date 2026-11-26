#!/usr/bin/env bash
# Fetch a squad CSV from S3 and load it.
#   load-squad.sh <squads/... .csv key> --team "Onyx NVL" --season 2026-27 [more load_squad.R options]
. "$(dirname "$0")/common.sh"
key=$1; shift
mkdir -p "$(dirname "$DATA_DIR/$key")"
aws s3 cp --only-show-errors "s3://$BUCKET/$key" "$DATA_DIR/$key"
dc run --rm -T ingest load_squad.R "/data/$key" "$@"
