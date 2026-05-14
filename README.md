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
# Ensure system is updated
sudo apt-get update && sudo apt-get install -y git curl

# Install yq (used by deploy.sh)
sudo snap install yq

# Install MicroK8s
sudo snap install microk8s --classic

# Grant your user permissions to run microk8s commands without sudo
sudo usermod -a -G microk8s $USER
sudo chown -f -R $USER ~/.kube
newgrp microk8s # apply group changes to current shell

# Alias the native microk8s.kubectl to standard 'kubectl' for ease of use
sudo snap alias microk8s.kubectl kubectl

# Enable required MicroK8s addons (Check dns, hostpath-storag or storage using "microk8s status" for availability)
microk8s enable dns storage
```

## ArgoCD setup

```bash
# Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=120s

# access the Argo CD web UI securely from your browser at http://localhost:8080
kubectl port-forward svc/argocd-server -n argocd 8080:443

# Get argocd ui pw
kubectl get secret argocd-initial-admin-secret \
  -n argocd \
  -o jsonpath="{.data.password}" | base64 -d && echo
```

## Initial Kubernetes Setup for Kafka & Argo CD Deployment

```bash
# Create Kafka Namespace
kubectl create namespace kafka

# Create Container Registry Secret
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password=YOUR_PAT \
  -n argocd

# Bootstrap root app (once)
kubectl apply -f argocd/local/root-app.yaml
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
  -- psql -U postgres -d sourcedb
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

---

## CDC Test Flow (Salesforce → Kafka → Postgres)

**Pipeline:** Salesforce CDC → Camel Salesforce Kafka Connector → Kafka → JDBC Sink → `postgresql-sink`

New manifests added:
- `06-sf-credentials-secret.yaml` — Salesforce OAuth credentials
- `07-kafka-connector-sf-source.yaml` — source connector (Account CDC)
- `08-kafka-connector-sf-sink.yaml` — sink connector (writes to staging table)

---

### Step 0 — Salesforce setup (one-time, in Salesforce Setup UI)

**A. Enable CDC on the target object**

1. Go to **Setup → Integrations → Change Data Capture**
2. Move **Account** (or whichever object) from Available to Selected
3. Save

**B. Create a Connected App**

1. **Setup → App Manager → New Connected App**
2. Check **Enable OAuth Settings**
3. Callback URL: `https://localhost` (not used, just required)
4. Selected OAuth Scopes: `api`, `refresh_token`, `offline_access`
5. Save — note the **Consumer Key** and **Consumer Secret**

**C. Get your security token** (if not already have it)

**Setup → My Personal Information → Reset My Security Token** — it will be emailed to you.

---

### Step 1 — Fill in credentials and apply the Secret

```bash
# Edit the secret file with your real values
vim manifests/local/strimzi/06-sf-credentials-secret.yaml

# password = Salesforce password + security token concatenated (no space)
# e.g. password: "MyPassword123ABCDefTokenXyz"

kubectl apply -f manifests/local/strimzi/06-sf-credentials-secret.yaml
```

---

### Step 2 — Rebuild the KafkaConnect image (adds Camel Salesforce plugin)

Applying the updated `03-kafka-connect.yaml` triggers Strimzi to rebuild the Connect image with the new plugin:

```bash
kubectl apply -f manifests/local/strimzi/03-kafka-connect.yaml
```

Watch the build pod:

```bash
kubectl get pods -n kafka -w
# A pod named debezium-connect-build-* will appear, run, then complete
# Then the debezium-connect-connect-* pod restarts with the new image
```

This takes a few minutes. Wait until the connect pod is Running again before proceeding.

---

### Step 3 — Pre-create the staging table in postgresql-sink

The SF sink connector writes raw CDC event JSON. The table must exist before the connector starts.

```bash
kubectl exec -it -n postgresql-sink \
  $(kubectl get pod -n postgresql-sink -l app.kubernetes.io/name=postgresql \
    -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U sinkuser -d sinkdb
```

```sql
CREATE TABLE IF NOT EXISTS sf_cdc_events (
    id          SERIAL PRIMARY KEY,
    value       TEXT,           -- raw Salesforce CDC event JSON
    received_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

### Step 4 — Deploy the SF source and sink connectors

```bash
kubectl apply -f manifests/local/strimzi/07-kafka-connector-sf-source.yaml
kubectl apply -f manifests/local/strimzi/08-kafka-connector-sf-sink.yaml
```

Verify all four connectors are READY:

```bash
kubectl get kafkaconnector -n kafka
# NAME        CLUSTER            CONNECTOR CLASS                                                    READY
# pg-sink     debezium-connect   io.debezium.connector.jdbc.JdbcSinkConnector                       True
# pg-source   debezium-connect   io.debezium.connector.postgresql.PostgresConnector                 True
# sf-sink     debezium-connect   io.debezium.connector.jdbc.JdbcSinkConnector                       True
# sf-source   debezium-connect   org.apache.camel.kafkaconnector.salesforce.CamelSalesforceSource…  True
```

---

### Step 5 — Trigger a CDC event in Salesforce

In Salesforce, update any Account record (name, phone, etc.). A CDC event is published within seconds.

---

### Step 6 — Verify the event arrived in Kafka

```bash
kubectl exec -it -n kafka \
  $(kubectl get pod -n kafka -l strimzi.io/name=cdc-kafka-kafka \
    -o jsonpath='{.items[0].metadata.name}') \
  -- bin/kafka-console-consumer.sh \
     --bootstrap-server localhost:9092 \
     --topic sf.AccountChangeEvent \
     --from-beginning \
     --max-messages 5
```

Expect a JSON payload like:
```json
{
  "schema": "...",
  "payload": {
    "ChangeEventHeader": {
      "entityName": "Account",
      "recordIds": ["001..."],
      "changeType": "UPDATE",
      "changedFields": ["Name"]
    },
    "Name": "Updated Account Name"
  }
}
```

---

### Step 7 — Check the row landed in postgresql-sink

```bash
kubectl exec -it -n postgresql-sink \
  $(kubectl get pod -n postgresql-sink -l app.kubernetes.io/name=postgresql \
    -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U sinkuser -d sinkdb
```

```sql
SELECT id, received_at, value::jsonb -> 'payload' -> 'ChangeEventHeader' ->> 'changeType' AS change_type
FROM sf_cdc_events
ORDER BY received_at DESC
LIMIT 5;
```

---

### Salesforce Troubleshooting

| Symptom | Fix |
|---|---|
| `sf-source` connector not READY | `kubectl describe kafkaconnector sf-source -n kafka` — check for auth errors |
| `INVALID_SESSION_ID` in Connect logs | Wrong username/password/security-token in the secret |
| No events in Kafka after Salesforce change | Confirm CDC is enabled for the object in Salesforce Setup |
| `sf.AccountChangeEvent` topic not created | Check Connect worker logs: `kubectl logs -n kafka -l strimzi.io/name=debezium-connect -f` |
| Want to track a different SF object | Edit `07-kafka-connector-sf-source.yaml`: change `topicName` to `/data/<Object>ChangeEvent` |

### Tracking a different Salesforce object

To track e.g. `Contact` instead of `Account`:

1. Enable CDC for Contact in Salesforce Setup → Change Data Capture
2. In `07-kafka-connector-sf-source.yaml`: set `camel.source.endpoint.topicName: /data/ContactChangeEvent` and `kafka.topic: sf.ContactChangeEvent`
3. In `08-kafka-connector-sf-sink.yaml`: the `topics.regex: "sf\\..*"` already covers all SF topics — no change needed
4. `kubectl apply` both files