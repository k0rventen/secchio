# Backup Operator — Agent Instructions

## Project Overview

This is a Kubernetes backup operator built with Python and kopf. It uses restic to back up PVCs on a schedule, storing snapshots in S3-compatible storage (MinIO, AWS S3, etc.).

## Architecture

- `src/handlers.py` — kopf entrypoint (`kopf run -A handlers.py`); thin handlers delegating to `reconcile`/`status`, plus a startup hook that serves the UI
- `src/reconcile.py` — domain logic: BackupSchedules → CronJobs, Restores → Jobs
- `src/status.py` — reads Job outcomes into per-PVC entries on `BackupSchedule.status` and into `Restore` status
- `src/k8s.py` — cached API clients + patch/apply/list helpers (no domain logic)
- `src/restic.py` — restic environment construction (including the `SECCHIO_*` vars that drive the restic image's entrypoint) and termination-log parsing
- `src/workloads.py` — builds the backup CronJob and restore Job; the restic work runs in the image's entrypoint
- `src/crds.py` — pydantic models mirroring the CRD spec
- `src/common.py` — constants and pure naming/label helpers
- `src/api.py` — FastAPI UI + static frontend (`src/static/index.html`) for listing schedules (with per-PVC status) and restores, and triggering manual backups/restores
- `restic-pod/` — the restic image (Dockerfile + `entrypoint.sh`) the backup/restore pods run
- `docs/` — assets referenced by the README/chart (`icon.svg`, `ui.png`); the directory is currently gitignored, so treat those references as local-only
- `tests/` — unit tests; run the Docker test target (see Verification) before handing work back
- `charts/secchio/` — Helm chart with CRDs, RBAC, ServiceAccount, Deployment, and Service
- `tests/test-manifests.yaml` — end-to-end test setup (alpine pod + PVC + BackupSchedule + S3 secret)
- `AUDIT.md` — merged audit + roadmap; the source of truth for known issues and planned features

## CRDs

- `charts/secchio/crds/backupschedule.yaml` — BackupSchedule CRD (namespaced; status holds per-PVC entries)
- `charts/secchio/crds/restore.yaml` — Restore CRD (namespaced, same-namespace restore)

## Key Patterns

- BackupSchedules are **namespaced**: each selects PVCs in its own namespace via label selectors, and reads its credentials Secret from that namespace
- Retention runs in the same Job as the backup (no secondary pod)
- The backup/restore pods run the `restic-pod/` image. The operator only sets `SECCHIO_*` env vars; the image's `entrypoint.sh` runs restic (`--json` output parsed with `jq`) and writes the pod termination log the operator reads back
- Repo path is variabilized per PVC: `s3:<endpoint>/<pvc_namespace>/<pvc_name>` to avoid collisions
- Cron scheduling is delegated to Kubernetes: the operator only sets the CronJob's `schedule` field (no in-operator cron matching) and uses the `kubernetes` Python client to create workloads
- BackupSchedules create one CronJob per matching PVC; per-PVC status lives in `status.backups`
- Status is written from Job events (`@kopf.on.event` in `handlers.py`) and, for BackupSchedules, re-synced every 60s by `status.resync_schedule()` from the `check_schedules` timer. Restore status is still event-only (see `AUDIT.md` P1-2)
- `JOB_TTL_SECONDS` (900s) controls how long finished Jobs/pods are kept; status must be read before the TTL controller deletes them
- The operator is stateless: working state lives in the CRDs and their child CronJobs/Jobs
- The operator runs as a single Deployment with a Service exposing the FastAPI UI on port 8000
- CRD models live in `crds.py` (pydantic). A field whose CRD name is not the camelCase of its Python name (e.g. `snapshotID`) uses `aliased(...)`, **not** `Field(alias=...)` — the latter makes pyright reject the snake_case keyword despite `populate_by_name`.

## Deployment

1. Apply CRDs first: `kubectl apply -f charts/secchio/crds/`
2. Deploy operator using Helm: `helm install secchio-operator charts/secchio --namespace secchio-system --create-namespace`
3. Apply test setup: `kubectl apply -f tests/test-manifests.yaml`
4. For development: `tilt up` (uses `charts/secchio/values-tilt.yaml`)

## Development

- Python 3.14+, dependencies in `pyproject.toml`
- uv for package management, virtual env in .venv: can be activated

## Verification

The canonical way to run the full suite is the Dockerfile's `tests` target, which runs `make -f tests/Makefile verify` (lint, typecheck, pyright, shellcheck, pytest) inside the build image:

```sh
docker buildx build -t secchio-tests --target tests .
```

This is the same target CI uses (`.github/workflows/pr-test.yaml`) and needs only Docker/buildx. For faster iteration, run the individual stages locally through `uv` (the Makefile drives everything through `uv`):

| Command                            | What it does                                                  |
| ---------------------------------- | ------------------------------------------------------------- |
| `make -f tests/Makefile verify`    | lint + typecheck + pyright + shellcheck + tests (the all-in-one) |
| `make -f tests/Makefile lint`      | `ruff check` and `ruff format --check` over `src/` + `tests/`  |
| `make -f tests/Makefile typecheck` | `mypy src/` (pydantic plugin enabled)                          |
| `make -f tests/Makefile pyright`   | `pyright`                                                      |
| `make -f tests/Makefile shellcheck`| `shellcheck` the restic-pod entrypoint                         |
| `make -f tests/Makefile test`      | `pytest` over `tests/`                                        |
| `make -f tests/Makefile format`    | `ruff check --fix` + `ruff format` (auto-fix)                  |

## Known Gaps / Roadmap

`docs/AUDIT.md` is the merged audit and roadmap. Check it before starting non-trivial work: it lists the open security, correctness, and hardening items.

## When Working on This Project

- Keep changes minimal and focused — the operator is small and tightly coupled
- The CRD schema in `charts/secchio/crds/*.yaml` is the source of truth for spec fields
- `crds.py` models mirror the CRD; update both when changing the schema
- The operator runs as a single Deployment with a Service exposing the FastAPI UI on port 8000
- When you fix or discover an issue, update `AUDIT.md` (status + remediation checklist) so it stays current
