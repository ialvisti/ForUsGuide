"""
Tests para el RAG Engine.

Tests unitarios para las funciones principales del RAG engine.

The engine now delegates all LLM calls to an `LLMRouter`, so tests build a
mock router that exposes an async `call()` method and pass it into the
`RAGEngine` constructor.
"""

import json as _json

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
            model_used="gpt-5.4",
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
        assert resp.metadata["model"] == "gpt-5.4"
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
