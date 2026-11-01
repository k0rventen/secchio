"""Reading Job outcomes back into CR status.

A restic Job reports what happened through its pod's termination log; this module
turns a finished Job into a per-PVC entry on the owning ``BackupSchedule`` (or a
``Restore`` status). It writes status, but never creates workloads.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from kubernetes.client import ApiClient, CustomObjectsApi, V1Job
from kubernetes.client.exceptions import ApiException, OpenApiException

import k8s
from common import (
    CRD_PLURAL_BACKUPSCHEDULES,
    CRD_PLURAL_RESTORES,
    LABEL_PVC_NAME,
    LABEL_RESTORE,
    LABEL_SCHEDULE,
    extract_pvc_name,
    extract_restore_name,
    extract_schedule_name,
    make_cronjob_name,
    now_iso,
)
from crds import (
    BackupEntry,
    BackupSchedule,
    BackupScheduleStatus,
    BackupScheduleSummary,
    Phase,
    RestoreStatus,
    SchedulePhase,
)
from restic import TerminationInfo, extract_termination_info

logger = logging.getLogger("status")

# Bare client used only to normalize kubernetes-client objects to plain dicts.
_api_client = ApiClient()

# Serialize read-modify-write cycles on a schedule's status. This is process-local
# coordination only; the operator keeps no durable state.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def schedule_lock(namespace: str, schedule_name: str) -> threading.Lock:
    """Return the process-local lock that serializes a schedule's status writes."""
    key = f"{namespace}/{schedule_name}"
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


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
    """Message of a Job's ``Failed`` condition, if any."""
    for cond in conditions or []:
        if cond.get("type") == "Failed" and cond.get("status") == "True":
            return cond.get("message") or cond.get("reason") or ""
    return ""


def _job_creation_time(job: V1Job) -> float:
    """Sort key for picking the newest Job; missing timestamps sort first."""
    meta = job.metadata
    if meta and meta.creation_timestamp:
        return meta.creation_timestamp.timestamp()
    return 0.0


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or ``+00:00`` offset)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def job_event(job: JobStatus) -> None:
    """Route a Job event to the CR that owns it.

    A PVC can have several Jobs (scheduled and manual). An event for an older Job
    (for example its TTL deletion, or a replay after an operator restart) must not
    overwrite a newer run, so backup events re-sync from the newest Job.
    """
    if not job.namespace:
        return
    if restore_name := extract_restore_name(job.labels):
        _sync_restore_from_job(job, restore_name)
    elif schedule_name := extract_schedule_name(job.labels):
        pvc_name = extract_pvc_name(job.labels)
        if pvc_name:
            sync_backup_entry(schedule_name, job.namespace, pvc_name)


def sync_backup_entry(schedule_name: str, namespace: str, pvc_name: str) -> None:
    """Sync one schedule entry from the newest Job for that PVC."""
    with schedule_lock(namespace, schedule_name):
        try:
            jobs = k8s.get_batch_api().list_namespaced_job(
                namespace=namespace,
                label_selector=(
                    f"{LABEL_SCHEDULE}={schedule_name},{LABEL_PVC_NAME}={pvc_name},!{LABEL_RESTORE}"
                ),
            )
        except (ApiException, OpenApiException, ValueError, OSError) as e:
            logger.warning("Failed to list jobs for %s/%s: %s", schedule_name, pvc_name, e)
            return
        if not jobs.items:
            return

        job = JobStatus.from_job(max(jobs.items, key=_job_creation_time))
        entry = _entry_from_job(job, schedule_name, pvc_name)
        if entry is not None:
            _patch_schedule_entry(schedule_name, namespace, entry)


def _entry_from_job(job: JobStatus, schedule_name: str, pvc_name: str) -> BackupEntry | None:
    """Build the per-PVC status entry from a Job outcome."""
    entry = BackupEntry(
        pvc_name=pvc_name,
        cronjob_name=make_cronjob_name(schedule_name, pvc_name),
    )
    if job.succeeded:
        info = extract_termination_info(job.name, job.namespace)
        entry.phase = Phase.COMPLETED
        entry.last_backup_time = job.completion_time or now_iso()
        entry.snapshots = info.snapshots
        entry.message = "Backup completed successfully"
    elif job.failed:
        info = extract_termination_info(job.name, job.namespace)
        entry.phase = Phase.FAILED
        entry.last_backup_time = job.start_time or now_iso()
        entry.snapshots = info.snapshots
        entry.message = f"Backup failed: {_failure_reason(info, job, 'Backup job failed')}"
        entry.reason = info.waiting_reason
    elif job.active:
        entry.phase = Phase.RUNNING
        entry.last_backup_time = job.start_time
        entry.message = f"Backup job {job.name} is running"
    else:
        # Pending/unknown Job: keep the previous entry rather than resetting it.
        return None
    return entry


def _patch_schedule_entry(schedule_name: str, namespace: str, entry: BackupEntry) -> None:
    """Replace one entry in the schedule status and recompute the aggregate."""
    custom_api = k8s.get_custom_objects_api()
    item = k8s.get_backup_schedule(custom_api, schedule_name, namespace)
    if item is None:
        return

    schedule = BackupSchedule.from_crd(item)
    entries = {existing.pvc_name: existing for existing in schedule.status.backups}
    entries[entry.pvc_name] = entry
    k8s.patch_status(
        custom_api,
        plural=CRD_PLURAL_BACKUPSCHEDULES,
        name=schedule_name,
        namespace=namespace,
        status=aggregate_schedule_status(entries.values()),
    )


def _failure_reason(info: TerminationInfo, job: JobStatus, fallback: str) -> str:
    """The most specific failure reason available for a failed Job."""
    if info.error:
        return info.error
    if info.waiting_message:
        if info.waiting_reason:
            return f"{info.waiting_reason}: {info.waiting_message}"
        return info.waiting_message
    if info.waiting_reason:
        return info.waiting_reason
    return job.failure_reason or fallback


def _sync_restore_from_job(job: JobStatus, restore_name: str) -> None:
    """Update a Restore from its Job's outcome."""
    info = extract_termination_info(job.name, job.namespace)
    snapshot = info.restored_snapshot_id or info.snapshot_id

    if job.succeeded:
        status = RestoreStatus(
            phase=Phase.COMPLETED,
            completion_time=job.completion_time or now_iso(),
            snapshot_id=snapshot,
            message="Restore completed successfully",
        )
    elif job.failed:
        reason = _failure_reason(info, job, "Restore job failed")
        status = RestoreStatus(
            phase=Phase.FAILED,
            completion_time=now_iso(),
            snapshot_id=snapshot,
            message=f"Restore failed: {reason}",
        )
    elif job.active:
        status = RestoreStatus(
            phase=Phase.RUNNING,
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


def aggregate_schedule_status(entries: Iterable[BackupEntry]) -> BackupScheduleStatus:
    """Compute the schedule-level status from its per-PVC entries."""
    entries = list(entries)
    if not entries:
        return BackupScheduleStatus(
            phase=SchedulePhase.PENDING,
            all_ok=False,
            last_backup_time="",
            message="No matching PVCs or backups found",
            summary=BackupScheduleSummary(),
            backups=[],
        )

    total = len(entries)
    healthy = sum(1 for entry in entries if entry.phase == Phase.COMPLETED)
    failed = sum(1 for entry in entries if entry.phase == Phase.FAILED)
    running = sum(1 for entry in entries if entry.phase == Phase.RUNNING)

    latest_backup_time = ""
    latest_backup_dt: datetime | None = None
    for entry in entries:
        parsed = _parse_timestamp(entry.last_backup_time)
        if parsed is not None and (latest_backup_dt is None or parsed > latest_backup_dt):
            latest_backup_dt = parsed
            latest_backup_time = entry.last_backup_time

    all_ok = healthy == total
    if failed:
        phase = SchedulePhase.DEGRADED
        message = f"{failed}/{total} backups failed"
    elif running:
        phase = SchedulePhase.RUNNING
        message = f"{running}/{total} backups running"
    elif all_ok:
        phase = SchedulePhase.HEALTHY
        message = f"All {total} backups healthy"
    else:
        phase = SchedulePhase.PENDING
        message = f"{healthy}/{total} backups completed"

    return BackupScheduleStatus(
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
        backups=entries,
    )


def reconcile_schedule_status(
    custom_api: CustomObjectsApi,
    schedule_name: str,
    namespace: str,
    entries: Iterable[BackupEntry],
) -> None:
    """Patch the schedule status from the given per-PVC entries."""
    k8s.patch_status(
        custom_api,
        plural=CRD_PLURAL_BACKUPSCHEDULES,
        name=schedule_name,
        namespace=namespace,
        status=aggregate_schedule_status(entries),
    )
