"""Typed models that match the CRDs (see charts/secchio/crds).

Python fields are snake_case and the CRD format is camelCase
so `alias_generator` does the translation
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Model(BaseModel):
    """Base model: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


def aliased(wire_name: str, **field_kwargs: Any) -> Any:
    """A field whose CRD wire name is not the camelCase of its Python name.

    Sets both the validation and serialization aliases (rather than a single
    ``alias``) so type checkers keep using the Python name for ``__init__`` —
    pyright otherwise rejects ``Model(some_field=...)`` despite
    ``populate_by_name``.
    """
    return Field(validation_alias=wire_name, serialization_alias=wire_name, **field_kwargs)


class BackupPhase(StrEnum):
    """Lifecycle phase of a single PVC Backup."""

    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


class RestorePhase(StrEnum):
    """Lifecycle phase of a Restore operation."""

    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


class SchedulePhase(StrEnum):
    """Aggregated health phase of a BackupSchedule."""

    PENDING = "Pending"
    RUNNING = "Running"
    HEALTHY = "Healthy"
    DEGRADED = "Degraded"


class PVCSelector(Model):
    """Label selector used to match PVCs."""

    selector: dict[str, str] = Field(default_factory=dict)


class RetentionConfig(Model):
    """GFS retention policy, rendered as restic ``forget`` args."""

    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None

    def to_restic_args(self) -> list[str]:
        args: list[str] = []
        for keep, flag in (
            (self.keep_daily, "--keep-daily"),
            (self.keep_weekly, "--keep-weekly"),
            (self.keep_monthly, "--keep-monthly"),
            (self.keep_yearly, "--keep-yearly"),
        ):
            if keep:
                args += [flag, str(keep)]
        return args


class S3BackendConfig(Model):
    """S3 repository backend (the only supported backend)."""

    endpoint: str = ""
    secret_name: str = ""
    insecure_tls: bool = aliased("insecureTLS", default=False)

    def to_restic_repo(self) -> str:
        return f"s3:{self.endpoint}"


# --- BackupSchedule (cluster-scoped) --------------------------------------


class BackupScheduleSpec(Model):
    pvc: PVCSelector = Field(default_factory=PVCSelector)
    schedule: str = ""
    repo: S3BackendConfig = Field(default_factory=S3BackendConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class BackupScheduleSummary(Model):
    """Counts of backups under a BackupSchedule."""

    total_backups: int = 0
    healthy_backups: int = 0
    failed_backups: int = 0
    running_backups: int = 0


class BackupScheduleStatus(Model):
    phase: SchedulePhase | str = SchedulePhase.PENDING
    all_ok: bool = False
    last_backup_time: str = ""
    message: str = ""
    summary: BackupScheduleSummary = Field(default_factory=BackupScheduleSummary)


# --- Backup (namespaced, one per PVC) -------------------------------------


class BackupSpec(Model):
    pvc_name: str = ""
    pvc_namespace: str = ""
    repo: S3BackendConfig = Field(default_factory=S3BackendConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    schedule: str = ""
    secret_name: str = ""


class SnapshotInfo(Model):
    """A restic snapshot as surfaced in status and the UI."""

    id: str = ""
    time: str = ""


class BackupStatus(Model):
    phase: BackupPhase | str = BackupPhase.PENDING
    last_backup_time: str = ""
    last_snapshot_id: str = aliased("lastSnapshotID", default="")
    snapshots: list[SnapshotInfo] = Field(default_factory=list)
    message: str = ""
    reason: str = ""


class Backup(Model):
    name: str = ""
    namespace: str = ""
    uid: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    spec: BackupSpec = Field(default_factory=BackupSpec)
    status: BackupStatus = Field(default_factory=BackupStatus)

    @classmethod
    def from_crd(cls, item: dict[str, Any]) -> Backup:
        meta = item.get("metadata") or {}
        return cls.model_validate(
            {
                "name": meta.get("name", ""),
                "namespace": meta.get("namespace", ""),
                "uid": meta.get("uid", ""),
                "labels": meta.get("labels") or {},
                "spec": item.get("spec") or {},
                "status": item.get("status") or {},
            },
        )

    def to_crd_body(self, group: str, version: str) -> dict[str, Any]:
        return {
            "apiVersion": f"{group}/{version}",
            "kind": "Backup",
            "metadata": {"name": self.name, "namespace": self.namespace, "labels": self.labels},
            "spec": self.spec.model_dump(by_alias=True),
        }


# --- Restore (namespaced) -------------------------------------------------


class RestoreSpec(Model):
    target_pvc: str = aliased("targetPVC", default="")
    target_pvc_namespace: str = aliased("targetPVCNamespace", default="")
    backup_ref: str = ""
    source_pvc: str = aliased("sourcePVC", default="")
    source_pvc_namespace: str = aliased("sourcePVCNamespace", default="")
    snapshot_id: str = aliased("snapshotID", default="latest")


class RestoreStatus(Model):
    phase: RestorePhase | str = RestorePhase.PENDING
    start_time: str = ""
    completion_time: str = ""
    snapshot_id: str = aliased("snapshotID", default="")
    message: str = ""


class Restore(Model):
    name: str = ""
    namespace: str = ""
    uid: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    spec: RestoreSpec = Field(default_factory=RestoreSpec)
    status: RestoreStatus = Field(default_factory=RestoreStatus)

    @classmethod
    def from_crd(cls, item: dict[str, Any]) -> Restore:
        meta = item.get("metadata") or {}
        return cls.model_validate(
            {
                "name": meta.get("name", ""),
                "namespace": meta.get("namespace", ""),
                "uid": meta.get("uid", ""),
                "labels": meta.get("labels") or {},
                "spec": item.get("spec") or {},
                "status": item.get("status") or {},
            },
        )
