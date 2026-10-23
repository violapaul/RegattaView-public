#!/bin/bash
# aws_presign_log.sh
#
# Search the Lambda PRESIGN events that record who minted each presigned
# S3 URL. The Lambda emits one structured INFO line per presigned-URL
# mint; this script pulls them out of CloudWatch Logs and renders a
# table (or JSONL with --raw).
#
# Join target: each row's (key, ip, ts) joins against the S3 server
# access log at s3://regattaanalysis-logs/regattaanalysis/ on the same
# (key, remote-IP, ~minute) tuple to identify who fetched what.
#
# Usage:
#   Scripts/aws_presign_log.sh                          # last 1 hour
#   Scripts/aws_presign_log.sh --hours 6
#   Scripts/aws_presign_log.sh --since 2026-04-25
#   Scripts/aws_presign_log.sh --since "2026-04-25 09:00"
#   Scripts/aws_presign_log.sh --identity alice@example.com
#   Scripts/aws_presign_log.sh --method GET --key-prefix www/Insta360/
#   Scripts/aws_presign_log.sh --raw                    # JSONL, sorted
#
# Env:
#   PRESIGN_LOG_GROUP   override log group (default /aws/lambda/montlake_upload)
#   AWS_PROFILE         standard
#
# Requires: aws CLI, jq.
set -euo pipefail

LOG_GROUP="${PRESIGN_LOG_GROUP:-/aws/lambda/montlake_upload}"
HOURS=1
SINCE=""
IDENTITY=""
METHOD=""
KEY_PREFIX=""
RAW=0

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed -n 's/^# \?//p' | sed '$d'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --hours)        HOURS="$2"; shift 2 ;;
    --since)        SINCE="$2"; shift 2 ;;
    --identity)     IDENTITY="$2"; shift 2 ;;
    --method)       METHOD="$2"; shift 2 ;;
    --key-prefix)   KEY_PREFIX="$2"; shift 2 ;;
    --raw)          RAW=1; shift ;;
    -h|--help)      usage ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null 2>&1 || { echo "jq required; install via 'brew install jq'" >&2; exit 1; }

# Resolve start time. Try macOS BSD date first, then GNU.
if [ -n "$SINCE" ]; then
  start_s=$(date -j -f "%Y-%m-%d %H:%M" "$SINCE" +%s 2>/dev/null \
         || date -j -f "%Y-%m-%d"        "$SINCE" +%s 2>/dev/null \
         || date -d "$SINCE"                     +%s 2>/dev/null) \
    || { echo "could not parse --since '$SINCE'" >&2; exit 2; }
else
  start_s=$(( $(date +%s) - HOURS * 3600 ))
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

aws logs filter-log-events \
  --log-group-name "$LOG_GROUP" \
  --start-time "${start_s}000" \
  --filter-pattern '"PRESIGN"' \
  --output json \
  > "$TMP/raw.json"

# Parse PRESIGN payloads, decorate with ISO ts, filter, sort by ts.
jq -c --arg ident "$IDENTITY" --arg method "$METHOD" --arg pfx "$KEY_PREFIX" '
  [ .events[]
    | . as $e
    | ((.message | capture("PRESIGN (?<j>\\{[^}]+\\})")).j | fromjson) as $p
    | ({ts: ($e.timestamp / 1000 | gmtime | strftime("%Y-%m-%dT%H:%M:%SZ"))} + $p)
    | select(($ident  == "" or .identity == $ident) and
             ($method == "" or .method   == $method) and
             ($pfx    == "" or (.key | startswith($pfx))))
  ] | sort_by(.ts) | .[]
' "$TMP/raw.json" > "$TMP/rows.json"

cnt=$(wc -l < "$TMP/rows.json" | tr -d ' ')
range_label="last ${HOURS}h"
[ -n "$SINCE" ] && range_label="since $SINCE"

if [ "$RAW" -eq 1 ]; then
  cat "$TMP/rows.json"
else
  echo "PRESIGN events ($range_label, $LOG_GROUP): $cnt"
  if [ "$cnt" -gt 0 ]; then
    echo
    printf "%-21s %-30s %-6s %-15s %s\n" ts identity method ip key
    printf -- '-%.0s' {1..130}; echo
    jq -r '[.ts, .identity, .method, .ip, .key] | @tsv' "$TMP/rows.json" \
      | awk -F'\t' '{printf "%-21s %-30s %-6s %-15s %s\n", $1, $2, $3, $4, $5}'
  fi
fi
