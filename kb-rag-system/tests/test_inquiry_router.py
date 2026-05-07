"""
Unit tests for the Inquiry Router classifier engine.

Covers:
- Deterministic predicates (positive + negative cases for each feature).
- The conservative fast-path rules (knowledge / generate / defer).
- The LLM path with mocked LLMRouter (parse, low-confidence fallback, malformed JSON).
- The originally failing real inquiry — must fast-path to ``knowledge_question``.
- The Vestwell rollover regression — heuristics must catch "previous employer" +
  "rollover" so signals carry the rollover intent into the LLM call.
- ``user_message`` contract: populated only on ``needs_more_info``; fallback when
  the LLM omits it; forced ``None`` on other routes.
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
    _resolve_user_message,
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
        provider_used="gemini",
        model_used="gemini-2.5-flash",
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
            "I have medical bills and need to withdraw money"
        )
        assert signals["hardship_signal"] is True
        assert signals["wants_funds"] is True

    def test_hardship_signal_negative_no_withdraw_intent(self):
        # "medical bills" alone is enough for hardship_signal in detect_advisory_concepts,
        # but with no withdraw verb the wants_funds flag stays off — that's the
        # combination the fast-path actually cares about.
        signals = compute_deterministic_features("my friend mentioned medical bills")
        assert signals["wants_funds"] is False

    def test_loan_signal_positive(self):
        signals = compute_deterministic_features("I want to borrow against my 401k")
        assert signals["loan_signal"] is True

    def test_loan_signal_general_repayment(self):
        # A general "loan repayment rules" question still trips the keyword
        # detector — that's fine; the fast-path requires both a loan signal
        # AND an eligibility verb to route to generate_response.
        signals = compute_deterministic_features("loan repayment rules")
        assert signals["has_eligibility_verb"] is False

    def test_separation_signal_positive(self):
        signals = compute_deterministic_features("I left my job last month")
        assert signals["separation_signal"] is True

    def test_separation_signal_third_person(self):
        # Third-person phrasing should not flip the first-person status flag.
        signals = compute_deterministic_features("what happens when someone leaves?")
        assert signals["has_first_person_status"] is False

    def test_rollover_keyword_triggers_wants_funds(self):
        # New: "rollover" alone now flips wants_funds so signals carry intent
        # into the LLM call even when there's no withdraw/cash-out verb.
        signals = compute_deterministic_features(
            "I want to rollover my balance to another provider"
        )
        assert signals["wants_funds"] is True

    def test_previous_employer_triggers_separation(self):
        # New: "previous employer" / "old employer" now count as separation
        # signals even without an explicit "I left/quit" verb.
        signals = compute_deterministic_features(
            "I have an old 401k from a previous employer that I want to move"
        )
        assert signals["separation_signal"] is True


# ---------------------------------------------------------------------------
# 2) Fast-path rules
# ---------------------------------------------------------------------------

class TestFastPathRules:

    def test_short_interrogative_no_signals_routes_knowledge(self):
        signals = compute_deterministic_features("how long does approval take?")
        decision = apply_fast_path_rules("how long does approval take?", signals)
        assert decision is not None
        assert decision.route == "knowledge_question"
        assert decision.confidence >= 0.85

    def test_eligibility_verb_plus_hardship_routes_generate(self):
        signals = compute_deterministic_features(
            "can I qualify for a hardship withdrawal for medical bills?"
        )
        decision = apply_fast_path_rules(
            "can I qualify for a hardship withdrawal for medical bills?", signals
        )
        assert decision is not None
        assert decision.route == "generate_response"
        assert decision.confidence >= 0.85

    def test_mixed_signals_defers_to_llm(self):
        # Short interrogative + first-person status: ambiguous, defer.
        signals = compute_deterministic_features("what is my balance right now?")
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
            '"reasoning": "Educational question", "user_message": null}'
        )

        # Inquiry that DOES NOT fast-path (mixes signals so LLM is consulted).
        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "knowledge_question"
        assert result.confidence == pytest.approx(0.8)
        assert result.fast_path_hit is False
        assert "Educational question" in result.reasoning
        assert result.user_message is None
        mock_llm_router.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_low_confidence_coerced_to_needs_more_info(
        self, engine, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.40, '
            '"reasoning": "weak signal", "user_message": null}'
        )

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "needs_more_info"
        assert result.confidence < CONFIDENCE_FALLBACK_THRESHOLD
        assert "Low confidence" in result.reasoning
        assert "weak signal" in result.reasoning  # original reasoning preserved
        # The LLM said knowledge_question with user_message=null; after coerce we
        # need *some* string for the participant — fall back to the default.
        assert result.user_message is not None
        assert result.user_message.strip() != ""

    @pytest.mark.asyncio
    async def test_llm_malformed_json_falls_back(self, engine, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response("not json at all {{{")

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "needs_more_info"
        assert result.confidence == 0.0
        assert result.user_message is not None  # fallback default

    def test_safe_parse_unit_empty_content(self):
        parsed = _safe_parse_classifier_json("")
        assert parsed["route"] == "needs_more_info"
        assert parsed["confidence"] == 0.0
        assert parsed["user_message"] is None

    def test_safe_parse_unit_invalid_route(self):
        parsed = _safe_parse_classifier_json(
            '{"route": "delete_everything", "confidence": 0.99, "reasoning": "x"}'
        )
        assert parsed["route"] == "needs_more_info"


# ---------------------------------------------------------------------------
# 4) user_message contract
# ---------------------------------------------------------------------------

class TestUserMessageContract:

    def test_resolve_returns_none_for_non_needs_more_info(self):
        assert _resolve_user_message("knowledge_question", "ignored text") is None
        assert _resolve_user_message("generate_response", "ignored text") is None

    def test_resolve_returns_text_for_needs_more_info(self):
        assert (
            _resolve_user_message("needs_more_info", "  Please tell me more.  ")
            == "Please tell me more."
        )

    def test_resolve_falls_back_when_missing(self):
        msg = _resolve_user_message("needs_more_info", None)
        assert isinstance(msg, str) and msg.strip() != ""

    def test_resolve_falls_back_on_blank(self):
        assert _resolve_user_message("needs_more_info", "   ") is not None

    @pytest.mark.asyncio
    async def test_llm_needs_more_info_user_message_populated(
        self, engine, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.85, '
            '"reasoning": "topic unclear", '
            '"user_message": "Could you tell me what topic you need help with?"}'
        )

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "needs_more_info"
        assert result.user_message == "Could you tell me what topic you need help with?"

    @pytest.mark.asyncio
    async def test_llm_omitting_user_message_on_needs_more_info_falls_back(
        self, engine, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.85, '
            '"reasoning": "topic unclear"}'
        )

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "needs_more_info"
        assert isinstance(result.user_message, str)
        assert result.user_message.strip() != ""

    @pytest.mark.asyncio
    async def test_llm_user_message_forced_none_on_other_routes(
        self, engine, mock_llm_router
    ):
        # LLM returned a stray user_message even though route is knowledge_question.
        # Engine must defensively force it to None.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "ok", "user_message": "stray text"}'
        )

        result = await engine.classify(
            inquiry="what is my plan's vesting schedule for employer match?",
        )

        assert result.route == "knowledge_question"
        assert result.user_message is None


# ---------------------------------------------------------------------------
# 5) Real inquiries — regression suite
# ---------------------------------------------------------------------------

class TestRealInquiries:

    @pytest.mark.asyncio
    async def test_punctual_inquiry_routes_to_knowledge_question(self, engine):
        # The original motivating bug — must fast-path to knowledge_question.
        result = await engine.classify(
            inquiry=(
                "Hi there I was wondering how many business days til I can "
                "see it get approved. Thank you"
            ),
        )
        assert result.route == "knowledge_question"
        assert result.confidence >= 0.7
        assert result.fast_path_hit is True
        assert result.user_message is None

    @pytest.mark.asyncio
    async def test_vestwell_rollover_signals_now_carry_intent(
        self, engine, mock_llm_router
    ):
        # Regression for the misclassified Vestwell rollover. The fast-path
        # cannot fire (no eligibility verb), but the deterministic signals must
        # now carry rollover intent into the LLM via wants_funds + separation.
        # We mock the LLM to return knowledge_question (the procedural HOW
        # answer) so we can also validate the user_message=None contract.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.85, '
            '"reasoning": "procedural HOW question about incoming rollover", '
            '"user_message": null}'
        )

        inquiry = (
            "Hi there, I want to rollover a 401k from a previous employer... "
            "how can I do that? The old employer has my 401k in a vestwell account."
        )
        result = await engine.classify(inquiry=inquiry)

        # Signals: the heuristic fix is what unlocks the LLM's ability to
        # disambiguate. Both flags must now be True even without the request
        # carrying a topic hint.
        assert result.signals["wants_funds"] is True
        assert result.signals["separation_signal"] is True

        # And the LLM (with the new prompt) routes it correctly.
        assert result.route == "knowledge_question"
        assert result.fast_path_hit is False
        assert result.user_message is None
