"""Unit tests for the UI-triggered manual backup Job."""

from __future__ import annotations

from unittest import mock

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
