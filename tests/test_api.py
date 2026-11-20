"""Unit tests for the UI-triggered manual backup Job and disaster recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from kubernetes.client import (
    V1CronJob,
    V1CronJobSpec,
    V1JobSpec,
    V1JobTemplateSpec,
    V1ObjectMeta,
    V1PodTemplateSpec,
)

import api
from common import JOB_TTL_SECONDS


def _schedule_item() -> dict:
    return {
        "metadata": {"name": "sched", "namespace": "ns"},
        "spec": {
            "pvc": {"selector": {"app": "data"}},
            "schedule": "0 2 * * *",
            "repo": {"endpoint": "minio/bucket", "secretName": "creds"},
        },
        "status": {
            "backups": [
                {"pvcName": "data", "cronjobName": "sched-backup-data-cronjob"},
                {"pvcName": "logs", "cronjobName": "sched-backup-logs-cronjob"},
            ]
        },
    }


def test_manual_backup_job_gets_ttl() -> None:
    cronjob = V1CronJob(
        metadata=V1ObjectMeta(name="bk-cronjob", namespace="ns"),
        spec=V1CronJobSpec(
            schedule="0 2 * * *",
            job_template=V1JobTemplateSpec(
                spec=V1JobSpec(backoff_limit=2, template=V1PodTemplateSpec())
            ),
        ),
    )
    batch = mock.Mock()
    batch.read_namespaced_cron_job.return_value = cronjob
    batch.create_namespaced_job.side_effect = lambda *, namespace, body: body

    with mock.patch.object(api, "get_batch_api", return_value=batch):
        api._trigger_backup_job("ns", "bk-cronjob")

    created = batch.create_namespaced_job.call_args.kwargs["body"]
    assert created.spec.ttl_seconds_after_finished == JOB_TTL_SECONDS


def test_manual_backup_refused_when_suspended() -> None:
    cronjob = V1CronJob(
        metadata=V1ObjectMeta(name="bk-cronjob", namespace="ns"),
        spec=V1CronJobSpec(
            schedule="0 2 * * *",
            suspend=True,
            job_template=V1JobTemplateSpec(
                spec=V1JobSpec(backoff_limit=2, template=V1PodTemplateSpec())
            ),
        ),
    )
    batch = mock.Mock()
    batch.read_namespaced_cron_job.return_value = cronjob

    with (
        mock.patch.object(api, "get_batch_api", return_value=batch),
        pytest.raises(api.HTTPException) as exc,
    ):
        api._trigger_backup_job("ns", "bk-cronjob")

    assert exc.value.status_code == 409
    batch.create_namespaced_job.assert_not_called()


def test_set_suspend_patches_spec_and_cronjobs() -> None:
    custom = mock.Mock()
    batch = mock.Mock()
    cronjob = SimpleNamespace(metadata=SimpleNamespace(name="sched-backup-data-cronjob"))
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=custom),
        mock.patch.object(api, "get_batch_api", return_value=batch),
        mock.patch.object(api, "list_cronjobs_for_schedule", return_value=[cronjob]),
    ):
        api._set_suspend("ns", "sched", True)

    assert custom.patch_namespaced_custom_object.call_args.kwargs["body"] == {
        "spec": {"suspend": True}
    }
    batch.patch_namespaced_cron_job.assert_called_once_with(
        name="sched-backup-data-cronjob",
        namespace="ns",
        body={"spec": {"suspend": True}},
    )


def test_scan_schedule_creates_jobs() -> None:
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=mock.Mock()),
        mock.patch.object(api, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(api, "_create_scan_job", side_effect=["j1", "j2"]) as create_scan,
    ):
        result = asyncio.run(api.scan_schedule("ns", "sched"))

    assert create_scan.call_count == 2
    assert result["triggered"] == [
        {"pvc": "data", "job_name": "j1"},
        {"pvc": "logs", "job_name": "j2"},
    ]
    assert result["errors"] == []


def test_disaster_recovery_suspends_and_creates_restores() -> None:
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=mock.Mock()),
        mock.patch.object(api, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(api, "_active_backup_jobs", return_value=[]),
        mock.patch.object(api, "_set_suspend") as set_suspend,
        mock.patch.object(api, "_create_restore", side_effect=["r1", "r2"]) as create_restore,
    ):
        result = asyncio.run(api.disaster_recovery("ns", "sched"))

    set_suspend.assert_called_once_with("ns", "sched", True)
    assert create_restore.call_args_list == [
        mock.call("ns", "sched", "data", "data", "latest"),
        mock.call("ns", "sched", "logs", "logs", "latest"),
    ]
    assert result["triggered"] == [
        {"pvc": "data", "restore": "r1"},
        {"pvc": "logs", "restore": "r2"},
    ]
    assert result["errors"] == []


def test_disaster_recovery_refuses_when_backup_running() -> None:
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=mock.Mock()),
        mock.patch.object(api, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(api, "_active_backup_jobs", return_value=["running-job"]),
        mock.patch.object(api, "_set_suspend") as set_suspend,
        mock.patch.object(api, "_create_restore") as create_restore,
        pytest.raises(api.HTTPException) as exc,
    ):
        asyncio.run(api.disaster_recovery("ns", "sched"))

    assert exc.value.status_code == 409
    set_suspend.assert_not_called()
    create_restore.assert_not_called()


def test_disaster_recovery_reports_partial_failures() -> None:
    error = api.ApiException(status=500, reason="boom")
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=mock.Mock()),
        mock.patch.object(api, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(api, "_active_backup_jobs", return_value=[]),
        mock.patch.object(api, "_set_suspend"),
        mock.patch.object(api, "_create_restore", side_effect=["r1", error]),
    ):
        result = asyncio.run(api.disaster_recovery("ns", "sched"))

    assert result["triggered"] == [{"pvc": "data", "restore": "r1"}]
    assert result["errors"][0]["pvc"] == "logs"


def test_resume_schedule_unsuspends() -> None:
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=mock.Mock()),
        mock.patch.object(api, "get_backup_schedule", return_value=_schedule_item()),
        mock.patch.object(api, "_set_suspend") as set_suspend,
    ):
        result = asyncio.run(api.resume_schedule("ns", "sched"))

    assert result["status"] == "resumed"
    set_suspend.assert_called_once_with("ns", "sched", False)


def test_schedule_views_include_suspend() -> None:
    item = _schedule_item()
    item["spec"]["suspend"] = True
    with (
        mock.patch.object(api, "get_custom_objects_api", return_value=mock.Mock()),
        mock.patch.object(api, "list_backup_schedules", return_value=[item]),
    ):
        views = api._schedule_views()

    assert views[0]["suspend"] is True
