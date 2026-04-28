"""
Unit tests for the LLM Router.

These tests cover the routing layer — dispatch by task_type, cross-provider
fallback on exceptions, empty-content retry on OpenAI, and the settings-
driven routing table builder. Network / SDK calls are fully mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from data_pipeline.llm_router import (
    LLMEmptyResponseError,
    LLMProvider,
    LLMResponse,
    LLMRouter,
    ModelConfig,
    TaskRoute,
    _model_config_from_name,
    build_routes_from_settings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_openai_response(content: str | None, finish_reason: str = "stop"):
    """Build a minimal object that mimics `openai.chat.completions.create` output."""
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content),
        finish_reason=finish_reason,
    )
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_gemini_response(text: str | None):
    """Build a minimal object that mimics the google-genai response."""
    um = SimpleNamespace(
        prompt_token_count=7, candidates_token_count=13, total_token_count=20
    )
    return SimpleNamespace(text=text, usage_metadata=um, candidates=[])


@pytest.fixture
def router_with_openai():
    """Router with a fake OpenAI client attached (no real network)."""
    router = LLMRouter(openai_api_key="fake-openai-key")
    mock_openai = AsyncMock()
    router._openai_client = Mock()
    router._openai_client.chat = Mock()
    router._openai_client.chat.completions = Mock()
    router._openai_client.chat.completions.create = mock_openai
    return router, mock_openai


@pytest.fixture
def fake_genai_types(monkeypatch):
    """Install a minimal stub for `google.genai.types` so tests don't require
    the real SDK to be installed."""
    fake = SimpleNamespace(
        ThinkingConfig=lambda thinking_budget=0: SimpleNamespace(
            thinking_budget=thinking_budget
        ),
        GenerateContentConfig=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr("data_pipeline.llm_router.genai_types", fake)
    return fake


@pytest.fixture
def router_with_gemini(fake_genai_types):
    """Router with a fake Gemini client attached."""
    router = LLMRouter()
    aio_models = Mock()
    aio_models.generate_content = AsyncMock()
    router._gemini_client = Mock()
    router._gemini_client.aio = Mock()
    router._gemini_client.aio.models = aio_models
    return router, aio_models.generate_content


# ---------------------------------------------------------------------------
# OpenAI dispatch
# ---------------------------------------------------------------------------

class TestOpenAIDispatch:

    @pytest.mark.asyncio
    async def test_happy_path_gpt5(self, router_with_openai):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')

        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5", reasoning_effort="medium"
            )),
        })

        resp = await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert resp.content == '{"ok": true}'
        assert resp.provider_used == "openai"
        assert resp.model_used == "gpt-5.5"
        assert resp.usage == {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }

        kwargs = mock_create.call_args.kwargs
        assert kwargs["model"] == "gpt-5.5"
        assert kwargs["reasoning_effort"] == "medium"
        assert kwargs["max_completion_tokens"] >= LLMRouter.GPT5_MIN_COMPLETION_TOKENS
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_non_gpt5_uses_temperature_and_max_tokens(self, router_with_openai):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')

        router.configure_routes({
            "decompose": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-4o-mini", temperature=0.2,
            )),
        })

        await router.call("decompose", "sys", "usr", max_tokens=150)

        kwargs = mock_create.call_args.kwargs
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["max_tokens"] == 150
        assert kwargs["temperature"] == 0.2
        assert "max_completion_tokens" not in kwargs
        assert "reasoning_effort" not in kwargs

    @pytest.mark.asyncio
    async def test_empty_content_retries_then_raises(self, router_with_openai):
        router, mock_create = router_with_openai
        mock_create.side_effect = [
            _make_openai_response(None, finish_reason="length"),
            _make_openai_response("   ", finish_reason="length"),
        ]

        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5",
            )),
        })

        with pytest.raises(LLMEmptyResponseError):
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert mock_create.await_count == LLMRouter.EMPTY_RESPONSE_RETRIES + 1

    @pytest.mark.asyncio
    async def test_empty_content_then_success_returns_content(self, router_with_openai):
        router, mock_create = router_with_openai
        mock_create.side_effect = [
            _make_openai_response(None, finish_reason="length"),
            _make_openai_response('{"ok": true}'),
        ]

        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5",
            )),
        })

        resp = await router.call("gr_outcome", "sys", "usr", max_tokens=800)
        assert resp.content == '{"ok": true}'
        assert mock_create.await_count == 2


# ---------------------------------------------------------------------------
# Fallback logic
# ---------------------------------------------------------------------------

class TestFallback:

    @pytest.mark.asyncio
    async def test_falls_back_on_primary_exception(
        self, router_with_openai, router_with_gemini, fake_genai_types,
    ):
        """When the primary raises, the router must dispatch to the fallback."""
        openai_router, mock_openai = router_with_openai
        _, mock_gemini = router_with_gemini
        # Attach the gemini client onto the openai router so both are reachable.
        openai_router._gemini_client = Mock()
        openai_router._gemini_client.aio = Mock()
        openai_router._gemini_client.aio.models = Mock()
        openai_router._gemini_client.aio.models.generate_content = mock_gemini

        mock_openai.side_effect = RuntimeError("openai 500")
        mock_gemini.return_value = _make_gemini_response('{"ok": "fallback"}')

        openai_router.configure_routes({
            "gr_outcome": TaskRoute(
                primary=ModelConfig(
                    provider=LLMProvider.OPENAI, model="gpt-5.5",
                ),
                fallback=ModelConfig(
                    provider=LLMProvider.GEMINI, model="gemini-2.5-pro",
                    thinking_budget=8192,
                ),
            ),
        })

        resp = await openai_router.call("gr_outcome", "sys", "usr", max_tokens=800)
        assert resp.content == '{"ok": "fallback"}'
        assert resp.provider_used == "gemini"
        assert resp.model_used == "gemini-2.5-pro"
        mock_openai.assert_awaited()
        mock_gemini.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_fallback_propagates_exception(self, router_with_openai):
        router, mock_create = router_with_openai
        mock_create.side_effect = RuntimeError("boom")

        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5",
            )),
        })

        with pytest.raises(RuntimeError, match="boom"):
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)


# ---------------------------------------------------------------------------
# Gemini dispatch
# ---------------------------------------------------------------------------

class TestGeminiDispatch:

    @pytest.mark.asyncio
    async def test_gemini_happy_path(self, router_with_gemini):
        router, mock_generate = router_with_gemini
        mock_generate.return_value = _make_gemini_response('{"ok": true}')

        router.configure_routes({
            "gr_response": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.GEMINI, model="gemini-2.5-flash",
                thinking_budget=4096,
            )),
        })

        resp = await router.call("gr_response", "sys", "usr", max_tokens=500)

        assert resp.content == '{"ok": true}'
        assert resp.provider_used == "gemini"
        assert resp.model_used == "gemini-2.5-flash"
        assert resp.usage == {
            "prompt_tokens": 7,
            "completion_tokens": 13,
            "total_tokens": 20,
        }
        mock_generate.assert_awaited_once()
        call_kwargs = mock_generate.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_gemini_empty_text_raises(self, router_with_gemini):
        router, mock_generate = router_with_gemini
        mock_generate.return_value = _make_gemini_response(None)

        router.configure_routes({
            "gr_response": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.GEMINI, model="gemini-2.5-flash",
            )),
        })

        with pytest.raises(LLMEmptyResponseError):
            await router.call("gr_response", "sys", "usr", max_tokens=500)


# ---------------------------------------------------------------------------
# Routing table builder
# ---------------------------------------------------------------------------

class TestRoutingTable:

    def test_unknown_task_raises(self, router_with_openai):
        router, _ = router_with_openai
        router.configure_routes({})
        import asyncio
        with pytest.raises(ValueError, match="No route configured"):
            asyncio.get_event_loop().run_until_complete(
                router.call("missing", "sys", "usr", max_tokens=100)
            )

    def test_model_config_from_name_gpt5(self):
        cfg = _model_config_from_name("gpt-5.5")
        assert cfg.provider == LLMProvider.OPENAI
        assert cfg.reasoning_effort == "medium"

    def test_model_config_from_name_gpt4(self):
        cfg = _model_config_from_name("gpt-4o-mini")
        assert cfg.provider == LLMProvider.OPENAI
        assert cfg.reasoning_effort is None

    def test_model_config_from_name_gemini_flash(self):
        cfg = _model_config_from_name("gemini-2.5-flash")
        assert cfg.provider == LLMProvider.GEMINI
        assert cfg.thinking_budget == 4096

    def test_model_config_from_name_gemini_pro(self):
        cfg = _model_config_from_name("gemini-2.5-pro")
        assert cfg.provider == LLMProvider.GEMINI
        assert cfg.thinking_budget == 8192

    def test_model_config_from_name_unknown_prefix_raises(self):
        with pytest.raises(ValueError, match="Unknown model prefix"):
            _model_config_from_name("claude-3.5-sonnet")

    def test_build_routes_decompose_has_zero_thinking(self):
        """Decompose is a trivial task; its Gemini route should disable thinking."""
        settings = SimpleNamespace(
            LLM_ROUTE_DECOMPOSE="gemini-2.5-flash",
            LLM_ROUTE_REQUIRED_DATA="gemini-2.5-flash",
            LLM_ROUTE_GR_OUTCOME="gpt-5.5",
            LLM_ROUTE_GR_RESPONSE="gemini-2.5-flash",
            LLM_ROUTE_KNOWLEDGE="gemini-2.5-flash",
        )
        routes = build_routes_from_settings(settings)

        assert routes["decompose"].primary.thinking_budget == 0
        # Other Gemini routes keep a non-zero thinking budget.
        assert routes["required_data"].primary.thinking_budget == 4096
        # gr_outcome stays on OpenAI.
        assert routes["gr_outcome"].primary.provider == LLMProvider.OPENAI
        assert routes["gr_outcome"].primary.model == "gpt-5.5"

    def test_build_routes_cross_provider_fallback(self):
        settings = SimpleNamespace(
            LLM_ROUTE_DECOMPOSE="gemini-2.5-flash",
            LLM_ROUTE_REQUIRED_DATA="gemini-2.5-flash",
            LLM_ROUTE_GR_OUTCOME="gpt-5.5",
            LLM_ROUTE_GR_RESPONSE="gemini-2.5-flash",
            LLM_ROUTE_KNOWLEDGE="gemini-2.5-flash",
        )
        routes = build_routes_from_settings(settings)

        # Gemini primary -> OpenAI fallback
        assert routes["gr_response"].fallback.provider == LLMProvider.OPENAI
        # OpenAI primary -> Gemini fallback
        assert routes["gr_outcome"].fallback.provider == LLMProvider.GEMINI

    def test_build_routes_all_openai_shadow_deploy(self):
        """Shadow deploy config: all routes on OpenAI keeps current behaviour."""
        settings = SimpleNamespace(
            LLM_ROUTE_DECOMPOSE="gpt-5.5",
            LLM_ROUTE_REQUIRED_DATA="gpt-5.5",
            LLM_ROUTE_GR_OUTCOME="gpt-5.5",
            LLM_ROUTE_GR_RESPONSE="gpt-5.5",
            LLM_ROUTE_KNOWLEDGE="gpt-5.5",
        )
        routes = build_routes_from_settings(settings)
        for route in routes.values():
            assert route.primary.provider == LLMProvider.OPENAI
            assert route.fallback.provider == LLMProvider.GEMINI
