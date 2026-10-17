"""Reconciliation: turning CRDs into the workloads that do the work.

Schedule CRs fan out into Backup CRs; Backup CRs into CronJobs; Restore CRs into
Jobs — plus tearing those down on delete. Reading a Job's outcome back into CR
status lives in :mod:`status`.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes.client import CustomObjectsApi

import k8s
import status
from backup import JobConfig, create_cronjob
from common import (
    CRD_GROUP,
    CRD_PLURAL_BACKUPS,
    CRD_PLURAL_RESTORES,
    CRD_VERSION,
    REASON_SECRET_MISSING,
    RESTIC_IMAGE,
    extract_pvc_name,
    extract_schedule_name,
    make_backup_labels,
    make_backup_name,
    make_cronjob_name,
    make_restore_job_name,
    now_iso,
)
from crds import (
    Backup,
    BackupPhase,
    BackupScheduleSpec,
    BackupSpec,
    BackupStatus,
    RestorePhase,
    RestoreSpec,
    RestoreStatus,
)
from restore import RestoreJobConfig, create_restore_job

logger = logging.getLogger("reconcile")


# --- BackupSchedule -------------------------------------------------------


def schedule(name: str, spec: dict[str, Any]) -> None:
    """Reconcile a BackupSchedule into one Backup CR per matching PVC, plus drift."""
    custom_api = k8s.get_custom_objects_api()
    schedule_spec = BackupScheduleSpec.model_validate(spec)
    matched = k8s.list_pvcs_by_selector(k8s.get_core_api(), schedule_spec.pvc.selector)
    logger.info("Schedule %s: %d PVCs match selector", name, len(matched))

    desired = _desired_backups(name, schedule_spec, matched)
    live = _live_backups(custom_api, name)

    for key, wanted in desired.items():
        current = live.get(key)
        if current is None:
            logger.info("Creating new Backup: %s in %s", wanted.name, wanted.namespace)
            k8s.create_or_update_backup(custom_api, wanted.to_crd_body(CRD_GROUP, CRD_VERSION))
        elif current.spec != wanted.spec or current.labels != wanted.labels:
            logger.info("Spec or labels diff detected for Backup %s, patching...", wanted.name)
            k8s.patch_backup(
                custom_api,
                wanted.name,
                wanted.namespace,
                {
                    "metadata": {"labels": wanted.labels},
                    "spec": wanted.spec.model_dump(by_alias=True),
                },
            )

    for key, orphan in live.items():
        if key not in desired:
            logger.info("Deleting orphaned Backup: %s in %s", orphan.name, orphan.namespace)
            k8s.delete_backup(custom_api, orphan.name, orphan.namespace)

    status.reconcile_schedule_status(custom_api, name)


def delete_schedule(name: str) -> None:
    """Delete every Backup CR that belongs to a BackupSchedule."""
    custom_api = k8s.get_custom_objects_api()
    for item in k8s.list_backups_for_schedule(custom_api, name):
        meta = item.get("metadata", {})
        k8s.delete_backup(custom_api, meta.get("name", ""), meta.get("namespace", ""))


def _desired_backups(
    schedule_name: str,
    schedule_spec: BackupScheduleSpec,
    matched: list[tuple[str, str]],
) -> dict[tuple[str, str], Backup]:
    """The Backup CRs a schedule should own, keyed by (pvc name, namespace)."""
    desired: dict[tuple[str, str], Backup] = {}
    for pvc_name, pvc_namespace in matched:
        desired[(pvc_name, pvc_namespace)] = Backup(
            name=make_backup_name(schedule_name, pvc_name),
            namespace=pvc_namespace,
            spec=BackupSpec(
                pvc_name=pvc_name,
                pvc_namespace=pvc_namespace,
                repo=schedule_spec.repo,
                retention=schedule_spec.retention,
                schedule=schedule_spec.schedule,
                secret_name=schedule_spec.repo.secret_name,
            ),
            labels=make_backup_labels(schedule_name, pvc_name),
        )
    return desired


def _live_backups(
    custom_api: CustomObjectsApi,
    schedule_name: str,
) -> dict[tuple[str, str], Backup]:
    """The Backup CRs a schedule currently owns, keyed by (pvc name, namespace)."""
    live: dict[tuple[str, str], Backup] = {}
    for item in k8s.list_backups_for_schedule(custom_api, schedule_name):
        backup = Backup.from_crd(item)
        pvc_name = extract_pvc_name(backup.labels) or backup.spec.pvc_name
        if pvc_name and backup.namespace:
            live[(pvc_name, backup.namespace)] = backup
    return live


# --- Backup ---------------------------------------------------------------


def backup(
    name: str,
    namespace: str,
    spec: dict[str, Any],
    meta: dict[str, Any],
    current_status: dict[str, Any] | None = None,
) -> bool:
    """Create or update the CronJob for a Backup CR.

    Refuses to create the CronJob when the credentials Secret is missing from the
    target namespace: the pod would otherwise sit in ``CreateContainerConfigError``
    with no visible reason. The failure is surfaced on the Backup status instead,
    and cleared once the Secret appears.

    Returns ``True`` when the CronJob was ensured, ``False`` when it was skipped
    because the Secret is missing (so callers can avoid clobbering the failure with
    a stale job status).
    """
    backup_spec = BackupSpec.model_validate(spec)
    target_namespace = backup_spec.pvc_namespace or namespace
    secret_name = backup_spec.secret_name or backup_spec.repo.secret_name

    if not secret_name or not k8s.secret_exists(target_namespace, secret_name):
        if secret_name:
            message = (
                f"Secret '{secret_name}' not found in namespace '{target_namespace}'; "
                "backup CronJob not created"
            )
        else:
            message = "No credentials Secret configured; backup CronJob not created"
        logger.warning("Backup %s in %s: %s", name, namespace, message)
        _patch_backup_status(
            name,
            namespace,
            current_status,
            phase=BackupPhase.FAILED,
            reason=REASON_SECRET_MISSING,
            message=message,
        )
        return False

    if (current_status or {}).get("reason") == REASON_SECRET_MISSING:
        _patch_backup_status(
            name,
            namespace,
            current_status,
            phase=BackupPhase.PENDING,
            reason="",
            message="",
        )

    job_config = JobConfig(
        name=make_cronjob_name(name),
        namespace=target_namespace,
        pvc_name=backup_spec.pvc_name,
        pvc_namespace=target_namespace,
        repo=backup_spec.repo,
        retention=backup_spec.retention,
        image=RESTIC_IMAGE,
        secret_name=secret_name,
        schedule_name=extract_schedule_name(meta.get("labels", {})),
        backup_name=name,
        backup_uid=meta.get("uid", ""),
    )
    k8s.create_or_update_cronjob(
        k8s.get_batch_api(),
        create_cronjob(job_config, backup_spec.schedule),
    )
    return True


def _patch_backup_status(
    name: str,
    namespace: str,
    current_status: dict[str, Any] | None,
    *,
    phase: BackupPhase,
    reason: str,
    message: str,
) -> None:
    """Patch a Backup's status, preserving snapshot history from the current status."""
    status_obj = BackupStatus.model_validate(current_status or {})
    status_obj.phase = phase
    status_obj.reason = reason
    status_obj.message = message
    k8s.patch_status(
        k8s.get_custom_objects_api(),
        plural=CRD_PLURAL_BACKUPS,
        name=name,
        namespace=namespace,
        status=status_obj,
    )


def delete_backup(name: str, namespace: str, meta: dict[str, Any]) -> None:
    """Delete a Backup's CronJob and refresh the status of its schedule."""
    k8s.delete_cronjob(k8s.get_batch_api(), make_cronjob_name(name), namespace)
    schedule_name = extract_schedule_name(meta.get("labels", {}))
    if schedule_name:
        status.reconcile_schedule_status(k8s.get_custom_objects_api(), schedule_name)


# --- Restore --------------------------------------------------------------


def restore(name: str, namespace: str, spec: dict[str, Any], meta: dict[str, Any]) -> None:
    """Create or reconcile the Job for a Restore CR."""
    custom_api = k8s.get_custom_objects_api()
    restore_spec = RestoreSpec.model_validate(spec)
    backup_item = k8s.find_backup(
        custom_api,
        restore_spec.backup_ref,
        preferred_namespace=restore_spec.source_pvc_namespace or namespace,
    )
    if not backup_item:
        message = f"Referenced Backup '{restore_spec.backup_ref}' not found"
        logger.error("Restore %s in %s: %s", name, namespace, message)
        k8s.patch_status(
            custom_api,
            plural=CRD_PLURAL_RESTORES,
            name=name,
            namespace=namespace,
            status=RestoreStatus(
                phase=RestorePhase.FAILED,
                completion_time=now_iso(),
                message=message,
            ),
        )
        return

    backup_cr = Backup.from_crd(backup_item)
    target_namespace = restore_spec.target_pvc_namespace or namespace
    job_config = RestoreJobConfig(
        name=make_restore_job_name(name),
        namespace=target_namespace,
        target_pvc=restore_spec.target_pvc,
        source_pvc=restore_spec.source_pvc or backup_cr.spec.pvc_name,
        source_pvc_namespace=restore_spec.source_pvc_namespace or backup_cr.spec.pvc_namespace,
        repo=backup_cr.spec.repo,
        secret_name=backup_cr.spec.secret_name or backup_cr.spec.repo.secret_name,
        image=RESTIC_IMAGE,
        snapshot_id=restore_spec.snapshot_id,
        restore_name=name,
        restore_uid=meta.get("uid", ""),
        backup_name=backup_cr.name,
    )
    k8s.create_or_update_restore_job(k8s.get_batch_api(), create_restore_job(job_config))
    k8s.patch_status(
        custom_api,
        plural=CRD_PLURAL_RESTORES,
        name=name,
        namespace=namespace,
        status=RestoreStatus(
            phase=RestorePhase.RUNNING,
            start_time=now_iso(),
            snapshot_id=restore_spec.snapshot_id,
            message=f"Restore job {job_config.name} is running",
        ),
    )


def delete_restore(name: str, namespace: str) -> None:
    """Delete a Restore's Job."""
    k8s.delete_job(k8s.get_batch_api(), make_restore_job_name(name), namespace)
