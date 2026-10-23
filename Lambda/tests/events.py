"""
Test event factories for the Lambda handler.

Lambda Function URL events wrap the HTTP body in a specific envelope.
These helpers build that envelope so you can call lambda_handler() locally.
"""

import json
import base64

LAMBDA_HOST = "blsywilg2fba6564nixqxwntyy0iagoj.lambda-url.us-west-2.on.aws"


def make_event(body_dict, method="POST"):
    """Build a Lambda Function URL event envelope around a JSON body."""
    body_json = json.dumps(body_dict)
    body_b64 = base64.b64encode(body_json.encode()).decode()

    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/",
        "rawQueryString": "",
        "headers": {
            "content-type": "application/json",
            "host": LAMBDA_HOST,
        },
        "requestContext": {
            "accountId": "anonymous",
            "apiId": LAMBDA_HOST.split(".")[0],
            "domainName": LAMBDA_HOST,
            "http": {
                "method": method,
                "path": "/",
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test-harness/1.0",
            },
            "requestId": "test-local",
            "routeKey": "$default",
            "stage": "$default",
            "time": "01/Jan/2026:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": body_b64,
        "isBase64Encoded": True,
    }


def options_event():
    """Build an OPTIONS preflight event."""
    return make_event({}, method="OPTIONS")


# Legacy credentials for local tests. The real password lives only in the
# Lambda env; test_local.py patches lambda_function.LEGACY_PASSWORD to match.
TEST_LEGACY_PASSWORD = "local-test-password"
CREDS = {"username": "peergynt", "password": TEST_LEGACY_PASSWORD}


def test_event():
    return make_event({**CREDS, "method": "TEST"})


def get_event(bucket="regattaanalysis", filename="www/Insta360/videos.json"):
    return make_event({**CREDS, "method": "GET", "bucket": bucket, "filename": filename})


def put_event(bucket="regattaanalysis", filename="www/Insta360/curated.json", exact=True):
    return make_event({**CREDS, "method": "PUT", "bucket": bucket, "filename": filename, "exact": exact})


def put_timestamped_event(bucket="montlakeupload", filename="test"):
    """PUT without exact flag — filename gets a timestamp suffix."""
    return make_event({**CREDS, "method": "PUT", "bucket": bucket, "filename": filename})


def bad_password_event():
    return make_event({"username": "peergynt", "password": "wrong", "method": "TEST"})


# --- Token-path events (Google Sign-In) -------------------------------------
# The token value is opaque here — tests stub out verify_google_id_token() to
# decide which email it represents.

def token_test_event(token="STUB_TOKEN"):
    return make_event({"id_token": token, "method": "TEST"})


def token_get_event(token="STUB_TOKEN", bucket="regattaanalysis",
                    filename="www/Insta360/videos.json"):
    return make_event({"id_token": token, "method": "GET",
                       "bucket": bucket, "filename": filename})


def token_put_event(token="STUB_TOKEN", bucket="regattaanalysis",
                    filename="www/Insta360/curated.json", exact=True):
    return make_event({"id_token": token, "method": "PUT", "bucket": bucket,
                       "filename": filename, "exact": exact})


def bad_token_event():
    return make_event({"id_token": "INVALID_TOKEN", "method": "TEST"})
