"""variables/constants and utils called from everywhere else"""

from __future__ import annotations

import os
from datetime import UTC, datetime

# Operator configuration (set from the Helm chart as env vars)
RESTIC_IMAGE = os.environ.get("RESTIC_IMAGE", "ghcr.io/k0rventen/secchio-restic:latest")
OPERATOR_VERSION = os.environ.get("OPERATOR_VERSION", "dev")

# CRD Schema Constants
CRD_GROUP = "secchio.cocointhe.cloud"
CRD_VERSION = "v1alpha1"
CRD_PLURAL_BACKUPSCHEDULES = "backupschedules"
CRD_PLURAL_RESTORES = "restores"

# Domain Label Keys
LABEL_APP = "app.kubernetes.io/name"
LABEL_APP_VALUE = "secchio"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_MANAGED_BY_VALUE = "secchio-operator"
LABEL_SCHEDULE = "secchio.cocointhe.cloud/schedule"
LABEL_RESTORE = "secchio.cocointhe.cloud/restore"
LABEL_PVC_NAME = "secchio.cocointhe.cloud/pvc-name"

# Status reasons
REASON_CRONJOB_REJECTED = "CronJobRejected"

# How long a finished backup/restore Job (and its pod) is kept before Kubernetes
# garbage-collects it. Long enough for the operator to read the pod termination log
# and update the CR status.
JOB_TTL_SECONDS = 900  # 15m


# Naming helpers
def make_cronjob_name(schedule_name: str, pvc_name: str) -> str:
    """Generate the CronJob name for a schedule/PVC pair."""
    return f"{schedule_name}-backup-{pvc_name}-cronjob"


def make_restore_job_name(restore_name: str) -> str:
    """Generate a standard Job name for a Restore resource."""
    return f"{restore_name}-job"


# Label helpers
def _base_labels() -> dict[str, str]:
    """Labels common to every object the operator creates."""
    return {
        "app": "secchio",
        LABEL_APP: LABEL_APP_VALUE,
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
    }


def make_cronjob_labels(schedule_name: str, pvc_name: str) -> dict[str, str]:
    """Generate standard labels for a backup CronJob."""
    return {
        **_base_labels(),
        LABEL_SCHEDULE: schedule_name,
        LABEL_PVC_NAME: pvc_name,
    }


def make_restore_labels(restore_name: str, schedule_name: str, pvc_name: str) -> dict[str, str]:
    """Generate standard labels for a restore Job."""
    return {
        **_base_labels(),
        LABEL_RESTORE: restore_name,
        LABEL_SCHEDULE: schedule_name,
        LABEL_PVC_NAME: pvc_name,
    }


# Label extractors
def extract_pvc_name(labels: dict[str, str]) -> str:
    """Extract PVC name from standard labels."""
    return labels.get(LABEL_PVC_NAME, "")


def extract_schedule_name(labels: dict[str, str]) -> str:
    """Extract schedule name from standard labels."""
    return labels.get(LABEL_SCHEDULE, "")


def extract_restore_name(labels: dict[str, str]) -> str:
    """Extract restore name from standard labels."""
    return labels.get(LABEL_RESTORE, "")


def now_iso() -> str:
    """Current time as an ISO-8601 string, used for status timestamps."""
    return datetime.now(UTC).isoformat()
