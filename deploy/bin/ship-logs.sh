#!/usr/bin/env bash
# Hourly: copy the containers' log lines since the last run to S3, one gzip per hour:
#   s3://$BUCKET/logs/YYYY/MM/DD/YYYY-MM-DDTHHMMZ.log.gz
# Each line: <timestamp> <service> | <message>. The api's messages are JSON (event=http|tool|login|...).
. "$(dirname "$0")/common.sh"
state=$DATA_DIR/logs/last-shipped
mkdir -p "$DATA_DIR/logs"
until=$(date -u +%Y-%m-%dT%H:%M:%SZ)
since=$(cat "$state" 2>/dev/null || date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)
tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.gz"' EXIT
dc logs --no-color --timestamps --since "$since" --until "$until" > "$tmp"
if [ -s "$tmp" ]; then
  gzip -9 "$tmp"
  key="logs/$(date -u -d "$until" +%Y/%m/%d)/$(date -u -d "$until" +%Y-%m-%dT%H%MZ).log.gz"
  aws s3 cp --only-show-errors "$tmp.gz" "s3://$BUCKET/$key"
fi
echo "$until" > "$state"
