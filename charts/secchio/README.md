# secchio

K8s PVC backup & restore operator.

secchio backs up PersistentVolumeClaims on a cron schedule into S3-compatible storage using [restic](https://restic.net/), and restores them on demand. It is
built on [kopf](https://kopf.readthedocs.io/) for talking to Kubernetes, and ships
a small web UI from which you can view and trigger backups and restores.

## Overview

Three custom resources (`secchio.cocointhe.cloud/v1alpha1`) describe backup/restore
intent; the operator turns them into native Kubernetes workloads:

- **BackupSchedule**: matches PVCs by label and manages one `Backup` per match.
- **Backup**: owns a CronJob in the PVC's namespace.
- **Restore**: owns a one-off Job that restores a snapshot into a PVC.

The CronJob/Job pods are the only things that touch restic and the S3 repository.
The operator only orchestrates native objects, so existing backups keep running even
if the operator is down.

## Prerequisites

- A Kubernetes cluster with `kubectl` configured and a default StorageClass.
- Helm 3.8+ (OCI registry support).
- An S3 bucket and RW credentials.

## Installing the chart

The chart is published as an OCI artifact on GHCR:

```sh
helm install secchio-operator oci://ghcr.io/k0rventen/secchio \
  --create-namespace --namespace secchio-system \
  --version 0.8.1
```

The CRDs are bundled in `crds/` and are installed automatically on the first install.

### Accessing the UI

The operator exposes a web UI on port 8000 (Service `secchio-ui`). Reach it with a
port-forward:

```sh
kubectl port-forward -n secchio-system svc/secchio-ui 8000:8000
```

then open <http://localhost:8000>.

> The chart intentionally does not expose the UI through an Ingress, because of the
> nature of the actions available (triggering backups and restores). Adding
> authentication is out of scope; use a port-forward or a solution like
> [warpgate](https://github.com/warp-tech/warpgate) to secure access.

## Uninstalling

```sh
helm uninstall secchio-operator -n secchio-system
```

> CRDs are not removed by `helm uninstall`. Delete them manually if you want to
> remove them too:
>
> ```sh
> kubectl delete crd \
>   backupschedules.secchio.cocointhe.cloud \
>   backups.secchio.cocointhe.cloud \
>   restores.secchio.cocointhe.cloud
> ```

## Configuration

| Key | Description | Default |
| --- | --- | --- |
| `operator.image` | Operator container image repository | `ghcr.io/k0rventen/secchio-operator` |
| `operator.tag` | Operator container image tag | `v0.7.2` |
| `restic.image` | restic workload image repository | `ghcr.io/k0rventen/secchio-restic` |
| `restic.tag` | restic workload image tag | `v0.7.2` |

## Usage

Create a credentials Secret and a BackupSchedule:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: restic-s3-secret
  namespace: production
type: Opaque
stringData:
  RESTIC_PASSWORD: "changeme-restic-password"
  AWS_ACCESS_KEY_ID: "changeme-access-key-id"
  AWS_SECRET_ACCESS_KEY: "changeme-secret-access-key"
---
apiVersion: secchio.cocointhe.cloud/v1alpha1
kind: BackupSchedule
metadata:
  name: python-notes
spec:
  pvc:
    selector:
      app: python-notes-app
  schedule: "*/10 * * * *"
  repo:
    endpoint: 192.168.1.20/backups
    secretName: restic-s3-secret
    insecureTLS: true
  retention:
    keepDaily: 7
    keepWeekly: 4
    keepMonthly: 3
```

# Caveat
The credentials Secret must exist in **each namespace** where backups run, because
the backup pods are created in the PVC's namespace. If it is missing, the operator
reports the `Backup` as `Failed` with `reason: SecretNotFound` and does not create
the CronJob.

## Links

- Source: <https://github.com/k0rventen/secchio>
- restic: <https://restic.net/>
- kopf: <https://kopf.readthedocs.io/>
