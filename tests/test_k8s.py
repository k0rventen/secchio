"""Unit tests for the thin Kubernetes helpers."""

from __future__ import annotations

from unittest import mock

from kubernetes.client.exceptions import ApiException

import k8s
from crds import BackupStatus


def test_patch_status_falls_back_without_status_subresource() -> None:
    api = mock.Mock()
    api.patch_namespaced_custom_object_status.side_effect = ApiException(status=404)

    k8s.patch_status(api, plural="backups", name="bk", namespace="ns", status=BackupStatus())

    api.patch_namespaced_custom_object_status.assert_called_once()
    api.patch_namespaced_custom_object.assert_called_once()


def test_secret_exists_distinguishes_missing_from_api_errors() -> None:
    core = mock.Mock()
    with mock.patch.object(k8s, "get_core_api", return_value=core):
        core.read_namespaced_secret.side_effect = ApiException(status=404)
        assert k8s.secret_exists("ns", "creds") is False

        core.read_namespaced_secret.side_effect = ApiException(status=500)
        assert k8s.secret_exists("ns", "creds") is True

        core.read_namespaced_secret.side_effect = None
        assert k8s.secret_exists("ns", "creds") is True

    assert k8s.secret_exists("ns", "") is False


def test_owner_reference_requires_name_and_uid() -> None:
    assert k8s.owner_reference("g/v1", "Backup", "", "uid") == []
    assert k8s.owner_reference("g/v1", "Backup", "name", "") == []

    ref = k8s.owner_reference("g/v1", "Backup", "name", "uid")[0]
    assert ref.controller is True
    assert ref.block_owner_deletion is True
