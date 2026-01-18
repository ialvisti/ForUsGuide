"""
Tests para el RAG Engine.

Tests unitarios para las funciones principales del RAG engine.
"""

import pytest
from unittest.mock import Mock, patch
from data_pipeline.rag_engine import RAGEngine


class TestRAGEngine:
    """Tests para RAGEngine."""
    
    @pytest.fixture
    def mock_rag_engine(self):
        """Fixture para RAG engine con mocks."""
        with patch('data_pipeline.rag_engine.OpenAI'), \
             patch('data_pipeline.rag_engine.PineconeUploader'):
            engine = RAGEngine(
                openai_api_key="test-key",
                model="gpt-4o-mini"
            )
            return engine
    
    def test_rag_engine_initialization(self, mock_rag_engine):
        """Test que RAG engine se inicializa correctamente."""
        assert mock_rag_engine is not None
        assert mock_rag_engine.model == "gpt-4o-mini"
        assert mock_rag_engine.temperature == 0.1
    
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
        assert confidence > 0.7  # Should have boost from critical chunk
    
    def test_determine_decision_high_confidence(self, mock_rag_engine):
        """Test decision con alta confidence."""
        decision = mock_rag_engine._determine_decision(0.85)
        assert decision == "can_proceed"
    
    def test_determine_decision_medium_confidence(self, mock_rag_engine):
        """Test decision con media confidence."""
        decision = mock_rag_engine._determine_decision(0.65)
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
    def engine(self):
        """Engine para tests."""
        with patch('data_pipeline.rag_engine.OpenAI'), \
             patch('data_pipeline.rag_engine.PineconeUploader'):
            return RAGEngine(openai_api_key="test")
    
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
