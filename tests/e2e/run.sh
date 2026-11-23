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

k -n "$NS" exec deploy/e2e-workload -- sh -c 'echo version-1 > /data/data.txt'

k apply -f tests/e2e/manifests/schedule.yaml

k -n "$NS" wait --for=create "cronjob/$CRONJOB" --timeout=20s

k -n "$NS" create job --from="cronjob/$CRONJOB" e2e-scheduled-backup
echo "waiting for the scheduled backup..."
k -n "$NS" wait --for=condition=complete job/e2e-scheduled-backup --timeout=20s
wait_for_snapshots 1 || { echo "no snapshot after the scheduled backup"; exit 1; }
first_snapshot=$(k -n "$NS" get backupschedule "$SCHEDULE" -o jsonpath='{.status.backups[0].snapshots[0].id}')
echo "first snapshot: $first_snapshot"

k -n "$NS" exec deploy/e2e-workload -- sh -c 'echo version-2 > /data/data.txt'

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
echo 'e2e tests OK !'
