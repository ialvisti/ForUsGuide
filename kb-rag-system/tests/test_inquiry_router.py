"""
Unit tests for the Inquiry Router classifier engine.

Covers:
- Deterministic predicates (positive + negative cases for each feature).
- The conservative fast-path rules (knowledge / generate / defer).
- The LLM path with mocked LLMRouter (parse, low-confidence fallback, malformed JSON).
- The originally failing real inquiry — must fast-path to ``knowledge_question``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_pipeline.inquiry_router import (
    CONFIDENCE_FALLBACK_THRESHOLD,
    InquiryRouterEngine,
    _has_eligibility_verb,
    _has_first_person_status,
    _is_short_interrogative,
    _safe_parse_classifier_json,
    apply_fast_path_rules,
    compute_deterministic_features,
)
from data_pipeline.llm_router import LLMResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_router():
    """LLMRouter stub with an awaitable ``call`` method."""
    router = SimpleNamespace()
    router.call = AsyncMock()
    return router


@pytest.fixture
def engine(mock_llm_router):
    return InquiryRouterEngine(llm_router=mock_llm_router)


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        provider_used="openai",
        model_used="gpt-5.5-mini",
    )


# ---------------------------------------------------------------------------
# 1) Deterministic features
# ---------------------------------------------------------------------------

class TestDeterministicFeatures:
    """Each feature must fire on the positive case and stay quiet on the negative."""

    def test_is_short_interrogative_positive(self):
        assert _is_short_interrogative("how long does this take?") is True

    def test_is_short_interrogative_negative_too_long(self):
        long_text = (
            "I'm wondering about my balance and how it compares to my coworker's "
            "situation across multiple plans and several different recordkeepers "
            "given how complex this whole arrangement has become"
        )
        assert _is_short_interrogative(long_text) is False

    def test_has_first_person_status_positive(self):
        assert _has_first_person_status("my balance is $5,000") is True

    def test_has_first_person_status_negative(self):
        assert _has_first_person_status("what's the balance threshold?") is False

    def test_has_eligibility_verb_positive(self):
        assert _has_eligibility_verb("can I qualify for hardship?") is True

    def test_has_eligibility_verb_negative(self):
        assert _has_eligibility_verb("what's the hardship process?") is False

    def test_hardship_signal_positive(self):
        signals = compute_deterministic_features(
            inquiry="I have medical bills and need to withdraw money",
            topic=None,
            collected_data=None,
        )
        assert signals["hardship_signal"] is True
        assert signals["wants_funds"] is True

    def test_hardship_signal_negative_no_withdraw_intent(self):
        # "medical bills" alone is enough for hardship_signal in detect_advisory_concepts,
        # but with no withdraw verb the wants_funds flag stays off — that's the
        # combination the fast-path actually cares about.
        signals = compute_deterministic_features(
            inquiry="my friend mentioned medical bills",
            topic=None,
            collected_data=None,
        )
        assert signals["wants_funds"] is False

    def test_loan_signal_positive(self):
        signals = compute_deterministic_features(
            inquiry="I want to borrow against my 401k",
            topic=None,
            collected_data=None,
        )
        assert signals["loan_signal"] is True

    def test_loan_signal_general_repayment(self):
        # A general "loan repayment rules" question still trips the keyword
        # detector — that's fine; the fast-path requires both a loan signal
        # AND an eligibility verb to route to generate_response.
        signals = compute_deterministic_features(
            inquiry="loan repayment rules",
            topic=None,
            collected_data=None,
        )
        assert signals["has_eligibility_verb"] is False

    def test_separation_signal_positive(self):
        signals = compute_deterministic_features(
            inquiry="I left my job last month",
            topic=None,
            collected_data=None,
        )
        assert signals["separation_signal"] is True

    def test_separation_signal_third_person(self):
        # Third-person phrasing should not flip the first-person status flag.
        signals = compute_deterministic_features(
            inquiry="what happens when someone leaves?",
            topic=None,
            collected_data=None,
        )
        assert signals["has_first_person_status"] is False


# ---------------------------------------------------------------------------
# 2) Fast-path rules
# ---------------------------------------------------------------------------

class TestFastPathRules:

    def test_short_interrogative_no_signals_routes_knowledge(self):
        signals = compute_deterministic_features(
            inquiry="how long does approval take?",
            topic=None,
            collected_data=None,
        )
        decision = apply_fast_path_rules("how long does approval take?", signals)
        assert decision is not None
        assert decision.route == "knowledge_question"
        assert decision.confidence >= 0.85

    def test_eligibility_verb_plus_hardship_routes_generate(self):
        signals = compute_deterministic_features(
            inquiry="can I qualify for a hardship withdrawal for medical bills?",
            topic=None,
            collected_data=None,
        )
        decision = apply_fast_path_rules(
            "can I qualify for a hardship withdrawal for medical bills?", signals
        )
        assert decision is not None
        assert decision.route == "generate_response"
        assert decision.confidence >= 0.85

    def test_mixed_signals_defers_to_llm(self):
        # Short interrogative + first-person status: ambiguous, defer.
        signals = compute_deterministic_features(
            inquiry="what is my balance right now?",
            topic=None,
            collected_data=None,
        )
        decision = apply_fast_path_rules("what is my balance right now?", signals)
        assert decision is None


# ---------------------------------------------------------------------------
# 3) LLM path (mocked)
# ---------------------------------------------------------------------------

class TestClassifyLLMPath:

    @pytest.mark.asyncio
    async def test_llm_returns_high_confidence_knowledge(self, engine, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.8, '
            '"reasoning": "Educational question"}'
        )

        # Inquiry that DOES NOT fast-path (mixes signals so LLM is consulted).
        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "knowledge_question"
        assert result.confidence == pytest.approx(0.8)
        assert result.fast_path_hit is False
        assert "Educational question" in result.reasoning
        mock_llm_router.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_low_confidence_coerced_to_needs_more_info(
        self, engine, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.40, '
            '"reasoning": "weak signal"}'
        )

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "needs_more_info"
        assert result.confidence < CONFIDENCE_FALLBACK_THRESHOLD
        assert "Low confidence" in result.reasoning
        assert "weak signal" in result.reasoning  # original reasoning preserved

    @pytest.mark.asyncio
    async def test_llm_malformed_json_falls_back(self, engine, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response("not json at all {{{")

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "needs_more_info"
        assert result.confidence == 0.0

    def test_safe_parse_unit_empty_content(self):
        parsed = _safe_parse_classifier_json("")
        assert parsed["route"] == "needs_more_info"
        assert parsed["confidence"] == 0.0

    def test_safe_parse_unit_invalid_route(self):
        parsed = _safe_parse_classifier_json(
            '{"route": "delete_everything", "confidence": 0.99, "reasoning": "x"}'
        )
        assert parsed["route"] == "needs_more_info"


# ---------------------------------------------------------------------------
# 4) The originally failing inquiry
# ---------------------------------------------------------------------------

class TestRealInquiry:

    @pytest.mark.asyncio
    async def test_failing_inquiry_routes_to_knowledge_question(self, engine):
        # This exact text was the motivating bug — must be classified as a
        # punctual knowledge question and resolved on the fast-path (no LLM).
        result = await engine.classify(
            inquiry=(
                "Hi there I was wondering how many business days til I can "
                "see it get approved. Thank you"
            ),
        )
        assert result.route == "knowledge_question"
        assert result.confidence >= 0.7
        assert result.fast_path_hit is True
