"""Unit tests for schedule fan-out and restore reconciliation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from kubernetes.client.exceptions import ApiException

import reconcile
from common import REASON_CRONJOB_REJECTED
from crds import Phase

SCHEDULE_SPEC = {
    "pvc": {"selector": {"app": "data"}},
    "schedule": "0 2 * * *",
    "repo": {"endpoint": "minio/bucket", "secretName": "creds"},
    "retention": {"keepDaily": 7},
}

RESTORE_SPEC = {
    "scheduleName": "sched",
    "pvcName": "data",
    "targetPVC": "restore-data",
    "snapshotID": "latest",
}


def _schedule_item(name: str = "sched", namespace: str = "ns", status: dict | None = None) -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace, "uid": "uid"},
        "spec": SCHEDULE_SPEC,
        "status": status or {},
    }


def _cronjob(name: str):
    return mock.Mock(metadata=SimpleNamespace(name=name))


def test_schedule_creates_cronjob_for_matching_pvc() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=["data"]),
        mock.patch.object(reconcile.k8s, "list_cronjobs_for_schedule", return_value=[]),
        mock.patch.object(reconcile.k8s, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(reconcile.k8s, "create_or_update_cronjob") as create,
        mock.patch.object(reconcile.status, "reconcile_schedule_status") as reconcile_status,
    ):
        reconcile.schedule(name="sched", namespace="ns", spec=SCHEDULE_SPEC, uid="uid")

    create.assert_called_once()
    cronjob = create.call_args.args[1]
    assert cronjob.metadata.name == "sched-backup-data-cronjob"
    assert cronjob.metadata.owner_references[0].kind == "BackupSchedule"

    entries = reconcile_status.call_args.args[3]
    assert [(entry.pvc_name, entry.cronjob_name) for entry in entries] == [
        ("data", "sched-backup-data-cronjob")
    ]
    assert entries[0].phase == Phase.PENDING


def test_schedule_deletes_orphaned_cronjob() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=[]),
        mock.patch.object(
            reconcile.k8s,
            "list_cronjobs_for_schedule",
            return_value=[_cronjob("sched-backup-old-cronjob")],
        ),
        mock.patch.object(reconcile.k8s, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(reconcile.k8s, "delete_cronjob") as delete,
        mock.patch.object(reconcile.status, "reconcile_schedule_status"),
    ):
        reconcile.schedule(name="sched", namespace="ns", spec=SCHEDULE_SPEC, uid="uid")

    delete.assert_called_once_with(mock.ANY, "sched-backup-old-cronjob", "ns")


def test_schedule_surfaces_cronjob_rejection() -> None:
    error = ApiException(status=422, reason="Unprocessable Entity")
    error.body = (
        '{"kind":"Status","message":"CronJob.batch \\"x\\" is invalid: '
        'spec.schedule: Invalid value: \\"60 * * * *\\": ...","reason":"Invalid"}'
    )
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=["data"]),
        mock.patch.object(reconcile.k8s, "list_cronjobs_for_schedule", return_value=[]),
        mock.patch.object(reconcile.k8s, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(reconcile.k8s, "create_or_update_cronjob", side_effect=error),
        mock.patch.object(reconcile.status, "reconcile_schedule_status") as reconcile_status,
    ):
        reconcile.schedule(name="sched", namespace="ns", spec=SCHEDULE_SPEC, uid="uid")

    entry = reconcile_status.call_args.args[3][0]
    assert entry.phase == Phase.FAILED
    assert entry.reason == REASON_CRONJOB_REJECTED
    assert "Invalid value" in entry.message


def test_schedule_clears_cronjob_rejection_once_accepted() -> None:
    current = {
        "backups": [
            {
                "pvcName": "data",
                "cronjobName": "sched-backup-data-cronjob",
                "phase": "Failed",
                "reason": REASON_CRONJOB_REJECTED,
            }
        ]
    }
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=["data"]),
        mock.patch.object(reconcile.k8s, "list_cronjobs_for_schedule", return_value=[]),
        mock.patch.object(
            reconcile.k8s,
            "get_backup_schedule",
            return_value=_schedule_item(status=current),
        ),
        mock.patch.object(reconcile.k8s, "create_or_update_cronjob"),
        mock.patch.object(reconcile.status, "reconcile_schedule_status") as reconcile_status,
    ):
        reconcile.schedule(name="sched", namespace="ns", spec=SCHEDULE_SPEC, uid="uid")

    entry = reconcile_status.call_args.args[3][0]
    assert entry.phase == Phase.PENDING
    assert entry.reason == ""


def test_restore_reports_missing_schedule() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_backup_schedule", return_value=None),
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
    ):
        reconcile.restore(name="rt", namespace="ns", spec=RESTORE_SPEC, meta={})

    patch_status.assert_called_once()
    result = patch_status.call_args.kwargs["status"]
    assert result.phase == Phase.FAILED
    assert "not found" in result.message


def test_restore_creates_job() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(
            reconcile.k8s, "create_or_update_restore_job", return_value=True
        ) as create_job,
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
    ):
        reconcile.restore(name="rt", namespace="ns", spec=RESTORE_SPEC, meta={})

    create_job.assert_called_once()
    assert patch_status.call_args.kwargs["status"].phase == Phase.RUNNING


def test_restore_does_not_reset_status_when_job_exists() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(
            reconcile.k8s, "create_or_update_restore_job", return_value=False
        ) as create_job,
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
    ):
        reconcile.restore(name="rt", namespace="ns", spec=RESTORE_SPEC, meta={})

    create_job.assert_called_once()
    patch_status.assert_not_called()
