"""
Unit tests for the coverage-driven Inquiry Router classifier engine.

Covers:
- Deterministic predicates (now hints, not authoritative).
- The ``CoveragePack`` dataclass and its prompt rendering.
- The classify() flow end-to-end with a mocked LLM and a mocked coverage
  pack builder — exercising the routes (KQ punctual on procedural article,
  GR eligibility, GR transactional, NMI unclear, NMI no-coverage), retrieval
  failure / empty states, low-confidence fallback, and the
  ``KQ_TOP_SCORE_FLOOR`` safety net.
- The lenient JSON parser (markdown fences, JSON-in-chatter, truncation).
- The input normalizer (email scaffolding, signatures, inline emails).
- The ``user_message`` and ``coverage_basis`` contracts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_pipeline.inquiry_router import (
    CONFIDENCE_FALLBACK_THRESHOLD,
    KQ_TOP_SCORE_FLOOR,
    CoveragePack,
    InquiryRouterEngine,
    _has_eligibility_verb,
    _has_first_person_status,
    _is_short_interrogative,
    _normalize_inquiry,
    _resolve_coverage_basis,
    _resolve_user_message,
    _safe_parse_classifier_json,
    compute_deterministic_features,
)
from data_pipeline.llm_router import LLMResponse


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_router():
    """LLMRouter stub with an awaitable ``call`` method."""
    router = SimpleNamespace()
    router.call = AsyncMock()
    return router


def _llm_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        provider_used="gemini",
        model_used="gemini-2.5-flash",
    )


def _chunk(
    article_title: str,
    chunk_type: str,
    score: float,
    chunk_tier: str = "high",
    topic: str = "distribution",
    content: str = "",
) -> dict:
    """Shape a Pinecone-style hit the engine knows how to read."""
    return {
        "id": f"{article_title}-{chunk_type}",
        "score": score,
        "metadata": {
            "article_title": article_title,
            "chunk_type": chunk_type,
            "chunk_tier": chunk_tier,
            "topic": topic,
            "content": content or f"sample {chunk_type} content for {article_title}",
        },
    }


def _make_pack_builder(pack: CoveragePack) -> AsyncMock:
    """Build a coverage_pack_builder mock that returns ``pack``."""
    builder = AsyncMock()
    builder.return_value = pack
    return builder


def _ok_pack(chunks: list, top_score: float | None = None) -> CoveragePack:
    distinct_articles: list[str] = []
    chunk_types_present: list[str] = []
    for c in chunks:
        md = c.get("metadata", {}) or {}
        title = md.get("article_title")
        if title and title not in distinct_articles:
            distinct_articles.append(title)
        ct = md.get("chunk_type")
        if ct and ct not in chunk_types_present:
            chunk_types_present.append(ct)
    return CoveragePack(
        retrieval_status="ok",
        top_score=top_score if top_score is not None else max(
            (c.get("score", 0.0) for c in chunks), default=0.0
        ),
        chunk_count=len(chunks),
        distinct_articles=distinct_articles,
        chunk_types_present=chunk_types_present,
        chunks=chunks,
    )


def _engine(
    mock_llm_router,
    pack: CoveragePack | None = None,
) -> tuple[InquiryRouterEngine, AsyncMock | None]:
    """Build an engine wired up with a pack builder returning ``pack``."""
    builder = _make_pack_builder(pack) if pack is not None else None
    engine = InquiryRouterEngine(
        llm_router=mock_llm_router,
        coverage_pack_builder=builder,
    )
    return engine, builder


# ---------------------------------------------------------------------------
# 1) Deterministic features — kept as informative hints, not authoritative
# ---------------------------------------------------------------------------

class TestDeterministicFeatures:

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

    def test_loan_signal_positive(self):
        signals = compute_deterministic_features("I want to borrow against my 401k")
        assert signals["loan_signal"] is True

    def test_separation_signal_positive(self):
        signals = compute_deterministic_features("I left my job last month")
        assert signals["separation_signal"] is True

    def test_transactional_intent_id_like_to_rollover(self):
        signals = compute_deterministic_features(
            "Hi, I'd like to roll over my 401k into my Fidelity account. "
            "Can you help me with that please?"
        )
        assert signals["has_action_verb"] is True
        assert signals["wants_funds"] is True
        assert signals["transactional_intent"] is True

    def test_transactional_intent_negative_education(self):
        signals = compute_deterministic_features(
            "what is the 60-day rollover rule?"
        )
        assert signals["has_action_verb"] is False
        assert signals["transactional_intent"] is False


# ---------------------------------------------------------------------------
# 2) CoveragePack dataclass + prompt rendering
# ---------------------------------------------------------------------------

class TestCoveragePack:

    def test_empty_pack_renders_summary_only(self):
        pack = CoveragePack.empty()
        block = pack.to_prompt_block()
        assert "retrieval_status: empty" in block
        assert "top_score: 0.00" in block
        assert "chunk_count: 0" in block
        # No "chunks:" line because there's nothing to list.
        assert "  chunks:" not in block

    def test_failed_pack_includes_error(self):
        pack = CoveragePack.failed("PineconeRetrievalError")
        block = pack.to_prompt_block()
        assert "retrieval_status: failed" in block
        assert "pinecone_error: PineconeRetrievalError" in block

    def test_ok_pack_renders_chunk_details(self):
        chunks = [
            _chunk("Hardship Article", "business_rules", 0.62, topic="hardship"),
            _chunk("Hardship Article", "steps", 0.55, topic="hardship"),
        ]
        pack = _ok_pack(chunks)
        block = pack.to_prompt_block()
        assert "retrieval_status: ok" in block
        assert "top_score: 0.62" in block
        assert "chunk_count: 2" in block
        assert "['Hardship Article']" in block
        assert "type=business_rules" in block
        assert "type=steps" in block
        assert "Hardship Article" in block

    def test_signals_dict_contract(self):
        pack = _ok_pack([_chunk("X", "definitions", 0.5)])
        d = pack.signals_dict()
        assert set(d.keys()) == {
            "retrieval_status",
            "top_score",
            "chunk_count",
            "distinct_articles",
            "chunk_types_present",
            "pinecone_error",
        }
        assert d["retrieval_status"] == "ok"
        assert d["pinecone_error"] is None

    def test_chunk_excerpt_truncated(self):
        long_content = "a" * 1000
        chunks = [
            _chunk("X", "business_rules", 0.5, content=long_content),
        ]
        pack = _ok_pack(chunks)
        block = pack.to_prompt_block()
        # Excerpt should be capped well below 1000 chars and end with the
        # ellipsis marker the renderer appends.
        assert "..." in block
        # The original full string must not appear verbatim.
        assert long_content not in block


# ---------------------------------------------------------------------------
# 3) Coverage-driven routing — the LLM is the authority but its verdict is
#    grounded in the chunks it sees
# ---------------------------------------------------------------------------

class TestCoverageDrivenRouting:

    @pytest.mark.asyncio
    async def test_punctual_question_on_procedural_article_routes_kq(
        self, mock_llm_router
    ):
        # "When does the hardship check arrive?" — punctual timeline from a
        # business_rules chunk inside the (otherwise procedural) hardship
        # article. The LLM should pick KQ when given that chunk.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "business_rules chunk states the timeline directly", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk("Hardship Withdrawal", "business_rules", 0.62, topic="hardship"),
                _chunk("Hardship Withdrawal", "steps", 0.50, topic="hardship"),
            ]
        )
        engine, builder = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(
            inquiry="When does the hardship check arrive?"
        )

        builder.assert_awaited_once()
        assert result.route == "knowledge_question"
        assert result.metadata["coverage_basis"] == "kb_direct_answer"
        assert result.metadata["coverage_signals"]["retrieval_status"] == "ok"
        assert result.metadata["coverage_signals"]["top_score"] == pytest.approx(0.62)

    @pytest.mark.asyncio
    async def test_eligibility_question_with_decision_guide_routes_gr(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.92, '
            '"reasoning": "decision_guide + required_data chunks require eligibility", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk("Hardship Withdrawal", "decision_guide", 0.71, topic="hardship"),
                _chunk(
                    "Hardship Withdrawal",
                    "required_data_must_have",
                    0.66,
                    topic="hardship",
                ),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(
            inquiry="Am I eligible for a hardship withdrawal?"
        )

        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"
        assert result.user_message is None

    @pytest.mark.asyncio
    async def test_transactional_request_routes_gr(self, mock_llm_router):
        # Bug-report inquiry: "I'd like to roll over my 401k into Fidelity".
        # No fast-path anymore; LLM sees a decision_guide chunk and emits GR.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.9, '
            '"reasoning": "outgoing rollover requires eligibility evaluation", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk(
                    "Outgoing Rollover", "decision_guide", 0.68, topic="rollover"
                ),
                _chunk(
                    "Outgoing Rollover",
                    "required_data_must_have",
                    0.65,
                    topic="rollover",
                ),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(
            inquiry=(
                "Hi, I'd like to roll over my 401k into my Fidelity account. "
                "Can you help me with that please?"
            )
        )

        assert result.route == "generate_response"
        # Fast-path is gone — every classification now goes through the LLM.
        assert result.fast_path_hit is False

    @pytest.mark.asyncio
    async def test_topic_unclear_routes_nmi(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.85, '
            '"reasoning": "inquiry mentions plan but no identifiable topic", '
            '"coverage_basis": "topic_unclear", '
            '"user_message": "Could you tell me what topic you need help with?"}'
        )
        # Even a high-score retrieval can't save an inherently unclear question —
        # but in practice such retrievals are also weak. Use a weak pack.
        pack = _ok_pack(
            [
                _chunk("Plan Overview", "definitions", 0.32, topic="general"),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(inquiry="I have a question about my plan")

        assert result.route == "needs_more_info"
        assert result.metadata["coverage_basis"] == "topic_unclear"
        assert (
            result.user_message
            == "Could you tell me what topic you need help with?"
        )

    @pytest.mark.asyncio
    async def test_topically_adjacent_routes_nmi(self, mock_llm_router):
        # Address-update inquiry, but retrieval only returns distribution-flavor
        # chunks. The LLM should pick NMI with coverage_basis=no_coverage.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.8, '
            '"reasoning": "retrieved chunks describe distributions, not address updates", '
            '"coverage_basis": "no_coverage", '
            '"user_message": "Could you confirm what you\'re trying to update?"}'
        )
        pack = _ok_pack(
            [
                _chunk("Force Out", "business_rules", 0.45, topic="force_out"),
                _chunk("EACA Refunds", "definitions", 0.40, topic="contributions"),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(
            inquiry="How do I update my address on file?"
        )

        assert result.route == "needs_more_info"
        assert result.metadata["coverage_basis"] == "no_coverage"

    @pytest.mark.asyncio
    async def test_incoming_rollover_with_only_outgoing_chunks_routes_nmi(
        self, mock_llm_router
    ):
        # The exact bug-report regression: incoming rollover question but the
        # KB only has outgoing rollover content. LLM must recognize the
        # direction mismatch and pick NMI.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.85, '
            '"reasoning": "retrieved chunks cover outgoing rollover only, not incoming", '
            '"coverage_basis": "no_coverage", '
            '"user_message": "Just to be sure — do you want to bring money INTO your plan, or move it out?"}'
        )
        pack = _ok_pack(
            [
                _chunk("Outgoing Rollover", "decision_guide", 0.60, topic="rollover"),
                _chunk("Outgoing Rollover", "business_rules", 0.55, topic="rollover"),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(
            inquiry=(
                "Hi there, I want to rollover a 401k from a previous employer... "
                "how can I do that?"
            )
        )

        assert result.route == "needs_more_info"
        assert result.metadata["coverage_basis"] == "no_coverage"
        assert result.user_message is not None

    # ------------------------------------------------------------------
    # PARTICIPANT-INTENT OVERRIDE regression tests
    # ------------------------------------------------------------------
    # These pin the expected post-fix verdict for inquiries that have
    # transactional/eligibility intent over the participant's own funds,
    # even when the retrieved chunks are procedural (steps, faqs, examples,
    # business_rules) — the kind that pre-fix the LLM mis-classified as KQ.
    # The LLM is mocked: the assertion is that the engine wires the
    # post-fix verdict through to the resolver/metadata correctly.

    @pytest.mark.asyncio
    async def test_loan_start_routes_gr_despite_procedural_chunks(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.85, '
            '"reasoning": "transactional intent on own funds; procedural chunks '
            'are not a substitute for eligibility evaluation", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk("401(k) Loan Guide", "steps", 0.61, topic="loan"),
                _chunk("401(k) Loan Guide", "faqs", 0.55, topic="loan"),
                _chunk("401(k) Loan Guide", "references", 0.50, topic="loan"),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)
        result = await engine.classify(
            inquiry="I want to take a loan from my 401(k), how do I start?"
        )
        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"

    @pytest.mark.asyncio
    async def test_move_to_ira_routes_gr_despite_termination_steps(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.88, '
            '"reasoning": "transactional intent over own funds; examples + steps '
            'illustrate the procedure but do not evaluate THIS participant", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk(
                    "Termination Rollover", "examples", 0.64, topic="rollover"
                ),
                _chunk("Termination Rollover", "steps", 0.58, topic="rollover"),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)
        result = await engine.classify(
            inquiry="Help me move my balance to an IRA at Schwab"
        )
        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"

    @pytest.mark.asyncio
    async def test_separated_options_routes_gr_despite_faq_list(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.9, '
            '"reasoning": "participant separated with specific balance — needs '
            'per-option eligibility evaluation, FAQ is only the menu", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk(
                    "Post-Separation Options", "faqs", 0.66, topic="distribution"
                ),
                _chunk(
                    "Post-Separation Options", "steps", 0.58, topic="distribution"
                ),
                _chunk(
                    "Post-Separation Options", "examples", 0.52, topic="distribution"
                ),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)
        result = await engine.classify(
            inquiry="I separated last week with $80k, what are my options?"
        )
        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"

    @pytest.mark.asyncio
    async def test_eligibility_with_balance_routes_gr_despite_business_rule(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.87, '
            '"reasoning": "eligibility verb + participant-specific balance — '
            'force-out threshold is an input to the evaluation, not the answer", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk("Force Out", "business_rules", 0.63, topic="force_out"),
                _chunk("Force Out", "examples", 0.55, topic="force_out"),
                _chunk(
                    "Force Out", "additional_notes", 0.48, topic="force_out"
                ),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)
        result = await engine.classify(
            inquiry="Am I eligible to take a distribution if my balance is only $400?"
        )
        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"

    @pytest.mark.asyncio
    async def test_in_service_age_routes_gr_despite_faq_rule(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.86, '
            '"reasoning": "eligibility verb + first-person age and status — '
            'needs plan-specific eligibility flow against age 59½ rules", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk(
                    "In-Service Withdrawal", "examples", 0.60, topic="in_service"
                ),
                _chunk(
                    "In-Service Withdrawal", "faqs", 0.55, topic="in_service"
                ),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)
        result = await engine.classify(
            inquiry="Can I do in-service withdrawal? I'm 55 and still working"
        )
        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"

    @pytest.mark.asyncio
    async def test_rollover_to_new_employer_routes_gr_despite_definition(
        self, mock_llm_router
    ):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.88, '
            '"reasoning": "transactional intent over own funds; definitions '
            'state rollovers CAN go to another plan but THIS rollover needs '
            'the participant\'s facts", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        pack = _ok_pack(
            [
                _chunk(
                    "Outgoing Rollover", "examples", 0.63, topic="rollover"
                ),
                _chunk(
                    "Outgoing Rollover", "business_rules", 0.58, topic="rollover"
                ),
                _chunk(
                    "Outgoing Rollover", "definitions", 0.54, topic="rollover"
                ),
                _chunk("Outgoing Rollover", "faqs", 0.50, topic="rollover"),
            ]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)
        result = await engine.classify(
            inquiry="I'd love to roll over my 401k to my new employer's plan"
        )
        assert result.route == "generate_response"
        assert result.metadata["coverage_basis"] == "participant_eligibility"


# ---------------------------------------------------------------------------
# 4) Retrieval failure modes
# ---------------------------------------------------------------------------

class TestRetrievalFailureModes:

    @pytest.mark.asyncio
    async def test_retrieval_empty_steers_nmi(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.9, '
            '"reasoning": "no chunks retrieved", '
            '"coverage_basis": "no_coverage", '
            '"user_message": "Could you share more detail?"}'
        )
        engine, builder = _engine(mock_llm_router, pack=CoveragePack.empty())

        result = await engine.classify(inquiry="some inquiry the KB does not cover")

        builder.assert_awaited_once()
        # Engine should pass the empty pack to the LLM and reflect retrieval_status.
        assert (
            result.metadata["coverage_signals"]["retrieval_status"] == "empty"
        )
        assert result.route == "needs_more_info"

    @pytest.mark.asyncio
    async def test_retrieval_failed_steers_nmi(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.9, '
            '"reasoning": "retrieval failed; cannot confirm coverage", '
            '"coverage_basis": "no_coverage", '
            '"user_message": "Could you share more detail?"}'
        )
        pack = CoveragePack.failed("PineconeRetrievalError")
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(inquiry="any inquiry while Pinecone is down")

        assert (
            result.metadata["coverage_signals"]["retrieval_status"] == "failed"
        )
        assert (
            result.metadata["coverage_signals"]["pinecone_error"]
            == "PineconeRetrievalError"
        )
        assert result.route == "needs_more_info"

    @pytest.mark.asyncio
    async def test_builder_exception_caught_by_engine(self, mock_llm_router):
        # Belt-and-suspenders: a builder that forgot to catch its own exception
        # must not crash the classifier. The engine converts to a failed pack.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.9, '
            '"reasoning": "retrieval failed", '
            '"coverage_basis": "no_coverage", '
            '"user_message": null}'
        )
        builder = AsyncMock(side_effect=RuntimeError("kaboom"))
        engine = InquiryRouterEngine(
            llm_router=mock_llm_router, coverage_pack_builder=builder
        )

        result = await engine.classify(inquiry="anything")

        assert (
            result.metadata["coverage_signals"]["retrieval_status"] == "failed"
        )
        assert (
            result.metadata["coverage_signals"]["pinecone_error"] == "RuntimeError"
        )

    @pytest.mark.asyncio
    async def test_engine_without_builder_proceeds_with_empty_pack(
        self, mock_llm_router
    ):
        # When no builder is wired up (degraded mode / unit tests without pack),
        # the engine still runs — the LLM sees retrieval_status=empty.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "needs_more_info", "confidence": 0.9, '
            '"reasoning": "no retrieval", '
            '"coverage_basis": "no_coverage", '
            '"user_message": null}'
        )
        engine = InquiryRouterEngine(llm_router=mock_llm_router)

        result = await engine.classify(inquiry="any inquiry")

        assert (
            result.metadata["coverage_signals"]["retrieval_status"] == "empty"
        )


# ---------------------------------------------------------------------------
# 5) Coverage gate: low-confidence fallback + KQ_TOP_SCORE_FLOOR safety net
# ---------------------------------------------------------------------------

class TestCoverageGate:

    @pytest.mark.asyncio
    async def test_low_confidence_coerced_to_nmi(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.40, '
            '"reasoning": "weak signal", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        pack = _ok_pack([_chunk("X", "business_rules", 0.55)])
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(inquiry="an ambiguous question")

        assert result.route == "needs_more_info"
        assert result.confidence < CONFIDENCE_FALLBACK_THRESHOLD
        assert "Low confidence" in result.reasoning
        assert "weak signal" in result.reasoning
        assert result.user_message is not None
        assert result.user_message.strip() != ""

    @pytest.mark.asyncio
    async def test_kq_top_score_below_floor_downgraded_to_nmi(
        self, mock_llm_router
    ):
        # LLM confidently says KQ but the top chunk score is below the floor —
        # safety net must downgrade to NMI. Confidence stays high so the
        # confidence-fallback path is NOT what triggers it.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.95, '
            '"reasoning": "claims direct answer", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        # Score below KQ_TOP_SCORE_FLOOR.
        pack = _ok_pack([_chunk("X", "definitions", KQ_TOP_SCORE_FLOOR - 0.05)])
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(inquiry="some question")

        assert result.route == "needs_more_info"
        assert "Safety net" in result.reasoning
        assert "below floor" in result.reasoning
        # Confidence preserved (the LLM was confident — we just don't trust it).
        assert result.confidence == pytest.approx(0.95)
        assert result.user_message is not None

    @pytest.mark.asyncio
    async def test_kq_above_floor_passes_through(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.85, '
            '"reasoning": "covered", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        pack = _ok_pack([_chunk("X", "definitions", KQ_TOP_SCORE_FLOOR + 0.10)])
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(inquiry="some question")

        assert result.route == "knowledge_question"
        assert "Safety net" not in result.reasoning

    @pytest.mark.asyncio
    async def test_gr_with_empty_retrieval_allowed(self, mock_llm_router):
        # GR is allowed even when retrieval is empty — downstream required-data
        # does its own retrieval, so the router shouldn't block.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "generate_response", "confidence": 0.9, '
            '"reasoning": "transactional intent — downstream will retrieve", '
            '"coverage_basis": "participant_eligibility", "user_message": null}'
        )
        engine, _ = _engine(mock_llm_router, pack=CoveragePack.empty())

        result = await engine.classify(inquiry="I want to take a loan")

        assert result.route == "generate_response"
        # Safety net only applies to KQ.
        assert "Safety net" not in result.reasoning


# ---------------------------------------------------------------------------
# 6) JSON parser
# ---------------------------------------------------------------------------

class TestSafeParser:

    def test_empty_content(self):
        parsed, parse_ok = _safe_parse_classifier_json("")
        assert parse_ok is False
        assert parsed["route"] == "needs_more_info"
        assert parsed["confidence"] == 0.0
        assert parsed["user_message"] is None

    def test_invalid_route_coerced(self):
        parsed, parse_ok = _safe_parse_classifier_json(
            '{"route": "delete_everything", "confidence": 0.99, "reasoning": "x"}'
        )
        assert parse_ok is True
        assert parsed["route"] == "needs_more_info"
        assert parsed["coverage_basis"] == "topic_unclear"

    def test_strips_json_markdown_fence(self):
        content = (
            '```json\n{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "ok"}\n```'
        )
        parsed, parse_ok = _safe_parse_classifier_json(content)
        assert parse_ok is True
        assert parsed["route"] == "knowledge_question"

    def test_strips_bare_markdown_fence(self):
        content = '```\n{"route": "generate_response", "confidence": 0.8}\n```'
        parsed, parse_ok = _safe_parse_classifier_json(content)
        assert parse_ok is True
        assert parsed["route"] == "generate_response"

    def test_extracts_json_from_chatter(self):
        content = (
            'Here is my analysis: {"route": "knowledge_question", '
            '"confidence": 0.9, "reasoning": "ok"} done.'
        )
        parsed, parse_ok = _safe_parse_classifier_json(content)
        assert parse_ok is True
        assert parsed["route"] == "knowledge_question"

    def test_truncated_json_still_unparseable(self):
        content = '{"route": "knowledge_question", "confidence": 0.9, "reaso'
        parsed, parse_ok = _safe_parse_classifier_json(content)
        assert parse_ok is False
        assert parsed["route"] == "needs_more_info"


# ---------------------------------------------------------------------------
# 7) Contract resolvers (user_message + coverage_basis)
# ---------------------------------------------------------------------------

class TestUserMessageContract:

    def test_returns_none_for_non_nmi(self):
        assert _resolve_user_message("knowledge_question", "ignored text") is None
        assert _resolve_user_message("generate_response", "ignored text") is None

    def test_returns_text_for_nmi(self):
        assert (
            _resolve_user_message("needs_more_info", "  Please tell me more.  ")
            == "Please tell me more."
        )

    def test_falls_back_when_missing(self):
        msg = _resolve_user_message("needs_more_info", None)
        assert isinstance(msg, str) and msg.strip() != ""

    def test_falls_back_on_blank(self):
        assert _resolve_user_message("needs_more_info", "   ") is not None


class TestCoverageBasisContract:

    def test_valid_value_passes_through(self):
        assert (
            _resolve_coverage_basis("knowledge_question", "kb_direct_answer")
            == "kb_direct_answer"
        )

    def test_missing_value_defaults_by_route(self):
        assert (
            _resolve_coverage_basis("knowledge_question", None) == "kb_direct_answer"
        )
        assert (
            _resolve_coverage_basis("generate_response", None)
            == "participant_eligibility"
        )
        assert _resolve_coverage_basis("needs_more_info", None) == "topic_unclear"

    def test_unknown_value_defaults_by_route(self):
        assert (
            _resolve_coverage_basis("knowledge_question", "gibberish")
            == "kb_direct_answer"
        )


# ---------------------------------------------------------------------------
# 8) LLM-path retries (Gemini truncation → fallback)
# ---------------------------------------------------------------------------

class TestParseFallback:

    @pytest.mark.asyncio
    async def test_parse_failure_triggers_fallback_retry(self, mock_llm_router):
        mock_llm_router.call.side_effect = [
            _llm_response("not json {{{"),
            _llm_response(
                '{"route": "knowledge_question", "confidence": 0.9, '
                '"reasoning": "ok from fallback", '
                '"coverage_basis": "kb_direct_answer", "user_message": null}'
            ),
        ]
        pack = _ok_pack([_chunk("X", "definitions", 0.65)])
        engine, _ = _engine(mock_llm_router, pack=pack)

        result = await engine.classify(inquiry="any inquiry")

        assert mock_llm_router.call.call_count == 2
        assert (
            mock_llm_router.call.call_args_list[1].kwargs.get("force_fallback")
            is True
        )
        assert result.route == "knowledge_question"
        assert result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_both_parses_fail_returns_nmi(self, mock_llm_router):
        mock_llm_router.call.side_effect = [
            _llm_response("garbage 1"),
            _llm_response("garbage 2"),
        ]
        engine, _ = _engine(mock_llm_router, pack=CoveragePack.empty())

        result = await engine.classify(inquiry="some inquiry")

        assert mock_llm_router.call.call_count == 2
        assert result.route == "needs_more_info"
        assert result.confidence == 0.0
        assert result.user_message is not None

    @pytest.mark.asyncio
    async def test_parse_failure_with_no_fallback_does_not_crash(
        self, mock_llm_router
    ):
        mock_llm_router.call.side_effect = [
            _llm_response("not json"),
            ValueError("force_fallback=True but no fallback configured"),
        ]
        engine, _ = _engine(mock_llm_router, pack=CoveragePack.empty())

        result = await engine.classify(inquiry="some inquiry")

        assert mock_llm_router.call.call_count == 2
        assert result.route == "needs_more_info"
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# 9) Input normalizer
# ---------------------------------------------------------------------------

class TestInputNormalizer:

    def test_strips_request_summary_wrapper(self):
        out = _normalize_inquiry(
            "Request: I would like to roll over my 401k. "
            "Summary: Customer wants rollover help."
        )
        assert "Request:" not in out
        assert "Summary:" not in out
        assert "roll over my 401k" in out
        assert "Customer wants rollover help" in out

    def test_strips_subject_body_wrapper_case_insensitive(self):
        out = _normalize_inquiry(
            "Subject: Rollover. Body: Hi I want to rollover my 401k."
        )
        assert "Subject:" not in out
        assert "Body:" not in out
        assert "I want to rollover my 401k" in out

    def test_strips_from_message_uppercase(self):
        out = _normalize_inquiry(
            "FROM: customer. MESSAGE: I want to rollover my 401k."
        )
        assert "FROM:" not in out
        assert "MESSAGE:" not in out
        assert "I want to rollover my 401k" in out

    def test_replaces_inline_emails(self):
        out = _normalize_inquiry(
            "My old account is at oldemail@example.com, "
            "my new is at newemail+work@anza.xyz - I want to rollover."
        )
        assert "oldemail@example.com" not in out
        assert "newemail+work@anza.xyz" not in out
        assert out.count("EMAIL") == 2

    def test_strips_trailing_dash_signature(self):
        out = _normalize_inquiry(
            "Hi support team, I want to rollover my 401k. Thanks. -- John Smith"
        )
        assert "-- John" not in out
        assert "rollover my 401k" in out

    def test_strips_trailing_pleasantry(self):
        out = _normalize_inquiry(
            "I want to rollover my 401k from prior employer. Thanks!"
        )
        assert out.lower().rstrip(" .!,").endswith("prior employer")

    def test_clean_input_idempotent(self):
        clean = "How do I rollover my 401k from a previous employer?"
        assert _normalize_inquiry(clean) == clean

    def test_empty_input_returns_empty(self):
        assert _normalize_inquiry("") == ""
        assert _normalize_inquiry(None) == ""

    def test_only_metadata_labels_returns_raw(self):
        raw = "Subject: Body:"
        out = _normalize_inquiry(raw)
        assert out

    def test_collapses_whitespace(self):
        out = _normalize_inquiry("Hi\n\n  I  want  to\trollover.")
        assert "  " not in out
        assert "\n" not in out
        assert "\t" not in out


# ---------------------------------------------------------------------------
# 10) Engine integration: normalization propagates to LLM prompt + builder
# ---------------------------------------------------------------------------

class TestNormalizationIntegration:

    @pytest.mark.asyncio
    async def test_wrappered_inquiry_normalized_before_llm(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "ok", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        pack = _ok_pack([_chunk("Vesting", "definitions", 0.55)])
        engine, _ = _engine(mock_llm_router, pack=pack)

        await engine.classify(
            inquiry=(
                "Request: what is my plan's vesting schedule for the "
                "employer match? Summary: customer asks about vesting."
            )
        )

        kwargs = mock_llm_router.call.call_args.kwargs
        assert "Request:" not in kwargs["user_prompt"]
        assert "Summary:" not in kwargs["user_prompt"]

    @pytest.mark.asyncio
    async def test_pack_builder_receives_normalized_inquiry(self, mock_llm_router):
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "ok", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        builder = _make_pack_builder(
            _ok_pack([_chunk("X", "definitions", 0.55)])
        )
        engine = InquiryRouterEngine(
            llm_router=mock_llm_router, coverage_pack_builder=builder
        )

        await engine.classify(
            inquiry="Request: How do I rollover. Summary: User wants rollover help."
        )

        forwarded = builder.call_args[0][0]
        assert "Request:" not in forwarded
        assert "Summary:" not in forwarded

    @pytest.mark.asyncio
    async def test_coverage_block_embedded_in_user_prompt(self, mock_llm_router):
        # Sanity: the coverage block actually reaches the LLM user prompt.
        mock_llm_router.call.return_value = _llm_response(
            '{"route": "knowledge_question", "confidence": 0.9, '
            '"reasoning": "ok", '
            '"coverage_basis": "kb_direct_answer", "user_message": null}'
        )
        pack = _ok_pack(
            [_chunk("Hardship Article", "business_rules", 0.62, topic="hardship")]
        )
        engine, _ = _engine(mock_llm_router, pack=pack)

        await engine.classify(inquiry="When does the hardship check arrive?")

        kwargs = mock_llm_router.call.call_args.kwargs
        assert "RETRIEVED_COVERAGE:" in kwargs["user_prompt"]
        assert "Hardship Article" in kwargs["user_prompt"]
        assert "type=business_rules" in kwargs["user_prompt"]
