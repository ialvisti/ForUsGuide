# Stage 4 — Paginated Hydration, Trusted Correlation, and Evidence Broker

> **For Claude Opus 5:** Execute this stage after Stages 2 and 3. Read the master plan and the current finalization-base implementations before editing. Do not invent historical evidence.

**Goal:** Combine bounded DevRev pages with durable review state and defensible
RAG provenance, add privacy-preserving correlation for future verified
workloads, and expose existing logs only through a bounded read-only broker.

**Architecture:** A service layer hydrates a ticket plus one requested timeline
page, classifies entries using configured identities, caches bounded normalized
messages, and joins review/evidence summaries. Producer instrumentation stores
an HMAC ticket reference only when the caller is cryptographically verified.
A separate broker with read-only access to `(default)` returns an allowlisted
sanitized envelope to the console; the console never queries production logs
directly.

**Tech Stack:** Python 3.12, asyncio, Firestore repository, DevRev client, existing RAG/logging pipeline, pytest.

---

## Prerequisites

```bash
set -euo pipefail
export PLAN_ROOT="${PLAN_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide/tickets-development-plan}"
export TICKETS_BASE_SHA="${TICKETS_BASE_SHA:-eed9b34967c59b8bfec34026c9a8637581f2036a}"
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
test -r "$PLAN_ROOT/README.md"
test "$(git -C "$IMPL_ROOT" rev-parse --show-toplevel)" = "$IMPL_ROOT"
test -x "$PYTHON_BIN"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
git -C "$IMPL_ROOT" merge-base --is-ancestor "$TICKETS_BASE_SHA" HEAD
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_devrev_client.py \
  tests/test_ticket_review_repository.py -q
```

Read the current versions of:

- `data_pipeline/execution_logger.py`
- `data_pipeline/rag_engine.py`
- `data_pipeline/chunking.py`
- `api/ticket_worker.py`
- `api/main.py`
- `api/models.py`
- all tests that assert execution-log and public-response shapes

Do not rely on line numbers from an older branch.

## Files

Create:

- `kb-rag-system/data_pipeline/ticket_review_service.py`
- `kb-rag-system/data_pipeline/ticket_review_provenance.py`
- `kb-rag-system/data_pipeline/ticket_evidence_broker.py`
- `kb-rag-system/api/tickets_evidence_broker_main.py`
- `kb-rag-system/tests/test_ticket_review_service.py`
- `kb-rag-system/tests/test_ticket_review_provenance.py`
- `kb-rag-system/tests/test_ticket_evidence_broker.py`

Modify, as required by tested provenance:

- `kb-rag-system/data_pipeline/execution_logger.py`
- `kb-rag-system/data_pipeline/rag_engine.py`
- `kb-rag-system/api/ticket_worker.py`
- `kb-rag-system/api/main.py`
- `kb-rag-system/api/tickets_console_config.py`
- `kb-rag-system/api/ticket_review_models.py`
- `kb-rag-system/firestore.indexes.json`
- corresponding existing tests/fixtures

Do not modify `chunking.py` or the Pinecone vector-ID scheme. Existing
order-dependent IDs remain as observed identifiers; an ID v2/reindex is a
separate, approved project. Keep all producer modifications backward
compatible and narrow.

## Step 1 — Write failing hydration/classification tests

Cover:

1. One ticket detail call fetches `works.get` plus exactly one requested
   timeline page; following `next_cursor` is an explicit second call.
2. Each page preserves normalized source order and carries
   `next_cursor`, `truncated`, `partial`, and warnings. An explicitly loaded
   multi-page set may be chronologically sorted with stable ties.
3. Classification never depends on display-name string guesses alone.
4. Actor classes:
   - `participant`: explicit Rev user/external actor types;
   - `human_agent`: configured Dev user IDs not in AI/system sets;
   - `ai_or_system`: explicitly configured author IDs or documented system actor types;
   - `event`: change events;
   - `unknown`: everything else.
5. Configured author ID sets take precedence over ambiguous names.
6. Internal/private comments are labeled and never merged into the participant-facing reply.
7. Unknown body types render a safe placeholder plus metadata; no raw HTML is passed to the UI.
8. Bounded root-level `devrev_message_cache` upsert does not alter the durable
   human review.
9. DevRev `object_version` and modified timestamp prevent older cache data overwriting newer data.
10. Missing DevRev detail/timeline or a guard limit returns a typed partial
    result; it does not erase existing review fields or claim completeness.
11. Existing Firestore review fields overlay the live ticket without letting remote data overwrite rating/comments/status.
12. A live ticket with no review returns an explicit `review=None`, not a fake unreviewed document.
13. A historical ticket with no defensible identifiers returns `correlation_status="unavailable"`.
14. Timestamp/text similarity may return `candidate_links`, but never sets `linked` automatically.
15. Candidate output contains a short-lived signed token bound to ticket,
    review, actor, sanitized broker reference/digest, and expiry—not a
    caller-editable internal reference.
16. Evidence link creation revalidates that token plus the current broker
    result; nonexistent, cross-ticket/review, expired, tampered, and replayed
    tokens fail without IDOR leakage.
17. An explicit valid evidence link changes status to `manual` and records
    actor/reason/audit event.
18. Exact display-ID mode is mutually exclusive with list filters, calls
    scoped `works.get`, overlays at most one review summary, and returns a
    singleton page without a cursor; it never sends a nonexistent display-ID
    filter to `works.list`.

Run:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_service.py -q
```

Expected before implementation: missing service import.

## Step 2 — Implement hydration service

Required high-level interface:

```python
class TicketReviewService:
    async def list_live_tickets(
        self, query: DevRevTicketFilters
    ) -> CursorPage[DevRevTicketWithReviewSummary]: ...
    async def get_live_ticket_by_display_id(
        self, display_id: str, actor: ReviewerIdentity
    ) -> CursorPage[DevRevTicketWithReviewSummary]: ...
    async def get_ticket_detail(
        self, ticket_ref: str, actor: ReviewerIdentity
    ) -> TicketDetailEnvelope: ...
    async def get_timeline_page(
        self, ticket_ref: str, cursor: str | None
    ) -> TimelinePage: ...
    async def import_review(
        self, ticket_ref: str, actor: ReviewerIdentity,
        request_context: MutationContext,
    ) -> TicketReview: ...
    async def list_reviews(
        self, query: ReviewListQuery
    ) -> CursorPage[TicketReview]: ...
    async def patch_review(
        self, review_id: str, patch: ReviewPatch, expected_version: int,
        actor: ReviewerIdentity, request_context: MutationContext,
    ) -> TicketReview: ...
```

Rules:

- Live DevRev and Firestore calls that are independent may run concurrently with `asyncio.gather`, but partial failure must be explicit.
- Never let a failed cache write fail an otherwise valid read; log a sanitized warning and mark cache state degraded.
- Never let a failed DevRev fetch overwrite durable human review data.
- Bound concurrent `works.get` hydration; do not fan out unbounded requests from a list page.
- Cache keys and logs use hashes, not ticket text or raw cursors.
- The list endpoint overlays only bounded review summaries. It does not fetch
  timelines per row.
- Detail never silently loops through an unbounded timeline. The browser
  explicitly follows returned cursors.

## Step 3 — Freeze message classification configuration

Add settings/models for:

```text
TICKETS_DEVREV_AI_AUTHOR_IDS
TICKETS_DEVREV_SYSTEM_AUTHOR_IDS
TICKETS_DEVREV_HUMAN_AUTHOR_IDS (optional allowlist/override)
```

These values are IDs/DONs, not names. Empty AI/system configuration must result in `unknown` where identity is ambiguous, with an operator warning in session diagnostics.

Do not hardcode organization-specific IDs in source or tests.

## Step 4 — Write failing provenance tests

Cover new executions and backward compatibility:

1. A raw DevRev header/body field from an unverified caller can produce only a
   `candidate` and never `linked`.
2. Existing Cloud Run IAM plus a valid replay-protected n8n signature over
   timestamp/normalized DON/request digest/existing idempotency-key hash produces
   `HMAC-SHA256(lookup_secret, normalized DON)` plus `correlation_source`,
   configured workload-binding hash, ingress key version, lookup key version,
   and
   `correlation_trust=verified_workload`. Ingress and lookup keys are distinct.
3. No raw DON/display/timeline ID enters execution logs, error text, or INFO
   logs. Existing `request_id_hash` privacy behavior remains unchanged.
4. When correlation context is absent, existing endpoint behavior and response
   schemas are unchanged.
5. Ticket-handler jobs created server-side may use their validated internal job
   ID plus the same HMAC external reference.
6. Execution records include:
   - schema version;
   - HMAC ticket reference, source/trust, principal hash when verified;
   - internal job ID plus existing request/trace hashes;
   - endpoint/route;
   - model/provider, static `prompt_template_id`,
     `prompt_template_sha256`, config version, and optional rendered-prompt
     trace hash that is never treated as a semantic version;
   - deployed revision/commit if available;
   - Pinecone index + namespace;
   - source article IDs;
   - observed current vector ID + score + article ID + content SHA-256 +
     ordinal + chunk type/tier;
   - normalized response hash;
   - timings/errors;
   - explicit missing-provenance flags.
7. Execution records exclude:
   - API keys/tokens;
   - full ticket body;
   - participant email/name;
   - full generated response;
   - full chunk text;
   - scraped participant data.
8. Chunk references are bounded but do not claim IDs are stable across
   reindexing; no chunk-ID generation code changes.
9. Existing legacy log documents without the new fields parse as
   schema v0/unknown.
10. `used_chunks` remains excluded from public poll responses while sanitized
    references remain private.
11. Logging failure never changes the participant-facing API result.
12. Existing Firestore TTL fields and runtime-safety tests remain intact.
13. Broker tests prove: only versioned HMAC indexed lookup, bounded active
    keyring fan-out, maximum result count, allowlisted fields, no raw text/PII,
    legacy unavailable response, and caller authorization dependency at the
    app boundary.
14. Producer current-key rotation and mixed historical
    `(lookup_key_version, ticket_lookup_hmac)` lookup work without a raw DON;
    unknown/disabled versions fail explicitly and old keys cannot be retired
    before the evidence-retention contract permits it.

Run and observe the intended failures:

```bash
"$PYTHON_BIN" -m pytest tests/test_ticket_review_provenance.py \
  tests/test_ticket_evidence_broker.py -q
```

## Step 5 — Implement provenance without changing the public contract

Create pure helpers in `ticket_review_provenance.py`:

- normalize text before hashing (Unicode normalization, whitespace normalization, no case-fold if it changes meaning);
- SHA-256 response hash;
- verify the separate n8n ingress signature with constant-time comparison,
  ±5-minute timestamp window, and the existing durable idempotency receipt;
- HMAC-SHA256 lookup reference from normalized DON with a separate versioned
  lookup key; never log either key/DON;
- static prompt-template artifact ID/SHA-256 and config/model version;
- optional rendered-prompt trace hash, labeled non-semantic;
- sanitize/bound chunk references;
- derive deployment metadata from safe runtime variables such as `K_REVISION` and an explicit commit/image variable when configured;
- parse both new and legacy execution documents.

Do not claim the Pinecone index contents were at a specific commit unless an explicit index/content version is available. Use `index_version=null` plus a missing flag.

Extend `ExecutionLogger` with a versioned, additive schema. Preserve its “logging failures do not break API responses” property, but emit a structured metric for failed writes.

Where the active legacy n8n flow cannot supply an authenticated correlation
context, document and return `unavailable`. Do not correlate merely because
two texts look similar or a client supplied a header.

## Step 6 — Add the bounded evidence broker

`tickets_evidence_broker_main.py` is a separate FastAPI app/lifecycle. It
initializes only settings, a read-only Firestore client for `(default)`, and
`TicketEvidenceBroker`. It must not import `api.main`, `rag_engine`, Pinecone,
OpenAI, DevRev, ticket worker, or the console repository.

The broker receives a bounded raw DON only from the authenticated console
service, computes the HMAC in memory, queries only indexed allowlisted sources
(`execution_logs`, `ticket_executions`, `ticket_jobs` where defensible), and
returns `RagEvidenceEnvelope`. It never returns raw prompts, responses, chunk
text, participant data, arbitrary log fields, or an unbounded list.

Queries must be index-backed and bounded. Do not scan a whole collection to find a ticket.

If necessary, add canonical indexes to `firestore.indexes.json` and corresponding repository tests. Terraform implementation remains Stage 10.

## External Contract Gate — active n8n correlation

Create
`kb-rag-system/Development Docs/TICKETS_N8N_CORRELATION_CONTRACT.md` with:

- exact request field carrying the DevRev DON;
- existing Cloud Run invoker boundary plus separate ingress-signature header,
  canonical timestamp/DON/request/idempotency-hash bytes, key version, and
  constant-time verification;
- normalization/HMAC version;
- no raw identifier logging;
- response/timeline-entry correlation responsibility;
- fixture request/response contract;
- owner, rollout, rollback, and proof required.

Do not edit or deploy the external n8n flow in this stage. Future automatic
`linked` status remains disabled until the n8n owner supplies an approved
workflow revision and staging evidence. Optional/unverified headers never
satisfy the gate.

## Step 7 — Regression verification

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_service.py \
  tests/test_ticket_review_provenance.py \
  tests/test_ticket_evidence_broker.py -q
"$PYTHON_BIN" -m pytest tests/test_api.py \
  tests/test_ticket_worker.py \
  tests/test_ticket_job_repository.py \
  tests/test_ticket_security.py \
  tests/test_ticket_runtime_safety.py -q
"$PYTHON_BIN" -m compileall -q api data_pipeline
"$PYTHON_BIN" -m json.tool firestore.indexes.json >/dev/null
git -C "$IMPL_ROOT" diff --check
```

Run a privacy scan over changed Python/tests:

```bash
privacy_matches="$(
  git -C "$IMPL_ROOT" diff --unified=0 |
    rg -n -i 'ticket_body|email_body|collected_data|authorization|api[_-]?key|token' ||
    true
)"
printf '%s\n' "$privacy_matches"
```

Manually review every match and prove no new log/persistence path stores prohibited payloads.

## Definition of Done

- DevRev detail/timeline hydration is bounded and explicit about partial failures.
- Message classification uses identities/types, not display-name guesses.
- New executions support verified-workload HMAC correlation; automatic linking
  remains disabled until the external n8n contract gate is proven.
- No raw external identifier, participant content, or redefined vector ID was
  introduced.
- Evidence reads use the separate bounded broker; the console has no
  `(default)` database access.
- Historical gaps are rendered as gaps, never inferred as fact.
- Public RAG/ticket API schemas and behavior remain backward compatible.
- No live external mutation occurred.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/data_pipeline/ticket_review_service.py \
  kb-rag-system/data_pipeline/ticket_review_provenance.py \
  kb-rag-system/data_pipeline/ticket_evidence_broker.py \
  kb-rag-system/data_pipeline/execution_logger.py \
  kb-rag-system/data_pipeline/rag_engine.py \
  kb-rag-system/api/tickets_evidence_broker_main.py \
  kb-rag-system/api/ticket_worker.py \
  kb-rag-system/api/main.py \
  kb-rag-system/api/tickets_console_config.py \
  kb-rag-system/api/ticket_review_models.py \
  kb-rag-system/tests/test_ticket_review_service.py \
  kb-rag-system/tests/test_ticket_review_provenance.py \
  kb-rag-system/tests/test_ticket_evidence_broker.py \
  kb-rag-system/tests/test_api.py \
  kb-rag-system/tests/test_ticket_worker.py \
  kb-rag-system/tests/test_ticket_job_repository.py \
  kb-rag-system/tests/test_ticket_security.py \
  kb-rag-system/tests/test_ticket_runtime_safety.py \
  kb-rag-system/firestore.indexes.json \
  "kb-rag-system/Development Docs/TICKETS_N8N_CORRELATION_CONTRACT.md"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/data_pipeline/ticket_review_service.py \
  --allow kb-rag-system/data_pipeline/ticket_review_provenance.py \
  --allow kb-rag-system/data_pipeline/ticket_evidence_broker.py \
  --allow kb-rag-system/data_pipeline/execution_logger.py \
  --allow kb-rag-system/data_pipeline/rag_engine.py \
  --allow kb-rag-system/api/tickets_evidence_broker_main.py \
  --allow kb-rag-system/api/ticket_worker.py \
  --allow kb-rag-system/api/main.py \
  --allow kb-rag-system/api/tickets_console_config.py \
  --allow kb-rag-system/api/ticket_review_models.py \
  --allow kb-rag-system/tests/test_ticket_review_service.py \
  --allow kb-rag-system/tests/test_ticket_review_provenance.py \
  --allow kb-rag-system/tests/test_ticket_evidence_broker.py \
  --allow kb-rag-system/tests/test_api.py \
  --allow kb-rag-system/tests/test_ticket_worker.py \
  --allow kb-rag-system/tests/test_ticket_job_repository.py \
  --allow kb-rag-system/tests/test_ticket_security.py \
  --allow kb-rag-system/tests/test_ticket_runtime_safety.py \
  --allow kb-rag-system/firestore.indexes.json \
  --allow "kb-rag-system/Development Docs/TICKETS_N8N_CORRELATION_CONTRACT.md"
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "feat(tickets): add trusted provenance evidence boundary"
```

Proceed to Stage 5 only after the staged-scope helper and complete diff pass.
