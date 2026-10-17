"""Backup workload construction

Builds the Kubernetes CronJob that runs the restic image for a single PVC. The
image's entrypoint (see ``restic-pod/``) does the actual work — init the repo,
back up /data, then forget per the retention policy — driven by the SECCHIO_*
environment variables set here.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import (
    V1CronJob,
    V1CronJobSpec,
    V1JobSpec,
    V1JobTemplateSpec,
    V1ObjectMeta,
    V1PodTemplateSpec,
)

from common import CRD_GROUP, CRD_VERSION, make_cronjob_labels
from crds import RetentionConfig, S3BackendConfig
from k8s import owner_reference
from restic import build_restic_env, build_restic_pod, entrypoint_env


@dataclass
class JobConfig:
    """Configuration for a backup CronJob."""

    name: str
    namespace: str
    pvc_name: str
    pvc_namespace: str
    repo: S3BackendConfig
    retention: RetentionConfig
    image: str
    secret_name: str
    schedule_name: str = ""
    backup_name: str = ""
    backup_uid: str = ""


def create_cronjob(config: JobConfig, schedule: str) -> V1CronJob:
    """Create a Kubernetes CronJob that backs up a single PVC with restic."""
    env = build_restic_env(config.repo, config.secret_name, config.pvc_name, config.pvc_namespace)
    env += entrypoint_env(
        "backup",
        insecure_tls=config.repo.insecure_tls,
        retention=config.retention,
    )
    pod_spec = build_restic_pod(
        container_name="restic-backup",
        image=config.image,
        env=env,
        pvc_name=config.pvc_name,
        read_only=True,
    )

    schedule_label = config.schedule_name or (
        config.name.split("-")[0] if "-" in config.name else config.name
    )
    backup_label = config.backup_name or config.name
    cronjob_labels = make_cronjob_labels(schedule_label, backup_label, config.pvc_name)

    return V1CronJob(
        api_version="batch/v1",
        kind="CronJob",
        metadata=V1ObjectMeta(
            name=config.name,
            namespace=config.namespace,
            labels=cronjob_labels,
            owner_references=owner_reference(
                f"{CRD_GROUP}/{CRD_VERSION}", "Backup", config.backup_name, config.backup_uid
            )
            or None,
        ),
        spec=V1CronJobSpec(
            schedule=schedule,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=2,
            job_template=V1JobTemplateSpec(
                metadata=V1ObjectMeta(labels=cronjob_labels),
                spec=V1JobSpec(
                    backoff_limit=2,
                    template=V1PodTemplateSpec(
                        metadata=V1ObjectMeta(labels=cronjob_labels),
                        spec=pod_spec,
                    ),
                ),
            ),
        ),
    )
