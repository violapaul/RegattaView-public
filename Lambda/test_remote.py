#!/usr/bin/env python3
"""
Test a live Lambda endpoint (production or test alias).

Sends real HTTP requests to the Lambda Function URL and validates responses.
Use --url to target a specific endpoint (defaults to production).

The legacy (iOS uploader) password is never stored in the repo. Export
LEGACY_UPLOADER_PASSWORD to run the legacy-scope tests; they skip otherwise.

Usage:
    python Lambda/test_remote.py                         # hit production
    python Lambda/test_remote.py --url https://xyz...    # hit test alias
    python Lambda/test_remote.py -v                      # verbose
"""

import argparse
import json
import os
import sys
import unittest
import requests

PROD_URL = "https://blsywilg2fba6564nixqxwntyy0iagoj.lambda-url.us-west-2.on.aws/"

LEGACY_PASSWORD = os.environ.get("LEGACY_UPLOADER_PASSWORD", "")
CREDS = {"username": "peergynt", "password": LEGACY_PASSWORD}


def _post(url, body):
    return requests.post(url, data=json.dumps(body), timeout=15)


class TestLambdaRemote(unittest.TestCase):
    """
    Lambda Function URL returns the handler's statusCode as the HTTP status
    and the handler's body string as the HTTP body.
    """
    url = PROD_URL

    # -- Connectivity / auth -------------------------------------------------

    def test_options_preflight(self):
        resp = requests.options(self.url, timeout=10)
        self.assertIn(resp.status_code, [200, 204])

    def test_bad_password_returns_401(self):
        resp = _post(self.url, {"username": "peergynt", "password": "wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_old_hardcoded_password_rejected(self):
        # The pre-2026-10 password shipped in git history and iOS builds.
        resp = _post(self.url, {"username": "peergynt", "password": "montlake",
                                "method": "TEST"})
        self.assertEqual(resp.status_code, 401)

    def test_unauthenticated_put_rejected(self):
        resp = _post(self.url, {"method": "PUT", "bucket": "regattaanalysis",
                                "filename": "auth/allowlist.json", "exact": True})
        self.assertEqual(resp.status_code, 401)


@unittest.skipUnless(LEGACY_PASSWORD, "LEGACY_UPLOADER_PASSWORD not set")
class TestLegacyScopeRemote(unittest.TestCase):
    url = PROD_URL

    def test_method_test(self):
        resp = _post(self.url, {**CREDS, "method": "TEST"})
        self.assertEqual(resp.status_code, 200)

    def test_put_timestamped_montlakeupload(self):
        resp = _post(self.url, {**CREDS, "method": "PUT",
                                "bucket": "montlakeupload",
                                "filename": "test_probe", "exact": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["filename"].startswith("test_probe_"))

    def test_get_site_bucket_denied(self):
        resp = _post(self.url, {**CREDS, "method": "GET",
                                "bucket": "regattaanalysis",
                                "filename": "www/Insta360/videos.json"})
        self.assertEqual(resp.status_code, 403)

    def test_put_site_bucket_denied(self):
        resp = _post(self.url, {**CREDS, "method": "PUT",
                                "bucket": "regattaanalysis",
                                "filename": "www/sailview/probe.js", "exact": True})
        self.assertEqual(resp.status_code, 403)


def main():
    parser = argparse.ArgumentParser(description="Test a live Lambda endpoint")
    parser.add_argument("--url", default=PROD_URL, help="Lambda Function URL to test")
    args, remaining = parser.parse_known_args()

    TestLambdaRemote.url = args.url
    TestLegacyScopeRemote.url = args.url

    sys.argv[1:] = remaining
    unittest.main(module=__name__, exit=True)


if __name__ == "__main__":
    main()
