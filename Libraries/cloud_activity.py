"""
Cloud activity observability for RegattaView.

Stitches read-only AWS sources into a single per-day, per-user report
covering both billing usage and security activity:

  1. Cost Explorer (`get_cost_and_usage`)         — daily $ by SERVICE × USAGE_TYPE
  2. S3 server access logs                         — origin reads, COPY ops, bytes
     s3://regattaanalysis-logs/regattaanalysis/<UTC-prefix>...
  3. CloudFront access logs                        — real client IP, end-user view
     s3://regattaanalysis-logs/cf-logs/EPCTF7XCNPLK2.YYYY-MM-DD-HH.<hash>.gz
  4. Lambda CloudWatch PRESIGN events             — identity ↔ key mapping
     Log group /aws/lambda/montlake_upload, lines 'PRESIGN {json}'
  5. Lambda CloudWatch auth/security events       — sign-ins + failures + denies
     Same log group, lines 'SIGNIN {json}', 'AUTH_FAIL {json}',
     'WOULD_DENY {json}', 'Allowlist denied <email>: <reason>'.
  6. Allowlist snapshot at s3://regattaanalysis/auth/allowlist.json
  7. CloudFront distribution enabled-flag (killswitch indicator) via
     `cloudfront:GetDistribution` on EPCTF7XCNPLK2.

Identity attribution joins (key, ip, time-window) between the PRESIGN log and
the access logs (S3 or CF). This is the pattern from Scripts/aws_presign_log.sh
and AWS_SECURITY_STATUS.md §4, generalised to bulk aggregation.

All log data is cached under ~/Data/AwsUsage/ so re-runs are cheap. Past-day
records are written once and never rewritten; today's records are always
re-fetched. The directory name `AwsUsage/` is historical and predates the
module rename from `aws_usage.py` to `cloud_activity.py`; kept as-is so the
existing cache is reused.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import shlex
import subprocess
import time as time_mod
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import boto3
import pandas as pd

from Libraries.log_helper import LOG


# --- Constants ---------------------------------------------------------------

BUCKET_DATA = "regattaanalysis"
BUCKET_LOGS = "regattaanalysis-logs"
S3_LOG_PREFIX = "regattaanalysis/"            # trailing slash
CF_LOG_PREFIX = "cf-logs/"                    # trailing slash
CF_DISTRIBUTION = "EPCTF7XCNPLK2"
LAMBDA_LOG_GROUP = "/aws/lambda/montlake_upload"
# Own IP, stripped from CloudFront per-user stats. Set REGATTA_OWN_IP in the
# repo .env (gitignored); unset = nothing stripped.
OWN_IP_DEFAULT = os.environ.get("REGATTA_OWN_IP") or None
REPORT_TIMEZONE = "America/Los_Angeles"
REPORT_TZ = ZoneInfo(REPORT_TIMEZONE)
SERVICE_IDENTITIES = {"peergynt"}

# Any IAM user/* principal seen in S3 access logs is treated as an
# "operator" identity (race_upload.py, aws_sync.sh, manual aws-cli). These
# bypass the Lambda — no PRESIGN — so they otherwise go unattributed.
# Role-based principals (the Lambda's own role, CloudFront OAI, etc.) are
# excluded because their traffic is already covered by other rows.
_IAM_USER_RE = re.compile(r"arn:aws:iam::\d+:user/(\S+)")


def _iam_principal_name(requester: str) -> Optional[str]:
    """`arn:aws:iam::<acct>:user/<name>` → `<name>`. None otherwise."""
    if not requester or requester == "-":
        return None
    m = _IAM_USER_RE.match(requester)
    if not m:
        return None
    return m.group(1)

CACHE_ROOT = Path(os.path.expanduser("~/Data/AwsUsage"))

# S3 server-access logging and CloudFront access logging were both enabled
# on 2026-04-25 (see AWS_SECURITY_STATUS.md §2). Cost Explorer data goes
# back further, but per-byte / per-user attribution does not.
ACCESS_LOG_START = "2026-04-25"

# Per-file S3 GETs are tiny but numerous; thread pool keeps overall fetch
# time within a few seconds even on heavy days (4-5k files).
S3_FETCH_WORKERS = 32

# Content-class buckets, in match-order. Prefix-prefix wins.
CONTENT_CLASSES: list[tuple[str, str]] = [
    ("www/Insta360/",      "video"),
    ("www/Virb/",          "video-legacy"),
    ("www/Poses/",         "poses"),
    ("www/Analyze/",       "analyze"),
    ("www/Comments/",      "comments"),
    ("www/sailview/",      "frontend"),
    ("www/SeattleTiles/",  "tiles"),
    ("auth/",              "auth"),
]


def classify_key(key: str) -> str:
    key = key.lstrip("/")
    if key.startswith(("Insta360/", "Virb/", "Poses/", "Analyze/", "Comments/")):
        key = "www/" + key
    if (key.startswith(("js/", "css/"))
            or key.endswith(".html")
            or key in {"favicon.ico", "manifest.json", "site.webmanifest"}):
        return "frontend"
    for pfx, cls in CONTENT_CLASSES:
        if key.startswith(pfx):
            return cls
    return "other"


def _identity_kind(identity: str) -> str:
    """Classify identity as 'service' (legacy creds), 'operator' (IAM
    user/* principal — set elsewhere, this helper is for human/service
    only), or 'user' (allowlisted human). Operator rows are tagged at
    aggregation time, not here, since the same string could in principle
    be a human email."""
    return "service" if identity in SERVICE_IDENTITIES else "user"


# --- Cache layout ------------------------------------------------------------

def _cache_path(kind: str, day: str, ext: str) -> Path:
    """kind in {cost, s3logs, cflogs, presign, warnings}; day = YYYY-MM-DD."""
    p = CACHE_ROOT / kind
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{day}.{ext}"


def _is_complete_past_day(day: str) -> bool:
    """A UTC day is 'complete' (immutable for caching) once we're well into
    the next UTC day. We use a 6h grace window so end-of-day log delivery
    has time to flush."""
    today_utc = datetime.now(timezone.utc).date()
    target = date.fromisoformat(day)
    if target >= today_utc:
        return False
    # Be a little conservative: yesterday is "complete" only after 06:00 UTC today
    if target == today_utc - timedelta(days=1):
        return datetime.now(timezone.utc).hour >= 6
    return True


# --- Cost Explorer fetcher ---------------------------------------------------

def fetch_cost_explorer(start: str, end: str, *, no_cache: bool = False) -> pd.DataFrame:
    """
    Daily UnblendedCost grouped by SERVICE × USAGE_TYPE for [start, end).

    Returns DataFrame with columns: day, service, usage_type, cost.
    """
    # CE cache is per-window (start..end), not per-day — the API call is one
    # round-trip and we don't pay per-row to cache. Use a hash of the window.
    cache_key = f"{start}_{end}"
    cache_file = _cache_path("cost", cache_key, "json")

    if not no_cache and cache_file.exists():
        # Always refresh today's data: only trust cache if `end` is fully in the past
        end_date = date.fromisoformat(end)
        if end_date <= datetime.now(timezone.utc).date():
            try:
                with cache_file.open() as f:
                    raw = json.load(f)
                LOG.logger.debug(f"CE cache hit: {cache_file}")
                return _ce_to_dataframe(raw)
            except (json.JSONDecodeError, OSError):
                pass

    ce = boto3.client("ce")
    raw_results: list[dict] = []
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        raw_results.extend(resp.get("ResultsByTime", []))
        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    with cache_file.open("w") as f:
        json.dump({"ResultsByTime": raw_results}, f)
    LOG.logger.info(f"CE: cached {sum(len(r.get('Groups', [])) for r in raw_results)} rows -> {cache_file}")

    return _ce_to_dataframe({"ResultsByTime": raw_results})


def _ce_to_dataframe(raw: dict) -> pd.DataFrame:
    rows = []
    for r in raw.get("ResultsByTime", []):
        day = r["TimePeriod"]["Start"]
        for g in r.get("Groups", []):
            keys = g.get("Keys", ["?", "?"])
            service = keys[0] if len(keys) > 0 else "?"
            usage_type = keys[1] if len(keys) > 1 else ""
            cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
            rows.append({"day": day, "service": service, "usage_type": usage_type, "cost": cost})
    if not rows:
        return pd.DataFrame(columns=["day", "service", "usage_type", "cost"])
    return pd.DataFrame(rows)


# --- S3 access log fetcher / parser -----------------------------------------

# S3 server access log fields (positional). See:
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/LogFormat.html
S3_FIELDS = [
    "bucket_owner", "bucket", "time_start", "time_offset", "remote_ip",
    "requester", "request_id", "operation", "key", "request_uri",
    "http_status", "error_code", "bytes_sent", "object_size", "total_time_ms",
    "turn_around_time_ms", "referer", "user_agent",
]
_S3_TIME_RE = re.compile(r"\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def parse_s3_access_log_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        p = shlex.split(line)
    except ValueError:
        return None
    if len(p) < 18:
        return None
    raw_time = p[2]
    m = _S3_TIME_RE.match(raw_time)
    if not m:
        return None
    ts = datetime(
        int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)),
        tzinfo=timezone.utc,
    )
    bytes_sent = int(p[12]) if p[12].isdigit() else 0
    object_size = int(p[13]) if p[13].isdigit() else 0
    return {
        "time": ts,
        "ip": p[4],
        "requester": p[5],
        "op": p[7],
        "key": p[8],
        "status": p[10],
        "bytes_sent": bytes_sent,
        "object_size": object_size,
        "user_agent": p[17],
    }


def fetch_s3_access_logs(day: str, *, no_cache: bool = False) -> pd.DataFrame:
    """
    All S3 server access log records for the given UTC day.
    Cached as parquet under ~/Data/AwsUsage/s3logs/<day>.parquet.
    """
    cache_file = _cache_path("s3logs", day, "parquet")
    if not no_cache and cache_file.exists() and _is_complete_past_day(day):
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass

    # Stream log objects directly to avoid littering /tmp.
    s3 = boto3.client("s3")
    prefix = f"{S3_LOG_PREFIX}{day}"
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET_LOGS, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    LOG.logger.info(f"S3 logs {day}: {len(keys)} files")

    def _read_one(key: str) -> list[dict]:
        body = s3.get_object(Bucket=BUCKET_LOGS, Key=key)["Body"].read()
        out: list[dict] = []
        for line in body.decode("utf-8", errors="replace").splitlines():
            row = parse_s3_access_log_line(line)
            if row is not None:
                out.append(row)
        return out

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=S3_FETCH_WORKERS) as ex:
        for batch in ex.map(_read_one, keys):
            rows.extend(batch)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["content_class"] = df["key"].map(classify_key)
    if _is_complete_past_day(day):
        df.to_parquet(cache_file)
        LOG.logger.info(f"S3 logs {day}: {len(df)} rows -> {cache_file}")
    return df


# --- CloudFront access log fetcher / parser ---------------------------------

# CF log field index map; populated from the #Fields header on first line.
# We only keep fields we use, but parse generically so format drift fails loud.
_CF_KEEP = {
    "date", "time", "x-edge-location", "sc-bytes", "c-ip", "cs-method",
    "cs(Host)", "cs-uri-stem", "sc-status", "cs(User-Agent)",
    "x-edge-result-type", "cs-bytes", "time-taken", "time-to-first-byte",
    "x-edge-detailed-result-type", "sc-content-type",
    "sc-range-start", "sc-range-end",
}


def _parse_cf_log_bytes(blob: bytes) -> list[dict]:
    """Parse one gzipped CF log file. Returns list of records."""
    out: list[dict] = []
    fields: list[str] = []
    text = gzip.decompress(blob).decode("utf-8", errors="replace")
    for raw in text.splitlines():
        if not raw:
            continue
        if raw.startswith("#Version"):
            continue
        if raw.startswith("#Fields:"):
            fields = raw[len("#Fields:"):].strip().split()
            continue
        if not fields:
            continue
        parts = raw.split("\t")
        if len(parts) != len(fields):
            continue
        rec: dict[str, Any] = {}
        for f, v in zip(fields, parts):
            if f in _CF_KEEP:
                rec[f] = v
        # Build a single timezone-aware UTC timestamp
        d = rec.get("date", "")
        t = rec.get("time", "")
        if d and t:
            try:
                rec["time"] = datetime.fromisoformat(f"{d}T{t}+00:00")
            except ValueError:
                continue
        # Coerce numerics
        for nf in ("sc-bytes", "cs-bytes", "sc-status"):
            v = rec.get(nf, "-")
            rec[nf] = int(v) if v.lstrip("-").isdigit() else 0
        for nf in ("time-taken", "time-to-first-byte"):
            v = rec.get(nf, "-")
            try:
                rec[nf] = float(v)
            except (TypeError, ValueError):
                rec[nf] = 0.0
        # URL-decode user agent (CF percent-encodes spaces and friends)
        if "cs(User-Agent)" in rec:
            rec["cs(User-Agent)"] = unquote(rec["cs(User-Agent)"])
        # Drop the now-redundant date/time strings
        rec.pop("date", None)
        out.append(rec)
    return out


def fetch_cf_access_logs(day: str, *, no_cache: bool = False) -> pd.DataFrame:
    """
    All CloudFront access log records for the given UTC day.
    Filename pattern is <DistID>.YYYY-MM-DD-HH.<hash>.gz, hour-bucketed.
    """
    cache_file = _cache_path("cflogs", day, "parquet")
    if not no_cache and cache_file.exists() and _is_complete_past_day(day):
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass

    s3 = boto3.client("s3")
    prefix = f"{CF_LOG_PREFIX}{CF_DISTRIBUTION}.{day}"
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET_LOGS, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    LOG.logger.info(f"CF logs {day}: {len(keys)} files")

    def _read_cf(key: str) -> list[dict]:
        body = s3.get_object(Bucket=BUCKET_LOGS, Key=key)["Body"].read()
        try:
            return _parse_cf_log_bytes(body)
        except OSError:
            return []

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=S3_FETCH_WORKERS) as ex:
        for batch in ex.map(_read_cf, keys):
            rows.extend(batch)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.rename(columns={
            "c-ip": "client_ip",
            "cs-method": "method",
            "cs-uri-stem": "uri",
            "sc-status": "status",
            "sc-bytes": "bytes",
            "cs(User-Agent)": "user_agent",
            "cs(Host)": "host",
            "x-edge-result-type": "edge_result",
            "x-edge-location": "edge_loc",
            "time-to-first-byte": "ttfb",
            "time-taken": "time_total",
        })
        df["content_class"] = df["uri"].fillna("").map(
            lambda u: classify_key(u.lstrip("/"))
        )
    if _is_complete_past_day(day):
        df.to_parquet(cache_file)
        LOG.logger.info(f"CF logs {day}: {len(df)} rows -> {cache_file}")
    return df


# --- Lambda PRESIGN event fetcher --------------------------------------------

_PRESIGN_RE = re.compile(r"PRESIGN (\{.*\})")


def fetch_presign_logs(day: str, *, no_cache: bool = False) -> pd.DataFrame:
    """
    PRESIGN events from the Lambda CloudWatch log group for the given UTC day.
    Uses filter_log_events with PRESIGN filter pattern (matches the existing
    Scripts/aws_presign_log.sh approach).
    """
    cache_file = _cache_path("presign", day, "parquet")
    if not no_cache and cache_file.exists() and _is_complete_past_day(day):
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass

    start_dt = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    end_dt = start_dt + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    logs = boto3.client("logs")
    rows: list[dict] = []
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = dict(
            logGroupName=LAMBDA_LOG_GROUP,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern='"PRESIGN"',
            limit=10000,
        )
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            LOG.logger.warning(f"Log group {LAMBDA_LOG_GROUP} not found")
            break
        for ev in resp.get("events", []):
            msg = ev.get("message", "")
            m = _PRESIGN_RE.search(msg)
            if not m:
                continue
            try:
                p = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            rows.append({
                "time": datetime.fromtimestamp(ev["timestamp"] / 1000, tz=timezone.utc),
                "identity": p.get("identity", ""),
                "method": p.get("method", ""),
                "bucket": p.get("bucket", ""),
                "key": p.get("key", ""),
                "ip": p.get("ip", ""),
                "user_agent": p.get("user_agent", ""),
                "ttl_s": int(p.get("ttl_s", 0) or 0),
                "signer": p.get("signer", ""),
            })
        next_token = resp.get("nextToken")
        if not next_token:
            break

    df = pd.DataFrame(rows)
    if _is_complete_past_day(day):
        df.to_parquet(cache_file)
        LOG.logger.info(f"PRESIGN {day}: {len(df)} rows -> {cache_file}")
    return df


# --- Auth / security event fetch --------------------------------------------

# Single multi-pattern CloudWatch filter that picks up all four security-
# relevant log shapes the Lambda emits today. PRESIGN is deliberately
# excluded — it's already pulled by fetch_presign_logs with its own cache.
_AUTH_FILTER = '?SIGNIN ?AUTH_FAIL ?WOULD_DENY ?"Allowlist denied"'

# Allowlist-denied lines are unstructured (positional %s, %s formatting):
#   "Allowlist denied alice@example.com: expired"
_ALLOWLIST_DENIED_RE = re.compile(
    r"Allowlist denied (\S+):\s*(.+?)\s*$"
)

# All four event kinds use a single JSON suffix shape except Allowlist-
# denied. The bare-token prefix anchors which kind we're parsing.
_TAGGED_JSON_RE = re.compile(r"(SIGNIN|AUTH_FAIL|WOULD_DENY)\s+(\{.*?\})\s*$",
                             re.DOTALL)


def _parse_auth_event(message: str) -> Optional[dict]:
    """Return a normalised dict for one CloudWatch event message, or None
    if the line is not one of our known auth/security shapes.

    Output schema (any missing field is left as empty string):
      kind:      event class — 'signin' | 'auth_fail' | 'would_deny' |
                 'allowlist_denied'. Used to bucket rows in the report.
      auth_kind: credential mechanism — only populated for AUTH_FAIL:
                 'google' | 'session' | 'legacy' | 'none'. The AUTH_FAIL
                 log line's own JSON also calls this field `kind`, so we
                 rename here to avoid colliding with the outer `kind`.
      reason:    short string from the underlying line
      identity:  verified email (signin / allowlist_denied / would_deny);
                 blank when only an attempted username is known (auth_fail)
      attempted: username or token-attempt label when identity is unknown
      method, bucket, key: present for would_deny; otherwise blank
      ip, user_agent: from request context (signin / auth_fail / would_deny)
    """
    m = _TAGGED_JSON_RE.search(message)
    if m:
        tag, blob = m.group(1), m.group(2)
        try:
            p = json.loads(blob)
        except json.JSONDecodeError:
            return None
        if tag == "SIGNIN":
            return {
                "kind": "signin",
                "auth_kind": "",
                "reason": "",
                "identity": p.get("identity", "") or "",
                "attempted": "",
                "method": "", "bucket": "", "key": "",
                "ip": p.get("ip", "") or "",
                "user_agent": p.get("user_agent", "") or "",
            }
        if tag == "AUTH_FAIL":
            return {
                "kind": "auth_fail",
                "auth_kind": p.get("kind", "") or "",
                "reason": p.get("reason", "") or "",
                "identity": "",
                "attempted": p.get("attempted", "") or "",
                "method": "", "bucket": "", "key": "",
                "ip": p.get("ip", "") or "",
                "user_agent": p.get("user_agent", "") or "",
            }
        if tag == "WOULD_DENY":
            return {
                "kind": "would_deny",
                "auth_kind": "",
                "reason": p.get("reason", "") or "",
                "identity": p.get("identity", "") or "",
                "attempted": "",
                "method": p.get("method", "") or "",
                "bucket": p.get("bucket", "") or "",
                "key": p.get("key", "") or "",
                "ip": p.get("ip", "") or "",
                "user_agent": p.get("user_agent", "") or "",
            }
    m2 = _ALLOWLIST_DENIED_RE.search(message)
    if m2:
        return {
            "kind": "allowlist_denied",
            "auth_kind": "",
            "reason": m2.group(2).strip(),
            "identity": m2.group(1).strip(),
            "attempted": "",
            "method": "", "bucket": "", "key": "",
            "ip": "", "user_agent": "",
        }
    return None


def fetch_lambda_auth_events(day: str, *, no_cache: bool = False) -> pd.DataFrame:
    """SIGNIN / AUTH_FAIL / WOULD_DENY / Allowlist-denied events from the
    Lambda log group for the given UTC day. Cached to ~/Data/AwsUsage/warnings/.

    Past UTC days are written once; today is always re-fetched.
    """
    cache_file = _cache_path("warnings", day, "parquet")
    if not no_cache and cache_file.exists() and _is_complete_past_day(day):
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass

    start_dt = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    end_dt = start_dt + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    logs = boto3.client("logs")
    rows: list[dict] = []
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = dict(
            logGroupName=LAMBDA_LOG_GROUP,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern=_AUTH_FILTER,
            limit=10000,
        )
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            LOG.logger.warning(f"Log group {LAMBDA_LOG_GROUP} not found")
            break
        for ev in resp.get("events", []):
            parsed = _parse_auth_event(ev.get("message", ""))
            if parsed is None:
                continue
            parsed["time"] = datetime.fromtimestamp(
                ev["timestamp"] / 1000, tz=timezone.utc)
            rows.append(parsed)
        next_token = resp.get("nextToken")
        if not next_token:
            break

    cols = ["time", "kind", "auth_kind", "reason", "identity", "attempted",
            "method", "bucket", "key", "ip", "user_agent"]
    df = pd.DataFrame(rows, columns=cols)
    if _is_complete_past_day(day):
        df.to_parquet(cache_file)
        LOG.logger.info(f"WARNINGS {day}: {len(df)} rows -> {cache_file}")
    return df


# --- Allowlist + CloudFront status (not day-windowed) -----------------------

def fetch_allowlist() -> pd.DataFrame:
    """Current snapshot of s3://regattaanalysis/auth/allowlist.json. No
    cache — the file is small (a few KB), and there is no per-day variant
    to compute. Columns: email, expires_at, added_at, note, days_since_added,
    days_until_expiry. Missing timestamps surface as NaT."""
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=BUCKET_DATA, Key="auth/allowlist.json")
    raw = json.loads(obj["Body"].read().decode("utf-8"))
    rows = []
    now = datetime.now(timezone.utc)
    for entry in raw:
        added = entry.get("added_at")
        expires = entry.get("expires_at")
        added_dt = pd.to_datetime(added, utc=True, errors="coerce") if added else pd.NaT
        expires_dt = pd.to_datetime(expires, utc=True, errors="coerce") if expires else pd.NaT
        rows.append({
            "email": entry.get("email", "") or "",
            "added_at": added_dt,
            "expires_at": expires_dt,
            "note": entry.get("note", "") or "",
            "days_since_added": (
                (now - added_dt).total_seconds() / 86400.0
                if pd.notna(added_dt) else float("nan")
            ),
            "days_until_expiry": (
                (expires_dt - now).total_seconds() / 86400.0
                if pd.notna(expires_dt) else float("nan")
            ),
        })
    return pd.DataFrame(rows, columns=[
        "email", "added_at", "expires_at", "note",
        "days_since_added", "days_until_expiry",
    ])


def fetch_cloudfront_status() -> dict:
    """Killswitch indicator for the regattaview.net CloudFront distribution.

    Returns {"enabled": bool, "domain": str, "last_modified": datetime}.
    """
    cf = boto3.client("cloudfront")
    resp = cf.get_distribution(Id=CF_DISTRIBUTION)
    d = resp.get("Distribution", {})
    cfg = d.get("DistributionConfig", {})
    aliases = (cfg.get("Aliases") or {}).get("Items") or []
    domain = aliases[0] if aliases else d.get("DomainName", "")
    return {
        "enabled": bool(cfg.get("Enabled", False)),
        "domain": domain,
        "last_modified": d.get("LastModifiedTime"),
        "id": CF_DISTRIBUTION,
    }


# --- Identity attribution ----------------------------------------------------

def attribute_identities(
    presign: pd.DataFrame,
    access: pd.DataFrame,
    *,
    kind: str,                          # 's3' or 'cf'
    ip_match: bool = True,
) -> pd.DataFrame:
    """
    Tag each access-log row with the most-recent matching PRESIGN's identity.

    Match: same key (S3 logs) or same uri-stem (CF logs), access.time within
    [presign.time, presign.time + ttl_s + 60s], and (if ip_match) same ip.

    Returns access with an added 'identity' column ('' when no match).
    """
    if access.empty:
        out = access.copy()
        out["identity"] = pd.Series([], dtype=object)
        return out
    if presign.empty:
        out = access.copy()
        out["identity"] = ""
        return out

    if kind == "s3":
        access_key = access["key"].astype(str)
        access_ip = access["ip"].astype(str) if ip_match else None
    elif kind == "cf":
        access_key = access["uri"].fillna("").map(lambda u: u.lstrip("/"))
        access_ip = access["client_ip"].astype(str) if ip_match else None
    else:
        raise ValueError(f"kind must be 's3' or 'cf', got {kind!r}")

    # Build key-bucketed PRESIGN index for cheap lookup.
    presign_sorted = presign.sort_values("time").reset_index(drop=True)
    by_key: dict[str, list[int]] = defaultdict(list)
    for i, k in enumerate(presign_sorted["key"]):
        by_key[k].append(i)

    p_times = presign_sorted["time"].to_numpy()
    p_ttls = presign_sorted["ttl_s"].to_numpy()
    p_ips = presign_sorted["ip"].astype(str).to_numpy()
    p_ids = presign_sorted["identity"].astype(str).to_numpy()

    out_ids: list[str] = []
    a_times = access["time"].to_numpy()
    for i in range(len(access)):
        k = access_key.iloc[i]
        candidates = by_key.get(k)
        if not candidates:
            out_ids.append("")
            continue
        a_t = a_times[i]
        match_id = ""
        # Walk backwards: most recent presign match wins
        for j in reversed(candidates):
            p_t = p_times[j]
            ttl = int(p_ttls[j]) if p_ttls[j] else 3600
            # access must be in [p_t, p_t + ttl + 60s]
            if a_t < p_t:
                continue
            if (a_t - p_t).total_seconds() > ttl + 60:
                continue
            if ip_match and access_ip is not None and p_ips[j] != access_ip.iloc[i]:
                continue
            match_id = p_ids[j]
            break
        out_ids.append(match_id)

    out = access.copy()
    out["identity"] = out_ids
    return out


# --- Aggregation -------------------------------------------------------------

def _date_iter(start: str, end: str) -> Iterable[str]:
    """Inclusive on both ends."""
    a = date.fromisoformat(start)
    b = date.fromisoformat(end)
    d = a
    while d <= b:
        yield d.isoformat()
        d += timedelta(days=1)


@dataclass
class Window:
    start: str          # local YYYY-MM-DD inclusive
    end: str            # local YYYY-MM-DD inclusive

    @property
    def ce_end(self) -> str:
        """Cost Explorer's End is exclusive — bump by one day."""
        return (date.fromisoformat(self.end) + timedelta(days=1)).isoformat()

    @property
    def days(self) -> list[str]:
        """Local report days."""
        return list(_date_iter(self.start, self.end))

    @property
    def local_start(self) -> datetime:
        return datetime.combine(
            date.fromisoformat(self.start),
            datetime.min.time(),
            tzinfo=REPORT_TZ,
        )

    @property
    def local_end_exclusive(self) -> datetime:
        return datetime.combine(
            date.fromisoformat(self.end) + timedelta(days=1),
            datetime.min.time(),
            tzinfo=REPORT_TZ,
        )

    @property
    def utc_days(self) -> list[str]:
        """UTC log-object days needed to cover the local report window."""
        start_utc = self.local_start.astimezone(timezone.utc).date()
        end_utc = (self.local_end_exclusive - timedelta(microseconds=1)).astimezone(timezone.utc).date()
        return list(_date_iter(start_utc.isoformat(), end_utc.isoformat()))

    @property
    def utc_presign_days(self) -> list[str]:
        """UTC CloudWatch days needed for attribution, including prior TTLs."""
        # A URL signed just before local midnight can be used after midnight.
        # Include the prior 25h so legacy 24h signed URLs still attribute.
        start_utc = (self.local_start - timedelta(hours=25)).astimezone(timezone.utc).date()
        end_utc = (self.local_end_exclusive - timedelta(microseconds=1)).astimezone(timezone.utc).date()
        return list(_date_iter(start_utc.isoformat(), end_utc.isoformat()))


def _filter_time_window(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    if df.empty or "time" not in df.columns:
        return df
    t = pd.to_datetime(df["time"], utc=True)
    mask = (t >= start.astimezone(timezone.utc)) & (t < end.astimezone(timezone.utc))
    return df.loc[mask].copy()


def _filter_to_report_window(df: pd.DataFrame, window: Window) -> pd.DataFrame:
    return _filter_time_window(df, window.local_start, window.local_end_exclusive)


def _local_day_strings(times: pd.Series) -> pd.Series:
    return pd.to_datetime(times, utc=True).dt.tz_convert(REPORT_TZ).dt.date.astype(str)


def build_report(
    window: Window,
    *,
    own_ip: Optional[str] = OWN_IP_DEFAULT,
    no_cache: bool = False,
) -> dict:
    """
    Pull all sources for the window, attribute identities, summarize.
    Returns a structured dict; render_markdown turns it into the report.
    """
    LOG.logger.info(f"Building report for {window.start}..{window.end} (own_ip={own_ip!r})")

    # 1. Cost Explorer
    ce_df = fetch_cost_explorer(window.start, window.ce_end, no_cache=no_cache)

    # 2..4. Per-day log fetches
    s3_frames: list[pd.DataFrame] = []
    cf_frames: list[pd.DataFrame] = []
    pre_frames: list[pd.DataFrame] = []
    warn_frames: list[pd.DataFrame] = []
    for d in window.utc_days:
        try:
            s3_frames.append(fetch_s3_access_logs(d, no_cache=no_cache))
        except Exception as e:
            LOG.logger.warning(f"S3 logs {d}: fetch failed: {e}")
        try:
            cf_frames.append(fetch_cf_access_logs(d, no_cache=no_cache))
        except Exception as e:
            LOG.logger.warning(f"CF logs {d}: fetch failed: {e}")
        try:
            warn_frames.append(fetch_lambda_auth_events(d, no_cache=no_cache))
        except Exception as e:
            LOG.logger.warning(f"Warnings {d}: fetch failed: {e}")
    for d in window.utc_presign_days:
        try:
            pre_frames.append(fetch_presign_logs(d, no_cache=no_cache))
        except Exception as e:
            LOG.logger.warning(f"PRESIGN {d}: fetch failed: {e}")

    def _safe_concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        nonempty = [f for f in frames if not f.empty]
        if not nonempty:
            return pd.DataFrame()
        return pd.concat(nonempty, ignore_index=True)

    s3_df = _safe_concat(s3_frames)
    cf_df = _safe_concat(cf_frames)
    pre_all_df = _safe_concat(pre_frames)
    warn_all_df = _safe_concat(warn_frames)

    s3_df = _filter_to_report_window(s3_df, window)
    cf_df = _filter_to_report_window(cf_df, window)
    pre_df = _filter_to_report_window(pre_all_df, window)
    warn_df = _filter_to_report_window(warn_all_df, window)
    pre_attr_df = _filter_time_window(
        pre_all_df,
        window.local_start - timedelta(hours=25),
        window.local_end_exclusive,
    )

    # Attribute (CF first because real client IPs live there)
    if not cf_df.empty:
        cf_df = attribute_identities(pre_attr_df, cf_df, kind="cf")
    if not s3_df.empty:
        s3_df = attribute_identities(pre_attr_df, s3_df, kind="s3")

    out = _summarize(window, ce_df, s3_df, cf_df, pre_df, own_ip=own_ip)

    # Security section — best-effort. A single live AWS hiccup on
    # allowlist/CF fetch must not break the cost-side report, which is
    # already paid-for at this point.
    try:
        allowlist_df = fetch_allowlist()
    except Exception as e:
        LOG.logger.warning(f"Allowlist fetch failed: {e}")
        allowlist_df = pd.DataFrame()
    try:
        cf_status = fetch_cloudfront_status()
    except Exception as e:
        LOG.logger.warning(f"CF status fetch failed: {e}")
        cf_status = {"enabled": None, "domain": "", "last_modified": None,
                     "id": CF_DISTRIBUTION, "error": str(e)}

    out["security"] = _build_security_section(
        window,
        warnings=warn_df,
        presign_window=pre_df,
        presign_all=pre_all_df,
        s3=s3_df,
        cf=cf_df,
        allowlist=allowlist_df,
        cf_status=cf_status,
    )
    out["sources"]["warning_rows"] = int(len(warn_df))
    return out


# Substrings that mark a user-agent as non-browser / bot-ish. Conservative
# — false positives are noisy, so the matcher requires one of these tokens
# rather than guessing from absence of "Mozilla". `python-requests` covers
# the iOS Uploader (which uses URLSession with a custom UA) only when the
# UA isn't overridden; in practice peergynt traffic uses Swift's default
# UA and falls through the filter — counted under service identities, not
# here. curl/wget are the canonical "someone is poking" signal.
_BOT_UA_RE = re.compile(
    r"(?i)(bot|crawl|spider|wget|curl|python-requests|scrapy|httpx|libwww)"
)


def _historical_presign_baseline(window: Window) -> pd.DataFrame:
    """Load PRESIGN cache parquets for every complete UTC day *before* the
    window's local start (minus a small buffer for tz boundary). Returns a
    dataframe with identity + ip columns only (de-duplicated). Empty
    dataframe if no historical cache exists yet."""
    presign_dir = CACHE_ROOT / "presign"
    if not presign_dir.exists():
        return pd.DataFrame(columns=["identity", "ip"])

    cutoff_utc = (window.local_start - timedelta(hours=25)
                  ).astimezone(timezone.utc).date().isoformat()
    rows: list[pd.DataFrame] = []
    for f in sorted(presign_dir.glob("*.parquet")):
        day = f.stem
        if day >= cutoff_utc:
            continue
        try:
            df = pd.read_parquet(f, columns=["identity", "ip"])
        except Exception:
            continue
        if not df.empty:
            rows.append(df)
    if not rows:
        return pd.DataFrame(columns=["identity", "ip"])
    base = pd.concat(rows, ignore_index=True).drop_duplicates()
    base = base[base["identity"].astype(str) != ""]
    return base


def _build_security_section(
    window: Window,
    *,
    warnings: pd.DataFrame,
    presign_window: pd.DataFrame,
    presign_all: pd.DataFrame,
    s3: pd.DataFrame,
    cf: pd.DataFrame,
    allowlist: pd.DataFrame,
    cf_status: dict,
) -> dict:
    """Assemble the security / activity slice of the report.

    Most of the work is just bucketing the warnings dataframe by `kind`
    and computing simple set-differences against the PRESIGN baseline.
    No anomaly thresholds beyond "presence" — we're producing a readable
    triage table, not an alerting pipeline. The §5 budget and Cost
    Anomaly Detection still own cost-alarm escalation.
    """
    sec: dict[str, Any] = {}

    # 1. Warning buckets (already filtered to window in caller).
    by_kind: dict[str, pd.DataFrame] = {}
    if not warnings.empty:
        for k in ("signin", "auth_fail", "would_deny", "allowlist_denied"):
            sub = warnings[warnings["kind"] == k].sort_values("time")
            if not sub.empty:
                by_kind[k] = sub.reset_index(drop=True)
    sec["counts"] = {k: int(len(by_kind.get(k, []))) for k in
                     ("signin", "auth_fail", "would_deny", "allowlist_denied")}
    sec["warnings_by_kind"] = {
        k: df[[c for c in df.columns if c != "kind"]].to_dict("records")
        for k, df in by_kind.items()
    }

    # 2. New identities / new (identity, ip) pairs vs. historical PRESIGN.
    baseline = _historical_presign_baseline(window)
    baseline_idents: set[str] = set(baseline["identity"].astype(str)) if not baseline.empty else set()
    baseline_pairs: set[tuple[str, str]] = (
        set(map(tuple, baseline[["identity", "ip"]].astype(str).itertuples(index=False, name=None)))
        if not baseline.empty else set()
    )
    if presign_window.empty:
        sec["new_identities"] = []
        sec["new_ips_per_identity"] = []
    else:
        cur_idents = (presign_window[["identity", "time"]]
                      .dropna(subset=["identity"])
                      .copy())
        cur_idents = cur_idents[cur_idents["identity"].astype(str) != ""]
        first_seen = (cur_idents.sort_values("time")
                      .groupby("identity").first().reset_index())
        sec["new_identities"] = [
            {"identity": r["identity"],
             "first_seen_in_window": pd.to_datetime(r["time"], utc=True).isoformat()}
            for _, r in first_seen.iterrows()
            if r["identity"] not in baseline_idents
        ]

        cur_pairs = (presign_window[["identity", "ip", "time"]]
                     .dropna(subset=["identity", "ip"]).copy())
        cur_pairs = cur_pairs[(cur_pairs["identity"].astype(str) != "")
                              & (cur_pairs["ip"].astype(str) != "")]
        first_pair = (cur_pairs.sort_values("time")
                      .groupby(["identity", "ip"]).first().reset_index())
        sec["new_ips_per_identity"] = [
            {"identity": r["identity"], "ip": r["ip"],
             "first_seen_in_window": pd.to_datetime(r["time"], utc=True).isoformat()}
            for _, r in first_pair.iterrows()
            if (r["identity"], r["ip"]) not in baseline_pairs
            and r["identity"] not in {x["identity"] for x in sec["new_identities"]}
        ]

    # 3. Anonymous / non-Lambda direct-to-S3 fetches. The S3 access log's
    # `requester` field is "-" for any anonymous request; after the 2026-04-25
    # public-access lockdown those should be 403s, but record anything that
    # slips through. We also flag requesters that aren't our Lambda's IAM role
    # — currently we can't enumerate that string without an extra IAM call, so
    # heuristic: requester containing "AROA" or "AIDA" prefixes is IAM; "-"
    # or an opaque arn is anonymous-ish.
    if s3.empty or "requester" not in s3.columns:
        sec["anonymous_s3_hits"] = []
    else:
        anon_mask = s3["requester"].astype(str).str.startswith("-")
        anon = s3[anon_mask][["time", "ip", "key", "status", "bytes_sent",
                              "user_agent"] if "user_agent" in s3.columns
                             else ["time", "ip", "key", "status", "bytes_sent"]]
        sec["anonymous_s3_hits"] = anon.sort_values("time").to_dict("records")

    # 4. Bot / non-browser user-agents. Aggregate over PRESIGN + CF. Service
    # identities (peergynt) are excluded — their UA is expected to be
    # non-browser-like.
    ua_rows = []
    if not presign_window.empty and "user_agent" in presign_window.columns:
        for _, r in presign_window.iterrows():
            ua = str(r.get("user_agent") or "")
            ident = str(r.get("identity") or "")
            if ident in SERVICE_IDENTITIES:
                continue
            if _BOT_UA_RE.search(ua):
                ua_rows.append({"source": "presign", "identity": ident, "ua": ua})
    if not cf.empty and "user_agent" in cf.columns:
        for _, r in cf.iterrows():
            ua = str(r.get("user_agent") or "")
            ident = str(r.get("identity") or "")
            if _BOT_UA_RE.search(ua):
                ua_rows.append({"source": "cf", "identity": ident, "ua": ua})
    if ua_rows:
        ua_df = pd.DataFrame(ua_rows)
        agg = (ua_df.groupby(["source", "identity", "ua"])
                     .size().reset_index(name="count")
                     .sort_values("count", ascending=False))
        sec["bot_user_agents"] = agg.to_dict("records")
    else:
        sec["bot_user_agents"] = []

    # 5. Allowlist snapshot — sort by added_at desc so recent additions are
    # at the top of the rendered list.
    if allowlist.empty:
        sec["allowlist"] = []
        sec["allowlist_recent"] = []
        sec["allowlist_expiring"] = []
    else:
        al = allowlist.copy()
        al = al.sort_values("added_at", ascending=False, na_position="last")
        sec["allowlist"] = [
            {
                "email": r["email"],
                "added_at": (pd.to_datetime(r["added_at"], utc=True).isoformat()
                             if pd.notna(r["added_at"]) else None),
                "expires_at": (pd.to_datetime(r["expires_at"], utc=True).isoformat()
                               if pd.notna(r["expires_at"]) else None),
                "note": r["note"],
                "days_since_added": (round(float(r["days_since_added"]), 1)
                                     if pd.notna(r["days_since_added"]) else None),
                "days_until_expiry": (round(float(r["days_until_expiry"]), 1)
                                      if pd.notna(r["days_until_expiry"]) else None),
            }
            for _, r in al.iterrows()
        ]
        sec["allowlist_recent"] = [
            e for e in sec["allowlist"]
            if e["days_since_added"] is not None and e["days_since_added"] <= 7.0
        ]
        sec["allowlist_expiring"] = [
            e for e in sec["allowlist"]
            if e["days_until_expiry"] is not None and 0 <= e["days_until_expiry"] <= 14.0
        ]

    # 6. CloudFront status (killswitch indicator)
    sec["cloudfront"] = {
        **cf_status,
        "last_modified": (pd.to_datetime(cf_status["last_modified"], utc=True).isoformat()
                          if cf_status.get("last_modified") is not None else None),
    }

    return sec


def _summarize(
    window: Window,
    ce: pd.DataFrame,
    s3: pd.DataFrame,
    cf: pd.DataFrame,
    pre: pd.DataFrame,
    *,
    own_ip: Optional[str],
) -> dict:
    out: dict[str, Any] = {
        "window": {"start": window.start, "end": window.end, "days": len(window.days)},
        "sources": {
            "cost_rows": int(len(ce)),
            "s3_rows": int(len(s3)),
            "cf_rows": int(len(cf)),
            "presign_rows": int(len(pre)),
        },
    }

    # --- Cost ---------------------------------------------------------------
    if ce.empty:
        out["total_cost"] = 0.0
        out["cost_by_day"] = {}
        out["cost_by_service"] = {}
        out["cost_by_day_service"] = {}
        out["cost_by_day_usagetype"] = {}
    else:
        out["total_cost"] = round(float(ce["cost"].sum()), 4)
        out["cost_by_day"] = {
            d: round(float(g["cost"].sum()), 4)
            for d, g in ce.groupby("day")
        }
        out["cost_by_service"] = {
            s: round(float(g["cost"].sum()), 4)
            for s, g in ce.groupby("service")
            if g["cost"].sum() > 0.0001
        }
        out["cost_by_day_service"] = {
            d: {s: round(float(g["cost"].sum()), 4)
                for s, g in dg.groupby("service")
                if g["cost"].sum() > 0.0001}
            for d, dg in ce.groupby("day")
        }
        # Day x usage_type — keep just S3 (the cost-anomaly target)
        s3_ce = ce[ce["service"] == "Amazon Simple Storage Service"]
        out["cost_by_day_usagetype"] = {
            d: {ut: round(float(g["cost"].sum()), 4)
                for ut, g in dg.groupby("usage_type")
                if g["cost"].sum() > 0.0001}
            for d, dg in s3_ce.groupby("day")
        }

    # --- S3 access summary --------------------------------------------------
    if s3.empty:
        out["s3"] = {"empty": True}
    else:
        ok = s3["status"].isin(["200", "206"])
        s3_ok = s3[ok]
        bytes_total = int(s3_ok["bytes_sent"].sum())
        copy = s3[s3["op"] == "REST.COPY.OBJECT"]
        copy_get = s3[s3["op"] == "REST.COPY.OBJECT_GET"]
        out["s3"] = {
            "rows": int(len(s3)),
            "ok_rows": int(len(s3_ok)),
            "bytes_sent_total": bytes_total,
            "by_op": {
                op: {
                    "count": int(len(g)),
                    "bytes": int(g["bytes_sent"].sum()),
                }
                for op, g in s3.groupby("op")
            },
            "by_status": s3["status"].value_counts().to_dict(),
            "by_content_class_bytes": {
                cls: int(g["bytes_sent"].sum())
                for cls, g in s3_ok.groupby("content_class")
                if g["bytes_sent"].sum() > 0
            },
            "copy_object": {
                "count": int(len(copy)),
                "total_object_size": int(copy["object_size"].sum()) if len(copy) else 0,
                "by_day": _local_day_strings(copy["time"]).value_counts().to_dict() if len(copy) else {},
            },
            "copy_object_get": {
                "count": int(len(copy_get)),
                "total_object_size": int(copy_get["object_size"].sum()) if len(copy_get) else 0,
            },
            # Per-day S3 bytes for anomaly detection
            "bytes_by_day": s3_ok.groupby(_local_day_strings(s3_ok["time"]))["bytes_sent"].sum().astype(int).to_dict(),
        }

    # --- CF access summary --------------------------------------------------
    if cf.empty:
        out["cf"] = {"empty": True}
    else:
        ok = cf["status"].isin([200, 206])
        cf_ok = cf[ok]
        # Strip your-own-IP (CF logs hold real client IPs, not edges)
        if own_ip:
            cf_user = cf_ok[cf_ok["client_ip"] != own_ip]
        else:
            cf_user = cf_ok

        # Scanner traffic = 4xx with 'wp-' / '.php' / known WP probe patterns
        scanner_pat = re.compile(r"(wp-includes|wp-admin|xmlrpc\.php|wlwmanifest|/\.env|wordpress)", re.I)
        cf_scan = cf[cf["uri"].fillna("").map(lambda u: bool(scanner_pat.search(u)))]

        out["cf"] = {
            "rows": int(len(cf)),
            "ok_rows": int(len(cf_ok)),
            "bytes_total": int(cf_ok["bytes"].sum()),
            "user_rows": int(len(cf_user)),
            "user_bytes": int(cf_user["bytes"].sum()),
            "unique_user_ips": int(cf_user["client_ip"].nunique()) if len(cf_user) else 0,
            "by_status": {int(k): int(v) for k, v in cf["status"].value_counts().items()},
            "by_edge_result": cf["edge_result"].value_counts().head(8).to_dict(),
            "by_content_class_user_bytes": {
                cls: int(g["bytes"].sum())
                for cls, g in cf_user.groupby("content_class")
                if g["bytes"].sum() > 0
            },
            "scanner": {
                "rows": int(len(cf_scan)),
                "unique_ips": int(cf_scan["client_ip"].nunique()) if len(cf_scan) else 0,
                "top_ips": cf_scan["client_ip"].value_counts().head(5).to_dict() if len(cf_scan) else {},
                "top_uris": cf_scan["uri"].value_counts().head(5).to_dict() if len(cf_scan) else {},
            },
            "user_bytes_by_day": cf_user.groupby(_local_day_strings(cf_user["time"]))["bytes"].sum().astype(int).to_dict(),
        }

    # --- User attribution (preferred source: CF; fallback: S3 direct) -------
    by_user: dict[str, dict[str, Any]] = {}
    if not cf.empty and "identity" in cf.columns:
        for ident, g in cf[(cf["identity"].astype(str) != "") & cf["status"].isin([200, 206])].groupby("identity"):
            top_files = (g.groupby("uri")["bytes"].sum()
                         .sort_values(ascending=False).head(5)
                         .astype(int).to_dict())
            by_user.setdefault(ident, {
                "gb": 0.0,
                "gets": 0,
                "top_files": {},
                "kind": _identity_kind(ident),
            })
            by_user[ident]["gb"] += round(float(g["bytes"].sum()) / 1e9, 4)
            by_user[ident]["gets"] += int(len(g))
            by_user[ident]["top_files"] = top_files
    # Fold in direct-S3 (presign-resolved) traffic that didn't go through CF
    if not s3.empty and "identity" in s3.columns:
        s3_user = s3[(s3["identity"].astype(str) != "") &
                     s3["status"].isin(["200", "206"]) &
                     (s3["op"] == "REST.GET.OBJECT")]
        for ident, g in s3_user.groupby("identity"):
            d = by_user.setdefault(ident, {
                "gb": 0.0,
                "gets": 0,
                "top_files": {},
                "kind": _identity_kind(ident),
                "ops_label": "GETs",
            })
            d["gb"] = round(d["gb"] + float(g["bytes_sent"].sum()) / 1e9, 4)
            d["gets"] += int(len(g))
            # Don't override top_files if CF set them (CF is canonical for users)
            if not d["top_files"]:
                d["top_files"] = (g.groupby("key")["bytes_sent"].sum()
                                  .sort_values(ascending=False).head(5)
                                  .astype(int).to_dict())

    # IAM operator attribution. race_upload.py / aws_sync.sh use s5cmd
    # with an IAM user key, bypassing the Lambda — no PRESIGN row, no
    # human identity. The requester field in the S3 access log carries
    # the IAM principal ARN. Parse user/* principals and surface them as
    # their own rows so the upload footprint is visible. Read bytes from
    # `object_size` on the multipart-complete op (REST.POST.UPLOAD) and
    # the single-PUT op (REST.PUT.OBJECT) — `bytes_sent` on these is the
    # server response (tiny ETag / empty), not the upload size.
    if not s3.empty and "requester" in s3.columns:
        write_ops = ("REST.PUT.OBJECT", "REST.POST.UPLOAD")
        s3_writes = s3[s3["op"].isin(write_ops)].copy()
        s3_writes["operator"] = (s3_writes["requester"].astype(str)
                                 .map(_iam_principal_name))
        s3_writes = s3_writes[s3_writes["operator"].notna()]
        if not s3_writes.empty:
            for op_name, g in s3_writes.groupby("operator"):
                sizes = g.groupby("key")["object_size"].max().astype(float)
                total_bytes = float(sizes.sum())
                top_files = (sizes.sort_values(ascending=False).head(5)
                                  .astype(int).to_dict())
                d = by_user.setdefault(op_name, {
                    "gb": 0.0,
                    "gets": 0,
                    "top_files": {},
                    "kind": "operator",
                    "ops_label": "PUTs",
                })
                d["kind"] = "operator"
                d["ops_label"] = "PUTs"
                d["gb"] = round(d["gb"] + total_bytes / 1e9, 4)
                d["gets"] += int(g["key"].nunique())
                d["top_files"] = top_files

    out["users"] = dict(sorted(by_user.items(), key=lambda kv: -kv[1]["gb"]))
    out["distinct_users"] = sum(1 for info in by_user.values() if info.get("kind") == "user")
    out["service_identities"] = sum(1 for info in by_user.values() if info.get("kind") == "service")
    out["operator_identities"] = sum(1 for info in by_user.values() if info.get("kind") == "operator")

    # --- Anomalies ----------------------------------------------------------
    out["anomalies"] = _flag_anomalies(out)

    # --- Health -------------------------------------------------------------
    health: dict[str, Any] = {}
    if not s3.empty:
        bad = s3[~s3["status"].isin(["200", "206", "204", "304"])]
        health["s3_4xx_5xx"] = int(len(bad))
    if not cf.empty:
        bad = cf[~cf["status"].isin([200, 206, 204, 301, 302, 304])]
        health["cf_4xx_5xx"] = int(len(bad))
        ok_cf = cf[cf["status"].isin([200, 206])]
        if len(ok_cf):
            health["cf_ttfb_p50"] = round(float(ok_cf["ttfb"].quantile(0.5)), 4)
            health["cf_ttfb_p95"] = round(float(ok_cf["ttfb"].quantile(0.95)), 4)
    out["health"] = health

    return out


def _flag_anomalies(report: dict) -> list[dict]:
    out: list[dict] = []

    # Cost: any day's cost > median + 3·MAD
    cbd = report.get("cost_by_day", {})
    if len(cbd) >= 3:
        vals = list(cbd.values())
        med = pd.Series(vals).median()
        mad = (pd.Series(vals) - med).abs().median() or 0.01
        for d, v in cbd.items():
            if v > med + 3 * mad and v > 5:
                out.append({
                    "severity": "high",
                    "kind": "cost-day",
                    "msg": f"{d}: ${v:.2f} (median ${med:.2f}, threshold ${med + 3*mad:.2f})",
                })

    # S3 usage-type: any (day, usage_type) > 10× the median for that usage_type
    cdu = report.get("cost_by_day_usagetype", {})
    by_ut: dict[str, list[float]] = defaultdict(list)
    for day, m in cdu.items():
        for ut, v in m.items():
            by_ut[ut].append(v)
    for ut, vals in by_ut.items():
        if len(vals) < 3:
            continue
        med = pd.Series(vals).median()
        if med <= 0:
            continue
        for day, m in cdu.items():
            if ut in m and m[ut] > 10 * med and m[ut] > 1:
                out.append({
                    "severity": "high",
                    "kind": "usage-type-spike",
                    "msg": f"{day} S3 {ut}: ${m[ut]:.2f} (median ${med:.4f}, 10× threshold ${10*med:.4f})",
                })

    # Server-side COPY storm — likely bulk migration
    s3sec = report.get("s3", {})
    copy = s3sec.get("copy_object", {})
    if copy and copy.get("count", 0) > 100:
        gb = copy.get("total_object_size", 0) / 1e9
        out.append({
            "severity": "info",
            "kind": "server-side-copy",
            "msg": f"REST.COPY.OBJECT count={copy['count']} ({gb:.1f} GB total) — bulk migration; "
                   f"IA-class sources pay $0.01/GB retrieval",
        })

    # Single (identity, key) > 25% of attributed user bytes (CF + S3 direct).
    # Only meaningful when there are multiple users — otherwise concentration
    # is trivially 100%.
    users = _human_users(report)
    user_total_bytes = int(sum(u.get("gb", 0.0) for u in users.values()) * 1e9)
    if len(users) >= 2 and user_total_bytes > 1_000_000_000:
        for ident, info in users.items():
            for fn, b in info.get("top_files", {}).items():
                if b > 0.25 * user_total_bytes:
                    out.append({
                        "severity": "info",
                        "kind": "concentrated-traffic",
                        "msg": f"{ident} fetched {b/1e9:.2f} GB of {fn} "
                               f"({100*b/user_total_bytes:.0f}% of all user bytes)",
                    })

    # Scanner activity (informational)
    scan = report.get("cf", {}).get("scanner", {})
    if scan and scan.get("rows", 0) > 50:
        out.append({
            "severity": "info",
            "kind": "scanner-traffic",
            "msg": f"{scan['rows']} scanner probe requests from {scan['unique_ips']} IPs "
                   f"(WordPress / .env / wp-includes patterns) — all should 4xx",
        })

    return out


# --- Markdown renderer -------------------------------------------------------

_SERVICE_SHORT = {
    "Amazon Simple Storage Service": "S3",
    "Amazon CloudFront": "CloudFront",
    "Amazon Route 53": "Route 53",
    "Amazon Registrar": "Registrar",
    "Amazon EC2 Container Registry (ECR)": "ECR",
    "AWS Lambda": "Lambda",
    "AWS Cost Explorer": "CostExplorer",
    "AWS Key Management Service": "KMS",
    "AWS Secrets Manager": "Secrets",
    "AWS Certificate Manager": "ACM",
    "AmazonCloudWatch": "CloudWatch",
    "Amazon Simple Notification Service": "SNS",
    "Amazon Simple Email Service": "SES",
    "EC2 - Other": "EC2-Other",
    "Tax": "Tax",
}


def _short_service(name: str) -> str:
    if name in _SERVICE_SHORT:
        return _SERVICE_SHORT[name]
    return name.replace("Amazon ", "").replace("AWS ", "")[:18]


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _week_buckets(days: list[str]) -> list[tuple[str, list[str]]]:
    """
    Group YYYY-MM-DD strings into US-week buckets (Sun–Sat) and return
    [(label, [days_in_bucket]), ...] sorted oldest→newest. Label is the
    actual span of data in the bucket, e.g. 'Apr 26 – May 02' or just
    'Apr 28' if the bucket holds a single day.
    """
    if not days:
        return []
    by_period: dict[pd.Period, list[str]] = defaultdict(list)
    for d in days:
        p = pd.Period(d, freq="W-SAT")        # week ends Saturday
        by_period[p].append(d)
    out: list[tuple[str, list[str]]] = []
    for p in sorted(by_period.keys()):
        ds = sorted(by_period[p])
        if len(ds) == 1:
            label = pd.Timestamp(ds[0]).strftime("%b %d")
        else:
            label = f"{pd.Timestamp(ds[0]).strftime('%b %d')} – {pd.Timestamp(ds[-1]).strftime('%b %d')}"
        out.append((label, ds))
    return out


def _fmt_gb(b: int) -> str:
    if b >= 1e9:
        return f"{b/1e9:.2f} GB"
    if b >= 1e6:
        return f"{b/1e6:.1f} MB"
    if b >= 1e3:
        return f"{b/1e3:.1f} KB"
    return f"{b} B"


def _md_cell(value: Any) -> str:
    """Keep generated Markdown table cells single-line and pipe-safe."""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return re.sub(r"\s+", " ", text).strip()


def _human_users(report: dict) -> dict:
    return {
        ident: info
        for ident, info in report.get("users", {}).items()
        if info.get("kind") == "user"
    }


def _service_users(report: dict) -> dict:
    return {
        ident: info
        for ident, info in report.get("users", {}).items()
        if info.get("kind") == "service"
    }


def _operator_users(report: dict) -> dict:
    return {
        ident: info
        for ident, info in report.get("users", {}).items()
        if info.get("kind") == "operator"
    }


def _sum_s3_usage_types(report: dict, pattern: str) -> tuple[float, dict[str, float]]:
    rx = re.compile(pattern, re.I)
    by_type: dict[str, float] = defaultdict(float)
    for day_map in report.get("cost_by_day_usagetype", {}).values():
        for usage_type, cost in day_map.items():
            if rx.search(usage_type):
                by_type[usage_type] += float(cost)
    return sum(by_type.values()), dict(sorted(by_type.items(), key=lambda kv: -kv[1]))


def _bill_driver_rows(report: dict) -> list[tuple[str, str, str]]:
    """Return plain-English rows that separate viewing from operational cost."""
    rows: list[tuple[str, str, str]] = []
    users = _human_users(report)
    services = _service_users(report)
    cf = report.get("cf", {})
    s3sec = report.get("s3", {})
    total_cost = float(report.get("total_cost", 0.0) or 0.0)
    s3_cost = float(report.get("cost_by_service", {}).get("Amazon Simple Storage Service", 0.0) or 0.0)

    user_total_bytes = int(sum(u.get("gb", 0.0) for u in users.values()) * 1e9)
    if user_total_bytes:
        rows.append((
            "Human viewing / signed data",
            _fmt_gb(user_total_bytes),
            "Traffic tied to signed URLs and users; this is the closest watching metric.",
        ))

    cf_video_bytes = int(cf.get("by_content_class_user_bytes", {}).get("video", 0) or 0)
    cloudfront_cost = float(report.get("cost_by_service", {}).get("Amazon CloudFront", 0.0) or 0.0)
    transfer_cost, _ = _sum_s3_usage_types(report, r"DataTransfer|Bandwidth")
    if cf_video_bytes or cloudfront_cost or transfer_cost:
        amount = f"{_fmt_gb(cf_video_bytes)} delivered; {_fmt_money(cloudfront_cost)} CloudFront, {_fmt_money(transfer_cost)} S3 transfer"
        rows.append((
            "User video watching cost",
            amount,
            "Watching is visible as delivered video GB; AWS does not split the S3 transfer/cache-fill bill cleanly by viewer.",
        ))

    cf_user_bytes = int(cf.get("user_bytes", 0) or 0)
    if cf_user_bytes:
        rows.append((
            "CloudFront delivered",
            _fmt_gb(cf_user_bytes),
            "Bytes sent to viewers from the CDN. This is delivery, not the S3 bill by itself.",
        ))

    s3_origin_bytes = int(s3sec.get("bytes_sent_total", 0) or 0)
    if s3_origin_bytes:
        rows.append((
            "S3 origin/cache-fill reads",
            _fmt_gb(s3_origin_bytes),
            "Backend reads for cache misses, signed fallback, poses/logs, and tooling; do not add to CF as extra viewers.",
        ))

    service_total_bytes = int(sum(u.get("gb", 0.0) for u in services.values()) * 1e9)
    if service_total_bytes:
        rows.append((
            "Service credential activity",
            _fmt_gb(service_total_bytes),
            "Legacy uploader/service traffic, not a human site user.",
        ))

    retrieval_cost, retrieval_by_type = _sum_s3_usage_types(report, r"Retrieval")
    if retrieval_cost > 0.005:
        copy = s3sec.get("copy_object", {}) if s3sec else {}
        if copy.get("count", 0):
            meaning = (
                f"Mostly operational retrieval/copy work ({copy['count']} server-side copy ops), "
                "not normal video watching."
            )
        else:
            meaning = "Retrieval from infrequent-access storage; can be caused by cache fills or operational reads."
        top_usage = next(iter(retrieval_by_type), "S3 retrieval")
        rows.append((f"{top_usage}", _fmt_money(retrieval_cost), meaning))

    if transfer_cost > 0.005:
        rows.append((
            "S3 data transfer",
            _fmt_money(transfer_cost),
            "Bytes leaving S3; can be direct signed downloads or origin traffic before CloudFront cache absorbs re-watches.",
        ))

    storage_cost, _ = _sum_s3_usage_types(report, r"TimedStorage")
    if storage_cost > 0.005:
        rows.append((
            "S3 storage",
            _fmt_money(storage_cost),
            "Standing storage charge for objects at rest.",
        ))

    request_cost, _ = _sum_s3_usage_types(report, r"Requests")
    if request_cost > 0.005:
        rows.append((
            "S3 requests",
            _fmt_money(request_cost),
            "GET/PUT/list request charges; usually small compared with retrieval/storage.",
        ))

    other_cost = total_cost - s3_cost
    if other_cost > 0.005:
        rows.append((
            "Other AWS services",
            _fmt_money(other_cost),
            "Registrar, Route 53, tax, and small account/service charges.",
        ))

    return rows


def _fmt_when(iso: Optional[str]) -> str:
    """ISO UTC timestamp -> 'YYYY-MM-DD HH:MM' in the report's local TZ."""
    if not iso:
        return "—"
    try:
        return (pd.to_datetime(iso, utc=True)
                  .tz_convert(REPORT_TZ).strftime("%Y-%m-%d %H:%M"))
    except Exception:
        return str(iso)


def _short_ua(ua: str, limit: int = 64) -> str:
    ua = (ua or "").replace("|", "\\|")
    return ua if len(ua) <= limit else ua[: limit - 1] + "…"


def _render_security_lines(report: dict) -> list[str]:
    """Markdown lines for the security review section. Returns [] if the
    report has no security data attached (very old cached report)."""
    sec = report.get("security")
    if not sec:
        return []

    lines: list[str] = ["## Security review", ""]

    counts = sec.get("counts", {})
    if counts:
        lines.append("| Event kind | Count |")
        lines.append("|---|---:|")
        for k in ("signin", "auth_fail", "would_deny", "allowlist_denied"):
            lines.append(f"| `{k}` | {counts.get(k, 0)} |")
        lines.append("")

    cf_status = sec.get("cloudfront", {})
    if cf_status:
        enabled = cf_status.get("enabled")
        state = ("✅ enabled" if enabled is True
                 else "🛑 KILLSWITCHED" if enabled is False
                 else "❓ unknown")
        lm = _fmt_when(cf_status.get("last_modified"))
        domain = cf_status.get("domain", "")
        cid = cf_status.get("id", "")
        line = f"- **CloudFront** ({cid} → `{domain}`): {state}, last modified {lm}"
        if cf_status.get("error"):
            line += f"  — fetch error: `{cf_status['error']}`"
        lines.append(line)
        lines.append("")

    by_kind = sec.get("warnings_by_kind", {})

    # Auth failures. The "kind" column shows the credential mechanism
    # (google / session / legacy / none) — we already know every row here
    # is an auth_fail event, so showing the event class would be
    # redundant. `auth_kind` is preserved separately in the parsed
    # event so this collision-free.
    af = by_kind.get("auth_fail", [])
    if af:
        lines.append("### Auth failures")
        lines.append("| When | kind | reason | attempted | ip | ua |")
        lines.append("|---|---|---|---|---|---|")
        for r in af:
            lines.append(
                "| {when} | {kind} | {reason} | {attempted} | {ip} | {ua} |".format(
                    when=_fmt_when(r.get("time")),
                    kind=r.get("auth_kind") or "",
                    reason=r.get("reason", ""),
                    attempted=r.get("attempted") or "",
                    ip=r.get("ip", ""),
                    ua=_short_ua(r.get("user_agent", "")),
                )
            )
        lines.append("")

    # Off-allowlist sign-in attempts (verified email, not on list)
    ad = by_kind.get("allowlist_denied", [])
    if ad:
        lines.append("### Off-allowlist sign-in attempts")
        lines.append("| When | email | reason |")
        lines.append("|---|---|---|")
        for r in ad:
            lines.append(
                f"| {_fmt_when(r.get('time'))} | "
                f"{r.get('identity', '')} | {r.get('reason', '')} |"
            )
        lines.append("")

    # Successful sign-ins
    si = by_kind.get("signin", [])
    if si:
        lines.append("### Sign-ins (fresh Google → session-token mint)")
        lines.append("| When | identity | ip | ua |")
        lines.append("|---|---|---|---|")
        for r in si:
            lines.append(
                f"| {_fmt_when(r.get('time'))} | "
                f"{r.get('identity', '')} | {r.get('ip', '')} | "
                f"{_short_ua(r.get('user_agent', ''))} |"
            )
        lines.append("")

    new_ids = sec.get("new_identities", [])
    if new_ids:
        lines.append("### New identities in window (not seen in PRESIGN history)")
        for r in new_ids:
            lines.append(
                f"- `{r['identity']}` — first seen {_fmt_when(r['first_seen_in_window'])}"
            )
        lines.append("")

    new_ips = sec.get("new_ips_per_identity", [])
    if new_ips:
        lines.append("### New IPs per identity")
        for r in new_ips:
            lines.append(
                f"- `{r['identity']}` from `{r['ip']}` — "
                f"first seen {_fmt_when(r['first_seen_in_window'])}"
            )
        lines.append("")

    wd = by_kind.get("would_deny", [])
    if wd:
        lines.append("### WOULD_DENY hits (signing policy in dry-run)")
        lines.append("| When | identity | method | bucket | key | reason |")
        lines.append("|---|---|---|---|---|---|")
        for r in wd:
            lines.append(
                f"| {_fmt_when(r.get('time'))} | "
                f"{r.get('identity', '')} | {r.get('method', '')} | "
                f"{r.get('bucket', '')} | `{r.get('key', '')}` | "
                f"{r.get('reason', '')} |"
            )
        lines.append("")

    anon = sec.get("anonymous_s3_hits", [])
    if anon:
        lines.append("### Direct-to-S3 anonymous fetches")
        lines.append("| When | ip | key | status | bytes |")
        lines.append("|---|---|---|---|---:|")
        for r in anon[:50]:
            lines.append(
                f"| {_fmt_when(r.get('time'))} | "
                f"{r.get('ip', '')} | `{r.get('key', '')}` | "
                f"{r.get('status', '')} | {int(r.get('bytes_sent') or 0)} |"
            )
        if len(anon) > 50:
            lines.append(f"\n…and {len(anon) - 50} more.")
        lines.append("")

    bots = sec.get("bot_user_agents", [])
    if bots:
        lines.append("### Bot / non-browser user-agents")
        lines.append("| Count | source | identity | ua |")
        lines.append("|---:|---|---|---|")
        for r in bots[:20]:
            lines.append(
                f"| {r.get('count', 0)} | {r.get('source', '')} | "
                f"{r.get('identity', '') or '—'} | {_short_ua(r.get('ua', ''))} |"
            )
        lines.append("")

    al = sec.get("allowlist", [])
    if al:
        lines.append(f"### Allowlist ({len(al)} entries)")
        recent = sec.get("allowlist_recent", [])
        expiring = sec.get("allowlist_expiring", [])
        if recent:
            lines.append(f"- **Recent (≤7d):** "
                         + ", ".join(f"`{e['email']}`" for e in recent))
        if expiring:
            lines.append(f"- **Expiring (≤14d):** "
                         + ", ".join(
                             f"`{e['email']}` ({e['days_until_expiry']}d)"
                             for e in expiring))
        lines.append("")
        lines.append("| Email | Added | Expires | Days left | Note |")
        lines.append("|---|---|---|---:|---|")
        for e in al:
            note = (e.get("note") or "").replace("|", r"\|")
            days_left = (e.get("days_until_expiry")
                         if e.get("days_until_expiry") is not None else "—")
            lines.append(
                f"| `{e['email']}` | {_fmt_when(e.get('added_at'))} | "
                f"{_fmt_when(e.get('expires_at'))} | "
                f"{days_left} | {note} |"
            )
        lines.append("")

    return lines


def render_markdown(report: dict, *, security_only: bool = False) -> str:
    w = report["window"]
    lines: list[str] = []
    if security_only:
        lines.append(
            f"# Cloud activity — security review — {w['start']} → {w['end']} "
            f"({w['days']} local day{'s' if w['days']!=1 else ''}, {REPORT_TIMEZONE})"
        )
        lines.append("")
        lines.extend(_render_security_lines(report))
        lines.append(f"\nGenerated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.")
        return "\n".join(lines) + "\n"

    lines.append(
        f"# AWS usage — {w['start']} → {w['end']} "
        f"({w['days']} local day{'s' if w['days']!=1 else ''}, {REPORT_TIMEZONE})"
    )
    lines.append("")

    # Headline
    total = report.get("total_cost", 0.0)
    distinct_users = report.get("distinct_users", 0)
    service_count = report.get("service_identities", 0)
    operator_count = report.get("operator_identities", 0)
    user_total_bytes = int(sum(u.get("gb", 0.0) for u in _human_users(report).values()) * 1e9)
    operator_total_bytes = int(sum(u.get("gb", 0.0) for u in _operator_users(report).values()) * 1e9)
    cbd = report.get("cost_by_day", {})
    biggest_day = max(cbd.items(), key=lambda kv: kv[1]) if cbd else (None, 0.0)
    lines.append("| Summary | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Total cost | {_fmt_money(total)} |")
    if biggest_day[0] is not None:
        lines.append(f"| Biggest day | {_md_cell(f'{biggest_day[0]} ({_fmt_money(biggest_day[1])})')} |")
    lines.append(f"| Human users | {distinct_users} |")
    if service_count:
        lines.append(f"| Service identities | {service_count} |")
    if operator_count:
        lines.append(f"| IAM operators | {operator_count} |")
    lines.append(f"| Human-attributed traffic | {_fmt_gb(user_total_bytes)} |")
    if operator_total_bytes:
        lines.append(f"| Operator uploads | {_fmt_gb(operator_total_bytes)} |")
    lines.append("")

    driver_rows = _bill_driver_rows(report)
    if driver_rows:
        lines.append("## What drove this")
        lines.append("| Thing | Amount | How to read it |")
        lines.append("|---|---:|---|")
        for name, amount, meaning in driver_rows:
            lines.append(f"| {_md_cell(name)} | {_md_cell(amount)} | {_md_cell(meaning)} |")
        lines.append("")

    # Anomalies up top
    anomalies = report.get("anomalies", [])
    if anomalies:
        lines.append("## Anomalies / callouts")
        for a in anomalies:
            sev_marker = {"high": "⚠ HIGH", "info": "•"}.get(a["severity"], "•")
            lines.append(f"- {sev_marker} **{a['kind']}**: {a['msg']}")
        lines.append("")

    # Cost by week (US, Sun–Sat). Single-day reports get a single row.
    if cbd:
        lines.append("## Cost")
        services = sorted(report.get("cost_by_service", {}).keys(),
                          key=lambda s: -report["cost_by_service"][s])[:6]
        header = ["Week", "Total"] + [_short_service(s) for s in services]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        cbds = report.get("cost_by_day_service", {})
        for label, week_days in _week_buckets(sorted(cbd.keys())):
            week_total = sum(cbd[d] for d in week_days)
            row = [label, _fmt_money(week_total)]
            for s in services:
                v = sum(cbds.get(d, {}).get(s, 0.0) for d in week_days)
                row.append(_fmt_money(v) if v > 0.005 else "-")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # S3 usage-type spotlight
    cdu = report.get("cost_by_day_usagetype", {})
    if cdu:
        # Pick top 6 usage types by total
        ut_total: dict[str, float] = defaultdict(float)
        for day, m in cdu.items():
            for ut, v in m.items():
                ut_total[ut] += v
        top_ut = [ut for ut, _ in sorted(ut_total.items(), key=lambda kv: -kv[1])[:6]]
        if top_ut:
            lines.append("## S3 usage-type breakdown")
            lines.append("| Week | " + " | ".join(top_ut) + " |")
            lines.append("|" + "|".join(["---"] * (len(top_ut) + 1)) + "|")
            for label, week_days in _week_buckets(sorted(cdu.keys())):
                row = [label]
                for ut in top_ut:
                    v = sum(cdu.get(d, {}).get(ut, 0.0) for d in week_days)
                    row.append(_fmt_money(v) if v > 0.005 else "-")
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")

    # End-user activity. CF delivered bytes are the user-facing view when
    # CloudFront signing is enabled. S3 origin bytes show cache-miss/backend
    # traffic and should not be added to CF bytes as "total user traffic".
    cf = report.get("cf", {})
    s3sec = report.get("s3", {})
    if (cf and not cf.get("empty")) or (s3sec and not s3sec.get("empty")):
        lines.append("## End-user activity")
        cf_by_day = cf.get("user_bytes_by_day", {}) if cf else {}
        s3_by_day = s3sec.get("bytes_by_day", {}) if s3sec else {}
        all_days = sorted(set(cf_by_day) | set(s3_by_day))
        if all_days:
            lines.append("| Week | CF delivered GB | S3 origin/cache-fill GB |")
            lines.append("|---|---:|---:|")
            for label, week_days in _week_buckets(all_days):
                cf_b = sum(cf_by_day.get(d, 0) for d in week_days)
                s3_b = sum(s3_by_day.get(d, 0) for d in week_days)
                lines.append(f"| {label} | {cf_b/1e9:.3f} | {s3_b/1e9:.3f} |")
            lines.append("")
        bcc = cf.get("by_content_class_user_bytes", {}) if cf else {}
        if bcc:
            lines.append("**CF user-bytes by content class:**")
            for cls, b in sorted(bcc.items(), key=lambda kv: -kv[1]):
                lines.append(f"- `{cls}` — {_fmt_gb(b)}")
            lines.append("")
        s3cc = s3sec.get("by_content_class_bytes", {}) if s3sec else {}
        if s3cc:
            lines.append("**S3 origin/cache-fill bytes by content class:**")
            for cls, b in sorted(s3cc.items(), key=lambda kv: -kv[1]):
                lines.append(f"- `{cls}` — {_fmt_gb(b)}")
            lines.append("")
        scan = cf.get("scanner", {}) if cf else {}
        if scan.get("rows", 0):
            lines.append(f"**Scanner traffic:** {scan['rows']} probes from {scan['unique_ips']} IPs (all should 4xx).")
            lines.append("")

    # Per-user table
    users = report.get("users", {})
    if users:
        lines.append("## Per-user attribution")
        lines.append("| Identity | GB | Ops | Top file |")
        lines.append("|---|---:|---:|---|")
        for ident, info in users.items():
            top = next(iter(info.get("top_files", {}).items()), (None, 0))
            top_str = f"`{top[0]}` ({_fmt_gb(top[1])})" if top[0] else "—"
            k = info.get("kind", "user")
            kind_label = ("Service credential" if k == "service"
                          else "IAM operator" if k == "operator"
                          else "Human user")
            ops_label = info.get("ops_label", "GETs")
            lines.append(
                f"| `{_md_cell(ident)}` ({kind_label}) | {info['gb']:.3f} | "
                f"{info['gets']} {ops_label} | {_md_cell(top_str)} |"
            )
        lines.append("")

    # S3 ops (operational health window)
    s3sec = report.get("s3", {})
    if s3sec and not s3sec.get("empty"):
        lines.append("## S3 origin/cache-fill operations")
        by_op = s3sec.get("by_op", {})
        if by_op:
            lines.append("| Operation | Count | Bytes sent |")
            lines.append("|---|---:|---:|")
            for op, info in sorted(by_op.items(), key=lambda kv: -kv[1]["count"])[:12]:
                lines.append(f"| `{op}` | {info['count']} | {_fmt_gb(info['bytes'])} |")
            lines.append("")
        copy = s3sec.get("copy_object", {})
        if copy.get("count", 0) > 0:
            lines.append(f"**Server-side COPY:** {copy['count']} ops, {_fmt_gb(copy['total_object_size'])} of source bytes "
                         f"(IA-class sources cost $0.01/GB to retrieve)")
            lines.append("")

    # Health
    health = report.get("health", {})
    if health:
        lines.append("## Operational health")
        if "cf_ttfb_p50" in health:
            lines.append(f"- CF TTFB: p50 {health['cf_ttfb_p50']:.3f}s, p95 {health['cf_ttfb_p95']:.3f}s")
        if "cf_4xx_5xx" in health:
            lines.append(f"- CF non-2xx: {health['cf_4xx_5xx']}")
        if "s3_4xx_5xx" in health:
            lines.append(f"- S3 non-2xx: {health['s3_4xx_5xx']}")
        lines.append("")

    # Security review
    lines.extend(_render_security_lines(report))

    # Footnotes
    lines.append("---")
    src = report.get("sources", {})
    lines.append(
        f"Sources: Cost Explorer ({src.get('cost_rows',0)} rows), "
        f"S3 access logs ({src.get('s3_rows',0)} rows), "
        f"CF access logs ({src.get('cf_rows',0)} rows), "
        f"PRESIGN events ({src.get('presign_rows',0)} rows), "
        f"auth/security warnings ({src.get('warning_rows',0)} rows). "
        f"Access-log days use {REPORT_TIMEZONE}; Cost Explorer remains AWS billing UTC. "
        f"CF log delivery is best-effort, lags 1–2h."
    )
    if w["start"] < ACCESS_LOG_START:
        lines.append(
            f"\n**Note:** S3 / CloudFront access logs only started {ACCESS_LOG_START}. "
            f"Cost numbers cover the full window, but per-byte and per-user "
            f"attribution before that date will appear as zero / missing."
        )
    lines.append(f"\nGenerated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.")
    return "\n".join(lines) + "\n"
