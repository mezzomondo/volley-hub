#!/usr/bin/env bash
# One-off: create the private, versioned S3 bucket for Terraform state and write backend.hcl.
# The state holds secrets (database password, signing key): never commit it.
set -euo pipefail
export AWS_PROFILE=${VH_AWS_PROFILE:-volley-hub}
REGION=${AWS_REGION:-eu-west-2}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
B="volley-hub-tfstate-${ACCOUNT}-${REGION}"

if ! aws s3api head-bucket --bucket "$B" 2>/dev/null; then
  aws s3api create-bucket --bucket "$B" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$B" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$B" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$B" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-ownership-controls --bucket "$B" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-tagging --bucket "$B" --tagging 'TagSet=[{Key=project,Value=volley-hub}]'
aws s3api put-bucket-lifecycle-configuration --bucket "$B" --lifecycle-configuration \
  '{"Rules":[{"ID":"old-versions","Status":"Enabled","Filter":{},"NoncurrentVersionExpiration":{"NoncurrentDays":90,"NewerNoncurrentVersions":10}}]}'

cd "$(dirname "$0")/../infra/terraform"
printf 'bucket  = "%s"\nprofile = "%s"\n' "$B" "$AWS_PROFILE" > backend.hcl
echo "state bucket: $B"
echo "next: cd infra/terraform && terraform init -backend-config=backend.hcl"
