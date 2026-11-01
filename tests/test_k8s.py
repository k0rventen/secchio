"""Unit tests for the thin Kubernetes helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from kubernetes.client import (
    V1Container,
    V1CronJob,
    V1CronJobSpec,
    V1JobSpec,
    V1JobTemplateSpec,
    V1ObjectMeta,
    V1PodSpec,
    V1PodTemplateSpec,
)
from kubernetes.client.exceptions import ApiException

import k8s
from crds import RestoreStatus


def test_patch_status_falls_back_without_status_subresource() -> None:
    api = mock.Mock()
    api.patch_namespaced_custom_object_status.side_effect = ApiException(status=404)

    k8s.patch_status(api, plural="restores", name="rt", namespace="ns", status=RestoreStatus())

    api.patch_namespaced_custom_object_status.assert_called_once()
    api.patch_namespaced_custom_object.assert_called_once()


def test_list_pvcs_by_selector_is_namespace_scoped() -> None:
    api = mock.Mock()
    api.list_namespaced_persistent_volume_claim.return_value = mock.Mock(
        items=[mock.Mock(metadata=SimpleNamespace(name="data"))]
    )

    assert k8s.list_pvcs_by_selector(api, {"app": "x"}, "ns") == ["data"]
    api.list_namespaced_persistent_volume_claim.assert_called_once_with(
        namespace="ns", label_selector="app=x"
    )


def test_list_cronjobs_for_schedule_uses_label_selector() -> None:
    api = mock.Mock()
    api.list_namespaced_cron_job.return_value = mock.Mock(items=[object()])

    items = k8s.list_cronjobs_for_schedule(api, "ns", "sched")

    assert len(items) == 1
    api.list_namespaced_cron_job.assert_called_once_with(
        namespace="ns", label_selector="secchio.cocointhe.cloud/schedule=sched"
    )


def test_get_backup_schedule_returns_none_on_404() -> None:
    api = mock.Mock()
    api.get_namespaced_custom_object.side_effect = ApiException(status=404)

    assert k8s.get_backup_schedule(api, "sched", "ns") is None


def test_get_backup_schedule_raises_on_other_errors() -> None:
    api = mock.Mock()
    api.get_namespaced_custom_object.side_effect = ApiException(status=500)

    with pytest.raises(ApiException):
        k8s.get_backup_schedule(api, "sched", "ns")


def test_owner_reference_requires_name_and_uid() -> None:
    assert k8s.owner_reference("g/v1", "BackupSchedule", "", "uid") == []
    assert k8s.owner_reference("g/v1", "BackupSchedule", "name", "") == []

    ref = k8s.owner_reference("g/v1", "BackupSchedule", "name", "uid")[0]
    assert ref.controller is True
    assert ref.block_owner_deletion is True


def _cronjob(schedule: str = "0 2 * * *") -> V1CronJob:
    return V1CronJob(
        metadata=V1ObjectMeta(name="bk-cronjob", namespace="ns", labels={"a": "b"}),
        spec=V1CronJobSpec(
            schedule=schedule,
            job_template=V1JobTemplateSpec(
                spec=V1JobSpec(
                    template=V1PodTemplateSpec(
                        spec=V1PodSpec(containers=[V1Container(name="c", image="img")])
                    )
                )
            ),
        ),
    )


def test_create_or_update_cronjob_skips_unchanged_replace() -> None:
    desired = _cronjob()
    live = _cronjob()
    # Server-defaulted fields must not trigger a replace.
    assert live.spec is not None
    live.spec.suspend = False
    job_template = live.spec.job_template
    assert job_template is not None and job_template.spec is not None
    pod_spec = job_template.spec.template.spec
    assert pod_spec is not None and pod_spec.containers
    pod_spec.containers[0].image_pull_policy = "IfNotPresent"

    batch = mock.Mock()
    batch.create_namespaced_cron_job.side_effect = ApiException(status=409)
    batch.read_namespaced_cron_job.return_value = live

    k8s.create_or_update_cronjob(batch, desired)

    batch.replace_namespaced_cron_job.assert_not_called()


def test_create_or_update_cronjob_replaces_on_drift() -> None:
    desired = _cronjob()
    live = _cronjob(schedule="0 1 * * *")

    batch = mock.Mock()
    batch.create_namespaced_cron_job.side_effect = ApiException(status=409)
    batch.read_namespaced_cron_job.return_value = live

    k8s.create_or_update_cronjob(batch, desired)

    batch.replace_namespaced_cron_job.assert_called_once_with(
        name="bk-cronjob", namespace="ns", body=desired
    )


def test_create_or_update_cronjob_recreates_after_delete_race() -> None:
    batch = mock.Mock()
    batch.create_namespaced_cron_job.side_effect = [ApiException(status=409), None]
    batch.read_namespaced_cron_job.side_effect = ApiException(status=404)

    k8s.create_or_update_cronjob(batch, _cronjob())

    assert batch.create_namespaced_cron_job.call_count == 2
    batch.replace_namespaced_cron_job.assert_not_called()
