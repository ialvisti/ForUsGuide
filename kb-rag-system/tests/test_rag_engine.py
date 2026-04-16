"""
Tests para el RAG Engine.

Tests unitarios para las funciones principales del RAG engine.

The engine now delegates all LLM calls to an `LLMRouter`, so tests build a
mock router that exposes an async `call()` method and pass it into the
`RAGEngine` constructor.
"""

import pytest
from unittest.mock import Mock, patch


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
