# Stage 0 — Context

## Today's endpoints

The KB RAG System has three endpoints (in [api/main.py](../kb-rag-system/api/main.py)):

| Endpoint | Purpose | When called by n8n |
|---|---|---|
| `POST /api/v1/required-data` | Identify what participant/plan fields must be collected | On inquiry detection |
| `POST /api/v1/generate-response` | Heavy 2-phase LLM (eligibility outcome + structured response) | After ForUsBots collects data |
| `POST /api/v1/knowledge-question` | Light single-LLM-call factual answer | UI / support lookups |

n8n unconditionally pushes participant inquiries through `required-data → ForUsBots → generate-response`. That heavy path is correct for eligibility decisions (hardship qualification, vested-balance questions) but is overkill for **punctual factual questions** the participant happens to ask.

## Failing case

Inquiry:
> "Hi there I was wondering how many business days til I can see it get approved. Thank you"

Was routed to `generate-response`, which returned:

- `decision = uncertain`
- `outcome = blocked_missing_data`
- `confidence = 0.558`
- five clarifying questions
- escalation flagged

The participant only wanted a number of business days. The KB has the timeframe; the heavy path's eligibility framing buried it under data-collection requirements that didn't apply.

## Fix

A new endpoint, `POST /api/v1/route-inquiry`, classifies an inquiry up front and tells n8n which downstream endpoint to call:

- punctual factual questions → `knowledge-question`
- eligibility-driven inquiries → `required-data → ForUsBots → generate-response`
- ambiguous inquiries → today's flow (no regression)

## Design decisions (locked)

1. **Three routes**: `knowledge_question` | `generate_response` | `needs_more_info`. The third preserves today's behavior on ambiguous inputs.
2. **Decision-only by default with optional `delegate=true`**: default returns the routing decision; with `delegate=true` the API calls the downstream in-process and wraps the result.
3. **Staged rollout**: shadow → knowledge-only → full, behind a `ROUTER_MODE` env flag.
4. **Hybrid classifier**: deterministic features (reusing `RAGEngine._detect_advisory_concepts` signals) + small LLM call. Fast-path on obvious cases.
5. **Confidence-based fallback**: if classifier confidence < 0.55, return `needs_more_info` (safe default).

## Files touched (across stages)

- [kb-rag-system/api/main.py](../kb-rag-system/api/main.py) — new endpoint
- [kb-rag-system/api/models.py](../kb-rag-system/api/models.py) — request/response models
- [kb-rag-system/api/config.py](../kb-rag-system/api/config.py) — env vars
- [kb-rag-system/data_pipeline/inquiry_router.py](../kb-rag-system/data_pipeline/inquiry_router.py) — new module
- [kb-rag-system/data_pipeline/rag_engine.py](../kb-rag-system/data_pipeline/rag_engine.py) — extract helper
- [kb-rag-system/data_pipeline/llm_router.py](../kb-rag-system/data_pipeline/llm_router.py) — task route + reasoning override
- [kb-rag-system/data_pipeline/prompts.py](../kb-rag-system/data_pipeline/prompts.py) — classifier prompts
- [kb-rag-system/tests/test_api.py](../kb-rag-system/tests/test_api.py) — endpoint tests
- [kb-rag-system/tests/test_inquiry_router.py](../kb-rag-system/tests/test_inquiry_router.py) — new unit tests
- [kb-rag-system/rag-testing/test_endpoints_stress.py](../kb-rag-system/rag-testing/test_endpoints_stress.py) — accuracy suite
