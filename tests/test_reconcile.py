"""Unit tests for the Backup reconcile preflight (credentials Secret check).

The operator must not create a CronJob whose pods would sit in
``CreateContainerConfigError``; instead it surfaces the missing Secret on the
Backup status and clears it once the Secret appears.
"""

from __future__ import annotations

from unittest import mock

import reconcile
from common import REASON_SECRET_MISSING, make_backup_labels
from crds import BackupPhase, RestorePhase

SPEC = {
    "pvcName": "data",
    "pvcNamespace": "ns",
    "repo": {"endpoint": "minio/bucket", "secretName": "creds"},
    "schedule": "* * * * *",
}


def test_backup_skips_cronjob_when_secret_missing() -> None:
    with (
        mock.patch.object(reconcile.k8s, "secret_exists", return_value=False),
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
        mock.patch.object(reconcile.k8s, "create_or_update_cronjob") as create_cronjob,
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
    ):
        ensured = reconcile.backup(name="bk", namespace="ns", spec=SPEC, meta={})

    assert ensured is False
    create_cronjob.assert_not_called()
    patch_status.assert_called_once()
    status = patch_status.call_args.kwargs["status"]
    assert status.phase == BackupPhase.FAILED
    assert status.reason == REASON_SECRET_MISSING
    assert "creds" in status.message


def test_backup_creates_cronjob_when_secret_present() -> None:
    with (
        mock.patch.object(reconcile.k8s, "secret_exists", return_value=True),
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
        mock.patch.object(reconcile.k8s, "create_or_update_cronjob") as create_cronjob,
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
    ):
        ensured = reconcile.backup(name="bk", namespace="ns", spec=SPEC, meta={})

    assert ensured is True
    create_cronjob.assert_called_once()
    patch_status.assert_not_called()


def test_backup_clears_secret_missing_failure() -> None:
    current = {
        "phase": "Failed",
        "reason": REASON_SECRET_MISSING,
        "snapshots": [{"id": "a", "time": "t1"}, {"id": "b", "time": "t2"}],
    }
    with (
        mock.patch.object(reconcile.k8s, "secret_exists", return_value=True),
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
        mock.patch.object(reconcile.k8s, "create_or_update_cronjob"),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
    ):
        reconcile.backup(name="bk", namespace="ns", spec=SPEC, meta={}, current_status=current)

    patch_status.assert_called_once()
    status = patch_status.call_args.kwargs["status"]
    assert status.phase == BackupPhase.PENDING
    assert status.reason == ""
    # Snapshot history must survive the clear.
    assert [s.id for s in status.snapshots] == ["a", "b"]


SCHEDULE_SPEC = {
    "pvc": {"selector": {"app": "data"}},
    "schedule": "0 2 * * *",
    "repo": {"endpoint": "minio/bucket", "secretName": "creds"},
    "retention": {"keepDaily": 7},
}

RESTORE_SPEC = {
    "targetPVC": "restore-data",
    "targetPVCNamespace": "ns",
    "backupRef": "sched-backup-data",
}


def _backup_item(name: str, namespace: str, pvc: str, schedule: str = "0 2 * * *") -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": make_backup_labels("sched", pvc),
        },
        "spec": {
            "pvcName": pvc,
            "pvcNamespace": namespace,
            "repo": {"endpoint": "minio/bucket", "secretName": "creds"},
            "retention": {"keepDaily": 7},
            "schedule": schedule,
            "secretName": "creds",
        },
        "status": {},
    }


def test_schedule_creates_backup_for_matching_pvc() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=[("data", "ns")]),
        mock.patch.object(reconcile.k8s, "list_backups_for_schedule", return_value=[]),
        mock.patch.object(reconcile.k8s, "create_or_update_backup") as create,
        mock.patch.object(reconcile.status, "reconcile_schedule_status"),
    ):
        reconcile.schedule(name="sched", spec=SCHEDULE_SPEC)

    create.assert_called_once()
    body = create.call_args.args[1]
    assert body["metadata"]["name"] == "sched-backup-data"
    assert body["metadata"]["namespace"] == "ns"
    assert body["spec"]["pvcName"] == "data"
    assert body["spec"]["schedule"] == "0 2 * * *"


def test_schedule_patches_drifted_backup() -> None:
    live = _backup_item("sched-backup-data", "ns", "data", schedule="0 1 * * *")
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=[("data", "ns")]),
        mock.patch.object(reconcile.k8s, "list_backups_for_schedule", return_value=[live]),
        mock.patch.object(reconcile.k8s, "patch_backup") as patch,
        mock.patch.object(reconcile.status, "reconcile_schedule_status"),
    ):
        reconcile.schedule(name="sched", spec=SCHEDULE_SPEC)

    patch.assert_called_once()
    assert patch.call_args.args[1] == "sched-backup-data"
    assert patch.call_args.args[3]["spec"]["schedule"] == "0 2 * * *"


def test_schedule_deletes_orphaned_backup() -> None:
    live = _backup_item("sched-backup-old", "ns", "old")
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "get_core_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "list_pvcs_by_selector", return_value=[]),
        mock.patch.object(reconcile.k8s, "list_backups_for_schedule", return_value=[live]),
        mock.patch.object(reconcile.k8s, "delete_backup") as delete,
        mock.patch.object(reconcile.status, "reconcile_schedule_status"),
    ):
        reconcile.schedule(name="sched", spec=SCHEDULE_SPEC)

    delete.assert_called_once_with(mock.ANY, "sched-backup-old", "ns")


def test_restore_reports_missing_backup() -> None:
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "find_backup", return_value=None),
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
    ):
        reconcile.restore(name="rt", namespace="ns", spec=RESTORE_SPEC, meta={})

    patch_status.assert_called_once()
    result = patch_status.call_args.kwargs["status"]
    assert result.phase == RestorePhase.FAILED
    assert "not found" in result.message


def test_restore_creates_job() -> None:
    backup_item = _backup_item("sched-backup-data", "ns", "data")
    with (
        mock.patch.object(reconcile.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "find_backup", return_value=backup_item),
        mock.patch.object(reconcile.k8s, "get_batch_api", return_value=object()),
        mock.patch.object(reconcile.k8s, "create_or_update_restore_job") as create_job,
        mock.patch.object(reconcile.k8s, "patch_status") as patch_status,
    ):
        reconcile.restore(name="rt", namespace="ns", spec=RESTORE_SPEC, meta={})

    create_job.assert_called_once()
    assert patch_status.call_args.kwargs["status"].phase == RestorePhase.RUNNING
