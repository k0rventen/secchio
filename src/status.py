"""Reading Job outcomes back into CR status.

A restic Job reports what happened through its pod's termination log; this module
turns a finished Job into ``Backup``/``Restore`` status and aggregates child Backup
statuses into the ``BackupSchedule``. It writes status, but never creates workloads
(that is :mod:`reconcile`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from kubernetes.client import ApiClient, CustomObjectsApi, V1Job
from kubernetes.client.exceptions import ApiException, OpenApiException

import k8s
from common import (
    CRD_GROUP,
    CRD_PLURAL_BACKUPS,
    CRD_PLURAL_BACKUPSCHEDULES,
    CRD_PLURAL_RESTORES,
    CRD_VERSION,
    LABEL_BACKUP,
    LABEL_RESTORE,
    extract_backup_name,
    extract_restore_name,
    extract_schedule_name,
    now_iso,
)
from crds import (
    Backup,
    BackupPhase,
    BackupScheduleStatus,
    BackupScheduleSummary,
    BackupStatus,
    RestorePhase,
    RestoreStatus,
    SchedulePhase,
)
from restic import extract_termination_info

logger = logging.getLogger("status")

# Bare client used only to normalize kubernetes-client objects to plain dicts.
_api_client = ApiClient()


@dataclass
class JobStatus:
    """Normalized outcome of a batch Job, from a kopf event or the API."""

    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    active: int = 0
    succeeded: int = 0
    failed: int = 0
    start_time: str = ""
    completion_time: str = ""
    failure_reason: str = ""

    @classmethod
    def from_kopf_body(cls, body: Mapping[str, Any]) -> JobStatus:
        meta = body.get("metadata", {})
        status = body.get("status", {})
        return cls(
            name=meta.get("name", ""),
            namespace=meta.get("namespace", ""),
            labels=meta.get("labels", {}) or {},
            active=status.get("active") or 0,
            succeeded=status.get("succeeded") or 0,
            failed=status.get("failed") or 0,
            start_time=status.get("startTime", ""),
            completion_time=status.get("completionTime", ""),
            failure_reason=_failed_reason(status.get("conditions") or []),
        )

    @classmethod
    def from_job(cls, job: V1Job) -> JobStatus:
        """Normalize a kubernetes-client Job object via the dict path."""
        body = cast(Mapping[str, Any], _api_client.sanitize_for_serialization(job))
        return cls.from_kopf_body(body)


def _failed_reason(conditions: Any) -> str:
    """Message of a Job's ``Failed`` condition, if any.

    Accepts both kopf's raw dicts and the kubernetes client's condition objects.
    """
    for cond in conditions or []:
        if isinstance(cond, dict):
            if cond.get("type") == "Failed" and cond.get("status") == "True":
                return cond.get("message") or cond.get("reason") or ""
        elif cond.type == "Failed" and cond.status == "True":
            return cond.message or cond.reason or ""
    return ""


def _job_creation_time(job: V1Job) -> float:
    """Sort key for picking the newest Job; missing timestamps sort first."""
    meta = job.metadata
    if meta and meta.creation_timestamp:
        return meta.creation_timestamp.timestamp()
    return 0.0


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or ``+00:00`` offset).

    Kubernetes emits ``...Z`` while :func:`common.now_iso` emits ``...+00:00``;
    comparing the raw strings would mis-order the same instant, so callers compare
    the parsed values instead. Returns ``None`` for empty or unparseable input.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def job_status(job: JobStatus) -> None:
    """Route a Job outcome to the Backup or Restore resource that owns it."""
    if not job.namespace:
        return
    if restore_name := extract_restore_name(job.labels):
        _sync_restore_from_job(job, restore_name)
    elif backup_name := extract_backup_name(job.labels):
        _sync_backup_from_job(job, backup_name)


def backup_status(name: str, namespace: str) -> None:
    """Sync a Backup's status from its most recent Job."""
    try:
        # Restore Jobs also carry LABEL_BACKUP (the source Backup), so exclude them
        # explicitly; otherwise the newest Restore Job could shadow the real one.
        jobs = k8s.get_batch_api().list_namespaced_job(
            namespace=namespace,
            label_selector=f"{LABEL_BACKUP}={name},!{LABEL_RESTORE}",
        )
        if not jobs.items:
            return
        job_status(JobStatus.from_job(max(jobs.items, key=_job_creation_time)))
    except (ApiException, OpenApiException, ValueError, OSError) as e:
        logger.warning("Failed to sync backup status from jobs for %s: %s", name, e)


def _sync_restore_from_job(job: JobStatus, restore_name: str) -> None:
    """Update a Restore from its Job's outcome."""
    info = extract_termination_info(job.name, job.namespace)
    snapshot = info.restored_snapshot_id or info.snapshot_id

    if job.succeeded:
        status = RestoreStatus(
            phase=RestorePhase.COMPLETED,
            completion_time=job.completion_time or now_iso(),
            snapshot_id=snapshot,
            message="Restore completed successfully",
        )
    elif job.failed:
        reason = info.error or job.failure_reason or "Restore job failed"
        status = RestoreStatus(
            phase=RestorePhase.FAILED,
            completion_time=now_iso(),
            snapshot_id=snapshot,
            message=f"Restore failed: {reason}",
        )
    elif job.active:
        status = RestoreStatus(
            phase=RestorePhase.RUNNING,
            start_time=job.start_time or now_iso(),
            snapshot_id=snapshot,
            message=f"Restore job {job.name} is running",
        )
    else:
        return

    k8s.patch_status(
        k8s.get_custom_objects_api(),
        plural=CRD_PLURAL_RESTORES,
        name=restore_name,
        namespace=job.namespace,
        status=status,
    )


def _sync_backup_from_job(job: JobStatus, backup_name: str) -> None:
    """Update a Backup, and the BackupSchedule that owns it, from the Job's outcome."""
    custom_api = k8s.get_custom_objects_api()
    schedule_name = extract_schedule_name(job.labels)

    if job.succeeded:
        info = extract_termination_info(job.name, job.namespace)
        status = BackupStatus(
            phase=BackupPhase.COMPLETED,
            last_backup_time=job.completion_time or now_iso(),
            snapshots=info.snapshots,
            message="Backup completed successfully",
        )
    elif job.failed:
        info = extract_termination_info(job.name, job.namespace)
        reason = info.error or job.failure_reason or "Backup job failed"
        status = BackupStatus(
            phase=BackupPhase.FAILED,
            last_backup_time=job.start_time or now_iso(),
            snapshots=info.snapshots,
            message=f"Backup failed: {reason}",
        )
    elif job.active:
        status = BackupStatus(
            phase=BackupPhase.RUNNING,
            last_backup_time=job.start_time,
            message=f"Backup job {job.name} is running",
        )
    else:
        return

    _update_backup_status(custom_api, backup_name, job.namespace, status)
    if schedule_name:
        reconcile_schedule_status(custom_api, schedule_name)


def _update_backup_status(
    custom_api: CustomObjectsApi,
    name: str,
    namespace: str,
    status: BackupStatus,
) -> None:
    """Patch a Backup's status, keeping snapshot history when the new status has none."""
    if not status.snapshots:
        previous = _read_backup_status(custom_api, name, namespace)
        status.snapshots = previous.snapshots

    k8s.patch_status(
        custom_api,
        plural=CRD_PLURAL_BACKUPS,
        name=name,
        namespace=namespace,
        status=status,
    )


def _read_backup_status(custom_api: CustomObjectsApi, name: str, namespace: str) -> BackupStatus:
    """Read a Backup's current status, or an empty one if it cannot be read."""
    try:
        obj: Any = custom_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL_BACKUPS,
            name=name,
            namespace=namespace,
        )
        if isinstance(obj, dict):
            return Backup.from_crd(obj).status
    except ApiException:
        pass
    return BackupStatus()


def reconcile_schedule_status(custom_api: CustomObjectsApi, schedule_name: str) -> None:
    """Aggregate the status of all child Backups into the BackupSchedule status."""
    items = k8s.list_backups_for_schedule(custom_api, schedule_name)
    if not items:
        status = BackupScheduleStatus(
            phase=SchedulePhase.PENDING,
            all_ok=False,
            last_backup_time="",
            message="No matching PVCs or backups found",
            summary=BackupScheduleSummary(),
        )
    else:
        total = len(items)
        healthy = 0
        failed = 0
        running = 0
        latest_backup_time = ""
        latest_backup_dt: datetime | None = None

        for item in items:
            backup = Backup.from_crd(item)
            phase = backup.status.phase
            if phase == BackupPhase.COMPLETED:
                healthy += 1
            elif phase == BackupPhase.FAILED:
                failed += 1
            elif phase == BackupPhase.RUNNING:
                running += 1

            parsed = _parse_timestamp(backup.status.last_backup_time)
            if parsed is not None and (latest_backup_dt is None or parsed > latest_backup_dt):
                latest_backup_dt = parsed
                latest_backup_time = backup.status.last_backup_time

        all_ok = total > 0 and healthy == total
        if failed > 0:
            phase = SchedulePhase.DEGRADED
            message = f"{failed}/{total} backups failed"
        elif running > 0:
            phase = SchedulePhase.RUNNING
            message = f"{running}/{total} backups running"
        elif all_ok:
            phase = SchedulePhase.HEALTHY
            message = f"All {total} backups healthy"
        else:
            phase = SchedulePhase.PENDING
            message = f"{healthy}/{total} backups completed"

        status = BackupScheduleStatus(
            phase=phase,
            all_ok=all_ok,
            last_backup_time=latest_backup_time,
            message=message,
            summary=BackupScheduleSummary(
                total_backups=total,
                healthy_backups=healthy,
                failed_backups=failed,
                running_backups=running,
            ),
        )

    k8s.patch_status(
        custom_api,
        plural=CRD_PLURAL_BACKUPSCHEDULES,
        name=schedule_name,
        status=status,
    )
