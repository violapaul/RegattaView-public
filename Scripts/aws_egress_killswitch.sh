#!/bin/bash
# aws_egress_killswitch.sh
#
# Stop S3 egress fast in response to a cost-budget alert by disabling the
# CloudFront distribution that fronts the regattaanalysis bucket.
#
# Usage:
#   Scripts/aws_egress_killswitch.sh off    # disable CF (site goes 5xx)
#   Scripts/aws_egress_killswitch.sh on     # re-enable CF (site recovers)
#   Scripts/aws_egress_killswitch.sh        # default: off
#
# Notes:
# - CloudFront propagation takes ~10 min after the API call.
# - The S3 bucket policy already denies anonymous public reads (BPA on);
#   only CF (via OAI) and Lambda-signed URLs can fetch. This script only
#   addresses the CF path; Lambda-signed URLs continue to work, so a
#   determined client with a fresh signed URL can still pull data. For a
#   total stop, also temporarily detach s3:GetObject from the Lambda's
#   IAM role.
# - Idempotent: re-running with the same target is a no-op.
# - Requires aws CLI + jq. AWS_PROFILE inherited from the environment.
set -euo pipefail

DIST_ID="${CF_DIST_ID:-EPCTF7XCNPLK2}"
ACTION="${1:-off}"

case "$ACTION" in
  off) TARGET="false" ;;
  on)  TARGET="true"  ;;
  *)   echo "Usage: $0 [off|on]" >&2; exit 2 ;;
esac

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found; install via 'brew install jq'" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

aws cloudfront get-distribution-config --id "$DIST_ID" --output json > "$TMP/cf.json"
ETAG=$(jq -r .ETag                         "$TMP/cf.json")
CUR=$( jq -r .DistributionConfig.Enabled   "$TMP/cf.json")
DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName' --output text)

echo "distribution $DIST_ID ($DOMAIN)"
echo "  current Enabled: $CUR"
echo "  target Enabled : $TARGET"

if [ "$CUR" = "$TARGET" ]; then
  echo "no change needed"
  exit 0
fi

jq ".DistributionConfig.Enabled = $TARGET | .DistributionConfig" "$TMP/cf.json" > "$TMP/cf-new.json"

aws cloudfront update-distribution \
  --id "$DIST_ID" \
  --if-match "$ETAG" \
  --distribution-config "file://$TMP/cf-new.json" \
  --query 'Distribution.{Id:Id,Status:Status,Enabled:DistributionConfig.Enabled}' \
  --output table

echo "submitted; CloudFront takes ~10 min to fully deploy"
