#!/usr/bin/env bash
# Copy the report scripts from S3 (uploaded by `vh tools`) to the server. They stay private: not in git.
. "$(dirname "$0")/common.sh"
mkdir -p "$DATA_DIR/tools"
aws s3 sync --only-show-errors --delete "s3://$BUCKET/tools/" "$DATA_DIR/tools/"
ls -l "$DATA_DIR/tools"
