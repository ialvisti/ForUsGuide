"""
Tests para el RAG Engine.

Tests unitarios para las funciones principales del RAG engine.

The engine now delegates all LLM calls to an `LLMRouter`, so tests build a
mock router that exposes an async `call()` method and pass it into the
`RAGEngine` constructor.
"""

import json as _json
import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, Mock, patch


@pytest.fixture
def mock_router():
    """A minimal LLMRouter double with an awaitable `call()` method."""
    from unittest.mock import AsyncMock
    router = Mock()
    router.call = AsyncMock()
    return router


@pytest.fixture
def mock_rag_engine(mock_router):
    """RAG engine with mocked LLM router and Pinecone."""
    from data_pipeline.rag_engine import RAGEngine
    with patch('data_pipeline.rag_engine.PineconeUploader'):
        return RAGEngine(llm_router=mock_router)


class TestRAGEngine:
    """Tests para RAGEngine."""

    def test_rag_engine_initialization(self, mock_rag_engine):
        """Test que RAG engine se inicializa correctamente."""
        assert mock_rag_engine is not None
        assert mock_rag_engine.router is not None

    def test_rag_engine_requires_router(self):
        """Passing llm_router=None should raise."""
        from data_pipeline.rag_engine import RAGEngine
        with pytest.raises(ValueError, match="llm_router"):
            RAGEngine(llm_router=None)

    def test_calculate_confidence_empty_chunks(self, mock_rag_engine):
        """Test confidence con lista vacía."""
        confidence = mock_rag_engine._calculate_confidence([])
        assert confidence == 0.0

    def test_calculate_confidence_with_chunks(self, mock_rag_engine):
        """Test confidence con chunks."""
        chunks = [
            {'score': 0.8, 'metadata': {'chunk_tier': 'critical'}},
            {'score': 0.7, 'metadata': {'chunk_tier': 'high'}},
            {'score': 0.6, 'metadata': {'chunk_tier': 'medium'}}
        ]
        confidence = mock_rag_engine._calculate_confidence(chunks)
        assert 0.0 <= confidence <= 1.0
        # With a critical chunk, confidence should be above the "uncertain" floor.
        assert confidence > 0.45

    def test_determine_decision_high_confidence(self, mock_rag_engine):
        """Test decision con alta confidence."""
        decision = mock_rag_engine._determine_decision(0.85)
        assert decision == "can_proceed"

    def test_determine_decision_medium_confidence(self, mock_rag_engine):
        """Test decision con media confidence (0.45 <= conf < 0.65 → uncertain)."""
        decision = mock_rag_engine._determine_decision(0.50)
        assert decision == "uncertain"

    def test_determine_decision_low_confidence(self, mock_rag_engine):
        """Test decision con baja confidence."""
        decision = mock_rag_engine._determine_decision(0.3)
        assert decision == "out_of_scope"

    def test_organize_chunks_by_tier(self, mock_rag_engine):
        """Test organización de chunks por tier."""
        chunks = [
            {
                'id': 'chunk1',
                'score': 0.9,
                'metadata': {
                    'chunk_tier': 'critical',
                    'content': 'Critical content'
                }
            },
            {
                'id': 'chunk2',
                'score': 0.7,
                'metadata': {
                    'chunk_tier': 'high',
                    'content': 'High content'
                }
            }
        ]

        organized = mock_rag_engine._organize_chunks_by_tier(chunks)

        assert 'critical' in organized
        assert 'high' in organized
        assert len(organized['critical']) == 1
        assert len(organized['high']) == 1
        assert organized['critical'][0]['content'] == 'Critical content'


class TestConfidenceCalculation:
    """Tests específicos para cálculo de confidence."""

    @pytest.fixture
    def engine(self, mock_router):
        """Engine para tests."""
        from data_pipeline.rag_engine import RAGEngine
        with patch('data_pipeline.rag_engine.PineconeUploader'):
            return RAGEngine(llm_router=mock_router)

    def test_confidence_boost_with_critical_chunks(self, engine):
        """Test que chunks CRITICAL aumentan confidence."""
        chunks_without_critical = [
            {'score': 0.5, 'metadata': {'chunk_tier': 'high'}},
            {'score': 0.5, 'metadata': {'chunk_tier': 'high'}},
            {'score': 0.5, 'metadata': {'chunk_tier': 'high'}}
        ]

        chunks_with_critical = [
            {'score': 0.5, 'metadata': {'chunk_tier': 'critical'}},
            {'score': 0.5, 'metadata': {'chunk_tier': 'critical'}},
            {'score': 0.5, 'metadata': {'chunk_tier': 'high'}}
        ]

        conf_without = engine._calculate_confidence(chunks_without_critical)
        conf_with = engine._calculate_confidence(chunks_with_critical)

        assert conf_with > conf_without


class TestLLMDispatch:
    """Ensure _call_llm correctly delegates to the router with the task_type."""

    @pytest.mark.asyncio
    async def test_call_llm_forwards_task_type(self, mock_router):
        """_call_llm must pass task_type verbatim to router.call."""
        from data_pipeline.rag_engine import RAGEngine
        from data_pipeline.llm_router import LLMResponse

        mock_router.call.return_value = LLMResponse(
            content='{"ok": true}',
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            provider_used="gemini",
            model_used="gemini-2.5-flash",
        )

        with patch('data_pipeline.rag_engine.PineconeUploader'):
            engine = RAGEngine(llm_router=mock_router)

        result = await engine._call_llm(
            system_prompt="sys",
            user_prompt="usr",
            max_tokens=100,
            task_type="decompose",
        )

        mock_router.call.assert_awaited_once_with(
            task_type="decompose",
            system_prompt="sys",
            user_prompt="usr",
            max_tokens=100,
        )
        assert result.content == '{"ok": true}'
        assert result.provider_used == "gemini"
        assert result.model_used == "gemini-2.5-flash"


class TestRequiredDataSafetyNetHelpers:
    """Unit tests for the required_data safety-net predicate and parser."""

    def test_parse_valid_json_extracts_gaps(self, mock_rag_engine):
        response = _json.dumps({
            "participant_data": [],
            "plan_data": [],
            "coverage_gaps": ["missing topic"],
        })
        parsed, gaps = mock_rag_engine._parse_required_data_response(response)
        assert parsed["participant_data"] == []
        assert gaps == ["missing topic"]

    def test_parse_invalid_json_defaults_to_empty(self, mock_rag_engine):
        parsed, gaps = mock_rag_engine._parse_required_data_response("not json at all")
        assert parsed == {"participant_data": [], "plan_data": [], "coverage_gaps": []}
        assert gaps == []

    def test_parse_filters_non_string_and_empty_gaps(self, mock_rag_engine):
        response = '{"participant_data": [], "coverage_gaps": ["real", "", null, 5]}'
        parsed, gaps = mock_rag_engine._parse_required_data_response(response)
        assert gaps == ["real"]

    def test_retry_triggers_on_empty_arrays_with_relevant_retrieval(self, mock_rag_engine):
        parsed = {"participant_data": [], "plan_data": []}
        threshold = mock_rag_engine.RD_RETRIEVAL_MIN_SCORE
        assert mock_rag_engine._should_retry_required_data(parsed, [], threshold + 0.10)

    def test_retry_skipped_when_retrieval_below_threshold(self, mock_rag_engine):
        parsed = {"participant_data": [], "plan_data": []}
        threshold = mock_rag_engine.RD_RETRIEVAL_MIN_SCORE
        assert not mock_rag_engine._should_retry_required_data(parsed, [], threshold - 0.10)

    def test_retry_skipped_when_coverage_gaps_reported(self, mock_rag_engine):
        parsed = {"participant_data": [], "plan_data": []}
        assert not mock_rag_engine._should_retry_required_data(parsed, ["gap"], 0.80)

    def test_retry_skipped_when_fields_non_empty(self, mock_rag_engine):
        parsed = {
            "participant_data": [{
                "field": "termination_date",
                "description": "d",
                "why_needed": "w",
                "data_type": "date",
                "required": True,
            }],
            "plan_data": [],
        }
        assert not mock_rag_engine._should_retry_required_data(parsed, [], 0.80)


class TestRequiredDataSafetyNetIntegration:
    """End-to-end test of get_required_data: safety net must retry with
    force_fallback when the primary LLM returns empty arrays despite rdmh
    chunks clearing the retrieval gate."""

    def _rdmh_chunks(self, score):
        return [{
            "id": "c1",
            "score": score,
            "metadata": {
                "chunk_type": "required_data_must_have",
                "chunk_tier": "critical",
                "article_id": "a1",
                "article_title": "Rollover Article",
                "content": (
                    "# Required Data — Must Have (Portal/Profile Data)\n"
                    "### Termination date\n"
                    "**Description:** date terminated.\n"
                    "**Why needed:** eligibility.\n"
                    "**Source:** participant_profile\n"
                ),
                "topic": "rollover",
            },
        }]

    @pytest.mark.asyncio
    async def test_fallback_retry_populates_fields(self, mock_router):
        from data_pipeline.rag_engine import RAGEngine
        from data_pipeline.llm_router import LLMResponse

        empty_resp = LLMResponse(
            content=_json.dumps({"participant_data": [], "plan_data": []}),
            usage={"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
            provider_used="gemini",
            model_used="gemini-2.5-flash",
        )
        full_resp = LLMResponse(
            content=_json.dumps({
                "participant_data": [{
                    "field": "termination_date",
                    "description": "date terminated",
                    "why_needed": "eligibility",
                    "data_type": "date",
                    "required": True,
                }],
                "plan_data": [],
                "coverage_gaps": [],
            }),
            usage={"prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260},
            provider_used="openai",
            model_used="gpt-5.5",
        )

        call_log = []

        async def router_call(**kwargs):
            call_log.append(kwargs)
            return full_resp if kwargs.get("force_fallback") else empty_resp

        mock_router.call = AsyncMock(side_effect=router_call)

        with patch("data_pipeline.rag_engine.PineconeUploader"):
            engine = RAGEngine(llm_router=mock_router)

        chunks = self._rdmh_chunks(engine.RD_RETRIEVAL_MIN_SCORE + 0.20)

        async def fake_decompose(inquiry, **kwargs):
            return [inquiry]

        async def fake_search(**kwargs):
            return chunks, {f"{inquiry_} rollover": chunks[0]["score"]
                            for inquiry_ in [chunks[0]["metadata"]["article_id"]]}

        engine._decompose_question = fake_decompose
        engine._search_for_required_data = fake_search

        resp = await engine.get_required_data(
            inquiry="How do I roll over my 401k?",
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="rollover",
        )

        assert len(call_log) == 2
        assert call_log[0].get("force_fallback", False) is False
        assert call_log[1].get("force_fallback") is True

        assert len(resp.required_fields["participant_data"]) == 1


        assert resp.required_fields["participant_data"][0]["field"] == "termination_date"
        assert resp.metadata["model"] == "gpt-5.5"
        assert resp.metadata["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_no_retry_when_primary_returns_populated(self, mock_router):
        from data_pipeline.rag_engine import RAGEngine
        from data_pipeline.llm_router import LLMResponse

        primary_resp = LLMResponse(
            content=_json.dumps({
                "participant_data": [{
                    "field": "termination_date",
                    "description": "date",
                    "why_needed": "elig",
                    "data_type": "date",
                    "required": True,
                }],
                "plan_data": [],
                "coverage_gaps": [],
            }),
            usage={"prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260},
            provider_used="gemini",
            model_used="gemini-2.5-flash",
        )

        call_log = []

        async def router_call(**kwargs):
            call_log.append(kwargs)
            return primary_resp

        mock_router.call = AsyncMock(side_effect=router_call)

        with patch("data_pipeline.rag_engine.PineconeUploader"):
            engine = RAGEngine(llm_router=mock_router)

        chunks = self._rdmh_chunks(engine.RD_RETRIEVAL_MIN_SCORE + 0.20)

        async def fake_decompose(inquiry, **kwargs):
            return [inquiry]

        async def fake_search(**kwargs):
            return chunks, {}

        engine._decompose_question = fake_decompose
        engine._search_for_required_data = fake_search

        resp = await engine.get_required_data(
            inquiry="How do I roll over my 401k?",
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="rollover",
        )

        assert len(call_log) == 1
        assert call_log[0].get("force_fallback", False) is False
        assert len(resp.required_fields["participant_data"]) == 1


class TestGenerateResponsePhaseFlow:
    @pytest.mark.asyncio
    async def test_phase1_timeout_continues_to_unified_generation(self, mock_rag_engine):
        from data_pipeline.llm_router import LLMResponse

        chunk = {
            "id": "termination_chunk",
            "score": 0.82,
            "metadata": {
                "article_id": "termination_article",
                "article_title": "Termination Distribution Guide",
                "topic": "termination_distribution_request",
                "chunk_type": "eligibility",
                "chunk_tier": "critical",
                "content": "Active participants need a terminated status and termination date.",
            },
        }

        mock_rag_engine.GR_PHASE1_TIMEOUT_SECONDS = 0.01
        mock_rag_engine._decompose_question = AsyncMock(
            return_value=["termination distribution eligibility"]
        )
        mock_rag_engine._search_for_response_parallel_cascade = AsyncMock(
            return_value=([chunk], {"termination distribution eligibility": 0.82})
        )
        mock_rag_engine._add_response_article_bundles = AsyncMock(
            return_value=([chunk], {"articles_added": []})
        )
        mock_rag_engine._build_context_with_diversity_and_tiers = Mock(
            return_value=(
                "Active participants need a terminated status and termination date.",
                [chunk],
                64,
                {
                    "dominant_mode": False,
                    "top_signal": 0.82,
                    "runner_up_signal": 0.0,
                    "ratio": 0.0,
                },
            )
        )

        async def fake_call_llm(*, task_type, **kwargs):
            if task_type == "gr_outcome":
                await asyncio.sleep(0.05)
                return LLMResponse(
                    content='{"outcome": "blocked_not_eligible"}',
                    usage={},
                    provider_used="openai",
                    model_used="gpt-5.4",
                )
            if task_type == "gr_response":
                return LLMResponse(
                    content=_json.dumps({
                        "outcome": "blocked_not_eligible",
                        "outcome_reason": (
                            "The participant is Active and does not have a "
                            "termination date."
                        ),
                        "response_to_participant": {
                            "opening": "You cannot start a separation distribution yet.",
                            "key_points": [
                                "Hardship or loan options may be worth reviewing.",
                            ],
                            "steps": [],
                            "warnings": [],
                        },
                        "questions_to_ask": [],
                        "escalation": {"needed": False, "reason": None},
                        "guardrails_applied": [],
                        "data_gaps": [],
                        "coverage_gaps": [],
                    }),
                    usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                    provider_used="openai",
                    model_used="gpt-5.4",
                )
            raise AssertionError(f"Unexpected task_type: {task_type}")

        mock_rag_engine._call_llm = AsyncMock(side_effect=fake_call_llm)

        result = await mock_rag_engine.generate_response(
            inquiry="Can I withdraw after quitting next month?",
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
            collected_data={
                "participant_data": {
                    "employment_status": "Active",
                    "termination_date": None,
                },
                "plan_data": {},
            },
            max_response_tokens=5000,
        )

        called_task_types = [
            call.kwargs["task_type"] for call in mock_rag_engine._call_llm.await_args_list
        ]
        assert "gr_response" in called_task_types
        assert result.response["outcome"] == "blocked_not_eligible"
        assert result.decision == "can_proceed"

    @pytest.mark.asyncio
    async def test_filter_excluded_articles_relaxes_when_all_excluded(self, mock_rag_engine):
        """If every retrieved chunk belongs to an excluded article, the engine must
        relax the exclusion list rather than returning out_of_scope with confidence 0.
        This converts a precision optimization into a graceful degradation."""
        from data_pipeline.llm_router import LLMResponse

        excluded_id = "missed_60_day_rollover_window"
        chunk = {
            "id": "chunk_1",
            "score": 0.78,
            "metadata": {
                "article_id": excluded_id,
                "article_title": "Missed 60-day Indirect Rollover Deadline",
                "topic": "rollover",
                "chunk_type": "business_rules",
                "chunk_tier": "high",
                "content": "60-day rollover rule applies to indirect rollovers.",
            },
        }

        mock_rag_engine._decompose_question = AsyncMock(
            return_value=["rollover guidance"]
        )
        mock_rag_engine._search_for_response_parallel_cascade = AsyncMock(
            return_value=([chunk], {"rollover guidance": 0.78})
        )
        mock_rag_engine._add_response_article_bundles = AsyncMock(
            return_value=([chunk], {"articles_added": []})
        )
        mock_rag_engine._build_context_with_diversity_and_tiers = Mock(
            return_value=(
                "60-day rollover rule applies to indirect rollovers.",
                [chunk],
                32,
                {"dominant_mode": False, "top_signal": 0.78, "runner_up_signal": 0.0, "ratio": 0.0},
            )
        )

        async def fake_call_llm(*, task_type, **kwargs):
            return LLMResponse(
                content=_json.dumps({
                    "outcome": "can_proceed",
                    "outcome_reason": "ok",
                    "response_to_participant": {
                        "opening": "ok", "key_points": [], "steps": [], "warnings": [],
                    },
                    "questions_to_ask": [],
                    "escalation": {"needed": False, "reason": None},
                    "guardrails_applied": [],
                    "data_gaps": [],
                    "coverage_gaps": [],
                }),
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                provider_used="openai",
                model_used="gpt-5.4",
            )

        mock_rag_engine._call_llm = AsyncMock(side_effect=fake_call_llm)

        result = await mock_rag_engine.generate_response(
            inquiry="I need rollover guidance",
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="rollover",
            collected_data={
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-02-01",
                    "account_balance": 25000,
                },
                "plan_data": {},
            },
            max_response_tokens=2000,
        )

        # The engine must NOT short-circuit with the legacy exact-procedure-filtering error.
        assert "exact-procedure filtering" not in (result.response.get("outcome_reason") or "")
        assert result.response["outcome"] != "blocked_missing_data"


class TestAdvisoryAlternatives:
    """Regression coverage for active participants asking for 401(k) funds."""

    def _jerry_data(self):
        return {
            "participant_data": {
                "employment_status": "Active",
                "termination_date": None,
                "first_name": "JERRY",
                "last_name": "FREED",
                "birth_date": "1968-05-11",
                "account_balance": 18176.94,
                "mfa_status": "Enrolled",
            },
            "plan_data": {
                "company_name": "Check Out My LLC",
                "company_status": "Ongoing",
            },
        }

    def _gr16_inquiry(self):
        return (
            "I need my 401(k) money as fast as possible. What are my "
            "fastest delivery options and what do they cost?"
        )

    def _gr16_data(self):
        return {
            "participant_data": {
                "employment_status": "Terminated",
                "termination_date": "2026-02-01",
                "total_vested_balance": "$12,000",
                "delivery_preference": "Fastest available",
                "address_type": "P.O. Box",
            },
            "plan_data": {"blackout_period": False},
        }

    def test_detects_active_cashout_housing_emergency_alternatives(self, mock_rag_engine):
        inquiry = (
            "The participant is considering quitting in the next 4 weeks due to a "
            "financial emergency involving their rented house being sold, and wants "
            "to know if they can withdraw money from their ForUsAll 401(k) plan due "
            "to separation from employment."
        )

        signal = mock_rag_engine._detect_advisory_concepts(
            inquiry=inquiry,
            topic="hardship_withdrawal",
            collected_data=self._jerry_data(),
        )

        assert signal["active_participant"] is True
        assert "termination_distribution_request" in signal["detected_concepts"]
        assert "in_service_withdrawal_options" in signal["alternative_concepts"]
        assert "hardship_withdrawal" in signal["alternative_concepts"]
        assert "loan" in signal["alternative_concepts"]

    def test_inactive_status_does_not_trigger_while_employed_alternatives(self, mock_rag_engine):
        collected_data = self._jerry_data()
        collected_data["participant_data"]["employment_status"] = "Inactive"
        inquiry = (
            "Inactive participant wants to withdraw money because of a hardship "
            "after termination."
        )

        signal = mock_rag_engine._detect_advisory_concepts(
            inquiry=inquiry,
            topic="hardship_withdrawal",
            collected_data=collected_data,
        )

        assert signal["active_participant"] is False
        assert "in_service_withdrawal_options" not in signal["alternative_concepts"]
        assert "loan" not in signal["alternative_concepts"]

    def test_terminated_rollover_builds_exact_procedure_profile(self, mock_rag_engine):
        inquiry = (
            "Michael Ditton left his employer and wants to rollover his ForUsAll "
            "401(k) account balance out to a new manager. He is requesting "
            "instructions for this process."
        )

        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=inquiry,
            topic="rollover",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data={
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-03-27",
                    "account_balance": 85788.66,
                    "mfa_status": "Enrolled",
                },
                "plan_data": {"company_status": "Ongoing"},
            },
        )

        assert profile["mode"] == "exact_procedure"
        assert profile["employment_state"] == "terminated"
        assert profile["primary_action"] == "termination_rollover"
        assert profile["rollover_mode"] == "single_destination"
        assert profile["primary_article_id"] == (
            "lt_request_401k_termination_withdrawal_or_rollover"
        )
        assert "can_i_split_my_401_k_rollover_between_multiple_providers" in profile[
            "excluded_articles"
        ]
        assert "missed_60_day_rollover_window" in profile["excluded_articles"]
        assert "401k_force_out_process_involuntary_distribution_balance_thresholds_safe_harbor_ira_rollovers_fee_outs_compliance" in profile["excluded_articles"]

    def test_single_new_manager_does_not_trigger_split_rollover_profile(self, mock_rag_engine):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry="I left my employer and want to roll over my 401(k) to a new provider.",
            topic="rollover",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data={
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-03-27",
                    "account_balance": 25000,
                },
                "plan_data": {},
            },
        )

        assert profile["rollover_mode"] == "single_destination"
        assert profile["signals"]["split_rollover"] is False
        assert "can_i_split_my_401_k_rollover_between_multiple_providers" in profile[
            "excluded_articles"
        ]

    def test_direct_rollover_profile_excludes_missed_60_day_article(self, mock_rag_engine):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry="I need instructions for a direct rollover to Fidelity.",
            topic="rollover",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data={
                "participant_data": {
                    "employment_status": "Terminated",
                    "termination_date": "2026-02-01",
                    "account_balance": 45000,
                },
                "plan_data": {},
            },
        )

        assert profile["signals"]["indirect_rollover_60_day"] is False
        assert "missed_60_day_rollover_window" in profile["excluded_articles"]

    def test_left_his_company_triggers_exact_procedure_termination_rollover(
        self,
        mock_rag_engine,
    ):
        """TKT-879503 regression: 'left his company' must trigger termination_distribution
        signal even when participant_data.employment_status is still 'Active' upstream."""
        inquiry = (
            "The participant, Thomas Wu, has recently left his company that uses "
            "ForUsAll and wants to transfer his ForUsAll 401(k) assets out of his "
            "ForUsAll 401(k) plan to another IRA plan. He is seeking guidance on "
            "the rollover process to consolidate his retirement accounts."
        )

        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=inquiry,
            topic="rollover",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data={
                "participant_data": {
                    "employment_status": "Active",
                    "termination_date": None,
                    "account_balance": 32536.47,
                    "mfa_status": "Enrolled",
                },
                "plan_data": {"company_status": "Ongoing"},
            },
        )

        assert profile["signals"]["termination_distribution"] is True
        assert profile["mode"] == "exact_procedure"
        assert profile["primary_action"] == "termination_rollover"
        assert profile["primary_article_id"] == (
            "lt_request_401k_termination_withdrawal_or_rollover"
        )

    @pytest.mark.parametrize(
        "phrase",
        [
            "left his company",
            "left her company",
            "left their company",
            "left the company",
            "left my company",
            "no longer employed",
            "former employer",
            "recently left",
        ],
    )
    def test_company_phrase_variants_trigger_termination_signal(
        self,
        mock_rag_engine,
        phrase,
    ):
        signals = mock_rag_engine._infer_retrieval_signals(
            inquiry=f"The participant {phrase} and wants to roll over their 401(k).",
            topic="rollover",
            collected_data={
                "participant_data": {
                    "employment_status": "Active",
                    "termination_date": None,
                },
                "plan_data": {},
            },
        )
        assert signals["termination_distribution"] is True, (
            f"Expected '{phrase}' to trigger termination_distribution"
        )

    def test_rollover_topic_filter_includes_termination_articles(
        self,
        mock_rag_engine,
    ):
        """The rollover topic must map to all rollover-relevant article topics in the KB,
        not only 'rollover' (which only matches the 60-day deadline article)."""
        resolved = mock_rag_engine._resolve_topic_filter("rollover")
        assert resolved is not None
        assert "rollover" in resolved
        assert "termination_distribution_request" in resolved
        assert "distribution" in resolved
        # The rollover set is mixed (not all global-only), so RK lanes must NOT
        # be skipped for it.
        assert not set(resolved).issubset(mock_rag_engine.GLOBAL_ONLY_TOPICS)

    def test_advisory_separation_picks_up_company_phrase(self, mock_rag_engine):
        signal = mock_rag_engine._detect_advisory_concepts(
            inquiry=(
                "Participant left his company and wants to access funds from "
                "their 401(k)."
            ),
            topic="rollover",
            collected_data={
                "participant_data": {"employment_status": "Active"},
                "plan_data": {},
            },
        )
        assert signal["separation_signal"] is True

    def test_terminated_distribution_delivery_builds_exact_procedure_profile(
        self,
        mock_rag_engine,
    ):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=self._gr16_inquiry(),
            topic="distributions",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self._gr16_data(),
        )

        assert profile["mode"] == "exact_procedure"
        assert profile["primary_action"] == "termination_distribution"
        assert profile["primary_article_id"] == (
            "lt_request_401k_termination_withdrawal_or_rollover"
        )
        assert profile["signals"]["delivery_or_fee_request"] is True

    def test_delivery_cost_question_is_informational_options(self, mock_rag_engine):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=self._gr16_inquiry(),
            topic="distributions",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self._gr16_data(),
        )

        assert profile["inquiry_intent"] == "informational_options"

    def test_informational_delivery_question_does_not_block_on_identity_fields(
        self,
        mock_rag_engine,
    ):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=self._gr16_inquiry(),
            topic="distributions",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self._gr16_data(),
        )
        llm_response = {
            "outcome": "blocked_missing_data",
            "outcome_reason": (
                "Eligibility cannot be fully confirmed because participant "
                "name, email, company name, wire instructions, and a physical "
                "street address were not provided."
            ),
            "response_to_participant": {
                "opening": "We can outline the fastest delivery options.",
                "key_points": [
                    "Wire and overnight check are the fastest listed options.",
                ],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [
                {"question": "What is your full name?", "why": "Lookup"},
                {"question": "What is your email?", "why": "Lookup"},
                {"question": "What is your company name?", "why": "Lookup"},
                {"question": "What are your wire instructions?", "why": "Wire"},
                {
                    "question": "What physical street address should be used?",
                    "why": "Overnight checks cannot go to a P.O. Box",
                },
            ],
            "data_gaps": [
                "Participant name",
                "Email address",
                "Company name",
                "Wire instructions if wire delivery is selected",
                "Physical street address if overnight check delivery is selected",
            ],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [],
            "coverage_gaps": [],
        }

        normalized, policy_info = mock_rag_engine._apply_informational_outcome_policy(
            parsed=llm_response,
            retrieval_profile=profile,
            collected_data=self._gr16_data(),
        )

        assert normalized["outcome"] == "can_proceed"
        assert policy_info["normalized"] is True
        assert policy_info["core_eligibility_supported"] is True
        assert policy_info["missing_data_classes"]["core_eligibility_missing"] == []

    def test_execution_details_missing_are_not_core_eligibility_missing(
        self,
        mock_rag_engine,
    ):
        classes = mock_rag_engine._classify_missing_data(
            [
                "wire routing number and account number",
                "physical street address for overnight check",
                "delivery method final choice",
                "distribution type",
                "participant full name",
                "email address",
                "company name",
            ]
        )

        assert classes["core_eligibility_missing"] == []
        assert set(classes["execution_details_missing"]) == {
            "wire routing number and account number",
            "physical street address for overnight check",
            "delivery method final choice",
            "distribution type",
        }
        assert set(classes["identity_lookup_missing"]) == {
            "participant full name",
            "email address",
            "company name",
        }

    def test_expands_queries_for_while_employed_hardship_and_loan(self, mock_rag_engine):
        inquiry = (
            "Active participant wants to withdraw money because of a financial "
            "emergency and may quit next month."
        )
        signal = mock_rag_engine._detect_advisory_concepts(
            inquiry=inquiry,
            topic="termination_distribution_request",
            collected_data=self._jerry_data(),
        )

        expanded = mock_rag_engine._expand_queries_with_advisory_concepts(
            sub_queries=[
                "401k distribution eligibility after separation from employment",
                "401k hardship withdrawal housing emergency rules",
                "ForUsAll post-termination distribution request process",
            ],
            inquiry=inquiry,
            topic="termination_distribution_request",
            advisory_signal=signal,
        )

        assert any("while employed 401k access options" in q for q in expanded)
        assert any("hardship withdrawal eviction foreclosure" in q for q in expanded)
        assert any("401k loan eligibility active employee" in q for q in expanded)
        assert len(expanded) <= 6

    def test_context_builder_reserves_chunks_for_required_advisory_concepts(
        self,
        mock_rag_engine,
    ):
        def chunk(chunk_id, article_id, topic, score, chunk_type="eligibility"):
            return {
                "id": chunk_id,
                "score": score,
                "metadata": {
                    "article_id": article_id,
                    "article_title": article_id.replace("_", " ").title(),
                    "topic": topic,
                    "chunk_type": chunk_type,
                    "chunk_tier": "critical",
                    "content": f"{chunk_id} compact context.",
                },
            }

        noise_chunks = [
            chunk(f"noise_{idx}", f"noise_article_{idx}", "distribution", 0.95 - idx * 0.01)
            for idx in range(12)
        ]
        advisory_chunks = [
            chunk(
                "termination_required",
                "termination_article",
                "termination_distribution_request",
                0.62,
            ),
            chunk(
                "in_service_required",
                "in_service_article",
                "in_service_withdrawal_options",
                0.61,
            ),
            chunk(
                "hardship_required",
                "hardship_article",
                "hardship_withdrawal",
                0.60,
                "decision_guide",
            ),
            chunk("loan_required", "loan_article", "loan", 0.59, "decision_guide"),
        ]
        advisory_signal = {
            "detected_concepts": [
                "termination_distribution_request",
                "in_service_withdrawal_options",
            ],
            "alternative_concepts": ["hardship_withdrawal", "loan"],
        }

        _context, selected, _tokens, dominance_info = (
            mock_rag_engine._build_context_with_diversity_and_tiers(
                chunks=noise_chunks + advisory_chunks,
                budget=40,
                max_per_article=3,
                advisory_signal=advisory_signal,
            )
        )

        selected_ids = {chunk["id"] for chunk in selected}
        assert {"termination_required", "in_service_required"}.issubset(selected_ids)
        assert {"hardship_required", "loan_required"}.issubset(selected_ids)
        assert dominance_info["concept_quotas_applied"] == [
            "termination_distribution_request",
            "in_service_withdrawal_options",
            "hardship_withdrawal",
            "loan",
        ]

    def test_exact_procedure_context_prioritizes_primary_article_and_drops_references(
        self,
        mock_rag_engine,
    ):
        def chunk(chunk_id, article_id, topic, chunk_type, score=0.9):
            return {
                "id": chunk_id,
                "score": score,
                "metadata": {
                    "article_id": article_id,
                    "article_title": article_id.replace("_", " ").title(),
                    "topic": topic,
                    "chunk_type": chunk_type,
                    "chunk_tier": "critical",
                    "content": f"{chunk_id} exact procedure context.",
                },
            }

        primary_article = "lt_request_401k_termination_withdrawal_or_rollover"
        chunks = [
            chunk("primary_decision", primary_article, "termination_distribution_request", "decision_guide"),
            chunk("primary_steps", primary_article, "termination_distribution_request", "steps"),
            chunk("primary_rules", primary_article, "termination_distribution_request", "business_rules"),
            chunk("primary_refs", primary_article, "termination_distribution_request", "references"),
            chunk("split_steps", "can_i_split_my_401_k_rollover_between_multiple_providers", "distribution", "steps", 0.95),
            chunk("rmd_refs", "401k_required_minimum_distributions_rmds_rules_deadlines_penalties_exceptions_and_roth_conversion_impact", "distribution", "references", 0.94),
        ]
        profile = {
            "mode": "exact_procedure",
            "primary_article_id": primary_article,
            "excluded_articles": [
                "can_i_split_my_401_k_rollover_between_multiple_providers",
                "401k_required_minimum_distributions_rmds_rules_deadlines_penalties_exceptions_and_roth_conversion_impact",
            ],
            "exclusion_reasons": {},
            "signals": {},
        }

        _context, selected, _tokens, dominance_info = (
            mock_rag_engine._build_context_with_diversity_and_tiers(
                chunks=chunks,
                budget=200,
                max_per_article=6,
                advisory_signal={"detected_concepts": ["termination_distribution_request"]},
                retrieval_profile=profile,
            )
        )

        selected_ids = {chunk["id"] for chunk in selected}
        assert {"primary_decision", "primary_steps", "primary_rules"}.issubset(
            selected_ids
        )
        assert "primary_refs" not in selected_ids
        assert "split_steps" not in selected_ids
        assert "rmd_refs" not in selected_ids
        assert dominance_info["dominant_mode"] is True
        assert dominance_info["top_article_id"] == primary_article

    def test_exact_procedure_context_for_delivery_costs_excludes_tangential_articles(
        self,
        mock_rag_engine,
    ):
        def chunk(chunk_id, article_id, chunk_type, score=0.9):
            return {
                "id": chunk_id,
                "score": score,
                "metadata": {
                    "article_id": article_id,
                    "article_title": article_id.replace("_", " ").title(),
                    "topic": "termination_distribution_request",
                    "chunk_type": chunk_type,
                    "chunk_tier": "critical",
                    "content": f"{chunk_id} delivery/cost context.",
                },
            }

        primary_article = "lt_request_401k_termination_withdrawal_or_rollover"
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=self._gr16_inquiry(),
            topic="distributions",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self._gr16_data(),
        )
        chunks = [
            chunk("primary_decision", primary_article, "decision_guide"),
            chunk("primary_eligibility", primary_article, "eligibility"),
            chunk("primary_rules", primary_article, "business_rules"),
            chunk("primary_steps", primary_article, "steps"),
            chunk("primary_guardrails", primary_article, "guardrails"),
            chunk("primary_fees", primary_article, "fees_details"),
            chunk("primary_missing", primary_article, "required_data_if_missing"),
            chunk("primary_frames", primary_article, "response_frames"),
            chunk(
                "split_rollover",
                "can_i_split_my_401_k_rollover_between_multiple_providers",
                "business_rules",
                0.99,
            ),
            chunk(
                "loan_rules",
                "lt_401k_loan_complete_guide_submission_repayment_support",
                "business_rules",
                0.98,
            ),
            chunk(
                "general_options",
                "401k_savings_after_leaving_your_job_rollovers_cash_outs_roth_pre_tax",
                "business_rules",
                0.97,
            ),
        ]

        _context, selected, _tokens, _dominance_info = (
            mock_rag_engine._build_context_with_diversity_and_tiers(
                chunks=chunks,
                budget=400,
                max_per_article=8,
                advisory_signal={"detected_concepts": ["termination_distribution_request"]},
                retrieval_profile=profile,
            )
        )

        selected_ids = {chunk["id"] for chunk in selected}
        assert {
            "primary_decision",
            "primary_eligibility",
            "primary_rules",
            "primary_steps",
            "primary_guardrails",
            "primary_fees",
        }.issubset(selected_ids)
        assert "primary_missing" not in selected_ids
        assert "primary_frames" not in selected_ids
        assert "split_rollover" not in selected_ids
        assert "loan_rules" not in selected_ids
        assert "general_options" not in selected_ids

    @pytest.mark.asyncio
    async def test_context_bundles_add_high_value_chunks_for_weak_alternative_hit(
        self,
        mock_rag_engine,
    ):
        weak_reference_hit = {
            "id": "hardship_ref",
            "score": 0.26,
            "metadata": {
                "article_id": "forusall_401k_hardship_withdrawal_complete_guide",
                "article_title": "ForUsAll 401(k) Hardship Withdrawal",
                "topic": "hardship_withdrawal",
                "tags": ["Hardship Request"],
                "subtopics": ["irs_approved_reasons"],
                "chunk_type": "references",
                "chunk_tier": "low",
                "content": "# References\nContact support.",
            },
        }
        high_value_bundle = [
            {
                "id": "hardship_decision",
                "score": 1.0,
                "metadata": {
                    "article_id": "forusall_401k_hardship_withdrawal_complete_guide",
                    "article_title": "ForUsAll 401(k) Hardship Withdrawal",
                    "topic": "hardship_withdrawal",
                    "tags": ["Hardship Request"],
                    "subtopics": ["irs_approved_reasons"],
                    "chunk_type": "decision_guide",
                    "chunk_tier": "critical",
                    "content": "# Decision Guide\nPreventing eviction or foreclosure may qualify.",
                },
            },
            {
                "id": "hardship_rules",
                "score": 1.0,
                "metadata": {
                    "article_id": "forusall_401k_hardship_withdrawal_complete_guide",
                    "article_title": "ForUsAll 401(k) Hardship Withdrawal",
                    "topic": "hardship_withdrawal",
                    "tags": ["Hardship Request"],
                    "subtopics": ["irs_approved_reasons"],
                    "chunk_type": "business_rules",
                    "chunk_tier": "critical",
                    "content": "# Business Rules\nPlan rules and documentation apply.",
                },
            },
        ]

        def fake_fetch(prefix=None, limit=100, tier=None, chunk_type=None):
            assert prefix == "forusall_401k_hardship_withdrawal_complete_guide"
            return high_value_bundle

        mock_rag_engine.pinecone.list_and_fetch_chunks = fake_fetch
        signal = {
            "detected_concepts": ["termination_distribution_request"],
            "alternative_concepts": ["hardship_withdrawal", "loan"],
        }

        chunks, bundle_info = await mock_rag_engine._add_response_article_bundles(
            chunks=[weak_reference_hit],
            advisory_signal=signal,
        )

        chunk_ids = {chunk["id"] for chunk in chunks}
        assert "hardship_decision" in chunk_ids
        assert "hardship_rules" in chunk_ids
        assert bundle_info["articles_added"] == [
            "forusall_401k_hardship_withdrawal_complete_guide"
        ]

    @pytest.mark.asyncio
    async def test_context_bundles_do_not_cap_out_hardship_and_loan_alternatives(
        self,
        mock_rag_engine,
    ):
        chunks = []
        for aid, topic, score in [
            ("termination_a", "termination_distribution_request", 0.70),
            ("termination_b", "termination_distribution_request", 0.69),
            ("termination_c", "termination_distribution_request", 0.68),
            ("in_service_a", "in_service_withdrawal_options", 0.67),
            ("in_service_b", "in_service_withdrawal_options", 0.66),
            ("in_service_c", "in_service_withdrawal_options", 0.65),
            ("hardship_article", "hardship_withdrawal", 0.50),
            ("loan_article", "loan", 0.49),
        ]:
            chunks.append({
                "id": f"{aid}_ref",
                "score": score,
                "metadata": {
                    "article_id": aid,
                    "article_title": aid.replace("_", " ").title(),
                    "topic": topic,
                    "chunk_type": "references",
                    "chunk_tier": "low",
                    "content": "Reference chunk.",
                },
            })

        def fake_fetch(prefix=None, limit=100, tier=None, chunk_type=None):
            return [{
                "id": f"{prefix}_bundle",
                "score": 1.0,
                "metadata": {
                    "article_id": prefix,
                    "article_title": prefix.replace("_", " ").title(),
                    "topic": "loan" if prefix == "loan_article" else "hardship_withdrawal",
                    "chunk_type": "decision_guide",
                    "chunk_tier": "critical",
                    "content": "High value decision guidance.",
                },
            }]

        mock_rag_engine.pinecone.list_and_fetch_chunks = fake_fetch
        signal = {
            "detected_concepts": [
                "termination_distribution_request",
                "in_service_withdrawal_options",
            ],
            "alternative_concepts": ["hardship_withdrawal", "loan"],
        }

        enriched, bundle_info = await mock_rag_engine._add_response_article_bundles(
            chunks=chunks,
            advisory_signal=signal,
        )

        chunk_ids = {chunk["id"] for chunk in enriched}
        assert "hardship_article_bundle" in chunk_ids
        assert "loan_article_bundle" in chunk_ids
        assert "hardship_article" in bundle_info["articles_added"]
        assert "loan_article" in bundle_info["articles_added"]

    def test_prompt_contract_preserves_main_outcome_and_requires_alternative_caveats(self):
        from data_pipeline.prompts import build_generate_response_prompt

        system_prompt, _user_prompt = build_generate_response_prompt(
            context="Context includes termination distribution, hardship, and loan articles.",
            inquiry="Can I withdraw money after quitting because of a housing emergency?",
            collected_data=self._jerry_data(),
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
            max_tokens=3000,
        )

        assert "outcome describes the participant's primary requested action" in system_prompt
        assert "blocked_not_eligible" in system_prompt
        assert "alternatives" in system_prompt.lower()
        assert "Do not say a rented-house sale automatically qualifies" in system_prompt
        assert "explicitly mention eviction or foreclosure" in system_prompt

    def test_prompt_contract_allows_can_proceed_for_options_and_costs(self):
        from data_pipeline.prompts import build_generate_response_prompt

        system_prompt, _user_prompt = build_generate_response_prompt(
            context="Context includes fees, delivery methods, and timelines.",
            inquiry=self._gr16_inquiry(),
            collected_data=self._gr16_data(),
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="distributions",
            max_tokens=3000,
        )

        assert "Missing execution or identity details" in system_prompt
        assert "options, instructions, costs, fees, or delivery timelines" in system_prompt
        assert "do not force blocked_missing_data" in system_prompt
        assert "questions_to_ask" in system_prompt
        assert "without a participant name" in system_prompt


# ── Global-only topic skip (skip RK-specific lanes/levels) ──

RAG_LOGGER = "data_pipeline.rag_engine"


def _go_chunk(idx, *, article_id="hardship_guide", topic="hardship_withdrawal",
              scope="global", chunk_type="required_data_must_have", score=0.7,
              record_keeper=None):
    """Build a chunk shaped like a Pinecone hit for the global-only tests."""
    meta = {
        "article_id": article_id,
        "article_title": "ForUsAll 401(k) Hardship Withdrawal — Complete Guide",
        "topic": topic,
        "scope": scope,
        "chunk_type": chunk_type,
        "content": "Hardship withdrawal eligibility, fees, steps and timelines.",
    }
    if record_keeper is not None:
        meta["record_keeper"] = record_keeper
    return {"id": f"go_chunk_{idx}", "score": score, "metadata": meta}


def _capturing_cached_query(calls, *, chunks_per_call=4, score=0.7,
                            chunk_type="decision_guide", empty=False,
                            provenance=False, topic="hardship_withdrawal"):
    """Async spy for ``_cached_query`` that records every ``filter_dict``.

    Returns unique-id chunks per call so the merged set clears
    ``GR_FALLBACK_MIN_CHUNKS`` (the H fallback does not fire) and each chunk
    scores above the fallback / cascade-sufficiency thresholds. ``empty=True``
    forces 0 chunks (to exercise the must_have=[] / broad-fallback path).

    ``provenance=True`` makes the returned ``article_id`` depend on the filter:
    a record_keeper-filtered lane (A/C) yields ``"rk_specific_noise"`` while the
    record_keeper-free lanes (E/G/H) yield the global ``"hardship_guide"``. This
    lets a test prove the *correct* article arrives via E/G and that no
    RK-specific article leaks in (which would only happen if A/C had run).
    """
    state = {"i": 0}

    async def spy(query_text, top_k=None, filter_dict=None, rerank=None):
        calls.append(filter_dict)
        if empty:
            return []
        base = state["i"]
        state["i"] += chunks_per_call
        article_id = "hardship_guide"
        if provenance and filter_dict and "record_keeper" in filter_dict:
            article_id = "rk_specific_noise"
        return [
            _go_chunk(base + j, article_id=article_id, topic=topic,
                      chunk_type=chunk_type, score=score)
            for j in range(chunks_per_call)
        ]

    return spy


class TestGlobalOnlyTopicSkip:
    """The lane/level-skip optimization for structurally global-only topics."""

    # ---- generate path: _search_for_response_parallel_cascade ----

    @pytest.mark.asyncio
    async def test_global_only_topic_skips_rk_lanes_in_generate_response(
        self, mock_rag_engine, caplog
    ):
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls)
        )

        chunks, _scores = await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["hardship withdrawal eligibility"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        # No lane filtered by record_keeper (A and C were skipped).
        assert all("record_keeper" not in (f or {}) for f in calls)
        # Only E (scope=global) and G (semantic, filter=None) ran.
        assert any(f is None for f in calls), "G:semantic lane (filter=None) missing"
        assert any(
            f and f.get("scope") == {"$eq": "global"} for f in calls
        ), "E:global_broad lane missing"
        assert "Global-only topic detected" in caplog.text
        assert chunks  # the global lanes still return content

    @pytest.mark.asyncio
    async def test_global_only_topic_hardship_guide_surfaces_in_generate_response(
        self, mock_rag_engine
    ):
        calls = []
        # provenance spy: E/G return the global guide; A/C (if they ran) would
        # return "rk_specific_noise". This gives the assertion real signal.
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls, provenance=True)
        )

        chunks, _scores = await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["hardship withdrawal eligibility"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        article_ids = {c["metadata"].get("article_id") for c in chunks}
        # The correct global article must still reach the caller via E/G...
        assert "hardship_guide" in article_ids
        # ...and NO RK-specific article leaks in (it would only appear if the
        # skipped A/C lanes had run).
        assert "rk_specific_noise" not in article_ids

    @pytest.mark.asyncio
    async def test_caller_alias_hardship_triggers_skip(self, mock_rag_engine, caplog):
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls)
        )

        await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["hardship withdrawal"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship",  # alias -> ["hardship_withdrawal"]
        )

        assert all("record_keeper" not in (f or {}) for f in calls)
        assert "Global-only topic detected" in caplog.text

    @pytest.mark.asyncio
    async def test_mixed_alias_rollover_does_not_skip_rk_lanes(
        self, mock_rag_engine, caplog
    ):
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls)
        )

        await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["rollover guidance"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="rollover",  # resolves to a mixed (non global-only) set
        )

        # RK-filtered lanes A and C must still run.
        assert any(
            f and f.get("record_keeper") == {"$eq": "LT Trust"} for f in calls
        )
        assert "Global-only topic detected" not in caplog.text

    @pytest.mark.asyncio
    async def test_no_record_keeper_is_noop(self, mock_rag_engine, caplog):
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls)
        )

        await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["hardship withdrawal"],
            record_keeper=None,
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        # A and C are off because has_rk is False, not because of the skip.
        assert all("record_keeper" not in (f or {}) for f in calls)
        # The skip log must NOT fire when there is no record_keeper.
        assert "Global-only topic detected" not in caplog.text

    @pytest.mark.asyncio
    async def test_global_only_h_fallback_pins_scope_global(
        self, mock_rag_engine, caplog
    ):
        """If the H fallback fires under skip_rk_lanes, it must stay pinned to
        scope=global so it cannot reintroduce RK-specific articles. Forcing the
        fallback (low score) and asserting the pin makes a revert of the pin
        fail loudly."""
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        # score below GR_FALLBACK_MIN_SCORE (0.35) forces the H fallback.
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls, score=0.10)
        )

        await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["hardship withdrawal eligibility"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        # E and G run in the primary gather; H runs afterwards -> 3 calls total.
        assert len(calls) == 3, f"expected E, G, H; got {calls}"
        assert "GR fallback triggered" in caplog.text
        # The H fallback (the last call) is scope-pinned to global.
        assert calls[-1] == {
            "plan_type": {"$in": ["401(k)", "all"]},
            "scope": {"$eq": "global"},
        }
        assert all("record_keeper" not in (f or {}) for f in calls)

    # ---- required_data path: _search_for_required_data ----

    @pytest.mark.asyncio
    async def test_global_only_topic_skips_rk_levels_in_required_data(
        self, mock_rag_engine, caplog
    ):
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls, chunk_type="required_data_must_have")
        )

        _chunks, per_query_scores = await mock_rag_engine._search_for_required_data(
            enriched_queries=["hardship withdrawal required data"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        # (a) No record_keeper-filtered level ran.
        assert all("record_keeper" not in (f or {}) for f in calls)
        # (b) per_query_scores was mutated (catches the dropped-mutation hazard).
        assert any(v > 0 for v in per_query_scores.values())
        # (c) the skip log fired.
        assert "skipping RK-specific levels in required_data" in caplog.text

    @pytest.mark.asyncio
    async def test_global_only_required_data_skips_broad_fallback(
        self, mock_rag_engine
    ):
        calls = []
        # must_have=[] simulation: the topic-scoped lane returns 0 chunks.
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls, empty=True)
        )

        chunks, _scores = await mock_rag_engine._search_for_required_data(
            enriched_queries=["hardship withdrawal required data"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        assert chunks == []
        # No RK-filtered call anywhere (broad-fallback would re-run RK levels).
        assert all("record_keeper" not in (f or {}) for f in calls)
        # Every issued cascade filter is topic-scoped (broad-fallback drops the
        # topic key); its absence proves the broad re-run did not happen.
        assert all("topic" in (f or {}) for f in calls)
        # Exactly the two record_keeper-free levels (scope=global, then any),
        # no Phase-2 context query, no broad fallback.
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_global_only_in_service_skips_rk_levels_in_required_data(
        self, mock_rag_engine, caplog
    ):
        """in_service_withdrawal_options (must_have=[3], lane fills) takes the
        same skip path as hardship — confirms the predicate covers all three
        global-only topics, not just hardship."""
        caplog.set_level(logging.INFO, logger=RAG_LOGGER)
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(
                calls,
                chunk_type="required_data_must_have",
                topic="in_service_withdrawal_options",
            )
        )

        _chunks, per_query_scores = await mock_rag_engine._search_for_required_data(
            enriched_queries=["in service withdrawal options required data"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="in_service_withdrawal_options",
        )

        assert all("record_keeper" not in (f or {}) for f in calls)
        assert any(v > 0 for v in per_query_scores.values())
        assert "skipping RK-specific levels in required_data" in caplog.text

    @pytest.mark.asyncio
    async def test_global_only_excess_required_data_skips_broad_fallback(
        self, mock_rag_engine
    ):
        """excess_contribution_refund (must_have=[], empty lane) mirrors the
        hardship empty-lane case: no RK level, no broad fallback."""
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls, empty=True)
        )

        chunks, _scores = await mock_rag_engine._search_for_required_data(
            enriched_queries=["excess contribution refund required data"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="excess_contribution_refund",
        )

        assert chunks == []
        assert all("record_keeper" not in (f or {}) for f in calls)
        assert all("topic" in (f or {}) for f in calls)
        assert len(calls) == 2

    # ---- predicate / drift / flag ----

    def test_global_only_topics_are_canonical(self, mock_rag_engine):
        from data_pipeline.rag_engine import TOPIC_NORMALIZATION_MAP

        # TOPIC_NORMALIZATION_MAP values are List[str]; flatten before union.
        canonical = set().union(*TOPIC_NORMALIZATION_MAP.values())
        assert mock_rag_engine.GLOBAL_ONLY_TOPICS.issubset(canonical)

    @pytest.mark.asyncio
    async def test_feature_flag_disables_skip(self, mock_rag_engine):
        # Disable the kill-switch on the instance (shadows the class attr).
        mock_rag_engine.GLOBAL_ONLY_SKIP_ENABLED = False
        calls = []
        mock_rag_engine._cached_query = AsyncMock(
            side_effect=_capturing_cached_query(calls)
        )

        await mock_rag_engine._search_for_response_parallel_cascade(
            enriched_queries=["hardship withdrawal"],
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="hardship_withdrawal",
        )

        # With the flag off, hardship behaves like any RK topic: A and C run.
        assert any(
            f and f.get("record_keeper") == {"$eq": "LT Trust"} for f in calls
        )

    def test_is_global_only_topic_predicate(self, mock_rag_engine):
        assert mock_rag_engine._is_global_only_topic(["hardship_withdrawal"])
        assert mock_rag_engine._is_global_only_topic(
            ["hardship_withdrawal", "excess_contribution_refund"]
        )
        # Mixed set -> not a subset.
        assert not mock_rag_engine._is_global_only_topic(
            ["hardship_withdrawal", "rollover"]
        )
        # Empty / None guards.
        assert not mock_rag_engine._is_global_only_topic([])
        assert not mock_rag_engine._is_global_only_topic(None)


def test_global_only_topics_have_no_rk_specific_articles():
    """Static drift guard (primary): the KB JSON on disk is the source of
    truth. If an RK-specific article is ever added for a global-only topic,
    the skip would make it permanently unreachable — fail loudly here."""
    import glob
    import json
    from pathlib import Path

    from data_pipeline.rag_engine import RAGEngine

    repo_root = Path(__file__).resolve().parents[2]
    pa_dir = repo_root / "PA"
    paths = glob.glob(str(pa_dir / "**" / "*.json"), recursive=True)
    assert paths, f"No KB JSON files found under {pa_dir}"

    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("metadata", {}) or {}
        if meta.get("topic") in RAGEngine.GLOBAL_ONLY_TOPICS:
            rk = meta.get("record_keeper")
            assert rk in (None, "all"), (
                f"{path}: global-only topic with record_keeper={rk!r}. "
                f"Remove the topic from GLOBAL_ONLY_TOPICS or change the article."
            )
            assert meta.get("scope") == "global", (
                f"{path}: global-only topic with scope={meta.get('scope')!r} "
                f"(expected 'global')."
            )


class TestEvalFixesF1F2F7:
    """Regression tests for eval 2026-06-22 fixes F2 (hardship signal scope),
    F1 (incoming-rollover exact mode), F7 (explicit separation overrides active)."""

    def _active(self):
        return {
            "participant_data": {"employment_status": "Active", "account_balance": 40000},
            "plan_data": {"company_status": "Ongoing"},
        }

    # ---- F2: hardship signal must not fire on generic words ----
    def test_f2_incoming_rollover_house_does_not_trigger_hardship(self, mock_rag_engine):
        signal = mock_rag_engine._detect_advisory_concepts(
            inquiry=("Participant wants to roll over her old 401(k) from a previous "
                     "provider into her current plan; she also mentioned she just bought a house."),
            topic="rollover",
            collected_data=self._active(),
        )
        assert signal["hardship_signal"] is False
        assert "hardship_withdrawal" not in signal["detected_concepts"]

    def test_f2_real_hardship_still_detected(self, mock_rag_engine):
        signal = mock_rag_engine._detect_advisory_concepts(
            inquiry="Participant needs a hardship withdrawal to avoid eviction from their home.",
            topic="hardship_withdrawal",
            collected_data=self._active(),
        )
        assert signal["hardship_signal"] is True

    def test_f2_house_with_foreclosure_context_triggers_but_alone_does_not(self, mock_rag_engine):
        with_ctx = mock_rag_engine._detect_advisory_concepts(
            inquiry="My house is in foreclosure and I need funds.", topic="general",
            collected_data=self._active())
        without_ctx = mock_rag_engine._detect_advisory_concepts(
            inquiry="I want to move my 401(k) money to buy a house for my kids.", topic="general",
            collected_data=self._active())
        assert with_ctx["hardship_signal"] is True
        assert without_ctx["hardship_signal"] is False

    # ---- F7: explicit separation claim detection + active override ----
    def test_f7_explicit_separation_claim_detection(self, mock_rag_engine):
        for text in ["I resigned last month and want to cash out",
                     "the participant no longer works at the company",
                     "I am a former employee and want my 401(k)",
                     "they let me go and I want to withdraw"]:
            sig = mock_rag_engine._detect_advisory_concepts(text, "general", None)
            assert sig["explicit_separation_claim"] is True, text

    def test_f7_future_or_rollover_source_is_not_a_separation_claim(self, mock_rag_engine):
        for text in ["I'm thinking about quitting next year",
                     "I want to roll over my previous employer's 401(k) into this plan"]:
            sig = mock_rag_engine._detect_advisory_concepts(text, "rollover", None)
            assert sig["explicit_separation_claim"] is False, text

    def test_f7_active_status_with_separation_claim_overrides(self, mock_rag_engine):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry="The participant says they no longer work at the company and wants to cash out their 401(k).",
            topic="termination_distribution_request",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data={"participant_data": {"employment_status": "Active"}, "plan_data": {}},
        )
        assert profile["separation_status_conflict"] is True
        assert profile["signals"]["explicit_separation_claim"] is True
        assert profile["signals"]["termination_distribution"] is True
        excluded = profile["excluded_articles"]
        assert mock_rag_engine.IN_SERVICE_ARTICLE_ID in excluded
        assert mock_rag_engine.HARDSHIP_ARTICLE_ID in excluded
        assert mock_rag_engine.LT_LOAN_ARTICLE_ID in excluded

    # ---- F1: incoming rollover exact-procedure mode ----
    def test_f1_incoming_rollover_builds_exact_procedure_profile(self, mock_rag_engine):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry=("Participant wants to roll over her previous 401(k) from CalSavers "
                     "into her current ForUsAll account and is requesting instructions."),
            topic="rollover",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self._active(),
        )
        assert profile["mode"] == "exact_procedure"
        assert profile["primary_action"] == "incoming_rollover"
        assert profile["rollover_mode"] == "incoming"
        assert profile["primary_article_id"] == "lt_request_401k_termination_withdrawal_or_rollover"
        assert profile["signals"]["incoming_rollover"] is True
        assert profile["signals"]["termination_distribution"] is False
        assert mock_rag_engine.GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID in profile["excluded_articles"]

    def test_f1_outgoing_rollover_to_external_is_not_incoming(self, mock_rag_engine):
        profile = mock_rag_engine._build_retrieval_profile(
            inquiry="I am a former employee and want to roll over my 401(k) to my Schwab account.",
            topic="rollover",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data={"participant_data": {"employment_status": "Terminated", "termination_date": "2026-01-10"}, "plan_data": {}},
        )
        assert profile["signals"]["incoming_rollover"] is False
        assert profile["primary_action"] != "incoming_rollover"
