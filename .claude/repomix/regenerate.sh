#!/usr/bin/env bash
# Regenerate scoped repomix packs for RegattaView.
# Output: .claude/repomix/pack_<domain>.md
#
# Idempotent: a pack is regenerated only if a tracked/untracked-not-ignored
# file in its scope is newer than the pack itself (or the pack is missing).
#
# Usage from repo root:
#   bash .claude/repomix/regenerate.sh                   # refresh only stale packs
#   bash .claude/repomix/regenerate.sh python frontend   # refresh only these (if stale)
#   bash .claude/repomix/regenerate.sh --force           # rebuild everything
#   bash .claude/repomix/regenerate.sh --force python    # rebuild just python

set -euo pipefail

cd "$(dirname "$0")/../.."
OUT=".claude/repomix"

COMMON_IGNORE="\
**/__pycache__/**,\
**/.pytest_cache/**,\
**/.ipynb_checkpoints/**,\
**/.DS_Store,\
**/node_modules/**,\
**/dist/**,\
**/build/**,\
**/.pio/**,\
**/.vscode/**,\
**/DerivedData/**,\
**/*.parquet,**/*.bin,**/*.mp4,**/*.mov,**/*.mkv,**/*.avi,\
**/*.bag,**/*.mcap,**/*.log,\
**/*.ipynb,\
**/package-lock.json,**/Podfile.lock,\
**/*.xcworkspace/**,**/xcuserdata/**,**/*.pbxproj,\
Analyze/*_frame_audit.json,\
Analyze/starts.json*,Analyze/tracks.json*,Analyze/starts_edits.json,\
.claude/repomix/**,\
repomix-output.*"

FORCE=0
SELECT=()
for arg in "$@"; do
  case "$arg" in
    -f|--force) FORCE=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *) SELECT+=("$arg") ;;
  esac
done

# Is pack_$1 stale given scope-dir list in $2 (space-separated)?
# Stale = pack missing OR any tracked/untracked-not-ignored file in scope newer than pack.
is_stale () {
  local pack_file="$1"
  local scope="$2"
  [ -f "$pack_file" ] || return 0
  # shellcheck disable=SC2086
  local newer
  newer=$(git ls-files --cached --others --exclude-standard -- $scope 2>/dev/null \
          | while IFS= read -r f; do
              [ -e "$f" ] && [ "$f" -nt "$pack_file" ] && { echo "$f"; break; }
            done)
  [ -n "$newer" ]
}

selected () {
  [ ${#SELECT[@]} -eq 0 ] && return 0
  local n
  for n in "${SELECT[@]}"; do [ "$n" = "$1" ] && return 0; done
  return 1
}

pack () {
  local name="$1" include="$2" scope="$3"
  local pack_file="$OUT/pack_${name}.md"

  selected "$name" || return 0

  if [ "$FORCE" -eq 0 ] && ! is_stale "$pack_file" "$scope"; then
    printf 'pack_%-12s  up to date\n' "$name"
    return 0
  fi

  echo "=== regenerating pack_${name} ==="
  repomix \
    --style markdown \
    --compress \
    --remove-comments \
    --remove-empty-lines \
    --include "$include" \
    --ignore "$COMMON_IGNORE" \
    --output "$pack_file" \
    --no-security-check \
    --top-files-len 5 \
    2>&1 | grep -E "Total (Files|Tokens)" || true
}

#     name           include-pattern (for repomix)                                                         scope-dirs (for staleness check)
pack  python         "Libraries/**,Scripts/**,*.py"                                                        "Libraries Scripts"
pack  analyze        "Analyze/**"                                                                          "Analyze"
pack  calibration    "Calibration/**"                                                                      "Calibration"
pack  lambda         "Lambda/**"                                                                           "Lambda"
pack  frontend       "Src/src/**,Src/webpack.config.js,Src/package.json,Src/jsconfig.json,Src/README.md"   "Src/src Src/webpack.config.js Src/package.json Src/jsconfig.json Src/README.md"
pack  firmware       "Arduino/**"                                                                          "Arduino"
pack  ios            "iOS/**"                                                                              "iOS"
