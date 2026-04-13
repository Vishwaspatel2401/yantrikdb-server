# Kubernetes Deployment

## Single Node

```bash
kubectl apply -f yantrikdb-single.yaml
kubectl port-forward svc/yantrikdb 7438:7438
curl http://localhost:7438/v1/health
```

## HA Cluster (2 voters + 1 witness)

```bash
# 1. Update the cluster secret
kubectl create secret generic yantrikdb-cluster-secret \
  --from-literal=cluster-secret="$(openssl rand -hex 32)"

# 2. Deploy
kubectl apply -f yantrikdb-cluster.yaml

# 3. Wait for pods
kubectl rollout status statefulset/yantrikdb

# 4. Create database + token
kubectl exec yantrikdb-0 -- yantrikdb db --data-dir /data create mydb
TOKEN=$(kubectl exec yantrikdb-0 -- yantrikdb token --data-dir /data create --db mydb --label k8s)
echo "Token: $TOKEN"

# 5. Port-forward and test
kubectl port-forward svc/yantrikdb 7438:7438
curl -H "Authorization: Bearer $TOKEN" http://localhost:7438/v1/stats
```

## Features

- **Readiness probe**: `GET /v1/health/deep` — checks engine lock, control DB, cluster quorum
- **Liveness probe**: `GET /v1/health` — shallow process-alive check
- **Prometheus metrics**: scrape `/metrics` on port 7438
- **Persistent storage**: each voter gets its own PVC via StatefulSet
- **Witness**: runs as a tiny Deployment (~32MB RAM)

## Scaling

The cluster supports exactly 2 voters. Do NOT scale the StatefulSet beyond 2 —
the peer list in the ConfigMap is static. For read replicas, add a separate
Deployment with `role = "read_replica"`.
