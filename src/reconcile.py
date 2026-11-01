"""Reconciliation: turning CRDs into the workloads that do the work.

A BackupSchedule fans out into one CronJob per matching PVC; a Restore becomes a
one-off Job. Reading a Job's outcome back into CR status lives in :mod:`status`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kubernetes.client.exceptions import ApiException

import k8s
import status
from common import (
    CRD_PLURAL_RESTORES,
    REASON_CRONJOB_REJECTED,
    RESTIC_IMAGE,
    make_cronjob_name,
    make_restore_job_name,
    now_iso,
)
from crds import (
    BackupEntry,
    BackupSchedule,
    BackupScheduleSpec,
    Phase,
    RestoreSpec,
    RestoreStatus,
)
from workloads import WorkloadConfig, create_cronjob, create_restore_job

logger = logging.getLogger("reconcile")


# --- BackupSchedule -------------------------------------------------------


def schedule(name: str, namespace: str, spec: dict[str, Any], uid: str) -> None:
    """Reconcile a BackupSchedule into one CronJob per matching PVC."""
    custom_api = k8s.get_custom_objects_api()
    batch_api = k8s.get_batch_api()
    schedule_spec = BackupScheduleSpec.model_validate(spec)
    matched = k8s.list_pvcs_by_selector(k8s.get_core_api(), schedule_spec.pvc.selector, namespace)
    logger.info("Schedule %s in %s: %d PVCs match selector", name, namespace, len(matched))

    live = {
        cronjob.metadata.name: cronjob
        for cronjob in k8s.list_cronjobs_for_schedule(batch_api, namespace, name)
        if cronjob.metadata and cronjob.metadata.name
    }

    # Serialize with Job-driven status updates and read the status fresh: the
    # handler's payload can be stale by the time this runs.
    with status.schedule_lock(namespace, name):
        item = k8s.get_backup_schedule(custom_api, name, namespace)
        if item is None:
            return
        current = BackupSchedule.from_crd(item).status
        current_entries = {entry.pvc_name: entry for entry in current.backups}

        desired_names: set[str] = set()
        entries: list[BackupEntry] = []
        for pvc_name in matched:
            cronjob_name = make_cronjob_name(name, pvc_name)
            desired_names.add(cronjob_name)
            entry = current_entries.get(pvc_name, BackupEntry(pvc_name=pvc_name))
            entry.cronjob_name = cronjob_name

            config = WorkloadConfig(
                name=cronjob_name,
                namespace=namespace,
                pvc_name=pvc_name,
                repo=schedule_spec.repo,
                secret_name=schedule_spec.repo.secret_name,
                image=RESTIC_IMAGE,
                mode="backup",
                schedule=schedule_spec.schedule,
                time_zone=schedule_spec.time_zone,
                retention=schedule_spec.retention,
                schedule_name=name,
                owner_kind="BackupSchedule",
                owner_name=name,
                owner_uid=uid,
            )
            try:
                k8s.create_or_update_cronjob(batch_api, create_cronjob(config))
            except ApiException as e:
                message = f"CronJob rejected by Kubernetes: {_api_error_message(e)}"
                logger.warning("Schedule %s in %s: %s", name, namespace, message)
                entry.phase = Phase.FAILED
                entry.reason = REASON_CRONJOB_REJECTED
                entry.message = message
            else:
                if entry.reason == REASON_CRONJOB_REJECTED:
                    entry.phase = Phase.PENDING
                    entry.reason = ""
                    entry.message = ""
            entries.append(entry)

        for cronjob_name in live:
            if cronjob_name not in desired_names:
                logger.info("Deleting orphaned CronJob: %s in %s", cronjob_name, namespace)
                k8s.delete_cronjob(batch_api, cronjob_name, namespace)

        status.reconcile_schedule_status(custom_api, name, namespace, entries)


def _api_error_message(error: ApiException) -> str:
    """The human-readable message from a Kubernetes API error, if available."""
    if isinstance(error.body, str):
        try:
            body = json.loads(error.body)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return str(body["message"])
    return error.reason or str(error)


# --- Restore --------------------------------------------------------------


def restore(name: str, namespace: str, spec: dict[str, Any], meta: dict[str, Any]) -> None:
    """Create the Job for a Restore CR."""
    custom_api = k8s.get_custom_objects_api()
    restore_spec = RestoreSpec.model_validate(spec)
    schedule_item = k8s.get_backup_schedule(custom_api, restore_spec.schedule_name, namespace)
    if schedule_item is None:
        message = (
            f"BackupSchedule '{restore_spec.schedule_name}' not found in namespace '{namespace}'"
        )
        logger.error("Restore %s in %s: %s", name, namespace, message)
        k8s.patch_status(
            custom_api,
            plural=CRD_PLURAL_RESTORES,
            name=name,
            namespace=namespace,
            status=RestoreStatus(
                phase=Phase.FAILED,
                completion_time=now_iso(),
                message=message,
            ),
        )
        return

    schedule_cr = BackupSchedule.from_crd(schedule_item)
    config = WorkloadConfig(
        name=make_restore_job_name(name),
        namespace=namespace,
        pvc_name=restore_spec.pvc_name,
        repo=schedule_cr.spec.repo,
        secret_name=schedule_cr.spec.repo.secret_name,
        image=RESTIC_IMAGE,
        mode="restore",
        snapshot_id=restore_spec.snapshot_id,
        target_pvc=restore_spec.target_pvc or restore_spec.pvc_name,
        schedule_name=restore_spec.schedule_name,
        owner_kind="Restore",
        owner_name=name,
        owner_uid=meta.get("uid", ""),
    )
    created = k8s.create_or_update_restore_job(k8s.get_batch_api(), create_restore_job(config))
    if not created:
        # The Job already exists (e.g. a retried create). Leave its status alone.
        return

    k8s.patch_status(
        custom_api,
        plural=CRD_PLURAL_RESTORES,
        name=name,
        namespace=namespace,
        status=RestoreStatus(
            phase=Phase.RUNNING,
            start_time=now_iso(),
            snapshot_id=restore_spec.snapshot_id,
            message=f"Restore job {config.name} is running",
        ),
    )


def delete_restore(name: str, namespace: str) -> None:
    """Delete a Restore's Job."""
    k8s.delete_job(k8s.get_batch_api(), make_restore_job_name(name), namespace)
