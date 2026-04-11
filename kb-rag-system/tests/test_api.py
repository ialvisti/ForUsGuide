"""
Tests para la API FastAPI.

Tests de integración para los endpoints de la API.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, Mock, AsyncMock
import os


@pytest.fixture
def test_api_key():
    """API key para tests."""
    return "test-api-key-12345"


@pytest.fixture
def mock_env(test_api_key, monkeypatch):
    """Mock de variables de entorno."""
    monkeypatch.setenv("API_KEY", test_api_key)
    monkeypatch.setenv("PINECONE_API_KEY", "test-pinecone-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("INDEX_NAME", "test-index")
    monkeypatch.setenv("NAMESPACE", "test-namespace")


@pytest.fixture
def client(mock_env):
    """Test client for FastAPI.
    
    Patches constructors + validate_settings so the lifespan runs
    successfully and stores mocked instances on app.state.
    Uses context-manager form so lifespan events fire correctly.
    """
    mock_engine = Mock()
    mock_pinecone = Mock()
    mock_pinecone.get_index_stats.return_value = {'total_vectors': 0}
    
    with patch('api.main.validate_settings'), \
         patch('api.main.RAGEngine', return_value=mock_engine), \
         patch('api.main.PineconeUploader', return_value=mock_pinecone):
        from api.main import app
        with TestClient(app) as c:
            yield c


class TestHealthEndpoint:
    """Tests para /health endpoint."""
    
    def test_health_check_success(self, client):
        """Test health check exitoso."""
        client.app.state.pinecone_uploader.get_index_stats.return_value = {
            'total_vectors': 33
        }
        
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data['status'] in ['healthy', 'degraded']
        assert 'version' in data
        assert 'pinecone_connected' in data
        assert 'openai_configured' in data


class TestRootEndpoint:
    """Tests para / endpoint."""
    
    def test_root_endpoint(self, client):
        """Test root endpoint."""
        response = client.get("/")
        
        assert response.status_code == 200
        data = response.json()
        assert 'name' in data
        assert 'version' in data
        assert 'status' in data


class TestAuthenticationAPI:
    """Tests para autenticación."""
    
    def test_missing_api_key(self, client):
        """Test request sin API key."""
        response = client.post("/api/v1/required-data", json={})
        
        assert response.status_code == 401
        assert 'API key missing' in response.json()['message']
    
    def test_invalid_api_key(self, client, test_api_key):
        """Test request con API key inválida."""
        response = client.post(
            "/api/v1/required-data",
            json={},
            headers={"X-API-Key": "wrong-key"}
        )
        
        assert response.status_code == 403
        assert 'Invalid API key' in response.json()['message']


class TestRequiredDataEndpoint:
    """Tests para /api/v1/required-data."""
    
    def test_required_data_validation_error(self, client, test_api_key):
        """Test validación de datos."""
        response = client.post(
            "/api/v1/required-data",
            json={
                "inquiry": "short",  # Too short (min 10)
                "record_keeper": "LT Trust",
                "plan_type": "401(k)",
                "topic": "rollover"
            },
            headers={"X-API-Key": test_api_key}
        )
        
        assert response.status_code == 422  # Validation error
    
    def test_required_data_success(self, client, test_api_key):
        """Test request exitosa."""
        mock_response = Mock()
        mock_response.article_reference = {
            "article_id": "test",
            "title": "Test Article",
            "confidence": 0.9
        }
        mock_response.required_fields = {
            "participant_data": [],
            "plan_data": []
        }
        mock_response.confidence = 0.9
        mock_response.metadata = {}
        
        # The mock is on app.state from the lifespan; set async return value
        client.app.state.rag_engine.get_required_data = AsyncMock(
            return_value=mock_response
        )
        
        response = client.post(
            "/api/v1/required-data",
            json={
                "inquiry": "I want to rollover my 401k balance",
                "record_keeper": "LT Trust",
                "plan_type": "401(k)",
                "topic": "rollover"
            },
            headers={"X-API-Key": test_api_key}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert 'article_reference' in data
        assert 'required_fields' in data
        assert 'confidence' in data


class TestGenerateResponseEndpoint:
    """Tests para /api/v1/generate-response."""
    
    def test_generate_response_validation_error(self, client, test_api_key):
        """Test validación de datos."""
        response = client.post(
            "/api/v1/generate-response",
            json={
                "inquiry": "How?",  # Too short
                "record_keeper": "LT Trust",
                "plan_type": "401(k)",
                "topic": "rollover",
                "collected_data": {}
            },
            headers={"X-API-Key": test_api_key}
        )
        
        assert response.status_code == 422
    
    def test_generate_response_success(self, client, test_api_key):
        """Test request exitosa."""
        mock_response = Mock()
        mock_response.decision = "can_proceed"
        mock_response.confidence = 0.85
        mock_response.response = {
            "outcome": "can_proceed",
            "outcome_reason": "Test response",
            "response_to_participant": {
                "opening": "Test opening",
                "key_points": [],
                "steps": [],
                "warnings": []
            },
            "questions_to_ask": [],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [],
            "data_gaps": []
        }
        mock_response.metadata = {}
        
        # The mock is on app.state from the lifespan; set async return value
        client.app.state.rag_engine.generate_response = AsyncMock(
            return_value=mock_response
        )
        
        response = client.post(
            "/api/v1/generate-response",
            json={
                "inquiry": "How do I complete a rollover?",
                "record_keeper": "LT Trust",
                "plan_type": "401(k)",
                "topic": "rollover",
                "collected_data": {
                    "participant_data": {"balance": "$1000"},
                    "plan_data": {}
                },
                "max_response_tokens": 1500,
                "total_inquiries_in_ticket": 1
            },
            headers={"X-API-Key": test_api_key}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert 'decision' in data
        assert 'confidence' in data
        assert 'response' in data


class TestRequiredDataNoMatch:
    """Tests for required-data no-match early exit behavior."""

    def test_required_data_no_match_low_confidence_with_gaps(self, client, test_api_key):
        """When the engine returns null article_id with coverage gaps, the API
        should propagate the no-match response (200 OK, null article, empty fields)."""
        mock_response = Mock()
        mock_response.article_reference = {
            "article_id": None,
            "title": None,
            "confidence": 0.341
        }
        mock_response.required_fields = {
            "participant_data": [],
            "plan_data": []
        }
        mock_response.confidence = 0.341
        mock_response.source_articles = []
        mock_response.used_chunks = []
        mock_response.coverage_gaps = [
            "ForUsAll account activation email not received / account access setup troubleshooting"
        ]
        mock_response.metadata = {
            "no_match_reason": "Confidence (0.341) below threshold with coverage gaps",
            "chunks_used": 0,
            "sub_queries": ["activation email"],
            "per_query_scores": {},
            "unique_articles": 0,
            "relevant_articles": 0,
            "coverage_gaps": [
                "ForUsAll account activation email not received / account access setup troubleshooting"
            ]
        }

        client.app.state.rag_engine.get_required_data = AsyncMock(
            return_value=mock_response
        )

        response = client.post(
            "/api/v1/required-data",
            json={
                "inquiry": "Participant is not receiving the account activation email at matt@atlasup.com",
                "record_keeper": "LT Trust",
                "plan_type": "401(k)",
                "topic": "account_access"
            },
            headers={"X-API-Key": test_api_key}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["article_reference"]["article_id"] is None
        assert data["article_reference"]["title"] is None
        assert data["required_fields"]["participant_data"] == []
        assert data["required_fields"]["plan_data"] == []
        assert len(data["coverage_gaps"]) >= 1
        assert data["confidence"] < 0.40

    def test_required_data_normal_match_passes_through(self, client, test_api_key):
        """High-confidence matches should return a valid article reference."""
        mock_response = Mock()
        mock_response.article_reference = {
            "article_id": "lt_request_401k_termination_withdrawal_or_rollover",
            "title": "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover",
            "confidence": 0.85
        }
        mock_response.required_fields = {
            "participant_data": [
                {
                    "field": "termination_date",
                    "description": "Date of termination",
                    "why_needed": "Verify eligibility",
                    "data_type": "date",
                    "required": True
                }
            ],
            "plan_data": []
        }
        mock_response.confidence = 0.85
        mock_response.source_articles = []
        mock_response.used_chunks = []
        mock_response.coverage_gaps = []
        mock_response.metadata = {
            "chunks_used": 5,
            "sub_queries": ["termination rollover"],
            "per_query_scores": {},
            "unique_articles": 1,
            "relevant_articles": 1,
            "coverage_gaps": []
        }

        client.app.state.rag_engine.get_required_data = AsyncMock(
            return_value=mock_response
        )

        response = client.post(
            "/api/v1/required-data",
            json={
                "inquiry": "I left my job and want to roll over my 401k to Fidelity",
                "record_keeper": "LT Trust",
                "plan_type": "401(k)",
                "topic": "termination_distribution_request"
            },
            headers={"X-API-Key": test_api_key}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["article_reference"]["article_id"] is not None
        assert data["confidence"] >= 0.65
        assert data["coverage_gaps"] == []
        assert len(data["required_fields"]["participant_data"]) >= 1


class TestRequestIDTracking:
    """Tests para Request ID tracking."""
    
    def test_request_id_in_response_headers(self, client):
        """Test que Request ID está en response headers."""
        response = client.get("/health")
        
        assert 'X-Request-ID' in response.headers
        assert len(response.headers['X-Request-ID']) > 0
