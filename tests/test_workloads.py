"""Regression tests: the restic workload pods stay hardened."""

from __future__ import annotations

from kubernetes.client import V1Container, V1PodSpec

from backup import JobConfig, create_cronjob
from crds import RetentionConfig, S3BackendConfig
from restore import RestoreJobConfig, create_restore_job

REPO = S3BackendConfig(endpoint="minio/bucket", secret_name="creds")
IMAGE = "restic/restic:latest"


def _backup_pod() -> V1PodSpec:
    cronjob = create_cronjob(
        JobConfig(
            name="bk-cronjob",
            namespace="ns",
            pvc_name="data",
            pvc_namespace="ns",
            repo=REPO,
            retention=RetentionConfig(keep_daily=1),
            image=IMAGE,
            secret_name="creds",
        ),
        schedule="* * * * *",
    )
    spec = cronjob.spec
    assert spec is not None and spec.job_template is not None
    job_template = spec.job_template.spec
    assert job_template is not None and job_template.template is not None
    pod = job_template.template.spec
    assert pod is not None
    return pod


def _restore_pod() -> V1PodSpec:
    job = create_restore_job(
        RestoreJobConfig(
            name="rt-job",
            namespace="ns",
            target_pvc="data",
            source_pvc="data",
            source_pvc_namespace="ns",
            repo=REPO,
            secret_name="creds",
            image=IMAGE,
        )
    )
    spec = job.spec
    assert spec is not None and spec.template is not None
    pod = spec.template.spec
    assert pod is not None
    return pod


def _container(pod: V1PodSpec) -> V1Container:
    assert pod.containers
    return pod.containers[0]


def _env_value(container: V1Container, name: str) -> str | None:
    for var in container.env or []:
        if var.name == name:
            return var.value
    return None


def _assert_common_hardening(pod: V1PodSpec, container: V1Container) -> None:
    assert pod.automount_service_account_token is False
    assert pod.enable_service_links is False

    assert pod.security_context is not None
    assert pod.security_context.seccomp_profile is not None
    assert pod.security_context.seccomp_profile.type == "RuntimeDefault"

    context = container.security_context
    assert context is not None
    assert context.allow_privilege_escalation is False
    assert context.read_only_root_filesystem is True
    assert context.capabilities is not None
    assert context.capabilities.drop == ["ALL"]
    # DAC_OVERRIDE is baseline-PSA-legal; DAC_READ_SEARCH is not.
    assert context.capabilities.add == ["DAC_OVERRIDE"]

    # restic's cache must live on a writable volume.
    assert _env_value(container, "RESTIC_CACHE_DIR") == "/tmp"
    assert any(v.name == "restic-cache" for v in pod.volumes or [])
    assert any(m.mount_path == "/tmp" for m in container.volume_mounts or [])


def test_backup_pod_is_hardened() -> None:
    pod = _backup_pod()
    container = _container(pod)
    _assert_common_hardening(pod, container)

    data_mount = next(m for m in container.volume_mounts or [] if m.mount_path == "/data")
    assert data_mount.read_only is True


def test_restore_pod_is_hardened() -> None:
    pod = _restore_pod()
    container = _container(pod)
    _assert_common_hardening(pod, container)

    data_mount = next(m for m in container.volume_mounts or [] if m.mount_path == "/data")
    assert data_mount.read_only is False


def test_backup_cronjob_spec_and_env() -> None:
    cronjob = create_cronjob(
        JobConfig(
            name="bk-cronjob",
            namespace="ns",
            pvc_name="data",
            pvc_namespace="ns",
            repo=REPO,
            retention=RetentionConfig(keep_daily=1),
            image=IMAGE,
            secret_name="creds",
            schedule_name="sched",
            backup_name="bk",
            backup_uid="uid",
        ),
        schedule="0 2 * * *",
    )

    assert cronjob.metadata is not None
    assert cronjob.metadata.name == "bk-cronjob"
    assert cronjob.metadata.namespace == "ns"
    assert cronjob.metadata.owner_references is not None
    assert cronjob.metadata.owner_references[0].kind == "Backup"

    spec = cronjob.spec
    assert spec is not None
    assert spec.schedule == "0 2 * * *"
    assert spec.concurrency_policy == "Forbid"
    assert spec.successful_jobs_history_limit == 1
    assert spec.failed_jobs_history_limit == 2

    job_template = spec.job_template
    assert job_template is not None and job_template.spec is not None
    pod = job_template.spec.template.spec
    assert pod is not None
    container = _container(pod)
    assert _env_value(container, "RESTIC_REPOSITORY") == "s3:minio/bucket/ns/data"
    assert _env_value(container, "SECCHIO_MODE") == "backup"
    assert _env_value(container, "SECCHIO_RETENTION") == "--keep-daily 1"


def test_restore_job_spec_and_env() -> None:
    job = create_restore_job(
        RestoreJobConfig(
            name="rt-job",
            namespace="ns",
            target_pvc="target",
            source_pvc="source",
            source_pvc_namespace="src-ns",
            repo=REPO,
            secret_name="creds",
            image=IMAGE,
            snapshot_id="abc123",
            restore_name="rt",
            restore_uid="uid",
            backup_name="bk",
        )
    )

    assert job.metadata is not None
    assert job.metadata.name == "rt-job"
    assert job.metadata.owner_references is not None
    assert job.metadata.owner_references[0].kind == "Restore"

    spec = job.spec
    assert spec is not None
    assert spec.backoff_limit == 1

    pod = spec.template.spec
    assert pod is not None
    container = _container(pod)
    assert _env_value(container, "RESTIC_REPOSITORY") == "s3:minio/bucket/src-ns/source"
    assert _env_value(container, "SECCHIO_MODE") == "restore"
    assert _env_value(container, "SECCHIO_SNAPSHOT") == "abc123"
