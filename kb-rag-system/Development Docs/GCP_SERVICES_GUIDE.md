# GCP Services Adaptation Guide

## Table of Contents

1. [Overview](#overview)
2. [Deployment Status](#deployment-status)
3. [GCP Services Map](#gcp-services-map)
4. [Service 1: Cloud Run (Compute)](#service-1-cloud-run)
5. [Service 2: Artifact Registry (Container Images)](#service-2-artifact-registry)
6. [Service 3: Secret Manager (Secrets)](#service-3-secret-manager)
7. [Service 4: Cloud Storage (Article Store)](#service-4-cloud-storage)
8. [Service 5: Firestore (Execution Logs)](#service-5-firestore)
9. [Service 6: Cloud Build (CI/CD)](#service-6-cloud-build)
10. [Service 7: BigQuery (Analytics)](#service-7-bigquery)
11. [Service 8: Cloud Logging and Monitoring](#service-8-cloud-logging-and-monitoring)
12. [GCP APIs Enabled](#gcp-apis-enabled)
13. [IAM and Service Accounts](#iam-and-service-accounts)
14. [n8n Integration (AWS → GCP)](#n8n-integration-aws--gcp)
15. [Implementation Order](#implementation-order)
16. [Code Changes Required](#code-changes-required)
17. [Environment Variables Reference](#environment-variables-reference)
18. [Cost Estimates](#cost-estimates)

---

## Overview

This document details the kb-rag-system production deployment on Google Cloud Platform. The system was originally deployed on Render with OpenAI and Pinecone. The current state keeps OpenAI + Pinecone and uses GCP infrastructure for compute, secrets, storage, logging, and analytics.

### What stays the same

- **OpenAI** as LLM provider (GPT-5.4 with reasoning)
- **Pinecone** as vector database (serverless, integrated embeddings)
- **FastAPI** application code (minimal changes)
- **Dockerfile** (minor adjustments for GCP)

### What changes

| Component | Previous (Render) | Current (GCP) |
|-----------|-------------------|---------------|
| Compute | Render Web Service | **Cloud Run** |
| Container registry | Render builds from Git | **Artifact Registry** |
| Secrets | `.env` file on Render | **Secret Manager** |
| Article storage | Local JSON files | **Cloud Storage** |
| Execution logs | Application logs only | **Firestore** |
| CI/CD | Render auto-deploy | **Cloud Build** |
| Analytics | None | **BigQuery + Looker Studio** |
| Monitoring | Render metrics | **Cloud Logging + Monitoring** |

---

## Deployment Status

> **Last updated: April 8, 2026**

### Production URLs

| Resource | URL / ID |
|----------|----------|
| Cloud Run service | `https://kb-rag-system-900340137010.us-central1.run.app` |
| Artifact Registry | `us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system` |
| GCS bucket | `gs://rag-kb-system-kb-articles` |
| GCP Project ID | `rag-kb-system` |
| GCP Project Number | `900340137010` |
| GCP Organization | `forusall.com` (ID: `223006977722`) |
| GitHub repo | `ialvist/ForUsGuide` |

### What is deployed and working

| Component | Status | Notes |
|-----------|--------|-------|
| Cloud Run | **Live** | Revision `kb-rag-system-00002-gl2`, 512Mi, 1 CPU, 0-5 instances |
| Artifact Registry | **Active** | Repository `kb-rag` in `us-central1` |
| Secret Manager | **3 secrets** | `api-key` (auto-generated), `openai-api-key`, `pinecone-api-key` |
| Cloud Storage | **12 articles uploaded** | Bucket `rag-kb-system-kb-articles` with versioning |
| Firestore | **Active, logging enabled** | Native mode, `execution_logs` collection, free tier |
| Cloud Build | **Trigger active** | Auto-deploy on push to `main` via GitHub |
| BigQuery | **Dataset created** | `rag-kb-system:kb_analytics` |
| Cloud Monitoring | **2 alerts configured** | Error rate (5xx > 5) and latency (> 30s) |

### Organization policy constraints (forusall.com)

These org-level policies affect the project:

| Constraint | Impact | Workaround |
|-----------|--------|------------|
| `iam.allowedPolicyMemberDomains` | Cannot add `allUsers` to Cloud Run (no public access) | All callers authenticate via IAM identity tokens |
| `iam.disableServiceAccountKeyCreation` | Cannot create SA key JSON files | n8n uses Google OAuth2 (corporate account) + IAM Credentials API to generate tokens |

### Authentication flow

```
n8n (AWS)
  → Google OAuth2 (ivan.alvis@forusall.com)
  → IAM Credentials API (generateIdToken for kb-rag-client SA)
  → Cloud Run (Authorization: Bearer <token> + X-API-Key: <key>)
```

### Service accounts

| Service Account | Purpose | Roles |
|----------------|---------|-------|
| `kb-rag-runner@rag-kb-system.iam.gserviceaccount.com` | Cloud Run runtime identity | `secretmanager.secretAccessor`, `datastore.user`, `storage.objectViewer`, `logging.logWriter`, `monitoring.metricWriter` |
| `900340137010-compute@developer.gserviceaccount.com` | Cloud Build (default compute SA) | `storage.admin`, `artifactregistry.writer`, `logging.logWriter` |
| `900340137010@cloudbuild.gserviceaccount.com` | Cloud Build service agent | `run.admin`, `iam.serviceAccountUser` |
| `kb-rag-client@rag-kb-system.iam.gserviceaccount.com` | External API client (n8n) | `run.invoker` on kb-rag-system |

---

## GCP Services Map

```
                          CI/CD Pipeline
GitHub ──push to main──▶ Cloud Build ──▶ Artifact Registry
                                │              │
                                │         Docker images
                                ▼
n8n (AWS) ──OAuth2+Token──▶ Cloud Run (FastAPI)
Developer ──IAM Token─────▶    │
                               │
               ┌───────────────┼───────────────┐
               │               │               │
               ▼               ▼               ▼
        Secret Manager   Cloud Storage     Firestore
        (3 API keys)    (12 KB articles)  (execution_logs)
                                               │
                                               ▼
                                           BigQuery
                                         (kb_analytics)
                                               │
                                               ▼
                                         Looker Studio
                                          (dashboards)

Cloud Run also connects to:
  → OpenAI (GPT-5.4 LLM)
  → Pinecone (547 vectors, semantic search)
  → Cloud Logging (structured logs, automatic)
  → Cloud Monitoring (2 alerts: error rate, latency)
```

---

## Service 1: Cloud Run

### What it does

Runs your Docker container as a fully managed serverless service. Scales from 0 to N instances automatically.

### Current deployment

```bash
gcloud run deploy kb-rag-system \
  --image us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system:latest \
  --region us-central1 \
  --platform managed \
  --port 8000 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 5 \
  --timeout 300 \
  --service-account=kb-rag-runner@rag-kb-system.iam.gserviceaccount.com \
  --set-env-vars "ENVIRONMENT=production,LOG_LEVEL=INFO,GCP_PROJECT=rag-kb-system,INDEX_NAME=kb-articles-production,NAMESPACE=kb_articles,OPENAI_MODEL=gpt-5.4,OPENAI_REASONING_EFFORT=medium,ENABLE_EXECUTION_LOGGING=true,GCS_BUCKET=rag-kb-system-kb-articles" \
  --set-secrets "API_KEY=api-key:latest,OPENAI_API_KEY=openai-api-key:latest,PINECONE_API_KEY=pinecone-api-key:latest"
```

**Note:** `--allow-unauthenticated` is not used because the organization policy blocks public access. All callers must authenticate via IAM (identity token).

### Key configuration

| Setting | Value | Reason |
|---------|-------|--------|
| Memory | 512Mi | Sufficient for FastAPI + tiktoken + Pinecone SDK |
| CPU | 1 | GPT-5.4 calls are I/O bound (network wait), not CPU bound |
| Min instances | 0 | Scale to zero when no traffic (cost savings) |
| Max instances | 5 | Cap to control costs; increase as traffic grows |
| Timeout | 300s | `generate_response` can take up to 180s (LLM timeout) + buffer |
| Concurrency | 80 (default) | FastAPI handles concurrent async requests well |

### Accessing the API

Cloud Run requires IAM authentication. Every request needs an identity token:

```bash
# From your local machine (using your Google account)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  https://kb-rag-system-900340137010.us-central1.run.app/health

# For authenticated endpoints, also include the API key
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "X-API-Key: $(gcloud secrets versions access latest --secret=api-key)" \
  -H "Content-Type: application/json" \
  -d '{"question": "What is a 401k rollover?"}' \
  https://kb-rag-system-900340137010.us-central1.run.app/api/v1/knowledge-question
```

### Dockerfile adjustments

```dockerfile
ENV PORT=8000
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 2"]
```

Cloud Run sets the `PORT` environment variable. Using `${PORT}` instead of hardcoded `8000` follows GCP best practices.

### Custom domain (future)

```bash
gcloud run domain-mappings create \
  --service kb-rag-system \
  --domain api.forusguide.com \
  --region us-central1
```

---

## Service 2: Artifact Registry

### What it does

Stores your Docker images with versioning, vulnerability scanning, and access control.

### Setup (completed)

```bash
gcloud artifacts repositories create kb-rag \
  --repository-format docker \
  --location us-central1 \
  --description "KB RAG System Docker images"
```

### Building images

Since Docker Desktop is not always available locally, use Cloud Build to build in the cloud:

```bash
gcloud builds submit kb-rag-system/ \
  --tag us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system:latest
```

### Image naming convention

```
us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system:TAG

Tags:
  :latest         - current production
  :v1.2.3         - semantic version
  :sha-abc1234    - git commit SHA (set by Cloud Build)
```

---

## Service 3: Secret Manager

### What it does

Securely stores and manages API keys, passwords, and other sensitive configuration. Replaces the `.env` file.

### Secrets created

```bash
# API key was auto-generated (cryptographically secure)
openssl rand -base64 32 | tr -d '\n' | gcloud secrets create api-key --data-file=-

# OpenAI and Pinecone keys copied from existing .env
echo -n "sk-your-openai-key" | gcloud secrets create openai-api-key --data-file=-
echo -n "your-pinecone-key" | gcloud secrets create pinecone-api-key --data-file=-
```

### Retrieving the API key

```bash
gcloud secrets versions access latest --secret=api-key
```

### How Cloud Run accesses secrets

Secrets are mounted as environment variables at deploy time (see `--set-secrets` in the Cloud Run deploy command). No code changes needed — `pydantic-settings` reads them from env vars.

### Secret rotation

```bash
# Add a new version (rotates the secret)
echo -n "new-api-key-value" | gcloud secrets versions add openai-api-key --data-file=-

# Redeploy to pick up the new version (if using :latest)
gcloud run services update kb-rag-system --region us-central1
```

---

## Service 4: Cloud Storage

### What it does

Stores KB article JSON files centrally. Replaces local filesystem storage for articles.

### Bucket setup (completed)

```bash
gcloud storage buckets create gs://rag-kb-system-kb-articles \
  --location us-central1 \
  --uniform-bucket-level-access

gcloud storage buckets update gs://rag-kb-system-kb-articles --versioning

# 12 articles uploaded April 7, 2026
gcloud storage cp "PA/Distributions/"*.json gs://rag-kb-system-kb-articles/articles/
```

### Bucket structure

```
gs://rag-kb-system-kb-articles/
  articles/
    401(k) Options After Leaving Your Job (...).json
    ForUsAll 401(k) Hardship Withdrawal (...).json
    LT: How to Request a 401(k) Termination (...).json
    ... (12 articles total)
  archive/
    (old versions, moved by lifecycle policy)
```

### Code changes

Implemented in `data_pipeline/storage.py`:

```python
class ArticleStore:
    def __init__(self, bucket_name: str, project: Optional[str] = None): ...
    def get_article(self, article_id: str) -> dict: ...
    def list_articles(self, prefix: str = "articles/") -> list[str]: ...
    def upload_article(self, article_id: str, data: dict) -> None: ...
    def delete_article(self, article_id: str) -> None: ...
    def article_exists(self, article_id: str) -> bool: ...
```

---

## Service 5: Firestore

### What it does

Stores structured execution logs for every API request. Enables querying, analysis, and audit trails.

### Setup (completed)

```bash
gcloud firestore databases create --location=us-central1
```

Console: https://console.cloud.google.com/firestore/databases/(default)/data/execution_logs?project=rag-kb-system

### Data model

```
Collection: execution_logs
  Document: {auto-generated ID}
    - request_id: string
    - endpoint: string ("required_data" | "generate_response" | "knowledge_question")
    - timestamp: timestamp
    - duration_ms: number
    - request:
        - inquiry: string
        - topic: string
        - record_keeper: string (nullable)
        - plan_type: string
    - response:
        - decision: string (for generate_response)
        - confidence: number
        - outcome: string
        - chunks_used: number
        - coverage_gaps: array[string]
    - llm_metadata:
        - model: string
        - prompt_tokens: number
        - completion_tokens: number
        - total_tokens: number
    - source_articles: array[string]
    - error: string (nullable)
```

### Code changes

Implemented in `data_pipeline/execution_logger.py`. Integrated into all three API endpoints in `api/main.py` with:
- Conditional initialization via `ENABLE_EXECUTION_LOGGING` setting
- Logging on both success and error paths
- Fire-and-forget pattern (logging failures never break the API response)

---

## Service 6: Cloud Build

### What it does

Automates building Docker images and deploying to Cloud Run on every push to `main`.

### Setup (completed)

The `cloudbuild.yaml` is in `kb-rag-system/`:

```yaml
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:$SHORT_SHA',
           '-t', 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:latest', '.']
    dir: 'kb-rag-system'

  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', '--all-tags', 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system']

  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    entrypoint: gcloud
    args: ['run', 'deploy', 'kb-rag-system',
           '--image', 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:$SHORT_SHA',
           '--region', 'us-central1', '--platform', 'managed']

options:
  logging: CLOUD_LOGGING_ONLY

images:
  - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:$SHORT_SHA'
  - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:latest'
```

### GitHub trigger

Created via GCP Console (Cloud Build → Triggers):

| Setting | Value |
|---------|-------|
| Trigger name | `deploy-kb-rag-system` |
| Source | `ialvist/ForUsGuide` |
| Branch | `^main$` |
| Config file | `kb-rag-system/cloudbuild.yaml` |
| Service account | `900340137010-compute@developer.gserviceaccount.com` |

---

## Service 7: BigQuery

### What it does

Enables SQL-based analytics over execution logs. Connects to Looker Studio for dashboards.

### Setup (completed)

```bash
bq mk --dataset --location=us-central1 rag-kb-system:kb_analytics
```

### Pending: Firestore → BigQuery export

```bash
firebase ext:install firestore-bigquery-export \
  --project=rag-kb-system \
  --params="COLLECTION_PATH=execution_logs,DATASET_ID=kb_analytics,TABLE_ID=execution_logs"
```

### Example analytics queries

```sql
-- Top 10 most common topics
SELECT request.topic, COUNT(*) as total_requests,
  AVG(response.confidence) as avg_confidence
FROM `rag-kb-system.kb_analytics.execution_logs`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY request.topic ORDER BY total_requests DESC LIMIT 10;

-- Daily cost estimate
SELECT DATE(timestamp) as day, COUNT(*) as requests,
  SUM(llm_metadata.total_tokens) as total_tokens,
  ROUND(SUM(llm_metadata.total_tokens) / 1000000.0 * 10, 2) as estimated_cost_usd
FROM `rag-kb-system.kb_analytics.execution_logs`
GROUP BY day ORDER BY day DESC;

-- Coverage gap analysis
SELECT gap, COUNT(*) as occurrences,
  ARRAY_AGG(DISTINCT request.topic) as related_topics
FROM `rag-kb-system.kb_analytics.execution_logs`,
UNNEST(response.coverage_gaps) as gap
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY gap ORDER BY occurrences DESC;

-- Error rate by endpoint
SELECT endpoint, COUNTIF(error IS NOT NULL) as errors, COUNT(*) as total,
  ROUND(COUNTIF(error IS NOT NULL) / COUNT(*) * 100, 2) as error_rate_pct
FROM `rag-kb-system.kb_analytics.execution_logs`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY endpoint;
```

### Looker Studio dashboard

1. Go to [Looker Studio](https://lookerstudio.google.com/)
2. Create Data Source → BigQuery → select `kb_analytics.execution_logs`
3. Build charts: requests over time, confidence distribution, token usage, cost trends

---

## Service 8: Cloud Logging and Monitoring

### What it does

Cloud Run automatically sends all stdout/stderr to Cloud Logging. Cloud Monitoring provides metrics, dashboards, and alerts.

### Structured logging (completed)

```python
if settings.ENVIRONMENT == "production":
    try:
        import google.cloud.logging as cloud_logging
        cloud_logging.Client().setup_logging()
    except Exception:
        pass
```

### Alerts configured

Created via GCP Console (Monitoring → Alerting):

| Alert | Metric | Threshold | Window |
|-------|--------|-----------|--------|
| KB RAG High Error Rate | `request_count` where `response_code_class=5xx` | > 5 | 5 min |
| KB RAG High Latency | `request_latencies` | > 30,000 ms | 5 min |

Console: https://console.cloud.google.com/monitoring/alerting?project=rag-kb-system

### Useful log queries

```
# All errors
resource.type="cloud_run_revision"
resource.labels.service_name="kb-rag-system"
severity>=ERROR

# LLM call logs
resource.type="cloud_run_revision"
resource.labels.service_name="kb-rag-system"
jsonPayload.message=~"Llamando GPT"

# Slow requests (> 10 seconds)
resource.type="cloud_run_revision"
httpRequest.latency>"10s"
```

---

## GCP APIs Enabled

Active APIs in project `rag-kb-system`:

```bash
# Compute and Deploy
gcloud services enable run.googleapis.com              # Cloud Run
gcloud services enable artifactregistry.googleapis.com # Artifact Registry
gcloud services enable cloudbuild.googleapis.com       # Cloud Build

# Security
gcloud services enable secretmanager.googleapis.com    # Secret Manager
gcloud services enable iam.googleapis.com              # IAM
gcloud services enable iamcredentials.googleapis.com   # IAM Credentials (token generation for n8n)

# Storage and Data
gcloud services enable storage.googleapis.com          # Cloud Storage
gcloud services enable firestore.googleapis.com        # Firestore
gcloud services enable bigquery.googleapis.com         # BigQuery

# Observability
gcloud services enable logging.googleapis.com          # Cloud Logging
gcloud services enable monitoring.googleapis.com       # Cloud Monitoring
gcloud services enable cloudtrace.googleapis.com       # Cloud Trace
```

### Disabled / Not used

| API | Reason |
|-----|--------|
| `sts.googleapis.com` | Was enabled for Workload Identity Federation; disabled after switching to OAuth2 auth flow |
| `aiplatform.googleapis.com` | Future: Vertex AI for Gemini hybrid (not needed yet) |

---

## IAM and Service Accounts

### Cloud Run service account (kb-rag-runner)

Dedicated service account for Cloud Run runtime. Follows least-privilege principle.

```bash
gcloud iam service-accounts create kb-rag-runner \
  --display-name="KB RAG System Runner"

SA_EMAIL="kb-rag-runner@rag-kb-system.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding rag-kb-system \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
gcloud projects add-iam-policy-binding rag-kb-system \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding rag-kb-system \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectViewer"
gcloud projects add-iam-policy-binding rag-kb-system \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding rag-kb-system \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/monitoring.metricWriter"
```

### Cloud Build service accounts

```bash
# Default compute SA (builds images)
# 900340137010-compute@developer.gserviceaccount.com
# Roles: storage.admin, artifactregistry.writer, logging.logWriter

# Cloud Build SA (deploys to Cloud Run)
# 900340137010@cloudbuild.gserviceaccount.com
# Roles: run.admin, iam.serviceAccountUser
```

### API client service account (kb-rag-client)

For n8n to invoke Cloud Run. Identity tokens are generated via Google OAuth2 + IAM Credentials API.

```bash
gcloud iam service-accounts create kb-rag-client \
  --display-name="KB RAG API Client (n8n)"

# Grant invoke permission on Cloud Run
gcloud run services add-iam-policy-binding kb-rag-system \
  --region=us-central1 \
  --member="serviceAccount:kb-rag-client@rag-kb-system.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# Grant token creation permission to the corporate account
gcloud iam service-accounts add-iam-policy-binding \
  kb-rag-client@rag-kb-system.iam.gserviceaccount.com \
  --member="user:ivan.alvis@forusall.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

**Note:** SA key creation is blocked by org policy. Tokens are generated via IAM Credentials API using the corporate Google OAuth2 credential.

---

## n8n Integration (AWS → GCP)

n8n runs on AWS and calls the Cloud Run API. Because the org policy blocks both public access and SA key creation, authentication uses Google OAuth2 with the corporate account.

### How it works

```
n8n workflow:
  1. HTTP Request node authenticates via Google OAuth2 (ivan.alvis@forusall.com)
  2. Calls IAM Credentials API to generate an identity token for kb-rag-client SA
  3. Uses that token to call the Cloud Run API endpoints
```

### Step 1 — Create OAuth2 Client in GCP Console

1. Go to https://console.cloud.google.com/apis/credentials?project=rag-kb-system
2. Create Credentials → OAuth client ID → Web application
3. Name: `n8n`
4. Authorized redirect URI: `https://n8n.forusall.com/rest/oauth2-credential/callback`
5. Copy Client ID and Client Secret

### Step 2 — Configure in n8n

**Credential:** Create a **Google OAuth2 API** credential with:
- Client ID and Client Secret from step 1
- Scope: `https://www.googleapis.com/auth/cloud-platform`

**Node 1 (HTTP Request) — Generate identity token:**

- Method: `POST`
- URL: `https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/kb-rag-client@rag-kb-system.iam.gserviceaccount.com:generateIdToken`
- Authentication: Google OAuth2 API credential
- Body:

```json
{
  "audience": "https://kb-rag-system-900340137010.us-central1.run.app",
  "includeEmail": true
}
```

**Node 2 (HTTP Request) — Call the RAG API:**

- URL: `https://kb-rag-system-900340137010.us-central1.run.app/api/v1/generate-response`
- Headers:
  - `Authorization: Bearer {{ $json.token }}`
  - `X-API-Key: <your-api-key>`
- Body: the request payload

### API request examples

**knowledge-question** (no API key required):

```json
{
  "question": "What is a 401k rollover?"
}
```

**required-data:**

```json
{
  "inquiry": "I want to take a hardship withdrawal from my 401k",
  "topic": "hardship",
  "plan_type": "401(k)",
  "record_keeper": "LT Trust"
}
```

**generate-response:**

```json
{
  "inquiry": "I want to take a hardship withdrawal from my 401k",
  "topic": "hardship",
  "plan_type": "401(k)",
  "record_keeper": "LT Trust",
  "collected_data": {
    "participant_data": {
      "employment_status": "active",
      "age": 35,
      "vested_balance": 45000
    },
    "plan_data": {
      "hardship_withdrawals_allowed": true,
      "loan_available": true
    }
  },
  "max_response_tokens": 5000,
  "total_inquiries_in_ticket": 1
}
```

---

## Implementation Order

### Phase 1: Foundation — COMPLETED (April 7, 2026)

1. ~~Create GCP project~~ → `rag-kb-system`
2. ~~Enable all APIs~~
3. ~~Create service accounts and assign IAM roles~~
4. ~~Create Artifact Registry repository~~ → `kb-rag`
5. ~~Create secrets in Secret Manager~~ → 3 secrets
6. ~~Deploy to Cloud Run (manual first deploy via Cloud Build)~~
7. ~~Verify health check~~ → `{"status":"healthy","pinecone_connected":true,"openai_configured":true,"total_vectors":547}`

### Phase 2: CI/CD — COMPLETED (April 7, 2026)

1. ~~Create `cloudbuild.yaml`~~
2. ~~Connect GitHub repo to Cloud Build~~ → trigger via GCP Console
3. ~~Grant Cloud Build permissions~~
4. Test rollback flow (pending)

### Phase 3: Storage and Logging — COMPLETED (April 7, 2026)

1. ~~Create Cloud Storage bucket~~ → `rag-kb-system-kb-articles`
2. ~~Upload existing articles~~ → 12 articles
3. ~~`data_pipeline/storage.py` already created~~
4. ~~Create Firestore database~~
5. ~~`data_pipeline/execution_logger.py` already created~~
6. ~~Integrate ExecutionLogger into API endpoints~~
7. ~~Verify logs appear in Firestore~~ → confirmed via knowledge-question test

### Phase 4: Analytics — PARTIALLY COMPLETED (April 7, 2026)

1. Firestore → BigQuery export (pending: install Firebase extension)
2. ~~Create BigQuery dataset~~ → `kb_analytics`
3. Build Looker Studio dashboard (pending)
4. ~~Set up Cloud Monitoring alerts~~ → 2 alerts created
5. Create useful log-based metrics (pending)

### Phase 5: n8n Integration — IN PROGRESS

1. ~~Create `kb-rag-client` SA with `run.invoker` role~~
2. ~~Grant `serviceAccountTokenCreator` to corporate account~~
3. Create OAuth2 Client in GCP Console (pending)
4. Configure Google OAuth2 credential in n8n (pending)
5. Test end-to-end flow from n8n to Cloud Run (pending)

### Phase 6: Optimization (Ongoing)

1. Tune Cloud Run settings (memory, concurrency, min instances)
2. Add Cloud CDN if caching makes sense
3. Set up budget alerts in GCP Billing
4. Review and optimize based on dashboard insights

---

## Code Changes Required

### Summary of files created

| File | Purpose | Status |
|------|---------|--------|
| `data_pipeline/storage.py` | Cloud Storage article reader/writer | **Created** |
| `data_pipeline/execution_logger.py` | Firestore execution logger | **Created** |
| `cloudbuild.yaml` | CI/CD pipeline definition | **Created** |

### Summary of files modified

| File | Changes | Status |
|------|---------|--------|
| `api/config.py` | Added `GCP_PROJECT`, `GCS_BUCKET`, `ENABLE_EXECUTION_LOGGING` settings | **Done** |
| `api/main.py` | Initialized `ExecutionLogger` in lifespan; structured logging for production; logger in all 3 endpoints | **Done** |
| `Dockerfile` | Uses `${PORT}` env var for Cloud Run compatibility | **Done** |
| `requirements.txt` | Added `google-cloud-firestore`, `google-cloud-storage`, `google-cloud-logging` | **Done** |

### Dependencies added

```txt
google-cloud-firestore>=2.16.0
google-cloud-storage>=2.16.0
google-cloud-logging>=3.10.0
```

---

## Environment Variables Reference

### Production (Cloud Run + Secret Manager)

| Variable | Source | Value |
|----------|--------|-------|
| `API_KEY` | Secret Manager | `api-key:latest` (auto-generated) |
| `OPENAI_API_KEY` | Secret Manager | `openai-api-key:latest` |
| `PINECONE_API_KEY` | Secret Manager | `pinecone-api-key:latest` |
| `ENVIRONMENT` | Env var | `production` |
| `LOG_LEVEL` | Env var | `INFO` |
| `GCP_PROJECT` | Env var | `rag-kb-system` |
| `GCS_BUCKET` | Env var | `rag-kb-system-kb-articles` |
| `ENABLE_EXECUTION_LOGGING` | Env var | `true` |
| `INDEX_NAME` | Env var | `kb-articles-production` |
| `NAMESPACE` | Env var | `kb_articles` |
| `OPENAI_MODEL` | Env var | `gpt-5.4` |
| `OPENAI_REASONING_EFFORT` | Env var | `medium` |

### Local development

Keep using `.env` file. No changes needed for local dev.

---

## Cost Estimates

### Monthly cost by service (low traffic: ~100 req/day)

| Service | Free Tier | Estimated Usage | Cost |
|---------|-----------|-----------------|------|
| Cloud Run | 2M requests, 360K vCPU-sec | ~3K requests | $0 |
| Artifact Registry | 500MB free | ~500MB | $0 |
| Secret Manager | 6 versions, 10K accesses | 3 secrets, ~3K accesses | $0 |
| Cloud Storage | 5GB free | <1GB | $0 |
| Firestore | 1 GiB, 50K reads/day | ~3K writes/day | $0 |
| BigQuery | 1TB queries, 10GB storage | <1GB | $0 |
| Cloud Build | 120 min/day | ~10 builds/month | $0 |
| Cloud Logging | 50 GiB/month | <1 GiB | $0 |
| **GCP Total** | | | **$0-5/month** |
| OpenAI (GPT-5.4) | | ~3K requests | **$750-1,200/month** |
| Pinecone | | Serverless | **$0-15/month** |
| **Grand Total** | | | **$750-1,220/month** |

The dominant cost is OpenAI. GCP infrastructure is practically free at this scale. See [HYBRID_LLM_ARCHITECTURE.md](./HYBRID_LLM_ARCHITECTURE.md) for how to reduce LLM costs by 50%+ with the Gemini hybrid approach.
