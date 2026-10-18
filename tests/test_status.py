"""Unit tests for Job -> CR status mapping and schedule aggregation."""

from __future__ import annotations

from unittest import mock

import status
from common import LABEL_BACKUP, LABEL_RESTORE, LABEL_SCHEDULE
from crds import BackupPhase, BackupScheduleStatus, RestorePhase, SchedulePhase, SnapshotInfo
from restic import TerminationInfo
from status import JobStatus


def _backup_item(phase: str, last_backup_time: str = "") -> dict:
    return {
        "metadata": {"name": "bk", "namespace": "ns"},
        "spec": {},
        "status": {"phase": phase, "lastBackupTime": last_backup_time},
    }


def _aggregate(items: list[dict]) -> BackupScheduleStatus:
    with (
        mock.patch.object(status.k8s, "list_backups_for_schedule", return_value=items),
        mock.patch.object(status.k8s, "patch_status") as patch_status,
    ):
        status.reconcile_schedule_status(mock.Mock(), "sched")
    return patch_status.call_args.kwargs["status"]


def test_schedule_status_healthy() -> None:
    result = _aggregate([_backup_item("Completed")])
    assert result.phase == SchedulePhase.HEALTHY
    assert result.all_ok is True
    assert result.summary.total_backups == 1


def test_schedule_status_degraded() -> None:
    result = _aggregate([_backup_item("Completed"), _backup_item("Failed")])
    assert result.phase == SchedulePhase.DEGRADED
    assert result.summary.failed_backups == 1


def test_schedule_status_empty_is_pending() -> None:
    result = _aggregate([])
    assert result.phase == SchedulePhase.PENDING
    assert result.summary.total_backups == 0


def test_schedule_status_uses_latest_timestamp() -> None:
    result = _aggregate(
        [
            _backup_item("Completed", "2026-01-01T00:00:00Z"),
            _backup_item("Completed", "2026-01-02T00:00:00+00:00"),
        ]
    )
    assert result.last_backup_time == "2026-01-02T00:00:00+00:00"


def test_parse_timestamp_handles_z_and_offset() -> None:
    assert status._parse_timestamp("2026-01-01T00:00:00Z") == status._parse_timestamp(
        "2026-01-01T00:00:00+00:00"
    )
    assert status._parse_timestamp("") is None
    assert status._parse_timestamp("nope") is None


def test_backup_job_success_updates_status() -> None:
    job = JobStatus(
        name="j",
        namespace="ns",
        labels={LABEL_BACKUP: "bk", LABEL_SCHEDULE: "sched"},
        succeeded=1,
        completion_time="2026-01-01T00:00:00Z",
    )
    info = TerminationInfo(snapshots=[SnapshotInfo(id="abc", time="t", size=1)])
    with (
        mock.patch.object(status, "extract_termination_info", return_value=info),
        mock.patch.object(status.k8s, "get_custom_objects_api", return_value=object()),
        mock.patch.object(status.k8s, "patch_status") as patch_status,
        mock.patch.object(status, "reconcile_schedule_status") as reconcile_schedule,
    ):
        status._sync_backup_from_job(job, "bk")

    result = patch_status.call_args.kwargs["status"]
    assert result.phase == BackupPhase.COMPLETED
    assert [s.id for s in result.snapshots] == ["abc"]
    reconcile_schedule.assert_called_once()


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
    assert result.phase == RestorePhase.FAILED
    assert "boom" in result.message


def test_backup_status_excludes_restore_jobs() -> None:
    batch = mock.Mock()
    batch.list_namespaced_job.return_value = mock.Mock(items=[])
    with (
        mock.patch.object(status.k8s, "get_batch_api", return_value=batch),
        mock.patch.object(status, "job_status") as job_status,
    ):
        status.backup_status("bk", "ns")

    batch.list_namespaced_job.assert_called_once_with(
        namespace="ns", label_selector=f"{LABEL_BACKUP}=bk,!{LABEL_RESTORE}"
    )
    job_status.assert_not_called()
