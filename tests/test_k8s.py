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
    V1ResourceRequirements,
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
    pvc = mock.Mock(metadata=SimpleNamespace(name="data"))
    api.list_namespaced_persistent_volume_claim.return_value = mock.Mock(items=[pvc])

    assert k8s.list_pvcs_by_selector(api, {"app": "x"}, "ns") == [pvc]
    api.list_namespaced_persistent_volume_claim.assert_called_once_with(
        namespace="ns", label_selector="app=x"
    )


def _pod_mounting(pvc_name: str, node: str = "node-1") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(deletion_timestamp=None),
        spec=SimpleNamespace(
            node_name=node,
            volumes=[SimpleNamespace(persistent_volume_claim=SimpleNamespace(claim_name=pvc_name))],
        ),
        status=SimpleNamespace(phase="Running"),
    )


def test_pod_nodes_by_pvc_maps_scheduled_consumer() -> None:
    api = mock.Mock()
    api.list_namespaced_pod.return_value = mock.Mock(items=[_pod_mounting("data")])

    with mock.patch.object(k8s, "get_core_api", return_value=api):
        assert k8s.pod_nodes_by_pvc("ns") == {"data": "node-1"}


def test_pod_nodes_by_pvc_ignores_unscheduled_pods() -> None:
    api = mock.Mock()
    api.list_namespaced_pod.return_value = mock.Mock(items=[_pod_mounting("data", node="")])

    with mock.patch.object(k8s, "get_core_api", return_value=api):
        assert k8s.pod_nodes_by_pvc("ns") == {}


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


def test_get_pvc_returns_none_on_404() -> None:
    api = mock.Mock()
    api.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)

    assert k8s.get_pvc(api, "data", "ns") is None


def test_get_pvc_raises_on_other_errors() -> None:
    api = mock.Mock()
    api.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=500)

    with pytest.raises(ApiException):
        k8s.get_pvc(api, "data", "ns")


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


def _set_resources(cronjob: V1CronJob, resources: V1ResourceRequirements) -> None:
    spec = cronjob.spec
    assert spec is not None and spec.job_template is not None
    job_spec = spec.job_template.spec
    assert job_spec is not None and job_spec.template is not None
    pod_spec = job_spec.template.spec
    assert pod_spec is not None and pod_spec.containers
    pod_spec.containers[0].resources = resources


def test_create_or_update_cronjob_ignores_equivalent_quantities() -> None:
    desired = _cronjob()
    live = _cronjob()
    # The API server canonicalizes 1000m to 1 and 128Mi to 134217728.
    _set_resources(
        desired,
        V1ResourceRequirements(
            requests={"cpu": "1000m", "memory": "128Mi"},
            limits={"cpu": "1", "memory": "512Mi"},
        ),
    )
    _set_resources(
        live,
        V1ResourceRequirements(
            requests={"cpu": "1", "memory": "134217728"},
            limits={"cpu": "1000m", "memory": "536870912"},
        ),
    )

    batch = mock.Mock()
    batch.create_namespaced_cron_job.side_effect = ApiException(status=409)
    batch.read_namespaced_cron_job.return_value = live

    k8s.create_or_update_cronjob(batch, desired)

    batch.replace_namespaced_cron_job.assert_not_called()


def test_create_or_update_cronjob_replaces_on_resource_drift() -> None:
    desired = _cronjob()
    live = _cronjob()
    _set_resources(desired, V1ResourceRequirements(limits={"cpu": "1"}))
    _set_resources(live, V1ResourceRequirements(limits={"cpu": "2"}))

    batch = mock.Mock()
    batch.create_namespaced_cron_job.side_effect = ApiException(status=409)
    batch.read_namespaced_cron_job.return_value = live

    k8s.create_or_update_cronjob(batch, desired)

    batch.replace_namespaced_cron_job.assert_called_once_with(
        name="bk-cronjob", namespace="ns", body=desired
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
