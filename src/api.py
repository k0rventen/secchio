"""FastAPI UI for the backup operator

Provides endpoints to list BackupSchedules, Backups, and Restores,
and trigger manual backups and restores (from the UI)

Frontend is served as a staticfile directly with fastapi
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
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
    CRD_PLURAL_BACKUPS,
    CRD_PLURAL_RESTORES,
    CRD_VERSION,
    LABEL_SCHEDULE,
    make_cronjob_name,
    make_restore_labels,
)
from crds import Backup, PVCSelector, Restore
from k8s import (
    get_batch_api,
    get_core_api,
    get_custom_objects_api,
    list_backup_schedules,
    list_pvcs_by_selector,
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
    backup_ref: str
    backup_namespace: str = ""
    target_pvc: str
    target_pvc_namespace: str = ""
    snapshot_id: str = "latest"


def _list_custom(plural: str) -> list[dict[str, Any]]:
    """List a cluster-scoped custom resource, or ``[]`` on failure."""
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


def _backup_view(backup: Backup) -> dict[str, Any]:
    return {
        "name": backup.name,
        "namespace": backup.namespace,
        "pvc_name": backup.spec.pvc_name,
        "pvc_namespace": backup.spec.pvc_namespace,
        "schedule_name": backup.labels.get(LABEL_SCHEDULE, ""),
        "phase": str(backup.status.phase),
        "last_backup_time": backup.status.last_backup_time,
        "snapshots": [s.model_dump() for s in backup.status.snapshots],
        "message": backup.status.message,
        "cronjob_name": make_cronjob_name(backup.name),
    }


def _restore_view(restore: Restore, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": restore.name,
        "namespace": restore.namespace,
        "target_pvc": restore.spec.target_pvc,
        "target_pvc_namespace": restore.spec.target_pvc_namespace,
        "backup_ref": restore.spec.backup_ref,
        "source_pvc": restore.spec.source_pvc,
        "source_pvc_namespace": restore.spec.source_pvc_namespace,
        "snapshot_id": restore.spec.snapshot_id,
        "phase": str(restore.status.phase),
        "start_time": restore.status.start_time,
        "completion_time": restore.status.completion_time,
        "restored_snapshot_id": restore.status.snapshot_id,
        "message": restore.status.message,
        "creation_timestamp": item.get("metadata", {}).get("creationTimestamp", ""),
    }


def _schedule_views() -> list[dict[str, Any]]:
    """BackupSchedules with their matched PVCs and associated child Backups."""
    core_api = get_core_api()

    backups_by_schedule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _list_custom(CRD_PLURAL_BACKUPS):
        backup = Backup.from_crd(item)
        backups_by_schedule[backup.labels.get(LABEL_SCHEDULE, "")].append(_backup_view(backup))

    views = []
    for item in list_backup_schedules(get_custom_objects_api()):
        spec = item.get("spec", {})
        name = item.get("metadata", {}).get("name", "")
        selector = PVCSelector.model_validate(spec.get("pvc") or {})
        matched = list_pvcs_by_selector(core_api, selector.selector)
        views.append(
            {
                "name": name,
                "pvc_selector": spec.get("pvc", {}),
                "schedule": spec.get("schedule", ""),
                "repo": spec.get("repo", {}),
                "retention": spec.get("retention", {}),
                "status": item.get("status", {}),
                "matched_pvcs": [
                    {"name": pvc_name, "namespace": pvc_namespace}
                    for pvc_name, pvc_namespace in matched
                ],
                "backups": backups_by_schedule.get(name, []),
            },
        )
    return views


@app.get("/", response_class=HTMLResponse)
async def read_root() -> str:
    """Serve the main HTML page."""
    return _INDEX_HTML


@app.get("/schedules")
@app.get("/api/schedules")
async def list_schedules() -> list[dict[str, Any]]:
    """List all BackupSchedules with matched PVCs and associated child Backups."""
    return _schedule_views()


@app.get("/schedules/{schedule_name}")
@app.get("/api/schedules/{schedule_name}")
async def get_schedule(schedule_name: str) -> dict[str, Any]:
    """Get a single BackupSchedule by name."""
    for view in _schedule_views():
        if view["name"] == schedule_name:
            return view
    raise HTTPException(status_code=404, detail="Schedule not found")


@app.get("/api/backups")
async def list_backups() -> list[dict[str, Any]]:
    """List all Backup custom resources across the cluster."""
    return [_backup_view(Backup.from_crd(item)) for item in _list_custom(CRD_PLURAL_BACKUPS)]


@app.post("/api/backups/{namespace}/{backup_name}/trigger")
async def trigger_backup(namespace: str, backup_name: str) -> dict[str, Any]:
    """Manually trigger a backup by instantiating a Job from its CronJob."""
    batch_api = get_batch_api()
    cronjob_name = make_cronjob_name(backup_name)
    try:
        cronjob = batch_api.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"CronJob '{cronjob_name}' not found for backup in namespace '{namespace}'",
            )
        raise HTTPException(status_code=500, detail=f"Failed to read CronJob: {e}")

    if not (cronjob.spec and cronjob.spec.job_template):
        raise HTTPException(status_code=500, detail="CronJob template spec is missing")
    template = cronjob.spec.job_template
    if not template.spec:
        raise HTTPException(status_code=500, detail="CronJob template spec is missing")

    # Kubernetes names are capped at 63 characters.
    manual_job_name = f"{backup_name[:40]}-manual-{int(time.time())}"
    job_labels = dict((template.metadata.labels if template.metadata else None) or {})

    job = V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=manual_job_name,
            namespace=namespace,
            labels=job_labels,
            annotations={"cronjob.kubernetes.io/instantiate": "manual"},
        ),
        spec=template.spec,
    )

    try:
        created = batch_api.create_namespaced_job(namespace=namespace, body=job)
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Failed to create backup job: {e}")
    created_name = (
        created.metadata.name if created.metadata and created.metadata.name else manual_job_name
    )
    return {
        "status": "triggered",
        "message": f"Manual backup job '{created_name}' created successfully",
        "job_name": created_name,
    }


@app.get("/api/restores")
async def list_restores() -> list[dict[str, Any]]:
    """List all Restore custom resources across the cluster."""
    return [
        _restore_view(Restore.from_crd(item), item) for item in _list_custom(CRD_PLURAL_RESTORES)
    ]


@app.post("/api/restores")
async def trigger_restore(req: CreateRestoreRequest) -> dict[str, Any]:
    """Create a Restore custom resource to initiate a restore operation."""
    custom_api = get_custom_objects_api()
    target_ns = req.target_pvc_namespace or req.backup_namespace or "default"
    target_pvc = req.target_pvc

    restore_name = f"restore-{target_pvc}"[:45] + f"-{int(time.time())}"

    restore_body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "Restore",
        "metadata": {
            "name": restore_name,
            "namespace": target_ns,
            "labels": make_restore_labels(restore_name, req.backup_ref, target_pvc),
        },
        "spec": {
            "backupRef": req.backup_ref,
            "targetPVC": target_pvc,
            "targetPVCNamespace": target_ns,
            "sourcePVCNamespace": req.backup_namespace,
            "snapshotID": req.snapshot_id or "latest",
        },
    }

    try:
        custom_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=target_ns,
            plural=CRD_PLURAL_RESTORES,
            body=restore_body,
        )
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Failed to create Restore resource: {e}")
    return {
        "status": "created",
        "message": f"Restore '{restore_name}' initiated in namespace '{target_ns}'",
        "name": restore_name,
        "namespace": target_ns,
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
