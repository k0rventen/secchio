"""Backup workload construction

Builds the Kubernetes CronJob that runs restic for a single PVC
creates the repo if it does not exists yet, then restic backup (from /data where the pvc is mounted)
then restic forget if the retention is configured
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes.client import (
    V1Container,
    V1CronJob,
    V1CronJobSpec,
    V1JobSpec,
    V1JobTemplateSpec,
    V1ObjectMeta,
    V1OwnerReference,
    V1PersistentVolumeClaimVolumeSource,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
    V1Volume,
    V1VolumeMount,
)

from common import make_cronjob_labels
from crds import RetentionConfig, S3BackendConfig
from restic import (
    build_restic_env,
    cache_mount,
    cache_volume,
)


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


def build_backup_command(retention: RetentionConfig, insecure_tls: bool = False) -> list[str]:
    """Build the restic command sequence (init -> backup -> forget) for a backup job.

    Captures the new snapshot ID and remaining snapshot IDs, writing them to
    /dev/termination-log so the operator can retrieve status via standard pod.status
    without requiring cluster-wide pods/log RBAC access.
    """
    tls_flag = " --insecure-tls" if insecure_tls else ""
    forget_step = ""
    if args := retention.to_restic_args():
        forget_step = f"restic forget{tls_flag} --verbose --prune " + " ".join(args)

    script_lines = [
        "set -euo pipefail",
        f"restic snapshots{tls_flag} > /dev/null 2>&1 || restic init{tls_flag}",
        f"BACKUP_OUT=$(restic backup /data --verbose{tls_flag})",
        'echo "$BACKUP_OUT"',
        "SNAP_ID=$(echo \"$BACKUP_OUT\" | grep -oE 'snapshot [a-f0-9]+ saved' | cut -d' ' -f2 || true)",
    ]
    if forget_step:
        script_lines.append(forget_step)

    script_lines.extend(
        [
            f'ALL_SNAPS=$(restic snapshots --compact{tls_flag} 2>/dev/null | grep -E "^[a-f0-9]{{8}}" | cut -d" " -f1 | tr "\\n" "," | sed "s/,$//" || true)',
            'printf "SNAPSHOT_ID=%s\\nALL_SNAPSHOTS=%s\\n" "$SNAP_ID" "$ALL_SNAPS" > /dev/termination-log',
        ],
    )

    return ["/bin/sh", "-c", "\n".join(script_lines)]


def create_cronjob(config: JobConfig, schedule: str) -> V1CronJob:
    """Create a Kubernetes CronJob that backs up a single PVC with restic."""
    env = build_restic_env(config.repo, config.secret_name, config.pvc_name, config.pvc_namespace)
    cmd = build_backup_command(config.retention, config.repo.insecure_tls)

    volumes = [
        V1Volume(
            name=f"pvc-{config.pvc_name}",
            persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(claim_name=config.pvc_name),
        ),
        cache_volume(),
    ]

    mounts = [
        V1VolumeMount(
            name=f"pvc-{config.pvc_name}",
            mount_path="/data",
            read_only=True,
        ),
        cache_mount(),
    ]

    container = V1Container(
        name="restic-backup",
        image=config.image,
        command=cmd,
        env=env,
        volume_mounts=mounts,
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
        automount_service_account_token=False,
        enable_service_links=False,
    )

    owner_references = []
    if config.backup_name and config.backup_uid:
        owner_references.append(
            V1OwnerReference(
                api_version="secchio.cocointhe.cloud/v1alpha1",
                kind="Backup",
                name=config.backup_name,
                uid=config.backup_uid,
                controller=True,
                block_owner_deletion=True,
            ),
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
            owner_references=owner_references or None,
        ),
        spec=V1CronJobSpec(
            schedule=schedule,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=2,
            job_template=V1JobTemplateSpec(
                metadata=V1ObjectMeta(labels=cronjob_labels),
                spec=V1JobSpec(
                    template=V1PodTemplateSpec(
                        metadata=V1ObjectMeta(labels=cronjob_labels),
                        spec=pod_spec,
                    ),
                ),
            ),
        ),
    )
