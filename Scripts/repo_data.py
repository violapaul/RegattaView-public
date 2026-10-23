#!/usr/bin/env python3
"""Sync repo data that is kept out of git with s3://regattaview-repo-data/tree/.

Paths come from `repo_data.manifest` at the repo root (trailing "/" = whole
directory; `scripts/` subdirectories stay in git and are skipped). The bucket
is private and versioned, so it replaces git history for these files.

    conda run -n sailing python Scripts/repo_data.py status
    conda run -n sailing python Scripts/repo_data.py pull [--force] [--dry-run]
    conda run -n sailing python Scripts/repo_data.py push [--dry-run]
    ... pull --force Analyze/Notes/<slug>/    # limit any command to path prefixes

pull downloads files missing locally; it never overwrites a local file that
differs from the bucket unless --force (so unpushed edits are safe).
push uploads new and changed local files; nothing is ever deleted on either
side (an overwritten S3 object stays as a prior version).
"""

import argparse
import base64
import hashlib
import mimetypes
import os
import subprocess
import sys

import boto3

BUCKET = "regattaview-repo-data"
PREFIX = "tree/"
SKIP_PARTS = {"scripts", "__pycache__", ".ipynb_checkpoints"}
SKIP_NAMES = {".DS_Store"}

ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                      capture_output=True, text=True,
                      cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()


def manifest_entries():
    with open(os.path.join(ROOT, "repo_data.manifest")) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def _skipped(rel):
    parts = rel.split("/")
    return (any(p in SKIP_PARTS for p in parts[:-1]) or parts[-1] in SKIP_NAMES
            or parts[-1].endswith(".pyc"))


def _covered(rel, entries):
    return any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in entries)


def local_files(entries):
    out = set()
    for e in entries:
        path = os.path.join(ROOT, e)
        if e.endswith("/"):
            for d, _, names in os.walk(path):
                for n in names:
                    rel = os.path.relpath(os.path.join(d, n), ROOT)
                    if not _skipped(rel):
                        out.add(rel)
        elif os.path.isfile(path):
            out.add(e)
    return out


def remote_files(s3, entries):
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        for o in page.get("Contents", []):
            rel = o["Key"][len(PREFIX):]
            if _covered(rel, entries) and not _skipped(rel):
                out[rel] = o["ETag"].strip('"')
    return out


def md5(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def classify(s3, entries):
    loc, rem = local_files(entries), remote_files(s3, entries)
    # Objects are uploaded with single-part put_object, so ETag == MD5.
    differ = sorted(r for r in loc & rem.keys() if md5(r) != rem[r])
    return sorted(loc - rem.keys()), sorted(rem.keys() - loc), differ


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "pull", "push"])
    ap.add_argument("--force", action="store_true",
                    help="pull: overwrite local files that differ from the bucket")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("paths", nargs="*",
                    help="only act on files under these repo-relative path prefixes")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name="us-west-2")
    entries = manifest_entries()
    local_only, remote_only, differ = classify(s3, entries)
    if args.paths:
        keep = lambda rows: [r for r in rows if any(r.startswith(p) for p in args.paths)]
        local_only, remote_only, differ = keep(local_only), keep(remote_only), keep(differ)

    if args.cmd == "status":
        for label, rows in (("local only (push to upload)", local_only),
                            ("bucket only (pull to download)", remote_only),
                            ("differ", differ)):
            print(f"{label}: {len(rows)}")
            for r in rows:
                print(f"  {r}")
        return

    if args.cmd == "pull":
        todo = remote_only + (differ if args.force else [])
        for r in todo:
            print(f"{'would pull' if args.dry_run else 'pull'} {r}")
            if not args.dry_run:
                dest = os.path.join(ROOT, r)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                s3.download_file(BUCKET, PREFIX + r, dest)
        if differ and not args.force:
            print(f"\n{len(differ)} local file(s) differ from the bucket and were left "
                  "alone (push them, or pull --force to take the bucket copy):",
                  file=sys.stderr)
            for r in differ:
                print(f"  {r}", file=sys.stderr)
        return

    for r in local_only + differ:  # push
        print(f"{'would push' if args.dry_run else 'push'} {r}")
        if not args.dry_run:
            with open(os.path.join(ROOT, r), "rb") as f:
                data = f.read()
            s3.put_object(
                Bucket=BUCKET, Key=PREFIX + r, Body=data,
                ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode(),
                ContentType=mimetypes.guess_type(r)[0] or "application/octet-stream")


if __name__ == "__main__":
    main()
