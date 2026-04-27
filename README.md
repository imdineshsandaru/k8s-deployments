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

# Get argocd ui pw
kubectl get secret argocd-initial-admin-secret \
  -n argocd \
  -o jsonpath="{.data.password}" | base64 -d && echo
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

---

## CDC Test Flow (Postgres → Kafka → Postgres)

**Pipeline:** `postgresql-source` → Debezium → Kafka → JDBC Sink → `postgresql-sink`

### 0. Verify all connectors are healthy

```bash
kubectl get kafkaconnector -n kafka
# NAME        CLUSTER            CONNECTOR CLASS                                      MAX TASKS   READY
# pg-sink     debezium-connect   io.debezium.connector.jdbc.JdbcSinkConnector         1           True
# pg-source   debezium-connect   io.debezium.connector.postgresql.PostgresConnector   1           True
```

---

### 1. Insert a row in Source PostgreSQL

```bash
kubectl exec -it -n postgresql-source \
  $(kubectl get pod -n postgresql-source -l app.kubernetes.io/name=postgresql \
    -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U replicator -d sourcedb
```

```sql
-- Create the table if it doesn't already exist
CREATE TABLE IF NOT EXISTS public.users (
    id   SERIAL PRIMARY KEY,
    name TEXT
);

-- Insert a test row
INSERT INTO public.users (name) VALUES ('hello-cdc');
```

Debezium picks up the change and publishes it to the Kafka topic `cdc.public.users`.

---

### 2. Verify the event arrived in Kafka

```bash
kubectl exec -it -n kafka \
  $(kubectl get pod -n kafka -l strimzi.io/name=cdc-kafka-kafka \
    -o jsonpath='{.items[0].metadata.name}') \
  -- bin/kafka-console-consumer.sh \
     --bootstrap-server localhost:9092 \
     --topic cdc.public.users \
     --from-beginning \
     --max-messages 5
```

Expect a JSON payload with `before`, `after`, and `source` fields (Debezium change envelope).

---

### 3. Check the row landed in Sink PostgreSQL

```bash
kubectl exec -it -n postgresql-sink \
  $(kubectl get pod -n postgresql-sink -l app.kubernetes.io/name=postgresql \
    -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U sinkuser -d sinkdb
```

```sql
-- The JDBC sink auto-creates the table.
-- Table name = Kafka topic name with dots replaced by underscores.
SELECT * FROM cdc_public_users;
-- Expected: id=1  name='hello-cdc'
```

---

### 4. Test UPDATE propagation

```sql
-- In SOURCE psql (sourcedb)
UPDATE public.users SET name = 'updated-cdc' WHERE id = 1;
```

```sql
-- In SINK psql (sinkdb) — check ~5 seconds later
SELECT * FROM cdc_public_users;
-- Expected: id=1  name='updated-cdc'   (insert.mode=upsert)
```

---

### 5. Test DELETE behaviour

```sql
-- In SOURCE psql (sourcedb)
DELETE FROM public.users WHERE id = 1;
```

```sql
-- In SINK psql (sinkdb)
SELECT * FROM cdc_public_users;
-- Row stays — sink is configured with delete.handling.mode=drop
```

---

### Sink table naming

The JDBC sink connector derives the table name from the Kafka topic:

| Source table     | Kafka topic          | Sink table          |
|------------------|----------------------|---------------------|
| `public.users`   | `cdc.public.users`   | `cdc_public_users`  |

---

### Troubleshooting

| Symptom | Command |
|---|---|
| Connector not READY | `kubectl describe kafkaconnector pg-sink -n kafka` |
| Inspect Connect worker logs | `kubectl logs -n kafka -l strimzi.io/name=debezium-connect -f` |
| List all Kafka topics | `kubectl exec -n kafka <broker-pod> -- bin/kafka-topics.sh --bootstrap-server localhost:9092 --list` |
| Restart a failed connector task | `kubectl annotate kafkaconnector pg-sink -n kafka strimzi.io/restart-task=0 --overwrite` |
| Restart the full connector | `kubectl annotate kafkaconnector pg-sink -n kafka strimzi.io/restart=true --overwrite` |
| WAL level must be `logical` | `psql -U replicator -d sourcedb -c "SHOW wal_level;"` |