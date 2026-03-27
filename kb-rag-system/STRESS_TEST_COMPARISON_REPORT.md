# Stress Test Comparison Report
## Generate Response vs Knowledge Question

**Date:** March 24, 2026  
**Test Type:** Adversarial Stress Test (12 questions x 2 endpoints)  
**API:** `http://localhost:8000` | Model: `gpt-5.4` | Reasoning: `medium`  
**Data File:** `test_results_stress_comparison.json`

---

## 1. Executive Summary

Both endpoints were tested against 12 adversarial questions designed to expose weaknesses in hallucination resistance, out-of-scope detection, and confidence calibration. **Knowledge Question (KQ)** demonstrated superior reliability and speed, while **Generate Response (GR)** provided more structured output with built-in safety guardrails but at a significant cost in latency and reliability.

| Metric | Knowledge Question | Generate Response | Winner |
|---|---|---|---|
| Success Rate | **12/12 (100%)** | 9/12 (75%) | KQ |
| Avg Latency | **27.8s** | 58.8s | KQ |
| Median Latency | **26.5s** | 61.5s | KQ |
| Avg Context Tokens | 3,668 | **1,985** | GR |
| Avg Response Tokens | N/R | **849** | — |
| Avg Chunks Used | 17.6 | **8.7** | GR |
| Avg Source Articles | 7.0 | 5.9 | Comparable |
| Avg Answer Length | **2,478 chars** | 1,080 chars | KQ |
| Guardrails | No | **Yes** | GR |
| Escalation Detection | No | **Yes** | GR |
| Coverage Gaps Reporting | Via metadata | **Structured** | GR |

---

## 2. Reliability Analysis

### 2.1 Success Rates

- **Knowledge Question: 100% (12/12)** — No failures across all adversarial categories.
- **Generate Response: 75% (9/12)** — 3 timeouts at the 120s mark.

### 2.2 Failed Tests (Generate Response)

| Test ID | Category | Failure Mode | Root Cause Analysis |
|---|---|---|---|
| ST01 | Fake Recordkeeper | Timeout (120s) | Complex Vanguard rollover query + RK cascade search with unknown recordkeeper. The search strategy likely exhausted retries against Vanguard-specific metadata filters before falling back. |
| ST09 | Multi-Topic (Mixed Coverage) | Timeout (120s) | Three distinct sub-topics (auto-increase, mega backdoor Roth, hardship) likely generated many sub-queries. The retrieval pipeline attempted multiple search lanes per topic, compounding latency beyond the timeout. |
| ST12 | Multi-Recordkeeper Consolidation | Timeout (120s) | Three recordkeepers mentioned (LT Trust, Empower, ADP) triggered the RK cascade search logic for each, with Empower and ADP having no matches. Combined with a complex rollover topic, this exceeded the timeout. |

**Pattern:** All 3 failures involve queries that trigger the GR-specific **RK cascade search** and **topic strategy** pipeline with entities not present in the knowledge base. This indicates the multi-lane search strategy doesn't fail fast enough when no matches exist.

### 2.3 Knowledge Question Resilience

KQ handled all 12 adversarial questions without issue, including the 3 that caused GR to timeout. This is because KQ uses a simpler `_cached_query` with `filter_dict=None` (index-wide search), avoiding the cascade logic entirely.

---

## 3. Latency Analysis

### 3.1 Overview (Successful Runs Only)

| Statistic | KQ | GR | GR/KQ Ratio |
|---|---|---|---|
| Min | 11.8s | 47.9s | 4.1x |
| Max | 49.9s | 71.5s | 1.4x |
| Mean | 27.8s | 58.8s | **2.1x** |
| Median | 26.5s | 61.5s | **2.3x** |
| Std Dev | 10.3s | 8.3s | — |
| Total (all) | 333.5s | 529.5s | 1.6x |

### 3.2 Per-Test Latency Comparison

| Test | Category | KQ (s) | GR (s) | GR Overhead (%) |
|---|---|---|---|---|
| ST02 | QDRO/Divorce | 11.8 | 53.1 | +348% |
| ST03 | Fake RK + Real Topic | 30.8 | 61.9 | +101% |
| ST04 | Wrong Plan (403b) | 34.9 | 49.8 | +43% |
| ST05 | Invented Feature | 22.4 | 61.5 | +175% |
| ST06 | Investments (OOS) | 24.0 | 47.9 | +99% |
| ST07 | Fabricated Legal Rule | 37.5 | 71.5 | +91% |
| ST08 | Beneficiary (OOS) | 16.8 | 67.7 | +304% |
| ST09 | Multi-Topic | 29.0 | TIMEOUT | — |
| ST10 | Contradictory RK | 21.7 | 52.4 | +141% |
| ST11 | Rule of 55 | 21.5 | 63.6 | +196% |
| ST12 | Multi-RK Consolidation | 49.9 | TIMEOUT | — |

**Key observations:**
- GR is consistently **2-4.5x slower** than KQ for the same question.
- The overhead comes from: (1) RK cascade search, (2) topic strategy enrichment, (3) structured JSON output generation with guardrails, and (4) larger prompt templates with collected_data.
- The lowest overhead (43%) was ST04 where GR only found 4 chunks and 1 article — minimal context to process.

### 3.3 Latency Breakdown by Component

GR's additional latency comes from these pipeline stages not present in KQ:

| Component | Estimated Additional Time |
|---|---|
| RK cascade search (up to 3 lanes) | ~10-20s |
| Topic strategy enrichment | ~5-10s |
| Larger prompt (collected_data, structured output schema) | ~5-15s |
| Guardrails generation | ~5-10s |
| **Total estimated overhead** | **~25-55s** |

---

## 4. Resource Utilization Analysis

### 4.1 Context Token Budget

| Metric | KQ | GR |
|---|---|---|
| Budget | Fixed 4,000 tokens | Dynamic (based on max_response_tokens) |
| Avg Used | 3,668 tokens | 1,985 tokens |
| Utilization | **92%** | ~50% |
| Min | 2,162 | 1,967 |
| Max | 3,996 | 2,000 |

**Analysis:** KQ fills nearly its entire 4,000-token context budget (92% utilization), pulling in broad context from the knowledge base. GR consistently uses ~2,000 tokens of context, which suggests its retrieval pipeline is more selective/filtered. This is by design — GR uses RK-specific filtering and topic strategies that narrow results.

### 4.2 Retrieval Volume

| Metric | KQ | GR | KQ/GR Ratio |
|---|---|---|---|
| Avg Chunks Retrieved | 17.6 | 8.7 | **2.0x** |
| Max Chunks | 29 | 13 | 2.2x |
| Min Chunks | 7 | 4 | 1.8x |
| Avg Articles | 7.0 | 5.9 | 1.2x |

**Analysis:** KQ retrieves roughly **2x more chunks** than GR. This broader context retrieval is what gives KQ its higher answer length and more comprehensive responses. GR's filtered approach retrieves fewer, more targeted chunks, which makes it faster at the LLM generation step but potentially misses relevant context.

### 4.3 Token Efficiency

| Metric | KQ | GR |
|---|---|---|
| Avg Context Tokens | 3,668 | 1,985 |
| Avg Response Tokens | N/R (est. ~800-1,200) | 849 |
| Chars output / Context Token | **0.68** | 0.54 |
| Total Context Tokens (12 tests) | 44,021 | 17,862 |

**Analysis:** KQ uses **2.5x more total context tokens** across all tests, but converts context to answer content more efficiently (0.68 chars per context token vs 0.54 for GR). However, GR's response tokens include structured metadata (JSON with steps, warnings, guardrails) that isn't captured in the answer_length metric.

### 4.4 Cost Estimation (per-query, approximate)

Assuming GPT-5.4 pricing:

| Component | KQ | GR |
|---|---|---|
| Embedding calls (sub-queries) | ~3 queries | ~3 queries + cascade |
| Pinecone queries | ~3 queries | ~6-9 queries (cascaded) |
| LLM context input | ~3,700 tokens | ~2,000 tokens |
| LLM response output | ~800-1,200 tokens | ~850 tokens |
| **Estimated relative cost** | **1x (baseline)** | **~1.3-1.5x** |

GR costs 30-50% more per query due to additional Pinecone queries and the cascade search, despite using fewer context tokens. When factoring in the 25% failure rate (wasted API calls on timeouts), effective cost rises to ~1.7-2x per successful response.

---

## 5. Response Quality Analysis

### 5.1 Answer Depth & Comprehensiveness

| Test | KQ Answer (chars) | GR Answer (chars) | KQ Richer? |
|---|---|---|---|
| ST02 (QDRO) | 705 | 708 | Comparable |
| ST03 (Force-out) | 2,677 | 1,184 | Yes |
| ST04 (403b) | 3,590 | 707 | Yes |
| ST05 (Invented Feature) | 1,892 | 1,542 | Slightly |
| ST06 (Investments) | 1,416 | 788 | Yes |
| ST07 (Fabricated Rule) | 2,529 | 1,338 | Yes |
| ST08 (Beneficiary) | 1,029 | 951 | Comparable |
| ST10 (Contradictory RK) | 1,682 | 1,550 | Comparable |
| ST11 (Rule of 55) | 2,181 | 951 | Yes |

KQ produces **2.3x longer answers** on average, providing more context, nuance, and supporting information. This makes KQ better for knowledge exploration and agent self-service.

GR produces shorter, more actionable responses focused on specific steps and warnings. This is by design for the ticket-response workflow.

### 5.2 Adversarial Trap Handling

Both endpoints demonstrated strong hallucination resistance:

| Capability | KQ | GR |
|---|---|---|
| Refuses fake recordkeepers | Yes | Yes |
| Detects out-of-scope topics | Yes (via confidence_note) | Yes (via decision + coverage_gaps) |
| Avoids fabricating procedures | Yes | Yes |
| Identifies mixed coverage | Yes | Yes (when not timing out) |
| Explicit guardrail statements | No | **Yes (structured list)** |
| Escalation recommendation | Informal (in text) | **Yes (structured field)** |

**GR advantage:** Every successful GR response included explicit `guardrails_applied` (3-6 guardrails per response) and structured `escalation` fields. This is critical for production use where automated downstream systems need machine-readable safety signals.

**KQ advantage:** KQ's broader context retrieval (17.6 chunks vs 8.7) sometimes provided better coverage for edge cases, giving more comprehensive "what we know and don't know" answers.

### 5.3 Confidence Calibration

**GR Decision Distribution (9 successful):**
- `out_of_scope`: 3 (ST02, ST06, ST08) — Correctly identified
- `uncertain`: 3 (ST04, ST05, ST11) — Appropriately cautious
- `can_proceed`: 3 (ST03, ST07, ST10) — Proceeded with caveats

**KQ Confidence Distribution (12 successful):**
- `limited_coverage`: 5 (ST02, ST04, ST05, ST06, ST08)
- `partially_covered`: 7 (ST01, ST03, ST07, ST09, ST10, ST11, ST12)

Both endpoints appropriately downgraded confidence for adversarial queries, but GR's three-level decision system (`out_of_scope` / `uncertain` / `can_proceed`) provides more actionable routing signals than KQ's two-level note (`limited_coverage` / `partially_covered`).

---

## 6. Architectural Differences & Impact

| Aspect | Knowledge Question | Generate Response |
|---|---|---|
| **Search Strategy** | `_cached_query`, no filters, index-wide | RK cascade + topic strategies + unfiltered lane |
| **Context Budget** | Fixed 4,000 tokens | Dynamic from `max_response_tokens` |
| **Chunk Prioritization** | `KQ_PRIORITIZED_TYPES` (business_rules, steps, faqs) | `_build_context_with_diversity_and_tiers` with article diversity |
| **Prompt Complexity** | Simple Q&A template | Complex structured output with collected_data, guardrails |
| **LLM Max Tokens** | Fixed 2,000 | Dynamic `completion_budget` |
| **Auth Required** | No | Yes (X-API-Key) |
| **Best For** | Knowledge exploration, agent self-service | Production ticket responses, automated workflows |

---

## 7. Key Remarks

### 7.1 Critical Issues

1. **GR Timeout Rate (25%)**: Three queries timed out at 120s. These were all complex multi-entity queries. In production, this translates to 1 in 4 difficult tickets failing silently if the client doesn't retry.

2. **RK Cascade is the Bottleneck**: The cascade search strategy (searching by recordkeeper, then falling back) is the primary latency driver. When the recordkeeper isn't in the KB, all cascade lanes fail before the fallback, wasting ~20-40s.

3. **`total_tokens` Not Reported**: Both endpoints report `total_tokens: 0` in metadata. This suggests the OpenAI API response field isn't being captured properly in `_call_llm()`, making cost tracking impossible.

### 7.2 Strengths

1. **KQ Reliability**: 100% success rate even on adversarial queries makes it the go-to for real-time user-facing scenarios.

2. **GR Safety Features**: Structured guardrails, escalation detection, and coverage gaps are production-critical features that KQ lacks.

3. **Both Resist Hallucination**: Neither endpoint fabricated information for fake recordkeepers, invented features, or out-of-scope topics.

4. **KQ Context Efficiency**: 92% context budget utilization means the retrieval pipeline is well-calibrated for broad knowledge queries.

---

## 8. Recommendations

### 8.1 High Priority — Fix GR Timeouts

**Problem:** GR times out on complex queries with unknown entities.

**Solution:** Add a fast-fail mechanism to the RK cascade search:

```python
# In _search_for_response, add early termination
if rk_results_count == 0 and cascade_attempt >= 2:
    break  # Don't waste time on further cascade lanes
```

Also consider reducing the GR timeout from 120s to 90s and implementing a circuit-breaker pattern that falls back to a simplified search (similar to KQ's approach) after 60s.

### 8.2 High Priority — Fix Token Tracking

**Problem:** `total_tokens` is always 0, making cost monitoring impossible.

**Solution:** In `_call_llm()`, capture `response.usage.total_tokens` from the OpenAI API response and propagate it through the result metadata:

```python
usage = response.usage
metadata["total_tokens"] = usage.total_tokens if usage else 0
```

### 8.3 Medium Priority — Add KQ Guardrails

**Problem:** KQ lacks structured safety signals (guardrails, escalation flags).

**Solution:** Add a lightweight post-processing step to KQ responses that extracts guardrails from the `confidence_note` and `coverage_gaps`. This could be rule-based (no LLM call needed):

```python
if confidence_note == "limited_coverage":
    guardrails = ["Answer based on limited KB coverage; verify with Support"]
    escalation_recommended = True
```

### 8.4 Medium Priority — Optimize GR Context Budget

**Problem:** GR only uses ~50% of its context token budget (1,985 of ~4,000 available).

**Solution:** The low utilization suggests the filtered retrieval (RK cascade + topic strategy) is too aggressive. Consider:
- Increase `GR_MAX_CHUNKS_PER_ARTICLE` to allow more chunks per article
- Add an unfiltered "padding" step that fills remaining context budget with general-topic chunks
- Match KQ's approach of using `KQ_CONTEXT_BUDGET = 4000` as a baseline

### 8.5 Medium Priority — GR Fallback to KQ Mode

**Problem:** When GR can't find RK-specific content, it wastes time on cascade search.

**Solution:** Implement a "degraded mode" where GR falls back to KQ-style index-wide search after the first cascade lane returns empty:

```python
if cascade_lane_1_empty:
    # Fall back to KQ-style broad search, then format as GR output
    chunks = self._cached_query(query, top_k=KQ_TOP_K_PER_QUERY)
```

### 8.6 Low Priority — Response Streaming

**Problem:** Both endpoints have long wait times (12-72s) before any content is returned.

**Solution:** Implement SSE (Server-Sent Events) streaming for both endpoints. This provides better UX by showing partial responses as they're generated:

```python
@app.post("/api/v1/knowledge-question/stream")
async def knowledge_question_stream(request: KnowledgeQuestionRequest):
    return StreamingResponse(engine.stream_knowledge_question(request.question))
```

### 8.7 Low Priority — Parallel Sub-Query Execution

**Problem:** Sub-queries appear to execute sequentially, adding latency.

**Solution:** Execute Pinecone queries in parallel using `asyncio.gather()`:

```python
results = await asyncio.gather(*[
    self._cached_query(sq) for sq in sub_queries
])
```

---

## 9. Recommendation Summary

| # | Priority | Recommendation | Expected Impact |
|---|---|---|---|
| 8.1 | **HIGH** | Fix GR timeouts with fast-fail cascade | GR success rate: 75% → ~95% |
| 8.2 | **HIGH** | Fix total_tokens tracking | Enable cost monitoring |
| 8.3 | MEDIUM | Add KQ guardrails (rule-based) | Parity with GR safety signals |
| 8.4 | MEDIUM | Optimize GR context budget usage | Better answer quality, ~50% → 80% utilization |
| 8.5 | MEDIUM | GR fallback to KQ-style search | Reduce timeout risk, -30% avg latency |
| 8.6 | LOW | Response streaming (SSE) | Better UX, perceived latency reduction |
| 8.7 | LOW | Parallel sub-query execution | -20-30% latency for both endpoints |

---

## 10. Conclusion

**Knowledge Question is the better-performing endpoint** for adversarial/stress scenarios. It delivers 100% reliability, 2x faster responses, and more comprehensive answers. Its broader search strategy proves more resilient against edge cases.

**Generate Response provides production-critical features** (guardrails, escalation, structured output) that KQ cannot match, but its reliability issues and 2x latency penalty must be addressed before it can handle the full spectrum of production queries.

**The optimal architecture** would combine KQ's search resilience with GR's structured output: implement the GR fallback to KQ-style search (Rec 8.5) and add lightweight guardrails to KQ (Rec 8.3), creating a unified pipeline that's both reliable and production-safe.

---

*Report generated from `test_results_stress_comparison.json` — 12 adversarial stress tests across both endpoints.*
