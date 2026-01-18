# RAG System Architecture - Knowledge Base API

## 📋 Table of Contents

1. [Introduction](#introduction)
2. [What is a RAG System?](#what-is-a-rag-system)
3. [General Architecture](#general-architecture)
4. [Chunking: The Heart of the System](#chunking-the-heart-of-the-system)
5. [Metadata and Filtering](#metadata-and-filtering)
6. [API Endpoints](#api-endpoints)
7. [Complete Data Flow](#complete-data-flow)
8. [Integration with Multi-Agent System](#integration-with-multi-agent-system)
9. [Production Considerations](#production-considerations)

---

## Introduction

The **KB RAG System** is a Retrieval-Augmented Generation system specifically designed to answer queries about 401(k) Participant Advisory Knowledge Base articles. It's not a traditional Q&A RAG, but an **operational RAG** that's part of a complex multi-agent system.

### Main Objective

Provide two critical functionalities:
1. **Identify what data is needed** from the participant to answer a query
2. **Generate contextualized responses** once the necessary data is available

### Use Cases

- Answer support tickets from 401(k) plan participants
- Automate collection of required information
- Provide consistent and compliance-ready responses
- Support multiple recordkeepers (LT Trust, Vanguard, etc.)
- Handle multiple inquiries in a single ticket

---

## What is a RAG System?

### RAG = Retrieval-Augmented Generation

A RAG system combines two components:

1. **Retrieval:** Searches for relevant information in a vector database
2. **Generation:** Uses an LLM to generate responses based on retrieved information

### Why RAG instead of just an LLM?

| Without RAG (LLM Only) | With RAG |
|----------------------|----------|
| ❌ Outdated information (training up to date X) | ✅ Always up-to-date information (real-time KB) |
| ❌ Hallucinations (makes up information) | ✅ Responses based on verified sources |
| ❌ Cannot access company-specific information | ✅ Access to proprietary KB |
| ❌ Inconsistent between responses | ✅ Consistent (same source → same response) |
| ❌ No compliance context | ✅ Includes guardrails and policies |

### Analogy

**Without RAG:** Like asking someone about a book they read months ago (limited memory, may confuse details)

**With RAG:** Like giving them the book open to relevant pages and asking them to answer based on those specific pages (accurate and verifiable information)

---

## General Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                    DevRev (CRM)                              │
│                  Participant Tickets                         │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                   n8n (Orchestrator)                         │
│  • Detects inquiries in ticket                              │
│  • Determines topics                                         │
│  • Calls KB API (2 times per inquiry)                       │
│  • Merges responses                                          │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│               KB RAG System (THIS PROJECT)                   │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  POST /api/v1/required-data                        │    │
│  │  • Input: inquiry + topic                          │    │
│  │  • Output: required fields (natural language)      │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  POST /api/v1/generate-response                    │    │
│  │  • Input: inquiry + topic + collected_data         │    │
│  │  • Output: response + guardrails + warnings        │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │           RAG Engine (Core Logic)                  │    │
│  │  1. Filter by metadata (record_keeper, plan)      │    │
│  │  2. Search chunks in Pinecone (semantic)           │    │
│  │  3. Rerank with bge-reranker-v2-m3                 │    │
│  │  4. Build context (respects token budget)          │    │
│  │  5. Call OpenAI GPT-4o-mini                        │    │
│  │  6. Parse and structure response                   │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              Pinecone Vector Database                        │
│                                                              │
│  • ~280 articles × ~30 chunks = ~8,400 vectors              │
│  • Embeddings: llama-text-embed-v2 (integrated)             │
│  • Enriched metadata for filtering                          │
│  • Namespace: kb_articles                                    │
└──────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                  ForUsBots (RPA)                             │
│  • Receives list of required fields                          │
│  • Scrapes participant portal                                │
│  • Returns data to n8n                                       │
└──────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│               DevRev AI (Final Generator)                    │
│  • Receives KB API responses (merged)                        │
│  • Generates final response to participant                   │
│  • Decides ticket action (close, escalate, etc.)            │
└──────────────────────────────────────────────────────────────┘
```

---

## Chunking: The Heart of the System

### What is Chunking?

**Chunking** is the process of dividing a large document into smaller, semantically coherent fragments (chunks).

### Why is it Necessary?

#### Problem without Chunking

Imagine you have a 5,000-word article about "How to Request a 401(k) Distribution":

- **Imprecise search:** If you search "How much does it cost?", the system returns the ENTIRE article
- **Token waste:** The LLM receives irrelevant information (steps, FAQs, etc.) when it only needs the fees section
- **Lower quality:** The LLM gets "distracted" by irrelevant information
- **Inefficient:** You pay to process thousands of unnecessary tokens

#### Solution with Chunking

The same article divided into ~33 specific chunks:

- **Chunk 1:** Required data (necessary fields)
- **Chunk 2:** Eligibility rules
- **Chunk 3:** Fees details
- **Chunk 4:** Steps 1-3 (first steps)
- **Chunk 5:** Steps 4-6 (intermediate steps)
- ... and so on

**Result:**
- ✅ Precise search: "How much does it cost?" → Only returns Chunk 3 (fees)
- ✅ Efficiency: LLM receives only 200 words instead of 5,000
- ✅ Higher quality: Focused and precise response
- ✅ Lower cost: 95% fewer tokens processed

### Implemented Chunking Strategy

Our system uses a **multi-tier usage-based strategy**:

#### Design Principle

Not all chunks are equal. Some are **critical** and always needed, others are **optional** and only included if there's space.

#### Priority Tiers

```
┌──────────────────────────────────────────────────────────┐
│  TIER CRITICAL (9 chunks)                                │
│  Always retrieved, regardless of token budget            │
│  ------------------------------------------------        │
│  • required_data (for /required-data)                   │
│  • decision_guide (to determine outcome)                │
│  • response_frames (response templates)                 │
│  • guardrails (what NOT to say)                         │
│  • critical business_rules (fees, eligibility, taxes)   │
└──────────────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│  TIER HIGH (10 chunks)                                   │
│  Retrieved if token budget available                     │
│  ------------------------------------------------        │
│  • steps (detailed procedures)                          │
│  • fees_details (cost breakdown)                        │
│  • common_issues (troubleshooting)                      │
│  • examples (specific use cases)                        │
└──────────────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│  TIER MEDIUM (5 chunks)                                  │
│  Useful but not essential information                    │
│  ------------------------------------------------        │
│  • high_impact_faqs (top frequent questions)            │
│  • examples (additional scenarios)                      │
└──────────────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│  TIER LOW (9 chunks)                                     │
│  Filler information, only if lots of space left         │
│  ------------------------------------------------        │
│  • regular_faqs (frequent questions)                    │
│  • definitions (term glossary)                          │
│  • additional_notes (supplementary notes)               │
│  • references (links and contacts)                      │
└──────────────────────────────────────────────────────────┘
```

### Chunk Types by Endpoint

#### For `/required-data` (Mode A)

**Objective:** Identify what data we need from the participant

**Retrieved chunks:**
- `required_data` - Complete list of fields (must_have, nice_to_have)
- `eligibility` - Eligibility rules to validate if it proceeds
- `critical_flags` - Special flags (portal_required, etc.)

**Example chunk content:**

```markdown
# Required Data for This Process

## Must Have (Required):

### Confirmation participant has left employer
**Description:** The participant confirms they are separated from service
**Why needed:** Determines this is a termination distribution
**Data type:** message_text
**Example:** "I have left my employer and want to withdraw my 401(k)"

### Requested transaction type  
**Description:** Cash withdrawal, full rollover, or partial rollover + cash
**Why needed:** Determines portal options and delivery requirements
**Data type:** message_text
**Examples:** "Lump Sum Cash", "Full Rollover", "Partial Rollover + Cash"

### Email address for confirmations
**Description:** Valid email for confirmation updates
**Why needed:** Required for portal submission
**Data type:** agent_input

[... more fields ...]
```

#### For `/generate-response` (Mode B)

**Objective:** Generate contextualized response with collected data

**Retrieved chunks (by tier, according to budget):**

**Tier Critical:**
- `decision_guide` - Determines if can proceed, is blocked, etc.
- `response_frames` - Response templates by outcome
- `guardrails` - What the agent must NOT say
- `business_rules` - Rules for fees, eligibility, taxes

**Tier High:**
- `steps` - Detailed procedure steps
- `fees_details` - Complete cost breakdown
- `common_issues` - Common problem resolution

**Tier Medium/Low:**
- `examples` - Specific use cases
- `faqs` - Frequently asked questions
- `definitions` - Glossary

**Example chunk content:**

```markdown
# Response Frames by Outcome

## Outcome: can_proceed

### Message Components:
- You can request a termination distribution in the ForUsAll portal
- A $75 distribution fee applies to all requests
- An additional $35 wire fee applies if you choose wire transfer

### Next Steps:
- Log in to https://account.forusall.com/login
- Navigate to Loans & Distributions
- Select Separation of Service as reason

### Warnings:
- 20% federal withholding applies to cash distributions
- Wire fees are non-refundable

### Do NOT Say:
- Exact delivery date guarantees
- That wire fees can be refunded
- That unvested funds can be distributed
```

### Semantic Grouping

Chunks are not divided arbitrarily by size, but **semantically**:

#### ❌ Bad: Division by Size

```
Chunk 1: First 500 characters of article
Chunk 2: Next 500 characters
Chunk 3: Next 500 characters
```

**Problem:** A chunk might start in the middle of a business rule or step, losing context.

#### ✅ Good: Semantic Division

```
Chunk 1: Business Rules - Fees (complete)
Chunk 2: Business Rules - Eligibility (complete)
Chunk 3: Business Rules - Tax Withholding (complete)
Chunk 4: Steps 1-3 (complete initial procedure)
Chunk 5: Steps 4-6 (complete intermediate procedure)
```

**Advantage:** Each chunk is a **complete unit of meaning**.

---

## Metadata and Filtering

### Why Metadata?

Metadata allows **filtering chunks before semantic search**, making the system more precise and efficient.

### Metadata Included in Each Chunk

```json
{
  "id": "lt_request_401k_withdrawal_chunk_5",
  "content": "# Business Rules: Fees...",
  "metadata": {
    // Article Metadata
    "article_id": "lt_request_401k_termination_withdrawal_or_rollover",
    "article_title": "LT: How to Request a 401(k) Termination...",
    "record_keeper": "LT Trust",           // ← CRITICAL FILTER
    "plan_type": "401(k)",                 // ← CRITICAL FILTER
    "scope": "recordkeeper-specific",
    "tags": ["Distribution", "Withdrawal", "Taxes"],
    "topic": "distribution",               // ← For routing
    "subtopics": ["termination_distribution", "rollover", "cash_withdrawal"],
    
    // Chunk Metadata
    "chunk_type": "business_rules",        // ← For endpoint routing
    "chunk_category": "fees",              // ← Specific subcategory
    "chunk_index": 5,                      // ← Order within article
    "chunk_tier": "critical",              // ← For prioritization
    
    // For Advanced Search
    "specific_topics": ["fees", "costs", "charges"],
    "content_hash": "a3f2d8c1"            // ← For deduplication
  }
}
```

### Filtering Strategy

#### MANDATORY Filters (always applied)

```python
# Before doing semantic search, filter:
filter = {
    "record_keeper": {"$eq": "LT Trust"},  # Only LT Trust articles
    "plan_type": {"$eq": "401(k)"}         # Only 401(k) plans
}
```

**Why?** Prevents articles from other recordkeepers (Vanguard, Fidelity) from contaminating results.

#### SOFT Filters (prefer but not require)

```python
# Prefer chunks that match the topic
preferred_filter = {
    "topic": {"$eq": "distribution"},
    "subtopics": {"$in": ["rollover", "cash_withdrawal"]}
}
```

**Why?** If there's no exact match, can search in related topics.

#### Result Prioritization

When multiple chunks match:

```
Priority 1: record_keeper + plan_type + topic + subtopic (Exact match)
Priority 2: record_keeper + plan_type + topic (Specific match)
Priority 3: plan_type + topic, scope="general" (General match)
Priority 4: topic only (Fallback with disclaimer)
```

**Example:**

Query: "What fees apply to LT Trust 401k withdrawals?"

```
Search with filters:
  record_keeper = "LT Trust"
  plan_type = "401(k)"
  topic = "distribution"
  subtopics contains "withdrawal"

Results ordered by:
1. LT Trust, 401(k), distribution, fees chunk → 100% match
2. LT Trust, 401(k), distribution, general chunk → 90% match
3. General, 401(k), distribution, fees chunk → 70% match
```

---

## API Endpoints

### Endpoint 1: `/api/v1/required-data`

**Purpose:** Identify what data we need from the participant to answer their query.

#### Request

```json
POST /api/v1/required-data
Content-Type: application/json
X-API-Key: <your-api-key>

{
  "inquiry": "Participant wants to rollover remaining 401k balance to Fidelity",
  "topic": "rollover",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "related_inquiries": [
    "How to close ForUsAll account"
  ]
}
```

#### Response

```json
{
  "article_reference": {
    "article_id": "lt_rollover_to_ira",
    "title": "LT: How to Complete a Rollover",
    "confidence": 0.95
  },
  
  "required_fields": {
    "participant_data": [
      {
        "field": "Current account balance",
        "description": "Total current balance in the ForUsAll 401(k)",
        "why_needed": "To determine if there are funds available to rollover",
        "data_type": "currency",
        "required": true
      },
      {
        "field": "Vested balance",
        "description": "Amount that is vested (eligible for distribution)",
        "why_needed": "Only vested amounts can be rolled over",
        "data_type": "currency",
        "required": true
      },
      {
        "field": "Employment status",
        "description": "Current status (terminated, active, etc.)",
        "why_needed": "Must be terminated to request distribution",
        "data_type": "string",
        "required": true
      }
    ],
    
    "plan_data": [
      {
        "field": "Plan status",
        "description": "Whether plan is active, terminated, or in blackout",
        "why_needed": "Distributions cannot be processed during blackout",
        "data_type": "string",
        "required": true
      },
      {
        "field": "Distribution fees",
        "description": "Fees that apply to distributions",
        "why_needed": "To inform participant of costs",
        "data_type": "object",
        "required": false
      }
    ]
  },
  
  "metadata": {
    "total_fields": 5,
    "critical_fields": 3,
    "estimated_complexity": "medium"
  }
}
```

#### Internal Flow

```
1. Receives request with inquiry + topic + record_keeper
2. Filters chunks by metadata:
   - record_keeper = "LT Trust"
   - plan_type = "401(k)"  
   - chunk_type = "required_data" | "eligibility" | "critical_flags"
3. Semantic search in Pinecone (top 5-10 chunks)
4. Rerank chunks
5. Build context with relevant chunks
6. LLM generates structured JSON response
7. Parse and return required_fields
```

---

### Endpoint 2: `/api/v1/generate-response`

**Purpose:** Generate contextualized response once we have the participant's data.

#### Request

```json
POST /api/v1/generate-response
Content-Type: application/json
X-API-Key: <your-api-key>

{
  "inquiry": "Participant wants to rollover $1,993.84 to Fidelity 401k",
  "topic": "rollover",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "related_inquiries": ["How to close account"],
  
  "collected_data": {
    "participant_data": {
      "current_balance": "$1,993.84",
      "vested_balance": "$1,993.84",
      "employment_status": "terminated"
    },
    "plan_data": {
      "plan_status": "active",
      "distribution_fees": {
        "base_fee": "$75",
        "wire_fee": "$35"
      }
    }
  },
  
  "context": {
    "max_response_tokens": 1500,
    "total_inquiries_in_ticket": 2
  }
}
```

#### Response

```json
{
  "inquiry_id": "auto-generated-uuid",
  
  "primary_source": {
    "article_id": "lt_rollover_process",
    "title": "LT: How to Complete a Rollover",
    "record_keeper": "LT Trust",
    "specificity": "recordkeeper-specific"
  },
  
  "decision": "can_proceed",
  "confidence": 0.97,
  
  "response": {
    "sections": [
      {
        "topic": "rollover_process",
        "answer_components": [
          "You can rollover the remaining $1,993.84 to your Fidelity 401(k)",
          "Log in to the ForUsAll portal and go to Loans & Distributions",
          "Select Rollover and provide your Fidelity account details",
          "A $75 distribution fee applies ($35 additional if you choose wire)"
        ],
        "steps": [
          "Log in to https://account.forusall.com/login",
          "Navigate to Loans & Distributions",
          "Select 'Rollover' as distribution type",
          "Enter Fidelity account information",
          "Review and submit request"
        ],
        "warnings": [
          "Distribution fee ($75) is non-refundable",
          "Wire fee ($35) is non-refundable if wire is chosen",
          "Verify Fidelity account details to avoid rejection"
        ]
      }
    ]
  },
  
  "guardrails": {
    "must_not_say": [
      "Exact delivery date guarantees",
      "That wire fees can be refunded",
      "That unvested amounts can be rolled over"
    ],
    "must_verify": [
      "Receiving institution details are correct"
    ]
  },
  
  "metadata": {
    "confidence": 0.97,
    "sources_used": ["business_rules.fees", "steps.1-5"],
    "token_count": 487,
    "processing_time_ms": 1250
  }
}
```

#### Internal Flow

```
1. Receives request with inquiry + topic + collected_data
2. Determines token budget (1500 tokens for 2 inquiries)
3. Filters chunks by metadata:
   - record_keeper = "LT Trust"
   - plan_type = "401(k)"
   - topic = "distribution"
4. Semantic search in Pinecone
5. Retrieves chunks by tier (until budget filled):
   - Tier CRITICAL: always
   - Tier HIGH: if fits
   - Tier MEDIUM/LOW: only if space left
6. Rerank retrieved chunks
7. Build optimized context
8. LLM generates response using specific prompt
9. Parse and structure response
10. Return JSON with response + guardrails + metadata
```

---

## Complete Data Flow

### Use Case: Ticket with 2 Inquiries

**Original Ticket:**
> "I want to rollover my 401k to Fidelity. Also want to close my account after."

#### Phase 1: Analysis (n8n)

```
AI Analyzer detects:
  - Inquiry 1: "Rollover to Fidelity" → topic: "rollover"
  - Inquiry 2: "Close account" → topic: "account_closure"
```

#### Phase 2: Data Collection (Sequential)

```
┌─ Inquiry 1: Rollover ─┐
│                        │
│ KB API /required-data  │ → Returns: ["current_balance", "vested_balance", 
│                        │              "employment_status", "plan_status"]
└────────────────────────┘
           │
           ▼
┌─ Inquiry 2: Account Closure ─┐
│                               │
│ KB API /required-data         │ → Returns: ["pending_distributions",
│                               │              "final_balance"]
└───────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ n8n MERGES required fields  │ → Consolidated list (no duplicates)
│ (deduplication)             │
└─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ AI Mapper translates to     │ → ["participant_data.balance",
│ ForUsBots fields            │    "participant_data.vesting",
│                             │    "plan_data.status"]
└─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ ForUsBots scrapes portal    │ → Gets real data
└─────────────────────────────┘
```

#### Phase 3: Response Generation (Sequential)

```
┌─ Inquiry 1: Rollover ─────────┐
│                                │
│ KB API /generate-response      │ → Response about rollover process
│ + collected_data               │   + fees + timelines + warnings
│                                │
│ Token budget: 1500 tokens      │
└────────────────────────────────┘
           │
           ▼
┌─ Inquiry 2: Account Closure ──┐
│                                │
│ KB API /generate-response      │ → Response about account closure
│ + collected_data               │   + what happens next + timelines
│                                │
│ Token budget: 1500 tokens      │
└────────────────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ n8n PACKAGES responses         │ → Consolidated bundle
│ (kb_bundle_v1)                 │   (shared context + inquiries)
└────────────────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ DevRev AI processes bundle     │ → Generates unified response
│ (context window: 4000 tokens)  │   + decides ticket action
└────────────────────────────────┘
```

#### Token Budget Management

```
Ticket with 2 inquiries:
  - Response 1: 1500 tokens max
  - Response 2: 1500 tokens max
  - Merge overhead: ~200 tokens
  ─────────────────────────────
  Total: ~3200 tokens (< 4000 DevRev AI limit)
```

---

## Integration with Multi-Agent System

### System Actors

```
DevRev (CRM) 
  ↓ triggers workflow
n8n (Orchestrator)
  ↓ queries x2
KB API (THIS SYSTEM)
  ↓ indicates required fields
AI Mapper
  ↓ translates to endpoints
ForUsBots (RPA)
  ↓ returns data
n8n (merges)
  ↓ queries x2 with data
KB API (responses)
  ↓ packages
n8n (bundle)
  ↓ sends bundle
DevRev AI (final decision)
```

### Responsibilities of Each Actor

#### DevRev (CRM)
- Receives participant tickets
- Triggers n8n workflow
- Receives final response and action

#### n8n (Orchestrator)
- Detects inquiries in ticket (with AI)
- Determines topics per inquiry
- Calls KB API (2 times per inquiry)
- Merges required_fields (deduplication)
- Calls AI Mapper
- Calls ForUsBots
- Merges responses into bundle
- Sends bundle to DevRev AI

#### KB API (This System)
- **DOES NOT** detect inquiries (n8n does)
- **DOES NOT** scrape data (ForUsBots does)
- **DOES NOT** decide CRM actions (DevRev AI does)
- **DOES** return what data is needed (natural language)
- **DOES** generate contextualized responses
- **DOES** include guardrails and warnings
- **DOES** respect token budgets

#### AI Mapper
- Translates natural language fields to ForUsBots fields
- Determines which endpoints to call (participant_data, plan_data)
- Builds payloads for ForUsBots

#### ForUsBots (RPA)
- Scrapes participant portal
- Returns structured data
- Doesn't interpret or decide, just extracts

#### DevRev AI
- Receives KB API bundle
- Generates final participant response
- Decides action (close ticket, escalate, create issue)
- Has ~4000 token context window

---

## Production Considerations

### Performance

- **Target latency:** < 2 seconds per request
- **Throughput:** ~10 requests/second
- **Caching:** Consider caching frequent chunks

### Scalability

- **Articles:** Designed for ~280, scalable to thousands
- **Chunks per article:** ~30-35
- **Total vectors:** ~8,400 (scalable to millions with Pinecone)

### Monitoring

- Confidence scores per response
- Token usage per request
- Pinecone and OpenAI latencies
- Error rates per endpoint

### Estimated Costs (Monthly)

```
Pinecone (Starter): ~$70/month
OpenAI API (GPT-4o-mini): ~$30-50/month (moderate usage)
Render (Deployment): ~$7-25/month
─────────────────────────────
Total: ~$110-150/month
```

### Maintenance

- **Article updates:** Automatic pipeline (see PIPELINE_GUIDE.md)
- **New articles:** Same pipeline
- **JSON structure changes:** Requires adjustment in chunking.py

---

## Next Steps

1. Create Pinecone index
2. Process and upload existing articles
3. Implement RAG engine with search and reranking
4. Create FastAPI endpoints
5. Testing with real tickets
6. Deploy to production

---

**Complete Documentation:** Also see `PIPELINE_GUIDE.md` for instructions on processing new articles.
