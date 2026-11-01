"""Round-trip tests for the CRD models.

These guard the wire contract: field aliases must match the CRD schemas, and a
spec that is dumped and re-validated must compare equal.
"""

from __future__ import annotations

from crds import (
    BackupEntry,
    BackupSchedule,
    BackupScheduleSpec,
    BackupScheduleStatus,
    BackupScheduleSummary,
    Phase,
    Restore,
    RestoreSpec,
    RestoreStatus,
    S3BackendConfig,
    SchedulePhase,
    SnapshotInfo,
)


def test_schedule_spec_and_restic_rendering() -> None:
    spec = BackupScheduleSpec.model_validate(
        {
            "pvc": {"selector": {"app": "nginx"}},
            "schedule": "*/5 * * * *",
            "repo": {"endpoint": "minio/bucket", "secretName": "s3"},
            "retention": {"keepDaily": 4, "keepWeekly": 2},
        }
    )
    assert spec.pvc.selector == {"app": "nginx"}
    assert spec.repo.endpoint == "minio/bucket"
    assert spec.repo.secret_name == "s3"
    assert spec.repo.insecure_tls is False
    assert spec.repo.to_restic_repo() == "s3:minio/bucket"
    assert spec.retention.to_restic_args() == ["--keep-daily", "4", "--keep-weekly", "2"]


def test_schedule_time_zone_wire_key() -> None:
    spec = BackupScheduleSpec.model_validate(
        {
            "pvc": {"selector": {"app": "nginx"}},
            "schedule": "0 2 * * *",
            "timeZone": "Europe/Brussels",
            "repo": {"endpoint": "minio/bucket", "secretName": "s3"},
        }
    )
    assert spec.time_zone == "Europe/Brussels"
    assert spec.model_dump(by_alias=True)["timeZone"] == "Europe/Brussels"
    assert BackupScheduleSpec.model_validate(spec.model_dump(by_alias=True)) == spec


def test_repo_insecure_tls_wire_key() -> None:
    repo = S3BackendConfig.model_validate(
        {"endpoint": "minio/bucket", "secretName": "s3", "insecureTLS": True}
    )
    assert repo.insecure_tls is True
    assert repo.model_dump(by_alias=True) == {
        "endpoint": "minio/bucket",
        "secretName": "s3",
        "insecureTLS": True,
    }


def test_schedule_status_wire_keys() -> None:
    status = BackupScheduleStatus(
        phase=SchedulePhase.HEALTHY,
        all_ok=True,
        backups=[
            BackupEntry(
                pvc_name="data",
                cronjob_name="sched-backup-data-cronjob",
                phase=Phase.COMPLETED,
                snapshots=[SnapshotInfo(id="abc", time="t", size=1)],
            )
        ],
    )
    dump = status.model_dump(by_alias=True, mode="json")
    assert set(dump) == {
        "phase",
        "allOk",
        "lastBackupTime",
        "message",
        "summary",
        "backups",
    }
    assert set(dump["summary"]) == {
        "totalBackups",
        "healthyBackups",
        "failedBackups",
        "runningBackups",
    }
    assert dump["backups"][0]["pvcName"] == "data"
    assert dump["backups"][0]["cronjobName"] == "sched-backup-data-cronjob"
    assert dump["backups"][0]["snapshots"] == [{"id": "abc", "time": "t", "size": 1}]


def test_schedule_summary_defaults_to_zero() -> None:
    assert BackupScheduleSummary() == BackupScheduleSummary(
        total_backups=0, healthy_backups=0, failed_backups=0, running_backups=0
    )


def test_backup_schedule_from_crd_reads_entries() -> None:
    schedule = BackupSchedule.from_crd(
        {
            "metadata": {"name": "sched", "namespace": "ns", "uid": "u1"},
            "spec": {
                "pvc": {"selector": {"app": "data"}},
                "schedule": "0 2 * * *",
                "repo": {"endpoint": "minio/bucket", "secretName": "creds"},
            },
            "status": {
                "backups": [
                    {
                        "pvcName": "data",
                        "cronjobName": "sched-backup-data-cronjob",
                        "phase": "Completed",
                    }
                ]
            },
        }
    )
    assert schedule.name == "sched"
    assert schedule.namespace == "ns"
    assert schedule.status.backups[0].pvc_name == "data"
    assert schedule.status.backups[0].phase == Phase.COMPLETED


def test_restore_spec_and_status() -> None:
    spec = RestoreSpec.model_validate(
        {
            "scheduleName": "sched",
            "pvcName": "data",
            "targetPVC": "data-restore",
            "snapshotID": "abc",
        }
    )
    assert spec.schedule_name == "sched"
    assert spec.pvc_name == "data"
    assert spec.target_pvc == "data-restore"
    assert spec.snapshot_id == "abc"
    assert set(spec.model_dump(by_alias=True)) == {
        "scheduleName",
        "pvcName",
        "targetPVC",
        "snapshotID",
    }

    status = RestoreStatus(phase="Running", snapshot_id="abc")
    assert set(status.model_dump(by_alias=True, mode="json")) == {
        "phase",
        "startTime",
        "completionTime",
        "snapshotID",
        "message",
    }

    restore = Restore.from_crd(
        {"metadata": {"name": "r1", "namespace": "ns"}, "spec": spec.model_dump(by_alias=True)}
    )
    assert restore.name == "r1"
    assert restore.spec.snapshot_id == "abc"


def test_status_phase_is_tolerant_of_unknown_values() -> None:
    # The operator must not crash on a phase value it does not recognise.
    assert str(BackupEntry.model_validate({"phase": ""}).phase) == ""
    assert str(BackupEntry.model_validate({"phase": "Weird"}).phase) == "Weird"
    assert BackupEntry().phase == Phase.PENDING


def test_to_crd_body_has_metadata_and_spec_only() -> None:
    restore = Restore(
        name="r1",
        namespace="ns",
        labels={"a": "b"},
        spec=RestoreSpec(schedule_name="sched", pvc_name="data"),
    )
    body = restore.to_crd_body("secchio.cocointhe.cloud", "v1alpha1")

    assert body["apiVersion"] == "secchio.cocointhe.cloud/v1alpha1"
    assert body["kind"] == "Restore"
    assert body["metadata"] == {"name": "r1", "namespace": "ns", "labels": {"a": "b"}}
    assert body["spec"]["scheduleName"] == "sched"
    assert "status" not in body
