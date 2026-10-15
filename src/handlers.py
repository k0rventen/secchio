"""operator entrypoint + kopf decorated functions + startup + fastapi"""

from __future__ import annotations

import logging
import threading
from typing import Any

import kopf
import uvicorn

import reconcile
import status as status_mod
from api import app
from common import CRD_GROUP, CRD_VERSION

logger = logging.getLogger("handlers")


def _serve_ui() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


@kopf.on.startup()
def start_up(settings: kopf.OperatorSettings, **kwargs: Any) -> None:
    settings.posting.level = logging.WARNING
    logging.getLogger("kopf.objects").setLevel(logging.WARNING)
    logger.info("kopf is starting")
    threading.Thread(target=_serve_ui, daemon=True).start()


# BackupSchedule
@kopf.on.create(CRD_GROUP, CRD_VERSION, "backupschedule")
@kopf.on.update(CRD_GROUP, CRD_VERSION, "backupschedule")
def on_schedule(*, name: str, spec: kopf.Spec, **kwargs: Any) -> None:
    reconcile.schedule(name=name, spec=dict(spec))


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "backupschedule")
def on_schedule_delete(*, name: str, **kwargs: Any) -> None:
    reconcile.delete_schedule(name=name)


@kopf.timer(CRD_GROUP, CRD_VERSION, "backupschedule", interval=60)
def check_schedules(*, name: str, spec: kopf.Spec, **kwargs: Any) -> None:
    """Periodically re-reconcile so newly created PVCs are picked up."""
    reconcile.schedule(name=name, spec=dict(spec))


# Backup
@kopf.on.create(CRD_GROUP, CRD_VERSION, "backup")
@kopf.on.update(CRD_GROUP, CRD_VERSION, "backup")
def on_backup(
    *,
    name: str,
    spec: kopf.Spec,
    meta: kopf.Meta,
    status: kopf.Status | None = None,
    namespace: str | None = None,
    **kwargs: Any,
) -> None:
    reconcile.backup(
        name=name,
        namespace=namespace or "",
        spec=dict(spec),
        meta=dict(meta),
        current_status=dict(status or {}),
    )


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "backup")
def on_backup_delete(
    *,
    name: str,
    meta: kopf.Meta,
    namespace: str | None = None,
    **kwargs: Any,
) -> None:
    reconcile.delete_backup(name=name, namespace=namespace or "", meta=dict(meta))


@kopf.timer(CRD_GROUP, CRD_VERSION, "backup", interval=60)
def check_backups(
    *,
    name: str,
    spec: kopf.Spec,
    meta: kopf.Meta,
    status: kopf.Status | None = None,
    namespace: str | None = None,
    **kwargs: Any,
) -> None:
    """Periodically ensure the CronJob exists and sync status from the latest Job."""
    ns = namespace or ""
    ensured = reconcile.backup(
        name=name,
        namespace=ns,
        spec=dict(spec),
        meta=dict(meta),
        current_status=dict(status or {}),
    )
    # Don't let a stale job status clobber a missing-Secret failure.
    if ensured:
        status_mod.backup_status(name=name, namespace=ns)


# Restore
@kopf.on.create(CRD_GROUP, CRD_VERSION, "restore")
@kopf.on.update(CRD_GROUP, CRD_VERSION, "restore")
def on_restore(
    *,
    name: str,
    spec: kopf.Spec,
    meta: kopf.Meta,
    namespace: str | None = None,
    **kwargs: Any,
) -> None:
    reconcile.restore(name=name, namespace=namespace or "", spec=dict(spec), meta=dict(meta))


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "restore")
def on_restore_delete(*, name: str, namespace: str | None = None, **kwargs: Any) -> None:
    reconcile.delete_restore(name=name, namespace=namespace or "")


# Jobs
@kopf.on.event("batch", "v1", "jobs", labels={"app": "secchio"})
def on_job(*, body: kopf.Body, **kwargs: Any) -> None:
    """Sync Backup/Restore status whenever a restic Job changes."""
    status_mod.job_status(status_mod.JobStatus.from_kopf_body(body))
