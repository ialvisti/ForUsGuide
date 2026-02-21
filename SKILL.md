---
name: fua-knowledge-base-rag
description: >
  Operational RAG system for ForUsAll 401(k) Participant Advisory knowledge base.
  Use when working with KB article JSON files, the FastAPI backend, Pinecone vector DB,
  chunking pipeline, prompt engineering, or the multi-agent integration (DevRev, n8n, ForUsBots).
  Triggers on tasks involving article processing, semantic search, metadata filtering,
  response generation, or data pipeline changes.
metadata:
  author: ForUsAll Engineering
  version: "1.0.0"
---

# FUA Knowledge Base RAG System

Operational RAG system that powers ForUsAll's Participant Advisory workflow for 401(k), 403(b), and 457 retirement plans. This is NOT a Q&A chatbot -- it is a structured, outcome-driven system integrated into a multi-agent pipeline.

## Architecture Overview

```
DevRev (CRM) -> n8n (Orchestrator) -> KB RAG API (this project) -> ForUsBots (RPA) -> DevRev AI (Final Response)
```

- **n8n** detects participant inquiries and calls the KB API
- **KB API** has two endpoints:
  - `POST /api/v1/required-data` -- identifies what data to collect from the participant portal
  - `POST /api/v1/generate-response` -- generates a contextualized, outcome-driven response
- **ForUsBots** scrapes participant portals using the required data fields
- **DevRev AI** uses the KB response to craft the final participant-facing message

## Tech Stack

- **Backend:** Python 3.12+ with FastAPI, Pydantic 2.x
- **Vector DB:** Pinecone Serverless (index: `kb-articles-production`, namespace: `kb_articles`, dimension: 1024, metric: cosine)
- **Embeddings:** Pinecone integrated embeddings (`llama-text-embed-v2`)
- **LLM:** OpenAI GPT-4o-mini (temperature: 0.1, reasoning_effort: medium)
- **Frontend:** Vanilla HTML/CSS/JavaScript (no frameworks)
- **Deployment:** Docker on Render (https://forusguide.onrender.com)
- **Testing:** pytest with pytest-asyncio

## KB Article JSON Schema (v2)

All articles follow `schema_version: kb_article_v2`. When creating or modifying articles, use this structure:

```json
{
  "metadata": {
    "article_id": "snake_case_identifier",
    "title": "Full Article Title",
    "description": "Detailed description of what this article covers",
    "audience": "Internal AI Support Agent",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    "scope": "recordkeeper-specific",
    "tags": ["tag1", "tag2"],
    "language": "en-US",
    "schema_version": "kb_article_v2",
    "transformed_at": "2025-01-15T00:00:00Z",
    "source_system": "DevRev",
    "topic": "snake_case_topic",
    "subtopics": ["subtopic_1", "subtopic_2"]
  },
  "details": {
    "critical_flags": {
      "portal_required": true,
      "mfa_relevant": false,
      "record_keeper_must_be": "LT Trust"
    },
    "business_rules": [
      {
        "category": "Category Name",
        "rules": ["Rule 1", "Rule 2"]
      }
    ],
    "required_data": {
      "must_have": [],
      "nice_to_have": [],
      "if_missing": [],
      "disambiguation_notes": []
    },
    "response_frames": {
      "can_proceed": { ... },
      "blocked_not_eligible": { ... },
      "blocked_missing_data": { ... },
      "ambiguous_plan_rules": { ... }
    }
  }
}
```

### Key rules for articles:

- `record_keeper` is `"LT Trust"` for recordkeeper-specific articles, or `null` for global articles
- `scope` must be `"global"` when `record_keeper` is null, `"recordkeeper-specific"` otherwise
- `article_id` uses snake_case (e.g., `lt_trust_termination_rollover_401k`)
- `topic` uses snake_case (e.g., `termination_distribution_request`)
- `audience` is always `"Internal AI Support Agent"` -- articles are NOT participant-facing
- Response frames are outcome-driven: `can_proceed`, `blocked_not_eligible`, `blocked_missing_data`, `ambiguous_plan_rules`

## Record Keepers

- **LT Trust** is ForUsAll's own recordkeeper. "LT Trust procedures" = "ForUsAll procedures"
- Other record keepers include: Vanguard, Fidelity, Charles Schwab
- Always filter by `record_keeper` + `plan_type` before semantic search
- Global articles (record_keeper=null) apply across all record keepers

## Multi-Tier Chunking Strategy

Chunks are prioritized by operational importance:

| Tier | Content Types | Purpose |
|------|--------------|---------|
| **CRITICAL** | required_data, decision_guide, response_frames, guardrails, critical business_rules | Must always be retrieved |
| **HIGH** | steps, fees_details, common_issues, examples | Important operational detail |
| **MEDIUM** | high_impact_faqs, examples, nice_to_have data | Supplementary context |
| **LOW** | regular_faqs, definitions, additional_notes, references | Background information |

### Chunk metadata fields stored in Pinecone:

- `article_id`, `article_title`, `record_keeper`, `plan_type`, `topic`, `subtopics`
- `chunk_type`, `chunk_category`, `chunk_tier`, `chunk_index`
- `tags`, `specific_topics`, `description`, `scope`
- `content` (the actual text), `content_hash` (for deduplication)
- `source_last_updated`, `transformed_at`

### Important: Pinecone does not index null values. Always clean metadata to remove None fields before upserting.

## API Conventions

### Endpoints

- `POST /api/v1/required-data` -- Rate limit: 60 req/min
- `POST /api/v1/generate-response` -- Rate limit: 30 req/min
- `GET /health` -- Health check
- `GET /docs` -- Swagger UI

### Authentication

- API key via `X-API-Key` header

### Decision Types (generate-response)

The system determines one of three decisions:
- `CAN_PROCEED` -- enough data to help the participant
- `UNCERTAIN` -- need more information
- `OUT_OF_SCOPE` -- outside KB coverage

### Outcome Types (response frames)

Responses are structured by outcome:
- `can_proceed` -- participant is eligible, provide steps
- `blocked_not_eligible` -- participant doesn't qualify
- `blocked_missing_data` -- need more data from portal/participant
- `ambiguous_plan_rules` -- plan rules are unclear, escalate

## Prompt Engineering Rules

- System prompts use a two-step approach for generate-response:
  1. Determine the outcome based on collected data
  2. Use the matching response frame to structure the reply
- Required-data prompts extract only "Must Have" fields categorized into `participant_data` and `plan_data`
- Always include record keeper context: "LT Trust = ForUsAll recordkeeper"
- Output must be valid JSON matching the Pydantic response models
- Temperature is kept very low (0.1) for consistency

## Code Conventions

- **Python style:** Type hints everywhere, Pydantic models for all data structures
- **Async:** FastAPI handlers and RAG engine methods are async
- **Config:** All settings via environment variables, loaded through Pydantic Settings
- **Error handling:** Structured error responses with appropriate HTTP status codes
- **Testing:** pytest with pytest-asyncio, httpx for API testing
- **Token management:** Token budgets are managed to stay within context windows

## Directory Structure

```
kb-rag-system/
  api/           -- FastAPI application (main.py, models.py, config.py, middleware.py)
  data_pipeline/ -- RAG engine, chunking, article processing, Pinecone upload, prompts
  ui/            -- Vanilla HTML/CSS/JS frontend (index.html, chunks.html)
  scripts/       -- Utility scripts for processing, verification, testing
  tests/         -- pytest test suite
  venv/          -- Python virtual environment (gitignored)

Participant Advisory/
  Distributions/  -- KB article JSON files for distribution topics
  Loans/          -- KB article JSON files for loan topics
```

## When Modifying This Project

1. **Adding a new article:** Follow the kb_article_v2 JSON schema exactly. Run through the chunking pipeline and upload to Pinecone.
2. **Changing chunking logic:** Respect the tier hierarchy. CRITICAL chunks must always be retrieved first.
3. **Modifying prompts:** Keep temperature low. Ensure output matches Pydantic models. Test with multiple record keepers.
4. **API changes:** Update Pydantic models first, then handlers. Maintain backward compatibility.
5. **UI changes:** The UI is vanilla HTML/CSS/JS -- no build step required.
6. **Metadata changes:** If adding new metadata fields, update chunking.py, models.py, and the Pinecone upsert logic. Remove None values before upserting.
