"""FastAPI UI for the backup operator.

Lists BackupSchedules (with their per-PVC status) and Restores, and triggers
manual backups/restores. The frontend is served as static files.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from kubernetes.client import V1Job, V1ObjectMeta
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel

from common import (
    CRD_GROUP,
    CRD_PLURAL_RESTORES,
    CRD_VERSION,
    JOB_TTL_SECONDS,
    OPERATOR_VERSION,
    make_restore_labels,
)
from crds import BackupSchedule, Restore, RestoreSpec
from k8s import (
    get_backup_schedule,
    get_batch_api,
    get_custom_objects_api,
    list_backup_schedules,
)

logger = logging.getLogger("api")

app = FastAPI(title="Backup Operator UI", description="UI for the backup operator")

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _load_index_html() -> str:
    """Read the UI once at startup; fall back to a placeholder if it is missing."""
    index = static_dir / "index.html"
    return index.read_text() if index.exists() else "no index.html"


_INDEX_HTML = _load_index_html()


class CreateRestoreRequest(BaseModel):
    namespace: str
    schedule_name: str
    pvc_name: str
    target_pvc: str = ""
    snapshot_id: str = "latest"


def _list_custom(plural: str) -> list[dict[str, Any]]:
    """List a custom resource across all namespaces, or ``[]`` on failure."""
    try:
        result: Any = get_custom_objects_api().list_cluster_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=plural,
        )
    except ApiException:
        logger.exception("Failed to list %s", plural)
        return []
    items = result.get("items", []) if isinstance(result, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _restore_view(restore: Restore) -> dict[str, Any]:
    return {
        "name": restore.name,
        "namespace": restore.namespace,
        "schedule_name": restore.spec.schedule_name,
        "pvc_name": restore.spec.pvc_name,
        "target_pvc": restore.spec.target_pvc or restore.spec.pvc_name,
        "snapshot_id": restore.spec.snapshot_id,
        "phase": str(restore.status.phase),
        "start_time": restore.status.start_time,
        "completion_time": restore.status.completion_time,
        "restored_snapshot_id": restore.status.snapshot_id,
        "message": restore.status.message,
    }


def _schedule_views() -> list[dict[str, Any]]:
    """BackupSchedules with their per-PVC status."""
    views = []
    for item in list_backup_schedules(get_custom_objects_api()):
        schedule = BackupSchedule.from_crd(item)
        views.append(
            {
                "name": schedule.name,
                "namespace": schedule.namespace,
                "schedule": schedule.spec.schedule,
                "time_zone": schedule.spec.time_zone,
                "repo": schedule.spec.repo.model_dump(by_alias=True),
                "retention": schedule.spec.retention.model_dump(by_alias=True),
                "status": schedule.status.model_dump(
                    by_alias=True, mode="json", exclude={"backups"}
                ),
                "backups": [
                    {
                        "pvc_name": entry.pvc_name,
                        "cronjob_name": entry.cronjob_name,
                        "phase": str(entry.phase),
                        "last_backup_time": entry.last_backup_time,
                        "snapshots": [snapshot.model_dump() for snapshot in entry.snapshots],
                        "message": entry.message,
                        "reason": entry.reason,
                    }
                    for entry in schedule.status.backups
                ],
            },
        )
    return views


@app.get("/", response_class=HTMLResponse)
async def read_root() -> str:
    """Serve the main HTML page."""
    return _INDEX_HTML


@app.get("/api/version")
async def get_version() -> dict[str, str]:
    """Return the operator version, for display in the UI."""
    return {"version": OPERATOR_VERSION}


@app.get("/api/schedules")
async def list_schedules() -> list[dict[str, Any]]:
    """List all BackupSchedules with their per-PVC status."""
    return _schedule_views()


def _trigger_backup_job(namespace: str, cronjob_name: str) -> str:
    """Instantiate a Job from a CronJob and return the Job name."""
    batch_api = get_batch_api()
    try:
        cronjob = batch_api.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"CronJob '{cronjob_name}' not found in namespace '{namespace}'",
            )
        raise HTTPException(status_code=500, detail=f"Failed to read CronJob: {e}")

    if not (cronjob.spec and cronjob.spec.job_template):
        raise HTTPException(status_code=500, detail="CronJob template spec is missing")
    template = cronjob.spec.job_template
    job_spec = template.spec
    if job_spec is None:
        raise HTTPException(status_code=500, detail="CronJob template spec is missing")

    # Kubernetes names are capped at 63 characters.
    manual_job_name = f"{cronjob_name[:40]}-manual-{int(time.time())}"
    job_labels = dict((template.metadata.labels if template.metadata else None) or {})

    job_spec.ttl_seconds_after_finished = JOB_TTL_SECONDS
    job = V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=manual_job_name,
            namespace=namespace,
            labels=job_labels,
            annotations={"cronjob.kubernetes.io/instantiate": "manual"},
        ),
        spec=job_spec,
    )

    try:
        created = batch_api.create_namespaced_job(namespace=namespace, body=job)
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Failed to create backup job: {e}")
    return created.metadata.name if created.metadata and created.metadata.name else manual_job_name


@app.post("/api/cronjobs/{namespace}/{cronjob_name}/trigger")
async def trigger_backup(namespace: str, cronjob_name: str) -> dict[str, Any]:
    """Manually trigger a backup by instantiating a Job from its CronJob."""
    created_name = _trigger_backup_job(namespace, cronjob_name)
    return {
        "status": "triggered",
        "message": f"Manual backup job '{created_name}' created successfully",
        "job_name": created_name,
    }


@app.post("/api/schedules/{namespace}/{schedule_name}/trigger")
async def trigger_schedule(namespace: str, schedule_name: str) -> dict[str, Any]:
    """Trigger a manual backup for every PVC under a BackupSchedule."""
    item = get_backup_schedule(get_custom_objects_api(), schedule_name, namespace)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"BackupSchedule '{schedule_name}' not found in namespace '{namespace}'",
        )
    schedule = BackupSchedule.from_crd(item)
    if not schedule.status.backups:
        raise HTTPException(
            status_code=404,
            detail=f"No backups found for schedule '{schedule_name}' in namespace '{namespace}'",
        )

    triggered: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for entry in schedule.status.backups:
        try:
            job_name = _trigger_backup_job(namespace, entry.cronjob_name)
            triggered.append({"pvc": entry.pvc_name, "job_name": job_name})
        except HTTPException as e:
            errors.append({"pvc": entry.pvc_name, "error": str(e.detail)})

    if not triggered:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to trigger any backups: {errors}",
        )

    return {
        "status": "triggered",
        "message": f"Triggered {len(triggered)}/{len(schedule.status.backups)} backups "
        f"for '{schedule_name}'",
        "triggered": triggered,
        "errors": errors,
    }


@app.get("/api/restores")
async def list_restores() -> list[dict[str, Any]]:
    """List all Restore custom resources across the cluster."""
    return [_restore_view(Restore.from_crd(item)) for item in _list_custom(CRD_PLURAL_RESTORES)]


@app.post("/api/restores")
async def trigger_restore(req: CreateRestoreRequest) -> dict[str, Any]:
    """Create a Restore custom resource to initiate a restore operation."""
    custom_api = get_custom_objects_api()
    target_pvc = req.target_pvc or req.pvc_name

    restore_name = f"restore-{req.pvc_name}"[:45] + f"-{int(time.time())}"
    restore = Restore(
        name=restore_name,
        namespace=req.namespace,
        labels=make_restore_labels(restore_name, req.schedule_name, req.pvc_name),
        spec=RestoreSpec(
            schedule_name=req.schedule_name,
            pvc_name=req.pvc_name,
            target_pvc=target_pvc,
            snapshot_id=req.snapshot_id or "latest",
        ),
    )

    try:
        custom_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=req.namespace,
            plural=CRD_PLURAL_RESTORES,
            body=restore.to_crd_body(CRD_GROUP, CRD_VERSION),
        )
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Failed to create Restore resource: {e}")
    return {
        "status": "created",
        "message": f"Restore '{restore_name}' initiated in namespace '{req.namespace}'",
        "name": restore_name,
        "namespace": req.namespace,
    }


@app.delete("/api/restores/{namespace}/{restore_name}")
async def delete_restore(namespace: str, restore_name: str) -> dict[str, Any]:
    """Delete a Restore custom resource (its Job is garbage-collected)."""
    try:
        get_custom_objects_api().delete_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural=CRD_PLURAL_RESTORES,
            name=restore_name,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Restore '{restore_name}' not found")
        raise HTTPException(status_code=500, detail=f"Failed to delete Restore: {e}")
    return {"status": "deleted", "message": f"Restore '{restore_name}' deleted"}
