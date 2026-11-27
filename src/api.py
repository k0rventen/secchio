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
    CRD_PLURAL_BACKUPSCHEDULES,
    CRD_PLURAL_RESTORES,
    CRD_VERSION,
    JOB_TTL_SECONDS,
    LABEL_RESTORE,
    LABEL_SCHEDULE,
    OPERATOR_VERSION,
    RESTIC_IMAGE,
    RESTIC_RESOURCES,
    make_cronjob_name,
    make_restore_labels,
)
from crds import BackupSchedule, Restore, RestoreSpec
from k8s import (
    get_backup_schedule,
    get_batch_api,
    get_custom_objects_api,
    list_cronjobs_for_schedule,
    list_custom_objects,
)
from workloads import WorkloadConfig, create_scan_job

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


def _get_schedule_or_404(
    namespace: str, schedule_name: str, *, require_backups: bool = False
) -> tuple[BackupSchedule, dict[str, Any]]:
    """Read a BackupSchedule, raising 404 when it (or its backups) is missing."""
    item = get_backup_schedule(get_custom_objects_api(), schedule_name, namespace)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"BackupSchedule '{schedule_name}' not found in namespace '{namespace}'",
        )
    schedule = BackupSchedule.from_crd(item)
    if require_backups and not schedule.status.backups:
        raise HTTPException(
            status_code=404,
            detail=f"No backups found for schedule '{schedule_name}' in namespace '{namespace}'",
        )
    return schedule, item


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
    for item in list_custom_objects(get_custom_objects_api(), CRD_PLURAL_BACKUPSCHEDULES):
        schedule = BackupSchedule.from_crd(item)
        views.append(
            {
                "name": schedule.name,
                "namespace": schedule.namespace,
                "schedule": schedule.spec.schedule,
                "time_zone": schedule.spec.time_zone,
                "suspend": schedule.spec.suspend,
                "repo": schedule.spec.repo.model_dump(by_alias=True),
                "retention": schedule.spec.retention.model_dump(by_alias=True),
                "status": schedule.status.model_dump(
                    by_alias=True, mode="json", exclude={"backups"}
                ),
                "backups": [entry.model_dump(mode="json") for entry in schedule.status.backups],
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

    if cronjob.spec and cronjob.spec.suspend:
        raise HTTPException(
            status_code=409,
            detail=f"CronJob '{cronjob_name}' is suspended",
        )

    template = cronjob.spec.job_template if cronjob.spec else None
    if template is None or template.spec is None:
        raise HTTPException(status_code=500, detail="CronJob template spec is missing")
    job_spec = template.spec
    pod_spec = job_spec.template.spec if job_spec.template else None
    for container in (pod_spec.containers if pod_spec else None) or []:
        for env in container.env or []:
            if env.name == "SECCHIO_START_DELAY":
                env.value = "0"

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
    schedule, _ = _get_schedule_or_404(namespace, schedule_name, require_backups=True)

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


def _create_scan_job(
    namespace: str,
    schedule_name: str,
    pvc_name: str,
    schedule: BackupSchedule,
    uid: str,
) -> str:
    """Create a Job that lists a PVC's snapshots and return its name."""
    job_name = f"{make_cronjob_name(schedule_name, pvc_name)[:40]}-scan-{int(time.time())}"
    config = WorkloadConfig(
        name=job_name,
        namespace=namespace,
        pvc_name=pvc_name,
        repo=schedule.spec.repo,
        image=RESTIC_IMAGE,
        mode="scan",
        resources=RESTIC_RESOURCES,
        schedule_name=schedule_name,
        owner_kind="BackupSchedule",
        owner_name=schedule_name,
        owner_uid=uid,
    )
    get_batch_api().create_namespaced_job(namespace=namespace, body=create_scan_job(config))
    return job_name


@app.post("/api/schedules/{namespace}/{schedule_name}/scan")
async def scan_schedule(namespace: str, schedule_name: str) -> dict[str, Any]:
    """Create a scan Job per PVC to refresh the schedule's snapshot list."""
    schedule, item = _get_schedule_or_404(namespace, schedule_name, require_backups=True)
    uid = str((item.get("metadata") or {}).get("uid", ""))
    triggered: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for entry in schedule.status.backups:
        try:
            job_name = _create_scan_job(namespace, schedule_name, entry.pvc_name, schedule, uid)
            triggered.append({"pvc": entry.pvc_name, "job_name": job_name})
        except ApiException as e:
            errors.append({"pvc": entry.pvc_name, "error": str(e)})

    return {
        "status": "triggered",
        "message": (
            f"Triggered {len(triggered)}/{len(schedule.status.backups)} snapshot scans "
            f"for '{schedule_name}'"
        ),
        "triggered": triggered,
        "errors": errors,
    }


def _active_backup_jobs(namespace: str, schedule_name: str) -> list[str]:
    """Names of backup Jobs for a schedule that are still active."""
    try:
        jobs = get_batch_api().list_namespaced_job(
            namespace=namespace,
            label_selector=f"{LABEL_SCHEDULE}={schedule_name},!{LABEL_RESTORE}",
        )
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Failed to list backup jobs: {e}")
    return [
        job.metadata.name
        for job in jobs.items
        if job.metadata and job.metadata.name and job.status and job.status.active
    ]


def _set_suspend(namespace: str, schedule_name: str, suspend: bool) -> None:
    """Persist the suspend flag and apply it to the schedule's CronJobs immediately."""
    try:
        get_custom_objects_api().patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural=CRD_PLURAL_BACKUPSCHEDULES,
            name=schedule_name,
            body={"spec": {"suspend": suspend}},
        )
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Failed to update schedule: {e}")

    batch_api = get_batch_api()
    for cronjob in list_cronjobs_for_schedule(batch_api, namespace, schedule_name):
        if not (cronjob.metadata and cronjob.metadata.name):
            continue
        try:
            batch_api.patch_namespaced_cron_job(
                name=cronjob.metadata.name,
                namespace=namespace,
                body={"spec": {"suspend": suspend}},
            )
        except ApiException as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to update CronJob '{cronjob.metadata.name}': {e}",
            )


@app.post("/api/schedules/{namespace}/{schedule_name}/disaster-recovery")
async def disaster_recovery(namespace: str, schedule_name: str) -> dict[str, Any]:
    """Suspend backups and restore every PVC in the schedule from its latest snapshot."""
    schedule, _ = _get_schedule_or_404(namespace, schedule_name, require_backups=True)

    active = _active_backup_jobs(namespace, schedule_name)
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Backup job(s) still running: {', '.join(active)}. Wait for them to finish.",
        )

    _set_suspend(namespace, schedule_name, True)

    triggered: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for entry in schedule.status.backups:
        try:
            restore_name = _create_restore(
                namespace, schedule_name, entry.pvc_name, entry.pvc_name, "latest"
            )
            triggered.append({"pvc": entry.pvc_name, "restore": restore_name})
        except ApiException as e:
            errors.append({"pvc": entry.pvc_name, "error": str(e)})

    return {
        "status": "triggered",
        "message": (
            f"Backups suspended; triggered {len(triggered)}/{len(schedule.status.backups)} "
            f"restores for '{schedule_name}'"
        ),
        "triggered": triggered,
        "errors": errors,
    }


@app.post("/api/schedules/{namespace}/{schedule_name}/resume")
async def resume_schedule(namespace: str, schedule_name: str) -> dict[str, Any]:
    """Resume scheduled backups for a BackupSchedule."""
    _get_schedule_or_404(namespace, schedule_name)
    _set_suspend(namespace, schedule_name, False)
    return {
        "status": "resumed",
        "message": f"Backups resumed for '{schedule_name}' in namespace '{namespace}'",
    }


@app.get("/api/restores")
async def list_restores() -> list[dict[str, Any]]:
    """List all Restore custom resources across the cluster."""
    items = list_custom_objects(get_custom_objects_api(), CRD_PLURAL_RESTORES)
    return [_restore_view(Restore.from_crd(item)) for item in items]


def _create_restore(
    namespace: str,
    schedule_name: str,
    pvc_name: str,
    target_pvc: str,
    snapshot_id: str,
) -> str:
    """Create a Restore CR and return its name."""
    restore_name = f"restore-{pvc_name}"[:45] + f"-{int(time.time())}"
    restore = Restore(
        name=restore_name,
        namespace=namespace,
        labels=make_restore_labels(restore_name, schedule_name, pvc_name),
        spec=RestoreSpec(
            schedule_name=schedule_name,
            pvc_name=pvc_name,
            target_pvc=target_pvc,
            snapshot_id=snapshot_id or "latest",
        ),
    )
    get_custom_objects_api().create_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural=CRD_PLURAL_RESTORES,
        body=restore.to_crd_body(CRD_GROUP, CRD_VERSION),
    )
    return restore_name


@app.post("/api/restores")
async def trigger_restore(req: CreateRestoreRequest) -> dict[str, Any]:
    """Create a Restore custom resource to initiate a restore operation."""
    try:
        restore_name = _create_restore(
            req.namespace,
            req.schedule_name,
            req.pvc_name,
            req.target_pvc or req.pvc_name,
            req.snapshot_id,
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
