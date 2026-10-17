"""restic integration: environment, pod hardening, and reading its results.

restic itself runs inside the job pods, never in the operator. This module builds
the environment and security context those pods need, and reads the snapshot IDs
restic reports through the pod termination log so the operator never needs
``pods/log`` RBAC.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from kubernetes.client import (
    V1Capabilities,
    V1EmptyDirVolumeSource,
    V1EnvVar,
    V1EnvVarSource,
    V1Pod,
    V1PodSecurityContext,
    V1SeccompProfile,
    V1SecretKeySelector,
    V1SecurityContext,
    V1Volume,
    V1VolumeMount,
)
from kubernetes.client.exceptions import ApiException, OpenApiException

from crds import RetentionConfig, S3BackendConfig, SnapshotInfo
from k8s import get_core_api

logger = logging.getLogger("restic")

# restic's local cache needs a writable volume, because the container's root
# filesystem is read-only.
CACHE_DIR = "/tmp"


def build_restic_env(
    repo: S3BackendConfig,
    secret_name: str,
    pvc_name: str,
    pvc_namespace: str,
) -> list[V1EnvVar]:
    """Build the environment variables a restic pod needs (S3 only).

    The repository path is variabilized with the PVC namespace and name to avoid
    collisions when several PVCs share one S3 bucket/prefix.
    """
    base_repo = repo.to_restic_repo()
    variabilized_repo = f"{base_repo}/{pvc_namespace}/{pvc_name}"

    return [
        V1EnvVar(name="RESTIC_REPOSITORY", value=variabilized_repo),
        V1EnvVar(name="RESTIC_HOST", value="secchio"),
        V1EnvVar(name="RESTIC_CACHE_DIR", value=CACHE_DIR),
        V1EnvVar(
            name="RESTIC_PASSWORD",
            value_from=V1EnvVarSource(
                secret_key_ref=V1SecretKeySelector(
                    name=secret_name,
                    key="RESTIC_PASSWORD",
                    optional=False,
                ),
            ),
        ),
        V1EnvVar(
            name="AWS_ACCESS_KEY_ID",
            value_from=V1EnvVarSource(
                secret_key_ref=V1SecretKeySelector(
                    name=secret_name,
                    key="AWS_ACCESS_KEY_ID",
                    optional=False,
                ),
            ),
        ),
        V1EnvVar(
            name="AWS_SECRET_ACCESS_KEY",
            value_from=V1EnvVarSource(
                secret_key_ref=V1SecretKeySelector(
                    name=secret_name,
                    key="AWS_SECRET_ACCESS_KEY",
                    optional=False,
                ),
            ),
        ),
    ]


def entrypoint_env(
    mode: str,
    *,
    insecure_tls: bool = False,
    retention: RetentionConfig | None = None,
    snapshot_id: str = "",
) -> list[V1EnvVar]:
    """Environment that configures the restic image's entrypoint.

    The image (see ``restic-pod/``) reads these to decide what to run, instead
    of the operator inlining a shell script into the pod spec.
    """
    env = [V1EnvVar(name="SECCHIO_MODE", value=mode)]
    if insecure_tls:
        env.append(V1EnvVar(name="SECCHIO_INSECURE_TLS", value="true"))
    if retention is not None and (args := retention.to_restic_args()):
        env.append(V1EnvVar(name="SECCHIO_RETENTION", value=" ".join(args)))
    if snapshot_id:
        env.append(V1EnvVar(name="SECCHIO_SNAPSHOT", value=snapshot_id))
    return env


@dataclass
class TerminationInfo:
    """What a restic Job wrote to its pod's /dev/termination-log."""

    snapshot_id: str = ""
    snapshots: list[SnapshotInfo] = field(default_factory=list)
    restored_snapshot_id: str = ""
    error: str = ""


def _pod_creation_time(pod: V1Pod) -> float:
    """Sort key for picking the newest pod; missing timestamps sort first."""
    meta = pod.metadata
    if meta and meta.creation_timestamp:
        return meta.creation_timestamp.timestamp()
    return 0.0


def extract_termination_info(job_name: str, namespace: str) -> TerminationInfo:
    """Parse the last pod's termination log for a Job.

    The restic scripts write ``SNAPSHOT_ID=``, ``SNAP=<id> <time>`` and
    ``RESTORED_SNAPSHOT_ID=`` lines to /dev/termination-log, so status can be read
    from the standard Pod API without ``pods/log`` RBAC access.
    """
    core_api = get_core_api()
    try:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        )
        if not pods.items:
            return TerminationInfo()

        pod_status = max(pods.items, key=_pod_creation_time).status
        statuses = pod_status.container_statuses if pod_status else None
        if not statuses:
            return TerminationInfo()

        term_state = statuses[-1].state.terminated if statuses[-1].state else None
        if not term_state:
            return TerminationInfo()

        message = term_state.message or ""
        info = parse_termination_message(message)
        if not info.snapshot_id and not info.snapshots and term_state.exit_code == 0:
            match = re.search(r"snapshot\s+([a-fA-F0-9]{8,})\s+saved", message)
            if match:
                info.snapshot_id = match.group(1)
        if info.snapshot_id and all(s.id != info.snapshot_id for s in info.snapshots):
            info.snapshots.insert(0, SnapshotInfo(id=info.snapshot_id))
        if term_state.exit_code != 0:
            info.error = message or f"Container exited with code {term_state.exit_code}"
        return info
    except (ApiException, OpenApiException, ValueError, OSError) as e:
        logger.warning("Failed to inspect termination log for job %s: %s", job_name, e)
        return TerminationInfo()


def parse_termination_message(message: str) -> TerminationInfo:
    """Read the ``KEY=value`` lines the restic scripts emit."""
    info = TerminationInfo()
    for raw in message.splitlines():
        line = raw.strip()
        if line.startswith("SNAPSHOT_ID="):
            info.snapshot_id = line.removeprefix("SNAPSHOT_ID=").strip()
        elif line.startswith("SNAP="):
            snap_id, _, snap_time = line.removeprefix("SNAP=").strip().partition(" ")
            if snap_id:
                info.snapshots.append(SnapshotInfo(id=snap_id, time=snap_time.strip()))
        elif line.startswith("RESTORED_SNAPSHOT_ID="):
            info.restored_snapshot_id = line.removeprefix("RESTORED_SNAPSHOT_ID=").strip()
    return info


def cache_volume() -> V1Volume:
    """Scratch space for restic's local cache (the container root is read-only)."""
    return V1Volume(name="restic-cache", empty_dir=V1EmptyDirVolumeSource())


def cache_mount() -> V1VolumeMount:
    """Mount point for :func:`cache_volume`."""
    return V1VolumeMount(name="restic-cache", mount_path=CACHE_DIR)


def pod_security_context() -> V1PodSecurityContext:
    """Pod-level hardening shared by the backup and restore pods."""
    return V1PodSecurityContext(seccomp_profile=V1SeccompProfile(type="RuntimeDefault"))


def container_security_context() -> V1SecurityContext:
    """Container hardening for a restic pod.

    The pod still runs as root, because restic must read the whole volume — but it
    drops every capability and adds back only ``DAC_OVERRIDE``, the one capability
    root needs to bypass file permissions and read (or overwrite) arbitrary files.
    ``DAC_OVERRIDE`` is chosen over the narrower ``DAC_READ_SEARCH`` because it is
    part of the Pod Security Standard *baseline* default capability set, so these
    pods stay admissible in baseline-enforcing namespaces. The root filesystem is
    read-only, so restic's cache is redirected to :data:`CACHE_DIR`.
    """
    return V1SecurityContext(
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        capabilities=V1Capabilities(drop=["ALL"], add=["DAC_OVERRIDE"]),
    )
