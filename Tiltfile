allow_k8s_contexts('k3s')

k8s_yaml(local('helm template --include-crds charts/secchio/ -n secchio-system --set operator.image=gitea.cocointhe.cloud/coco/secchio-operator --set restic.image=gitea.cocointhe.cloud/coco/secchio-restic --set=restic.tag=latest'))
docker_build("gitea.cocointhe.cloud/coco/secchio-operator",".")

local('docker build --push -t gitea.cocointhe.cloud/coco/secchio-restic:latest restic-pod/')

local_resource(
    'kube-port-forward',
    serve_cmd='kubectl port-forward -n secchio-system svc/secchio-ui 8002:8000'
)
