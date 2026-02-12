# Development Plan: Split `required_data` Chunk into Granular Sub-Chunks

## Context

The `/api/v1/required-data` endpoint returns the data fields that n8n/ForUsBots must extract from the participant portal before generating a response. Currently, the chunking module creates ONE large chunk containing ALL `required_data` sections (`must_have`, `nice_to_have`, `if_missing`, `disambiguation_notes`) with the same `chunk_type="required_data"` and `tier="critical"`.

This causes the LLM to return **all** fields mixed together (18 fields in the last test), when the endpoint should **only** return the `must_have` fields — the data points that come from the participant portal/profile.

### Problem Summary

| Section | Source Type | Purpose | Current Chunk | Should Be |
|---|---|---|---|---|
| `must_have` | `participant_profile` / `agent_input` | Data to extract from portal | `required_data` (critical) | Own chunk (critical) |
| `nice_to_have` | `message_text` | Context from conversation | `required_data` (critical) | Own chunk (medium) |
| `if_missing` | N/A (instructions) | What to ask if data missing | `required_data` (critical) | Own chunk (high) |
| `disambiguation_notes` | N/A (logic) | Decision-making rules | **NOT CHUNKED AT ALL** (bug) | Own chunk (medium) |

### Files to Modify

1. `kb-rag-system/data_pipeline/chunking.py` — Split into 4 chunks
2. `kb-rag-system/data_pipeline/rag_engine.py` — Update search filters to use new `chunk_type` values
3. `kb-rag-system/data_pipeline/prompts.py` — Refine the prompt to explicitly request only must_have fields
4. **Re-upload article** — Run `update_article.py` to re-process chunks in Pinecone

### Reference Article for Testing

```
Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json
```

Article ID: `lt_request_401k_termination_withdrawal_or_rollover`

---

## Task 1: Modify `chunking.py` — Split `_create_required_data_chunks`

**File:** `kb-rag-system/data_pipeline/chunking.py`

### 1.1 Replace `_create_required_data_chunks` method (lines 142-194)

The current method creates ONE chunk for the entire `required_data` section. Replace it to create **4 separate chunks**, each with its own `chunk_type` and `tier`.

**Current code to replace (lines 142-194):**

```python
def _create_required_data_chunks(
    self,
    article: Dict[str, Any],
    base_metadata: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Crea chunks para el modo required_data."""
    chunks = []
    details = article.get("details", {})
    
    # Chunk 1: Required Data Complete
    required_data = details.get("required_data", {})
    if required_data:
        content = self._format_required_data(required_data)
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="required_data",
            chunk_category="data_collection",
            tier="critical",
            topics=["data_requirements", "field_collection"]
        ))
    
    # Chunk 2: Eligibility Requirements
    # ... (keep as-is)
    
    # Chunk 3: Critical Flags
    # ... (keep as-is)
    
    return chunks
```

**New code:**

```python
def _create_required_data_chunks(
    self,
    article: Dict[str, Any],
    base_metadata: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Crea chunks granulares para el modo required_data.
    
    Genera chunks separados para cada sección de required_data:
    - must_have: Campos obligatorios del portal (para /required-data endpoint)
    - nice_to_have: Campos opcionales del mensaje (para /generate-response)
    - if_missing: Instrucciones cuando falta data (para /generate-response)
    - disambiguation_notes: Reglas de decisión (para /generate-response)
    """
    chunks = []
    details = article.get("details", {})
    required_data = details.get("required_data", {})
    
    # Chunk: Must Have fields (CRITICAL - used by /required-data endpoint)
    # These are the data points that must be extracted from the participant portal.
    must_have = required_data.get("must_have", [])
    if must_have:
        content = self._format_required_data_must_have(must_have)
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="required_data_must_have",
            chunk_category="portal_data_extraction",
            tier="critical",
            topics=["data_requirements", "must_have", "portal_data"]
        ))
    
    # Chunk: Nice to Have fields (MEDIUM - used by /generate-response endpoint)
    # These are optional data points derived from the conversation message text.
    nice_to_have = required_data.get("nice_to_have", [])
    if nice_to_have:
        content = self._format_required_data_nice_to_have(nice_to_have)
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="required_data_nice_to_have",
            chunk_category="conversation_context",
            tier="medium",
            topics=["data_requirements", "nice_to_have", "message_context"]
        ))
    
    # Chunk: If Missing instructions (HIGH - used by /generate-response endpoint)
    # These tell the AI agent what to ask the participant when data is missing.
    if_missing = required_data.get("if_missing", [])
    if if_missing:
        content = self._format_required_data_if_missing(if_missing)
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="required_data_if_missing",
            chunk_category="missing_data_handling",
            tier="high",
            topics=["data_requirements", "if_missing", "data_gaps"]
        ))
    
    # Chunk: Disambiguation Notes (MEDIUM - used by /generate-response endpoint)
    # Decision-making rules for edge cases. NOTE: Previously not chunked at all (bug).
    disambiguation = required_data.get("disambiguation_notes", [])
    if disambiguation:
        content = self._format_required_data_disambiguation(disambiguation)
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="required_data_disambiguation",
            chunk_category="decision_logic",
            tier="medium",
            topics=["data_requirements", "disambiguation", "edge_cases"]
        ))
    
    # Chunk: Eligibility Requirements (keep as-is)
    business_rules = details.get("business_rules", [])
    eligibility_rules = [
        rule for rule in business_rules 
        if rule.get("category") == "eligibility"
    ]
    if eligibility_rules:
        content = self._format_business_rules(eligibility_rules, "Eligibility")
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="eligibility",
            chunk_category="requirements",
            tier="critical",
            topics=["eligibility", "requirements"]
        ))
    
    # Chunk: Critical Flags (keep as-is)
    critical_flags = details.get("critical_flags", {})
    if critical_flags:
        content = self._format_critical_flags(critical_flags)
        chunks.append(self._create_chunk(
            content=content,
            base_metadata=base_metadata,
            chunk_type="critical_flags",
            chunk_category="validation",
            tier="critical",
            topics=["flags", "validation"]
        ))
    
    return chunks
```

### 1.2 Replace `_format_required_data` with 4 dedicated formatters (lines 196-240)

**Delete** the existing `_format_required_data` method entirely (lines 196-240) and replace with these 4 new methods:

```python
def _format_required_data_must_have(
    self,
    must_have: List[Dict[str, Any]]
) -> str:
    """Formatea must_have fields para su propio chunk."""
    lines = ["# Required Data — Must Have (Portal/Profile Data)\n"]
    lines.append("These fields MUST be collected from the participant portal or profile system.\n")
    
    for field in must_have:
        lines.append(f"### {field.get('data_point')}")
        lines.append(f"**Description:** {field.get('meaning')}")
        lines.append(f"**Why needed:** {field.get('why_needed')}")
        lines.append(f"**Source:** {field.get('source_type', 'participant_data')}")
        if field.get('example_values'):
            examples = field['example_values']
            if isinstance(examples, list):
                valid_examples = [str(ex) for ex in examples if ex is not None]
                if valid_examples:
                    lines.append(f"**Examples:** {', '.join(valid_examples)}")
            else:
                if examples is not None:
                    lines.append(f"**Example:** {examples}")
        lines.append("")
    
    return "\n".join(lines)

def _format_required_data_nice_to_have(
    self,
    nice_to_have: List[Dict[str, Any]]
) -> str:
    """Formatea nice_to_have fields para su propio chunk."""
    lines = ["# Required Data — Nice to Have (Conversation Context)\n"]
    lines.append("These fields are OPTIONAL and typically extracted from the participant's message.\n")
    
    for field in nice_to_have:
        lines.append(f"### {field.get('data_point')}")
        lines.append(f"**Description:** {field.get('meaning')}")
        lines.append(f"**Why needed:** {field.get('why_needed')}")
        lines.append(f"**Source:** {field.get('source_type', 'message_text')}")
        if field.get('example_values'):
            examples = field['example_values']
            if isinstance(examples, list):
                valid_examples = [str(ex) for ex in examples if ex is not None]
                if valid_examples:
                    lines.append(f"**Examples:** {', '.join(valid_examples)}")
        lines.append("")
    
    return "\n".join(lines)

def _format_required_data_if_missing(
    self,
    if_missing: List[Dict[str, Any]]
) -> str:
    """Formatea if_missing instructions para su propio chunk."""
    lines = ["# Required Data — If Missing (What to Ask)\n"]
    lines.append("When a required data point is not available, use these prompts to collect it.\n")
    
    for item in if_missing:
        lines.append(f"### Missing: {item.get('missing_data_point')}")
        lines.append(f"**Ask participant:** {item.get('ask_participant')}")
        if item.get('agent_note'):
            lines.append(f"**Agent note:** {item.get('agent_note')}")
        lines.append("")
    
    return "\n".join(lines)

def _format_required_data_disambiguation(
    self,
    disambiguation: List[str]
) -> str:
    """Formatea disambiguation notes para su propio chunk."""
    lines = ["# Required Data — Disambiguation Notes\n"]
    lines.append("Edge-case logic and decision rules for interpreting participant data.\n")
    
    for note in disambiguation:
        lines.append(f"- {note}")
    
    return "\n".join(lines)
```

**IMPORTANT:** Also delete the old `_format_required_data` method. It is no longer needed. Do NOT leave it as dead code.

---

## Task 2: Modify `rag_engine.py` — Update search to use `required_data_must_have`

**File:** `kb-rag-system/data_pipeline/rag_engine.py`

### 2.1 Update `_search_for_required_data` Phase 1 filter (line ~427)

The Phase 1 search currently looks for `chunk_type="required_data"`. This must change to `"required_data_must_have"` since that is the new chunk_type for must_have fields.

**Current code (line 427):**
```python
"chunk_type": {"$eq": "required_data"}
```

**New code:**
```python
"chunk_type": {"$eq": "required_data_must_have"}
```

### 2.2 Update `get_required_data` prioritize_types (line ~180)

The context building prioritizes certain chunk types. Update the priority list.

**Current code (line 180):**
```python
prioritize_types=['required_data', 'eligibility', 'business_rules']
```

**New code:**
```python
prioritize_types=['required_data_must_have', 'eligibility', 'business_rules']
```

### 2.3 No changes needed for `_search_for_response`

The `/generate-response` endpoint does NOT use the `required_data` chunk type filter — it searches by `record_keeper` + `plan_type` and relies on semantic similarity. The new `required_data_nice_to_have`, `required_data_if_missing`, and `required_data_disambiguation` chunks will be found naturally by semantic search when relevant. No changes needed.

---

## Task 3: Modify `prompts.py` — Refine the Required Data prompt

**File:** `kb-rag-system/data_pipeline/prompts.py`

### 3.1 Update `SYSTEM_PROMPT_REQUIRED_DATA` (lines 12-41)

The prompt should make it explicit that the endpoint is ONLY for extracting must-have portal/profile data fields. The LLM should not invent extra fields from business_rules context.

**Replace the entire `SYSTEM_PROMPT_REQUIRED_DATA` with:**

```python
SYSTEM_PROMPT_REQUIRED_DATA = """You are a specialized assistant for 401(k) participant advisory knowledge base.

Your task: Analyze the provided knowledge base context and determine what specific data fields are needed to properly respond to the participant's inquiry.

CRITICAL RULES:
1. Extract ONLY the data fields explicitly listed in the "Must Have" section of the context
2. These are fields that must be retrieved from the participant portal or profile system
3. Do NOT invent or add fields that are not explicitly listed in the "Must Have" section
4. Do NOT include fields from "Nice to Have" or any other section — those are handled separately
5. Categorize fields into participant_data and plan_data based on their source
6. For each field, specify:
   - field: Clear, snake_case name derived from the data point name
   - description: What this field represents (from the "Description" in context)
   - why_needed: Why we need this specific data (from "Why needed" in context)
   - data_type: One of [text, currency, date, boolean, number, list]
   - required: true (all must-have fields are required)
7. If the context contains no "Must Have" section, return empty arrays

Output must be valid JSON with this structure:
{
  "participant_data": [
    {
      "field": "field_name",
      "description": "what it is",
      "why_needed": "why we need it",
      "data_type": "text|currency|date|boolean|number|list",
      "required": true
    }
  ],
  "plan_data": [...]
}"""
```

No changes needed for `USER_PROMPT_REQUIRED_DATA_TEMPLATE` or for the generate-response prompts.

---

## Task 4: Re-upload the article to Pinecone

After completing Tasks 1-3, the article must be re-processed and re-uploaded so Pinecone has the new chunk types.

### 4.1 Verify chunks locally (dry-run)

Run from the `kb-rag-system/` directory:

```bash
cd kb-rag-system
python scripts/update_article.py "../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json" --dry-run --show-chunks
```

**Verify in the output that:**
- There are 4 new chunk types starting with `required_data_`:
  - `required_data_must_have` (tier: critical)
  - `required_data_nice_to_have` (tier: medium)
  - `required_data_if_missing` (tier: high)
  - `required_data_disambiguation` (tier: medium)
- There is NO chunk with `chunk_type="required_data"` (the old monolithic type)
- The `eligibility` and `critical_flags` chunks still exist (unchanged)
- Total chunk count should increase by ~3 (since 1 chunk becomes 4)

### 4.2 Upload to Pinecone

```bash
python scripts/update_article.py "../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json" --skip-confirmation
```

### 4.3 Verify in Pinecone

```bash
python scripts/verify_article.py "lt_request_401k_termination_withdrawal_or_rollover"
```

---

## Task 5: Test the endpoint

### 5.1 Restart the API (if not auto-reloading)

The API has `reload=True` in development, so it should auto-reload. If not:

```bash
bash scripts/start_api.sh
```

### 5.2 Send test request

Send this request to `POST /api/v1/required-data`:

```json
{
  "plan_type": "401(k)",
  "inquiry": "The user wants to rollover his 401(k) from LT Trust to Vanguard.",
  "record_keeper": "LT Trust",
  "topic": "rollover"
}
```

### 5.3 Expected response

The response should contain ONLY the 7 `must_have` fields, all with `required: true`:

| Expected Field | Data Type | Source |
|---|---|---|
| `termination_date` | date | participant_profile |
| `rehire_date` | date | participant_profile |
| `mfa_status` | text | participant_profile |
| `account_balance` | currency | participant_profile |
| `last_payroll_date` | date | agent_input |
| `participant_name` | text | participant_profile |
| `participant_status` | text | participant_profile |

**Should NOT contain** fields like `rollover_payment_method`, `wire_aba_routing_number`, `receiving_financial_institution_name`, etc. — those come from `nice_to_have` or are inferred from business_rules.

### 5.4 Verify confidence improved

The confidence should be higher than the previous 0.433 because:
- Phase 1 now searches specifically for `required_data_must_have` (smaller, more focused chunk)
- The chunk is smaller so it has better semantic alignment with the query

---

## Summary of Changes

| File | What Changes | Lines Affected |
|---|---|---|
| `data_pipeline/chunking.py` | Split `_create_required_data_chunks` into 4 chunks; replace `_format_required_data` with 4 formatters | ~142-240 |
| `data_pipeline/rag_engine.py` | Change `"required_data"` → `"required_data_must_have"` in 2 places | ~427, ~180 |
| `data_pipeline/prompts.py` | Refine system prompt to explicitly request only must-have fields | ~12-41 |
| Pinecone (via script) | Re-upload article with new chunk types | N/A (run `update_article.py`) |

### New chunk_type values

| Old | New | Tier | Endpoint |
|---|---|---|---|
| `required_data` | `required_data_must_have` | critical | `/required-data` |
| (was inside `required_data`) | `required_data_nice_to_have` | medium | `/generate-response` |
| (was inside `required_data`) | `required_data_if_missing` | high | `/generate-response` |
| (was NOT chunked — bug) | `required_data_disambiguation` | medium | `/generate-response` |

### Backward compatibility

- The old `chunk_type="required_data"` will no longer exist in Pinecone after re-upload
- The `update_article.py` script handles deletion of old chunks + upload of new ones atomically
- No API model changes needed (`RequiredDataResponse` schema stays the same)
- No changes to the `/generate-response` endpoint logic needed
