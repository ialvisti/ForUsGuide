"""
LLM Router — dispatches task-typed LLM calls to OpenAI or Gemini.

The RAG engine makes five distinct LLM calls per request flow:
  decompose, required_data, gr_outcome, gr_response, knowledge_question.

Each call can be routed to a different provider/model via environment
variables, with automatic cross-provider fallback on any exception.

Production on GCP uses Vertex AI (ADC — no API key), local dev can use
either the OpenAI API key or a Google AI Studio key.

See Development Docs/HYBRID_LLM_ARCHITECTURE.md for the full rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, NamedTuple, Optional

from openai import AsyncOpenAI

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

logger = logging.getLogger(__name__)


# ============================================================================
# Public types
# ============================================================================

class LLMProvider(Enum):
    OPENAI = "openai"
    GEMINI = "gemini"


class LLMResponse(NamedTuple):
    """Unified response from any LLM provider."""
    content: str
    usage: Optional[Dict[str, int]]
    provider_used: str
    model_used: str


@dataclass
class ModelConfig:
    """Configuration for a specific model invocation."""
    provider: LLMProvider
    model: str
    temperature: float = 0.1
    reasoning_effort: Optional[str] = None  # OpenAI GPT-5 only
    thinking_budget: Optional[int] = None   # Gemini thinking models only
    max_completion_floor: int = 0           # optional min max_completion_tokens


@dataclass
class TaskRoute:
    """Primary model for a task plus an optional fallback."""
    primary: ModelConfig
    fallback: Optional[ModelConfig] = None


class LLMEmptyResponseError(Exception):
    """Raised when the LLM returns empty or None content after all retries."""

    def __init__(self, finish_reason: str, usage: Any = None):
        self.finish_reason = finish_reason
        self.usage = usage
        super().__init__(
            f"LLM returned no content (finish_reason={finish_reason}, usage={usage})"
        )


# ============================================================================
# Router
# ============================================================================

class LLMRouter:
    """Routes LLM calls to optimal providers based on task type."""

    # GPT-5 with reasoning needs generous completion-token headroom. These
    # constants used to live in rag_engine.py and are consolidated here now.
    GPT5_REASONING_MULTIPLIER = 10
    GPT5_MIN_COMPLETION_TOKENS = 16000

    # Retry once on empty content before raising. OpenAI GPT-5 occasionally
    # returns content=None when reasoning tokens fully consume the budget.
    EMPTY_RESPONSE_RETRIES = 1

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        use_vertex_ai: bool = False,
        gcp_project: Optional[str] = None,
        gcp_location: str = "us-central1",
    ):
        self._openai_client: Optional[AsyncOpenAI] = None
        if openai_api_key:
            self._openai_client = AsyncOpenAI(api_key=openai_api_key)

        self._gemini_client: Any = None
        if use_vertex_ai and gcp_project:
            if genai is None:
                raise RuntimeError(
                    "google-genai package is not installed but USE_VERTEX_AI=true. "
                    "Add `google-genai` to requirements.txt."
                )
            self._gemini_client = genai.Client(
                vertexai=True,
                project=gcp_project,
                location=gcp_location,
            )
            logger.info(
                f"Gemini client initialised via Vertex AI "
                f"(project={gcp_project}, location={gcp_location})"
            )
        elif gemini_api_key:
            if genai is None:
                raise RuntimeError(
                    "google-genai package is not installed but GEMINI_API_KEY is set."
                )
            self._gemini_client = genai.Client(api_key=gemini_api_key)
            logger.info("Gemini client initialised via Google AI API key")

        self._routes: Dict[str, TaskRoute] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure_routes(self, routes: Dict[str, TaskRoute]) -> None:
        """Install the routing table. Called once at startup."""
        self._routes = routes
        for task, route in routes.items():
            fb = (
                f" -> fallback: {route.fallback.provider.value}:{route.fallback.model}"
                if route.fallback
                else ""
            )
            logger.info(
                f"LLM route: {task} -> {route.primary.provider.value}:{route.primary.model}{fb}"
            )

    async def call(
        self,
        task_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        """
        Route an LLM call based on task type.

        Tries the primary model first. On ANY exception, falls back to the
        secondary model if one is configured. Raises if both fail, or if the
        primary fails and there is no fallback.
        """
        route = self._routes.get(task_type)
        if not route:
            raise ValueError(f"No route configured for task_type={task_type}")

        try:
            return await self._dispatch(route.primary, system_prompt, user_prompt, max_tokens)
        except Exception as primary_error:
            if not route.fallback:
                raise
            logger.warning(
                f"[{task_type}] Primary ({route.primary.provider.value}:"
                f"{route.primary.model}) failed: {type(primary_error).__name__}: "
                f"{primary_error}. Falling back to "
                f"{route.fallback.provider.value}:{route.fallback.model}"
            )

        return await self._dispatch(route.fallback, system_prompt, user_prompt, max_tokens)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        if config.provider == LLMProvider.OPENAI:
            return await self._call_openai(config, system_prompt, user_prompt, max_tokens)
        if config.provider == LLMProvider.GEMINI:
            return await self._call_gemini(config, system_prompt, user_prompt, max_tokens)
        raise ValueError(f"Unknown provider: {config.provider}")

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    async def _call_openai(
        self,
        config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        if not self._openai_client:
            raise RuntimeError("OpenAI client not configured")

        is_gpt5 = "gpt-5" in config.model.lower()

        params: Dict[str, Any] = {
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
            logger.debug(
                f"OpenAI GPT-5 call: model={config.model}, scaled_tokens={scaled}, "
                f"reasoning_effort={config.reasoning_effort}"
            )
        else:
            params["max_tokens"] = max_tokens
            params["temperature"] = config.temperature

        last_finish_reason = "unknown"
        last_usage = None

        for attempt in range(1, self.EMPTY_RESPONSE_RETRIES + 2):
            response = await self._openai_client.chat.completions.create(**params)
            content = response.choices[0].message.content
            last_finish_reason = response.choices[0].finish_reason
            last_usage = response.usage

            if content and content.strip():
                usage_dict: Optional[Dict[str, int]] = None
                if last_usage:
                    usage_dict = {
                        "prompt_tokens": getattr(last_usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(last_usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(last_usage, "total_tokens", 0) or 0,
                    }
                return LLMResponse(
                    content=content,
                    usage=usage_dict,
                    provider_used=LLMProvider.OPENAI.value,
                    model_used=config.model,
                )

            logger.warning(
                f"OpenAI empty content (attempt {attempt}/"
                f"{self.EMPTY_RESPONSE_RETRIES + 1}). "
                f"finish_reason={last_finish_reason}, usage={last_usage}"
            )

        raise LLMEmptyResponseError(
            finish_reason=last_finish_reason,
            usage=last_usage,
        )

    # ------------------------------------------------------------------
    # Gemini
    # ------------------------------------------------------------------

    async def _call_gemini(
        self,
        config: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        if not self._gemini_client or genai_types is None:
            raise RuntimeError("Gemini client not configured")

        gen_config_kwargs: Dict[str, Any] = {
            "system_instruction": system_prompt,
            "response_mime_type": "application/json",
            "temperature": config.temperature,
            "max_output_tokens": max_tokens,
        }

        if config.thinking_budget is not None and config.thinking_budget > 0:
            gen_config_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=config.thinking_budget
            )

        gen_config = genai_types.GenerateContentConfig(**gen_config_kwargs)

        response = await self._gemini_client.aio.models.generate_content(
            model=config.model,
            contents=user_prompt,
            config=gen_config,
        )

        content = getattr(response, "text", None)
        if not content or not content.strip():
            finish_reason = "unknown"
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                finish_reason = str(getattr(candidates[0], "finish_reason", "unknown"))
            raise LLMEmptyResponseError(
                finish_reason=finish_reason,
                usage=getattr(response, "usage_metadata", None),
            )

        usage: Optional[Dict[str, int]] = None
        um = getattr(response, "usage_metadata", None)
        if um:
            usage = {
                "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(um, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(um, "total_token_count", 0) or 0,
            }

        return LLMResponse(
            content=content,
            usage=usage,
            provider_used=LLMProvider.GEMINI.value,
            model_used=config.model,
        )


# ============================================================================
# Routing table builder
# ============================================================================

# Default cross-provider fallback. An OpenAI primary falls back to Gemini Pro
# (high-quality reasoning) and vice-versa to GPT-5.4.
_DEFAULT_FALLBACK_BY_PROVIDER: Dict[LLMProvider, ModelConfig] = {
    LLMProvider.OPENAI: ModelConfig(
        provider=LLMProvider.GEMINI,
        model="gemini-2.5-pro",
        temperature=0.1,
        thinking_budget=8192,
    ),
    LLMProvider.GEMINI: ModelConfig(
        provider=LLMProvider.OPENAI,
        model="gpt-5.4",
        reasoning_effort="medium",
    ),
}


def _model_config_from_name(model_name: str) -> ModelConfig:
    """Infer provider, thinking budget, reasoning effort from the model name."""
    name = model_name.strip().lower()
    if name.startswith("gpt-"):
        return ModelConfig(
            provider=LLMProvider.OPENAI,
            model=model_name,
            reasoning_effort="medium" if "gpt-5" in name else None,
        )
    if name.startswith("gemini-"):
        # Decompose is a simple task; other Gemini tasks get a modest thinking budget.
        thinking = 8192 if "pro" in name else 4096
        return ModelConfig(
            provider=LLMProvider.GEMINI,
            model=model_name,
            temperature=0.1,
            thinking_budget=thinking,
        )
    raise ValueError(
        f"Unknown model prefix in '{model_name}'. "
        f"Expected 'gpt-*' or 'gemini-*'."
    )


# Per-task effort overrides applied on top of the defaults from
# `_model_config_from_name`. Mirrors the guidance in HYBRID_LLM_ARCHITECTURE.md:
# simple tasks get minimal reasoning/thinking; critical tasks keep their
# provider-default budgets. Only tasks that need an override appear here.
_TASK_EFFORT_OVERRIDES: Dict[str, Dict[str, Any]] = {
    # Decompose splits a question into 1–3 sub-queries. On Gemini, zero
    # thinking budget is enough — the few-shot prompt is sufficient
    # without a thinking pass. On GPT-5 we keep the provider default
    # (medium reasoning) because dropping it below medium causes the
    # model to skip decomposition on multi-concept inquiries.
    "decompose": {"thinking_budget": 0},
}


def build_routes_from_settings(settings: Any) -> Dict[str, TaskRoute]:
    """
    Build the routing table from the Settings object.

    The table has one entry per `task_type` used by RAGEngine. Each entry's
    primary model is read from an env var; the fallback is the default
    cross-provider one from `_DEFAULT_FALLBACK_BY_PROVIDER`.
    """
    route_map = {
        "decompose": settings.LLM_ROUTE_DECOMPOSE,
        "required_data": settings.LLM_ROUTE_REQUIRED_DATA,
        "gr_outcome": settings.LLM_ROUTE_GR_OUTCOME,
        "gr_response": settings.LLM_ROUTE_GR_RESPONSE,
        "knowledge_question": settings.LLM_ROUTE_KNOWLEDGE,
    }

    routes: Dict[str, TaskRoute] = {}
    for task, model_name in route_map.items():
        primary = _model_config_from_name(model_name)
        override = _TASK_EFFORT_OVERRIDES.get(task)
        if override:
            if primary.provider == LLMProvider.OPENAI and "reasoning_effort" in override:
                primary = replace(primary, reasoning_effort=override["reasoning_effort"])
            elif primary.provider == LLMProvider.GEMINI and "thinking_budget" in override:
                primary = replace(primary, thinking_budget=override["thinking_budget"])
        fallback = _DEFAULT_FALLBACK_BY_PROVIDER.get(primary.provider)
        routes[task] = TaskRoute(primary=primary, fallback=fallback)

    return routes
