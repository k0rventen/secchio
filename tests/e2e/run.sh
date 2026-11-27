#!/usr/bin/env bash
# End-to-end test for secchio. run with bash tests/e2e/run.sh
# add KEEP=1 to keep the cluster after failure for debbuging

set -euo pipefail
cd "$(dirname "$0")/../.."

CLUSTER="${CLUSTER:-secchio-e2e}"
OPERATOR_IMAGE="${OPERATOR_IMAGE:-secchio-operator:e2e}"
RESTIC_IMAGE="${RESTIC_IMAGE:-secchio-restic:e2e}"
NS=secchio-e2e
SCHEDULE=e2e-schedule
CRONJOB="$SCHEDULE-backup-e2e-data-cronjob"
API=http://127.0.0.1:18000

k() { kubectl --context "kind-$CLUSTER" "$@"; }

wait_for_snapshots() {
  for _ in $(seq 1 60); do
    ids=$(k -n "$NS" get backupschedule "$SCHEDULE" -o jsonpath='{.status.backups[*].snapshots[*].id}' 2>/dev/null || true)
    if [ "$(echo "$ids" | wc -w | tr -d ' ')" -ge "$1" ]; then return 0; fi
    sleep 1
  done
  return 1
}

# Seed /data with files covering a spread of modes and owners. Restoring them
# exercises restic's lchown/chmod/chtimes path: without CAP_CHOWN and CAP_FOWNER
# the restore pod fails with "operation not permitted".
seed_permissions() {
  k -n "$NS" exec deploy/e2e-workload -- sh -c '
    set -eu
    rm -rf /data/perms
    mkdir -p /data/perms/private /data/perms/shared /data/perms/locked /data/perms/sticky
    printf "version-1\n" > /data/data.txt

    printf "root 600\n"   > /data/perms/private/root-600.txt
    printf "nobody 400\n" > /data/perms/private/nobody-400.txt
    printf "inside 700\n" > /data/perms/private/inside.txt
    printf "user 644\n"   > /data/perms/shared/user-644.txt
    printf "world 777\n"  > /data/perms/shared/world-777.txt
    printf "inside 555\n" > /data/perms/shared/inside.txt
    printf "locked 000\n" > /data/perms/locked/none-000.txt
    printf "inside 000\n" > /data/perms/locked/inside.txt

    # Directory ownership first, then the per-file owners below.
    chown -R 1000:1000 /data/perms/private
    chown -R 0:0 /data/perms/shared
    chown -R 1000:1000 /data/perms/locked
    chmod 700 /data/perms/private
    chmod 555 /data/perms/shared
    chmod 000 /data/perms/locked
    chmod 1777 /data/perms/sticky

    chown 0:0 /data/perms/private/root-600.txt
    chmod 600 /data/perms/private/root-600.txt
    chown 65534:65534 /data/perms/private/nobody-400.txt
    chmod 400 /data/perms/private/nobody-400.txt
    chown 1000:2000 /data/perms/shared/user-644.txt
    chmod 644 /data/perms/shared/user-644.txt
    chown 0:0 /data/perms/shared/world-777.txt
    chmod 777 /data/perms/shared/world-777.txt
    chown 1000:1000 /data/perms/locked/none-000.txt
    chmod 000 /data/perms/locked/none-000.txt

    # The symlink itself must keep its owner after the restore.
    ln -s private/root-600.txt /data/perms/link-to-private
    chown -h 1000:1000 /data/perms/link-to-private
  '
}

# "path mode uid gid" for every seeded path, sorted for a stable comparison.
capture_permissions() {
  k -n "$NS" exec deploy/e2e-workload -- sh -c '
    find /data/perms /data/data.txt | sort | while IFS= read -r path; do
      stat -c "%n %a %u %g" "$path"
    done
  '
}

# Change contents and permissions so the restore has to put them back.
mutate_permissions() {
  k -n "$NS" exec deploy/e2e-workload -- sh -c '
    set -eu
    printf "version-2\n" > /data/data.txt
    chmod 777 /data/perms/private/root-600.txt
    chown 0:0 /data/perms/shared/user-644.txt
    chmod 755 /data/perms/locked
    rm -f /data/perms/shared/world-777.txt
    rm -f /data/perms/link-to-private
  '
}

cleanup() {
  [ -n "${PF_PID:-}" ] && kill "$PF_PID" >/dev/null 2>&1 || true
  [ "${KEEP:-0}" = "1" ] || kind delete cluster --name "$CLUSTER" || true
}
trap cleanup EXIT

for tool in docker kind kubectl helm curl; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool"; exit 1; }
done

kind delete cluster --name "$CLUSTER" 2>/dev/null || true
kind create cluster --name "$CLUSTER" --wait 120s

docker build -t "$OPERATOR_IMAGE" .
docker build -t "$RESTIC_IMAGE" restic-pod/
kind load docker-image "$OPERATOR_IMAGE" "$RESTIC_IMAGE" --name "$CLUSTER"

k apply -f tests/e2e/manifests/garage.yaml
k apply -f tests/e2e/manifests/workload.yaml

helm upgrade --install secchio-operator charts/secchio \
  -n secchio-system --create-namespace --wait --timeout 1m \
  --kube-context "kind-$CLUSTER" \
  --set operator.image="${OPERATOR_IMAGE%:*}" \
  --set operator.tag="${OPERATOR_IMAGE##*:}" \
  --set restic.image="${RESTIC_IMAGE%:*}" \
  --set restic.tag="${RESTIC_IMAGE##*:}"


k -n "$NS" wait --for=condition=Ready pod -l app=e2e-workload --timeout=30s
k -n garage rollout status deploy/garage --timeout=30s

seed_permissions
before_perms=$(capture_permissions)
echo "seeded file modes and owners:"
echo "$before_perms"

k apply -f tests/e2e/manifests/schedule.yaml

k -n "$NS" wait --for=create "cronjob/$CRONJOB" --timeout=20s

k -n "$NS" create job --from="cronjob/$CRONJOB" e2e-scheduled-backup
echo "waiting for the scheduled backup..."
k -n "$NS" wait --for=condition=complete job/e2e-scheduled-backup --timeout=20s
wait_for_snapshots 1 || { echo "no snapshot after the scheduled backup"; exit 1; }
first_snapshot=$(k -n "$NS" get backupschedule "$SCHEDULE" -o jsonpath='{.status.backups[0].snapshots[0].id}')
echo "first snapshot: $first_snapshot"

mutate_permissions

k -n secchio-system port-forward svc/secchio-ui 18000:8000 >/dev/null 2>&1 &
PF_PID=$!
sleep 1

job_name=$(curl -sf -X POST "$API/api/schedules/$NS/$SCHEDULE/trigger" \
  | sed -n 's/.*"job_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
[ -n "$job_name" ] || { echo "failed to trigger the manual backup"; exit 1; }
echo "waiting for the manual backup..."
k -n "$NS" wait --for=condition=complete "job/$job_name" --timeout=20s
wait_for_snapshots 2 || { echo "manual backup did not create a second snapshot"; exit 1; }
snapshots=$(k -n "$NS" get backupschedule "$SCHEDULE" -o jsonpath='{.status.backups[0].snapshots[*].id}')
echo "$snapshots" | grep -qw "$first_snapshot" || { echo "first snapshot disappeared"; exit 1; }
echo "snapshots: $snapshots"

restore_name=$(curl -sf -X POST "$API/api/restores" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":\"$NS\",\"schedule_name\":\"$SCHEDULE\",\"pvc_name\":\"e2e-data\",\"snapshot_id\":\"$first_snapshot\"}" \
  | sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
[ -n "$restore_name" ] || { echo "failed to create the restore"; exit 1; }

echo "waiting for the restore..."
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Completed "restore/$restore_name" --timeout=20s

content=$(k -n "$NS" exec deploy/e2e-workload -- cat /data/data.txt)
[ "$content" = "version-1" ] || { echo "expected version-1, got '$content'"; exit 1; }
echo "OK: restored data is version-1"

after_perms=$(capture_permissions)
if [ "$before_perms" != "$after_perms" ]; then
  echo "file modes/ownership were not restored:"
  diff -u <(printf '%s\n' "$before_perms") <(printf '%s\n' "$after_perms") || true
  exit 1
fi
echo "OK: file modes and ownership survived the restore"

locked=$(k -n "$NS" exec deploy/e2e-workload -- cat /data/perms/locked/inside.txt)
[ "$locked" = "inside 000" ] || { echo "expected 'inside 000', got '$locked'"; exit 1; }
echo "OK: files inside a mode-000 directory are readable after the restore"

echo 'e2e tests OK !'
