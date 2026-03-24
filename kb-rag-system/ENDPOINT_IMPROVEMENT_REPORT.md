# Endpoint Improvement Report: Adopting Knowledge Question Capabilities

> **Date:** March 23, 2026  
> **Scope:** Analysis of `/api/v1/knowledge-question` capabilities and adaptation plan for `/api/v1/required-data` and `/api/v1/generate-response`  
> **Status:** Proposed changes — not yet implemented

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Knowledge Question — Full Capability Inventory](#2-knowledge-question--full-capability-inventory)
3. [Current State of Each Endpoint](#3-current-state-of-each-endpoint)
4. [Capability Gap Matrix](#4-capability-gap-matrix)
5. [Detailed Adaptation Plan — Required Data](#5-detailed-adaptation-plan--required-data)
6. [Detailed Adaptation Plan — Generate Response](#6-detailed-adaptation-plan--generate-response)
7. [Context Token Budget Recommendations](#7-context-token-budget-recommendations)
8. [Implementation Order and Priorities](#8-implementation-order-and-priorities)
9. [Evidence from Stress Testing](#9-evidence-from-stress-testing)

---

## 1. Executive Summary

The `/api/v1/knowledge-question` endpoint has evolved into the most capable endpoint in the KB RAG system. It implements six advanced retrieval and generation techniques that the other two endpoints lack:

| Capability | Knowledge Question | Required Data | Generate Response |
|---|:---:|:---:|:---:|
| Query Decomposition | Yes | **No** | **No** |
| Parallel Multi-Query Search | Yes | Partial | **No** |
| Article Diversity Enforcement | Yes | **No** | **No** |
| LLM Coverage Gap Detection | Yes | **No** | **No** |
| Source Article Transparency | Yes | **No** | **No** |
| Hybrid Confidence (LLM + Retrieval) | Yes | **No** | **No** |

Adopting these capabilities into the other endpoints would significantly improve retrieval quality, confidence accuracy, and response completeness — especially for complex, multi-topic inquiries.

---

## 2. Knowledge Question — Full Capability Inventory

### 2.1 Query Decomposition

**Location:** `rag_engine.py` → `_decompose_question()`  
**Prompt:** `prompts.py` → `SYSTEM_PROMPT_DECOMPOSE_QUESTION`

A lightweight LLM call breaks a complex question into 1–3 focused sub-queries, each targeting a distinct 401(k) concept. This is critical because a single embedding vector cannot capture all facets of a multi-topic question.

**How it works:**
1. The original question is sent to the LLM with decomposition instructions
2. The LLM returns `{"sub_queries": ["query 1", "query 2", "query 3"]}`
3. Each sub-query is 10–25 words, preserving critical details (dollar amounts, ages, recordkeepers)
4. Falls back to the original question on any error

**Example from stress test S1:**
- **Input:** "I currently have a $15,000 outstanding 401(k) loan with LT Trust. I just got laid off..."
- **Output sub-queries:**
  - `"terminated employee 401k loan repayment grace period after layoff LT Trust plan loan offset deadline"`
  - `"outstanding $15,000 401k loan after termination unable to repay deemed distribution taxes penalties Form 1099-R"`
  - `"roll over remaining 401k balance minus unpaid loan offset to Fidelity IRA direct rollover rules"`

**Token cost:** ~150 max_tokens per decomposition call (negligible).

### 2.2 Parallel Multi-Query Search

**Location:** `rag_engine.py` → `ask_knowledge_question()` lines 446–463

All sub-queries plus the original question are searched in parallel using `asyncio.gather()`. Each query retrieves up to 15 chunks (`KQ_TOP_K_PER_QUERY = 15`), with no metadata filters (broadest possible semantic search).

**How it works:**
1. Create one `_cached_query()` task per sub-query
2. If the original question is not already a sub-query, add it as an extra task
3. Run all tasks concurrently with `asyncio.gather(*search_tasks)`
4. Merge all results, deduplicate by chunk ID, rank by score descending

**Result:** Up to 60 raw chunks (4 queries × 15 each) before deduplication, typically 25–30 unique chunks after merge.

**Per-query score tracking:** After search, the engine records the best Pinecone score for each sub-query:

```python
per_query_scores = {}
for label, result_list in zip(query_labels, results):
    best = max((c.get('score', 0) for c in result_list), default=0)
    per_query_scores[label] = round(best, 4)
```

This metadata is returned in the response and is useful for diagnosing which sub-queries had poor retrieval.

### 2.3 Chunk Merge and Ranking

**Location:** `rag_engine.py` → `_merge_and_rank_chunks()`

Accepts variable-length chunk lists, deduplicates by chunk ID, and sorts by descending score. This ensures the highest-quality chunks from any sub-query surface to the top, regardless of which query found them.

### 2.4 Article Diversity in Context Building

**Location:** `rag_engine.py` → `_build_context_with_diversity()`

A two-phase context builder that prevents any single article from monopolizing the context window:

**Phase 1 — Diversity guarantee:**
- Identify the best-scoring chunk from each unique article
- Include one chunk per article, sorted by score, until the budget is reached
- This guarantees representation from every relevant article

**Phase 2 — Budget fill with type priority:**
- Fill remaining budget from the ranked chunk pool
- Prioritize high-value chunk types: `business_rules`, `eligibility`, `steps`, `faqs`, `guardrails`, `fees_details`
- Enforce a per-article cap (`KQ_MAX_CHUNKS_PER_ARTICLE = 6`)

**Configuration constants:**
```python
KQ_CONTEXT_BUDGET = 4000
KQ_TOP_K_PER_QUERY = 15
KQ_MAX_CHUNKS_PER_ARTICLE = 6
KQ_SOURCE_MIN_SCORE = 0.20
KQ_PRIORITIZED_TYPES = [
    'business_rules', 'eligibility', 'steps', 'faqs',
    'guardrails', 'fees_details'
]
```

**Context formatting** includes article attribution per section:
```
--- Section 1 (business_rules | Source: 401(k) Options After Leaving Your Job) ---
[content]
```

### 2.5 LLM Coverage Gap Detection

**Location:** `prompts.py` → `SYSTEM_PROMPT_KNOWLEDGE_QUESTION` (rule 9)  
**Engine:** `rag_engine.py` → `ask_knowledge_question()` lines 521–529

The system prompt instructs the LLM to report topics that the question asks about but are **entirely absent** from the context. The LLM returns these in a `coverage_gaps` array:

```json
{
  "answer": "...",
  "key_points": [...],
  "coverage_gaps": [
    "Inherited 401(k) distribution options for a named beneficiary after the participant's death",
    "Whether a non-spouse beneficiary can distribute inherited 401(k) assets over 10 years"
  ]
}
```

**Strict gap reporting rules** (from the prompt):
- Only report core topics that are **entirely absent** from context
- Do NOT report fine-grained details when the general topic IS covered
- Do NOT report tangential or secondary topics
- Return empty list when context addresses the main subject matter

### 2.6 Hybrid Confidence Model (LLM + Retrieval)

**Location:** `rag_engine.py` → `_calculate_knowledge_confidence()`

Returns a semantic label (`well_covered`, `partially_covered`, `limited_coverage`) instead of a raw number. Combines two signal sources:

**Primary signal — LLM-reported coverage gaps:**
| Gaps | Result |
|---|---|
| ≥ 3 | `limited_coverage` |
| 2 | `partially_covered` |
| 1 + good retrieval | `partially_covered` |
| 1 + poor retrieval | `limited_coverage` |

**Secondary signal (when 0 gaps) — Retrieval quality:**
| Avg Score (top 5) | High-Value Types | Result |
|---|---|---|
| ≥ 0.35 | ≥ 3 types | `well_covered` |
| ≥ 0.22 | ≥ 2 types | `partially_covered` |
| Otherwise | — | `limited_coverage` |

High-value chunk types checked: `business_rules`, `eligibility`, `steps`, `faqs`, `guardrails`.

### 2.7 Source Article Transparency

**Location:** `rag_engine.py` → `_build_source_articles()`  
**Model:** `models.py` → `SourceArticle`

Groups all selected chunks by `article_id` and returns a deduplicated list:

```json
{
  "article_id": "401k_force_out_process...",
  "article_title": "401(k) Force-Out Process...",
  "chunk_types_used": "business_rules, eligibility, faqs, guardrails",
  "relevance": "Covers distribution (business_rules, eligibility, faqs, guardrails)",
  "used_info": true,
  "max_score": 0.6119
}
```

- `used_info`: `true` when the article had at least one chunk with score ≥ `KQ_SOURCE_MIN_SCORE` (0.20)
- `max_score`: Highest chunk score for that article
- `chunk_types_used`: Which chunk types contributed from this article

### 2.8 Used Chunks Serialization

**Location:** `rag_engine.py` → `_serialize_used_chunks()`  
**Model:** `models.py` → `UsedChunk`

Every chunk fed to the LLM is returned in the response with:

```json
{
  "chunk_id": "lt_request_401k_termination..._faqs_0",
  "score": 0.5677,
  "chunk_type": "faqs",
  "chunk_tier": "high",
  "article_id": "lt_request_401k_termination...",
  "article_title": "LT: How to Request a 401(k) Termination...",
  "content_preview": "First ~200 characters...",
  "content": "Full chunk content"
}
```

### 2.9 Rich Metadata

The response `metadata` dictionary includes:

| Field | Description |
|---|---|
| `chunks_used` | Number of chunks selected for context |
| `context_tokens` | Tokens consumed by the context |
| `model` | LLM model used |
| `sub_queries` | The decomposed sub-queries |
| `unique_articles` | Total distinct articles consulted |
| `relevant_articles` | Articles with chunks above score threshold |
| `coverage_gaps` | LLM-reported missing topics |
| `per_query_scores` | Best Pinecone score per sub-query |

---

## 3. Current State of Each Endpoint

### 3.1 Required Data (`/api/v1/required-data`)

**Search strategy:**
- Single enriched query: `f"{inquiry} {topic}"`
- RK cascade: RK-specific → global → LT Trust fallback → any
- When RK provided: first two cascade levels run in parallel
- Phase 2: Fetches `eligibility` + `business_rules` chunks from the winning article
- Top-k: 3 for required_data, 7 for context

**Context building:**
- `_build_context_from_chunks()` — simple priority ordering by chunk type
- No article diversity enforcement
- Budget: **2500 tokens**
- Prioritized types: `required_data_must_have`, `eligibility`, `business_rules`

**Confidence:**
- Numeric 0.0–1.0
- Multi-component formula: retrieval+topic (55%), contextual support (10%), semantic similarity (35%)
- Topic relevance checking (article topic vs query topic via substring matching)
- No LLM input

**Response model:**
- `article_reference`: Single article (id, title, confidence)
- `required_fields`: Categorized field lists
- `confidence`: Numeric score
- `metadata`: `chunks_used`, `tokens_used`, `model`

**What it does well:**
- Topic relevance check is more sophisticated than what Knowledge Question has
- Multi-component confidence formula considers topic match explicitly
- RK cascade with parallel first phase is efficient

**What it lacks:**
- No query decomposition
- No multi-query parallel search
- No article diversity in context
- No coverage gap detection
- No source articles or used chunks in response
- Lower context budget (2500 vs 4000)
- No per-query scores

### 3.2 Generate Response (`/api/v1/generate-response`)

**Search strategy:**
- Single enriched query: `f"{inquiry} {topic} {collected_data_excerpt}"`
- RK cascade (same as Required Data)
- Topic strategies per cascade level: exact match → tags → no filter
- Top-k: 30 per strategy

**Context building:**
- `_organize_chunks_by_tier()` + `token_manager.build_context_with_tiers()`
- Tier priority: critical → high → medium → low
- No article diversity enforcement
- Budget: **dynamic** (`max_response_tokens - RESPONSE_MIN_TOKENS`)
- Default: `5000 - 1200 = 3800 tokens`

**Confidence:**
- Numeric 0.0–1.0
- Simple formula: average of top 3 scores × critical chunk boost (1.08 or 1.15)
- Decision mapping: ≥0.70 → `can_proceed`, ≥0.50 → `uncertain`, <0.50 → `out_of_scope`
- No LLM input

**Response model:**
- `decision`: `can_proceed | uncertain | out_of_scope`
- `confidence`: Numeric score
- `response`: Full outcome-driven schema (outcome, response_to_participant, questions_to_ask, escalation, guardrails_applied, data_gaps)
- `metadata`: `chunks_used`, `context_tokens`, `response_tokens`, `model`, `total_inquiries`

**What it does well:**
- Outcome-driven response schema is the most structured and actionable
- Tier-based context prioritization ensures critical information comes first
- Topic strategies with fallback cascade are thorough
- Collected data enriches the query for better semantic matching
- Dynamic token budgeting adapts to the requested response size

**What it lacks:**
- No query decomposition
- No multi-query parallel search
- No article diversity (tier priority can let one article dominate)
- No coverage gap detection (has `data_gaps` field but it's LLM-filled without explicit gap instructions)
- No source articles in response
- No used chunks serialization
- No per-query scores
- Confidence is purely retrieval-based

---

## 4. Capability Gap Matrix

| Capability | Knowledge Question | Required Data | Generate Response | Adaptation Effort |
|---|:---:|:---:|:---:|---|
| Query decomposition | Yes | **Missing** | **Missing** | Medium — reuse `_decompose_question()` |
| Parallel multi-query search | Yes (all sub-queries) | Partial (2 cascade levels) | **Missing** | Medium — add `asyncio.gather` |
| Chunk merge + dedup + rank | Yes | Yes | Yes (via topic strategies) | Already present |
| Article diversity in context | Yes (2-phase) | **Missing** | **Missing** | Low — reuse `_build_context_with_diversity()` |
| Coverage gap detection | Yes (LLM prompt + engine) | **Missing** | **Missing** | Medium — add to prompts + parse |
| Source articles in response | Yes (deduplicated) | **Missing** | **Missing** | Low — reuse `_build_source_articles()` |
| Used chunks in response | Yes (full serialization) | **Missing** | **Missing** | Low — reuse `_serialize_used_chunks()` |
| Per-query scores | Yes | **Missing** | **Missing** | Low — add after parallel search |
| Hybrid confidence (LLM+retrieval) | Yes (3-level semantic) | **Missing** (numeric only) | **Missing** (numeric only) | Medium — adapt formula |
| Topic relevance check | **Missing** | Yes | Yes (via strategies) | Low — adopt in KQ |
| Tier-based context priority | **Missing** | No | Yes | Low — adopt in KQ |
| RK cascade | No (no filters) | Yes | Yes | N/A (KQ is filterless by design) |

---

## 5. Detailed Adaptation Plan — Required Data

### Change 1: Add Query Decomposition

**What to change in `rag_engine.py` → `get_required_data()`:**

Add a decomposition step before searching. The sub-queries should preserve the inquiry's intent but focus each query on a distinct data requirement aspect.

```python
# Before searching, decompose the inquiry
sub_queries = await self._decompose_question(inquiry)
enriched_queries = [f"{sq} {topic}" for sq in sub_queries]
```

**Prompt adjustment:** The existing `SYSTEM_PROMPT_DECOMPOSE_QUESTION` works as-is. The sub-queries already preserve domain context (dollar amounts, plan types, recordkeepers).

**Fallback:** If decomposition fails, fall back to the current single `enriched_query`.

### Change 2: Parallel Multi-Query Search

**What to change in `_search_for_required_data()`:**

Replace the single-query Phase 1 with parallel queries for all sub-queries:

```python
# For each sub-query, search in parallel across the RK cascade
search_tasks = []
for sq in enriched_queries:
    for level in rk_cascade[:2]:  # Parallel for first 2 levels
        level_filters = {
            **level["filters"],
            "plan_type": {"$in": [plan_type, "all"]},
            "chunk_type": {"$eq": "required_data_must_have"}
        }
        search_tasks.append(self._cached_query(sq, top_k=5, filter_dict=level_filters))

results = await asyncio.gather(*search_tasks)
required_data_chunks = self._merge_and_rank_chunks(*results)
```

**Top-k adjustment:** Increase from 3 to 5 per query to account for multiple sub-queries competing for the same chunks.

### Change 3: Article Diversity in Context

**What to change:**

Replace `_build_context_from_chunks()` with `_build_context_with_diversity()` for the context building step:

```python
context, selected_chunks, tokens_used = self._build_context_with_diversity(
    chunks=merged,
    budget=context_budget,
    prioritize_types=['required_data_must_have', 'eligibility', 'business_rules'],
    max_per_article=4  # Tighter cap for required_data — focus on best match
)
```

### Change 4: Add Coverage Gap Detection

**What to change in `prompts.py` → `SYSTEM_PROMPT_REQUIRED_DATA`:**

Add a `coverage_gaps` field to the output schema:

```
Output must be valid JSON with this structure:
{
  "participant_data": [...],
  "plan_data": [...],
  "coverage_gaps": [
    "Data point or topic the inquiry asks about but NOT covered in the context"
  ]
}
```

**What to change in `rag_engine.py` → `get_required_data()`:**

Parse and include coverage gaps in the response:

```python
coverage_gaps = parsed.get("coverage_gaps", [])
```

### Change 5: Add Source Articles + Used Chunks to Response

**What to change in `models.py`:**

Add `source_articles`, `used_chunks`, and `coverage_gaps` fields to `RequiredDataResponse`:

```python
class RequiredDataResponse(BaseModel):
    article_reference: ArticleReference
    required_fields: Dict[str, List[RequiredField]]
    confidence: float
    source_articles: List[SourceArticle] = Field(default_factory=list)
    used_chunks: List[UsedChunk] = Field(default_factory=list)
    coverage_gaps: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any]
```

**What to change in `rag_engine.py` → `get_required_data()`:**

Build source articles and serialize used chunks after context building:

```python
source_articles = self._build_source_articles(selected_chunks)
used_chunks = self._serialize_used_chunks(selected_chunks)
```

### Change 6: Hybrid Confidence

**What to change in `_calculate_required_data_confidence()`:**

Add coverage gaps as an input signal. The existing multi-component formula is good — augment it with the LLM gap signal:

```python
def _calculate_required_data_confidence(self, chunks, query_topic, coverage_gaps=None):
    # Existing formula (retrieval + topic + similarity)...
    
    # Override with gap signal when gaps are reported
    coverage_gaps = coverage_gaps or []
    if len(coverage_gaps) >= 2:
        confidence = min(confidence, 0.40)  # Cap at uncertain
    elif len(coverage_gaps) == 1:
        confidence = min(confidence, 0.60)  # Reduce but don't cap too hard
    
    return round(min(1.0, confidence), 3)
```

### Change 7: Add Per-Query Scores to Metadata

```python
metadata={
    "chunks_used": len(selected_chunks),
    "tokens_used": tokens_used,
    "model": self.model,
    "sub_queries": sub_queries,
    "per_query_scores": per_query_scores,
    "unique_articles": len(set(c['metadata'].get('article_id') for c in selected_chunks)),
    "coverage_gaps": coverage_gaps
}
```

---

## 6. Detailed Adaptation Plan — Generate Response

### Change 1: Add Query Decomposition

**What to change in `rag_engine.py` → `generate_response()`:**

Add decomposition before searching:

```python
# Decompose the inquiry into sub-queries
sub_queries = await self._decompose_question(inquiry)
```

**Query enrichment per sub-query:**

```python
enriched_queries = []
for sq in sub_queries:
    parts = [sq, topic]
    if collected_data and "participant_data" in collected_data:
        for key, value in list(collected_data["participant_data"].items())[:3]:
            parts.append(f"{key}: {value}")
    enriched_queries.append(" ".join(parts))
```

### Change 2: Parallel Multi-Query Search with Topic Strategies

**What to change in `_search_for_response()`:**

For each RK cascade level, run topic strategies for all sub-queries in parallel:

```python
for level in rk_cascade:
    base_filters = {**level["filters"], "plan_type": {"$in": [plan_type, "all"]}}
    
    # Parallel search across all sub-queries
    search_tasks = [
        self._search_with_topic_strategies(eq, base_filters, topic)
        for eq in enriched_queries
    ]
    results = await asyncio.gather(*search_tasks)
    chunks = self._merge_and_rank_chunks(*results)
    
    if self._rk_results_sufficient(chunks, ...):
        return chunks
```

### Change 3: Hybrid Context Building (Diversity + Tiers)

**What to change:**

Create a new method `_build_context_with_diversity_and_tiers()` that combines both strategies:

**Phase 1 — Diversity:** Best chunk from each article (guarantees cross-article coverage)  
**Phase 2 — Tier priority:** Fill remaining budget from critical → high → medium → low, with a per-article cap

```python
def _build_context_with_diversity_and_tiers(self, chunks, budget, max_per_article=6):
    # Phase 1: Best chunk from each article (sorted by score)
    article_best = {}
    for chunk in chunks:
        aid = chunk['metadata'].get('article_id', 'unknown')
        if aid not in article_best or chunk['score'] > article_best[aid]['score']:
            article_best[aid] = chunk
    
    selected, selected_ids, tokens_used, article_counts = [], set(), 0, defaultdict(int)
    
    for _aid, chunk in sorted(article_best.items(), key=lambda x: x[1]['score'], reverse=True):
        content = chunk['metadata'].get('content', '')
        chunk_tokens = self.token_manager.count_tokens(content)
        if tokens_used + chunk_tokens <= budget:
            selected.append(chunk)
            selected_ids.add(chunk.get('id'))
            tokens_used += chunk_tokens
            article_counts[chunk['metadata'].get('article_id', 'unknown')] += 1
    
    # Phase 2: Fill by tier priority with per-article cap
    by_tier = self._organize_chunks_by_tier(chunks)
    for tier in ['critical', 'high', 'medium', 'low']:
        for chunk in by_tier.get(tier, []):
            cid = chunk.get('id')
            if cid in selected_ids:
                continue
            aid = chunk['metadata'].get('article_id', 'unknown')
            if article_counts[aid] >= max_per_article:
                continue
            content = chunk['metadata'].get('content', '')
            chunk_tokens = self.token_manager.count_tokens(content)
            if tokens_used + chunk_tokens <= budget:
                selected.append(chunk)
                selected_ids.add(cid)
                tokens_used += chunk_tokens
                article_counts[aid] += 1
    
    # Format context...
```

### Change 4: Coverage Gap Detection

**What to change in `prompts.py` → `SYSTEM_PROMPT_GENERATE_RESPONSE`:**

Add a `coverage_gaps` field to the response schema. This is distinct from the existing `data_gaps` field:

- `data_gaps`: Information missing from the KB context but potentially relevant
- `coverage_gaps`: Core topics the inquiry asks about that are **entirely absent** from context

Add to the schema section:

```
"coverage_gaps": [
    "Core topic the inquiry asks about that is entirely absent from the KB context"
]
```

Add a rule explaining the distinction:

```
11. "coverage_gaps" vs "data_gaps": coverage_gaps are core topics the inquiry fundamentally
    asks about that are ENTIRELY absent from the context. data_gaps are details that COULD
    be relevant but are not blocking. If the context covers the main subject, coverage_gaps
    should be empty.
```

**What to change in `rag_engine.py` → `generate_response()`:**

Parse and use coverage gaps:

```python
coverage_gaps = parsed.get("coverage_gaps", [])
```

### Change 5: Add Source Articles to Metadata

**What to change in `rag_engine.py` → `generate_response()`:**

After context building, build source articles and include in metadata:

```python
source_articles = self._build_source_articles(selected_chunks)

metadata={
    "chunks_used": len(selected_chunks),
    "context_tokens": tokens_used,
    "response_tokens": self.token_manager.count_tokens(llm_response),
    "model": self.model,
    "total_inquiries": total_inquiries_in_ticket,
    "sub_queries": sub_queries,
    "per_query_scores": per_query_scores,
    "source_articles": source_articles,
    "unique_articles": len(source_articles),
    "coverage_gaps": coverage_gaps
}
```

### Change 6: Hybrid Confidence

**What to change in `_calculate_confidence()` and `_determine_decision()`:**

Integrate coverage gaps into the decision:

```python
def _calculate_confidence(self, chunks, coverage_gaps=None):
    if not chunks:
        return 0.0
    
    # Existing: average of top 3 scores + critical boost
    top_scores = [chunk['score'] for chunk in chunks[:3]]
    avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
    
    critical_count = sum(1 for c in chunks if c['metadata'].get('chunk_tier') == 'critical')
    confidence = avg_score
    if critical_count >= 2:
        confidence = min(1.0, confidence * 1.15)
    elif critical_count >= 1:
        confidence = min(1.0, confidence * 1.08)
    
    # New: penalize for coverage gaps
    coverage_gaps = coverage_gaps or []
    if len(coverage_gaps) >= 3:
        confidence = min(confidence, 0.30)
    elif len(coverage_gaps) == 2:
        confidence = min(confidence, 0.45)
    elif len(coverage_gaps) == 1:
        confidence = min(confidence, 0.60)
    
    return round(confidence, 3)
```

### Change 7: Add Per-Query Scores

Same pattern as Knowledge Question — record best score per sub-query after parallel search and include in metadata.

---

## 7. Context Token Budget Recommendations

### Current Budgets

| Endpoint | Current Budget | How Set |
|---|---|---|
| Knowledge Question | **4000** tokens | Constant `KQ_CONTEXT_BUDGET` |
| Required Data | **2500** tokens | Hardcoded in `get_required_data()` |
| Generate Response | **Dynamic** (default ~3800) | `max_response_tokens - RESPONSE_MIN_TOKENS` |

### Recommended Changes

#### Required Data: 2500 → 3500 tokens

**Rationale:**
- With query decomposition and multi-query search, more chunks will be retrieved
- The diversity algorithm needs room to include one chunk per article
- Required data inquiries often cross topics (e.g., "rollover + loan"), needing broader context
- The LLM call only uses 800 max_tokens for completion, so the total is still manageable

**Change:**
```python
# In get_required_data():
context_budget = 3500  # was 2500

# Or define as a class constant:
RD_CONTEXT_BUDGET = 3500
```

#### Generate Response: Keep Dynamic, Raise Floor

**Rationale:**
- The dynamic budget (`max_response_tokens - 1200`) is a sound design
- However, `RESPONSE_MIN_TOKENS = 1200` leaves limited room when `max_response_tokens` is at the lower end (500)
- With decomposition bringing more relevant chunks, the context deserves more room

**Change:**
```python
# Option A: Increase minimum context floor
RESPONSE_MIN_CONTEXT_TOKENS = 2000

context_budget = max(
    self.RESPONSE_MIN_CONTEXT_TOKENS,
    max_response_tokens - self.RESPONSE_MIN_TOKENS
)
```

This ensures at least 2000 tokens of context even when `max_response_tokens` is low.

#### Knowledge Question: 4000 → 4000 (no change)

The current budget is well-calibrated. Stress test results show context_tokens ranging from 1569 (focused queries) to 3995 (broad queries), demonstrating the budget is used effectively without waste.

### Token Budget Summary

| Endpoint | Current | Proposed | Delta |
|---|---|---|---|
| Knowledge Question | 4000 | 4000 | — |
| Required Data | 2500 | **3500** | **+1000** |
| Generate Response (floor) | 0 (dynamic only) | **2000 minimum** | **+2000 floor** |
| Generate Response (default) | ~3800 | ~3800 | — |

### Impact on LLM Costs

| Endpoint | Extra Context Tokens | Extra Decomposition Call | Est. Cost Impact |
|---|---|---|---|
| Required Data | +1000 input tokens/call | +150 tokens (decompose) | ~+$0.002/call |
| Generate Response | +0 to +2000 input | +150 tokens (decompose) | ~+$0.003/call |

The cost increase is negligible relative to the quality improvement.

---

## 8. Implementation Order and Priorities

### Phase 1 — High Impact, Reuse Existing Code (Week 1)

| # | Change | Endpoint | Effort | Files Modified |
|---|---|---|---|---|
| 1 | Add query decomposition | Both | Medium | `rag_engine.py` |
| 2 | Article diversity in context | Required Data | Low | `rag_engine.py` |
| 3 | Hybrid diversity + tiers | Generate Response | Medium | `rag_engine.py` |
| 4 | Raise context budget to 3500 | Required Data | Trivial | `rag_engine.py` |
| 5 | Add context floor of 2000 | Generate Response | Trivial | `rag_engine.py` |

### Phase 2 — Confidence and Transparency (Week 2)

| # | Change | Endpoint | Effort | Files Modified |
|---|---|---|---|---|
| 6 | Coverage gap detection | Both | Medium | `prompts.py`, `rag_engine.py` |
| 7 | Hybrid confidence | Both | Medium | `rag_engine.py` |
| 8 | Source articles in response | Required Data | Low | `models.py`, `rag_engine.py`, `main.py` |
| 9 | Source articles in metadata | Generate Response | Low | `rag_engine.py` |

### Phase 3 — Observability and Debugging (Week 3)

| # | Change | Endpoint | Effort | Files Modified |
|---|---|---|---|---|
| 10 | Per-query scores in metadata | Both | Low | `rag_engine.py` |
| 11 | Used chunks in Required Data | Required Data | Low | `models.py`, `rag_engine.py`, `main.py` |
| 12 | Sub-queries in metadata | Both | Trivial | `rag_engine.py` |

### Phase 4 — Cross-Pollination to Knowledge Question (Week 3)

| # | Change | Endpoint | Effort | Files Modified |
|---|---|---|---|---|
| 13 | Tier-based priority in diversity Phase 2 | Knowledge Question | Low | `rag_engine.py` |
| 14 | Numeric confidence_score alongside note | Knowledge Question | Low | `models.py`, `rag_engine.py` |

---

## 9. Evidence from Stress Testing

### Knowledge Question Stress Test Results (test_results_stress_v2.json)

| ID | Category | Sub-Queries | Chunks | Articles | Coverage Gaps | Confidence | Time |
|---|---|---|---|---|---|---|---|
| S1 | Out-of-Scope (Loans) | 3 | 26 | 6 | 1 | partially_covered | 28.0s |
| S2 | Wrong Plan Type | 2 | 25 | 5 | 2 | partially_covered | 28.2s |
| S3 | Fabricated Rule Trap | 3 | 17 | 8 | 2 | partially_covered | 23.9s |
| S4 | Impossible Transaction | 3 | 27 | 7 | 2 | partially_covered | 30.0s |
| S5 | Beneficiary/Death | 3 | 14 | 7 | 3 | limited_coverage | 26.9s |
| S6 | QDRO/Divorce | 3 | 24 | 8 | 3 | limited_coverage | 26.0s |
| S7 | Vesting + Termination | 3 | 15 | 4 | 1 | partially_covered | 24.3s |
| S8 | Multi-System (HSA+IRA+401k) | 3 | 25 | 5 | 1 | partially_covered | 53.4s |
| S9 | Extreme Detail Tax Trap | 3 | 22 | 6 | 4 | limited_coverage | 22.4s |
| S10 | Contradictory Premises | 2 | 8 | 3 | 0 | well_covered | 41.2s |

### Key Observations

1. **Coverage gap detection is accurate.** S5 (inherited 401k — not in KB) correctly reports 3 gaps and marks `limited_coverage`. S10 (RMD rules — fully in KB) reports 0 gaps and marks `well_covered`. Without this capability, the other endpoints would return misleading numeric confidence for S5.

2. **Article diversity matters.** S1 pulls from 6 different articles to cover loans, rollovers, and termination procedures comprehensively. Without diversity enforcement, the rollover article alone would dominate the context, leaving loan-related chunks out.

3. **Query decomposition improves retrieval breadth.** S8 (in-service + Traditional IRA + Roth IRA + HSA) decomposes into 3 focused queries. The per-query scores show the HSA query scored 0.5082 while the in-service query scored 0.6855 — both found relevant content, but a single combined query would have been diluted.

4. **Per-query scores expose retrieval weaknesses.** In S5, all sub-queries score below 0.50 (range: 0.4156–0.4937), immediately signaling that the KB has limited content on this topic — even before the LLM reports coverage gaps.

### What This Means for the Other Endpoints

If Required Data or Generate Response received the same S1 inquiry ("$15,000 loan + layoff + rollover to Fidelity"):
- **Current behavior:** Single query `"I currently have a $15,000... rollover"` would retrieve mostly rollover chunks, potentially missing loan-specific required_data fields
- **With decomposition:** Three focused queries would retrieve both loan AND rollover required_data chunks
- **With diversity:** Context would include chunks from loan articles AND rollover articles
- **With coverage gaps:** The response would flag that "exact loan repayment grace period" is not in KB, rather than silently omitting it

---

*End of report. All proposed changes reuse existing Knowledge Question infrastructure — no new algorithms need to be designed.*
