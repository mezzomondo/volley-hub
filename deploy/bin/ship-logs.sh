#!/usr/bin/env bash
# Hourly (and at shutdown):
#  - api events, pure JSON lines:  $DATA_DIR/logs/events/YYYY-MM-DD.jsonl -> s3://$BUCKET/logs/events/
#    (read them with DuckDB: read_json('s3://BUCKET/logs/events/*.jsonl'))
#  - all containers' output since the last run, one gzip per hour -> s3://$BUCKET/logs/raw/YYYY/MM/DD/
# Local event files older than 120 days are removed (S3 keeps them 365 days).
. "$(dirname "$0")/common.sh"
logs=$DATA_DIR/logs
mkdir -p "$logs/events"
aws s3 sync --only-show-errors "$logs/events/" "s3://$BUCKET/logs/events/"
find "$logs/events" -name '*.jsonl' -mtime +120 -delete

state=$logs/last-shipped
until=$(date -u +%Y-%m-%dT%H:%M:%SZ)
since=$(cat "$state" 2>/dev/null || date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)
tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.gz"' EXIT
dc logs --no-color --timestamps --since "$since" --until "$until" > "$tmp"
if [ -s "$tmp" ]; then
  gzip -9 "$tmp"
  key="logs/raw/$(date -u -d "$until" +%Y/%m/%d)/$(date -u -d "$until" +%Y-%m-%dT%H%MZ).log.gz"
  aws s3 cp --only-show-errors "$tmp.gz" "s3://$BUCKET/$key"
fi
echo "$until" > "$state"
