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
    CoverageVerdict,
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
        # The fast-path cannot fire (no eligibility verb), but the
        # deterministic signals must carry rollover intent into the LLM via
        # wants_funds + separation. With no coverage_checker wired (the
        # legacy contract preserved by the default fixture), whatever the
        # LLM returns is passed through unchanged. The KB-coverage downgrade
        # for incoming rollovers is exercised in TestCoverageCheck.
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

        assert result.signals["wants_funds"] is True
        assert result.signals["separation_signal"] is True

        assert result.route == "knowledge_question"
        assert result.fast_path_hit is False
        assert result.user_message is None


# ---------------------------------------------------------------------------
# 6) Coverage-check post-step
# ---------------------------------------------------------------------------

class TestCoverageCheck:
    """The LLM-based coverage_checker downgrades knowledge_question verdicts
    to needs_more_info when the verifier says the retrieved KB chunks do not
    actually answer the inquiry.
    """

    @staticmethod
    def _make_checker(
        is_covered: bool, top_score: float = 0.5, reasoning: str = ""
    ) -> AsyncMock:
        checker = AsyncMock()
        checker.return_value = CoverageVerdict(
            is_covered=is_covered,
            top_score=top_score,
            reasoning=reasoning or ("covered" if is_covered else "not covered"),
        )
        return checker

    @pytest.mark.asyncio
    async def test_incoming_rollover_with_no_coverage_downgrades(
        self, mock_llm_router
    ):
        # The exact bug-report inquiry: LLM confidently says knowledge_question
        # but the verifier (with retrieved outgoing-rollover chunks) decides
        # the KB does not actually answer the incoming-rollover question.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.98, '
            '"reasoning": "covered by the knowledge base", '
            '"user_message": null}'
        )
        checker = self._make_checker(
            is_covered=False,
            top_score=0.60,
            reasoning="Retrieved articles cover outgoing rollover, not incoming.",
        )
        engine = InquiryRouterEngine(
            llm_router=mock_llm_router, coverage_checker=checker
        )

        inquiry = (
            "Hi there, I want to rollover a 401k from a previous employer... "
            "how can I do that? The old employer has my 401k in a vestwell account."
        )
        result = await engine.classify(inquiry=inquiry)

        checker.assert_awaited_once_with(inquiry)
        assert result.route == "needs_more_info"
        assert result.user_message is not None
        assert "KB coverage check rejected" in result.reasoning
        assert result.metadata["kb_coverage_top_score"] == 0.60
        assert (
            result.metadata["kb_coverage_reasoning"]
            == "Retrieved articles cover outgoing rollover, not incoming."
        )

    @pytest.mark.asyncio
    async def test_knowledge_question_with_coverage_passes_through(
        self, mock_llm_router
    ):
        # Procedural HOW with a separation signal — fast-path defers to LLM
        # (the procedural-HOW exception skips the eligibility-verb branch).
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "outgoing rollover HOW question", '
            '"user_message": null}'
        )
        checker = self._make_checker(
            is_covered=True, top_score=0.55, reasoning="LT termination article applies."
        )
        engine = InquiryRouterEngine(
            llm_router=mock_llm_router, coverage_checker=checker
        )

        result = await engine.classify(
            inquiry=(
                "I left my employer last month — how do I roll my 401(k) "
                "balance over to an IRA?"
            )
        )

        assert result.fast_path_hit is False
        assert result.route == "knowledge_question"
        assert result.user_message is None
        assert result.metadata["kb_coverage_top_score"] == 0.55

    @pytest.mark.asyncio
    async def test_generate_response_skips_coverage_check(self, mock_llm_router):
        # Coverage check is gated on route == "knowledge_question" — the
        # checker must not even be awaited for other routes.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.9, '
            '"reasoning": "hardship eligibility", "user_message": null}'
        )
        checker = self._make_checker(is_covered=False)
        engine = InquiryRouterEngine(
            llm_router=mock_llm_router, coverage_checker=checker
        )

        result = await engine.classify(
            inquiry=(
                "I'm still working but need $15k for medical bills, "
                "can I take a hardship?"
            )
        )

        checker.assert_not_awaited()
        assert result.route == "generate_response"
        assert result.metadata["kb_coverage_top_score"] is None

    @pytest.mark.asyncio
    async def test_engine_without_checker_preserves_legacy_behavior(
        self, mock_llm_router
    ):
        # The default constructor (no coverage_checker) must be a strict
        # passthrough of the LLM verdict. This pins the backwards-compat
        # contract for the existing `engine` fixture.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.98, '
            '"reasoning": "covered by KB", "user_message": null}'
        )
        engine = InquiryRouterEngine(llm_router=mock_llm_router)

        result = await engine.classify(inquiry="some inquiry text here")

        assert result.route == "knowledge_question"
        assert result.metadata["kb_coverage_top_score"] is None

    @pytest.mark.asyncio
    async def test_low_confidence_short_circuits_before_checker(
        self, mock_llm_router
    ):
        # If the confidence fallback already coerces to needs_more_info,
        # we save the Pinecone+verifier round-trip — checker must never be awaited.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.4, '
            '"reasoning": "unsure", "user_message": null}'
        )
        checker = self._make_checker(is_covered=True)
        engine = InquiryRouterEngine(
            llm_router=mock_llm_router, coverage_checker=checker
        )

        result = await engine.classify(inquiry="some ambiguous inquiry")

        checker.assert_not_awaited()
        assert result.route == "needs_more_info"
        assert result.metadata["kb_coverage_top_score"] is None
