"""
Tests para el RAG Engine.

Tests unitarios para las funciones principales del RAG engine.

The engine now delegates all LLM calls to an `LLMRouter`, so tests build a
mock router that exposes an async `call()` method and pass it into the
`RAGEngine` constructor.
"""

import json as _json
import asyncio

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

        async def fake_decompose(inquiry):
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

        async def fake_decompose(inquiry):
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
