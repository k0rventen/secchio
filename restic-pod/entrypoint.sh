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
set -o pipefail

TLS_FLAG=""
[ "${SECCHIO_INSECURE_TLS:-false}" = "true" ] && TLS_FLAG="--insecure-tls"

if [ "${SECCHIO_MODE:-}" = "backup" ]; then
  timeout 10s restic $TLS_FLAG snapshots --json >/dev/null 2>&1 || restic $TLS_FLAG init

  restic $TLS_FLAG backup /data --quiet --no-scan --json > /tmp/backup.json
  cat /tmp/backup.json
  SNAP_ID="$(jq -r 'select(.message_type == "summary") | .snapshot_id' /tmp/backup.json)"

  if [ -n "${SECCHIO_RETENTION:-}" ]; then
    restic $TLS_FLAG forget --prune $SECCHIO_RETENTION
  fi

  ALL_SNAPS="$(restic $TLS_FLAG snapshots --json | jq -r '[.[].short_id] | join(",")')"
  printf 'SNAPSHOT_ID=%s\nALL_SNAPSHOTS=%s\n' "$SNAP_ID" "$ALL_SNAPS" > /dev/termination-log

elif [ "${SECCHIO_MODE:-}" = "restore" ]; then
  SNAP="${SECCHIO_SNAPSHOT:-latest}"
  # resolve the snapshot first: this checks the backend is reachable and that
  # the snapshot exists, so we fail fast instead of mid-restore
  RESOLVED="$(timeout 10s restic $TLS_FLAG snapshots "$SNAP" --json | jq -r '.[0].short_id')"
  [ -n "$RESOLVED" ] || { echo "snapshot '$SNAP' not found in repository" >&2; exit 1; }
  restic $TLS_FLAG restore "$RESOLVED" --delete --target / -i /data --json
  printf 'RESTORED_SNAPSHOT_ID=%s\n' "$RESOLVED" > /dev/termination-log

else
  echo "SECCHIO_MODE must be 'backup' or 'restore' (got '${SECCHIO_MODE:-}')" >&2
  exit 2
fi
