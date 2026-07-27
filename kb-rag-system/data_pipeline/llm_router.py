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

import json
import logging
import math
import re
from datetime import date
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Mapping, NamedTuple, Optional

from openai import AsyncOpenAI

from api import metrics as ticket_metrics

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


@dataclass(frozen=True)
class LLMPricing:
    """Reviewed standard-traffic estimate in USD per million tokens."""

    input_usd_per_million: float
    output_usd_per_million: float


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

    def __init__(
        self,
        finish_reason: str,
        usage: Any = None,
        *,
        provider_used: Optional[str] = None,
        model_used: Optional[str] = None,
    ):
        # Provider fields are not a trusted logging surface.  Some SDKs expose
        # free-form safety text and request metadata here. Retain only bounded
        # numeric billing counters and canonical routing identifiers; never a
        # raw SDK object, body, prompt or provider diagnostic.
        self.finish_reason = _safe_finish_reason(finish_reason)
        self.usage = _sanitize_usage(usage)
        self.provider_used = (
            provider_used if provider_used in {"openai", "gemini"} else None
        )
        self.model_used = (
            model_used
            if isinstance(model_used, str)
            and re.fullmatch(
                r"(?:gpt|gemini)-[a-z0-9][a-z0-9._-]{0,126}",
                model_used,
            )
            else None
        )
        super().__init__("LLM returned no content")


_SAFE_FINISH_REASONS = frozenset({
    "stop", "length", "content_filter", "tool_calls", "safety", "unknown",
})


def _safe_finish_reason(value: Any) -> str:
    normalized = str(value).strip().lower() if value is not None else "unknown"
    return normalized if normalized in _SAFE_FINISH_REASONS else "unknown"


def _emit_llm_metric(metric: str, value: int | float, **labels: str) -> None:
    """Best-effort closed-schema telemetry; never changes an LLM outcome."""
    if not ticket_metrics.ticket_execution_active():
        return
    try:
        ticket_metrics.emit(metric, value, **labels)
    except (TypeError, ValueError):
        logger.error("LLM metric rejected by telemetry schema")


_PRICING_KEY_RE = re.compile(
    r"^(?:openai:gpt-|gemini:gemini-)[a-z0-9][a-z0-9._-]{0,119}$"
)
_PRICING_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PRICING_DOCUMENT_FIELDS = frozenset({"pricing_as_of", "source", "models"})
_PRICING_RATE_FIELDS = frozenset({
    "input_usd_per_million", "output_usd_per_million",
})
_MAX_USD_PER_MILLION = 500.0


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("LLM pricing JSON contains a duplicate key")
        result[key] = value
    return result


def parse_llm_pricing_json(raw: Any) -> dict[tuple[str, str], LLMPricing]:
    """Parse a strict, duplicate-free provider/model pricing document.

    Rates intentionally live in reviewed deployment configuration because
    providers and billing classes can change.  Empty config is supported for
    local and core-only processes; deployed worker/reconciler roles enforce
    coverage in :func:`api.config.validate_settings`.
    """
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_768:
        raise ValueError("LLM pricing JSON must be a bounded string")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("LLM pricing JSON is invalid") from exc
    if not isinstance(document, dict) \
            or frozenset(document) != _PRICING_DOCUMENT_FIELDS:
        raise ValueError("LLM pricing document has an invalid schema")

    pricing_as_of = document["pricing_as_of"]
    try:
        parsed_date = date.fromisoformat(pricing_as_of)
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM pricing_as_of must be an ISO date") from exc
    if parsed_date.isoformat() != pricing_as_of or parsed_date > date.today():
        raise ValueError("LLM pricing_as_of must be a reviewed past/current date")

    source = document["source"]
    if not isinstance(source, str) or not _PRICING_SOURCE_RE.fullmatch(source):
        raise ValueError("LLM pricing source must be a public documentation slug")

    models = document["models"]
    if not isinstance(models, dict):
        raise ValueError("LLM pricing models must be an object")
    parsed: dict[tuple[str, str], LLMPricing] = {}
    for raw_key, raw_prices in models.items():
        if not isinstance(raw_key, str) or not _PRICING_KEY_RE.fullmatch(raw_key):
            raise ValueError("LLM pricing key must be canonical provider:model")
        if not isinstance(raw_prices, dict) \
                or frozenset(raw_prices) != _PRICING_RATE_FIELDS:
            raise ValueError("LLM pricing entry has an invalid schema")
        values: list[float] = []
        for field in ("input_usd_per_million", "output_usd_per_million"):
            raw_value = raw_prices[field]
            if isinstance(raw_value, bool) \
                    or not isinstance(raw_value, (int, float)):
                raise ValueError("LLM pricing rate must be numeric")
            value = float(raw_value)
            if not math.isfinite(value) \
                    or not 0 <= value <= _MAX_USD_PER_MILLION:
                raise ValueError("LLM pricing rate is outside reviewed bounds")
            values.append(value)
        provider, model = raw_key.split(":", 1)
        parsed[(provider, model)] = LLMPricing(*values)
    return parsed


def required_pricing_keys(
    routes: Mapping[str, TaskRoute],
) -> frozenset[tuple[str, str]]:
    """Return every exact primary and fallback provider/model pair."""
    keys: set[tuple[str, str]] = set()
    for route in routes.values():
        for config in (route.primary, route.fallback):
            if config is not None:
                keys.add((config.provider.value, config.model))
    return frozenset(keys)


def _bounded_token_count(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) \
            and 0 <= value <= 1_000_000_000:
        return value
    return None


def _sanitize_usage(usage: Any) -> Optional[Dict[str, int]]:
    if not isinstance(usage, dict):
        return None
    prompt = _bounded_token_count(usage.get("prompt_tokens"))
    completion = _bounded_token_count(usage.get("completion_tokens"))
    if prompt is None or completion is None:
        return None
    total = _bounded_token_count(usage.get("total_tokens"))
    sanitized = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total is not None else prompt + completion,
    }
    if sanitized["total_tokens"] > 1_000_000_000:
        return None
    return sanitized


def _add_usage(
    accumulated: Optional[Dict[str, int]],
    current: Optional[Dict[str, int]],
) -> Optional[Dict[str, int]]:
    sanitized = _sanitize_usage(current)
    if sanitized is None:
        return accumulated
    if accumulated is None:
        return sanitized
    combined = {
        key: accumulated[key] + sanitized[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return _sanitize_usage(combined)


def _emit_llm_usage(
    response: LLMResponse,
    pricing: Mapping[tuple[str, str], LLMPricing],
) -> None:
    usage = response.usage
    if not isinstance(usage, dict):
        return
    input_tokens = _bounded_token_count(usage.get("prompt_tokens"))
    output_tokens = _bounded_token_count(usage.get("completion_tokens"))
    if input_tokens is not None:
        _emit_llm_metric("ticket_llm_tokens", input_tokens, reason="input")
    if output_tokens is not None:
        _emit_llm_metric("ticket_llm_tokens", output_tokens, reason="output")

    model_pricing = pricing.get((response.provider_used, response.model_used))
    if model_pricing is None or input_tokens is None or output_tokens is None:
        return
    estimated_cost = math.fsum((
        input_tokens * model_pricing.input_usd_per_million,
        output_tokens * model_pricing.output_usd_per_million,
    )) / 1_000_000
    _emit_llm_metric("ticket_llm_cost_usd", estimated_cost)


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
        self._pricing: dict[tuple[str, str], LLMPricing] = {}

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

    def configure_pricing(
        self,
        pricing: Mapping[tuple[str, str], LLMPricing],
    ) -> None:
        """Install reviewed rates without logging configuration contents."""
        self._pricing = dict(pricing)

    async def call(
        self,
        task_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        force_fallback: bool = False,
    ) -> LLMResponse:
        """
        Route an LLM call based on task type.

        Tries the primary model first. On ANY exception, falls back to the
        secondary model if one is configured. Raises if both fail, or if the
        primary fails and there is no fallback.

        When `force_fallback=True`, the primary is skipped and the fallback
        model is used directly. Callers use this to retry after the primary
        produced a valid but semantically wrong response (e.g., empty
        extraction despite relevant context).
        """
        route = self._routes.get(task_type)
        if not route:
            raise ValueError(f"No route configured for task_type={task_type}")

        if force_fallback:
            if not route.fallback:
                raise ValueError(
                    f"force_fallback=True but no fallback configured for {task_type}"
                )
            _emit_llm_metric("ticket_llm_fallback_count", 1, code="used")
            response = await self._dispatch(
                route.fallback, system_prompt, user_prompt, max_tokens
            )
            _emit_llm_usage(response, self._pricing)
            return response

        try:
            response = await self._dispatch(
                route.primary, system_prompt, user_prompt, max_tokens
            )
        except Exception as primary_error:
            if not route.fallback:
                raise
            logger.warning(
                "LLM primary failed; using configured fallback "
                "(task_type=%s, error_type=%s)",
                task_type,
                type(primary_error).__name__,
            )
        else:
            _emit_llm_metric("ticket_llm_fallback_count", 1, code="not_used")
            _emit_llm_usage(response, self._pricing)
            return response

        _emit_llm_metric("ticket_llm_fallback_count", 1, code="used")
        response = await self._dispatch(
            route.fallback, system_prompt, user_prompt, max_tokens
        )
        _emit_llm_usage(response, self._pricing)
        return response

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
        try:
            if config.provider == LLMProvider.OPENAI:
                return await self._call_openai(
                    config, system_prompt, user_prompt, max_tokens
                )
            if config.provider == LLMProvider.GEMINI:
                return await self._call_gemini(
                    config, system_prompt, user_prompt, max_tokens
                )
            raise ValueError(f"Unknown provider: {config.provider}")
        except LLMEmptyResponseError as exc:
            # Empty responses are still billable. Account for the sanitized
            # counters before a fallback/re-raise, without retaining content
            # or raw provider diagnostics in the exception.
            if exc.usage and exc.provider_used and exc.model_used:
                _emit_llm_usage(
                    LLMResponse(
                        content="",
                        usage=exc.usage,
                        provider_used=exc.provider_used,
                        model_used=exc.model_used,
                    ),
                    self._pricing,
                )
            raise

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
        accumulated_usage: Optional[Dict[str, int]] = None

        for attempt in range(1, self.EMPTY_RESPONSE_RETRIES + 2):
            response = await self._openai_client.chat.completions.create(**params)
            content = response.choices[0].message.content
            last_finish_reason = response.choices[0].finish_reason
            raw_usage = response.usage
            usage_dict: Optional[Dict[str, int]] = None
            if raw_usage:
                usage_dict = {
                    "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": (
                        getattr(raw_usage, "completion_tokens", 0) or 0
                    ),
                    "total_tokens": getattr(raw_usage, "total_tokens", 0) or 0,
                }
            accumulated_usage = _add_usage(accumulated_usage, usage_dict)

            if content and content.strip():
                return LLMResponse(
                    content=content,
                    usage=accumulated_usage,
                    provider_used=LLMProvider.OPENAI.value,
                    model_used=config.model,
                )

            logger.warning(
                f"OpenAI empty content (attempt {attempt}/"
                f"{self.EMPTY_RESPONSE_RETRIES + 1})"
            )

        raise LLMEmptyResponseError(
            finish_reason=last_finish_reason,
            usage=accumulated_usage,
            provider_used=LLMProvider.OPENAI.value,
            model_used=config.model,
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

        # NOTE: thinking_budget=0 must be sent explicitly to *disable* thinking
        # on Gemini 2.5 Flash. If we omit thinking_config entirely the API
        # falls back to dynamic thinking and burns tokens reasoning.
        if config.thinking_budget is not None:
            gen_config_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=config.thinking_budget
            )

        gen_config = genai_types.GenerateContentConfig(**gen_config_kwargs)

        response = await self._gemini_client.aio.models.generate_content(
            model=config.model,
            contents=user_prompt,
            config=gen_config,
        )

        usage: Optional[Dict[str, int]] = None
        um = getattr(response, "usage_metadata", None)
        if um:
            candidate_tokens = getattr(um, "candidates_token_count", 0) or 0
            thoughts_tokens = getattr(um, "thoughts_token_count", 0) or 0
            usage = _sanitize_usage({
                "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
                # Vertex bills response and reasoning as output.  The SDK
                # reports these separately, so combine them exactly once.
                "completion_tokens": candidate_tokens + thoughts_tokens,
                "total_tokens": getattr(um, "total_token_count", 0) or 0,
            })

        content = getattr(response, "text", None)
        if not content or not content.strip():
            finish_reason = "unknown"
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                finish_reason = str(getattr(candidates[0], "finish_reason", "unknown"))
            raise LLMEmptyResponseError(
                finish_reason=finish_reason,
                usage=usage,
                provider_used=LLMProvider.GEMINI.value,
                model_used=config.model,
            )

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
# (high-quality reasoning) and vice-versa to GPT-5.5.
_DEFAULT_FALLBACK_BY_PROVIDER: Dict[LLMProvider, ModelConfig] = {
    LLMProvider.OPENAI: ModelConfig(
        provider=LLMProvider.GEMINI,
        model="gemini-2.5-pro",
        temperature=0.1,
        thinking_budget=8192,
    ),
    LLMProvider.GEMINI: ModelConfig(
        provider=LLMProvider.OPENAI,
        model="gpt-5.5",
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
    # Inquiry classifier needs real reasoning to disambiguate nuanced cases
    # (e.g. incoming vs outgoing rollover, procedural HOW vs eligibility
    # WHETHER). On Gemini Flash give it a moderate thinking budget; the
    # OpenAI fallback (gpt-5.5) keeps its provider-default medium reasoning.
    "classify_inquiry": {"thinking_budget": 4096},
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
        "classify_inquiry": settings.LLM_ROUTE_CLASSIFY,
        # End-to-end ticket handler agents (LLM-first).
        "extract_inquiries": settings.LLM_ROUTE_EXTRACT_INQUIRIES,
        "kb_question_synthesis": settings.LLM_ROUTE_KB_QUESTION_SYNTHESIS,
        "forusbots_field_map": settings.LLM_ROUTE_FORUSBOTS_FIELD_MAP,
        "gr_body_build": settings.LLM_ROUTE_GR_BODY_BUILD,
        "ticket_field_extract": settings.LLM_ROUTE_TICKET_FIELD_EXTRACT,
    }

    def _apply_override(cfg: ModelConfig, override: Dict[str, Any]) -> ModelConfig:
        updates: Dict[str, Any] = {}
        if cfg.provider == LLMProvider.OPENAI and "reasoning_effort" in override:
            updates["reasoning_effort"] = override["reasoning_effort"]
        if cfg.provider == LLMProvider.GEMINI:
            if "thinking_budget" in override:
                updates["thinking_budget"] = override["thinking_budget"]
            if "gemini_fallback_model" in override:
                updates["model"] = override["gemini_fallback_model"]
        return replace(cfg, **updates) if updates else cfg

    routes: Dict[str, TaskRoute] = {}
    for task, model_name in route_map.items():
        primary = _model_config_from_name(model_name)
        fallback = _DEFAULT_FALLBACK_BY_PROVIDER.get(primary.provider)
        override = _TASK_EFFORT_OVERRIDES.get(task)
        if override:
            primary = _apply_override(primary, override)
            if fallback is not None:
                fallback = _apply_override(fallback, override)
        routes[task] = TaskRoute(primary=primary, fallback=fallback)

    return routes
