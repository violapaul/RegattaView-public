#!/usr/bin/env bash
#
# Cold-backup the local-only RegattaView corpus to Glacier Deep Archive.
#
# Covers data that is irrecoverable if the workstation hard drive fails AND
# is not already on S3 via the normal pipeline:
#   - /Users/viola/Data/BBLog/                          raw BoatBrain logs
#   - /Users/viola/Data/SailingData/CompressedLogs/     pre-BBLog Actisense logs
#   - /Users/viola/Data/SailingData/GPXLogs/            legacy GPX tracks
#   - /Users/viola/Data/SailingData/metadata.yml        canonical race metadata
#   - NamedLogs.symlinks.txt                            symlink-mapping snapshot
#   - s3://regattaview-repo-data/                       repo data kept out of git
#                                                       (notes, docs, fixtures; S3→S3)
#
# Idempotent: re-run any time to catch new sessions. Existing objects on
# DEEP_ARCHIVE are not re-uploaded by `aws s3 sync` unless local size/mtime
# changed. Restore costs apply on retrieval (Glacier Deep Archive: bulk
# restore ~12h, ~$0.0025/GB).
#
# Cost model (us-west-2 as of 2026-05):
#   storage:     ~$0.00099/GB/month (14.6 GB → ~$0.015/mo)
#   PUT request: ~$0.05/1000 (2.5k files → ~$0.13 one-time)
#   retrieval:   ~$0.0025/GB bulk, ~$0.02/GB standard
#
# Refer to BACKUP.md for the full disaster-recovery story.

set -euo pipefail

# Defaults to the Administrator profile because the cold-backup bucket is
# operated outside the live-deploy IAM scope: `regatta-uploader` is locked
# down to s3:* on `regattaanalysis/*` only. This script is invoked by hand
# at low frequency, so running as admin is acceptable. To rotate onto a
# narrower principal later, attach an inline policy granting s3:* on
# arn:aws:s3:::regattaview-cold-backup/* and override AWS_PROFILE.
AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_PROFILE
BUCKET="${COLD_BACKUP_BUCKET:-regattaview-cold-backup}"
REGION="${COLD_BACKUP_REGION:-us-west-2}"
STORAGE_CLASS="DEEP_ARCHIVE"
DATA_ROOT="/Users/viola/Data"
SAILING="${DATA_ROOT}/SailingData"

log() { printf '[cold_backup] %s\n' "$*"; }

# 1. Ensure bucket exists with the hardened config we want. Idempotent: each
#    sub-step is a no-op when already in the desired state.
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    log "Creating s3://$BUCKET in $REGION"
    aws s3api create-bucket \
        --bucket "$BUCKET" \
        --region "$REGION" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null

    log "Applying Block Public Access"
    aws s3api put-public-access-block \
        --bucket "$BUCKET" \
        --public-access-block-configuration \
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

    # Versioning protects against accidental overwrite/delete; storage cost
    # of old versions is negligible for cold data and they also transition
    # via the lifecycle below.
    log "Enabling versioning"
    aws s3api put-bucket-versioning \
        --bucket "$BUCKET" \
        --versioning-configuration Status=Enabled

    # Belt-and-suspenders: anything that *did* land in STANDARD (e.g. an
    # operator copying a file without --storage-class) transitions to
    # DEEP_ARCHIVE after 1 day, and noncurrent versions expire after 365d.
    log "Applying lifecycle policy (force DEEP_ARCHIVE, expire old versions)"
    aws s3api put-bucket-lifecycle-configuration \
        --bucket "$BUCKET" \
        --lifecycle-configuration '{
          "Rules": [
            {
              "ID": "force-deep-archive",
              "Status": "Enabled",
              "Filter": {"Prefix": ""},
              "Transitions": [
                {"Days": 1, "StorageClass": "DEEP_ARCHIVE"}
              ],
              "NoncurrentVersionExpiration": {"NoncurrentDays": 365}
            }
          ]
        }'
else
    log "Bucket s3://$BUCKET already exists"
fi

sync_tree() {
    local src="$1" dst_prefix="$2"
    if [ ! -d "$src" ]; then
        log "Skipping missing source: $src"
        return
    fi
    log "Syncing $src -> s3://$BUCKET/$dst_prefix"
    aws s3 sync "$src" "s3://$BUCKET/$dst_prefix" \
        --storage-class "$STORAGE_CLASS" \
        --no-follow-symlinks \
        --exclude ".DS_Store" \
        --exclude "._*" \
        --exclude ".fseventsd/*" \
        --exclude ".Spotlight-V100/*"
}

# 2. Sync the data trees.
sync_tree "${DATA_ROOT}/BBLog/"            "BBLog/"
sync_tree "${SAILING}/CompressedLogs/"     "CompressedLogs/"
sync_tree "${SAILING}/GPXLogs/"            "GPXLogs/"

# 3. NamedLogs is a directory of symlinks into CompressedLogs (date-named
#    aliases). Backing up just the mapping is enough — the underlying bytes
#    live in CompressedLogs/.
NAMED_LOGS="${SAILING}/NamedLogs"
if [ -d "$NAMED_LOGS" ]; then
    snapshot="$(mktemp -t namedlogs_symlinks.XXXXXX)"
    trap 'rm -f "$snapshot"' EXIT
    log "Generating NamedLogs symlink snapshot"
    (cd "$NAMED_LOGS" && find . -maxdepth 1 -type l -print0 | \
        xargs -0 -I{} sh -c 'printf "%s\t%s\n" "$1" "$(readlink "$1")"' _ {} \
        | sort) > "$snapshot"
    log "Uploading NamedLogs.symlinks.txt ($(wc -l < "$snapshot" | tr -d ' ') entries)"
    aws s3 cp "$snapshot" "s3://$BUCKET/NamedLogs.symlinks.txt" \
        --storage-class "$STORAGE_CLASS"
fi

# 4. Race metadata YAML (single file, hand-edited fields are only here).
if [ -f "${SAILING}/metadata.yml" ]; then
    log "Uploading metadata.yml"
    aws s3 cp "${SAILING}/metadata.yml" "s3://$BUCKET/metadata.yml" \
        --storage-class "$STORAGE_CLASS"
fi

# 5. Repo data (notes, write-ups, ops docs, Results fixtures — see
#    repo_data.manifest). Its only other copy is one versioned S3 bucket, so
#    mirror the current objects server-side (no local download).
REPO_DATA_BUCKET="${REPO_DATA_BUCKET:-regattaview-repo-data}"
log "Syncing s3://$REPO_DATA_BUCKET -> s3://$BUCKET/repo-data/"
aws s3 sync "s3://$REPO_DATA_BUCKET/" "s3://$BUCKET/repo-data/" \
    --storage-class "$STORAGE_CLASS"

# Small collaborative documents are never overwritten from a local stale copy.
# Preserve their current server versions in the versioned backup bucket too.
aws s3 sync s3://regattaanalysis/www/Comments/ "s3://$BUCKET/shared/www/Comments/" \
    --storage-class "$STORAGE_CLASS"
for shared_key in www/Analyze/events_edits.json www/Insta360/curated.json; do
    # list-objects-v2 succeeds for a legitimately absent optional document;
    # permission/network failures remain fatal under set -e.
    object_count=$(aws s3api list-objects-v2 --bucket regattaanalysis --prefix "$shared_key" \
        --query 'KeyCount' --output text)
    if [ "$object_count" -gt 0 ]; then
        aws s3 cp "s3://regattaanalysis/$shared_key" \
            "s3://$BUCKET/shared/$shared_key" --storage-class "$STORAGE_CLASS"
    fi
done

log "Done. Inventory:"
aws s3 ls --recursive --summarize --human-readable "s3://$BUCKET/" | tail -4
