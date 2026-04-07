# GCP Services Adaptation Guide

## Table of Contents

1. [Overview](#overview)
2. [GCP Services Map](#gcp-services-map)
3. [Service 1: Cloud Run (Compute)](#service-1-cloud-run)
4. [Service 2: Artifact Registry (Container Images)](#service-2-artifact-registry)
5. [Service 3: Secret Manager (Secrets)](#service-3-secret-manager)
6. [Service 4: Cloud Storage (Article Store)](#service-4-cloud-storage)
7. [Service 5: Firestore (Execution Logs)](#service-5-firestore)
8. [Service 6: Cloud Build (CI/CD)](#service-6-cloud-build)
9. [Service 7: BigQuery (Analytics)](#service-7-bigquery)
10. [Service 8: Cloud Logging and Monitoring](#service-8-cloud-logging-and-monitoring)
11. [GCP APIs to Enable](#gcp-apis-to-enable)
12. [IAM and Service Accounts](#iam-and-service-accounts)
13. [Implementation Order](#implementation-order)
14. [Code Changes Required](#code-changes-required)
15. [Environment Variables Reference](#environment-variables-reference)
16. [Cost Estimates](#cost-estimates)

---

## Overview

This document details how to adapt the kb-rag-system for production on Google Cloud Platform. The current system runs on Render with OpenAI and Pinecone. The target state keeps OpenAI + Pinecone and adds GCP infrastructure for compute, secrets, storage, logging, and analytics.

### What stays the same

- **OpenAI** as LLM provider (GPT-5.4 with reasoning)
- **Pinecone** as vector database (serverless, integrated embeddings)
- **FastAPI** application code (minimal changes)
- **Dockerfile** (minor adjustments for GCP)

### What changes

| Component | Current (Render) | Target (GCP) |
|-----------|-----------------|---------------|
| Compute | Render Web Service | **Cloud Run** |
| Container registry | Render builds from Git | **Artifact Registry** |
| Secrets | `.env` file on Render | **Secret Manager** |
| Article storage | Local JSON files | **Cloud Storage** |
| Execution logs | Application logs only | **Firestore** |
| CI/CD | Render auto-deploy | **Cloud Build** |
| Analytics | None | **BigQuery + Looker Studio** |
| Monitoring | Render metrics | **Cloud Logging + Monitoring** |

---

## GCP Services Map

```
Internet
   |
   v
Cloud Run  <--- Cloud Build <--- GitHub (push to main)
   |                |
   |           Artifact Registry (Docker images)
   |
   +---> Secret Manager (API keys)
   +---> Pinecone (vector search, external)
   +---> OpenAI (LLM, external)
   +---> Cloud Storage (KB articles JSON)
   +---> Firestore (execution logs)
   |         |
   |         +---> BigQuery (analytics export)
   |                    |
   |               Looker Studio (dashboards)
   |
   +---> Cloud Logging (auto-captured)
   +---> Cloud Monitoring (alerts)
```

---

## Service 1: Cloud Run

### What it does

Runs your Docker container as a fully managed serverless service. Scales from 0 to N instances automatically.

### Setup

```bash
# Deploy from a pre-built image in Artifact Registry
gcloud run deploy kb-rag-system \
  --image us-central1-docker.pkg.dev/PROJECT_ID/kb-rag/kb-rag-system:latest \
  --region us-central1 \
  --platform managed \
  --port 8000 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 5 \
  --timeout 300 \
  --set-env-vars "ENVIRONMENT=production,LOG_LEVEL=INFO" \
  --set-secrets "API_KEY=api-key:latest,OPENAI_API_KEY=openai-api-key:latest,PINECONE_API_KEY=pinecone-api-key:latest" \
  --allow-unauthenticated
```

### Key configuration

| Setting | Value | Reason |
|---------|-------|--------|
| Memory | 512Mi | Sufficient for FastAPI + tiktoken + Pinecone SDK |
| CPU | 1 | GPT-5.4 calls are I/O bound (network wait), not CPU bound |
| Min instances | 0 | Scale to zero when no traffic (cost savings) |
| Max instances | 5 | Cap to control costs; increase as traffic grows |
| Timeout | 300s | `generate_response` can take up to 180s (LLM timeout) + buffer |
| Concurrency | 80 (default) | FastAPI handles concurrent async requests well |

### Dockerfile adjustments

The existing Dockerfile works as-is. One small improvement for Cloud Run:

```dockerfile
# Add Cloud Run's PORT env var support
ENV PORT=8000
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 2"]
```

Cloud Run sets the `PORT` environment variable. Using `${PORT}` instead of hardcoded `8000` follows GCP best practices.

### Custom domain

```bash
# Map a custom domain (e.g., api.forusguide.com)
gcloud run domain-mappings create \
  --service kb-rag-system \
  --domain api.forusguide.com \
  --region us-central1
```

Cloud Run provides HTTPS with managed SSL certificates automatically.

---

## Service 2: Artifact Registry

### What it does

Stores your Docker images with versioning, vulnerability scanning, and access control.

### Setup

```bash
# Create a Docker repository
gcloud artifacts repositories create kb-rag \
  --repository-format docker \
  --location us-central1 \
  --description "KB RAG System Docker images"

# Configure Docker to push to Artifact Registry
gcloud auth configure-docker us-central1-docker.pkg.dev

# Build and push (manual, for reference — Cloud Build automates this)
docker build -t us-central1-docker.pkg.dev/PROJECT_ID/kb-rag/kb-rag-system:latest .
docker push us-central1-docker.pkg.dev/PROJECT_ID/kb-rag/kb-rag-system:latest
```

### Image naming convention

```
us-central1-docker.pkg.dev/PROJECT_ID/kb-rag/kb-rag-system:TAG

Tags:
  :latest         - current production
  :v1.2.3         - semantic version
  :sha-abc1234    - git commit SHA (set by Cloud Build)
```

---

## Service 3: Secret Manager

### What it does

Securely stores and manages API keys, passwords, and other sensitive configuration. Replaces the `.env` file.

### Secrets to create

```bash
# Create each secret
echo -n "your-api-key" | gcloud secrets create api-key --data-file=-
echo -n "sk-your-openai-key" | gcloud secrets create openai-api-key --data-file=-
echo -n "your-pinecone-key" | gcloud secrets create pinecone-api-key --data-file=-
```

### How Cloud Run accesses secrets

Secrets are mounted as environment variables at deploy time (see `--set-secrets` in the Cloud Run deploy command above). No code changes needed — `pydantic-settings` reads them from env vars just like today.

### Secret rotation

```bash
# Add a new version (rotates the secret)
echo -n "new-api-key-value" | gcloud secrets versions add openai-api-key --data-file=-

# Redeploy to pick up the new version (if using :latest)
gcloud run services update kb-rag-system --region us-central1
```

### Code changes required

**None for basic usage.** Cloud Run injects secrets as env vars, which `pydantic-settings` already reads.

If you want to access secrets programmatically (e.g., for dynamic rotation):

```python
# Optional: programmatic access
from google.cloud import secretmanager

def get_secret(project_id: str, secret_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")
```

---

## Service 4: Cloud Storage

### What it does

Stores KB article JSON files centrally. Replaces local filesystem storage for articles.

### Bucket setup

```bash
# Create bucket with versioning
gcloud storage buckets create gs://PROJECT_ID-kb-articles \
  --location us-central1 \
  --uniform-bucket-level-access

# Enable versioning (track article changes over time)
gcloud storage buckets update gs://PROJECT_ID-kb-articles --versioning

# Upload existing articles
gsutil -m cp articles/*.json gs://PROJECT_ID-kb-articles/articles/
```

### Bucket structure

```
gs://PROJECT_ID-kb-articles/
  articles/
    ART-001-rollover-lt-trust.json
    ART-002-distribution-lt-trust.json
    ...
  archive/
    (old versions, moved by lifecycle policy)
```

### Code changes required

The ingestion scripts (`scripts/process_single_article.py`) currently read from local filesystem. Add a GCS reader:

```python
# data_pipeline/storage.py

from google.cloud import storage

class ArticleStore:
    """Read/write KB articles from Cloud Storage."""

    def __init__(self, bucket_name: str):
        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket_name)

    def get_article(self, article_id: str) -> dict:
        """Download and parse a single article JSON."""
        blob = self.bucket.blob(f"articles/{article_id}.json")
        content = blob.download_as_text()
        return json.loads(content)

    def list_articles(self, prefix: str = "articles/") -> list[str]:
        """List all article IDs in the bucket."""
        blobs = self.bucket.list_blobs(prefix=prefix)
        return [b.name.split("/")[-1].replace(".json", "") for b in blobs]

    def upload_article(self, article_id: str, data: dict):
        """Upload an article JSON to the bucket."""
        blob = self.bucket.blob(f"articles/{article_id}.json")
        blob.upload_from_string(
            json.dumps(data, indent=2, ensure_ascii=False),
            content_type="application/json",
        )
```

### Auto-ingest on upload (optional, future)

Use Eventarc to trigger a Cloud Function when a new article is uploaded:

```bash
# This triggers re-ingestion (chunking + Pinecone upsert) automatically
gcloud functions deploy ingest-article \
  --runtime python312 \
  --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
  --trigger-event-filters="bucket=PROJECT_ID-kb-articles"
```

---

## Service 5: Firestore

### What it does

Stores structured execution logs for every API request. Enables querying, analysis, and audit trails.

### Setup

```bash
# Create Firestore database (Native mode)
gcloud firestore databases create --location=us-central1
```

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
    - source_articles: array[string]  (article IDs used)
    - error: string (nullable, if request failed)

Collection: daily_stats
  Document: {YYYY-MM-DD}
    - total_requests: number
    - by_endpoint: map
    - by_topic: map
    - avg_confidence: number
    - avg_duration_ms: number
    - error_count: number
    - total_tokens: number
    - estimated_cost_usd: number
```

### Code changes required

Add a logging service that the API middleware calls after each request:

```python
# data_pipeline/execution_logger.py

from google.cloud import firestore
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ExecutionLogger:
    """Logs API execution details to Firestore."""

    def __init__(self, project_id: Optional[str] = None):
        self.db = firestore.AsyncClient(project=project_id)
        self.collection = self.db.collection("execution_logs")

    async def log_execution(
        self,
        request_id: str,
        endpoint: str,
        duration_ms: float,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        error: Optional[str] = None,
    ):
        """Log a single API execution to Firestore."""
        doc = {
            "request_id": request_id,
            "endpoint": endpoint,
            "timestamp": datetime.now(timezone.utc),
            "duration_ms": round(duration_ms, 1),
            "request": {
                "inquiry": request_data.get("inquiry", "")[:500],
                "topic": request_data.get("topic", ""),
                "record_keeper": request_data.get("record_keeper"),
                "plan_type": request_data.get("plan_type", ""),
            },
            "response": {
                "decision": response_data.get("decision"),
                "confidence": response_data.get("confidence"),
                "outcome": response_data.get("response", {}).get("outcome"),
                "chunks_used": response_data.get("metadata", {}).get("chunks_used", 0),
                "coverage_gaps": response_data.get("coverage_gaps", []),
            },
            "llm_metadata": {
                "model": response_data.get("metadata", {}).get("model", ""),
                "prompt_tokens": response_data.get("metadata", {}).get("prompt_tokens", 0),
                "completion_tokens": response_data.get("metadata", {}).get("completion_tokens", 0),
                "total_tokens": response_data.get("metadata", {}).get("total_tokens", 0),
            },
            "source_articles": [
                sa.get("article_id", "")
                for sa in response_data.get("source_articles", [])
            ],
            "error": error,
        }

        try:
            await self.collection.add(doc)
        except Exception as e:
            # Never let logging failure break the API response
            logger.error(f"Failed to log execution to Firestore: {e}")
```

### Integration in `api/main.py`

```python
# In lifespan
app.state.execution_logger = ExecutionLogger(project_id=settings.GCP_PROJECT)

# In each endpoint, after getting the result:
import time

@app.post("/api/v1/generate-response", ...)
async def generate_response_endpoint(request, engine, exec_logger):
    start = time.monotonic()
    try:
        result = await engine.generate_response(...)
        duration_ms = (time.monotonic() - start) * 1000
        await exec_logger.log_execution(
            request_id=request.state.request_id,
            endpoint="generate_response",
            duration_ms=duration_ms,
            request_data=request.model_dump(),
            response_data=result.__dict__,
        )
        return result
    except Exception as e:
        duration_ms = (time.monotonic() - start) * 1000
        await exec_logger.log_execution(
            request_id=request.state.request_id,
            endpoint="generate_response",
            duration_ms=duration_ms,
            request_data=request.model_dump(),
            response_data={},
            error=str(e),
        )
        raise
```

---

## Service 6: Cloud Build

### What it does

Automates building Docker images and deploying to Cloud Run on every push to `main`.

### Setup

Create a `cloudbuild.yaml` at the repo root (or in `kb-rag-system/`):

```yaml
# cloudbuild.yaml
steps:
  # Build the Docker image
  - name: 'gcr.io/cloud-builders/docker'
    args:
      - 'build'
      - '-t'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:$SHORT_SHA'
      - '-t'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:latest'
      - '.'
    dir: 'kb-rag-system'

  # Push to Artifact Registry
  - name: 'gcr.io/cloud-builders/docker'
    args:
      - 'push'
      - '--all-tags'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system'

  # Deploy to Cloud Run
  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    entrypoint: gcloud
    args:
      - 'run'
      - 'deploy'
      - 'kb-rag-system'
      - '--image'
      - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:$SHORT_SHA'
      - '--region'
      - 'us-central1'
      - '--platform'
      - 'managed'

options:
  logging: CLOUD_LOGGING_ONLY

images:
  - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:$SHORT_SHA'
  - 'us-central1-docker.pkg.dev/$PROJECT_ID/kb-rag/kb-rag-system:latest'
```

### Connect to GitHub

```bash
# Create a trigger that runs on push to main
gcloud builds triggers create github \
  --repo-name="FUA-Knowledge-Base-Articles" \
  --repo-owner="YOUR_GITHUB_ORG" \
  --branch-pattern="^main$" \
  --build-config="kb-rag-system/cloudbuild.yaml" \
  --name="deploy-kb-rag-system"
```

---

## Service 7: BigQuery

### What it does

Enables SQL-based analytics over execution logs. Connects to Looker Studio for dashboards.

### Setup

```bash
# Create dataset
bq mk --dataset --location=us-central1 PROJECT_ID:kb_analytics

# Create scheduled export from Firestore (via Firestore Export + BigQuery)
# Or use the Firestore BigQuery Extension:
firebase ext:install firestore-bigquery-export \
  --project=PROJECT_ID \
  --params="COLLECTION_PATH=execution_logs,DATASET_ID=kb_analytics,TABLE_ID=execution_logs"
```

### Example analytics queries

```sql
-- Top 10 most common topics
SELECT
  request.topic,
  COUNT(*) as total_requests,
  AVG(response.confidence) as avg_confidence,
  AVG(duration_ms) as avg_latency_ms
FROM `PROJECT_ID.kb_analytics.execution_logs`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY request.topic
ORDER BY total_requests DESC
LIMIT 10;

-- Daily cost estimate
SELECT
  DATE(timestamp) as day,
  COUNT(*) as requests,
  SUM(llm_metadata.total_tokens) as total_tokens,
  ROUND(SUM(llm_metadata.total_tokens) / 1000000.0 * 10, 2) as estimated_cost_usd
FROM `PROJECT_ID.kb_analytics.execution_logs`
GROUP BY day
ORDER BY day DESC;

-- Coverage gap analysis (which topics need more KB articles)
SELECT
  gap,
  COUNT(*) as occurrences,
  ARRAY_AGG(DISTINCT request.topic) as related_topics
FROM `PROJECT_ID.kb_analytics.execution_logs`,
UNNEST(response.coverage_gaps) as gap
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY gap
ORDER BY occurrences DESC;

-- Articles most frequently used as sources
SELECT
  article_id,
  COUNT(*) as times_used,
  AVG(response.confidence) as avg_confidence
FROM `PROJECT_ID.kb_analytics.execution_logs`,
UNNEST(source_articles) as article_id
GROUP BY article_id
ORDER BY times_used DESC;

-- Error rate by endpoint
SELECT
  endpoint,
  COUNTIF(error IS NOT NULL) as errors,
  COUNT(*) as total,
  ROUND(COUNTIF(error IS NOT NULL) / COUNT(*) * 100, 2) as error_rate_pct
FROM `PROJECT_ID.kb_analytics.execution_logs`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY endpoint;
```

### Looker Studio dashboard

Connect BigQuery to Looker Studio (free) to build dashboards:

1. Go to [Looker Studio](https://lookerstudio.google.com/)
2. Create Data Source -> BigQuery -> select `kb_analytics.execution_logs`
3. Build charts: requests over time, confidence distribution, token usage, cost trends, top topics

---

## Service 8: Cloud Logging and Monitoring

### What it does

Cloud Run automatically sends all stdout/stderr to Cloud Logging. Cloud Monitoring provides metrics, dashboards, and alerts.

### Setup (mostly automatic)

Cloud Run logs are captured automatically. To get structured logs instead of plain text, use Python's `logging` with JSON format:

```python
# api/main.py — add structured logging for production
import google.cloud.logging

if settings.ENVIRONMENT == "production":
    client = google.cloud.logging.Client()
    client.setup_logging()
```

### Alerts to create

```bash
# Alert: error rate > 5% over 5 minutes
gcloud monitoring policies create \
  --display-name="KB RAG High Error Rate" \
  --condition-display-name="Error rate > 5%" \
  --condition-filter='resource.type="cloud_run_revision" AND metric.type="run.googleapis.com/request_count" AND metric.labels.response_code_class="5xx"'

# Alert: latency p95 > 30 seconds
gcloud monitoring policies create \
  --display-name="KB RAG High Latency" \
  --condition-display-name="P95 latency > 30s" \
  --condition-filter='resource.type="cloud_run_revision" AND metric.type="run.googleapis.com/request_latencies"'
```

### Useful log queries

```
# View all errors
resource.type="cloud_run_revision"
resource.labels.service_name="kb-rag-system"
severity>=ERROR

# View LLM call logs
resource.type="cloud_run_revision"
resource.labels.service_name="kb-rag-system"
jsonPayload.message=~"Llamando GPT"

# View slow requests (> 10 seconds)
resource.type="cloud_run_revision"
httpRequest.latency>"10s"
```

---

## GCP APIs to Enable

Run these commands in your GCP project. Every API listed below must be enabled before using the corresponding service:

```bash
PROJECT_ID="your-project-id"

# Core (required)
gcloud services enable run.googleapis.com                   # Cloud Run
gcloud services enable artifactregistry.googleapis.com      # Artifact Registry
gcloud services enable secretmanager.googleapis.com         # Secret Manager
gcloud services enable cloudbuild.googleapis.com            # Cloud Build

# Storage and Data (required)
gcloud services enable storage.googleapis.com               # Cloud Storage
gcloud services enable firestore.googleapis.com             # Firestore
gcloud services enable bigquery.googleapis.com              # BigQuery

# Monitoring (recommended)
gcloud services enable logging.googleapis.com               # Cloud Logging (usually enabled by default)
gcloud services enable monitoring.googleapis.com            # Cloud Monitoring
gcloud services enable cloudtrace.googleapis.com            # Cloud Trace (request tracing)

# Networking (if using custom domain)
gcloud services enable domains.googleapis.com               # Cloud Domains
gcloud services enable certificatemanager.googleapis.com    # Certificate Manager

# Future (for Gemini hybrid, not needed now)
# gcloud services enable aiplatform.googleapis.com          # Vertex AI
```

---

## IAM and Service Accounts

### Cloud Run service account

Cloud Run uses a service account to access other GCP services. Create a dedicated one instead of using the default compute SA:

```bash
# Create service account
gcloud iam service-accounts create kb-rag-runner \
  --display-name="KB RAG System Runner"

SA_EMAIL="kb-rag-runner@${PROJECT_ID}.iam.gserviceaccount.com"

# Grant required roles
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/datastore.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectViewer"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/logging.logWriter"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/monitoring.metricWriter"

# Deploy Cloud Run with this service account
gcloud run deploy kb-rag-system \
  --service-account=${SA_EMAIL} \
  ...
```

### Cloud Build service account

Cloud Build needs permissions to push images and deploy to Cloud Run:

```bash
CLOUDBUILD_SA="$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/artifactregistry.writer"
```

---

## Implementation Order

### Phase 1: Foundation (Week 1)

1. Create GCP project (if not exists)
2. Enable all APIs (see [GCP APIs to Enable](#gcp-apis-to-enable))
3. Create service accounts and assign IAM roles
4. Create Artifact Registry repository
5. Create secrets in Secret Manager
6. Deploy to Cloud Run (manual first deploy)
7. Verify health check and basic functionality

### Phase 2: CI/CD (Week 1-2)

1. Create `cloudbuild.yaml`
2. Connect GitHub repo to Cloud Build
3. Push to main and verify auto-deploy
4. Test rollback flow

### Phase 3: Storage and Logging (Week 2)

1. Create Cloud Storage bucket for articles
2. Upload existing articles
3. Create `data_pipeline/storage.py` (ArticleStore)
4. Create Firestore database
5. Create `data_pipeline/execution_logger.py`
6. Integrate ExecutionLogger into API endpoints
7. Verify logs appear in Firestore

### Phase 4: Analytics (Week 3)

1. Set up Firestore -> BigQuery export
2. Create BigQuery dataset and views
3. Build Looker Studio dashboard
4. Set up Cloud Monitoring alerts
5. Create useful log-based metrics

### Phase 5: Optimization (Ongoing)

1. Tune Cloud Run settings (memory, concurrency, min instances)
2. Add Cloud CDN if caching makes sense
3. Set up budget alerts in GCP Billing
4. Review and optimize based on dashboard insights

---

## Code Changes Required

### Summary of files to create

| File | Purpose |
|------|---------|
| `data_pipeline/storage.py` | Cloud Storage article reader/writer |
| `data_pipeline/execution_logger.py` | Firestore execution logger |
| `cloudbuild.yaml` | CI/CD pipeline definition |

### Summary of files to modify

| File | Changes |
|------|---------|
| `api/config.py` | Add `GCP_PROJECT`, `GCS_BUCKET`, `ENABLE_EXECUTION_LOGGING` settings |
| `api/main.py` | Initialize `ExecutionLogger` in lifespan; add structured logging for production; integrate logger into endpoints |
| `Dockerfile` | Use `${PORT}` env var for Cloud Run compatibility |
| `requirements.txt` | Add `google-cloud-firestore`, `google-cloud-storage`, `google-cloud-logging` |

### Dependencies to add

```txt
# requirements.txt additions
google-cloud-firestore>=2.16.0
google-cloud-storage>=2.16.0
google-cloud-logging>=3.10.0
```

---

## Environment Variables Reference

### Production (Cloud Run + Secret Manager)

| Variable | Source | Value |
|----------|--------|-------|
| `API_KEY` | Secret Manager | `api-key:latest` |
| `OPENAI_API_KEY` | Secret Manager | `openai-api-key:latest` |
| `PINECONE_API_KEY` | Secret Manager | `pinecone-api-key:latest` |
| `ENVIRONMENT` | Env var | `production` |
| `LOG_LEVEL` | Env var | `INFO` |
| `GCP_PROJECT` | Env var | `your-project-id` |
| `GCS_BUCKET` | Env var | `your-project-id-kb-articles` |
| `ENABLE_EXECUTION_LOGGING` | Env var | `true` |
| `INDEX_NAME` | Env var | `kb-articles-production` |
| `NAMESPACE` | Env var | `kb_articles` |
| `OPENAI_MODEL` | Env var | `gpt-5.4` |
| `OPENAI_REASONING_EFFORT` | Env var | `medium` |
| `ALLOWED_ORIGINS` | Env var | `https://your-domain.com` |

### Local development

Keep using `.env` file as today. No changes needed for local dev.

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
