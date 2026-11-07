# vcluster test cluster
allow_k8s_contexts('vcluster_vcluster_vcluster_mora')

# set the images to the local registry and latest restic image for automatic always pullpolicy
k8s_yaml(local('helm template --include-crds charts/secchio/ -n secchio-system --set operator.image=gitea.cocointhe.cloud/coco/secchio-operator --set restic.image=gitea.cocointhe.cloud/coco/secchio-restic --set=restic.tag=latest'))

# build the operator through buildx
custom_build(
  'gitea.cocointhe.cloud/coco/secchio-operator',
  'docker buildx build -t $EXPECTED_REF --push .',
  ['./src','Dockerfile'],
  skips_local_docker=True,
  disable_push=True,
)

# build the restic image
local('docker buildx build --push -t gitea.cocointhe.cloud/coco/secchio-restic:latest restic-pod/')

# enable port forwarding
local_resource(
    'ui-port-forward',
    serve_cmd='kubectl port-forward -n secchio-system svc/secchio-ui 8002:8000'
)
