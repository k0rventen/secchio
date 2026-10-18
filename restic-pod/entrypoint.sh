#!/bin/sh
#
# secchio restic entrypoint.
#
# Configuration via env vars:
#   SECCHIO_MODE          backup | restore
#   SECCHIO_INSECURE_TLS  "true" to add --insecure-tls to every restic call
#   SECCHIO_RETENTION     backup only: restic forget flags, e.g. "--keep-daily 7"
#   SECCHIO_SNAPSHOT      restore only: snapshot id or "latest" (default)
#
# Restic connection details come from the usual RESTIC_* / AWS_* env vars.
set -eu
# BusyBox ash (Alpine's /bin/sh) supports pipefail; ShellCheck's POSIX
# dialect does not know that.
# shellcheck disable=SC3040
set -o pipefail

TLS_FLAG=""
[ "${SECCHIO_INSECURE_TLS:-false}" = "true" ] && TLS_FLAG="--insecure-tls"

if [ "${SECCHIO_MODE:-}" = "backup" ]; then
  timeout 10s restic $TLS_FLAG snapshots --json >/dev/null 2>&1 || restic $TLS_FLAG init

  restic $TLS_FLAG backup /data --quiet --no-scan --json > /tmp/backup.json
  cat /tmp/backup.json
  SNAP_ID="$(jq -r 'select(.message_type == "summary") | .snapshot_id' /tmp/backup.json)"
  # restic's summary reports the full 64-char id; use the short form for display.
  SNAP_SHORT="$(printf '%.8s' "$SNAP_ID")"

  if [ -n "${SECCHIO_RETENTION:-}" ]; then
    # SECCHIO_RETENTION is a space-separated flag list and must word-split.
    # shellcheck disable=SC2086
    restic $TLS_FLAG forget --prune $SECCHIO_RETENTION \
      || echo "warning: restic forget failed; backup snapshot was still created" >&2
  fi

  # One SNAP=<short-id> <time> line per snapshot (newest first), so a truncated
  # termination log only loses the oldest entries rather than the whole list.
  SNAPS_JSON="$(restic $TLS_FLAG snapshots --json)"
  {
    printf 'SNAPSHOT_ID=%s\n' "$SNAP_SHORT"
    printf '%s' "$SNAPS_JSON" | jq -r 'sort_by(.time) | reverse | .[] | "SNAP=\(.short_id) \(.time) \(.summary.total_bytes_processed)"'
  } > /dev/termination-log

elif [ "${SECCHIO_MODE:-}" = "restore" ]; then
  SNAP="${SECCHIO_SNAPSHOT:-latest}"
  # resolve the snapshot first: this checks the backend is reachable and that
  # the snapshot exists, so we fail fast instead of mid-restore
  RESOLVED="$(timeout 10s restic $TLS_FLAG snapshots "$SNAP" --json | jq -r '.[0].short_id')"
  [ -n "$RESOLVED" ] || { echo "snapshot '$SNAP' not found in repository" >&2; exit 1; }
  rm -rf data/*
  restic $TLS_FLAG restore "$RESOLVED" --quiet --delete --target / -i /data --json
  printf 'RESTORED_SNAPSHOT_ID=%s\n' "$RESOLVED" > /dev/termination-log

else
  echo "SECCHIO_MODE must be 'backup' or 'restore' (got '${SECCHIO_MODE:-}')" >&2
  exit 2
fi
