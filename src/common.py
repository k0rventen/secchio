"""variables/constants and utils called from everywhere else"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime


def _parse_restic_resources(raw: str) -> dict[str, dict[str, str]]:
    """Parse the chart's ``restic.resources`` JSON, coercing quantities to strings.

    Helm's ``toJson`` renders unquoted YAML numbers (``cpu: 1``) as JSON numbers,
    while the Kubernetes client requires quantity strings.
    """
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {}
    return {
        section: {name: str(value) for name, value in quantities.items()}
        for section, quantities in parsed.items()
        if isinstance(quantities, dict)
    }


# Operator configuration (set from the Helm chart as env vars)
RESTIC_IMAGE = os.environ.get("RESTIC_IMAGE", "ghcr.io/k0rventen/secchio-restic:latest")
OPERATOR_VERSION = os.environ.get("OPERATOR_VERSION", "dev")
# Requests/limits for every restic pod (backup, restore, scan). The chart renders
# `restic.resources` into this JSON env var; an empty object means "unset".
RESTIC_RESOURCES = _parse_restic_resources(os.environ.get("RESTIC_RESOURCES", "{}"))
# Default window (seconds) over which scheduled backups are spread. A schedule's
# `spec.spreadSeconds` overrides it; 0 disables the spread.
RESTIC_DEFAULT_SPREAD_SECONDS = int(os.environ.get("RESTIC_DEFAULT_SPREAD_SECONDS", "0"))

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
LABEL_SCAN = "secchio.cocointhe.cloud/scan"
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


def make_scan_labels(schedule_name: str, pvc_name: str) -> dict[str, str]:
    """Generate standard labels for a snapshot-scan Job."""
    return {
        **_base_labels(),
        LABEL_SCHEDULE: schedule_name,
        LABEL_PVC_NAME: pvc_name,
        LABEL_SCAN: "true",
    }


# Scheduling helpers
def spread_delay_seconds(schedule_name: str, pvc_name: str, spread_seconds: int) -> int:
    """A stable per-PVC start delay in ``[0, spread_seconds)``.

    Hashing the schedule/PVC pair keeps the offset stable across reconciles and
    retries while spreading different PVCs across the window.
    """
    if spread_seconds <= 0:
        return 0
    digest = hashlib.sha256(f"{schedule_name}/{pvc_name}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % spread_seconds


def now_iso() -> str:
    """Current time as an ISO-8601 string, used for status timestamps."""
    return datetime.now(UTC).isoformat()
