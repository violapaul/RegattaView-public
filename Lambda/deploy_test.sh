#!/bin/bash
#
# Deploy Lambda/src/ to a TEST Lambda function, leaving production untouched.
#
# On first run this creates a new Lambda function called montlake_upload_test
# with the same role as production. Subsequent runs just update the code.
#
# After deploying, run:
#   python Lambda/test_remote.py --url <test function URL>

set -euo pipefail
cd "$(dirname "$0")"

PROD_FUNCTION="montlake_upload"
TEST_FUNCTION="montlake_upload_test"
AWS_PROFILE="${AWS_PROFILE:-default}"
ZIP_FILE="/tmp/lambda_deploy_test.zip"

echo "Installing vendored deps into src/vendor/ from requirements.txt"
rm -rf src/vendor
# Cross-build wheels for the Lambda runtime, not the macOS host. See deploy.sh.
pip install --quiet \
    --target src/vendor \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --python-version 3.12 \
    --implementation cp \
    -r requirements.txt

echo "Packaging Lambda from: src/"
cd src
rm -f "$ZIP_FILE"
zip -r "$ZIP_FILE" . -x '*.pyc' '__pycache__/*' '*.ipynb' '.DS_Store' '*.dist-info/*'
cd ..

echo "Zip: $ZIP_FILE ($(du -h "$ZIP_FILE" | cut -f1))"

# Check if the test function already exists
if aws lambda get-function --function-name "$TEST_FUNCTION" --profile "$AWS_PROFILE" &>/dev/null; then
    echo "Updating existing test function: $TEST_FUNCTION"
    aws lambda update-function-code \
        --function-name "$TEST_FUNCTION" \
        --zip-file "fileb://$ZIP_FILE" \
        --profile "$AWS_PROFILE"
else
    echo "Creating new test function: $TEST_FUNCTION"

    # Get the role ARN from the production function
    ROLE_ARN=$(aws lambda get-function-configuration \
        --function-name "$PROD_FUNCTION" \
        --profile "$AWS_PROFILE" \
        --query 'Role' \
        --output text)

    PROD_RUNTIME=$(aws lambda get-function-configuration \
        --function-name "$PROD_FUNCTION" \
        --profile "$AWS_PROFILE" \
        --query 'Runtime' \
        --output text)

    # Use python3.12 for new functions since older runtimes are blocked
    RUNTIME="python3.12"
    if [[ "$PROD_RUNTIME" != "$RUNTIME" ]]; then
        echo "  Note: prod uses $PROD_RUNTIME, test will use $RUNTIME"
    fi

    HANDLER=$(aws lambda get-function-configuration \
        --function-name "$PROD_FUNCTION" \
        --profile "$AWS_PROFILE" \
        --query 'Handler' \
        --output text)

    echo "  Role:    $ROLE_ARN"
    echo "  Runtime: $RUNTIME"
    echo "  Handler: $HANDLER"

    aws lambda create-function \
        --function-name "$TEST_FUNCTION" \
        --runtime "$RUNTIME" \
        --role "$ROLE_ARN" \
        --handler "$HANDLER" \
        --zip-file "fileb://$ZIP_FILE" \
        --profile "$AWS_PROFILE"

    echo "Enabling Function URL for $TEST_FUNCTION..."
    aws lambda create-function-url-config \
        --function-name "$TEST_FUNCTION" \
        --auth-type NONE \
        --profile "$AWS_PROFILE" || true

    # Grant public invoke permission
    aws lambda add-permission \
        --function-name "$TEST_FUNCTION" \
        --statement-id FunctionURLAllowPublicAccess \
        --action lambda:InvokeFunctionUrl \
        --principal '*' \
        --function-url-auth-type NONE \
        --profile "$AWS_PROFILE" 2>/dev/null || true
fi

# Get the function URL
TEST_URL=$(aws lambda get-function-url-config \
    --function-name "$TEST_FUNCTION" \
    --profile "$AWS_PROFILE" \
    --query 'FunctionUrl' \
    --output text 2>/dev/null || echo "")

echo ""
echo "Done. Test function deployed: $TEST_FUNCTION"
if [[ -n "$TEST_URL" ]]; then
    echo "URL: $TEST_URL"
    echo ""
    echo "Run tests against it:"
    echo "  python Lambda/test_remote.py --url $TEST_URL"
else
    echo ""
    echo "No Function URL configured yet. You can add one via:"
    echo "  aws lambda create-function-url-config --function-name $TEST_FUNCTION --auth-type NONE --profile $AWS_PROFILE"
fi
