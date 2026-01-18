"""
Tests para la API FastAPI.

Tests de integración para los endpoints de la API.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, Mock
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
    """Test client para FastAPI."""
    with patch('api.main.RAGEngine'), \
         patch('api.main.PineconeUploader'):
        from api.main import app
        return TestClient(app)


class TestHealthEndpoint:
    """Tests para /health endpoint."""
    
    def test_health_check_success(self, client):
        """Test health check exitoso."""
        with patch('api.main.pinecone_uploader') as mock_pinecone:
            mock_pinecone.get_index_stats.return_value = {'total_vectors': 33}
            
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
        
        with patch('api.main.rag_engine') as mock_engine:
            mock_engine.get_required_data.return_value = mock_response
            
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
        mock_response.response = {"sections": []}
        mock_response.guardrails = {"must_not_say": [], "must_verify": []}
        mock_response.metadata = {}
        
        with patch('api.main.rag_engine') as mock_engine:
            mock_engine.generate_response.return_value = mock_response
            
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


class TestRequestIDTracking:
    """Tests para Request ID tracking."""
    
    def test_request_id_in_response_headers(self, client):
        """Test que Request ID está en response headers."""
        response = client.get("/health")
        
        assert 'X-Request-ID' in response.headers
        assert len(response.headers['X-Request-ID']) > 0
