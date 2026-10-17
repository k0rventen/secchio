"""Restore job creation and execution logic.

Builds the Kubernetes Job that runs the restic image to restore a snapshot into
a target PersistentVolumeClaim. The image's entrypoint (see ``restic-pod/``)
does the actual work, driven by the SECCHIO_* environment variables set here.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import (
    V1Job,
    V1JobSpec,
    V1ObjectMeta,
    V1PodTemplateSpec,
)

from common import CRD_GROUP, CRD_VERSION, make_restore_labels
from crds import S3BackendConfig
from k8s import owner_reference
from restic import build_restic_env, build_restic_pod, entrypoint_env


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
    pod_spec = build_restic_pod(
        container_name="restic-restore",
        image=config.image,
        env=env,
        pvc_name=config.target_pvc,
        read_only=False,
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
            owner_references=owner_reference(
                f"{CRD_GROUP}/{CRD_VERSION}", "Restore", config.restore_name, config.restore_uid
            )
            or None,
        ),
        spec=V1JobSpec(
            backoff_limit=1,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=restore_labels),
                spec=pod_spec,
            ),
        ),
    )
