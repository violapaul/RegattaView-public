#!/bin/bash
#
# Download the currently deployed Lambda code from AWS.
# Useful to check if the deployed code has drifted from the local copy.
#
# Writes to Lambda/deployed/ so you can diff against Lambda/src/.

set -euo pipefail
cd "$(dirname "$0")"

FUNCTION_NAME="montlake_upload"
AWS_PROFILE="${AWS_PROFILE:-default}"
OUT_DIR="deployed"
ZIP_FILE="/tmp/lambda_download.zip"

echo "Fetching deployed code for: $FUNCTION_NAME"

# Get the code location URL
CODE_URL=$(aws lambda get-function \
    --function-name "$FUNCTION_NAME" \
    --profile "$AWS_PROFILE" \
    --query 'Code.Location' \
    --output text)

echo "Downloading zip..."
curl -sL "$CODE_URL" -o "$ZIP_FILE"

echo "Extracting to $OUT_DIR/"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
unzip -q "$ZIP_FILE" -d "$OUT_DIR"

echo ""
echo "Done. Compare with local source:"
echo "  diff -r src/ deployed/"
echo ""

# Run diff automatically (don't fail on differences)
diff -r src/ "$OUT_DIR/" && echo "No differences — local and deployed are in sync." || true
