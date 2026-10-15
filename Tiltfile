allow_k8s_contexts('k3s')
k8s_yaml(local('helm template --include-crds charts/secchio/ -n secchio-system -f charts/secchio/values-tilt.yaml'))
docker_build("gitea.cocointhe.cloud/coco/secchio",".")
