"""Restore job creation and execution logic.

This module handles creating Kubernetes Jobs that run restic to restore
snapshots to a target PersistentVolumeClaim.
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


def build_restore_command(snapshot_id: str = "latest", insecure_tls: bool = False) -> list[str]:
    """Build the restic command sequence for restoring a snapshot into /data."""
    tls_flag = " --insecure-tls" if insecure_tls else ""
    target_snap = snapshot_id or "latest"

    script_lines = [
        "set -euo pipefail",
        f"restic restore {target_snap} --delete --target / -i /data --verbose{tls_flag}",
        f'RESOLVED_SNAP=$(restic snapshots {target_snap} --compact{tls_flag} 2>/dev/null | grep -E "^[a-f0-9]{{8}}" | cut -d" " -f1 | head -n 1 || echo "{target_snap}")',
        'printf "RESTORED_SNAPSHOT_ID=%s\\n" "$RESOLVED_SNAP" > /dev/termination-log',
    ]

    return ["/bin/sh", "-c", "\n".join(script_lines)]


def create_restore_job(config: RestoreJobConfig) -> V1Job:
    """Create a Kubernetes batch/v1 Job for restoring data to target PVC."""
    env = build_restic_env(
        config.repo,
        config.secret_name,
        config.source_pvc,
        config.source_pvc_namespace,
    )
    cmd = build_restore_command(config.snapshot_id, config.repo.insecure_tls)

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
        command=cmd,
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
