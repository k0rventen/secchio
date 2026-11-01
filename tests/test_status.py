"""Unit tests for Job -> CR status mapping and schedule aggregation."""

from __future__ import annotations

from unittest import mock

import status
from common import LABEL_PVC_NAME, LABEL_RESTORE, LABEL_SCHEDULE
from crds import (
    BackupEntry,
    BackupScheduleStatus,
    Phase,
    SchedulePhase,
    SnapshotInfo,
)
from restic import TerminationInfo
from status import JobStatus


def _aggregate(entries: list[BackupEntry]) -> BackupScheduleStatus:
    return status.aggregate_schedule_status(entries)


def test_schedule_status_healthy() -> None:
    result = _aggregate([BackupEntry(phase=Phase.COMPLETED)])
    assert result.phase == SchedulePhase.HEALTHY
    assert result.all_ok is True
    assert result.summary.total_backups == 1


def test_schedule_status_degraded() -> None:
    result = _aggregate([BackupEntry(phase=Phase.COMPLETED), BackupEntry(phase=Phase.FAILED)])
    assert result.phase == SchedulePhase.DEGRADED
    assert result.summary.failed_backups == 1


def test_schedule_status_empty_is_pending() -> None:
    result = _aggregate([])
    assert result.phase == SchedulePhase.PENDING
    assert result.summary.total_backups == 0


def test_schedule_status_uses_latest_timestamp() -> None:
    result = _aggregate(
        [
            BackupEntry(phase=Phase.COMPLETED, last_backup_time="2026-01-01T00:00:00Z"),
            BackupEntry(
                phase=Phase.COMPLETED,
                last_backup_time="2026-01-02T00:00:00+00:00",
            ),
        ]
    )
    assert result.last_backup_time == "2026-01-02T00:00:00+00:00"


def test_parse_timestamp_handles_z_and_offset() -> None:
    assert status._parse_timestamp("2026-01-01T00:00:00Z") == status._parse_timestamp(
        "2026-01-01T00:00:00+00:00"
    )
    assert status._parse_timestamp("") is None
    assert status._parse_timestamp("nope") is None


def test_backup_entry_from_job_success() -> None:
    job = JobStatus(
        name="j",
        namespace="ns",
        labels={LABEL_SCHEDULE: "sched", LABEL_PVC_NAME: "data"},
        succeeded=1,
        completion_time="2026-01-01T00:00:00Z",
    )
    info = TerminationInfo(snapshots=[SnapshotInfo(id="abc", time="t", size=1)])
    with mock.patch.object(status, "extract_termination_info", return_value=info):
        entry = status._entry_from_job(job, "sched", "data")

    assert entry is not None
    assert entry.phase == Phase.COMPLETED
    assert entry.last_backup_time == "2026-01-01T00:00:00Z"
    assert [snapshot.id for snapshot in entry.snapshots] == ["abc"]
    assert entry.cronjob_name == "sched-backup-data-cronjob"


def test_backup_entry_from_job_failure_surfaces_waiting_reason() -> None:
    job = JobStatus(
        name="j",
        namespace="ns",
        labels={LABEL_SCHEDULE: "sched", LABEL_PVC_NAME: "data"},
        failed=1,
    )
    info = TerminationInfo(
        waiting_reason="CreateContainerConfigError",
        waiting_message='secret "creds" not found',
    )
    with mock.patch.object(status, "extract_termination_info", return_value=info):
        entry = status._entry_from_job(job, "sched", "data")

    assert entry is not None
    assert entry.phase == Phase.FAILED
    assert entry.reason == "CreateContainerConfigError"
    assert "creds" in entry.message


def test_backup_entry_from_pending_job_is_none() -> None:
    assert status._entry_from_job(JobStatus(name="j", namespace="ns"), "sched", "data") is None


def test_restore_job_failure_updates_status() -> None:
    job = JobStatus(name="j", namespace="ns", labels={LABEL_RESTORE: "rt"}, failed=1)
    with (
        mock.patch.object(
            status, "extract_termination_info", return_value=TerminationInfo(error="boom")
        ),
        mock.patch.object(status.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(status.k8s, "patch_status") as patch_status,
    ):
        status._sync_restore_from_job(job, "rt")

    result = patch_status.call_args.kwargs["status"]
    assert result.phase == Phase.FAILED
    assert "boom" in result.message


def test_sync_backup_entry_patches_schedule() -> None:
    job = JobStatus(
        name="j",
        namespace="ns",
        labels={LABEL_SCHEDULE: "sched", LABEL_PVC_NAME: "data"},
        succeeded=1,
        completion_time="2026-01-01T00:00:00Z",
    )
    schedule_item = {
        "metadata": {"name": "sched", "namespace": "ns"},
        "spec": {},
        "status": {"backups": [{"pvcName": "data", "cronjobName": "sched-backup-data-cronjob"}]},
    }
    batch = mock.Mock()
    batch.list_namespaced_job.return_value = mock.Mock(
        items=[mock.Mock(metadata=mock.Mock(creation_timestamp=None))]
    )

    with (
        mock.patch.object(status.k8s, "get_batch_api", return_value=batch),
        mock.patch.object(status.JobStatus, "from_job", return_value=job),
        mock.patch.object(status, "extract_termination_info", return_value=TerminationInfo()),
        mock.patch.object(status.k8s, "get_backup_schedule", return_value=schedule_item),
        mock.patch.object(status.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(status.k8s, "patch_status") as patch_status,
    ):
        status.sync_backup_entry("sched", "ns", "data")

    result = patch_status.call_args.kwargs["status"]
    assert result.backups[0].phase == Phase.COMPLETED
    assert result.backups[0].cronjob_name == "sched-backup-data-cronjob"


def test_sync_backup_entry_adds_missing_entry() -> None:
    schedule_item = {
        "metadata": {"name": "sched", "namespace": "ns"},
        "spec": {},
        "status": {"backups": []},
    }
    batch = mock.Mock()
    batch.list_namespaced_job.return_value = mock.Mock(
        items=[mock.Mock(metadata=mock.Mock(creation_timestamp=None))]
    )

    with (
        mock.patch.object(status.k8s, "get_batch_api", return_value=batch),
        mock.patch.object(
            status.JobStatus,
            "from_job",
            return_value=JobStatus(name="j", namespace="ns", succeeded=1),
        ),
        mock.patch.object(status, "extract_termination_info", return_value=TerminationInfo()),
        mock.patch.object(status.k8s, "get_backup_schedule", return_value=schedule_item),
        mock.patch.object(status.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(status.k8s, "patch_status") as patch_status,
    ):
        status.sync_backup_entry("sched", "ns", "data")

    result = patch_status.call_args.kwargs["status"]
    assert result.backups[0].pvc_name == "data"
    assert result.backups[0].phase == Phase.COMPLETED
