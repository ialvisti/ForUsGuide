# FUA Knowledge Base — Cloud Infrastructure Blueprint
## Companion Document to `INFRASTRUCTURE_DIAGRAM.html`

This document is an exhaustive, section-by-section walkthrough of the interactive infrastructure diagram located at [INFRASTRUCTURE_DIAGRAM.html](./INFRASTRUCTURE_DIAGRAM.html). Every box, badge, pill, arrow, and code fragment in the diagram is described here, so the diagram can be read and understood without ambiguity even by someone who has never seen the system.

---

## 0. Purpose & Scope

The diagram visualizes the complete cloud architecture of the **FUA Knowledge Base RAG system** — a Retrieval-Augmented Generation service that automates the resolution of 401(k) support tickets at ForUsAll. The diagram shows:

- **Who triggers the workflow** (the participant and DevRev CRM).
- **Who orchestrates it** (n8n + ForUsBots RPA).
- **What does the reasoning** (the KB RAG API on Cloud Run, Pinecone, and the LLMs).
- **What supports it operationally** (the nine GCP managed services that provide storage, analytics, secrets, CI/CD, observability, and identity).
- **How code reaches production** (the six-step CI/CD pipeline).
- **How a ticket flows end-to-end** (nine stages from inquiry submission to ticket closure).

The diagram is deliberately opinionated about colors and grouping; this document explains the reasoning behind each visual decision so that future maintainers of the document can keep it consistent as the system evolves.

---

## 1. Header Block

The header establishes the identity of the system and the coordinates of the production deployment.

| Element | Value | Meaning |
|---|---|---|
| **Eyebrow tag** | `ForUsAll · Engineering · Cloud Architecture` | Audience and domain. |
| **Title** | `FUA Knowledge Base — Cloud Infrastructure Blueprint` | The canonical name of the diagram. |
| **Subtitle** | One-paragraph summary of the system's role | Acts as the elevator pitch. |

### Meta strip (four cards directly under the header)

| Card | Value | Why it matters |
|---|---|---|
| **GCP Project** | `rag-kb-system` | The only GCP project this system lives in. All resources are scoped here. |
| **Region** | `us-central1` | Single-region deployment. All Cloud Run, Artifact Registry, and Cloud Storage artifacts are colocated. |
| **Organization** | `forusall.com` | Determines the IAM organization policies that apply (for instance, the one that forbids `allUsers` on Cloud Run). |
| **Production Endpoint** | `kb-rag-system-900340137010.us-central1.run.app` | Canonical URL that n8n and the DevRev AI Agent call. |

---

## 2. Legend

The legend is a horizontal strip of six colored pills; each color is used consistently across the rest of the diagram.

| Color | Meaning |
|---|---|
| **Blue (`#4285F4`)** | Google Cloud Platform — the primary cloud provider. Used for all GCP-native services. |
| **Orange (`#FF9900`)** | AWS — the hosting provider for n8n. |
| **Teal (`#46E2C2`)** | Render — the hosting provider for ForUsBots. |
| **Purple (`#a855f7`)** | Third-party SaaS — platforms that are external to both GCP and AWS (for example DevRev and Pinecone). |
| **Green (`#10a37f`)** | LLM providers (OpenAI, Gemini/Vertex AI). |
| **Indigo (`#4f46e5`)** | Vector Database. |
| **Bright green (`#22c55e`)** | CI/CD pipeline (GitHub → Cloud Build → Cloud Run). |

Every card elsewhere in the diagram carries a **pill** (a small colored capsule) at the bottom that references these categories. Pills are not purely decorative: they answer the question "where does this service live?" at a glance.

---

## 3. Layer 01 — Participant & CRM

This is the top of the diagram. It shows the humans and the CRM that initiate and terminate the workflow.

### 3.1 401(k) Participant
- **Icon**: gradient silhouette (blue → purple).
- **Role**: External end-user. Submits retirement-plan inquiries via email or participant portal.
- **Why they are in the diagram**: Every workflow run starts with exactly one participant message landing in DevRev. There are no other entry points.

### 3.2 DevRev (CRM ticketing platform)
- **Icon**: the stylized orange "D" arrow that is DevRev's official mark.
- **Role**: Hosts the ticket that the participant has submitted. When the ticket is created, a webhook fires and sends the ticket payload to n8n.
- **Pill**: `SaaS Platform` (external, purple).
- **Key behaviors**:
  - Produces the initial webhook that triggers the entire pipeline.
  - Receives the final reply + internal notes from n8n (Step 9).
  - Owns the ticket lifecycle (open → solved / escalated).

### 3.3 DevRev AI Agent
- **Icon**: orange radial starburst — a visual distinction from the base DevRev icon so the two can be told apart at a glance.
- **Role**: The LLM-powered agent inside DevRev that reads the `kb_bundle_v1` from n8n and produces the participant-facing reply together with the ticket-stage decision and internal notes.
- **Pill**: `AI Agent`.
- **Important nuance**: The AI Agent does **not** write directly to the ticket; it instead posts a webhook back to n8n (Step 8), which then performs the update in Step 9. That indirection is what allows n8n to enforce guardrails (never exposing internal system data, choosing the correct ticket stage) before the reply reaches the participant.

**Connector below this layer:** "Webhook POST · ticket payload" — indicates the transport from DevRev to n8n.

---

## 4. Layer 02 — Orchestration

This is the layer where the actual business workflow lives. The two cards here are hosted on **different** providers, which is why the layer tag reads `AWS + Render`.

### 4.1 n8n Workflow Engine
- **Icon**: the official pink n8n node-graph mark (four circles connected by diagonal lines).
- **Host**: AWS EC2.
- **Pill**: `AWS EC2` (orange).
- **Responsibilities** (the card lists six explicitly):
  1. **AI-driven inquiry decomposition** — splits a multi-topic ticket into individual inquiries.
  2. **Calls `/required-data`** — one call per inquiry to discover what participant data is needed.
  3. **Invokes ForUsBots RPA** — passes the merged, deduplicated field list to the scrapers.
  4. **Calls `/generate-response`** — one call per inquiry once the data is collected.
  5. **Merges payloads into `kb_bundle_v1`** — the consolidated artifact passed to the DevRev AI Agent.
  6. **Final injection & ticket closure** — receives the DevRev AI callback and writes to DevRev.

### 4.2 ForUsBots (RPA)
- **Icon**: stylized blue robot with a cyan antenna and two white eyes.
- **Host**: **Render** (not AWS — this was corrected from an earlier version of the diagram).
- **Pill**: `Render · Internal RPA` (teal).
- **Role**: Headless browser automation that logs into record-keeper portals and scrapes the requested fields (balance, vesting, loan status, eligibility, etc.). Given a list of fields, it returns their values.
- **Why it matters**: Without ForUsBots, the RAG engine could not ground its answers in the participant's real-time account state; the entire `/required-data` → scrape → `/generate-response` loop exists because the LLM alone cannot know balances or loan eligibility.

**Connector below this layer:** "HTTPS · X-API-Key · Identity Token (OAuth2)" — describes both authentication mechanisms in use against Cloud Run: the `X-API-Key` header for application-level auth, and the IAM Identity Token for Cloud Run ingress auth.

---

## 5. Layer 03 — KB RAG API (System Core)

This layer has a distinctive blue glow because it is the system's core — everything else is plumbing around it.

### 5.1 Cloud Run
- **Icon**: the GCP Cloud Run hexagon with the central play/arrow glyph.
- **Service name**: `kb-rag-system`.
- **Resource profile**: 512 MiB memory, 1 vCPU, 0–5 instance autoscaling, IAM-protected ingress.
- **Why 0 min-instances**: organization policy cost optimization — the service scales to zero when idle and cold-starts on demand.
- **Pill**: `Serverless · us-central1`.

### 5.2 FastAPI
- **Icon**: teal circular logo with the FastAPI lightning bolt.
- **Role**: The Python web framework running inside the Cloud Run container.
- **Tech pill**: `python 3.12`.
- **Responsibilities**: request routing, Pydantic validation, OpenAPI schema generation, middleware (auth, logging, error mapping).

### 5.3 Docker
- **Icon**: the Docker whale with container blocks on its back.
- **Role**: Packages the FastAPI app + the entire `data_pipeline/` package + Python dependencies into a single image.
- **Tech pill**: `python:3.12-slim` — chosen as the base image for its small size and predictable security posture.

### 5.4 RAG Engine
- **Icon**: the official Python double-snake logo (blue and yellow).
- **Role**: The core Python pipeline implementing retrieval-augmented generation.
- **Tech pill**: `rag_engine.py · 103 KB` — indicates that the core logic lives in a single large module.
- **Responsibilities**:
  - **Semantic search** — embeds the inquiry and hits Pinecone.
  - **Reranking** — re-scores the top-K chunks using a cross-encoder-like strategy to pick the most relevant ones.
  - **Token budgeting** — ensures the final prompt fits within the LLM's context window while preserving critical chunks.
  - **Multi-tier chunk selection** — applies the CRITICAL / HIGH / MEDIUM / LOW hierarchy.
  - **Structured response generation** — calls the LLM and parses the outcome-driven JSON output.

### 5.5 The three REST endpoints (card strip under the core services)

The endpoint cards each have a different left-border color so that they are easy to distinguish.

| Endpoint | Purpose | Output highlights |
|---|---|---|
| **`POST /api/v1/required-data`** | Given an inquiry + topic + record-keeper, returns the list of participant fields needed to resolve it. | List of fields (plus metadata used by n8n to map them to ForUsBots endpoints). |
| **`POST /api/v1/generate-response`** | Given an inquiry + collected data, returns the outcome-driven response. | `can_proceed` / `blocked_not_eligible` / `blocked_missing_data` / `ambiguous_plan_rules`, with source citations. |
| **`POST /api/v1/knowledge-question`** | Generic Q&A over the knowledge base (no participant data needed). | Answer + confidence + coverage metrics. |

**Connector below this layer:** "Semantic retrieval + LLM generation" — introduces Layer 04.

---

## 6. Layer 04 — Intelligence (Hybrid LLM Routing & Vector Search)

This layer explains why the system can answer at all: it is the set of AI providers and the vector database.

### 6.1 OpenAI
- **Icon**: the green knot spiral that OpenAI uses as its mark.
- **Role**: Primary reasoning model for outcome decisions and structured response bodies.
- **Tech pill**: `gpt-5.5 · gpt-4o-mini`.
- **When it is used**: specifically for the `gr_outcome` routing lane, where the system needs its strongest reasoning.

### 6.2 Google Gemini
- **Icon**: the four-point gradient star (Google blue → purple → pink → red).
- **Role**: Default router target for the other lanes (decomposition, knowledge, response body).
- **Tech pill**: `gemini-2.5-flash`.
- **Why default**: materially cheaper and faster than GPT-5.5 for equivalent routing lanes — this is the source of the hybrid-routing cost reduction documented in commit `2fe1735`.

### 6.3 Vertex AI
- **Icon**: a blue diamond with a central node and four satellite connection points — GCP-style.
- **Role**: The GCP-native way to access Gemini. Avoids storing a Google API key in Secret Manager; uses Application Default Credentials instead.
- **Pill**: `GCP · ADC`.
- **Why it exists as a separate card**: the LLM router can target Gemini either via the public Google AI API (locally) or via Vertex AI (in production). The choice is controlled by environment variables.

### 6.4 Pinecone
- **Icon**: stacked geometric pine-cone segments (white on dark tile) — inspired by the company's mark.
- **Role**: Serverless vector database storing the KB articles as embeddings.
- **Key configuration**:
  - Namespace: `kb_articles`.
  - Dimension: 1024.
  - Embedding model: `llama-text-embed-v2` (integrated into Pinecone, so no separate OpenAI/Google embedding call is needed).
  - Indexed vectors: ~547 at the time of the diagram.
- **Pill**: `Serverless Vector DB` (green highlight because it's mission-critical — no Pinecone, no retrieval, no system).

### 6.5 Multi-Tier Chunks
- **Icon**: a mosaic of colored blocks (red/amber/green/blue/purple/cyan) representing the different tiers.
- **Role**: Not a service but a **strategy** — the way the KB articles are chunked and prioritized before retrieval.
- **Tech pill**: `~8,400 chunks`.
- **How it works**:
  - **CRITICAL** chunks are always included if matched (never dropped by the token-budget pass).
  - **HIGH** chunks are included if there is budget.
  - **MEDIUM** chunks fill the remaining room.
  - **LOW** chunks are the last to be considered, generally as fallback.
- **Why it is shown as a separate card**: because the retrieval quality of the system is largely determined by this tiering, not by the LLM. Making it visible as its own box forces readers to notice it.

**Connector below this layer:** "Managed persistence, observability & security".

---

## 7. Layer 05 — Google Cloud Managed Services

The widest layer in the diagram — nine cards, one for each GCP-native service the system uses. These are grouped here because they all share the same identity, billing, IAM, and region.

### 7.1 Cloud Storage
- **Icon**: yellow/amber hexagon with three horizontal dashes (content indicator).
- **Bucket**: `rag-kb-system-kb-articles` with versioning enabled.
- **Role**: Source of truth for the raw KB JSON files. The data pipeline reads from here; the pipeline uploads from here to Pinecone.
- **Pill**: `Object Store`.

### 7.2 Firestore
- **Icon**: yellow/orange hexagon with three horizontal bars of descending width.
- **Mode**: Native.
- **Collection**: `execution_logs`.
- **Role**: Stores an execution trace for every request for debugging and post-hoc analysis (optional — toggled by config).
- **Pill**: `NoSQL`.

### 7.3 BigQuery
- **Icon**: blue circle with the BigQuery magnifying-glass motif and bar-chart.
- **Dataset**: `kb_analytics`.
- **Role**: Long-term analytics warehouse. Retrieval quality, latency, token usage, and LLM routing decisions are aggregated here for dashboards and regressions.
- **Pill**: `Data Warehouse`.

### 7.4 Secret Manager
- **Icon**: green shield with a closed padlock — styled as a GCP hexagon.
- **Secrets held**: `api-key`, `openai-api-key`, `pinecone-api-key`.
- **Role**: Supplies credentials to Cloud Run at runtime via `--set-secrets`. No secrets are baked into the Docker image.
- **Pill**: `Secrets` (highlighted green).

### 7.5 Cloud Logging
- **Icon**: blue rounded square with five descending log-line bars of decreasing opacity.
- **Role**: Auto-ingests `stdout` from Cloud Run. Logs are structured JSON with correlation IDs so that a single request can be traced across the request lifecycle.
- **Pill**: `Observability`.

### 7.6 Cloud Monitoring
- **Icon**: red circle with a line chart and three data points.
- **Alerts configured**:
  1. 5xx error rate > 5 per interval.
  2. Request latency > 30 s.
  3. Uptime check on `/health`.
- **Pill**: `Alerting`.

### 7.7 Artifact Registry
- **Icon**: green folder/pedestal with three horizontal lines.
- **Repository**: `kb-rag/kb-rag-system`.
- **Role**: Docker image registry. Cloud Build pushes images here; Cloud Run pulls from here.
- **Tagging strategy**: each image is tagged with both `$SHORT_SHA` (for rollback) and `latest` (for convenience).
- **Pill**: `Docker Registry`.

### 7.8 Cloud Build
- **Icon**: yellow rounded square with four connected circles — styled after the Cloud Build build-graph motif.
- **Role**: CI/CD executor. Reads `cloudbuild.yaml` and runs the three-step pipeline (build → push → deploy) whenever a commit lands on `main`.
- **Pill**: `CI/CD`.

### 7.9 IAM & Service Accounts
- **Icon**: blue card with a central user silhouette and two sidebar security pins.
- **Service account**: `kb-rag-runner@rag-kb-system.iam`, granted the minimum set of roles required (Secret Manager accessor, Pinecone/OpenAI are external so not represented in IAM).
- **Role**: Enforces least-privilege and handles the OAuth2 token exchange that n8n uses to call Cloud Run.
- **Pill**: `Identity`.

---

## 8. Continuous Delivery Pipeline

A six-card horizontal strip showing the end-to-end deployment flow. Each card has a numbered colored badge and a one-line description. The flow runs in under four minutes end-to-end.

| # | Stage | What happens |
|---|---|---|
| 1 | **GitHub** | Developer pushes to the `main` branch of `ialvist/ForUsGuide`. |
| 2 | **Cloud Build Trigger** | A Cloud Build trigger watching the branch detects the push, reads `cloudbuild.yaml`, and enqueues a build. |
| 3 | **Docker Build** | A Cloud Build step executes `docker build` using `python:3.12-slim` as the base, installing everything from `requirements.txt` and copying the source tree. |
| 4 | **Push Image** | The freshly built image is pushed to Artifact Registry with two tags: `$SHORT_SHA` and `latest`. |
| 5 | **Deploy Cloud Run** | `gcloud run deploy` performs a rolling update against the `kb-rag-system` Cloud Run service, injecting secrets from Secret Manager. |
| 6 | **Health Check** | The new revision is only promoted if `GET /health` succeeds — that endpoint validates the Pinecone connection and LLM configuration. |

The numbered badges use a different color per step (white, GCP yellow, Docker blue, GCP green, GCP blue, bright green) to reinforce which provider or tool is responsible for each stage.

---

## 9. End-to-End Ticket Resolution — 9 Stages

This is the central "data flow" of the diagram: nine numbered cards arranged horizontally on a dedicated dark panel. Each card's numbered badge takes the color of the provider handling that step, so the eye can trace hand-offs between providers.

| # | Stage | Badge color | Detail |
|---|---|---|---|
| 1 | **DevRev Ticket** | DevRev orange | Participant raises an inquiry. The ticket is created and fires the creation webhook to n8n. |
| 2 | **n8n Decomposition** | n8n pink | An AI node inside the n8n workflow splits the ticket into discrete inquiries and tags each with a topic. |
| 3 | **Required Data** | GCP blue | For each inquiry, n8n calls `POST /api/v1/required-data` on the KB RAG API to learn which participant fields are needed. |
| 4 | **ForUsBots RPA** | Cyan | n8n dispatches the merged, deduplicated field list to ForUsBots (on Render), which scrapes the record-keeper portal and returns the actual values. |
| 5 | **Pinecone Search** | Indigo | For each inquiry, the RAG engine performs a filtered semantic search against Pinecone (filters: `record_keeper`, `plan_type`, `topic`). |
| 6 | **LLM Routing** | OpenAI green | The hybrid router dispatches the generation request to Gemini or OpenAI depending on the routing lane (`gr_outcome` → OpenAI, others → Gemini). The result is a structured, outcome-driven response. |
| 7 | **`kb_bundle_v1`** | Purple | n8n merges the per-inquiry responses into a single consolidated bundle and posts it to the DevRev AI Agent. |
| 8 | **DevRev AI → n8n** | DevRev red | The DevRev AI Agent crafts the participant-facing reply, decides the ticket stage, writes internal notes, and then **calls back** to n8n at `https://n8nhooks.forusall.com/webhook/final-handling`. |
| 9 | **Ticket Injected & Closed** | Green | n8n takes the payload from Step 8 and (a) writes `participant_reply` and `internal_notes` to the DevRev ticket, and (b) sets its stage (solved/escalated) based on `set_stage_solved`. The ticket loop is now closed. |

### 9.1 Step 8 webhook payload (the boxed JSON)

A dedicated dark panel below the nine-card strip shows the exact HTTP payload that Step 8 produces. It is both a reference and a contract: changes to any field in this object break the whole flow.

```json
{
  "agentResponse": "{
    \"participant_reply\": \"Hi Selecia, thank you for reaching out about your hardship withdrawal...\",
    \"set_stage_solved\": false,
    \"stage_reason\": \"ResponseSource is Knowledge-Question, live status cannot be determined — escalation required.\",
    \"internal_notes\": \"Participant inquired about status of hardship withdrawal. Provided general info & advised Support.\"
  }",
  "ticketId": "TKT-874561"
}
```

Observations about this payload:

- **`agentResponse` is a JSON-encoded string, not a nested object.** DevRev's AI output is serialized as a string before being wrapped in the outer envelope. n8n parses it in Step 9.
- **`participant_reply`** — the visible text that will be added to the ticket. Written in the first person, signed by the ForUsAll Team.
- **`set_stage_solved`** — boolean. `true` only when the AI is confident the ticket is fully resolved. `false` means n8n must mark the ticket as escalated / pending human review rather than solved.
- **`stage_reason`** — free text describing why the AI made its stage decision. Used for internal diagnostics only; never surfaced to the participant.
- **`internal_notes`** — a short, structured log entry attached to the ticket for agents to read later.
- **`ticketId`** — used by n8n to know which DevRev ticket to update. Coming from the outer envelope is intentional (avoids depending on the AI to echo it correctly).

---

## 10. Complete Technology Stack

A ten-card grid summarizing every technology touching the system, grouped by category. Each card uses the color of the most iconic technology in its category.

| Category | Color | Technologies |
|---|---|---|
| **Compute** | Accent blue | Python 3.12 · FastAPI · Uvicorn · Docker · Cloud Run |
| **Storage & Data** | GCP green | Cloud Storage · Firestore · BigQuery |
| **Vector DB** | Indigo | Pinecone serverless · `llama-text-embed-v2` · 1024-dim |
| **Language Models** | OpenAI green | OpenAI GPT-5.5 · Gemini 2.5 Flash · Vertex AI |
| **CRM** | DevRev orange | DevRev · DevRev AI Agent · webhooks |
| **Orchestration** | n8n pink | n8n on AWS · ForUsBots RPA on Render |
| **CI/CD** | GCP yellow | GitHub · Cloud Build · Artifact Registry |
| **Security** | Green | IAM · Secret Manager · OAuth2 · X-API-Key |
| **Observability** | Red | Cloud Logging · Cloud Monitoring · Alerts |
| **Testing** | Purple | `pytest` · `httpx` · stress tests · 280 KB articles |

---

## 11. Visual Language Notes

These are the implicit rules the diagram follows. Understanding them helps keep the diagram consistent when new services or flows are added.

1. **Layers are numbered.** The `01 … 05` badges on the left of each layer header establish a strict top-down reading order that mirrors the request path (user → orchestration → core → AI → managed services).
2. **Every card has the same anatomy**: icon (branded) → name → one-sentence role → optional technical sub-line → provider pill. This predictability lets readers skim.
3. **Color == provider.** Blue for GCP, orange for AWS, teal for Render, purple for third-party SaaS, green for AI providers. A card's pill color always matches the provider hosting the service.
4. **Icons are real brand marks**, hand-drawn as inline SVG. Nothing is a generic shape.
5. **Highlighted cards** (green outline + glow) mark single points of failure: Pinecone and Secret Manager. If either is down, the system cannot serve traffic.
6. **Connectors are labeled.** The line between two layers always carries a one-line label describing what crosses it (e.g. `Webhook POST · ticket payload`, `HTTPS · X-API-Key · Identity Token`).
7. **The nine-step flow uses provider colors on its numbered badges** so that hand-offs are visible without reading the text.
8. **The webhook payload is shown verbatim**, not redrawn as boxes, because it is the only artifact in the flow whose exact shape is a production contract.

---

## 12. Responsiveness Behavior

The HTML is fully responsive; the layout reflows at these breakpoints:

| Breakpoint | Behavior |
|---|---|
| ≥ 1401 px | Nine-step flow shown as a single row of 9 cards with arrows between them. |
| ≤ 1400 px | Flow collapses to 5 columns; arrows are hidden (would point at the wrong cards). |
| ≤ 1200 px | CI/CD strip collapses from 6 to 3 columns. |
| ≤ 1024 px | Webhook header stacks vertically. |
| ≤ 900 px | Flow collapses to 3 columns. |
| ≤ 720 px | All service cards take the full width (one per row). |
| ≤ 560 px | Flow collapses to 2 columns; CI/CD strip collapses to 2 columns. |

Padding, font sizes, and header scaling use `clamp()` so there are no abrupt jumps between breakpoints.

---

## 13. Quick Mental Model (for newcomers)

If you only need to remember one thing, remember this: **the system is a two-call RAG loop orchestrated by n8n and closed by a DevRev AI callback.**

1. DevRev → n8n (ticket arrives).
2. n8n ↔ KB RAG API (`/required-data`) ↔ ForUsBots (gather facts).
3. n8n ↔ KB RAG API (`/generate-response`) (decide what to say).
4. n8n → DevRev AI (craft the human reply).
5. DevRev AI → n8n (Step 8, the callback).
6. n8n → DevRev (Step 9, close the ticket).

Everything else on the diagram — Pinecone, the LLMs, the nine GCP managed services, the CI/CD pipeline — exists to support that loop.
