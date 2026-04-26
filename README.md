# GitOps Repo

Helm charts → GHCR (OCI) → ArgoCD App-of-Apps → MicroK8s

## Structure

```
.
├── charts/                        # Helm chart sources
│   └── example-service/
├── envs/                          # Per-environment value overrides
│   ├── local/example-service/values.yaml
│   └── staging/example-service/values.yaml
├── argocd/
│   ├── local/
│   │   ├── root-app.yaml          # Bootstrap once with kubectl apply
│   │   └── apps/                  # One Application CR per service
│   └── staging/
│       ├── root-app.yaml
│       └── apps/
├── .github/workflows/
│   └── publish-charts.yaml        # Auto-publish changed charts to GHCR
├── services.local.yaml            # Which services to deploy locally
├── services.staging.yaml
└── deploy.sh
```

## Adding a new service

1. **Chart:**
   ```bash
   cp -r charts/example-service charts/my-svc
   # Update charts/my-svc/Chart.yaml  (name + version)
   # Update charts/my-svc/values.yaml
   ```

2. **Env values:**
   ```bash
   mkdir -p envs/local/my-svc
   cp envs/local/example-service/values.yaml envs/local/my-svc/values.yaml
   # Edit the copy for your service's local config
   ```

3. **ArgoCD Application CR:**
   ```bash
   cp argocd/local/apps/example-service.yaml argocd/local/apps/my-svc.yaml
   # Edit: metadata.name, spec.source.chart, spec.destination.namespace
   ```

4. **Enable in config:**
   ```yaml
   # services.local.yaml
   - name: my-svc
     enabled: true
   ```

5. **Deploy:**
   ```bash
   ./deploy.sh --env local
   ```

6. **Push** — GitHub Actions publishes the chart to GHCR automatically on merge to main.

## First-time MicroK8s setup

```bash
microk8s enable dns storage

# Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=120s

# Bootstrap root app (once)
kubectl apply -f argocd/local/root-app.yaml
```

## GHCR secret for ArgoCD (private repos)

```bash
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password=YOUR_PAT \
  -n argocd
```

## Deploy workflow

```bash
vim services.local.yaml       # toggle services on/off
./deploy.sh --env local       # apply
./deploy.sh --env local --dry-run  # preview without applying
```