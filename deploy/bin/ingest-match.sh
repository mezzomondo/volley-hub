#!/usr/bin/env bash
# Fetch a match from S3 and load it.
#   ingest-match.sh "<team>" <scouts/... .dvw key> [<videos/... key>]
. "$(dirname "$0")/common.sh"
team=$1; dvw_key=$2; video_key=${3:-}
mkdir -p "$(dirname "$DATA_DIR/$dvw_key")" "$DATA_DIR/videos"
aws s3 cp --only-show-errors "s3://$BUCKET/$dvw_key" "$DATA_DIR/$dvw_key"
args=(ingest_dvw.R "/data/$dvw_key" --team "$team")
if [ -n "$video_key" ]; then
  name=$(basename "$video_key")
  aws s3 cp --only-show-errors "s3://$BUCKET/$video_key" "$DATA_DIR/videos/$name"
  args+=(--video-key "$name")
fi
dc run --rm -T ingest "${args[@]}"
dc exec -T api volleyhub scout-check --source-file "$(basename "$dvw_key")" || true
