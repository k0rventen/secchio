#!/bin/sh
#
# secchio restic entrypoint.
# Configuration via env vars (plus the usual RESTIC_* / AWS_* env vars.) :
#   SECCHIO_MODE          backup | restore | scan
#   SECCHIO_INSECURE_TLS  "true" to add --insecure-tls to every restic call
#   SECCHIO_RETENTION     backup only: restic forget flags, e.g. "--keep-daily 7"
#   SECCHIO_SNAPSHOT      restore only: snapshot id or "latest" (default)
#   SECCHIO_START_DELAY   backup only: seconds to wait before starting (spread)
#
# Alpine's /bin/sh supports pipefail
# shellcheck disable=SC3040
set -euo pipefail

TLS_FLAG=""
[ "${SECCHIO_INSECURE_TLS:-false}" = "true" ] && TLS_FLAG="--insecure-tls"

# Spread scheduled backups so dozens of pods don't hit the repo at once.
START_DELAY="${SECCHIO_START_DELAY:-0}"
if [ "$START_DELAY" -gt 0 ]; then
  echo "waiting ${START_DELAY}s before starting (spread)"
  sleep "$START_DELAY"
fi

# unlock the repo just in case. if it fails this wont raise an error
restic $TLS_FLAG unlock || true

# grab everything in /data (mounted pvc) and backup
if [ "${SECCHIO_MODE:-}" = "backup" ]; then
  timeout 10s restic $TLS_FLAG snapshots --json >/dev/null 2>&1 || restic $TLS_FLAG init

  restic $TLS_FLAG backup /data --quiet --no-scan --json > /tmp/backup.json
  cat /tmp/backup.json
  SNAP_ID="$(jq -r '.snapshot_id' /tmp/backup.json)"
  # restic's summary reports the full 64-char id; use the short form for display.
  SNAP_SHORT="$(printf '%.8s' "$SNAP_ID")"

  if [ -n "${SECCHIO_RETENTION:-}" ]; then
    # SECCHIO_RETENTION is a space-separated flag list and must word-split.
    # shellcheck disable=SC2086
    restic $TLS_FLAG forget --prune $SECCHIO_RETENTION
  fi

  # formatted ouput for the termination-log to be read by the operator
  SNAPS_JSON="$(restic $TLS_FLAG --json snapshots )"
  {
    printf 'SNAPSHOT_ID=%s\n' "$SNAP_SHORT"
    printf '%s' "$SNAPS_JSON" | jq -r 'sort_by(.time) | reverse | .[] | "SNAP=\(.short_id) \(.time) \(.summary.total_bytes_processed)"'
  } > /dev/termination-log



# List the repo's snapshots so the operator can repopulate the schedule status (e.g. after an operator reinstall).
elif [ "${SECCHIO_MODE:-}" = "scan" ]; then
  restic $TLS_FLAG --json snapshots  \
    | jq -r 'sort_by(.time) | reverse | .[] | "SNAP=\(.short_id) \(.time) \(.summary.total_bytes_processed // 0)"' \
    > /dev/termination-log



# restore a given snapshot (or latest)
# we have to rm -rf beforehand otherwise we might ran out of disk
elif [ "${SECCHIO_MODE:-}" = "restore" ]; then
  SNAP="${SECCHIO_SNAPSHOT:-latest}"
  restic $TLS_FLAG --json snapshots | jq -er '[.[].short_id] | any(. == "'"$SNAP"'")'
  rm -rf data/*
  restic $TLS_FLAG restore --quiet --target / -i /data --json "$SNAP"
  printf 'RESTORED_SNAPSHOT_ID=%s\n' "$SNAP" > /dev/termination-log
fi
