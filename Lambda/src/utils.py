from botocore.exceptions import ClientError
import datetime as dt
import hashlib
import hmac
import logging
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

# `vendor/` was used by the previous google-auth-based implementation. After
# the slim refactor (2026-04-27) the Lambda has no third-party Python deps;
# this hook is preserved as a no-op so it keeps working if vendor/ ever
# returns.
_VENDOR = os.path.join(os.path.dirname(__file__), "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

def generate_presigned_url(s3_client, client_method, method_parameters, expires_in):
    """
    Generate a presigned Amazon S3 URL that can be used to perform an action.

    :param s3_client: A Boto3 Amazon S3 client.
    :param client_method: The name of the client method that the URL performs.
    :param method_parameters: The parameters of the specified client method.
    :param expires_in: The number of seconds the presigned URL is valid for.
    :return: The presigned URL.
    """
    try:
        url = s3_client.generate_presigned_url(
            ClientMethod=client_method,
            Params=method_parameters,
            ExpiresIn=expires_in
        )
    except ClientError:
        logger.exception(
            "Couldn't get a presigned URL for client method '%s'.", client_method)
        raise
    return url


def upload_name(filename):
    "Create an upload filename, handling the case if the request is missing."
    now = dt.datetime.now()
    date_string = now.strftime("%Y%m%d_%H%M%S")
    if filename:
        return filename + "_" + date_string
    else:
        return "upload_" + date_string


def fetch_path (d, path):
    "Given a deep JSON structure, return the results along a path of keys."
    if isinstance(d, dict):
        if len(path) == 1:
            return d.get(path[0])
        elif len(path) > 1:
            val = d.get(path[0])
            if val:
                return fetch_path(val, path[1:])

def set_path (d, path, new_val):
    "Given a deep JSON structure, set a key deep along a path of keys"
    if isinstance(d, dict):
        if len(path) == 1:
            d[path[0]] = new_val
            return True
        elif len(path) > 1:
            val = d.get(path[0])
            if val:
                return set_path(val, path[1:], new_val)


def get_method(event):
    "Extract the HTTP method from the event"
    return fetch_path(event, "requestContext http method".split())

def get_body(event):
    "Extract the body from the event"
    body = fetch_path(event, ['body'])
    isB64 = fetch_path(event, ['isBase64Encoded'])
    if body:
        if not isinstance(body, str) or len(body) > 2 * 1024 * 1024:
            raise ValueError('Body too large or invalid')
        if isB64:
            return json.loads(base64.b64decode(body))
        else:
            return json.loads(body)

def multiline_encode(long):
    """
    Take a long byte sequence, and encode and a list of bytes.  
    Good for creating constants you can put in tests.
    """
    b64 = base64.b64encode(long)
    res = []
    while len(b64) > 0:
        res.append(b64[:60])
        b64 = b64[60:]
    return res

def multiline_decode(byte_lines):
    "Takes a list of bytes, concats, and decodes."
    res = byte_lines[0]
    for l in byte_lines[1:]:
        res = res + l
    return base64.b64decode(res)


# ---------------------------------------------------------------------------
# Google Sign-In + per-user allowlist
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ALLOWLIST_BUCKET = os.environ.get("ALLOWLIST_BUCKET", "regattaanalysis")
ALLOWLIST_KEY = os.environ.get("ALLOWLIST_KEY", "auth/allowlist.json")
ALLOWLIST_TTL_SECONDS = int(os.environ.get("ALLOWLIST_TTL_SECONDS", "300"))

# JWKS is read from our own bucket so cold containers don't pay an HTTPS
# round-trip to Google (which was 8–10 s in CloudWatch). On `kid`-not-found we
# fetch from Google into the in-memory cache only: the Lambda never writes the
# S3 copy, so its role needs no PutObject on `auth/` (a writable JWKS would let
# anyone who can mint a PUT URL plant a key and forge Google ID tokens).
# Refresh the S3 copy out-of-band (see Lambda/README.md). Only initial
# sign-in verifies Google tokens; session-token calls never touch JWKS.
JWKS_BUCKET = os.environ.get("JWKS_BUCKET", ALLOWLIST_BUCKET)
JWKS_KEY = os.environ.get("JWKS_KEY", "auth/google-jwks.json")
JWKS_REFRESH_TTL_S = int(os.environ.get("JWKS_REFRESH_TTL_S", "3600"))
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

_allowlist_cache = {"data": None, "fetched_at": 0.0}

# JWKS keys, parsed once into (n, e) integer pairs keyed by `kid`. Module-
# level so warm containers reuse them. On a `kid` miss we re-fetch.
_jwks_cache = {"keys": None, "fetched_at": 0.0}
_jwks_last_attempt = 0.0
JWKS_MIN_REFRESH_S = 60

# Verified-token cache: (email, exp_epoch_s). ID tokens are signed and have
# a fixed `exp` claim; if we've already verified one this container
# lifetime, skip the JWKS dance entirely. Sized to bound memory under load.
_VERIFIED_TOKEN_CACHE = {}
_VERIFIED_TOKEN_CACHE_MAX = 256


# ---- Pure-Python RS256 ----------------------------------------------------
# RS256 = RSA-PKCS#1-v1.5 signature with SHA-256 hash. Verification is just:
#   1. RSA-decrypt the signature with the public key (modular exp).
#   2. Reconstruct the expected PKCS#1-v1.5-padded SHA-256 of the signing
#      input and compare. Plain `==` is fine: signatures are not secrets.

# DigestInfo prefix for SHA-256 (RFC 8017 §9.2 step 2): the ASN.1 DER
# encoding of `DigestInfo { digestAlgorithm = sha256, digest = OCTET STRING }`,
# minus the 32-byte digest itself.
_SHA256_DIGEST_INFO = bytes.fromhex(
    "3031300d060960864801650304020105000420")


def _b64url_decode(s):
    if isinstance(s, str):
        s = s.encode("ascii")
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


def _verify_rs256(token, jwk):
    """Verify an RS256 JWT against a JWK (with `n` and `e` base64url-encoded).
    Returns the parsed claims dict on success, None on signature mismatch."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        return None
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")

    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    sig_int = int.from_bytes(_b64url_decode(sig_b64), "big")
    if sig_int >= n:
        return None
    em_int = pow(sig_int, e, n)
    em_len = (n.bit_length() + 7) // 8
    em = em_int.to_bytes(em_len, "big")

    digest = hashlib.sha256(signing_input).digest()
    pad_len = em_len - len(_SHA256_DIGEST_INFO) - len(digest) - 3
    if pad_len < 8:  # PKCS#1 v1.5 requires ≥8 bytes of 0xff padding
        return None
    expected = (
        b"\x00\x01" + b"\xff" * pad_len + b"\x00"
        + _SHA256_DIGEST_INFO + digest
    )
    if em != expected:
        return None
    try:
        return json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None


# ---- CloudFront URL signing (RSA-SHA1) ------------------------------------
# CloudFront's trusted-key-group signing uses RSA-PKCS#1-v1.5 with SHA-1
# (legacy CF requirement; not configurable). Same envelope as RS256, just a
# SHA-1 digest and a different DigestInfo prefix. We extend the existing
# pure-Python RSA primitives (above) with a private-key signing path; the
# only third-party dep is `rsa` (pure-Python, ~20 KB) and only for parsing
# the PEM-encoded private key into (n, d) integers — the actual RSA op is
# stdlib pow().
#
# Used by `generate_cf_signed_url` below, which delegates to
# `botocore.signers.CloudFrontSigner` for the canned-policy URL assembly
# (CF uses a custom URL-safe base64 alphabet — `-_~` instead of `+/=` —
# which CloudFrontSigner already implements).

# DigestInfo prefix for SHA-1 (RFC 8017 §9.2 step 2): the ASN.1 DER
# encoding of `DigestInfo { digestAlgorithm = sha1, digest = OCTET STRING }`,
# minus the 20-byte digest itself.
_SHA1_DIGEST_INFO = bytes.fromhex("3021300906052b0e03021a05000414")

_CF_PRIVATE_KEY_SECRET_ARN = os.environ.get("CF_PRIVATE_KEY_SECRET_ARN", "")
_CF_KEY_PAIR_ID = os.environ.get("CF_KEY_PAIR_ID", "")
_CF_DOMAIN = os.environ.get("CF_DOMAIN", "regattaview.net")

# Cached parsed private key. Loaded once per warm container; the boto3
# Secrets Manager client + the rsa.PrivateKey PEM parse are both deferred
# inside the loader so the legacy S3-SigV4 path pays nothing on cold start.
_cf_private_key_cache = {"n": None, "d": None}


def _load_cf_private_key():
    """Parse the CF signing private key from Secrets Manager once per warm
    container. Returns (n, d). Raises RuntimeError if env not configured."""
    if _cf_private_key_cache["n"] is not None:
        return _cf_private_key_cache["n"], _cf_private_key_cache["d"]
    if not _CF_PRIVATE_KEY_SECRET_ARN:
        raise RuntimeError("CF_PRIVATE_KEY_SECRET_ARN env var is not set")
    import boto3  # lazy: avoid cold-start cost when CF signing is disabled
    import rsa    # lazy: vendored only when CF signing is enabled
    sm = boto3.client("secretsmanager")
    pem = sm.get_secret_value(
        SecretId=_CF_PRIVATE_KEY_SECRET_ARN
    )["SecretString"].encode("ascii")
    pk = rsa.PrivateKey.load_pkcs1(pem)
    _cf_private_key_cache["n"] = pk.n
    _cf_private_key_cache["d"] = pk.d
    return pk.n, pk.d


def _rsa_sha1_sign(message):
    """RSA-PKCS#1-v1.5 sign with SHA-1 over `message`. Returns the raw
    signature bytes (length = ceil(n.bit_length() / 8)). Used as the
    `rsa_signer` callable for botocore.signers.CloudFrontSigner."""
    n, d = _load_cf_private_key()
    em_len = (n.bit_length() + 7) // 8
    digest = hashlib.sha1(message).digest()
    pad_len = em_len - len(_SHA1_DIGEST_INFO) - len(digest) - 3
    if pad_len < 8:  # PKCS#1 v1.5 requires ≥8 bytes of 0xff padding
        raise ValueError("RSA modulus too small for PKCS#1 v1.5 + SHA-1")
    em = (b"\x00\x01" + b"\xff" * pad_len + b"\x00"
          + _SHA1_DIGEST_INFO + digest)
    em_int = int.from_bytes(em, "big")
    sig_int = pow(em_int, d, n)
    return sig_int.to_bytes(em_len, "big")


def generate_cf_signed_url(key, expires_in, cf_domain=None):
    """Return a CloudFront signed URL valid for `expires_in` seconds.

    `key` is the S3-relative path (e.g. ``www/Insta360/VID_….mp4``). The
    URL is built against ``cf_domain`` (default: env ``CF_DOMAIN``).
    Uses botocore's CloudFrontSigner with a canned policy — handles CF's
    custom URL-safe base64 alphabet and query-param assembly.
    """
    from botocore.signers import CloudFrontSigner  # lazy
    if not _CF_KEY_PAIR_ID:
        raise RuntimeError("CF_KEY_PAIR_ID env var is not set")
    domain = cf_domain or _CF_DOMAIN
    expire_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in)
    signer = CloudFrontSigner(_CF_KEY_PAIR_ID, _rsa_sha1_sign)
    base_url = f"https://{domain}/{urllib.parse.quote(key.lstrip('/'), safe='/')}"
    return signer.generate_presigned_url(base_url, date_less_than=expire_at)


# ---- JWKS loading ---------------------------------------------------------

def _fetch_google_jwks_direct():
    """One-shot fetch from Google with a tight timeout. Used as a fallback
    when the S3-cached JWKS is missing or out of date for a `kid`."""
    with urllib.request.urlopen(GOOGLE_JWKS_URL, timeout=3) as r:
        return json.loads(r.read())


def _load_jwks(s3_client, force_refresh=False):
    """Load the JWKS as {kid: jwk_dict}.

    Normal path: in-memory cache → S3 cache → Google. When the caller passes
    `force_refresh=True` (after a `kid` miss), skip BOTH caches and go
    straight to Google. Without the S3 skip a stale-but-well-formed S3
    JWKS silently wins over Google's current keys and every sign-in fails
    until the S3 object is manually overwritten — that was the 2026-05-11
    outage.
    """
    global _jwks_last_attempt
    now = time.time()
    if force_refresh and now - _jwks_last_attempt < JWKS_MIN_REFRESH_S:
        return _jwks_cache['keys'] or {}
    if (not force_refresh
            and _jwks_cache["keys"] is not None
            and now - _jwks_cache["fetched_at"] < JWKS_REFRESH_TTL_S):
        return _jwks_cache["keys"]

    jwks = None
    if not force_refresh:
        try:
            obj = s3_client.get_object(Bucket=JWKS_BUCKET, Key=JWKS_KEY)
            jwks = json.loads(obj["Body"].read())
        except (ClientError, ValueError):
            logger.info("JWKS not in s3://%s/%s; fetching from Google",
                        JWKS_BUCKET, JWKS_KEY)

    if not isinstance(jwks, dict) or "keys" not in jwks:
        if now - _jwks_last_attempt < JWKS_MIN_REFRESH_S:
            return _jwks_cache['keys'] or {}
        _jwks_last_attempt = now  # Also limit retries after an upstream failure.
        try:
            jwks = _fetch_google_jwks_direct()
        except Exception:
            logger.exception("Failed to fetch JWKS from Google")
            return _jwks_cache["keys"] or {}

    keys = {k["kid"]: k for k in jwks.get("keys", []) if k.get("kid")}
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


# ---- Verifier -------------------------------------------------------------

def verify_google_id_token(token, s3_client):
    """Return the verified, lowercased email address if the ID token is valid
    and issued for our OAuth client; else None."""
    if not isinstance(token, str) or not token or len(token) > 16384 or not GOOGLE_CLIENT_ID:
        return None

    # Hot path: same JWT verified earlier in this container's lifetime.
    now = time.time()
    cached = _VERIFIED_TOKEN_CACHE.get(token)
    if cached is not None:
        email, exp = cached
        if now < exp - 30:
            return email
        _VERIFIED_TOKEN_CACHE.pop(token, None)

    # Pull `kid` from the unverified header so we know which JWK to use.
    try:
        header_b64 = token.split(".", 1)[0]
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(header, dict): return None
    kid = header.get("kid")
    if not isinstance(kid, str) or len(kid) > 200: return None
    if not kid or header.get("alg") != "RS256":
        logger.warning("ID token has unsupported alg/kid: alg=%s kid=%s",
                       header.get("alg"), kid)
        return None

    keys = _load_jwks(s3_client)
    jwk = keys.get(kid)
    if jwk is None:
        # `kid` not in cache — Google may have rotated. Refresh once.
        keys = _load_jwks(s3_client, force_refresh=True)
        jwk = keys.get(kid)
        if jwk is None:
            logger.warning("ID token kid=%s not found in JWKS", kid)
            return None

    try:
        claims = _verify_rs256(token, jwk)
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return None
    if not isinstance(claims, dict):
        logger.warning("ID token signature verification failed")
        return None

    # Validate claims: aud, iss, exp, email_verified.
    if claims.get("aud") != GOOGLE_CLIENT_ID:
        logger.warning("ID token aud mismatch: %s", claims.get("aud"))
        return None
    if claims.get("iss") not in GOOGLE_ISSUERS:
        logger.warning("ID token iss not Google: %s", claims.get("iss"))
        return None
    exp = claims.get("exp", 0)
    if type(exp) is not int: return None
    if not exp or now >= exp:
        logger.warning("ID token expired (exp=%s, now=%s)", exp, int(now))
        return None
    if claims.get("email_verified") is not True:
        logger.warning("ID token email not verified for %s",
                       claims.get("email"))
        return None
    email = claims.get("email")
    if not isinstance(email, str) or not email or len(email) > 320:
        return None
    email = email.lower()

    if len(_VERIFIED_TOKEN_CACHE) >= _VERIFIED_TOKEN_CACHE_MAX:
        _VERIFIED_TOKEN_CACHE.clear()
    _VERIFIED_TOKEN_CACHE[token] = (email, exp)
    return email


# ---- App-issued session tokens (HS256 JWT) -------------------------------
# Verifying a Google ID token costs an S3 round-trip (JWKS) on first cold
# start and ~50 ms of pure-Python RSA per token thereafter. Once we've
# verified the user once, mint our own short-form HMAC-signed JWT — verify
# is microseconds (constant-time hmac compare), and the user stays signed
# in for SESSION_TTL_S regardless of Google's 1 h ID-token expiry.
#
# The HMAC secret is loaded once per warm container from Secrets Manager
# (SESSION_SECRET_ARN), falling back to the plain SESSION_SECRET env var.
# Rotating the secret invalidates all sessions — fine for an emergency
# response; we don't do graceful rotation here.

_SESSION_SECRET_ARN = os.environ.get("SESSION_SECRET_ARN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").encode("ascii")
SESSION_TTL_S = int(os.environ.get("SESSION_TTL_S", str(30 * 24 * 60 * 60)))
SESSION_ISSUER = os.environ.get("SESSION_ISSUER", "regatta")


def _session_secret():
    global SESSION_SECRET
    if not SESSION_SECRET and _SESSION_SECRET_ARN:
        import boto3  # lazy, like _load_cf_private_key
        sm = boto3.client("secretsmanager")
        SESSION_SECRET = sm.get_secret_value(
            SecretId=_SESSION_SECRET_ARN)["SecretString"].strip().encode("ascii")
    return SESSION_SECRET


def _b64url_encode(b):
    if isinstance(b, str):
        b = b.encode()
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def mint_session_token(email):
    """Issue a long-lived HS256 JWT for an already-verified email. Returns
    None if the SESSION_SECRET env var isn't set."""
    secret = _session_secret()
    if not secret or not email:
        if not secret:
            logger.error("SESSION_SECRET not set; cannot mint session token")
        return None
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": SESSION_ISSUER,
        "sub": email.lower(),
        "iat": now,
        "exp": now + SESSION_TTL_S,
    }
    h64 = _b64url_encode(json.dumps(header, separators=(",", ":")))
    p64 = _b64url_encode(json.dumps(payload, separators=(",", ":")))
    signing_input = (h64 + "." + p64).encode("ascii")
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return h64 + "." + p64 + "." + _b64url_encode(sig)


def verify_session_token(token):
    """Verify an HS256 session JWT we minted. Returns the lowercased email
    on success, None otherwise. Constant-time HMAC comparison."""
    if not isinstance(token, str) or not token or len(token) > 16384: return None
    try:
        header = json.loads(_b64url_decode(token.split('.')[0]))
        if not isinstance(header, dict) or header.get('alg') != 'HS256': return None
    except (ValueError, UnicodeError):
        return None
    secret = _session_secret()
    if not secret:
        return None
    try:
        h64, p64, s64 = token.split(".")
    except ValueError:
        return None
    try:
        signing_input = (h64 + "." + p64).encode("ascii")
    except UnicodeError:
        return None
    expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
    try:
        actual = _b64url_decode(s64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        claims = json.loads(_b64url_decode(p64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict) or claims.get("iss") != SESSION_ISSUER:
        return None
    exp = claims.get("exp", 0)
    if type(exp) is not int: return None
    if not exp or time.time() >= exp:
        return None
    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        return None
    return sub.lower()


def get_allowlist(s3_client, bucket=None, key=None, force_refresh=False):
    """Return the cached allowlist (list of dicts), refreshing from S3 if stale.
    Cache lives in module-level state, so it survives across warm Lambda invocations
    but resets on cold start. ALLOWLIST_TTL_SECONDS controls staleness."""
    bucket = bucket or ALLOWLIST_BUCKET
    key = key or ALLOWLIST_KEY
    now = time.time()
    if (
        not force_refresh
        and _allowlist_cache["data"] is not None
        and now - _allowlist_cache["fetched_at"] < ALLOWLIST_TTL_SECONDS
    ):
        return _allowlist_cache["data"]
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read())
        if not isinstance(data, list):
            raise ValueError("allowlist must be a JSON array")
        _allowlist_cache["data"] = data
        _allowlist_cache["fetched_at"] = now
        return data
    except (ClientError, ValueError):
        logger.exception("Failed to load allowlist from s3://%s/%s", bucket, key)
        # Expired authorization data cannot extend a removed user's access.
        # None (not []) so callers deny as "allowlist unavailable" rather than
        # telling a valid user they were removed.
        return None


def is_email_authorized(email, allowlist):
    """Return (ok: bool, reason: str). Reason is human-readable for logs / UI."""
    if not email:
        return False, "no email"
    if allowlist is None:
        return False, "allowlist unavailable"
    email = email.lower()
    now = dt.datetime.now(dt.timezone.utc)
    for entry in allowlist:
        entry_email = (entry.get("email") or "").lower()
        if entry_email != email:
            continue
        expires_at = entry.get("expires_at")
        if not expires_at:
            return True, "ok (no expiry)"
        try:
            # Accept ISO 8601 with trailing Z.
            exp = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False, "bad expires_at"
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)
        if now > exp:
            return False, "expired"
        return True, "ok"
    return False, "not on allowlist"