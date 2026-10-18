"""Unit tests for the Job -> CR status mapping helpers in ``reconcile``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from kubernetes.client import V1Job, V1JobStatus, V1ObjectMeta

import status
from common import LABEL_BACKUP, LABEL_RESTORE
from restic import TerminationInfo, parse_termination_message
from status import JobStatus, _failed_reason


def test_parse_termination_message() -> None:
    message = (
        "unrelated log line\n"
        "SNAPSHOT_ID=abc123\n"
        "SNAP=abc123 2026-01-01T00:00:00Z 1024\n"
        "SNAP=def456 2026-01-02T00:00:00Z 2048\n"
        "RESTORED_SNAPSHOT_ID=zzz\n"
    )
    info = parse_termination_message(message)
    assert info.snapshot_id == "abc123"
    assert [(s.id, s.time, s.size) for s in info.snapshots] == [
        ("abc123", "2026-01-01T00:00:00Z", 1024),
        ("def456", "2026-01-02T00:00:00Z", 2048),
    ]
    assert info.restored_snapshot_id == "zzz"


def test_parse_termination_message_is_empty_by_default() -> None:
    assert parse_termination_message("") == TerminationInfo()


def test_failed_reason_from_kopf_dicts() -> None:
    conditions = [
        {"type": "Complete", "status": "True", "message": "done"},
        {"type": "Failed", "status": "True", "message": "boom"},
    ]
    assert _failed_reason(conditions) == "boom"


def test_failed_reason_from_condition_objects() -> None:
    conditions = [SimpleNamespace(type="Failed", status="True", message="boom", reason="")]
    assert _failed_reason(conditions) == "boom"


def test_failed_reason_absent() -> None:
    assert _failed_reason([{"type": "Complete", "status": "True"}]) == ""
    assert _failed_reason([]) == ""


def test_job_status_from_kopf_body() -> None:
    body = {
        "metadata": {"name": "job-1", "namespace": "ns", "labels": {"a": "b"}},
        "status": {
            "succeeded": 1,
            "completionTime": "2026-01-01T00:00:00Z",
            "conditions": [{"type": "Failed", "status": "False"}],
        },
    }
    job = JobStatus.from_kopf_body(body)
    assert job.name == "job-1"
    assert job.namespace == "ns"
    assert job.labels == {"a": "b"}
    assert job.succeeded == 1
    assert job.completion_time == "2026-01-01T00:00:00Z"
    assert job.failure_reason == ""


def test_job_status_from_job() -> None:
    job = V1Job(
        metadata=V1ObjectMeta(name="j1", namespace="ns", labels={"a": "b"}),
        status=V1JobStatus(succeeded=1),
    )
    status = JobStatus.from_job(job)
    assert status.name == "j1"
    assert status.namespace == "ns"
    assert status.labels == {"a": "b"}
    assert status.succeeded == 1


def test_job_status_routes_by_label() -> None:
    with (
        mock.patch.object(status, "_sync_backup_from_job") as backup,
        mock.patch.object(status, "_sync_restore_from_job") as restore,
    ):
        status.job_status(JobStatus(name="j", namespace="ns", labels={LABEL_BACKUP: "bk"}))
        status.job_status(JobStatus(name="j", namespace="ns", labels={LABEL_RESTORE: "rt"}))
        status.job_status(JobStatus(name="j", namespace="", labels={LABEL_BACKUP: "bk"}))

    backup.assert_called_once()
    restore.assert_called_once()
