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
from kubernetes.client import BatchV1Api, CustomObjectsApi, V1CronJob, V1Job, V1OwnerReference
from kubernetes.client.exceptions import ApiException, OpenApiException

from common import (
    CRD_GROUP,
    CRD_PLURAL_BACKUPS,
    CRD_PLURAL_BACKUPSCHEDULES,
    CRD_VERSION,
    LABEL_SCHEDULE,
)
from crds import BackupScheduleStatus, BackupStatus, RestoreStatus

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


# Generic listing
def list_cluster_custom(
    api: CustomObjectsApi,
    plural: str,
    label_selector: str = "",
) -> list[dict[str, Any]]:
    """List a cluster-scoped custom resource, returning ``[]`` on failure."""
    params: dict[str, Any] = {"group": CRD_GROUP, "version": CRD_VERSION, "plural": plural}
    if label_selector:
        params["label_selector"] = label_selector
    try:
        result: Any = api.list_cluster_custom_object(**params)
    except ApiException:
        logger.exception("Failed to list %s", plural)
        return []
    items = result.get("items", []) if isinstance(result, dict) else []
    return [item for item in items if isinstance(item, dict)]


# Status
def patch_status(
    custom_api: CustomObjectsApi,
    *,
    plural: str,
    name: str,
    status: BackupStatus | RestoreStatus | BackupScheduleStatus,
    namespace: str | None = None,
) -> None:
    """Write a CR status, using the status subresource when it is available.

    Falls back to patching the main object if the API answers 404, which is what
    happens when a CRD was applied without ``subresources.status``.
    """
    body = {"status": status.model_dump(by_alias=True, mode="json")}
    cluster = namespace is None
    params: dict[str, Any] = {
        "group": CRD_GROUP,
        "version": CRD_VERSION,
        "plural": plural,
        "name": name,
        "body": body,
    }
    if namespace is not None:
        params["namespace"] = namespace

    def write(subresource: bool) -> None:
        if cluster and subresource:
            custom_api.patch_cluster_custom_object_status(**params)
        elif cluster:
            custom_api.patch_cluster_custom_object(**params)
        elif subresource:
            custom_api.patch_namespaced_custom_object_status(**params)
        else:
            custom_api.patch_namespaced_custom_object(**params)

    try:
        write(subresource=True)
    except ApiException as e:
        if e.status != 404:
            logger.exception("Failed to update status for %s %s", plural, name)
            return
        try:
            write(subresource=False)
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
    """Create a CronJob, replacing it if it already exists."""
    namespace = (cronjob.metadata.namespace or "") if cronjob.metadata else ""
    name = (cronjob.metadata.name or "") if cronjob.metadata else ""
    try:
        batch_api.create_namespaced_cron_job(namespace=namespace, body=cronjob)
        logger.info("Created cronjob %s", name)
    except ApiException as e:
        if e.status != 409:
            raise
        batch_api.replace_namespaced_cron_job(name=name, namespace=namespace, body=cronjob)
        logger.info("Updated cronjob %s", name)


def delete_cronjob(batch_api: BatchV1Api, name: str, namespace: str) -> None:
    """Delete a CronJob by name and namespace."""
    try:
        batch_api.delete_namespaced_cron_job(name=name, namespace=namespace)
        logger.info("Deleted cronjob %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


def create_or_update_restore_job(batch_api: BatchV1Api, job: V1Job) -> None:
    """Create a restore Job, leaving it alone if it already exists."""
    namespace = (job.metadata.namespace or "") if job.metadata else ""
    name = (job.metadata.name or "") if job.metadata else ""
    try:
        batch_api.create_namespaced_job(namespace=namespace, body=job)
        logger.info("Created restore job %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("Restore job %s already exists in %s", name, namespace)


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


# Backup custom resources
def create_or_update_backup(custom_api: CustomObjectsApi, backup: dict[str, Any]) -> None:
    """Create or update a Backup custom resource."""
    name = backup.get("metadata", {}).get("name", "")
    namespace = backup.get("metadata", {}).get("namespace", "")
    try:
        custom_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL_BACKUPS,
            namespace=namespace,
            body=backup,
        )
        logger.info("Created backup %s", name)
    except ApiException as e:
        if e.status != 409:
            raise
        custom_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL_BACKUPS,
            name=name,
            namespace=namespace,
            body=backup,
        )
        logger.info("Updated backup %s", name)


def patch_backup(
    custom_api: CustomObjectsApi,
    name: str,
    namespace: str,
    body: dict[str, Any],
) -> None:
    """Patch a Backup custom resource (spec and/or labels)."""
    custom_api.patch_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        plural=CRD_PLURAL_BACKUPS,
        name=name,
        namespace=namespace,
        body=body,
    )
    logger.info("Patched backup %s in %s", name, namespace)


def delete_backup(custom_api: CustomObjectsApi, name: str, namespace: str) -> None:
    """Delete a Backup custom resource."""
    try:
        custom_api.delete_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL_BACKUPS,
            name=name,
            namespace=namespace,
        )
        logger.info("Deleted backup %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


def list_backups_for_schedule(
    custom_api: CustomObjectsApi,
    schedule_name: str,
) -> list[dict[str, Any]]:
    """List the Backup custom resources that belong to a schedule."""
    return list_cluster_custom(custom_api, CRD_PLURAL_BACKUPS, f"{LABEL_SCHEDULE}={schedule_name}")


def find_backup(
    custom_api: CustomObjectsApi,
    backup_ref: str,
    preferred_namespace: str = "",
) -> dict[str, Any] | None:
    """Find a Backup CR by name, trying a preferred namespace first, else cluster-wide."""
    if preferred_namespace:
        try:
            item: Any = custom_api.get_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=preferred_namespace,
                plural=CRD_PLURAL_BACKUPS,
                name=backup_ref,
            )
            if isinstance(item, dict):
                return item
        except ApiException as e:
            if e.status != 404:
                logger.warning(
                    "Error looking up Backup %s in %s: %s",
                    backup_ref,
                    preferred_namespace,
                    e,
                )

    try:
        result: Any = custom_api.list_cluster_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            plural=CRD_PLURAL_BACKUPS,
        )
        if isinstance(result, dict):
            for item in result.get("items", []):
                if isinstance(item, dict) and item.get("metadata", {}).get("name") == backup_ref:
                    return item
    except ApiException:
        logger.exception("Failed to search for Backup %s", backup_ref)
    return None


# Validation
def secret_exists(namespace: str, name: str) -> bool:
    """Return True if a Secret exists in the namespace.

    A missing Secret is an actionable misconfiguration, so it reports ``False``.
    Any other API error is treated as "present" so a transient outage does not
    block backups.
    """
    if not name:
        return False
    try:
        get_core_api().read_namespaced_secret(name=name, namespace=namespace)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        logger.warning("Failed to check secret %s in %s: %s", name, namespace, e)
        return True
    except (OpenApiException, ValueError, OSError) as e:
        logger.warning("Failed to check secret %s in %s: %s", name, namespace, e)
        return True


# Discovery
def list_pvcs_by_selector(api: client.CoreV1Api, selector: dict[str, str]) -> list[tuple[str, str]]:
    """List PVC (name, namespace) tuples matching a label selector."""
    compiled_selectors = ",".join(f"{k}={v}" for k, v in selector.items())
    try:
        pvcs = api.list_persistent_volume_claim_for_all_namespaces(
            label_selector=compiled_selectors,
        )
        return [
            (p.metadata.name, p.metadata.namespace)
            for p in pvcs.items
            if p.metadata and p.metadata.name and p.metadata.namespace
        ]
    except ApiException:
        logger.exception("Failed to list PVCs with selector %s", selector)
        return []


def list_backup_schedules(api: CustomObjectsApi) -> list[dict[str, Any]]:
    """List all BackupSchedule custom resources across the cluster."""
    return list_cluster_custom(api, CRD_PLURAL_BACKUPSCHEDULES)
