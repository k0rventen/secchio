"""variables/constants and utils called from everywhere else"""

from __future__ import annotations

import os
from datetime import UTC, datetime

# Operator configuration (set from the Helm chart as env vars)
RESTIC_IMAGE = os.environ.get("RESTIC_IMAGE", "restic/restic:latest")

# CRD Schema Constants
CRD_GROUP = "secchio.cocointhe.cloud"
CRD_VERSION = "v1alpha1"
CRD_PLURAL_BACKUPSCHEDULES = "backupschedules"
CRD_PLURAL_BACKUPS = "backups"
CRD_PLURAL_RESTORES = "restores"

# Domain Label Keys
LABEL_APP = "app.kubernetes.io/name"
LABEL_APP_VALUE = "secchio"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_MANAGED_BY_VALUE = "secchio-operator"
LABEL_SCHEDULE = "secchio.cocointhe.cloud/schedule"
LABEL_BACKUP = "secchio.cocointhe.cloud/backup"
LABEL_RESTORE = "secchio.cocointhe.cloud/restore"
LABEL_PVC_NAME = "secchio.cocointhe.cloud/pvc-name"
LABEL_PVC_NAMESPACE = "secchio.cocointhe.cloud/pvc-namespace"
LABEL_COMPONENT = "app.kubernetes.io/component"

# Status reasons
REASON_SECRET_MISSING = "SecretNotFound"


# Naming helpers
def make_backup_name(schedule_name: str, pvc_name: str) -> str:
    """Generate a standard Backup custom resource name."""
    return f"{schedule_name}-backup-{pvc_name}"


def make_cronjob_name(backup_name: str) -> str:
    """Generate a standard CronJob name for a Backup resource."""
    return f"{backup_name}-cronjob"


def make_restore_job_name(restore_name: str) -> str:
    """Generate a standard Job name for a Restore resource."""
    return f"{restore_name}-job"


# Label helpers
def make_backup_labels(
    schedule_name: str,
    pvc_name: str,
    pvc_namespace: str = "",
) -> dict[str, str]:
    """Generate standard labels for a Backup custom resource."""
    labels = {
        "app": "secchio",
        LABEL_APP: LABEL_APP_VALUE,
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
        LABEL_SCHEDULE: schedule_name,
        LABEL_PVC_NAME: pvc_name,
    }
    if pvc_namespace:
        labels[LABEL_PVC_NAMESPACE] = pvc_namespace
    return labels


def make_cronjob_labels(schedule_name: str, backup_name: str, pvc_name: str) -> dict[str, str]:
    """Generate standard labels for a backup CronJob."""
    return {
        "app": "secchio",
        LABEL_APP: LABEL_APP_VALUE,
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
        LABEL_SCHEDULE: schedule_name,
        LABEL_BACKUP: backup_name,
        LABEL_PVC_NAME: pvc_name,
    }


def make_restore_labels(restore_name: str, backup_name: str, target_pvc: str) -> dict[str, str]:
    """Generate standard labels for a restore Job."""
    return {
        "app": "secchio",
        LABEL_APP: LABEL_APP_VALUE,
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
        LABEL_COMPONENT: "restore",
        LABEL_RESTORE: restore_name,
        LABEL_BACKUP: backup_name,
        LABEL_PVC_NAME: target_pvc,
    }


# Label extractors
def extract_pvc_name(labels: dict[str, str]) -> str:
    """Extract PVC name from standard labels."""
    return labels.get(LABEL_PVC_NAME, "")


def extract_schedule_name(labels: dict[str, str]) -> str:
    """Extract schedule name from standard labels."""
    return labels.get(LABEL_SCHEDULE, "")


def extract_backup_name(labels: dict[str, str]) -> str:
    """Extract backup name from standard labels."""
    return labels.get(LABEL_BACKUP, "")


def extract_restore_name(labels: dict[str, str]) -> str:
    """Extract restore name from standard labels."""
    return labels.get(LABEL_RESTORE, "")


def now_iso() -> str:
    """Current time as an ISO-8601 string, used for status timestamps."""
    return datetime.now(UTC).isoformat()
