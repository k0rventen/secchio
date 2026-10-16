"""Restore job creation and execution logic.

Builds the Kubernetes Job that runs the restic image to restore a snapshot into
a target PersistentVolumeClaim. The image's entrypoint (see ``restic-pod/``)
does the actual work, driven by the SECCHIO_* environment variables set here.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import (
    V1Container,
    V1Job,
    V1JobSpec,
    V1ObjectMeta,
    V1OwnerReference,
    V1PersistentVolumeClaimVolumeSource,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
    V1Volume,
    V1VolumeMount,
)

from common import CRD_GROUP, CRD_VERSION, make_restore_labels
from crds import S3BackendConfig
from restic import (
    build_restic_env,
    cache_mount,
    cache_volume,
    container_security_context,
    entrypoint_env,
    pod_security_context,
)


@dataclass
class RestoreJobConfig:
    """Configuration for a restore Job."""

    name: str
    namespace: str
    target_pvc: str
    source_pvc: str
    source_pvc_namespace: str
    repo: S3BackendConfig
    secret_name: str
    image: str
    snapshot_id: str = "latest"
    restore_name: str = ""
    restore_uid: str = ""
    backup_name: str = ""


def create_restore_job(config: RestoreJobConfig) -> V1Job:
    """Create a Kubernetes batch/v1 Job for restoring data to target PVC."""
    env = build_restic_env(
        config.repo,
        config.secret_name,
        config.source_pvc,
        config.source_pvc_namespace,
    )
    env += entrypoint_env(
        "restore",
        insecure_tls=config.repo.insecure_tls,
        snapshot_id=config.snapshot_id,
    )

    volumes = [
        V1Volume(
            name=f"pvc-{config.target_pvc}",
            persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                claim_name=config.target_pvc,
            ),
        ),
        cache_volume(),
    ]

    mounts = [
        V1VolumeMount(
            name=f"pvc-{config.target_pvc}",
            mount_path="/data",
            read_only=False,
        ),
        cache_mount(),
    ]

    container = V1Container(
        name="restic-restore",
        image=config.image,
        env=env,
        volume_mounts=mounts,
        security_context=container_security_context(),
        termination_message_path="/dev/termination-log",
        termination_message_policy="FallbackToLogsOnError",
        resources=V1ResourceRequirements(
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "1000m", "memory": "512Mi"},
        ),
    )

    pod_spec = V1PodSpec(
        restart_policy="OnFailure",
        containers=[container],
        volumes=volumes,
        security_context=pod_security_context(),
        automount_service_account_token=False,
        enable_service_links=False,
    )

    owner_references = []
    if config.restore_name and config.restore_uid:
        owner_references.append(
            V1OwnerReference(
                api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                kind="Restore",
                name=config.restore_name,
                uid=config.restore_uid,
                controller=True,
                block_owner_deletion=True,
            ),
        )

    restore_labels = make_restore_labels(
        restore_name=config.restore_name or config.name,
        backup_name=config.backup_name,
        target_pvc=config.target_pvc,
    )

    return V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=config.name,
            namespace=config.namespace,
            labels=restore_labels,
            owner_references=owner_references or None,
        ),
        spec=V1JobSpec(
            backoff_limit=1,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=restore_labels),
                spec=pod_spec,
            ),
        ),
    )
