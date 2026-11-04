#!/usr/bin/env bash
# Mirror historical releases from this machine instead of GitHub Actions.
#
# Each mirror is processed in batches of --limit releases, publishes index.json
# through the contents API, and deletes its downloads and builds when it is
# done.  Rerunning resumes: current releases are skipped without downloading.
#
#   tools/local-backfill.sh
#   tools/local-backfill.sh --flush
#   tools/local-backfill.sh --mirrors rondo-mirror,better-y-mirror
#   tools/local-backfill.sh --limit 3 --root /tmp/inni-backfill
#
# Requires an authenticated `gh` with push access to the mirror repositories.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORG="${ORG:-y1-community}"
WORK="${WORK:-${TMPDIR:-/tmp}/inni-backfill}"
LIMIT="${LIMIT:-5}"
MIRRORS="${MIRRORS:-}"
FLUSH="${FLUSH:-0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --mirrors) MIRRORS="${2:?missing value}"; shift 2 ;;
    --limit) LIMIT="${2:?missing value}"; shift 2 ;;
    --root) WORK="${2:?missing value}"; shift 2 ;;
    --org) ORG="${2:?missing value}"; shift 2 ;;
    --flush) FLUSH=1; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not authenticated" >&2
  exit 1
fi

mkdir -p "$WORK"

mirrors() {
  if [ -n "$MIRRORS" ]; then
    printf '%s\n' "${MIRRORS//,/$'\n'}"
  else
    gh repo list "$ORG" --limit 200 --json name --jq '.[].name' | grep -- '-mirror$'
  fi
}

free_gib() {
  df -Pk "$WORK" | awk 'NR==2 {print int($4 / 1048576)}'
}

flush_mirror() {
  mirror="$1"
  deleted=0
  failed=0
  echo "[$mirror] flushing generated portable assets"
  if ! listing=$(gh api --paginate "repos/$ORG/$mirror/releases?per_page=100" \
    --jq '.[] | .tag_name as $tag | .assets[]? | [$tag, .name] | @tsv'); then
    echo "[$mirror] could not list release assets" >&2
    return 1
  fi
  while IFS=$'\t' read -r tag name; do
    [ -n "$tag" ] && [ -n "$name" ] || continue
    case "$name" in
      *-windows.zip|*-windows-arm64.zip|*-linux.zip|*-linux-arm64.zip|*-macos.zip|stats.txt|release-button-*.svg)
        if gh release delete-asset -R "$ORG/$mirror" "$tag" "$name" -y >/dev/null 2>&1; then
          deleted=$((deleted + 1))
        else
          # A concurrent run or a transient API error should not skip the
          # whole mirror.  The sync below uploads with --clobber and replaces
          # any generated asset that is still present.
          echo "[$mirror] warning: could not delete $tag/$name; sync will replace it" >&2
          failed=$((failed + 1))
        fi
        ;;
    esac
  done <<< "$listing"
  echo "[$mirror] flush deleted $deleted asset(s); $failed warning(s)"
  # Listing failures are fatal above; individual deletes are recoverable by
  # the following --clobber upload.
  return 0
}

sync_mirror() {
  mirror="$1"
  dir="$WORK/$mirror"
  mkdir -p "$dir"
  gh api "repos/$ORG/$mirror/contents/mirror.config.json" --jq .content \
    | base64 -d >"$dir/mirror.config.json"
  source=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_repo"])' "$dir/mirror.config.json")
  if [ "$FLUSH" -eq 1 ]; then
    flush_mirror "$mirror" || return 1
  fi
  count=$(gh release list -R "$source" --limit 400 --json tagName --jq 'length')
  echo "[$mirror] $source: $count release(s)"

  mirror_failures=0
  offset=0
  while [ "$offset" -lt "$count" ]; do
    if [ "$(free_gib)" -lt 6 ]; then
      echo "[$mirror] less than 6 GiB free; stopping" >&2
      return 1
    fi
    echo "[$mirror] syncing releases $((offset + 1))..$((offset + LIMIT))"
    if ! python3 "$ROOT_DIR/tools/portable_mirror.py" sync \
      --config "$dir/mirror.config.json" \
      --index "$dir/index.json" \
      --json-report "$dir/mirror-report.json" \
      --publish-index \
      --limit "$LIMIT" --offset "$offset" \
      --work-dir "$dir/work" >>"$dir/sync.log" 2>&1; then
      # Keep going: one oversized or broken release should not block the rest
      # of the backlog.  The failure stays in the log and the exit status.
      echo "[$mirror] batch at offset $offset had failures; see $dir/sync.log" >&2
      mirror_failures=$((mirror_failures + 1))
    fi
    offset=$((offset + LIMIT))
  done

  # Releases already clean up their own downloads; the work root only holds the
  # fixture that lets reruns resume cheaply.
  rm -rf "$dir/work"
  if [ "$mirror_failures" -ne 0 ]; then
    echo "[$mirror] done with $mirror_failures failed batch(es)" >&2
    return 1
  fi
  echo "[$mirror] done"
}

failures=0
for mirror in $(mirrors); do
  if ! sync_mirror "$mirror"; then
    failures=$((failures + 1))
  fi
done

if [ "$failures" -ne 0 ]; then
  echo "$failures mirror(s) failed; logs are in $WORK" >&2
  exit 1
fi
