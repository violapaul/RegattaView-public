#!/usr/bin/env python3
"""
Test the Lambda handler locally — no network calls, no AWS deployment needed.

Invokes lambda_handler() directly with synthetic events and checks responses.
Requires AWS credentials (for S3 presigned URL generation via boto3).

Usage:
    python Lambda/test_local.py              # run all tests
    python Lambda/test_local.py -v           # verbose
    python Lambda/test_local.py -k test_get  # run one test
"""

import sys
import os
import json
import unittest
import datetime as dt
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

import lambda_function as lf
import utils
from events import (
    test_event, get_event, put_event, put_timestamped_event,
    bad_password_event, options_event,
    token_test_event, token_get_event, token_put_event, bad_token_event,
    TEST_LEGACY_PASSWORD,
)


def _future_iso(days=30):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _past_iso(days=1):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")


class TestLambdaLocal(unittest.TestCase):
    """Legacy (iOS uploader) credential: auth works only when the env
    password is configured, and it can only PUT timestamped objects into
    montlakeupload."""

    def setUp(self):
        p = patch.object(lf, "LEGACY_PASSWORD", TEST_LEGACY_PASSWORD)
        p.start()
        self.addCleanup(p.stop)

    def _call(self, event):
        return lf.lambda_handler(event, None)

    def _body(self, res):
        return json.loads(res["body"])

    # -- Auth ----------------------------------------------------------------

    def test_bad_password_returns_401(self):
        res = self._call(bad_password_event())
        self.assertEqual(res["statusCode"], 401)

    def test_options_returns_200(self):
        # Lambda Function URL CORS handles preflight before invocation, so
        # OPTIONS no longer reaches the handler in production. The handler's
        # OPTIONS branch is kept as a safety net and just returns 200; the
        # Access-Control-* headers come from Function URL CORS, not the app.
        res = self._call(options_event())
        self.assertEqual(res["statusCode"], 200)

    # -- TEST method ---------------------------------------------------------

    def test_method_test(self):
        res = self._call(test_event())
        self.assertEqual(res["statusCode"], 200)
        body = self._body(res)
        self.assertIn("test successful", body.get("message", ""))

    def test_legacy_disabled_when_env_password_unset(self):
        with patch.object(lf, "LEGACY_PASSWORD", ""):
            res = self._call(test_event())
        self.assertEqual(res["statusCode"], 401)

    def test_legacy_old_hardcoded_password_rejected(self):
        from events import make_event
        res = self._call(make_event(
            {"username": "peergynt", "password": "montlake", "method": "TEST"}))
        self.assertEqual(res["statusCode"], 401)

    # -- Legacy scope --------------------------------------------------------

    def test_legacy_get_denied(self):
        res = self._call(get_event())
        self.assertEqual(res["statusCode"], 403)

    def test_legacy_put_to_site_bucket_denied(self):
        for key in ("www/Insta360/curated.json", "www/sailview/sailview.js",
                    "auth/allowlist.json"):
            res = self._call(put_event(filename=key, exact=True))
            self.assertEqual(res["statusCode"], 403, key)

    def test_legacy_put_exact_is_ignored(self):
        res = self._call(put_event(bucket="montlakeupload", filename="trk",
                                   exact=True))
        self.assertEqual(res["statusCode"], 200)
        self.assertTrue(self._body(res)["filename"].startswith("trk_"))

    # -- PUT (timestamped) ---------------------------------------------------

    def test_put_timestamped_appends_suffix(self):
        res = self._call(put_timestamped_event(filename="myfile"))
        self.assertEqual(res["statusCode"], 200)
        body = self._body(res)
        self.assertNotEqual(body["filename"], "myfile")
        self.assertTrue(body["filename"].startswith("myfile_"))


class TestTokenAuth(unittest.TestCase):
    """Token (Google Sign-In) auth path. verify_google_id_token() is mocked
    so tests don't need real Google credentials; allowlist is mocked so
    tests don't need an S3 round-trip."""

    GOOD = "good@example.com"
    BAD = "stranger@example.com"
    EXPIRED = "expired@example.com"

    def _call(self, event):
        return lf.lambda_handler(event, None)

    def _body(self, res):
        return json.loads(res["body"])

    def _allowlist(self):
        return [
            {"email": self.GOOD, "expires_at": _future_iso(30)},
            {"email": self.EXPIRED, "expires_at": _past_iso(1)},
        ]

    def _patches(self, token_email):
        """Return a context that mocks the token verifier and the allowlist
        loader so the lambda handler runs purely in-memory."""
        return [
            patch.object(utils, "verify_google_id_token",
                         return_value=token_email),
            patch.object(utils, "get_allowlist",
                         return_value=self._allowlist()),
        ]

    def _with(self, token_email, fn):
        ps = self._patches(token_email)
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in ps:
                p.stop()

    def test_allowlisted_user_passes_test_method(self):
        res = self._with(self.GOOD, lambda: self._call(token_test_event()))
        self.assertEqual(res["statusCode"], 200)
        body = self._body(res)
        self.assertEqual(body.get("identity"), self.GOOD)

    def test_allowlisted_user_gets_signed_get(self):
        res = self._with(self.GOOD, lambda: self._call(token_get_event()))
        self.assertEqual(res["statusCode"], 200)
        body = self._body(res)
        self.assertIn("url", body)
        self.assertTrue(body["url"].startswith("https://"))

    def test_allowlisted_user_get_preserves_filename(self):
        fname = "www/Insta360/videos.json"
        res = self._with(self.GOOD, lambda: self._call(token_get_event(filename=fname)))
        self.assertEqual(self._body(res)["filename"], fname)

    def test_allowlisted_user_raw_put_curated_denied(self):
        res = self._with(self.GOOD, lambda: self._call(token_put_event()))
        self.assertEqual(res['statusCode'], 403)

    def test_allowlisted_user_cannot_put_site_or_auth(self):
        # Regression: with enforcement off these minted valid PUT URLs for
        # the site bundle (stored XSS) and the allowlist / JWKS (takeover).
        for key in ("www/sailview/sailview.js", "auth/allowlist.json",
                    "auth/google-jwks.json", "www/Insta360/VID_x.mp4"):
            res = self._with(self.GOOD, lambda: self._call(
                token_put_event(filename=key)))
            self.assertEqual(res["statusCode"], 403, key)

    def test_allowlisted_user_cannot_get_auth(self):
        res = self._with(self.GOOD, lambda: self._call(
            token_get_event(filename="auth/allowlist.json")))
        self.assertEqual(res["statusCode"], 403)

    def test_unknown_user_returns_403(self):
        res = self._with(self.BAD, lambda: self._call(token_test_event()))
        self.assertEqual(res["statusCode"], 403)
        self.assertEqual(self._body(res).get("reason"), "not on allowlist")

    def test_expired_user_returns_403(self):
        res = self._with(self.EXPIRED, lambda: self._call(token_test_event()))
        self.assertEqual(res["statusCode"], 403)
        self.assertEqual(self._body(res).get("reason"), "expired")

    def test_invalid_token_returns_401(self):
        # verify_google_id_token returns None for an invalid token.
        res = self._with(None, lambda: self._call(bad_token_event()))
        self.assertEqual(res["statusCode"], 401)
        self.assertEqual(self._body(res).get("reason"), "invalid token")


class TestAllowlistHelpers(unittest.TestCase):
    """Pure-function tests for is_email_authorized."""

    def test_match_with_future_expiry(self):
        ok, reason = utils.is_email_authorized(
            "a@b.com", [{"email": "a@b.com", "expires_at": _future_iso(1)}])
        self.assertTrue(ok)

    def test_match_no_expiry(self):
        ok, reason = utils.is_email_authorized(
            "a@b.com", [{"email": "a@b.com"}])
        self.assertTrue(ok)

    def test_case_insensitive(self):
        ok, _ = utils.is_email_authorized(
            "A@B.com", [{"email": "a@b.com", "expires_at": _future_iso(1)}])
        self.assertTrue(ok)

    def test_expired(self):
        ok, reason = utils.is_email_authorized(
            "a@b.com", [{"email": "a@b.com", "expires_at": _past_iso(1)}])
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_not_present(self):
        ok, reason = utils.is_email_authorized("x@y.com", [])
        self.assertFalse(ok)
        self.assertEqual(reason, "not on allowlist")

    def test_bad_expires_at(self):
        ok, reason = utils.is_email_authorized(
            "a@b.com", [{"email": "a@b.com", "expires_at": "not-a-date"}])
        self.assertFalse(ok)
        self.assertEqual(reason, "bad expires_at")


class TestSigningPolicy(unittest.TestCase):
    """Presign policy checks (enforced by default)."""

    def test_enforced_by_default(self):
        self.assertTrue(lf.SIGNING_POLICY_ENFORCE)

    def test_traversal_and_malformed_keys_denied(self):
        for key in ("www/Analyze/../../auth/allowlist.json",
                    "www/Comments/../sailview/comments.json",
                    "/www/Analyze/x.json", "www//Analyze/x.json", None, ""):
            self.assertEqual(
                lf._signing_policy_reason("GET", "regattaanalysis", key),
                "malformed key", key)

    def test_raw_put_insta360_denied(self):
        self.assertIsNotNone(lf._signing_policy_reason(
            "PUT", "regattaanalysis", "www/Insta360/curated.json"))
        self.assertIsNotNone(lf._signing_policy_reason(
            "PUT", "regattaanalysis", "www/Insta360/videos.json"))

    def test_get_known_prefix_allowed(self):
        reason = lf._signing_policy_reason(
            "GET", "regattaanalysis", "www/Insta360/videos.json")
        self.assertIsNone(reason)

    def test_get_unknown_bucket_denied(self):
        reason = lf._signing_policy_reason(
            "GET", "other-bucket", "www/Insta360/videos.json")
        self.assertEqual(reason, "GET bucket/key not allowlisted")

    def test_raw_put_comment_file_denied(self):
        reason = lf._signing_policy_reason(
            "PUT", "regattaanalysis", "www/Comments/paul/comments.json")
        self.assertIsNotNone(reason)

    def test_put_unknown_regattaanalysis_key_denied(self):
        reason = lf._signing_policy_reason(
            "PUT", "regattaanalysis", "www/Unexpected/file.json")
        self.assertIsNotNone(reason)


class TestSessionSecretLoading(unittest.TestCase):
    """SESSION_SECRET comes from Secrets Manager when SESSION_SECRET_ARN is set."""

    def test_loads_from_secrets_manager_once(self):
        class FakeSM:
            calls = 0

            def get_secret_value(self, SecretId):
                FakeSM.calls += 1
                assert SecretId == "arn:test"
                return {"SecretString": "s3cr3t\n"}

        import boto3
        with patch.object(utils, "SESSION_SECRET", b""), \
             patch.object(utils, "_SESSION_SECRET_ARN", "arn:test"), \
             patch.object(boto3, "client", return_value=FakeSM()):
            tok = utils.mint_session_token("A@B.com")
            self.assertEqual(utils.verify_session_token(tok), "a@b.com")
            self.assertEqual(utils.SESSION_SECRET, b"s3cr3t")
        self.assertEqual(FakeSM.calls, 1)


class TestCloudFrontSigning(unittest.TestCase):
    """CF signed-URL emission. Stubs _rsa_sha1_sign so the tests don't need
    a real key; the math is exercised separately in test_rsa_sha1_round_trip
    when the optional `rsa` library is importable."""

    def test_generate_cf_signed_url_structure(self):
        fake_sig = b"\x42" * 128  # RSA-1024 signature size
        with patch.object(utils, "_CF_KEY_PAIR_ID", "TESTKEYID"), \
             patch.object(utils, "_CF_DOMAIN", "example.cloudfront.net"), \
             patch.object(utils, "_rsa_sha1_sign", return_value=fake_sig):
            url = utils.generate_cf_signed_url(
                "www/Insta360/foo.mp4", 3600)
        # CF canned-policy URL:
        #   https://<domain>/<key>?Expires=<unix>&Signature=<b64>&Key-Pair-Id=<id>
        self.assertTrue(url.startswith(
            "https://example.cloudfront.net/www/Insta360/foo.mp4?"))
        self.assertIn("Expires=", url)
        self.assertIn("Signature=", url)
        self.assertIn("Key-Pair-Id=TESTKEYID", url)

    def test_generate_cf_signed_url_strips_leading_slash(self):
        fake_sig = b"\x42" * 128
        with patch.object(utils, "_CF_KEY_PAIR_ID", "ID"), \
             patch.object(utils, "_CF_DOMAIN", "d.example"), \
             patch.object(utils, "_rsa_sha1_sign", return_value=fake_sig):
            url = utils.generate_cf_signed_url("/www/foo", 60)
        # Single slash between domain and key — no `https://d.example//www/foo`.
        self.assertTrue(url.startswith("https://d.example/www/foo?"))

    def test_generate_cf_signed_url_requires_key_pair_id(self):
        with patch.object(utils, "_CF_KEY_PAIR_ID", ""):
            with self.assertRaises(RuntimeError):
                utils.generate_cf_signed_url("www/foo", 60)

    def test_load_cf_private_key_requires_secret_arn(self):
        # Reset cache so the env-var check actually runs (prior tests in the
        # process may have populated it).
        with patch.object(utils, "_cf_private_key_cache",
                          {"n": None, "d": None}), \
             patch.object(utils, "_CF_PRIVATE_KEY_SECRET_ARN", ""):
            with self.assertRaises(RuntimeError):
                utils._load_cf_private_key()

    def test_rsa_sha1_round_trip(self):
        """End-to-end math check: _rsa_sha1_sign produces a signature that
        verifies under PKCS#1 v1.5 + SHA-1. Skipped if `rsa` isn't available
        in the test env (it's only required for the deployed Lambda)."""
        try:
            import rsa
        except ImportError:
            self.skipTest("rsa library not installed in test env")
        # Small key so generation is fast; the algorithm is identical to
        # RSA-2048 used in prod.
        pub, priv = rsa.newkeys(1024)
        with patch.object(utils, "_cf_private_key_cache",
                          {"n": priv.n, "d": priv.d}):
            message = b"Hello from RegattaView CF signing test."
            sig = utils._rsa_sha1_sign(message)
        # Verify with the public key (n, e). _verify_rs256 won't help here
        # because that's hard-coded to SHA-256; do the SHA-1 verify inline.
        em_len = (priv.n.bit_length() + 7) // 8
        em_int = pow(int.from_bytes(sig, "big"), pub.e, priv.n)
        em = em_int.to_bytes(em_len, "big")
        import hashlib as _h
        digest = _h.sha1(message).digest()
        pad_len = em_len - len(utils._SHA1_DIGEST_INFO) - len(digest) - 3
        expected = (b"\x00\x01" + b"\xff" * pad_len + b"\x00"
                    + utils._SHA1_DIGEST_INFO + digest)
        self.assertEqual(em, expected)


class TestGetRouting(unittest.TestCase):
    """Verify the GET handler routes to the right signer based on
    CF_SIGNING_ENABLED. Reuses TestTokenAuth's allowlist / token mocks."""

    GOOD = "good@example.com"

    def _allowlist(self):
        return [{"email": self.GOOD, "expires_at": _future_iso(30)}]

    def _patches(self):
        return [
            patch.object(utils, "verify_google_id_token",
                         return_value=self.GOOD),
            patch.object(utils, "get_allowlist",
                         return_value=self._allowlist()),
        ]

    def _with(self, fn):
        ps = self._patches()
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in ps:
                p.stop()

    def test_get_uses_s3_when_cf_disabled(self):
        with patch.object(lf, "CF_SIGNING_ENABLED", False):
            res = self._with(lambda: lf.lambda_handler(
                token_get_event(), None))
        self.assertEqual(res["statusCode"], 200)
        body = json.loads(res["body"])
        # S3 SigV4 URL — host is the bucket vhost.
        self.assertIn("amazonaws.com", body["url"])

    def test_get_uses_cf_when_enabled(self):
        with patch.object(lf, "CF_SIGNING_ENABLED", True), \
             patch.object(utils, "generate_cf_signed_url",
                          return_value="https://regattaview.net/www/x?Signature=...&Key-Pair-Id=K") as mock_cf:
            res = self._with(lambda: lf.lambda_handler(
                token_get_event(), None))
        self.assertEqual(res["statusCode"], 200)
        body = json.loads(res["body"])
        self.assertEqual(body["url"],
                         "https://regattaview.net/www/x?Signature=...&Key-Pair-Id=K")
        mock_cf.assert_called_once()


class TestResolveCfHost(unittest.TestCase):
    """Origin-aware CF hostname resolution. Same-origin signing avoids the
    cross-origin OPTIONS preflight that 403s under TrustedKeyGroups."""

    def _ev(self, origin):
        if origin is None:
            return {"headers": {}}
        return {"headers": {"origin": origin}}

    def test_origin_www_returns_www(self):
        self.assertEqual(
            lf._resolve_cf_host(self._ev("https://www.regattaview.net")),
            "www.regattaview.net")

    def test_origin_apex_returns_apex(self):
        self.assertEqual(
            lf._resolve_cf_host(self._ev("https://regattaview.net")),
            "regattaview.net")

    def test_origin_with_port_strips_port(self):
        self.assertEqual(
            lf._resolve_cf_host(self._ev("https://www.regattaview.net:443")),
            "www.regattaview.net")

    def test_unknown_origin_falls_back_to_env(self):
        with patch.dict(os.environ, {"CF_DOMAIN": "fallback.example"}):
            self.assertEqual(
                lf._resolve_cf_host(self._ev("https://attacker.example")),
                "fallback.example")

    def test_no_origin_falls_back_to_env(self):
        with patch.dict(os.environ, {"CF_DOMAIN": "fallback.example"}):
            self.assertEqual(lf._resolve_cf_host(self._ev(None)),
                             "fallback.example")

    def test_origin_capital_header_name(self):
        # Function URL lowercases header keys, but the parser is defensive.
        self.assertEqual(
            lf._resolve_cf_host(
                {"headers": {"Origin": "https://regattaview.net"}}),
            "regattaview.net")


class TestJwksForceRefresh(unittest.TestCase):
    """Regression for the 2026-05-11 outage: when Google rotated its signing
    keys, the Lambda's S3-cached JWKS still held the old kids. The "force
    refresh" path that `verify_google_id_token` calls on a kid miss was
    supposed to bypass the cache and refetch from Google, but it only
    skipped the in-memory cache — it still read S3 first, found a
    well-formed (just stale) JWKS, and never reached Google. Every sign-in
    failed until the S3 object was manually overwritten."""

    def setUp(self):
        # Reset in-memory cache between tests so we don't leak state.
        utils._jwks_cache["keys"] = None
        utils._jwks_cache["fetched_at"] = 0.0
        utils._jwks_last_attempt = 0.0

    def _stale_s3_jwks(self):
        return {"keys": [{"kid": "OLD_KID", "kty": "RSA",
                          "alg": "RS256", "use": "sig",
                          "n": "x", "e": "AQAB"}]}

    def _fresh_google_jwks(self):
        return {"keys": [{"kid": "NEW_KID", "kty": "RSA",
                          "alg": "RS256", "use": "sig",
                          "n": "y", "e": "AQAB"}]}

    def test_force_refresh_skips_stale_s3(self):
        """force_refresh=True must skip the S3 read so a stale-but-valid
        cache can't shadow Google's current keys."""

        # If the bug returns, _load_jwks will return {OLD_KID: ...} and the
        # assertion below will fail.
        class FakeS3:
            def get_object(self, **kw):
                # If this is reached under force_refresh=True the bug is back.
                raise AssertionError(
                    "S3 must not be consulted under force_refresh=True")

        with patch.object(utils, "_fetch_google_jwks_direct",
                          return_value=self._fresh_google_jwks()):
            keys = utils._load_jwks(FakeS3(), force_refresh=True)

        self.assertIn("NEW_KID", keys)
        self.assertNotIn("OLD_KID", keys)

    def test_force_refresh_never_writes_s3(self):
        """The Lambda must not write the S3 JWKS: its role has no PutObject
        on auth/, so a writable JWKS can't be planted via a minted URL."""

        class FakeS3:
            def get_object(self, **kw):
                raise AssertionError("not consulted under force_refresh")

            def put_object(self, **kw):
                raise AssertionError("Lambda must not write the JWKS")

        with patch.object(utils, "_fetch_google_jwks_direct",
                          return_value=self._fresh_google_jwks()):
            keys = utils._load_jwks(FakeS3(), force_refresh=True)
        self.assertEqual(list(keys), ["NEW_KID"])

    def test_normal_path_still_uses_s3_cache(self):
        """Without force_refresh the S3 cache is consulted first (cheap)
        and Google is only hit on cache miss."""

        s3_jwks = self._stale_s3_jwks()  # well-formed; Google must NOT be hit

        class FakeS3:
            def __init__(self):
                self.calls = 0

            def get_object(self, **kw):
                self.calls += 1
                body = json.dumps(s3_jwks).encode()
                # Match boto3's StreamingBody.read() interface.

                class _Body:
                    def __init__(self, b): self._b = b
                    def read(self): return self._b
                return {"Body": _Body(body)}

        fake = FakeS3()
        with patch.object(utils, "_fetch_google_jwks_direct",
                          side_effect=AssertionError(
                              "Google must not be hit on the normal path")):
            keys = utils._load_jwks(fake, force_refresh=False)

        self.assertEqual(fake.calls, 1)
        self.assertIn("OLD_KID", keys)


class TestSigninLog(unittest.TestCase):
    """The handler must emit a single `SIGNIN` log line the moment a Google
    ID token first verifies and a session token is minted. Successful
    sign-ins were silent before, making post-incident triage rely on
    indirect signals like PRESIGN-after-quiet."""

    def setUp(self):
        utils._jwks_cache["keys"] = None
        utils._jwks_cache["fetched_at"] = 0.0
        utils._jwks_last_attempt = 0.0

    def _allowlist(self):
        return [{"email": "good@example.com", "expires_at": _future_iso(30)}]

    def test_signin_log_emitted_on_fresh_google_signin(self):
        with patch.object(utils, "verify_google_id_token",
                          return_value="good@example.com"), \
             patch.object(utils, "get_allowlist",
                          return_value=self._allowlist()), \
             patch.object(utils, "mint_session_token",
                          return_value="ses.XYZ"), \
             patch.object(lf.logger, "info") as mock_info:
            res = lf.lambda_handler(token_test_event(), None)

        self.assertEqual(res["statusCode"], 200)
        signin = [c for c in mock_info.call_args_list
                  if c.args and isinstance(c.args[0], str)
                  and c.args[0].startswith("SIGNIN ")]
        self.assertEqual(len(signin), 1, "expected exactly one SIGNIN log")
        payload = json.loads(signin[0].args[0][len("SIGNIN "):])
        self.assertEqual(payload["identity"], "good@example.com")

    def test_no_signin_log_when_session_token_reused(self):
        """A request authenticated by an existing session token (no new
        token minted) must not emit SIGNIN — otherwise every PRESIGN call
        in a long session would double-log."""
        # Simulate the session_token-only path: _authenticate returns the
        # identity but new_session_token is None.
        with patch.object(lf, "_authenticate",
                          return_value=("good@example.com",
                                        lf.PRESIGN_TTL_TOKEN, None, None)), \
             patch.object(lf.logger, "info") as mock_info:
            res = lf.lambda_handler(token_test_event(), None)

        self.assertEqual(res["statusCode"], 200)
        signin = [c for c in mock_info.call_args_list
                  if c.args and isinstance(c.args[0], str)
                  and c.args[0].startswith("SIGNIN ")]
        self.assertEqual(len(signin), 0, "SIGNIN must not fire without a new token")


class TestAuthFailLog(unittest.TestCase):
    """The handler must emit one structured AUTH_FAIL warning whenever
    _authenticate returns no identity. Failed sign-ins were previously
    silent in CloudWatch for every path except the Allowlist denied
    branch, so the security report could not count them."""

    def _parse_auth_fail(self, mock_warning):
        lines = [c for c in mock_warning.call_args_list
                 if c.args and isinstance(c.args[0], str)
                 and c.args[0].startswith("AUTH_FAIL ")]
        self.assertEqual(len(lines), 1, "expected exactly one AUTH_FAIL log")
        return json.loads(lines[0].args[0][len("AUTH_FAIL "):])

    def test_bad_legacy_password(self):
        with patch.object(lf.logger, "warning") as mock_warning:
            res = lf.lambda_handler(bad_password_event(), None)
        self.assertEqual(res["statusCode"], 401)
        payload = self._parse_auth_fail(mock_warning)
        self.assertEqual(payload["kind"], "legacy")
        self.assertEqual(payload["attempted"], "peergynt")
        self.assertEqual(payload["reason"], "auth failed")

    def test_invalid_google_token(self):
        with patch.object(utils, "verify_google_id_token", return_value=None), \
             patch.object(lf.logger, "warning") as mock_warning:
            res = lf.lambda_handler(bad_token_event(), None)
        self.assertEqual(res["statusCode"], 401)
        payload = self._parse_auth_fail(mock_warning)
        self.assertEqual(payload["kind"], "google")
        self.assertEqual(payload["reason"], "invalid token")
        self.assertEqual(payload["attempted"], "")

    def test_off_allowlist_google_signin(self):
        """Verified Google email that isn't on the allowlist: the existing
        `Allowlist denied <email>` warning still fires inside _authenticate,
        and the new AUTH_FAIL line fires from the handler. The report tool
        correlates them by (ip, ts)."""
        with patch.object(utils, "verify_google_id_token",
                          return_value="stranger@example.com"), \
             patch.object(utils, "get_allowlist", return_value=[]), \
             patch.object(lf.logger, "warning") as mock_warning:
            res = lf.lambda_handler(token_test_event(), None)
        self.assertEqual(res["statusCode"], 403)
        payload = self._parse_auth_fail(mock_warning)
        self.assertEqual(payload["kind"], "google")
        self.assertEqual(payload["reason"], "not on allowlist")


class TestDevOriginFallback(unittest.TestCase):
    """Dev pages (webpack-dev-server on localhost) get the S3 SigV4 path
    even when CF_SIGNING_ENABLED is true, because the prod CF
    distribution can't be made same-origin with localhost and its CORS
    setup doesn't reliably cover the dev case."""

    GOOD = "good@example.com"

    def _patches(self):
        return [
            patch.object(utils, "verify_google_id_token",
                         return_value=self.GOOD),
            patch.object(utils, "get_allowlist",
                         return_value=[{"email": self.GOOD,
                                        "expires_at": _future_iso(30)}]),
        ]

    def _with(self, fn):
        ps = self._patches()
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in ps:
                p.stop()

    def test_localhost_origin_detected(self):
        self.assertTrue(lf._is_dev_origin(
            {"headers": {"origin": "http://localhost:9000"}}))
        self.assertTrue(lf._is_dev_origin(
            {"headers": {"origin": "http://localhost:8080"}}))
        self.assertTrue(lf._is_dev_origin(
            {"headers": {"origin": "http://127.0.0.1:9000"}}))

    def test_prod_origin_not_dev(self):
        self.assertFalse(lf._is_dev_origin(
            {"headers": {"origin": "https://www.regattaview.net"}}))
        self.assertFalse(lf._is_dev_origin(
            {"headers": {"origin": "https://regattaview.net"}}))
        self.assertFalse(lf._is_dev_origin({"headers": {}}))

    def test_dev_origin_uses_s3_sigv4_even_with_cf_enabled(self):
        ev = token_get_event()
        ev["headers"] = {"origin": "http://localhost:9000"}
        with patch.object(lf, "CF_SIGNING_ENABLED", True), \
             patch.object(utils, "generate_cf_signed_url") as mock_cf:
            res = self._with(lambda: lf.lambda_handler(ev, None))
        self.assertEqual(res["statusCode"], 200)
        body = json.loads(res["body"])
        # S3 vhost — not regattaview.net.
        self.assertIn("amazonaws.com", body["url"])
        mock_cf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
