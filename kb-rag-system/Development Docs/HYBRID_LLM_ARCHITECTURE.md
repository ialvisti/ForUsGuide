# Hybrid LLM Architecture: OpenAI + Gemini

## Table of Contents

1. [Overview](#overview)
2. [Why a Hybrid Approach](#why-a-hybrid-approach)
3. [Architecture Design](#architecture-design)
4. [LLM Router Pattern](#llm-router-pattern)
5. [Routing Table](#routing-table)
6. [Implementation Guide](#implementation-guide)
7. [Gemini SDK Setup](#gemini-sdk-setup)
8. [Configuration Reference](#configuration-reference)
9. [Cost Analysis](#cost-analysis)
10. [Migration Checklist](#migration-checklist)

---

## Overview

This document describes the hybrid LLM architecture that routes different RAG engine tasks to optimal models across OpenAI and Google Gemini, maximizing quality on critical calls while minimizing cost on routine ones.

**Current state:** OpenAI routes use GPT-5.5 with reasoning (`medium` effort, 16K min completion tokens).

**Target state:** An LLM Router directs each task type to the best-fit model:

- **GPT-5.5** for critical eligibility reasoning (outcome determination)
- **Gemini 2.5 Flash** (thinking) for structured extraction and response generation
- Automatic fallback chains for reliability

---

## Why a Hybrid Approach

### Cost breakdown per `generate_response` call (current)

| LLM Call | Purpose | Est. Tokens (in+out) | Est. Cost |
|----------|---------|---------------------|-----------|
| `_decompose_question()` | Break inquiry into sub-queries | ~2K + 16K | ~$0.16 |
| Phase 1: `gr_outcome` | Determine eligibility outcome | ~5K + 16K | ~$0.30 |
| Phase 2: `gr_response` | Generate structured response | ~8K + 16K | ~$0.24 |
| **Total** | | | **~$0.70** |

At 100 requests/day = **~$2,100/month** just for LLM on this endpoint.

### Not all calls need GPT-5.5

- **`_decompose_question()`**: Simple task — break a question into 1-3 sub-queries. Any capable model handles this. Using GPT-5.5 with 16K reasoning budget is extreme overkill.
- **Phase 2 (`gr_response`)**: The outcome is already determined. The model follows a prescriptive schema with explicit content rules. Flash-tier thinking models handle this well.
- **Phase 1 (`gr_outcome`)**: This IS the critical call. An incorrect outcome (e.g., `can_proceed` when participant is blocked) has direct business impact. Keep the strongest model here.

---

## Architecture Design

```
                    ┌──────────────────────────────────┐
                    │           RAG Engine              │
                    │                                    │
                    │  _decompose_question()             │
                    │  get_required_data()               │
                    │  generate_response() Phase 1       │
                    │  generate_response() Phase 2       │
                    │  ask_knowledge_question()           │
                    └──────────────┬───────────────────┘
                                   │
                          task_type + prompts
                                   │
                    ┌──────────────▼───────────────────┐
                    │          LLM Router               │
                    │                                    │
                    │  1. Look up route for task_type    │
                    │  2. Call primary provider/model     │
                    │  3. On failure → call fallback     │
                    │  4. Return response + metadata     │
                    └──────┬───────────────┬───────────┘
                           │               │
                    ┌──────▼─────┐  ┌──────▼──────┐
                    │  OpenAI    │  │   Gemini     │
                    │  Provider  │  │   Provider   │
                    │            │  │              │
                    │ AsyncOpenAI│  │ genai.Client │
                    │            │  │  (aio)       │
                    │ - GPT-5.5  │  │ - 2.5 Flash  │
                    │ - GPT-4o-m │  │ - 2.5 Pro    │
                    └────────────┘  └──────────────┘
```

---

## LLM Router Pattern

### Core classes

```python
# data_pipeline/llm_router.py

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, NamedTuple
import logging

from openai import AsyncOpenAI
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class LLMProvider(Enum):
    OPENAI = "openai"
    GEMINI = "gemini"


class LLMResponse(NamedTuple):
    """Unified response from any LLM provider."""
    content: str
    usage: Optional[Dict[str, int]]
    provider_used: str   # "openai" or "gemini"
    model_used: str      # actual model name


@dataclass
class ModelConfig:
    """Configuration for a specific model."""
    provider: LLMProvider
    model: str
    temperature: float = 0.1
    reasoning_effort: Optional[str] = None  # OpenAI GPT-5 only
    thinking_budget: Optional[int] = None   # Gemini thinking models only
    max_completion_floor: int = 0           # minimum max_completion_tokens


@dataclass
class TaskRoute:
    """Routing config for a task type: primary model + optional fallback."""
    primary: ModelConfig
    fallback: Optional[ModelConfig] = None
```

### The Router

```python
class LLMRouter:
    """Routes LLM calls to optimal providers based on task type."""

    # GPT-5 reasoning models need extra headroom for reasoning tokens
    GPT5_REASONING_MULTIPLIER = 10
    GPT5_MIN_COMPLETION_TOKENS = 16000

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        use_vertex_ai: bool = False,
        gcp_project: Optional[str] = None,
        gcp_location: str = "us-central1",
    ):
        # OpenAI client
        self._openai_client: Optional[AsyncOpenAI] = None
        if openai_api_key:
            self._openai_client = AsyncOpenAI(api_key=openai_api_key)

        # Gemini client (Google AI or Vertex AI)
        self._gemini_client = None
        if use_vertex_ai and gcp_project:
            self._gemini_client = genai.Client(
                vertexai=True,
                project=gcp_project,
                location=gcp_location,
            )
        elif gemini_api_key:
            self._gemini_client = genai.Client(api_key=gemini_api_key)

        self._routes: Dict[str, TaskRoute] = {}

    def configure_routes(self, routes: Dict[str, TaskRoute]):
        """Set the routing table."""
        self._routes = routes
        for task, route in routes.items():
            fb = f" -> fallback: {route.fallback.model}" if route.fallback else ""
            logger.info(f"LLM route: {task} -> {route.primary.model}{fb}")

    async def call(
        self,
        task_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        """
        Route an LLM call based on task type.

        Tries the primary model first. On any exception, falls back
        to the secondary model (if configured). Raises if both fail.
        """
        route = self._routes.get(task_type)
        if not route:
            raise ValueError(f"No route configured for task_type={task_type}")

        # Try primary
        try:
            return await self._dispatch(route.primary, system_prompt, user_prompt, max_tokens)
        except Exception as e:
            if not route.fallback:
                raise
            logger.warning(
                f"[{task_type}] Primary ({route.primary.model}) failed: {e}. "
                f"Falling back to {route.fallback.model}"
            )

        # Try fallback
        return await self._dispatch(route.fallback, system_prompt, user_prompt, max_tokens)

    async def _dispatch(
        self,
        config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        """Dispatch to the correct provider."""
        if config.provider == LLMProvider.OPENAI:
            return await self._call_openai(config, system_prompt, user_prompt, max_tokens)
        elif config.provider == LLMProvider.GEMINI:
            return await self._call_gemini(config, system_prompt, user_prompt, max_tokens)
        else:
            raise ValueError(f"Unknown provider: {config.provider}")
```

### OpenAI provider method

```python
    async def _call_openai(
        self,
        config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        """Call OpenAI chat completions API."""
        if not self._openai_client:
            raise RuntimeError("OpenAI client not configured")

        is_gpt5 = "gpt-5" in config.model.lower()

        params = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        if is_gpt5:
            scaled = max(
                max_tokens * self.GPT5_REASONING_MULTIPLIER,
                self.GPT5_MIN_COMPLETION_TOKENS,
                config.max_completion_floor,
            )
            params["max_completion_tokens"] = scaled
            if config.reasoning_effort:
                params["reasoning_effort"] = config.reasoning_effort
        else:
            params["max_tokens"] = max_tokens
            params["temperature"] = config.temperature

        response = await self._openai_client.chat.completions.create(**params)
        content = response.choices[0].message.content
        usage_obj = response.usage

        if not content or not content.strip():
            raise RuntimeError(f"OpenAI returned empty content (finish_reason={response.choices[0].finish_reason})")

        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
        } if usage_obj else None

        return LLMResponse(
            content=content,
            usage=usage,
            provider_used="openai",
            model_used=config.model,
        )
```

### Gemini provider method

```python
    async def _call_gemini(
        self,
        config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        """Call Google Gemini API (Google AI or Vertex AI)."""
        if not self._gemini_client:
            raise RuntimeError("Gemini client not configured")

        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=config.temperature,
            max_output_tokens=max_tokens,
        )

        # Enable thinking for models that support it
        if config.thinking_budget and config.thinking_budget > 0:
            gen_config.thinking_config = types.ThinkingConfig(
                thinking_budget=config.thinking_budget
            )

        response = await self._gemini_client.aio.models.generate_content(
            model=config.model,
            contents=user_prompt,
            config=gen_config,
        )

        content = response.text
        if not content or not content.strip():
            raise RuntimeError("Gemini returned empty content")

        usage = None
        if response.usage_metadata:
            um = response.usage_metadata
            usage = {
                "prompt_tokens": um.prompt_token_count or 0,
                "completion_tokens": um.candidates_token_count or 0,
                "total_tokens": um.total_token_count or 0,
            }

        return LLMResponse(
            content=content,
            usage=usage,
            provider_used="gemini",
            model_used=config.model,
        )
```

---

## Routing Table

### Recommended default configuration

```python
DEFAULT_ROUTES = {
    "decompose": TaskRoute(
        primary=ModelConfig(
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-flash",
            temperature=0.1,
            thinking_budget=0,  # no thinking needed
        ),
        fallback=ModelConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-4o-mini",
            temperature=0.1,
        ),
    ),
    "required_data": TaskRoute(
        primary=ModelConfig(
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-flash",
            temperature=0.1,
            thinking_budget=4096,
        ),
        fallback=ModelConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-5.5",
            reasoning_effort="medium",
        ),
    ),
    "gr_outcome": TaskRoute(
        primary=ModelConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-5.5",
            reasoning_effort="medium",
        ),
        fallback=ModelConfig(
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-pro",
            temperature=0.1,
            thinking_budget=8192,
        ),
    ),
    "gr_response": TaskRoute(
        primary=ModelConfig(
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-flash",
            temperature=0.1,
            thinking_budget=8192,
        ),
        fallback=ModelConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-5.5",
            reasoning_effort="medium",
        ),
    ),
    "knowledge_question": TaskRoute(
        primary=ModelConfig(
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-flash",
            temperature=0.1,
            thinking_budget=8192,
        ),
        fallback=ModelConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-5.5",
            reasoning_effort="medium",
        ),
    ),
}
```

### Easy override via environment variables

Every route can be overridden with env vars. The config reads these and builds the routes:

```bash
# Override any route to use a different model
LLM_ROUTE_DECOMPOSE=gemini-2.5-flash
LLM_ROUTE_REQUIRED_DATA=gemini-2.5-flash
LLM_ROUTE_GR_OUTCOME=gpt-5.5          # keep strongest model here
LLM_ROUTE_GR_RESPONSE=gemini-2.5-flash
LLM_ROUTE_KNOWLEDGE=gemini-2.5-flash

# To test Gemini Pro on outcome determination:
LLM_ROUTE_GR_OUTCOME=gemini-2.5-pro
```

---

## Implementation Guide

### Step 1: Add dependency

```txt
# requirements.txt
google-genai>=1.0.0
```

The `google-genai` package is Google's unified Gen AI SDK. It supports both Google AI API (API key) and Vertex AI (IAM) behind the same interface.

### Step 2: Create `data_pipeline/llm_router.py`

Use the full implementation from [LLM Router Pattern](#llm-router-pattern) above.

### Step 3: Update `api/config.py`

Add these settings to the `Settings` class:

```python
class Settings(BaseSettings):
    # ... existing settings ...

    # Gemini / Vertex AI
    GEMINI_API_KEY: str = ""
    GCP_PROJECT: str = ""
    GCP_LOCATION: str = "us-central1"
    USE_VERTEX_AI: bool = False  # True in production (GCP), False for local dev

    # LLM Routing (model names — provider is inferred from prefix)
    LLM_ROUTE_DECOMPOSE: str = "gemini-2.5-flash"
    LLM_ROUTE_REQUIRED_DATA: str = "gemini-2.5-flash"
    LLM_ROUTE_GR_OUTCOME: str = "gpt-5.5"
    LLM_ROUTE_GR_RESPONSE: str = "gemini-2.5-flash"
    LLM_ROUTE_KNOWLEDGE: str = "gemini-2.5-flash"
```

Update `validate_settings()`:

```python
def validate_settings():
    errors = []

    if not settings.API_KEY:
        errors.append("API_KEY not configured")

    if not settings.PINECONE_API_KEY:
        errors.append("PINECONE_API_KEY not configured")

    # At least one LLM provider must be configured
    has_openai = bool(settings.OPENAI_API_KEY)
    has_gemini = bool(settings.GEMINI_API_KEY) or settings.USE_VERTEX_AI

    if not has_openai and not has_gemini:
        errors.append("At least one LLM provider required (OPENAI_API_KEY or GEMINI_API_KEY/USE_VERTEX_AI)")

    # Validate that routed models have their provider key
    routes = {
        "decompose": settings.LLM_ROUTE_DECOMPOSE,
        "required_data": settings.LLM_ROUTE_REQUIRED_DATA,
        "gr_outcome": settings.LLM_ROUTE_GR_OUTCOME,
        "gr_response": settings.LLM_ROUTE_GR_RESPONSE,
        "knowledge": settings.LLM_ROUTE_KNOWLEDGE,
    }
    for task, model in routes.items():
        if model.startswith("gpt-") and not has_openai:
            errors.append(f"Route {task} uses {model} but OPENAI_API_KEY is missing")
        if model.startswith("gemini-") and not has_gemini:
            errors.append(f"Route {task} uses {model} but Gemini is not configured")

    if errors:
        raise ValueError(f"Invalid configuration: {', '.join(errors)}")
    return True
```

### Step 4: Refactor `data_pipeline/rag_engine.py`

**Constructor change:**

```python
class RAGEngine:
    def __init__(
        self,
        llm_router: 'LLMRouter',
        pinecone_uploader: Optional['PineconeUploader'] = None,
    ):
        self.router = llm_router
        self.pinecone = pinecone_uploader or PineconeUploader()
        self.token_manager = TokenManager(model="gpt-4")
        self._search_cache = TTLCache(maxsize=self.CACHE_MAX_SIZE, ttl=self.CACHE_TTL_SECONDS)
```

**Remove from `RAGEngine`:**

- `self.openai_client`, `self.model`, `self.temperature`, `self.reasoning_effort`, `self.is_gpt5`
- `GPT5_REASONING_MULTIPLIER`, `GPT5_MIN_COMPLETION_TOKENS`
- `LLM_EMPTY_RESPONSE_RETRIES` (move retry logic into the router)

**`_call_llm()` changes:**

```python
async def _call_llm(
    self,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    task_type: str,  # NEW parameter
) -> LLMResponse:
    return await self.router.call(
        task_type=task_type,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
    )
```

**Update every call site to pass `task_type`:**

| Method | Call | task_type |
|--------|------|-----------|
| `_decompose_question()` | `self._call_llm(..., task_type="decompose")` | `"decompose"` |
| `get_required_data()` | `self._call_llm(..., task_type="required_data")` | `"required_data"` |
| `generate_response()` Phase 1 | `self._call_llm(..., task_type="gr_outcome")` | `"gr_outcome"` |
| `generate_response()` Phase 2 | `self._call_llm(..., task_type="gr_response")` | `"gr_response"` |
| `ask_knowledge_question()` | `self._call_llm(..., task_type="knowledge_question")` | `"knowledge_question"` |

**Metadata enrichment:**

Every response already includes a `metadata` dict. Add `provider_used` and `model_used`:

```python
metadata={
    # ... existing fields ...
    "provider_used": llm_result.provider_used,
    "model_used": llm_result.model_used,
}
```

### Step 5: Update `api/main.py` lifespan

```python
from data_pipeline.llm_router import LLMRouter, build_routes_from_settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()

    app.state.pinecone_uploader = PineconeUploader(
        api_key=settings.PINECONE_API_KEY,
        index_name=settings.INDEX_NAME,
        namespace=settings.NAMESPACE,
    )

    # Build LLM Router
    router = LLMRouter(
        openai_api_key=settings.OPENAI_API_KEY or None,
        gemini_api_key=settings.GEMINI_API_KEY or None,
        use_vertex_ai=settings.USE_VERTEX_AI,
        gcp_project=settings.GCP_PROJECT or None,
        gcp_location=settings.GCP_LOCATION,
    )
    router.configure_routes(build_routes_from_settings(settings))

    app.state.rag_engine = RAGEngine(
        llm_router=router,
        pinecone_uploader=app.state.pinecone_uploader,
    )

    logger.info("RAG Engine initialized with hybrid LLM routing")
    yield
```

### Step 6: Add `build_routes_from_settings()` helper

This function in `llm_router.py` reads the env-var-driven model names and builds the routing table with sensible defaults for thinking budgets and fallbacks:

```python
def _model_config_from_name(model_name: str) -> ModelConfig:
    """Infer provider, thinking budget, etc. from model name."""
    if model_name.startswith("gpt-"):
        return ModelConfig(
            provider=LLMProvider.OPENAI,
            model=model_name,
            reasoning_effort="medium" if "gpt-5" in model_name else None,
        )
    elif model_name.startswith("gemini-"):
        thinking = 8192 if "pro" in model_name else 4096
        return ModelConfig(
            provider=LLMProvider.GEMINI,
            model=model_name,
            temperature=0.1,
            thinking_budget=thinking,
        )
    else:
        raise ValueError(f"Unknown model prefix: {model_name}")


def build_routes_from_settings(settings) -> Dict[str, TaskRoute]:
    """Build routing table from settings env vars."""
    route_map = {
        "decompose": settings.LLM_ROUTE_DECOMPOSE,
        "required_data": settings.LLM_ROUTE_REQUIRED_DATA,
        "gr_outcome": settings.LLM_ROUTE_GR_OUTCOME,
        "gr_response": settings.LLM_ROUTE_GR_RESPONSE,
        "knowledge_question": settings.LLM_ROUTE_KNOWLEDGE,
    }

    # Default fallbacks: OpenAI tasks fall back to Gemini Pro, and vice versa
    fallback_map = {
        LLMProvider.OPENAI: ModelConfig(
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-pro",
            thinking_budget=8192,
        ),
        LLMProvider.GEMINI: ModelConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-5.5",
            reasoning_effort="medium",
        ),
    }

    routes = {}
    for task, model_name in route_map.items():
        primary = _model_config_from_name(model_name)
        fallback = fallback_map.get(primary.provider)
        routes[task] = TaskRoute(primary=primary, fallback=fallback)

    return routes
```

---

## Gemini SDK Setup

### Local development (Google AI API)

1. Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/)
2. Add to `.env`:

```bash
GEMINI_API_KEY=your-gemini-api-key
USE_VERTEX_AI=false
```

### Production on GCP (Vertex AI)

1. Enable the Vertex AI API in your GCP project
2. Grant the Cloud Run service account the `Vertex AI User` role
3. Set env vars:

```bash
USE_VERTEX_AI=true
GCP_PROJECT=your-project-id
GCP_LOCATION=us-central1
# No GEMINI_API_KEY needed — uses IAM authentication
```

The `google-genai` SDK automatically uses Application Default Credentials when `vertexai=True`.

---

## Configuration Reference

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | If any route uses `gpt-*` | `""` | OpenAI API key |
| `GEMINI_API_KEY` | If local dev + Gemini routes | `""` | Google AI API key |
| `USE_VERTEX_AI` | No | `false` | Use Vertex AI instead of Google AI |
| `GCP_PROJECT` | If `USE_VERTEX_AI=true` | `""` | GCP project ID |
| `GCP_LOCATION` | No | `us-central1` | Vertex AI region |
| `LLM_ROUTE_DECOMPOSE` | No | `gemini-2.5-flash` | Model for query decomposition |
| `LLM_ROUTE_REQUIRED_DATA` | No | `gemini-2.5-flash` | Model for required data extraction |
| `LLM_ROUTE_GR_OUTCOME` | No | `gpt-5.5` | Model for outcome determination |
| `LLM_ROUTE_GR_RESPONSE` | No | `gemini-2.5-flash` | Model for response generation |
| `LLM_ROUTE_KNOWLEDGE` | No | `gemini-2.5-flash` | Model for knowledge questions |

### Supported model names

| Model | Provider | Thinking | Use Case |
|-------|----------|----------|----------|
| `gpt-5.5` | OpenAI | Yes (reasoning_effort) | Critical reasoning |
| `gpt-4o-mini` | OpenAI | No | Simple/fast tasks |
| `gemini-2.5-flash` | Gemini | Optional (thinking_budget) | Cost-effective reasoning |
| `gemini-2.5-pro` | Gemini | Yes (thinking_budget) | High-quality reasoning |

---

## Cost Analysis

### Per-call cost estimates

| Task Type | GPT-5.5 (current OpenAI baseline) | Gemini 2.5 Flash | Savings |
|-----------|--------------------|-------------------|---------|
| decompose | ~$0.16 | ~$0.001 | 99% |
| required_data | ~$0.30 | ~$0.01 | 97% |
| gr_outcome | ~$0.30 | (keep GPT-5.5) | 0% |
| gr_response | ~$0.24 | ~$0.02 | 92% |
| knowledge_question | ~$0.40 | ~$0.01 | 98% |

### Monthly cost projection (100 requests/day)

| Scenario | LLM Cost/month |
|----------|----------------|
| All GPT-5.5 (current OpenAI baseline) | ~$2,100 |
| Hybrid (recommended) | ~$900 |
| All Gemini Flash | ~$120 |

### Per generate_response call

| Configuration | Cost | Quality |
|---------------|------|---------|
| All GPT-5.5 | ~$0.70 | Maximum |
| **Hybrid (recommended)** | **~$0.32** | **Maximum where it matters** |
| All Gemini Flash | ~$0.03 | Good (but riskier on outcome) |

---

## Migration Checklist

### Pre-implementation

- [ ] Get a Gemini API key from Google AI Studio (for local dev)
- [ ] Run the existing stress test suite to establish a quality baseline with GPT-5.5
- [ ] Save baseline results for comparison

### Implementation

- [ ] Add `google-genai` to `requirements.txt`
- [ ] Create `data_pipeline/llm_router.py`
- [ ] Update `api/config.py` with Gemini + routing settings
- [ ] Refactor `RAGEngine.__init__()` to accept `LLMRouter`
- [ ] Refactor `_call_llm()` to accept `task_type` and delegate to router
- [ ] Update all 5 call sites with appropriate `task_type`
- [ ] Add `provider_used` / `model_used` to response metadata
- [ ] Update `api/main.py` lifespan to build and inject router
- [ ] Update `validate_settings()` for new config

### Validation

- [ ] Run existing unit tests (`pytest`)
- [ ] Run stress test suite with hybrid routing
- [ ] Compare outcome determination accuracy between GPT-5.5 and Gemini Pro
- [ ] Compare response quality on Flash vs GPT-5.5 for Phase 2
- [ ] Monitor latency differences (Gemini may be faster for Flash calls)

### Production deployment

- [ ] Enable Vertex AI API in GCP project
- [ ] Grant Cloud Run service account `Vertex AI User` role
- [ ] Set `USE_VERTEX_AI=true` in production env
- [ ] Set `GCP_PROJECT` in production env
- [ ] Deploy and monitor logs for fallback events
- [ ] Set up alerting on fallback rate (should be < 1% in steady state)
