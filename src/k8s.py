"""Thin, domain-agnostic Kubernetes access.

Cached API clients plus create/update/delete/list helpers and status patching.
Nothing here knows about restic, schedules or the backup domain; that lives in
:mod:`reconcile`.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from kubernetes import client
from kubernetes.client import (
    BatchV1Api,
    CustomObjectsApi,
    V1CronJob,
    V1Job,
    V1OwnerReference,
    V1PersistentVolumeClaim,
)
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from common import CRD_GROUP, CRD_PLURAL_BACKUPSCHEDULES, CRD_VERSION, LABEL_SCHEDULE
from crds import BackupScheduleStatus, RestoreStatus

logger = logging.getLogger("k8s")


# Cached API clients
@functools.cache
def get_core_api() -> client.CoreV1Api:
    """Return a cached CoreV1Api client."""
    return client.CoreV1Api()


@functools.cache
def get_custom_objects_api() -> CustomObjectsApi:
    """Return a cached CustomObjectsApi client."""
    return CustomObjectsApi()


@functools.cache
def get_batch_api() -> BatchV1Api:
    """Return a cached BatchV1Api client."""
    return BatchV1Api()


# Status
def patch_status(
    custom_api: CustomObjectsApi,
    *,
    plural: str,
    name: str,
    namespace: str,
    status: BackupScheduleStatus | RestoreStatus,
) -> None:
    """Write a namespaced CR status, using the status subresource when available.

    Falls back to patching the main object if the API answers 404, which is what
    happens when a CRD was applied without ``subresources.status``.
    """
    params: dict[str, Any] = {
        "group": CRD_GROUP,
        "version": CRD_VERSION,
        "plural": plural,
        "name": name,
        "namespace": namespace,
        "body": {"status": status.model_dump(by_alias=True, mode="json")},
    }
    try:
        custom_api.patch_namespaced_custom_object_status(**params)
    except ApiException as e:
        if e.status != 404:
            logger.exception("Failed to update status for %s %s", plural, name)
            return
        try:
            custom_api.patch_namespaced_custom_object(**params)
        except ApiException:
            logger.exception("Failed to fallback-patch status for %s %s", plural, name)


# Workloads
def owner_reference(api_version: str, kind: str, name: str, uid: str) -> list[V1OwnerReference]:
    """A controller owner reference, or ``[]`` when name or uid is missing."""
    if not name or not uid:
        return []
    return [
        V1OwnerReference(
            api_version=api_version,
            kind=kind,
            name=name,
            uid=uid,
            controller=True,
            block_owner_deletion=True,
        )
    ]


def create_or_update_cronjob(batch_api: BatchV1Api, cronjob: V1CronJob) -> None:
    """Create a CronJob, replacing it only when the live one has drifted."""
    namespace = (cronjob.metadata.namespace or "") if cronjob.metadata else ""
    name = (cronjob.metadata.name or "") if cronjob.metadata else ""
    try:
        batch_api.create_namespaced_cron_job(namespace=namespace, body=cronjob)
        logger.info("Created cronjob %s", name)
        return
    except ApiException as e:
        if e.status != 409:
            raise

    try:
        existing = batch_api.read_namespaced_cron_job(name=name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        # Deleted between the create attempt and the read: recreate it.
        batch_api.create_namespaced_cron_job(namespace=namespace, body=cronjob)
        logger.info("Created cronjob %s", name)
        return

    drift = _cronjob_drift(existing, cronjob)
    if drift is None:
        logger.debug("Cronjob %s is up to date", name)
        return

    batch_api.replace_namespaced_cron_job(name=name, namespace=namespace, body=cronjob)
    logger.info("Updated cronjob %s (%s)", name, drift)


def _cronjob_drift(existing: V1CronJob, desired: V1CronJob) -> str | None:
    """The first field the operator sets that differs from the live CronJob.

    Kubernetes adds defaults on read, so the desired object is compared as a subset
    of the live object rather than for full equality.
    """
    desired_meta = desired.metadata.to_dict() if desired.metadata else {}
    existing_meta = existing.metadata.to_dict() if existing.metadata else {}
    for field in ("labels", "owner_references"):
        mismatch = _first_mismatch(desired_meta.get(field), existing_meta.get(field), field)
        if mismatch is not None:
            return mismatch
    if desired.spec is None or existing.spec is None:
        return None if desired.spec is existing.spec else "spec: one side is missing"
    desired_spec = desired.spec.to_dict()
    existing_spec = existing.spec.to_dict()
    _normalize_resource_quantities(desired_spec)
    _normalize_resource_quantities(existing_spec)
    return _first_mismatch(desired_spec, existing_spec, "spec")


def _normalize_resource_quantities(spec: dict[str, Any]) -> None:
    """Replace resource quantities with numbers so equivalent values compare equal.

    The API server canonicalizes quantities (``1000m`` becomes ``1``), so comparing
    the raw strings would report drift for values that mean the same thing.
    """
    job_spec = (spec.get("job_template") or {}).get("spec") or {}
    pod_spec = (job_spec.get("template") or {}).get("spec") or {}
    containers = pod_spec.get("containers")
    if not isinstance(containers, list):
        return
    for container in containers:
        resources = container.get("resources") if isinstance(container, dict) else None
        if not isinstance(resources, dict):
            continue
        for section in ("requests", "limits"):
            quantities = resources.get(section)
            if not isinstance(quantities, dict):
                continue
            for name, value in quantities.items():
                if isinstance(value, (str, int, float)):
                    try:
                        quantities[name] = parse_quantity(str(value))
                    except ValueError:
                        pass


def _first_mismatch(desired: Any, live: Any, path: str) -> str | None:
    """Describe the first value set in ``desired`` that does not match ``live``.

    ``None`` values in the desired object mean "not managed by the operator" and are
    ignored, which lets server-defaulted fields pass without a replace.
    """
    if desired is None:
        return None
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return f"{path}: expected object, found {type(live).__name__}"
        for key, value in desired.items():
            if value is None:
                continue
            if key not in live:
                return f"{path}.{key}: missing"
            mismatch = _first_mismatch(value, live[key], f"{path}.{key}")
            if mismatch is not None:
                return mismatch
        return None
    if isinstance(desired, list):
        if not isinstance(live, list):
            return f"{path}: expected list, found {type(live).__name__}"
        if len(desired) != len(live):
            return f"{path}: length {len(desired)} != {len(live)}"
        for index, (desired_item, live_item) in enumerate(zip(desired, live, strict=True)):
            mismatch = _first_mismatch(desired_item, live_item, f"{path}[{index}]")
            if mismatch is not None:
                return mismatch
        return None
    if desired != live:
        return f"{path}: {desired!r} != {live!r}"
    return None


def delete_cronjob(batch_api: BatchV1Api, name: str, namespace: str) -> None:
    """Delete a CronJob by name and namespace."""
    try:
        batch_api.delete_namespaced_cron_job(name=name, namespace=namespace)
        logger.info("Deleted cronjob %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


def create_or_update_restore_job(batch_api: BatchV1Api, job: V1Job) -> bool:
    """Create a restore Job, returning False if it already exists."""
    namespace = (job.metadata.namespace or "") if job.metadata else ""
    name = (job.metadata.name or "") if job.metadata else ""
    try:
        batch_api.create_namespaced_job(namespace=namespace, body=job)
        logger.info("Created restore job %s in %s", name, namespace)
        return True
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("Restore job %s already exists in %s", name, namespace)
        return False


def delete_job(batch_api: BatchV1Api, name: str, namespace: str) -> None:
    """Delete a batch Job by name and namespace."""
    try:
        batch_api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            propagation_policy="Background",
        )
        logger.info("Deleted job %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


# BackupSchedule custom resources
def get_backup_schedule(
    custom_api: CustomObjectsApi,
    name: str,
    namespace: str,
) -> dict[str, Any] | None:
    """Get a BackupSchedule by name, or ``None`` if it does not exist."""
    try:
        item: Any = custom_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural=CRD_PLURAL_BACKUPSCHEDULES,
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    return item if isinstance(item, dict) else None


# Discovery
def list_cronjobs_for_schedule(
    batch_api: BatchV1Api,
    namespace: str,
    schedule_name: str,
) -> list[V1CronJob]:
    """List the CronJobs that belong to a schedule."""
    try:
        result = batch_api.list_namespaced_cron_job(
            namespace=namespace,
            label_selector=f"{LABEL_SCHEDULE}={schedule_name}",
        )
    except ApiException:
        logger.exception("Failed to list cronjobs for schedule %s in %s", schedule_name, namespace)
        return []
    return result.items or []


def list_pvcs_by_selector(
    api: client.CoreV1Api,
    selector: dict[str, str],
    namespace: str,
) -> list[V1PersistentVolumeClaim]:
    """List PVCs matching a label selector in a namespace.

    API errors propagate: treating a failed list as "no PVCs" would make the
    reconciler delete every CronJob for the schedule.
    """
    compiled_selectors = ",".join(f"{k}={v}" for k, v in selector.items())
    pvcs = api.list_namespaced_persistent_volume_claim(
        namespace=namespace,
        label_selector=compiled_selectors,
    )
    return [p for p in pvcs.items if p.metadata and p.metadata.name]


def get_pvc(
    api: client.CoreV1Api,
    name: str,
    namespace: str,
) -> V1PersistentVolumeClaim | None:
    """Get a PVC by name, or ``None`` if it does not exist."""
    try:
        return api.read_namespaced_persistent_volume_claim(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def pod_nodes_by_pvc(namespace: str) -> dict[str, str]:
    """Map each PVC to the node of a scheduled pod mounting it."""
    pods = get_core_api().list_namespaced_pod(namespace=namespace)
    nodes: dict[str, str] = {}
    for pod in pods.items:
        meta, spec = pod.metadata, pod.spec
        if not meta or not spec or not spec.node_name or meta.deletion_timestamp:
            continue
        if pod.status and pod.status.phase in ("Succeeded", "Failed"):
            continue
        for volume in spec.volumes or []:
            claim = volume.persistent_volume_claim
            if claim and claim.claim_name:
                nodes.setdefault(claim.claim_name, spec.node_name)
    return nodes


def list_custom_objects(api: CustomObjectsApi, plural: str) -> list[dict[str, Any]]:
    """List a custom resource across all namespaces, or ``[]`` on failure."""
    try:
        result: Any = api.list_cluster_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=plural,
        )
    except ApiException:
        logger.exception("Failed to list %s", plural)
        return []
    items = result.get("items", []) if isinstance(result, dict) else []
    return [item for item in items if isinstance(item, dict)]
