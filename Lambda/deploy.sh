#!/bin/bash
#
# Deploy Lambda/src/ to the PRODUCTION montlake_upload function.
# This is the real deal — it replaces whatever is currently running.
#
# Usage:
#   ./Lambda/deploy.sh              # deploy from Lambda/src/
#   ./Lambda/deploy.sh --dry-run    # just build the zip, don't deploy

set -euo pipefail
cd "$(dirname "$0")"

FUNCTION_NAME="montlake_upload"
AWS_PROFILE="${AWS_PROFILE:-default}"
ZIP_FILE="/tmp/lambda_deploy.zip"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

rm -rf src/vendor
# Skip pip install when requirements.txt has no real entries — the Lambda
# is pure-Python stdlib + boto3 (runtime-provided) since the 2026-04-27
# slim refactor.
if grep -E -q '^[[:space:]]*[^#[:space:]]' requirements.txt; then
    echo "Installing vendored deps into src/vendor/ from requirements.txt"
    # Lambda runs Linux ARM64 (Graviton) as of 2026-04-27. Ask pip for those
    # wheels regardless of host OS; otherwise native bits ship for the host
    # arch and the runtime fails at import. manylinux2014_aarch64 works on
    # AL2023 + py3.12.
    pip install --quiet \
        --target src/vendor \
        --platform manylinux2014_aarch64 \
        --only-binary=:all: \
        --python-version 3.12 \
        --implementation cp \
        -r requirements.txt
else
    echo "requirements.txt is empty — no vendored deps."
fi

echo "Packaging Lambda from: src/"
cd src
rm -f "$ZIP_FILE"
zip -r "$ZIP_FILE" . -x '*.pyc' '__pycache__/*' '*.ipynb' '.DS_Store' '*.dist-info/*'
cd ..

echo "Zip: $ZIP_FILE ($(du -h "$ZIP_FILE" | cut -f1))"

if $DRY_RUN; then
    echo "[DRY RUN] Would deploy to: $FUNCTION_NAME"
    echo "Inspect zip: unzip -l $ZIP_FILE"
    exit 0
fi

echo "Deploying to PRODUCTION: $FUNCTION_NAME"
# `--architectures arm64` ships the function on Graviton — cheaper compute
# and slightly faster cold init. Architecture lives with the code, not the
# config, so it goes on update-function-code. Idempotent on re-deploy.
# The slim Lambda has no native deps, so no cross-build is required.
aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://$ZIP_FILE" \
    --architectures arm64 \
    --profile "$AWS_PROFILE"

echo ""
echo "Done. Run remote tests to verify:"
echo "  python Lambda/test_remote.py"
