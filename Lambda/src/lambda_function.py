import boto3
from botocore.exceptions import ClientError
import logging
import json
import datetime as dt
import hmac
import os

import utils
import documents

logger = logging.getLogger(__name__)
# Lambda's default root level is WARNING; bump so presign events flow to CloudWatch.
logger.setLevel(logging.INFO)

# Service credential for the legacy iOS Uploader (`iOS/Shared/Uploader.swift`).
# The password lives only in the Lambda env (LEGACY_UPLOADER_PASSWORD); when
# unset, legacy auth is disabled. The legacy identity may only PUT
# timestamped objects into `montlakeupload/` (enforced regardless of
# SIGNING_POLICY_ENFORCE) — the old hardcoded password shipped in git history
# and iOS binaries, so it must never again reach the site bucket.
LEGACY_USERNAME = "peergynt"
LEGACY_PASSWORD = os.environ.get("LEGACY_UPLOADER_PASSWORD", "")

# Presign TTLs. Token-authed sessions get a tighter window so a leaked signed
# URL ages out quickly; legacy sessions keep the old generous window until
# retired.
PRESIGN_TTL_TOKEN = 60 * 60          # 1h
PRESIGN_TTL_LEGACY = 24 * 60 * 60    # 24h

# The authorization boundary cannot be disabled by environment configuration.
SIGNING_POLICY_ENFORCE = True  # Never disable the authorization boundary.

# When enabled, GET signing returns a CloudFront signed URL (host
# `regattaview.net`) instead of an S3 SigV4 URL (host
# `regattaanalysis.s3.us-west-2.amazonaws.com`). PUTs always stay on S3
# SigV4 — CF doesn't sign PUTs in this setup. See `Lambda/README.md` for
# the env-var contract and `Scripts/cf_signing_setup.md` for the one-time
# AWS configuration.
CF_SIGNING_ENABLED = (
    os.environ.get("CF_SIGNING_ENABLED", "").lower()
    in ("1", "true", "yes")
)

# Both the apex and the www subdomain are aliases on the same CF
# distribution. Mint the signed URL at whichever host the calling page
# is on — same-origin fetches skip the CORS preflight that the signed
# /www/* behavior 403s on (TrustedKeyGroups requires a valid signature
# even on OPTIONS, but browsers don't include the signature in
# preflights). Anything not in the allowlist falls back to CF_DOMAIN.
_ALLOWED_CF_HOSTS = ("regattaview.net", "www.regattaview.net")


def _resolve_cf_host(event):
    """Pick the CF hostname for the signed URL, matching the request's
    browser Origin so the resulting fetch is same-origin and avoids the
    cross-origin OPTIONS preflight. Falls back to env CF_DOMAIN for
    non-browser callers (e.g. test_remote.py, the iOS uploader)."""
    headers = event.get("headers") or {}
    origin = headers.get("origin") or headers.get("Origin") or ""
    if origin:
        host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if host in _ALLOWED_CF_HOSTS:
            return host
    return os.environ.get("CF_DOMAIN", "regattaview.net")


def _is_dev_origin(event):
    """True when the calling page is the local webpack-dev-server
    (http://localhost:9000 by convention; any localhost port is accepted).

    Dev pages can't be served same-origin with the prod CF distribution,
    so signed CF URLs trigger cross-origin XHRs. The Managed-SimpleCORS
    response policy on /www/* is brittle (echoes ACAO only when an Origin
    header is present + the cache happens to have it), and the
    TrustedKeyGroups requirement breaks any OPTIONS preflight that does
    fire. The S3 SigV4 vhost path has solid CORS configured at the
    bucket level, so dev pages keep using it. Only affects code paths
    where CF_SIGNING_ENABLED would otherwise route to CloudFront.
    """
    headers = event.get("headers") or {}
    origin = headers.get("origin") or headers.get("Origin") or ""
    return origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:")


def _matches_prefix(key, prefixes):
    return any((key or "").startswith(prefix) for prefix in prefixes)


def _signing_policy_reason(method, bucket, key, identity=None):
    """Return None when this presign request is in the intended surface area.

    The Lambda role can read/write far more than clients need (e.g. the
    `auth/` allowlist and JWKS, the `www/sailview/` site bundle), so this
    allowlist is what keeps a signed-in user from minting URLs for those.
    """
    if not isinstance(key, str) or not key or key.startswith("/") \
            or "//" in key or ".." in key or "\\" in key or any(ord(c) < 32 for c in key):
        return "malformed key"

    if identity == LEGACY_USERNAME:
        if method == "PUT" and bucket == "montlakeupload":
            return None
        return "legacy credential is PUT-only to montlakeupload"

    if method == "GET":
        if bucket == "regattaanalysis" and _matches_prefix(key, (
            "www/Insta360/",
            "www/Logs/",
            "www/FullLogs/",
            "www/Analyze/",
            "www/Comments/",
            "www/SeattleTiles/",
        )):
            return None
        return "GET bucket/key not allowlisted"

    if method == "PUT":
        # montlakeupload is legacy-uploader-only (above); everything else
        # goes through validated document operations.
        return "Use validated document operations; direct PUT signing is disabled"

    return None


def _audit_signing_policy(identity, method, bucket, key, event):
    reason = _signing_policy_reason(method, bucket, key, identity)
    if not reason:
        return None
    http_ctx = event.get("requestContext", {}).get("http", {})
    logger.warning("WOULD_DENY " + json.dumps({
        "identity": identity,
        "method": method,
        "bucket": bucket,
        "key": key,
        "reason": reason,
        "enforced": SIGNING_POLICY_ENFORCE or identity == LEGACY_USERNAME,
        "ip": http_ctx.get("sourceIp"),
        "user_agent": http_ctx.get("userAgent"),
    }))
    return reason


def _authenticate(body, s3_client):
    """Return (identity, ttl, new_session_token, error).

    `new_session_token` is non-None only when the client just authenticated
    with a Google ID token and we minted a long-lived app session token for
    them; the client should store it and use it on every subsequent call
    instead of re-sending the Google ID token.
    """
    # 1. Preferred: app session token. HMAC verify is microseconds and
    #    doesn't touch S3, JWKS, or RSA. After first sign-in this is what
    #    every call hits.
    session = body.get("session_token")
    if session:
        email = utils.verify_session_token(session)
        if email:
            allowlist = utils.get_allowlist(s3_client)
            ok, reason = utils.is_email_authorized(email, allowlist)
            if not ok:
                logger.warning("Allowlist denied %s: %s", email, reason)
                return None, None, None, reason
            return email, PRESIGN_TTL_TOKEN, None, None
        # Invalid / expired session — fall through; the client may still
        # be presenting a Google ID token alongside (initial sign-in).

    # 2. Google ID token (initial sign-in or session expired). We verify
    #    once, then mint our own session token to return to the client.
    token = body.get("id_token")
    if token:
        email = utils.verify_google_id_token(token, s3_client)
        if not email:
            return None, None, None, "invalid token"
        allowlist = utils.get_allowlist(s3_client)
        ok, reason = utils.is_email_authorized(email, allowlist)
        if not ok:
            logger.warning("Allowlist denied %s: %s", email, reason)
            return None, None, None, reason
        new_session = utils.mint_session_token(email)
        return email, PRESIGN_TTL_TOKEN, new_session, None

    # 3. Legacy username/password (iOS Uploader only).
    username = body.get("username")
    password = body.get("password")
    if (LEGACY_PASSWORD and username == LEGACY_USERNAME
            and isinstance(password, str)
            and hmac.compare_digest(password.encode(), LEGACY_PASSWORD.encode())):
        return username, PRESIGN_TTL_LEGACY, None, None

    return None, None, None, "auth failed"


def lambda_handler(event, context):

    # CORS headers (Allow-Origin/Headers/Methods) are emitted by the
    # Function URL CORS config, not by the app. Returning them here too
    # made the browser see duplicate Access-Control-Allow-Origin and
    # block the response. Lambda also handles OPTIONS preflight when
    # Function URL CORS is configured, so the OPTIONS branch below is
    # vestigial but harmless.
    res = {}

    if utils.get_method(event) == 'OPTIONS':
        return {**res, 'statusCode': 200,
                'body': json.dumps({'message': 'Montlake options successful'})}

    if utils.get_method(event) != 'POST':
        return {**res, 'statusCode': 405,
                'body': json.dumps({'message': 'Method not allowed'})}

    try:
        body = utils.get_body(event)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {**res, 'statusCode': 400, 'body': json.dumps({'message': 'Invalid JSON body'})}
    if not isinstance(body, dict) or not body:
        return {**res, 'statusCode': 400,
                'body': json.dumps({'message': 'Missing body'})}

    for field in ('id_token', 'session_token', 'username', 'password', 'method', 'bucket', 'filename'):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > 16384):
            return {**res, 'statusCode': 400, 'body': json.dumps({'message': 'Invalid request field'})}

    s3_client = boto3.client('s3')
    identity, ttl, new_session_token, err = _authenticate(body, s3_client)
    if not identity:
        # Mirror of the SIGNIN line below: AUTH_FAIL gives the security
        # report tool a single uniform pattern to count failed sign-ins
        # (invalid Google token, expired/invalid session, wrong legacy
        # password, off-allowlist email). `kind` tells the reader which
        # credential the client presented; `attempted` is the only
        # identifier we can extract without trusting unverified token
        # claims (i.e. legacy `username`). For the Google/session paths
        # the verified email is known inside _authenticate and already
        # surfaces via the existing `Allowlist denied <email>` warning,
        # which the reader correlates by (ip, ts).
        http_ctx = event.get("requestContext", {}).get("http", {})
        if body.get("id_token"):
            kind = "google"
        elif body.get("session_token"):
            kind = "session"
        elif body.get("username"):
            kind = "legacy"
        else:
            kind = "none"
        logger.warning("AUTH_FAIL " + json.dumps({
            "reason": err,
            "kind": kind,
            "attempted": body.get("username") or "",
            "ip": http_ctx.get("sourceIp"),
            "user_agent": http_ctx.get("userAgent"),
        }))
        # Distinguish "expired" / "not on allowlist" so the frontend can show a
        # tailored screen instead of a generic re-login prompt.
        status = (403 if err in ("expired", "not on allowlist")
                  else 503 if err == "allowlist unavailable" else 401)
        return {**res, 'statusCode': status,
                'body': json.dumps({'message': f'Auth failed: {err}',
                                    'reason': err})}

    if new_session_token:
        # A new session token is minted only when a Google ID token verified
        # cleanly — i.e. this is the moment of sign-in. Successful sign-ins
        # were previously silent in CloudWatch (only failures logged), so
        # post-incident triage couldn't tell whether a given user got back in.
        http_ctx = event.get("requestContext", {}).get("http", {})
        logger.info("SIGNIN " + json.dumps({
            "identity": identity,
            "ip": http_ctx.get("sourceIp"),
            "user_agent": http_ctx.get("userAgent"),
        }))

    method = body.get('method', 'PUT')
    bucket = body.get('bucket', 'montlakeupload')

    if method in ('READ_JSON', 'SAVE_JSON', 'REGISTER_ALIAS', 'APPEND_COMMENT'):
        if identity == LEGACY_USERNAME:
            return {**res, 'statusCode': 403, 'body': json.dumps({'message': 'Not allowed'})}
        try:
            result = documents.handle(s3_client, body, identity)
            if new_session_token: result['session_token'] = new_session_token
            if method != 'READ_JSON':
                logger.info('DOCUMENT_WRITE ' + json.dumps({'identity': identity, 'method': method,
                    'key': body.get('filename'), 'alias': body.get('alias')}))
            return {**res, 'statusCode': 200, 'body': json.dumps(result)}
        except documents.DocumentError as exc:
            return {**res, 'statusCode': exc.status, 'body': json.dumps({'message': str(exc)})}

    url = None
    filename = body.get('filename')
    # Legacy callers always get timestamped names so they can't overwrite.
    enforce = True
    if method == 'PUT':
        if not body.get('exact') or identity == LEGACY_USERNAME:
            filename = utils.upload_name(filename)
        policy_reason = _audit_signing_policy(
            identity, method, bucket, filename, event)
        if policy_reason and enforce:
            return {**res, 'statusCode': 403,
                    'body': json.dumps({'message': 'Signing denied',
                                        'reason': policy_reason})}
        url = utils.generate_presigned_url(
            s3_client, 'put_object',
            {'Bucket': bucket, 'Key': filename}, ttl)
        signer = 's3'
    elif method == 'GET':
        policy_reason = _audit_signing_policy(
            identity, method, bucket, filename, event)
        if policy_reason and enforce:
            return {**res, 'statusCode': 403,
                    'body': json.dumps({'message': 'Signing denied',
                                        'reason': policy_reason})}
        if (CF_SIGNING_ENABLED
                and bucket == 'regattaanalysis'
                and not _is_dev_origin(event)):
            # Route data fetches through CloudFront (free 1 TB/mo egress
            # tier; edge cache absorbs re-watches). Only `regattaanalysis`
            # is fronted by the distribution; the other buckets keep S3
            # SigV4 so the path stays correct if/when they get URLs minted.
            # Dev pages (localhost) use the S3 SigV4 fallback below — the
            # bucket has CORS for localhost; CF doesn't, and it's not
            # worth bending the prod distribution to fix dev.
            cf_host = _resolve_cf_host(event)
            url = utils.generate_cf_signed_url(filename, ttl, cf_domain=cf_host)
            signer = 'cf'
        else:
            url = utils.generate_presigned_url(
                s3_client, 'get_object',
                {'Bucket': bucket, 'Key': filename}, ttl)
            signer = 's3'
    elif method == 'TEST':
        body_out = {'message': 'Montlake test successful',
                    'identity': identity}
        if new_session_token:
            body_out['session_token'] = new_session_token
        return {**res, 'statusCode': 200, 'body': json.dumps(body_out)}

    if url:
        # Per-user attribution for S3 server access logs: the access log only
        # records the Lambda's IAM role as Requester. Joining {key, ip,
        # ~minute} between this CloudWatch line and the S3 log identifies who
        # signed each fetch. A Lambda-IP / S3-IP mismatch on the same key is
        # diagnostic of a leaked URL.
        http_ctx = event.get("requestContext", {}).get("http", {})
        logger.info("PRESIGN " + json.dumps({
            "identity": identity,
            "method": method,
            "bucket": bucket,
            "key": filename,
            "ip": http_ctx.get("sourceIp"),
            "user_agent": http_ctx.get("userAgent"),
            "ttl_s": ttl,
            "signer": signer,
        }))
        body_out = dict(url=url, filename=filename, method=method)
        if new_session_token:
            # Migration path: clients arriving with only a Google id_token
            # receive a session_token in the response and use it from then
            # on. Existing sailview JS knows to look for `session_token`
            # in any 200 response.
            body_out['session_token'] = new_session_token
        return {
            **res,
            'statusCode': 200,
            'body': json.dumps(body_out),
        }

    return {**res, 'statusCode': 400,
            'body': json.dumps({'message': f'Unknown method: {method}'})}
