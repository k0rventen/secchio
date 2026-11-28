"""Kubernetes workload construction for backups and restores.

The restic image's entrypoint (see ``restic-pod/``) does the actual work; the
operator only builds the CronJob/Job and the ``SECCHIO_*`` environment that drives
it.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import (
    V1CronJob,
    V1CronJobSpec,
    V1Job,
    V1JobSpec,
    V1JobTemplateSpec,
    V1ObjectMeta,
    V1OwnerReference,
    V1PodSpec,
    V1PodTemplateSpec,
)

from common import (
    CRD_GROUP,
    CRD_VERSION,
    JOB_TTL_SECONDS,
    make_cronjob_labels,
    make_restore_labels,
    make_scan_labels,
)
from crds import RetentionConfig, S3BackendConfig
from k8s import owner_reference
from restic import build_restic_env, build_restic_pod, entrypoint_env


@dataclass
class WorkloadConfig:
    """Everything needed to build a backup CronJob or a restore Job."""

    name: str
    namespace: str
    pvc_name: str
    repo: S3BackendConfig
    image: str
    mode: str
    resources: dict[str, dict[str, str]] | None = None
    start_delay_seconds: int = 0
    node_selector: dict[str, str] | None = None
    schedule: str = ""
    time_zone: str = ""
    suspend: bool = False
    retention: RetentionConfig | None = None
    snapshot_id: str = ""
    target_pvc: str = ""
    schedule_name: str = ""
    owner_kind: str = ""
    owner_name: str = ""
    owner_uid: str = ""


def _pod(
    config: WorkloadConfig,
    *,
    container_name: str,
    read_only: bool,
    pvc_name: str,
    mount_pvc: bool = True,
) -> V1PodSpec:
    env = build_restic_env(config.repo, config.pvc_name, config.namespace)
    env += entrypoint_env(
        config.mode,
        insecure_tls=config.repo.insecure_tls,
        retention=config.retention,
        snapshot_id=config.snapshot_id,
        start_delay_seconds=config.start_delay_seconds,
    )
    return build_restic_pod(
        container_name=container_name,
        image=config.image,
        env=env,
        pvc_name=pvc_name,
        read_only=read_only,
        resources=config.resources,
        node_selector=config.node_selector,
        mount_pvc=mount_pvc,
    )


def _pod_template(labels: dict[str, str], pod_spec: V1PodSpec) -> V1PodTemplateSpec:
    return V1PodTemplateSpec(metadata=V1ObjectMeta(labels=labels), spec=pod_spec)


def _owner(config: WorkloadConfig) -> list[V1OwnerReference] | None:
    return (
        owner_reference(
            f"{CRD_GROUP}/{CRD_VERSION}",
            config.owner_kind,
            config.owner_name,
            config.owner_uid,
        )
        or None
    )


def create_cronjob(config: WorkloadConfig) -> V1CronJob:
    """Build the CronJob that backs up one PVC."""
    labels = make_cronjob_labels(config.schedule_name, config.pvc_name)
    pod_spec = _pod(
        config,
        container_name="restic-backup",
        read_only=True,
        pvc_name=config.pvc_name,
    )
    return V1CronJob(
        api_version="batch/v1",
        kind="CronJob",
        metadata=V1ObjectMeta(
            name=config.name,
            namespace=config.namespace,
            labels=labels,
            owner_references=_owner(config),
        ),
        spec=V1CronJobSpec(
            schedule=config.schedule,
            time_zone=config.time_zone or None,
            suspend=config.suspend,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=2,
            job_template=V1JobTemplateSpec(
                metadata=V1ObjectMeta(labels=labels),
                spec=V1JobSpec(
                    backoff_limit=2,
                    ttl_seconds_after_finished=JOB_TTL_SECONDS,
                    template=_pod_template(labels, pod_spec),
                ),
            ),
        ),
    )


def _job(config: WorkloadConfig, labels: dict[str, str], pod_spec: V1PodSpec) -> V1Job:
    """A one-off Job wrapping ``pod_spec`` with the shared Job settings."""
    return V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=config.name,
            namespace=config.namespace,
            labels=labels,
            owner_references=_owner(config),
        ),
        spec=V1JobSpec(
            backoff_limit=1,
            ttl_seconds_after_finished=JOB_TTL_SECONDS,
            template=_pod_template(labels, pod_spec),
        ),
    )


def create_scan_job(config: WorkloadConfig) -> V1Job:
    """Build the one-off Job that lists a PVC's snapshots in the restic repo."""
    labels = make_scan_labels(config.schedule_name, config.pvc_name)
    pod_spec = _pod(
        config,
        container_name="restic-scan",
        read_only=True,
        pvc_name=config.pvc_name,
        mount_pvc=False,
    )
    return _job(config, labels, pod_spec)


def create_restore_job(config: WorkloadConfig) -> V1Job:
    """Build the one-off Job that restores a snapshot into a PVC."""
    labels = make_restore_labels(config.owner_name, config.schedule_name, config.pvc_name)
    pod_spec = _pod(
        config,
        container_name="restic-restore",
        read_only=False,
        pvc_name=config.target_pvc or config.pvc_name,
    )
    return _job(config, labels, pod_spec)
