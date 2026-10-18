"""Round-trip tests for the CRD models.

These guard the wire contract: the field aliases must match the CRD schemas, and
a spec that is dumped and re-validated must compare equal (the desired/live diff
in the schedule reconciler relies on that equality).
"""

from __future__ import annotations

from crds import (
    Backup,
    BackupPhase,
    BackupScheduleSpec,
    BackupScheduleStatus,
    BackupScheduleSummary,
    BackupSpec,
    BackupStatus,
    Restore,
    RestoreSpec,
    RestoreStatus,
    S3BackendConfig,
    SchedulePhase,
    SnapshotInfo,
)


def test_backup_spec_wire_keys() -> None:
    spec = BackupSpec(pvc_name="nginx", pvc_namespace="backup-test", schedule="0 2 * * *")
    assert set(spec.model_dump(by_alias=True)) == {
        "pvcName",
        "pvcNamespace",
        "repo",
        "retention",
        "schedule",
        "secretName",
    }


def test_backup_spec_round_trips() -> None:
    spec = BackupSpec(pvc_name="nginx", pvc_namespace="backup-test", schedule="0 2 * * *")
    assert BackupSpec.model_validate(spec.model_dump(by_alias=True)) == spec


def test_backup_status_wire_keys() -> None:
    status = BackupStatus(
        phase=BackupPhase.COMPLETED,
        last_backup_time="2026-01-01T00:00:00Z",
        snapshots=[
            SnapshotInfo(id="abc123", time="2026-01-01T00:00:00Z", size=1024),
            SnapshotInfo(id="def456", time="2026-01-02T00:00:00Z", size=2048),
        ],
    )
    dump = status.model_dump(by_alias=True, mode="json")
    assert set(dump) == {
        "phase",
        "lastBackupTime",
        "snapshots",
        "message",
        "reason",
    }
    assert dump["snapshots"] == [
        {"id": "abc123", "time": "2026-01-01T00:00:00Z", "size": 1024},
        {"id": "def456", "time": "2026-01-02T00:00:00Z", "size": 2048},
    ]


def test_backup_from_crd() -> None:
    item = {
        "metadata": {
            "name": "bk",
            "namespace": "ns",
            "uid": "u1",
            "labels": {"secchio.cocointhe.cloud/schedule": "nginx"},
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {"pvcName": "nginx", "pvcNamespace": "ns", "schedule": "* * * * *"},
        "status": {
            "phase": "Completed",
            "snapshots": [
                {"id": "a", "time": "t1", "size": 10},
                {"id": "b", "time": "t2", "size": 20},
            ],
        },
    }
    bk = Backup.from_crd(item)
    assert bk.name == "bk"
    assert bk.namespace == "ns"
    assert bk.uid == "u1"
    assert bk.labels == {"secchio.cocointhe.cloud/schedule": "nginx"}
    assert bk.status.phase == BackupPhase.COMPLETED
    assert [(s.id, s.time, s.size) for s in bk.status.snapshots] == [
        ("a", "t1", 10),
        ("b", "t2", 20),
    ]


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
    status = BackupScheduleStatus(phase=SchedulePhase.HEALTHY, all_ok=True)
    dump = status.model_dump(by_alias=True, mode="json")
    assert set(dump) == {"phase", "allOk", "lastBackupTime", "message", "summary"}
    assert set(dump["summary"]) == {
        "totalBackups",
        "healthyBackups",
        "failedBackups",
        "runningBackups",
    }


def test_schedule_summary_defaults_to_zero() -> None:
    assert BackupScheduleSummary() == BackupScheduleSummary(
        total_backups=0, healthy_backups=0, failed_backups=0, running_backups=0
    )


def test_restore_spec_and_status() -> None:
    spec = RestoreSpec.model_validate(
        {
            "targetPVC": "app-data",
            "targetPVCNamespace": "backup-test",
            "backupRef": "bk1",
            "sourcePVC": "src",
            "sourcePVCNamespace": "ns",
            "snapshotID": "abc",
        }
    )
    assert spec.target_pvc == "app-data"
    assert spec.target_pvc_namespace == "backup-test"
    assert spec.source_pvc == "src"
    assert spec.snapshot_id == "abc"
    assert set(spec.model_dump(by_alias=True)) == {
        "targetPVC",
        "targetPVCNamespace",
        "backupRef",
        "sourcePVC",
        "sourcePVCNamespace",
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
    assert str(BackupStatus.model_validate({"phase": ""}).phase) == ""
    assert str(BackupStatus.model_validate({"phase": "Weird"}).phase) == "Weird"
    assert BackupStatus().phase == BackupPhase.PENDING


def test_to_crd_body_has_metadata_and_spec_only() -> None:
    backup = Backup(
        name="bk",
        namespace="ns",
        labels={"a": "b"},
        spec=BackupSpec(pvc_name="data", pvc_namespace="ns"),
    )
    body = backup.to_crd_body("secchio.cocointhe.cloud", "v1alpha1")

    assert body["apiVersion"] == "secchio.cocointhe.cloud/v1alpha1"
    assert body["kind"] == "Backup"
    assert body["metadata"] == {"name": "bk", "namespace": "ns", "labels": {"a": "b"}}
    assert body["spec"]["pvcName"] == "data"
    assert "status" not in body
