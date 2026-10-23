#!/usr/bin/env python3
"""
Manage the RegattaView per-user allowlist stored in S3.

The Lambda reads s3://regattaanalysis/auth/allowlist.json on each cold start
(cached ~5 min while warm). Each entry: {"email", "expires_at", "added_at",
"note"}. Lookup is case-insensitive on email; expires_at is an ISO 8601 UTC
timestamp.

Usage:
    Scripts/manage_allowlist.py list
    Scripts/manage_allowlist.py add EMAIL [--months N | --weeks N | --days N | --years N] [--note TEXT]
    Scripts/manage_allowlist.py extend EMAIL [--months N | ...]
    Scripts/manage_allowlist.py revoke EMAIL
    Scripts/manage_allowlist.py show EMAIL

Env: AWS_PROFILE (must point at an admin profile with s3:GetObject and
     s3:PutObject on the allowlist key — the upload-only `regatta` profile
     is NOT sufficient), ALLOWLIST_BUCKET (default: regattaanalysis),
     ALLOWLIST_KEY (default: auth/allowlist.json).

First-time bootstrap: if the allowlist key does not yet exist, the very
first `add` will create it.
"""

import argparse
import datetime as dt
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError


BUCKET = os.environ.get("ALLOWLIST_BUCKET", "regattaanalysis")
KEY = os.environ.get("ALLOWLIST_KEY", "auth/allowlist.json")
DEFAULT_MONTHS = 6


def _s3():
    return boto3.client("s3")


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _expiry_iso(months=0, weeks=0, days=0, years=0):
    days_total = days + weeks * 7 + int(months * 30.44) + int(years * 365.25)
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days_total)).isoformat().replace("+00:00", "Z")


def _load(s3):
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=KEY)
        data = json.loads(obj["Body"].read())
        if not isinstance(data, list):
            raise ValueError("allowlist must be a JSON array")
        return data
    except s3.exceptions.NoSuchKey:
        return []
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            return []
        if code in ("AccessDenied", "403"):
            sys.stderr.write(
                f"AccessDenied loading s3://{BUCKET}/{KEY} — does AWS_PROFILE "
                f"({os.environ.get('AWS_PROFILE', '(default)')}) have s3:GetObject "
                f"on the allowlist key? The upload-only `regatta` profile does NOT.\n")
        raise


def _save(s3, data):
    body = json.dumps(data, indent=2, sort_keys=True).encode()
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=body,
                  ContentType="application/json")


def _find(data, email):
    email = email.lower()
    for entry in data:
        if (entry.get("email") or "").lower() == email:
            return entry
    return None


def _format_row(entry):
    email = entry.get("email", "?")
    exp = entry.get("expires_at") or "(no expiry)"
    note = entry.get("note", "")
    added = entry.get("added_at", "")
    status = ""
    if entry.get("expires_at"):
        try:
            e = dt.datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
            now = dt.datetime.now(dt.timezone.utc)
            if e < now:
                status = "  [EXPIRED]"
            else:
                days = (e - now).days
                status = f"  ({days}d left)"
        except ValueError:
            status = "  [bad expires_at]"
    return f"  {email:30s}  exp={exp:25s}  added={added:25s}{status}  {note}"


# --- commands ---------------------------------------------------------------

def cmd_list(args):
    s3 = _s3()
    data = _load(s3)
    if not data:
        print("(allowlist is empty)")
        return
    print(f"Allowlist (s3://{BUCKET}/{KEY}, {len(data)} entries):")
    for entry in sorted(data, key=lambda e: e.get("email", "")):
        print(_format_row(entry))


def cmd_show(args):
    s3 = _s3()
    data = _load(s3)
    entry = _find(data, args.email)
    if not entry:
        print(f"{args.email}: not on allowlist")
        sys.exit(1)
    print(_format_row(entry))


def cmd_add(args):
    s3 = _s3()
    data = _load(s3)
    if _find(data, args.email):
        print(f"{args.email}: already present; use 'extend' to bump expiry")
        sys.exit(1)
    months, weeks, days, years = _ttl_args(args)
    entry = {
        "email": args.email.lower(),
        "expires_at": _expiry_iso(months=months, weeks=weeks, days=days, years=years),
        "added_at": _now_iso(),
        "note": args.note or "",
    }
    data.append(entry)
    _save(s3, data)
    print(f"Added {args.email}:")
    print(_format_row(entry))


def cmd_extend(args):
    s3 = _s3()
    data = _load(s3)
    entry = _find(data, args.email)
    if not entry:
        print(f"{args.email}: not on allowlist; use 'add' instead")
        sys.exit(1)
    months, weeks, days, years = _ttl_args(args)
    entry["expires_at"] = _expiry_iso(months=months, weeks=weeks, days=days, years=years)
    if args.note:
        entry["note"] = args.note
    _save(s3, data)
    print(f"Extended {args.email}:")
    print(_format_row(entry))


def cmd_revoke(args):
    s3 = _s3()
    data = _load(s3)
    entry = _find(data, args.email)
    if not entry:
        print(f"{args.email}: not on allowlist (nothing to revoke)")
        sys.exit(1)
    data = [e for e in data if (e.get("email") or "").lower() != args.email.lower()]
    _save(s3, data)
    print(f"Revoked {args.email}.")


def _ttl_args(args):
    months = args.months or 0
    weeks = args.weeks or 0
    days = args.days or 0
    years = args.years or 0
    if months == weeks == days == years == 0:
        months = DEFAULT_MONTHS
    return months, weeks, days, years


def _add_ttl_flags(p):
    p.add_argument("--months", type=int, default=0)
    p.add_argument("--weeks", type=int, default=0)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--years", type=int, default=0)
    p.add_argument("--note", type=str, default="")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    p = sub.add_parser("show")
    p.add_argument("email")

    p = sub.add_parser("add")
    p.add_argument("email")
    _add_ttl_flags(p)

    p = sub.add_parser("extend")
    p.add_argument("email")
    _add_ttl_flags(p)

    p = sub.add_parser("revoke")
    p.add_argument("email")

    args = ap.parse_args()
    {
        "list": cmd_list,
        "show": cmd_show,
        "add": cmd_add,
        "extend": cmd_extend,
        "revoke": cmd_revoke,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
