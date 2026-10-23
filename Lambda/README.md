# Lambda — montlake_upload

Local development, testing, and deployment for `montlake_upload`. It authenticates users, signs permitted data reads and upload-bucket writes, and validates collaborative document changes.

## Collaborative documents and security controls

Current allowlisted team members can edit shared playlists and review labels.
`READ_JSON` returns a document and ETag; `SAVE_JSON` accepts only the two supported
schemas and requires that revision. S3 conditional writes reject stale saves with
409. Raw PUT URLs for `regattaanalysis` are disabled. `REGISTER_ALIAS` binds a
comment name to the authenticated account; `APPEND_COMMENT` checks that owner
and generates the ID/time on the server. Existing ownerless comment files need
an operator migration and cannot be claimed by an arbitrary account.

Signing policy enforcement cannot be turned off. Expired allowlist caches fail
closed when refresh fails; unknown Google key IDs refresh at most once per minute
per warm container. Request/document sizes and fields are bounded. Infrastructure
controls are reproducible with `Scripts/harden_site.py --snapshot-dir PATH`
(read-only plan) and `--apply`: HTTPS, CSP/security headers, versioning, reserved
concurrency 10, and 365-day CloudWatch/S3 log retention. Review the saved rollback
configuration before applying changes.

Regression checks: `python -m unittest Lambda.test_local Lambda.test_security`.

## Directory layout

```
Lambda/
  src/                    # Source of truth for the Lambda code
    lambda_function.py    # Handler
    utils.py              # Helpers (presigned URLs, body parsing)
  tests/
    events.py             # Factory functions for test events
  deployed/               # (gitignored) Downloaded copy of what's live on AWS
  test_local.py           # Run handler locally with synthetic events
  test_remote.py          # Hit a live endpoint with real HTTP requests
  download.sh             # Download current deployed code for diffing
  deploy.sh               # Deploy src/ to PRODUCTION
  deploy_test.sh          # Deploy src/ to a TEST function (safe)
```

## Workflow

### 1. Edit code

Edit `Lambda/src/lambda_function.py` and `Lambda/src/utils.py` directly.

### 2. Test locally

Calls `lambda_handler()` in-process. Needs AWS credentials (for boto3 presigned URL generation) but doesn't deploy anything.

```bash
python Lambda/test_local.py -v
```

### 3. Deploy to test Lambda

Creates/updates a `montlake_upload_test` function with its own URL, leaving production alone.

```bash
./Lambda/deploy_test.sh
```

### 4. Test the deployed code

```bash
# Against the test function
python Lambda/test_remote.py --url https://YOUR_TEST_URL/

# Against production
python Lambda/test_remote.py
```

### 5. Deploy to production

```bash
./Lambda/deploy.sh            # deploy
./Lambda/deploy.sh --dry-run  # just build the zip
```

### 6. Check for drift

Download the currently deployed code and diff against your local copy:

```bash
./Lambda/download.sh
# It runs `diff -r src/ deployed/` automatically
```

## Credentials

The Lambda accepts two auth modes during the migration:

1. **Google Sign-In (preferred)**: client posts `{id_token: <google JWT>}`. The
   Lambda verifies the token against the OAuth client (via env var
   `GOOGLE_CLIENT_ID`) and looks up the email in
   `s3://regattaanalysis/auth/allowlist.json`. Allowlist managed by
   `Scripts/manage_allowlist.py`. Presigned URLs issued with a 1h TTL.
   These identities get GET signing and document operations only — no PUT
   signing to any bucket. If the allowlist can't be read the Lambda denies
   with 503 `allowlist unavailable` (clients keep their session token).
2. **Legacy iOS uploader** (`peergynt`): client posts `{username, password}`.
   The password is read from env `LEGACY_UPLOADER_PASSWORD`; **unset = legacy
   auth disabled** (the state since 2026-10-19 — the old hardcoded password
   leaked via git history and iOS binaries). This identity may only PUT
   timestamped objects into `montlakeupload/` (always enforced, `exact` is
   ignored); 24h TTL. The iOS app reads it from Info.plist
   `LegacyUploaderPassword` and skips uploads when absent.

The local test password for the legacy path is in `tests/events.py`
(`test_local.py` patches it in). `test_remote.py` reads
`LEGACY_UPLOADER_PASSWORD` from your shell and skips legacy tests without it. Token-path
tests in `test_local.py` mock `verify_google_id_token` so they don't need a
real Google account.

## Vendored deps

`requirements.txt` includes `rsa>=4.9` for parsing the CloudFront signing key;
its `pyasn1` dependency is installed alongside it. `deploy.sh` installs these
into the gitignored `src/vendor/` directory. The remaining runtime code uses
the standard library and boto3 (provided by the Lambda runtime).

History: until the slim refactor, the Lambda vendored `google-auth` +
`requests` (~24 MB total, dominated by `cryptography`'s 14 MB Rust
binary). `verify_google_id_token` used `google.oauth2.id_token.
verify_oauth2_token`, which fetched Google's JWKS over HTTPS on every
cold container — that round-trip was the dominant cold-start cost
(observed at 8–10 s in CloudWatch). The slim refactor replaced the whole
chain with ~30 lines of pure-Python RS256 verification (`utils.py::
_verify_rs256`) and cached the JWKS in S3 (`auth/google-jwks.json`),
removing all third-party deps and dropping cold start to ~400 ms at
1024 MB / arm64.

The cross-build flags in `deploy.sh` keep vendored dependencies compatible with
the Lambda runtime:

> `pip install --platform manylinux2014_aarch64 --only-binary=:all:
> --python-version 3.12 --implementation cp --target src/vendor -r requirements.txt`
>
> Without `--platform`, a macOS build host installs Mach-O binaries that
> fail at runtime with `ImportError: invalid ELF header`.

## Runtime configuration (out-of-band, not in code)

Lambda function settings that aren't captured in `src/`:

| Setting | Value | Why |
|---|---|---|
| Runtime | `python3.12` | Bumped from `python3.7` (EOL) on 2026-04-26. |
| Timeout | 15 s | Originally chosen for the google-auth JWKS-from-Google round-trip. Now mostly headroom; the slim Lambda finishes in ms warm and ~400 ms cold. Leave generous. |
| Memory | 1024 MB | Bumped from 128 MB on 2026-04-27. CPU scales with memory; 128 MB starved boto3.client('s3') cold init at ~2.8 s. At 1024 MB it's ~400 ms. ~$1–3/mo extra at our volume. |
| Architecture | `arm64` | Graviton. Switched from `x86_64` on 2026-04-27 alongside the slim refactor. With no native deps to cross-build, arm64 is free 20% off compute and slightly faster cold init. |
| Function URL CORS | `AllowOrigins=[regattaview.net, www.regattaview.net, CF auto-hostname, localhost:9000], AllowMethods=[POST], AllowHeaders=[content-type], MaxAge=3600` | Single source of truth for CORS — the handler does **not** emit `Access-Control-Allow-Origin`. Function URL CORS also covers timeout/error responses (when the handler never runs) and auto-handles OPTIONS preflight. Returning headers from both layers gave the browser duplicated `Access-Control-Allow-Origin` values, which it rejects. |
| Env var `GOOGLE_CLIENT_ID` | `…apps.googleusercontent.com` | Used by `verify_google_id_token`; matches the OAuth client in Google Cloud Console. |
| Env var `SESSION_SECRET_ARN` | `regattaview/session-secret` (live since 2026-10-19) | When set (and `SESSION_SECRET` is not), the HMAC key is loaded once per warm container from this secret. Role needs `secretsmanager:GetSecretValue` (`iam/regattaview_session_secret.json`). |
| Env var `SESSION_SECRET` | 64 hex chars (random 32 bytes) | **Unset in prod since 2026-10-19.** Plain-env fallback; takes precedence if set (rollback path). HMAC-SHA256 key for the app-issued session JWTs minted on Google sign-in. Set 2026-04-27. **Rotating invalidates all active sessions** — every signed-in user gets bounced to login on their next request. Don't rotate casually; the same secret is read by every warm container, so a fresh deploy without the env var would break auth. |
| Env var `SESSION_TTL_S` | unset → 30 days | Override to e.g. `604800` for 7-day sessions if desired. |
| Env var `JWKS_BUCKET` / `JWKS_KEY` | unset → `regattaanalysis` / `auth/google-jwks.json` | Where Google's JWKS is cached. **Read-only** for the Lambda (its role has no PutObject on `auth/`, so a minted URL can't plant a key): on `kid` miss it fetches from Google into memory only. Only initial Google sign-in needs JWKS. Refresh the S3 copy after Google rotates keys with `curl https://www.googleapis.com/oauth2/v3/certs \| aws s3 cp - s3://regattaanalysis/auth/google-jwks.json`. |
| Env var `ALLOWLIST_TTL_SECONDS` | unset → 300 (5 min) | Lower this to tighten revocation window when removing users from `auth/allowlist.json` (at the cost of one more S3 GET per Lambda invocation per cache-miss). 5 min is the default; a removed user is locked out within that window even with a 30-day session token. |
| `SIGNING_POLICY_ENFORCE` | always **true** | Presign allowlist (`_signing_policy_reason`). Denials return 403 and log `WOULD_DENY`. Environment overrides are ignored. |
| Env var `LEGACY_UPLOADER_PASSWORD` | unset (legacy auth off) | See Credentials. |
| Env var `CF_SIGNING_ENABLED` | unset / false | Feature flag for the data plane. When truthy, GET signing returns a CloudFront signed URL (host `regattaview.net`) instead of an S3 SigV4 URL. PUT stays on S3 SigV4 either way. Flip with `aws lambda update-function-configuration` for a one-line cutover/rollback. See `Scripts/cf_signing_setup.md` for the AWS-side prerequisites (key pair, key group, distribution behavior). |
| Env var `CF_DOMAIN` | unset → `regattaview.net` | CloudFront domain that fronts the `regattaanalysis` bucket. Override only if running against a non-prod distribution. |
| Env var `CF_KEY_PAIR_ID` | (the CF Public Key ID) | Returned by `aws cloudfront create-public-key`. Embedded in every signed URL as `Key-Pair-Id`. CF rejects URLs whose `Key-Pair-Id` isn't in any trusted key group on the matched cache behavior, so this is what makes rotation possible. |
| Env var `CF_PRIVATE_KEY_SECRET_ARN` | Secrets Manager ARN | Holds the PEM-encoded RSA-2048 private key used for `RSA-SHA1` signing. Lambda's IAM role needs `secretsmanager:GetSecretValue` on this ARN. Loaded once per warm container; no caching at module import (lazy in `_load_cf_private_key`) so cold starts on the legacy S3 path pay nothing. |

Inspect / change via:

```bash
aws lambda get-function-configuration --function-name montlake_upload
aws lambda get-function-url-config       --function-name montlake_upload
aws lambda update-function-configuration --function-name montlake_upload --timeout 15
aws lambda update-function-url-config    --function-name montlake_upload --cors '...'
```

If you create a fresh test function via `deploy_test.sh`, none of these
settings are inherited automatically — set them explicitly before
exercising the token-auth path.

## IAM role (`montlake_upload-role-*`)

Source of truth: `iam/montlake_upload_s3.json` (S3 Get/Put narrowed to the
exact prefixes the signing policy allows, plus read-only `auth/allowlist.json`
and `auth/google-jwks.json`), `iam/regattaview_session_secret.json`, and
`iam/regattaview_cf_signing_secret.json` (CF URL-signing key).
Any permission the role holds can be presigned, so keep these in lockstep
with `_signing_policy_reason`. Applied 2026-10-19 (old broad
`regattaanalysis_download` / `montlake_upload_lambda_policy` removed). To re-apply:

```bash
R=montlake_upload-role-tvmf9qvn
aws iam put-role-policy --role-name $R --policy-name montlake_upload_s3 \
    --policy-document file://Lambda/iam/montlake_upload_s3.json
# then remove the old broad policies:
aws iam delete-role-policy --role-name $R --policy-name regattaanalysis_download
aws iam delete-role-policy --role-name $R --policy-name montlake_upload_lambda_policy
```

## Logging

The handler emits one structured INFO line per minted presigned URL:

```
PRESIGN {"identity": "...", "method": "...", "bucket": "...",
         "key": "...", "ip": "...", "user_agent": "...", "ttl_s": ...}
```

`identity` is the Google email (token auth) or the legacy username.
`ip` and `user_agent` are captured from the Lambda Function URL request
context — i.e. the client that hit the Lambda, not necessarily the
eventual S3 fetcher. Used for who-watched-what attribution joined
against the S3 server access log; see `AWS_SECURITY_STATUS.md` §4 and
the helper `Scripts/aws_presign_log.sh`.

Do not log the raw presigned URL. The query string contains
`X-Amz-Signature`, so the URL is a bearer credential until expiry.
`utils.generate_presigned_url()` intentionally returns the URL without
printing it; use the `PRESIGN` line above for debugging and attribution.

Signing-policy denials emit warnings like:

```
WOULD_DENY {"identity": "...", "method": "GET", "bucket": "...",
            "key": "...", "reason": "...", "enforced": true}
```

Check them after any client change that touches new keys:

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/montlake_upload \
  --filter-pattern WOULD_DENY
```

The module logger is bumped to INFO at import time because Lambda's
default root level is WARNING.
