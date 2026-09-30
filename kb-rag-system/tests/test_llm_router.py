"""
Unit tests for the LLM Router.

These tests cover the routing layer — dispatch by task_type, cross-provider
fallback on exceptions, empty-content retry on OpenAI, and the settings-
driven routing table builder. Network / SDK calls are fully mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api import metrics as ticket_metrics
from data_pipeline.llm_router import (
    LLMPricing,
    LLMEmptyResponseError,
    LLMProvider,
    LLMRouter,
    ModelConfig,
    TaskRoute,
    _model_config_from_name,
    build_routes_from_settings,
    parse_llm_pricing_json,
    required_pricing_keys,
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


def _make_gemini_response(text: str | None, *, thoughts_tokens: int = 0):
    """Build a minimal object that mimics the google-genai response."""
    um = SimpleNamespace(
        prompt_token_count=7,
        candidates_token_count=13,
        thoughts_token_count=thoughts_tokens,
        total_token_count=20 + thoughts_tokens,
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
    async def test_success_emits_bounded_token_and_no_fallback_metrics(
        self, router_with_openai, monkeypatch,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5",
            )),
        })

        with ticket_metrics.ticket_execution_scope():
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert emitted == [
            ("ticket_llm_fallback_count", 1, {"code": "not_used"}),
            ("ticket_llm_tokens", 10, {"reason": "input"}),
            ("ticket_llm_tokens", 20, {"reason": "output"}),
        ]

    @pytest.mark.asyncio
    async def test_success_emits_estimated_cost_from_explicit_model_pricing(
        self, router_with_openai, monkeypatch,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')
        router.configure_pricing({
            ("openai", "gpt-5.5"): LLMPricing(
                input_usd_per_million=5.0,
                output_usd_per_million=30.0,
            ),
        })
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5",
            )),
        })

        with ticket_metrics.ticket_execution_scope():
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert emitted[-1] == (
            "ticket_llm_cost_usd",
            pytest.approx((10 * 5.0 + 20 * 30.0) / 1_000_000),
            {},
        )

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


def _all_route_settings(**overrides: str) -> SimpleNamespace:
    settings = {
        "LLM_ROUTE_DECOMPOSE": "gpt-5.5",
        "LLM_ROUTE_REQUIRED_DATA": "gpt-5.5",
        "LLM_ROUTE_GR_OUTCOME": "gpt-5.5",
        "LLM_ROUTE_GR_RESPONSE": "gpt-5.5",
        "LLM_ROUTE_KNOWLEDGE": "gpt-5.5",
        "LLM_ROUTE_CLASSIFY": "gpt-5.5-mini",
        "LLM_ROUTE_EXTRACT_INQUIRIES": "gpt-5.5",
        "LLM_ROUTE_KB_QUESTION_SYNTHESIS": "gpt-5.5",
        "LLM_ROUTE_FORUSBOTS_FIELD_MAP": "gpt-5.5",
        "LLM_ROUTE_GR_BODY_BUILD": "gpt-5.5",
        "LLM_ROUTE_TICKET_FIELD_EXTRACT": "gpt-5.5",
    }
    settings.update(overrides)
    return SimpleNamespace(**settings)


class TestGpt6SolRequestShape:
    """Exact gpt-6-sol uses the reasoning Chat Completions shape.

    These tests lock the observable request, not a private recognizer.
    """

    @pytest.mark.asyncio
    async def test_exact_gpt6_sol_omits_legacy_sampling_params(
        self, router_with_openai,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"answer":"ok"}')
        router.configure_routes(
            build_routes_from_settings(
                _all_route_settings(LLM_ROUTE_KNOWLEDGE="gpt-6-sol")
            )
        )

        resp = await router.call(
            "knowledge_question", "system", "user question", max_tokens=800,
        )

        assert resp.content == '{"answer":"ok"}'
        assert resp.provider_used == "openai"
        assert resp.model_used == "gpt-6-sol"
        assert resp.usage == {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }
        kwargs = mock_create.call_args.kwargs
        assert kwargs["model"] == "gpt-6-sol"
        assert kwargs["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user question"},
        ]
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["reasoning_effort"] == "medium"
        # 800 * 10 = 8000, which is below the documented GPT-6 Sol start.
        assert kwargs["max_completion_tokens"] == 25_000
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_gpt6_sol_allowance_keeps_ten_x_request_and_config_floor(
        self, router_with_openai,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')

        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-6-sol",
                temperature=0.1,
                reasoning_effort="medium",
            )),
        })
        await router.call("gr_outcome", "sys", "usr", max_tokens=3_000)
        # 3000 * 10 = 30000, above the 25000 GPT-6 Sol start.
        assert mock_create.call_args.kwargs["max_completion_tokens"] == 30_000
        assert "temperature" not in mock_create.call_args.kwargs

        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-6-sol",
                reasoning_effort="high",
                max_completion_floor=40_000,
            )),
        })
        await router.call("gr_outcome", "sys", "usr", max_tokens=100)
        kwargs = mock_create.call_args.kwargs
        assert kwargs["max_completion_tokens"] == 40_000
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["response_format"] == {"type": "json_object"}
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_gpt6_sol_empty_retry_parses_content_and_sums_usage(
        self, router_with_openai,
    ):
        router, mock_create = router_with_openai
        mock_create.side_effect = [
            _make_openai_response(None, finish_reason="length"),
            _make_openai_response('{"answer":1}'),
        ]
        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-6-sol",
                reasoning_effort="medium",
            )),
        })

        resp = await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert resp.content == '{"answer":1}'
        assert resp.usage == {
            "prompt_tokens": 20,
            "completion_tokens": 40,
            "total_tokens": 60,
        }
        assert mock_create.await_count == LLMRouter.EMPTY_RESPONSE_RETRIES + 1
        assert len(mock_create.await_args_list) == 2
        for call in mock_create.await_args_list:
            kwargs = call.kwargs
            assert kwargs["model"] == "gpt-6-sol"
            assert kwargs["response_format"] == {"type": "json_object"}
            assert kwargs["reasoning_effort"] == "medium"
            assert kwargs["max_completion_tokens"] == 25_000
            assert "temperature" not in kwargs
            assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_gpt5_allowance_stays_on_existing_floor(
        self, router_with_openai,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')
        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-5.5",
                reasoning_effort="medium",
            )),
        })

        await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        kwargs = mock_create.call_args.kwargs
        # 800 * 10 = 8000, so the existing GPT-5 floor of 16000 still wins.
        assert kwargs["max_completion_tokens"] == 16_000
        assert kwargs["reasoning_effort"] == "medium"
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs

        await router.call("gr_outcome", "sys", "usr", max_tokens=2_000)
        assert mock_create.call_args.kwargs["max_completion_tokens"] == 20_000

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model_name", ["gpt-6", "gpt-6-sol-preview", "gpt-4o-mini"])
    async def test_non_exact_openai_ids_keep_temperature_and_max_tokens(
        self, router_with_openai, model_name,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')
        router.configure_routes({
            "decompose": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI,
                model=model_name,
                temperature=0.2,
            )),
        })

        await router.call("decompose", "sys", "usr", max_tokens=150)

        kwargs = mock_create.call_args.kwargs
        assert kwargs["model"] == model_name
        assert kwargs["max_tokens"] == 150
        assert kwargs["temperature"] == 0.2
        assert kwargs["response_format"] == {"type": "json_object"}
        assert "max_completion_tokens" not in kwargs
        assert "reasoning_effort" not in kwargs


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
    async def test_fallback_emits_used_once_and_only_successful_usage(
        self, router_with_openai, router_with_gemini, monkeypatch,
    ):
        router, mock_openai = router_with_openai
        _, mock_gemini = router_with_gemini
        router._gemini_client = Mock()
        router._gemini_client.aio = Mock()
        router._gemini_client.aio.models = Mock()
        router._gemini_client.aio.models.generate_content = mock_gemini
        mock_openai.side_effect = RuntimeError("sensitive provider body")
        mock_gemini.return_value = _make_gemini_response('{"ok": true}')
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(
                primary=ModelConfig(
                    provider=LLMProvider.OPENAI, model="gpt-5.5",
                ),
                fallback=ModelConfig(
                    provider=LLMProvider.GEMINI, model="gemini-2.5-pro",
                ),
            ),
        })

        with ticket_metrics.ticket_execution_scope():
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert emitted == [
            ("ticket_llm_fallback_count", 1, {"code": "used"}),
            ("ticket_llm_tokens", 7, {"reason": "input"}),
            ("ticket_llm_tokens", 13, {"reason": "output"}),
        ]

    @pytest.mark.asyncio
    async def test_fallback_cost_uses_successful_provider_model_and_thinking_tokens(
        self, router_with_openai, router_with_gemini, monkeypatch,
    ):
        router, mock_openai = router_with_openai
        _, mock_gemini = router_with_gemini
        router._gemini_client = Mock()
        router._gemini_client.aio = Mock()
        router._gemini_client.aio.models = Mock()
        router._gemini_client.aio.models.generate_content = mock_gemini
        mock_openai.side_effect = RuntimeError("provider failure")
        mock_gemini.return_value = _make_gemini_response(
            '{"ok": true}', thoughts_tokens=11,
        )
        router.configure_pricing({
            ("gemini", "gemini-2.5-pro"): LLMPricing(
                input_usd_per_million=1.25,
                output_usd_per_million=10.0,
            ),
        })
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(
                primary=ModelConfig(
                    provider=LLMProvider.OPENAI, model="gpt-5.5",
                ),
                fallback=ModelConfig(
                    provider=LLMProvider.GEMINI, model="gemini-2.5-pro",
                ),
            ),
        })

        with ticket_metrics.ticket_execution_scope():
            response = await router.call(
                "gr_outcome", "sys", "usr", max_tokens=800,
            )

        assert response.usage["completion_tokens"] == 24
        assert emitted[-1] == (
            "ticket_llm_cost_usd",
            pytest.approx((7 * 1.25 + 24 * 10.0) / 1_000_000),
            {},
        )

    @pytest.mark.asyncio
    async def test_billed_empty_primary_attempts_and_fallback_are_counted_once(
        self, router_with_openai, router_with_gemini, monkeypatch,
    ):
        router, mock_openai = router_with_openai
        _, mock_gemini = router_with_gemini
        router._gemini_client = Mock()
        router._gemini_client.aio = Mock()
        router._gemini_client.aio.models = Mock()
        router._gemini_client.aio.models.generate_content = mock_gemini
        mock_openai.side_effect = [
            _make_openai_response(None, finish_reason="length"),
            _make_openai_response(None, finish_reason="length"),
        ]
        mock_gemini.return_value = _make_gemini_response('{"ok": true}')
        router.configure_pricing({
            ("openai", "gpt-5.5"): LLMPricing(5.0, 30.0),
            ("gemini", "gemini-2.5-pro"): LLMPricing(1.25, 10.0),
        })
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(
                primary=ModelConfig(
                    provider=LLMProvider.OPENAI, model="gpt-5.5",
                ),
                fallback=ModelConfig(
                    provider=LLMProvider.GEMINI, model="gemini-2.5-pro",
                ),
            ),
        })

        with ticket_metrics.ticket_execution_scope():
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        token_events = [event for event in emitted if event[0] == "ticket_llm_tokens"]
        assert token_events == [
            ("ticket_llm_tokens", 20, {"reason": "input"}),
            ("ticket_llm_tokens", 40, {"reason": "output"}),
            ("ticket_llm_tokens", 7, {"reason": "input"}),
            ("ticket_llm_tokens", 13, {"reason": "output"}),
        ]
        costs = [value for metric, value, _labels in emitted
                 if metric == "ticket_llm_cost_usd"]
        assert costs == pytest.approx([
            (20 * 5.0 + 40 * 30.0) / 1_000_000,
            (7 * 1.25 + 13 * 10.0) / 1_000_000,
        ])

    @pytest.mark.asyncio
    async def test_forced_fallback_is_observable(
        self, router_with_openai, router_with_gemini, monkeypatch,
    ):
        router, _ = router_with_openai
        _, mock_gemini = router_with_gemini
        router._gemini_client = Mock()
        router._gemini_client.aio = Mock()
        router._gemini_client.aio.models = Mock()
        router._gemini_client.aio.models.generate_content = mock_gemini
        mock_gemini.return_value = _make_gemini_response('{"ok": true}')
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "classify_inquiry": TaskRoute(
                primary=ModelConfig(
                    provider=LLMProvider.OPENAI, model="gpt-5.5",
                ),
                fallback=ModelConfig(
                    provider=LLMProvider.GEMINI, model="gemini-2.5-pro",
                ),
            ),
        })

        with ticket_metrics.ticket_execution_scope():
            await router.call(
                "classify_inquiry", "sys", "usr", max_tokens=800,
                force_fallback=True,
            )

        assert emitted[0] == (
            "ticket_llm_fallback_count", 1, {"code": "used"}
        )

    @pytest.mark.asyncio
    async def test_core_llm_call_does_not_emit_ticket_metrics(
        self, router_with_openai, monkeypatch,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-5.5",
            )),
        })

        await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert emitted == []

    @pytest.mark.asyncio
    async def test_unreviewed_model_never_emits_a_false_cost(
        self, router_with_openai, monkeypatch,
    ):
        router, mock_create = router_with_openai
        mock_create.return_value = _make_openai_response('{"ok": true}')
        router.configure_pricing({
            ("openai", "gpt-5.5"): LLMPricing(5.0, 30.0),
        })
        emitted = []
        monkeypatch.setattr(
            "data_pipeline.llm_router.ticket_metrics.emit",
            lambda metric, value, **labels: emitted.append(
                (metric, value, labels)
            ),
        )
        router.configure_routes({
            "gr_outcome": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.OPENAI, model="gpt-unreviewed",
            )),
        })

        with ticket_metrics.ticket_execution_scope():
            await router.call("gr_outcome", "sys", "usr", max_tokens=800)

        assert [metric for metric, _value, _labels in emitted] == [
            "ticket_llm_fallback_count",
            "ticket_llm_tokens",
            "ticket_llm_tokens",
        ]

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

    @pytest.mark.asyncio
    async def test_empty_error_and_logs_never_retain_provider_values(
        self, router_with_gemini, caplog,
    ):
        router, mock_generate = router_with_gemini
        sentinel = "Jane Doe jane@example.com participant-158948"
        response = _make_gemini_response(None)
        response.candidates = [SimpleNamespace(finish_reason=sentinel)]
        response.usage_metadata = SimpleNamespace(secret=sentinel)
        mock_generate.return_value = response
        router.configure_routes({
            "gr_response": TaskRoute(primary=ModelConfig(
                provider=LLMProvider.GEMINI, model="gemini-2.5-flash",
            )),
        })

        with caplog.at_level("WARNING"):
            with pytest.raises(LLMEmptyResponseError) as caught:
                await router.call("gr_response", "sys", "usr", max_tokens=500)

        assert sentinel not in caplog.text
        assert sentinel not in str(caught.value)
        assert sentinel not in repr(caught.value.__dict__)


# ---------------------------------------------------------------------------
# Routing table builder
# ---------------------------------------------------------------------------

class TestRoutingTable:

    def test_unknown_task_raises(self, router_with_openai):
        router, _ = router_with_openai
        router.configure_routes({})
        import asyncio
        with pytest.raises(ValueError, match="No route configured"):
            asyncio.run(
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

    def test_model_config_from_name_exact_gpt6_sol_uses_medium_effort(self):
        cfg = _model_config_from_name("gpt-6-sol")
        assert cfg.provider == LLMProvider.OPENAI
        assert cfg.model == "gpt-6-sol"
        assert cfg.reasoning_effort == "medium"
        assert cfg.temperature == 0.1

    def test_model_config_from_name_non_exact_gpt6_has_no_effort(self):
        for model_name in ("gpt-6", "gpt-6-sol-preview", "GPT-6-SOL-mini"):
            cfg = _model_config_from_name(model_name)
            assert cfg.model == model_name
            assert cfg.reasoning_effort is None

    def test_gpt6_sol_primary_keeps_cross_provider_fallback(self):
        routes = build_routes_from_settings(
            _all_route_settings(LLM_ROUTE_GR_OUTCOME="gpt-6-sol")
        )
        primary = routes["gr_outcome"].primary
        fallback = routes["gr_outcome"].fallback
        assert primary.provider == LLMProvider.OPENAI
        assert primary.model == "gpt-6-sol"
        assert primary.reasoning_effort == "medium"
        assert fallback.provider == LLMProvider.GEMINI
        assert fallback.model == "gemini-2.5-pro"
        assert routes["decompose"].primary.model == "gpt-5.5"
        assert routes["decompose"].primary.reasoning_effort == "medium"
        assert routes["classify_inquiry"].primary.model == "gpt-5.5-mini"
        assert routes["classify_inquiry"].primary.reasoning_effort == "medium"

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
            LLM_ROUTE_CLASSIFY="gpt-5.5-mini",
            LLM_ROUTE_EXTRACT_INQUIRIES="gpt-5.5",
            LLM_ROUTE_KB_QUESTION_SYNTHESIS="gpt-5.5",
            LLM_ROUTE_FORUSBOTS_FIELD_MAP="gpt-5.5",
            LLM_ROUTE_GR_BODY_BUILD="gpt-5.5",
            LLM_ROUTE_TICKET_FIELD_EXTRACT="gpt-5.5",
            LLM_ROUTE_VERIFY_COVERAGE="gemini-2.5-flash",
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
            LLM_ROUTE_CLASSIFY="gpt-5.5-mini",
            LLM_ROUTE_EXTRACT_INQUIRIES="gpt-5.5",
            LLM_ROUTE_KB_QUESTION_SYNTHESIS="gpt-5.5",
            LLM_ROUTE_FORUSBOTS_FIELD_MAP="gpt-5.5",
            LLM_ROUTE_GR_BODY_BUILD="gpt-5.5",
            LLM_ROUTE_TICKET_FIELD_EXTRACT="gpt-5.5",
            LLM_ROUTE_VERIFY_COVERAGE="gemini-2.5-flash",
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
            LLM_ROUTE_CLASSIFY="gpt-5.5-mini",
            LLM_ROUTE_EXTRACT_INQUIRIES="gpt-5.5",
            LLM_ROUTE_KB_QUESTION_SYNTHESIS="gpt-5.5",
            LLM_ROUTE_FORUSBOTS_FIELD_MAP="gpt-5.5",
            LLM_ROUTE_GR_BODY_BUILD="gpt-5.5",
            LLM_ROUTE_TICKET_FIELD_EXTRACT="gpt-5.5",
            LLM_ROUTE_VERIFY_COVERAGE="gpt-5.5",
        )
        routes = build_routes_from_settings(settings)
        for route in routes.values():
            assert route.primary.provider == LLMProvider.OPENAI
            assert route.fallback.provider == LLMProvider.GEMINI


class TestExplicitPricing:
    def test_strict_json_parses_exact_provider_model_map(self):
        parsed = parse_llm_pricing_json(
            """{
              "pricing_as_of": "2026-07-21",
              "source": "openai-google-official-public-pricing",
              "models": {
                "openai:gpt-5.5": {
                  "input_usd_per_million": 5,
                  "output_usd_per_million": 30
                },
                "gemini:gemini-2.5-pro": {
                  "input_usd_per_million": 1.25,
                  "output_usd_per_million": 10
                }
              }
            }"""
        )

        assert parsed == {
            ("openai", "gpt-5.5"): LLMPricing(5.0, 30.0),
            ("gemini", "gemini-2.5-pro"): LLMPricing(1.25, 10.0),
        }

    @pytest.mark.parametrize(
        "raw",
        [
            "[]",
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"openai:gpt-5.5":{"input_usd_per_million":5}}}',
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"openai:gpt-5.5":{"input_usd_per_million":5,'
            '"output_usd_per_million":30,"cached":0.5}}}',
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"openai:gpt-5.5":{"input_usd_per_million":NaN,'
            '"output_usd_per_million":30}}}',
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"openai:gpt-5.5":{"input_usd_per_million":-1,'
            '"output_usd_per_million":30}}}',
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"OpenAI:gpt-5.5":{"input_usd_per_million":5,'
            '"output_usd_per_million":30}}}',
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"openai:gemini-2.5-pro":{"input_usd_per_million":5,'
            '"output_usd_per_million":30}}}',
            '{"pricing_as_of":"2026-07-21","source":"official","models":'
            '{"openai:gpt-5.5":{"input_usd_per_million":5,'
            '"output_usd_per_million":30},"openai:gpt-5.5":'
            '{"input_usd_per_million":6,"output_usd_per_million":31}}}',
            '{"pricing_as_of":"2026/07/21","source":"official","models":{}}',
            '{"pricing_as_of":"2026-07-21","source":"https://secret",'
            '"models":{}}',
        ],
    )
    def test_strict_json_rejects_ambiguous_or_unbounded_prices(self, raw):
        with pytest.raises(ValueError, match="pricing"):
            parse_llm_pricing_json(raw)

    def test_required_keys_include_primary_and_cross_provider_fallback(self):
        routes = {
            "classify": TaskRoute(
                primary=ModelConfig(
                    provider=LLMProvider.GEMINI,
                    model="gemini-2.5-flash",
                ),
                fallback=ModelConfig(
                    provider=LLMProvider.OPENAI,
                    model="gpt-5.5",
                ),
            ),
        }

        assert required_pricing_keys(routes) == frozenset({
            ("gemini", "gemini-2.5-flash"),
            ("openai", "gpt-5.5"),
        })
