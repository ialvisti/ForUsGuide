# Stage 6 — Rollout

## Goal

Move production traffic to the router in three phases, gated by `ROUTER_MODE`. This is the only stage that changes user-visible behavior.

Depends on Stages 1-5 already in production with `ROUTER_MODE=disabled`.

## `ROUTER_MODE` semantics

| Value | Router endpoint | n8n behavior |
|---|---|---|
| `disabled` | reachable but n8n doesn't call it | legacy flow only |
| `shadow` | called for every inquiry | legacy flow always runs; router result is **logged, not honored** |
| `knowledge_only` | called for every inquiry | router honored only when `route == knowledge_question` AND `confidence >= 0.8`; everything else legacy |
| `full` | called for every inquiry | all three routes honored |

The mode is read from `settings.ROUTER_MODE` and surfaced in `/health` (added in Stage 4). The API itself **does not enforce** which mode is active — it simply returns the classification. n8n owns the dispatch logic; the env var is the contract n8n reads.

## Phase 1 — Shadow (~1 week)

### n8n change
Insert a step at the top of the workflow:

```
inquiry detected
  -> call POST /api/v1/route-inquiry (decision-only)
  -> log {ticket_id, inquiry, route, confidence, signals, fast_path_hit} to DevRev/BigQuery
  -> CONTINUE through legacy flow regardless
```

### What we're measuring
For each ticket, compare the router's decision to what the legacy flow actually produced:

- Did the router say `knowledge_question` while the legacy flow returned a high-quality `generate-response` answer? → router would have **shortcut a working flow** — fine if the answers match, concerning if they differ.
- Did the router say `knowledge_question` while the legacy flow returned `blocked_missing_data` like our motivating case? → router would have **avoided the regression** — green light.
- Did the router say `generate_response` while the inquiry was clearly factual? → mis-classification; iterate on prompt.

### Build a divergence query
Sample ~50 tickets where router decision diverges from the legacy flow's output quality. Manually classify each as router-correct or router-incorrect. Use that to:

- Adjust the system prompt.
- Tighten or loosen the fast-path regex.
- Add few-shot examples for failure modes seen.

### Promotion criteria
- ≥90% accuracy on shadow traffic vs. manual labels.
- ≤5% misrouting in the dangerous direction (eligibility cases routed to `knowledge_question`).
- No router 5xx or timeout regressions in `/health`.

## Phase 2 — Knowledge-only (~1 week)

### n8n change

```
inquiry detected
  -> call POST /api/v1/route-inquiry
  -> Switch:
       (route == "knowledge_question" AND confidence >= 0.8)
           -> call POST /api/v1/knowledge-question with suggested_payload
           -> return answer to DevRev
       else
           -> legacy flow (required-data -> ForUsBots -> generate-response)
```

This is the **lowest-risk activation**: we only short-circuit when the classifier is confident it's a factual question. Misrouting risk is bounded by the 0.8 threshold and the fact that `knowledge-question` is non-destructive (it just answers from the KB).

### What we're measuring
- Shortcut hit-rate (% of inquiries that take the new fast path).
- Customer satisfaction / agent overrides on shortcut answers vs. legacy answers.
- Any regression in legacy-flow inquiries (sanity — there shouldn't be).

### Promotion criteria
- Shortcut rate is meaningful (target: ≥15% of inquiries).
- No quality regression on shortcut answers vs. matched legacy answers (sample ~30).
- Latency improvement on shortcut path is real (skip `required-data` + ForUsBots saves ~seconds per ticket).

## Phase 3 — Full (ongoing)

### n8n change

```
inquiry detected
  -> call POST /api/v1/route-inquiry
  -> Switch:
       knowledge_question -> POST /api/v1/knowledge-question
       generate_response  -> existing flow (required-data -> ForUsBots -> generate-response)
       needs_more_info    -> existing flow
```

The `generate_response` route still goes through `required-data` → ForUsBots first today, because the router doesn't itself collect data. A later optimization can let the router skip `required-data` when `collected_data` is already present in the request — out of scope for this rollout.

### Rollback plan
Set `ROUTER_MODE=disabled` (or earlier phase). n8n should be programmed to read this from a config endpoint or fall back on the legacy flow when the router returns a 5xx. No code changes required to roll back.

## Observability

Throughout all phases, log to `ExecutionLogger` with `endpoint="route_inquiry"`. Include:

- `route`, `confidence`, `fast_path_hit`
- `signals` (dict)
- `latency_ms`, `model_used`, `provider_used`
- `delegate` flag, and (if delegated) the downstream endpoint's confidence

Grafana / BigQuery dashboard panels:

- Routing distribution (pie: knowledge_question / generate_response / needs_more_info).
- Fast-path vs. LLM-path ratio.
- Latency p50/p95 split by path.
- Confidence distribution histogram per route.
- Daily divergence count (Phase 1 only).

## Files modified

None in this stage. This is configuration, n8n workflow editing, and operational work.

## Done when

- `ROUTER_MODE=full` is live in production.
- Dashboards show stable routing distribution with no error spike.
- Original failing inquiry, when re-tested live, returns a concrete timeframe via the `knowledge_question` route.
