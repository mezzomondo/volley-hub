#!/usr/bin/env bash
# Nightly: database dump and new clips to S3. Videos and scouts are already in S3.
. "$(dirname "$0")/common.sh"
stamp=$(date -u +%Y-%m-%dT%H%MZ)
dc exec -T db pg_dump -U volleyhub -Fc volleyhub > "/tmp/volleyhub-$stamp.dump"
aws s3 cp --only-show-errors "/tmp/volleyhub-$stamp.dump" "s3://$BUCKET/backups/db/volleyhub-$stamp.dump"
rm -f "/tmp/volleyhub-$stamp.dump"
aws s3 sync --only-show-errors "$DATA_DIR/clips" "s3://$BUCKET/clips"
echo "backup $stamp done"
