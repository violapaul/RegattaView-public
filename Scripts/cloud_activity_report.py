#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python

"""
Cloud activity report CLI for RegattaView.

Pulls Cost Explorer + S3 access logs + CloudFront access logs + Lambda
PRESIGN events + auth-failure / sign-in / WOULD_DENY warnings for a date
window, joins identities by (key, ip, time), and writes a Markdown
report covering both billing usage and a security review section.
Optional SES email.

Default window is the last 7 complete local days. Default output path is
~/Resilio Sync/Dropbox/Sailboat/Reports/aws_usage_<from>_<to>.md (filename
prefix kept for compatibility with already-synced past reports).

Examples:
    Scripts/cloud_activity_report.py
    Scripts/cloud_activity_report.py --since 14d
    Scripts/cloud_activity_report.py --day 2026-05-02 --no-cache
    Scripts/cloud_activity_report.py --range 2026-04-26,2026-05-02 --email
    Scripts/cloud_activity_report.py --day 2026-05-02 --email --dry-run
    Scripts/cloud_activity_report.py --since 7d --security-only --print
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from Libraries.log_helper import LOG
from Libraries.env import load_dotenv

# Before importing cloud_activity: its OWN_IP_DEFAULT reads REGATTA_OWN_IP.
load_dotenv()

from Libraries.cloud_activity import (
    Window,
    build_report,
    fetch_cost_explorer,
    render_markdown,
    OWN_IP_DEFAULT,
    REPORT_TIMEZONE,
    REPORT_TZ,
)


DEFAULT_REPORTS_DIR = Path("/Users/viola/Resilio Sync/Dropbox/Sailboat/Reports")
# Personal addresses live in the repo .env (gitignored), not in source.
DEFAULT_SENDER = os.environ.get("REGATTA_REPORT_SENDER", "")
DEFAULT_RECIPIENTS = tuple(
    r.strip() for r in os.environ.get("REGATTA_REPORT_RECIPIENTS", "").split(",") if r.strip())
DEFAULT_SES_REGION = "us-west-2"


def _parse_window(args: argparse.Namespace) -> Window:
    """Resolve --day / --range / --since into a Window (inclusive both ends)."""
    if args.day:
        try:
            d = date.fromisoformat(args.day)
        except ValueError:
            sys.exit(f"--day must be YYYY-MM-DD, got: {args.day}")
        return Window(start=d.isoformat(), end=d.isoformat())
    if args.range:
        try:
            a_str, b_str = args.range.split(",")
            a = date.fromisoformat(a_str.strip())
            b = date.fromisoformat(b_str.strip())
        except ValueError:
            sys.exit(f"--range must be YYYY-MM-DD,YYYY-MM-DD, got: {args.range}")
        if b < a:
            sys.exit(f"--range end < start: {args.range}")
        return Window(start=a.isoformat(), end=b.isoformat())
    # --since Nd  (or 'today'), interpreted in the report timezone.
    today_local = datetime.now(REPORT_TZ).date()
    if args.since == "today":
        return Window(start=today_local.isoformat(), end=today_local.isoformat())
    m = re.fullmatch(r"(\d+)d", args.since)
    if not m:
        sys.exit(f"--since must be 'today' or 'Nd', got: {args.since}")
    n = int(m.group(1))
    end = today_local - timedelta(days=1)       # last *complete* local day
    start = end - timedelta(days=n - 1)
    return Window(start=start.isoformat(), end=end.isoformat())


def _verify_totals(window: Window) -> None:
    """
    Cross-check the cached/parsed CE totals against a fresh raw call.
    Fails loudly with a non-zero exit if they diverge by more than $0.01.
    """
    LOG.logger.info("Verifying CE totals against fresh raw call...")
    df_cached = fetch_cost_explorer(window.start, window.ce_end)
    df_fresh = fetch_cost_explorer(window.start, window.ce_end, no_cache=True)
    a = round(float(df_cached["cost"].sum()), 2)
    b = round(float(df_fresh["cost"].sum()), 2)
    if abs(a - b) > 0.01:
        sys.exit(f"--verify mismatch: cached=${a:.2f} fresh=${b:.2f}")
    LOG.logger.info(f"--verify OK: cached=${a:.2f} == fresh=${b:.2f}")


def _default_out_path(window: Window) -> Path:
    DEFAULT_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if window.start == window.end:
        name = f"aws_usage_{window.start}.md"
    else:
        name = f"aws_usage_{window.start}_{window.end}.md"
    return DEFAULT_REPORTS_DIR / name


def main() -> int:
    p = argparse.ArgumentParser(
        prog="cloud_activity_report.py",
        description="Generate a cloud usage + security activity report for RegattaView.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    win = p.add_mutually_exclusive_group()
    win.add_argument("--since", default="7d",
                     help=f"Local-day window length: e.g. 7d, 14d, today (default: 7d; timezone {REPORT_TIMEZONE})")
    win.add_argument("--day", help=f"Single local day, YYYY-MM-DD ({REPORT_TIMEZONE})")
    win.add_argument("--range", help=f"Local YYYY-MM-DD,YYYY-MM-DD inclusive ({REPORT_TIMEZONE})")

    p.add_argument("--out", help="Override output path (Markdown)")
    p.add_argument("--format", choices=["md", "json", "both"], default="md")
    p.add_argument("--email", action="store_true",
                   help=f"Also email the report via SES to {','.join(DEFAULT_RECIPIENTS)}")
    p.add_argument("--recipient", default=",".join(DEFAULT_RECIPIENTS),
                   help="Comma-separated email recipients (each must be SES-verified in sandbox mode)")
    p.add_argument("--sender", default=DEFAULT_SENDER,
                   help="SES sender (must be verified)")
    p.add_argument("--dry-run", action="store_true",
                   help="With --email: print subject + HTML preview, do not send")
    p.add_argument("--include-own-ip", action="store_true",
                   help=f"Don't strip own IP ({OWN_IP_DEFAULT}) from CF user stats")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass ~/Data/AwsUsage cache; force re-fetch")
    p.add_argument("--verify", action="store_true",
                   help="Cross-check cached CE totals against a fresh API call")
    p.add_argument("--quiet", action="store_true",
                   help="Reduce log noise (logger level WARNING)")
    p.add_argument("-p", "--print", action="store_true", dest="print_report",
                   help="Print report to stdout instead of writing to a file. "
                        "Pipe to `| tee path.md` if you want both.")
    p.add_argument("--security-only", action="store_true",
                   help="Render only the security review section "
                        "(sign-ins, auth failures, WOULD_DENY, allowlist, "
                        "killswitch state). Skips cost-attribution tables.")

    args = p.parse_args()
    if args.email and not (args.sender and args.recipient):
        p.error("--email needs --sender/--recipient or REGATTA_REPORT_SENDER/"
                "REGATTA_REPORT_RECIPIENTS in .env")

    if args.quiet:
        LOG.set_logging_level(logging.WARNING)

    window = _parse_window(args)

    if args.verify:
        _verify_totals(window)

    own_ip = None if args.include_own_ip else OWN_IP_DEFAULT
    report = build_report(window, own_ip=own_ip, no_cache=args.no_cache)
    md = render_markdown(report, security_only=args.security_only)

    if args.print_report:
        if args.format in ("md", "both"):
            sys.stdout.write(md)
        if args.format in ("json", "both"):
            json.dump(report, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
    else:
        out_path = Path(args.out) if args.out else _default_out_path(window)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if args.format in ("md", "both"):
            out_path.write_text(md)
            LOG.logger.info(f"Wrote {out_path}")
        if args.format in ("json", "both"):
            json_path = out_path.with_suffix(".json")
            json_path.write_text(json.dumps(report, indent=2, default=str))
            LOG.logger.info(f"Wrote {json_path}")

    if args.email:
        # Imported lazily so the core path doesn't need SES configured.
        from Libraries.cloud_activity_ses import send_report_email
        total = report.get("total_cost", 0.0)
        if window.start == window.end:
            subject = f"RegattaView AWS usage {window.start} — ${total:.2f}"
        else:
            subject = f"RegattaView AWS usage {window.start}..{window.end} — ${total:.2f}"
        recipients = [r.strip() for r in args.recipient.split(",") if r.strip()]
        ok = send_report_email(
            sender=args.sender,
            recipients=recipients,
            subject=subject,
            markdown_body=md,
            region=DEFAULT_SES_REGION,
            dry_run=args.dry_run,
        )
        if not ok:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
