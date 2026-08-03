"""
Tests para la API FastAPI.

Tests de integración para los endpoints de la API.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, Mock, AsyncMock
from typing import Optional
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
    # /api/v1/route-inquiry now gates on ROUTER_MODE; default 'disabled' would
    # 503 the whole endpoint suite. Keep tests honoring routes by default.
    monkeypatch.setenv("ROUTER_MODE", "full")
    # El singleton de settings se crea en el PRIMER import de api.config; si
    # otro archivo de tests lo importó antes con otro entorno, los setenv de
    # arriba no lo afectan. Fijar también el singleton hace a esta suite
    # independiente del orden de ejecución.
    import sys
    if "api.config" in sys.modules:
        from api.config import settings as _settings
        monkeypatch.setattr(_settings, "API_KEY", test_api_key)
        monkeypatch.setattr(_settings, "PINECONE_API_KEY", "test-pinecone-key")
        monkeypatch.setattr(_settings, "OPENAI_API_KEY", "test-openai-key")
        monkeypatch.setattr(_settings, "INDEX_NAME", "test-index")
        monkeypatch.setattr(_settings, "NAMESPACE", "test-namespace")
        monkeypatch.setattr(_settings, "ROUTER_MODE", "full")


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
    mock_inquiry_router = Mock()

    with patch('api.main.validate_settings'), \
         patch('api.main.RAGEngine', return_value=mock_engine), \
         patch('api.main.PineconeUploader', return_value=mock_pinecone), \
         patch('api.main.InquiryRouterEngine', return_value=mock_inquiry_router):
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
        mock_response.source_articles = []
        mock_response.used_chunks = []
        mock_response.coverage_gaps = []
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
        mock_response.source_articles = []
        mock_response.used_chunks = []
        mock_response.coverage_gaps = []
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


class TestRouteInquiryEndpoint:
    """Tests para /api/v1/route-inquiry."""

    def _make_classification(
        self,
        route: str,
        confidence: float,
        reasoning: str = "test",
        fast_path_hit: bool = True,
        signals: Optional[dict] = None,
        user_message: Optional[str] = None,
    ):
        """Build the dataclass-like result returned by InquiryRouterEngine.classify."""
        from data_pipeline.inquiry_router import ClassificationResult

        return ClassificationResult(
            route=route,
            confidence=confidence,
            reasoning=reasoning,
            signals=signals or {"is_short_interrogative": True},
            fast_path_hit=fast_path_hit,
            metadata={"latency_ms": 1.2, "model": None, "provider": None},
            user_message=user_message,
        )

    def test_route_inquiry_punctual_question(self, client, test_api_key):
        """Short timeframe question → knowledge_question with high confidence."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="knowledge_question", confidence=0.9
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": (
                    "Hi there I was wondering how many business days til I "
                    "can see it get approved. Thank you"
                )
            },
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "knowledge_question"
        assert data["confidence"] > 0.7
        assert data["suggested_endpoint"] == "/api/v1/knowledge-question"
        assert data["suggested_payload"] == {
            "question": (
                "Hi there I was wondering how many business days til I "
                "can see it get approved. Thank you"
            )
        }
        assert data["user_message"] is None

    def test_route_inquiry_hardship_routes_to_generate_response(
        self, client, test_api_key
    ):
        """Hardship + eligibility intent → generate_response with template payload."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="generate_response",
                confidence=0.88,
                signals={
                    "hardship_signal": True,
                    "has_eligibility_verb": True,
                },
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "Can I qualify for a hardship withdrawal for medical bills?"
            },
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "generate_response"
        assert data["suggested_endpoint"] == "/api/v1/generate-response"
        assert data["user_message"] is None

    def test_route_inquiry_transactional_rollover_routes_to_generate_response(
        self, client, test_api_key
    ):
        """Bug-report inquiry: 'I'd like to roll over my 401k into Fidelity'.

        Transactional intent without explicit eligibility verb. The engine's
        new fast-path rule routes this to generate_response because executing
        the rollover requires participant data (status, plan rules, balance,
        loans). Endpoint must surface the generate-response template.
        """
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="generate_response",
                confidence=0.85,
                reasoning="Transactional intent on participant funds.",
                signals={
                    "transactional_intent": True,
                    "has_action_verb": True,
                    "wants_funds": True,
                    "has_eligibility_verb": False,
                    "separation_signal": False,
                },
            )
        )

        inquiry = (
            "Hi, I'd like to roll over my 401k into my Fidelity account. "
            "Can you help me with that please?"
        )
        response = client.post(
            "/api/v1/route-inquiry",
            json={"inquiry": inquiry, "router_mode": "full"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "generate_response"
        assert data["confidence"] >= 0.85
        assert data["suggested_endpoint"] == "/api/v1/generate-response"
        assert data["signals"]["transactional_intent"] is True
        assert data["user_message"] is None

    def test_route_inquiry_ambiguous_includes_user_message(
        self, client, test_api_key
    ):
        """Ambiguous → needs_more_info, suggests required-data flow + populated user_message."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="needs_more_info",
                confidence=0.40,
                fast_path_hit=False,
                user_message="Could you tell me a bit more about what you need?",
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={"inquiry": "I'm not sure what I need to do here"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "needs_more_info"
        assert data["suggested_endpoint"] == "/api/v1/required-data"
        assert (
            data["user_message"]
            == "Could you tell me a bit more about what you need?"
        )

    def test_route_inquiry_user_message_null_on_other_routes(
        self, client, test_api_key
    ):
        """user_message must be null when the route is not needs_more_info."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="knowledge_question",
                confidence=0.95,
                user_message=None,
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={"inquiry": "What is the 60-day rollover rule?"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        assert response.json()["user_message"] is None

    def test_route_inquiry_auth_required(self, client):
        """Missing X-API-Key → 401."""
        response = client.post(
            "/api/v1/route-inquiry",
            json={"inquiry": "How long does approval take?"},
        )
        assert response.status_code == 401

    def test_route_inquiry_validation_short_inquiry(self, client, test_api_key):
        """Inquiries shorter than 10 chars are rejected by the request model."""
        response = client.post(
            "/api/v1/route-inquiry",
            json={"inquiry": "too short"},  # 9 chars
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == 422

    def test_route_inquiry_rejects_legacy_fields(self, client, test_api_key):
        """Legacy fields (record_keeper/topic/etc.) must be rejected with 422."""
        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "How do I rollover a 401k from a previous employer?",
                "topic": "rollover",  # legacy
            },
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == 422

    def test_route_inquiry_suggested_payload_generate_response_template(
        self, client, test_api_key
    ):
        """generate_response payload must be a template with placeholder None/{}."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="generate_response", confidence=0.9
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "Am I eligible to take a hardship for medical bills?"
            },
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        payload = response.json()["suggested_payload"]
        assert payload["inquiry"] == "Am I eligible to take a hardship for medical bills?"
        assert payload["record_keeper"] is None
        assert payload["plan_type"] is None
        assert payload["topic"] is None
        assert payload["collected_data"] == {}

    def test_route_inquiry_router_mode_disabled_returns_503(
        self, client, test_api_key
    ):
        """router_mode=disabled override → 503."""
        # The classifier should never be called when disabled.
        client.app.state.inquiry_router.classify = AsyncMock(
            side_effect=AssertionError("classifier should not run when disabled")
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "How long does approval take?",
                "router_mode": "disabled",
            },
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == 503

    def test_route_inquiry_router_mode_shadow_coerces_to_needs_more_info(
        self, client, test_api_key
    ):
        """router_mode=shadow → coerces a confident generate_response into needs_more_info."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="generate_response",
                confidence=0.92,
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "Can I qualify for a hardship withdrawal for medical bills?",
                "router_mode": "shadow",
            },
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "needs_more_info"
        # The original route is preserved in metadata for observability.
        assert data["metadata"]["original_route"] == "generate_response"
        assert "shadow" in data["metadata"]["router_mode_override"]
        # And user_message is populated even though the LLM didn't supply one
        # (the override fired AFTER classification).
        assert data["user_message"] is not None and data["user_message"].strip() != ""

    def test_route_inquiry_router_mode_knowledge_only_coerces_generate_response(
        self, client, test_api_key
    ):
        """router_mode=knowledge_only → generate_response → needs_more_info."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="generate_response", confidence=0.9
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "Am I eligible for a hardship withdrawal?",
                "router_mode": "knowledge_only",
            },
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "needs_more_info"
        assert data["metadata"]["original_route"] == "generate_response"

    def test_route_inquiry_router_mode_knowledge_only_passes_knowledge_through(
        self, client, test_api_key
    ):
        """router_mode=knowledge_only → knowledge_question is NOT coerced."""
        client.app.state.inquiry_router.classify = AsyncMock(
            return_value=self._make_classification(
                route="knowledge_question", confidence=0.9
            )
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={
                "inquiry": "What is the 60-day rollover rule?",
                "router_mode": "knowledge_only",
            },
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "knowledge_question"
        assert "router_mode_override" not in data["metadata"]


class TestCoveragePackBuilder:
    """The coverage pack builder retrieves the top-K KB chunks before each
    classification. Pinecone exceptions become ``CoveragePack.failed`` and
    zero results become ``CoveragePack.empty``; both states steer the LLM
    toward needs_more_info via the rendered coverage block.
    """

    @pytest.mark.asyncio
    async def test_unsafe_query_returns_blocked_non_pinecone_pack(self):
        from api.main import _make_coverage_pack_builder
        from data_pipeline.retrieval_privacy import UnsafeRetrievalQuery

        rag_engine = Mock()
        rag_engine._cached_query = AsyncMock(
            side_effect=UnsafeRetrievalQuery("sensitive text must stay private")
        )

        builder = _make_coverage_pack_builder(rag_engine)
        pack = await builder("synthetic retirement request")

        assert pack.retrieval_status == "blocked"
        assert pack.failure_kind == "unsafe_query"
        assert pack.retryable is False
        assert pack.pinecone_error is None

    @pytest.mark.asyncio
    async def test_pinecone_exception_returns_failed_pack(self):
        from api.main import _make_coverage_pack_builder

        rag_engine = Mock()
        rag_engine._cached_query = AsyncMock(
            side_effect=RuntimeError("pinecone outage")
        )

        builder = _make_coverage_pack_builder(rag_engine)
        pack = await builder("How do I rollover my 401k?")

        assert pack.retrieval_status == "failed"
        assert pack.top_score == 0.0
        assert pack.chunk_count == 0
        assert pack.pinecone_error == "RuntimeError"
        assert pack.failure_kind == "unknown"
        assert pack.retryable is False
        assert pack.chunks == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "failure_kind", "retryable"),
        [
            (400, "client_error", False),
            (429, "rate_limit", True),
            (503, "server_error", True),
        ],
    )
    async def test_typed_pinecone_failure_preserves_retry_taxonomy(
        self, status_code, failure_kind, retryable
    ):
        from api.main import _make_coverage_pack_builder
        from data_pipeline.pinecone_uploader import PineconeRetrievalError

        cause = RuntimeError("synthetic sdk failure")
        cause.status = status_code
        rag_engine = Mock()
        rag_engine._cached_query = AsyncMock(
            side_effect=PineconeRetrievalError(
                index_name="synthetic-index",
                namespace="synthetic-namespace",
                top_k=5,
                filter_dict=None,
                rerank=None,
                cause=cause,
            )
        )

        pack = await _make_coverage_pack_builder(rag_engine)(
            "synthetic retirement request"
        )

        assert pack.retrieval_status == "failed"
        assert pack.pinecone_error == "PineconeRetrievalError"
        assert pack.failure_kind == failure_kind
        assert pack.retryable is retryable

    @pytest.mark.asyncio
    async def test_zero_chunks_returns_empty_pack(self):
        from api.main import _make_coverage_pack_builder

        rag_engine = Mock()
        rag_engine._cached_query = AsyncMock(return_value=[])

        builder = _make_coverage_pack_builder(rag_engine)
        pack = await builder("How do I rollover my 401k?")

        assert pack.retrieval_status == "empty"
        assert pack.top_score == 0.0
        assert pack.chunk_count == 0
        assert pack.pinecone_error is None

    def test_route_inquiry_with_empty_retrieval_does_not_return_kq(
        self, client, test_api_key, monkeypatch
    ):
        # End-to-end through the route-inquiry endpoint: when retrieval is
        # empty the LLM sees retrieval_status=empty and should pick NMI.
        from api.main import _make_coverage_pack_builder
        from api.config import settings
        from data_pipeline.inquiry_router import InquiryRouterEngine
        from data_pipeline.llm_router import LLMResponse

        monkeypatch.setattr(settings, "API_KEY", test_api_key)
        rag_engine = Mock()
        rag_engine._cached_query = AsyncMock(return_value=[])
        llm_router = Mock()
        llm_router.call = AsyncMock(
            return_value=LLMResponse(
                content=(
                    '{"route": "needs_more_info", "confidence": 0.9, '
                    '"reasoning": "no chunks retrieved", '
                    '"coverage_basis": "no_coverage", '
                    '"user_message": "Could you share more detail?"}'
                ),
                usage=None,
                provider_used="gemini",
                model_used="gemini-2.5-flash",
            )
        )
        client.app.state.inquiry_router = InquiryRouterEngine(
            llm_router=llm_router,
            coverage_pack_builder=_make_coverage_pack_builder(rag_engine),
        )

        response = client.post(
            "/api/v1/route-inquiry",
            json={"inquiry": "How long does approval take?"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "needs_more_info"
        assert data["suggested_endpoint"] == "/api/v1/required-data"
        assert data["metadata"]["coverage_signals"]["retrieval_status"] == "empty"
        assert data["metadata"]["coverage_signals"]["top_score"] == 0.0
        assert data["metadata"]["coverage_basis"] == "no_coverage"

    @pytest.mark.asyncio
    async def test_chunks_present_populates_pack(self):
        from api.main import _make_coverage_pack_builder

        rag_engine = Mock()
        rag_engine._cached_query = AsyncMock(
            return_value=[
                {
                    "id": "c1",
                    "score": 0.62,
                    "metadata": {
                        "article_title": "Hardship Article",
                        "chunk_type": "business_rules",
                        "chunk_tier": "high",
                        "topic": "hardship",
                        "content": "Approval typically takes 7 business days.",
                    },
                },
                {
                    "id": "c2",
                    "score": 0.55,
                    "metadata": {
                        "article_title": "Hardship Article",
                        "chunk_type": "steps",
                        "chunk_tier": "high",
                        "topic": "hardship",
                        "content": "Step 1: submit the form.",
                    },
                },
            ]
        )

        builder = _make_coverage_pack_builder(rag_engine)
        pack = await builder("How long does approval take?")

        assert pack.retrieval_status == "ok"
        assert pack.top_score == pytest.approx(0.62)
        assert pack.chunk_count == 2
        assert pack.distinct_articles == ["Hardship Article"]
        assert pack.chunk_types_present == ["business_rules", "steps"]
        assert len(pack.chunks) == 2


class TestTicketHandlerContainment:
    """Task 0 (contención HT-02/HT-10): la configuración del ticket handler es
    fail-closed for unreviewed origins and missing credentials, while preserving
    the exact live ForUsBots origin."""

    def _pin_settings(self, monkeypatch, **overrides):
        """Fija en el singleton una configuración base válida y aplica overrides."""
        from api.config import settings as app_settings
        base = dict(
            APP_ROLE="producer",
            API_KEY="k",
            API_CLIENT_KEYS={"n8n": "mapped-k"},
            API_CLIENT_TENANTS={"n8n": "tenant-a"},
            PINECONE_API_KEY="p",
            OPENAI_API_KEY="o",
            GEMINI_API_KEY="g",
            ENVIRONMENT="production",
            APP_ENV="production",
            TICKET_HANDLER_MODE="full",
            FORUSBOTS_BASE_URL="https://forusbots.internal.example",
            FORUSBOTS_AUTH_TOKEN="tok",
            LLM_ROUTE_CLASSIFY="gpt-5.5",
            LLM_ROUTE_DECOMPOSE="gpt-5.5",
            LLM_ROUTE_GR_OUTCOME="gpt-5.5",
            LLM_ROUTE_GR_RESPONSE="gpt-5.5",
            LLM_ROUTE_KNOWLEDGE="gpt-5.5",
            LLM_ROUTE_REQUIRED_DATA="gpt-5.5",
            LLM_ROUTE_EXTRACT_INQUIRIES="gpt-5.5",
            LLM_ROUTE_KB_QUESTION_SYNTHESIS="gpt-5.5",
            LLM_ROUTE_FORUSBOTS_FIELD_MAP="gpt-5.5",
            LLM_ROUTE_GR_BODY_BUILD="gpt-5.5",
            LLM_ROUTE_TICKET_FIELD_EXTRACT="gpt-5.5",
            PARTICIPANT_PLAN_SOURCE="",
            TICKET_WIF_AUDIENCE="",
            TICKET_WIF_ALLOWED_EMAILS=[],
            TICKET_WIF_EXPECTED_EMAIL="",
            TICKET_LLM_PRICING_JSON=(
                '{"pricing_as_of":"2026-07-21",'
                '"source":"openai-google-official-public-pricing","models":{'
                '"openai:gpt-5.5":{"input_usd_per_million":5.0,'
                '"output_usd_per_million":30.0},'
                '"gemini:gemini-2.5-pro":{"input_usd_per_million":1.25,'
                '"output_usd_per_million":10.0}}}'
            ),
            TICKET_JOB_BACKEND="firestore",
            FIRESTORE_DATABASE="(default)",
            TICKET_TASK_QUEUE="cloudtasks",
            GCP_PROJECT="rag-kb-system",
            CLOUD_TASKS_LOCATION="us-central1",
            CLOUD_TASKS_QUEUE="ticket-jobs",
            TICKET_WORKER_URL="https://worker.example.run.app",
            TICKET_WORKER_AUDIENCE=(
                "https://kb-rag-ticket-worker."
                "rag-kb-system.ticket.internal"
            ),
            TICKET_WORKER_SERVICE_ACCOUNT=(
                "ticket-task-signer-prod@rag-kb-system.iam.gserviceaccount.com"
            ),
            TICKET_WORKER_REQUIRE_OIDC=True,
        )
        base.update(overrides)
        for key, value in base.items():
            monkeypatch.setattr(app_settings, key, value)

    def test_full_mode_accepts_reviewed_live_forusbots_origin(self, monkeypatch):
        from api.config import validate_settings
        self._pin_settings(
            monkeypatch,
            APP_ROLE="worker",
            FORUSBOTS_BASE_URL="http://35.224.156.104:10000",
        )
        assert validate_settings() is True

    def test_staging_active_accepts_reviewed_live_forusbots_origin(self, monkeypatch):
        from api.config import validate_settings

        self._pin_settings(
            monkeypatch,
            APP_ROLE="worker",
            ENVIRONMENT="staging",
            APP_ENV="staging",
            FIRESTORE_DATABASE="ticket-staging",
            FORUSBOTS_BASE_URL="http://35.224.156.104:10000",
            TICKET_WORKER_AUDIENCE=(
                "https://kb-rag-ticket-worker-staging."
                "rag-kb-system.ticket.internal"
            ),
            TICKET_WORKER_SERVICE_ACCOUNT=(
                "ticket-task-signer-stg@rag-kb-system.iam.gserviceaccount.com"
            ),
        )
        assert validate_settings() is True

    @pytest.mark.parametrize("base_url", [
        "https://user:raw-secret@forusbots.internal.example",
        "https://forusbots.internal.example?token=raw-secret",
        "https://forusbots.internal.example#raw-secret",
        "https://forusbots.internal.example/unreviewed-prefix",
    ])
    def test_active_mode_rejects_noncanonical_forusbots_origin_without_echoing_it(
        self, monkeypatch, base_url,
    ):
        from api.config import validate_settings

        self._pin_settings(
            monkeypatch, APP_ROLE="worker", FORUSBOTS_BASE_URL=base_url
        )
        with pytest.raises(
            ValueError, match="origen canónico revisado"
        ) as captured:
            validate_settings()

        assert "raw-secret" not in str(captured.value)

    def test_reconciler_validation_does_not_require_api_pinecone_or_llm_credentials(
            self, monkeypatch):
        from api.config import validate_settings

        self._pin_settings(
            monkeypatch,
            APP_ROLE="reconciler",
            TICKET_HANDLER_MODE="disabled",
            API_KEY="",
            API_CLIENT_KEYS={},
            API_CLIENT_TENANTS={},
            PINECONE_API_KEY="",
            OPENAI_API_KEY="",
            GEMINI_API_KEY="",
            USE_VERTEX_AI=False,
            TICKET_LLM_PRICING_JSON=(
                '{"pricing_as_of":"2026-07-21","source":"official",'
                '"models":{"openai:gpt-5.5":{'
                '"input_usd_per_million":5.0,'
                '"output_usd_per_million":30.0}}}'
            ),
            TICKET_JOB_BACKEND="firestore",
            FIRESTORE_DATABASE="(default)",
            TICKET_TASK_QUEUE="cloudtasks",
            GCP_PROJECT="rag-kb-system",
            CLOUD_TASKS_LOCATION="us-central1",
            CLOUD_TASKS_QUEUE="ticket-jobs",
            TICKET_WORKER_URL="https://worker.example.run.app",
            TICKET_WORKER_AUDIENCE=(
                "https://kb-rag-ticket-worker."
                "rag-kb-system.ticket.internal"
            ),
            TICKET_WORKER_SERVICE_ACCOUNT=(
                "ticket-task-signer-prod@rag-kb-system.iam.gserviceaccount.com"
            ),
            TICKET_WORKER_REQUIRE_OIDC=True,
        )
        assert validate_settings() is True

    def test_active_production_preserves_legacy_api_key_without_tenant_mapping(
            self, monkeypatch):
        from api.config import validate_settings

        self._pin_settings(
            monkeypatch,
            API_CLIENT_KEYS={"n8n": "mapped-k"},
            API_CLIENT_TENANTS={},
        )
        assert validate_settings() is True

    def test_full_mode_requires_forusbots_token(self, monkeypatch):
        """Un modo activo sin FORUSBOTS_AUTH_TOKEN no arranca (antes era warning)."""
        from api.config import validate_settings
        self._pin_settings(
            monkeypatch, APP_ROLE="worker", FORUSBOTS_AUTH_TOKEN=""
        )
        with pytest.raises(ValueError, match="FORUSBOTS_AUTH_TOKEN"):
            validate_settings()

    def test_non_production_accepts_same_reviewed_forusbots_origin(self, monkeypatch):
        from api.config import validate_settings
        self._pin_settings(
            monkeypatch,
            APP_ROLE="worker",
            ENVIRONMENT="development",
            APP_ENV="development",
            FORUSBOTS_BASE_URL="http://35.224.156.104:10000",
        )
        assert validate_settings() is True

    def test_request_cannot_expand_server_mode(self, client, test_api_key, monkeypatch):
        """Servidor disabled + body ticket_handler_mode=full → sigue 503."""
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "API_KEY", test_api_key)
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        response = client.post(
            "/api/v1/handle-ticket",
            json={
                "participant_id": "158948",
                "plan_id": "580",
                "company_name": "StarWars Inc.",
                "company_status": "Ongoing",
                "ticket": {
                    "username": "Ivan",
                    "user_email": "i@f.com",
                    "email_subject": "401k",
                    "email_body": "quiero retirar mi 401k",
                },
                "ticket_handler_mode": "full",
            },
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == 503

    def test_request_can_narrow_server_mode(self, client, test_api_key, monkeypatch):
        """Servidor full + body ticket_handler_mode=disabled → 503 (narrowing sí)."""
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "API_KEY", test_api_key)
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
        response = client.post(
            "/api/v1/handle-ticket",
            json={
                "participant_id": "158948",
                "plan_id": "580",
                "company_name": "StarWars Inc.",
                "company_status": "Ongoing",
                "ticket": {
                    "username": "Ivan",
                    "user_email": "i@f.com",
                    "email_subject": "401k",
                    "email_body": "quiero retirar mi 401k",
                },
                "ticket_handler_mode": "disabled",
            },
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# Producción (plan de finalización, Tarea 2 Pasos 1/4) — contrato de auth de
# rutas no-ticket y roles de proceso excluyentes. RED hasta Tarea 4 Paso 1a.
# ---------------------------------------------------------------------------

class TestNonTicketAuthContract:
    """El endurecimiento de tickets NO puede alterar la autenticación de las
    rutas core existentes (regresión guard)."""

    def test_non_ticket_routes_keep_existing_auth_contract(self, client, test_api_key, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "API_KEY", test_api_key)

        # /health es público (probe de Cloud Run)
        assert client.get("/health").status_code == 200

        # el contrato EXISTENTE: required-data/generate-response/route-inquiry
        # exigen X-API-Key (sin header → 401, key inválida → 403); el
        # endurecimiento de tickets no debe alterarlo.
        body = {"inquiry": "cash out my 401k please", "record_keeper": "LT Trust"}
        r_missing = client.post("/api/v1/required-data", json=body)
        assert r_missing.status_code == 401
        r_wrong = client.post("/api/v1/required-data", json=body,
                              headers={"X-API-Key": "wrong-key"})
        assert r_wrong.status_code == 403
        # con la key válida la ruta NO devuelve error de autenticación
        client.app.state.rag_engine.get_required_data = AsyncMock(
            side_effect=RuntimeError("engine stub"))
        r_ok = client.post("/api/v1/required-data", json=body,
                           headers={"X-API-Key": test_api_key})
        assert r_ok.status_code not in (401, 403)


class TestAppRoleSeparation:
    """APP_ROLE=producer|worker|reconciler con rutas excluyentes (plan
    Tarea 4 Paso 1a). El producer conserva la API completa no-ticket."""

    def test_producer_role_preserves_non_ticket_routes_and_core_readiness(self, client, monkeypatch):
        from api.config import settings as app_settings
        assert hasattr(app_settings, "APP_ROLE"), (
            "RED: settings.APP_ROLE no existe — los roles de proceso "
            "excluyentes no están implementados (Tarea 4 Paso 1a)"
        )
        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        paths = {getattr(r, "path", None) for r in client.app.routes}
        core = {"/health", "/livez", "/readyz", "/api/v1/knowledge-question",
                "/api/v1/generate-response", "/api/v1/required-data",
                "/api/v1/route-inquiry", "/api/v1/chunks", "/api/v1/index-stats"}
        assert core <= paths, f"faltan rutas core: {core - paths}"
        # el producer NUNCA sirve la ruta interna del worker (404: no revela)
        r = client.post("/internal/tasks/ticket-job", json={"job_id": "x"})
        assert r.status_code == 404, (
            f"el producer respondió {r.status_code} en la ruta interna del "
            "worker; debe ocultarla con 404"
        )
        # el rol worker sólo sirve la ruta interna + probes
        monkeypatch.setattr(app_settings, "APP_ROLE", "worker")
        assert client.get("/livez").status_code == 200
        r_core = client.post("/api/v1/knowledge-question", json={"question": "q"})
        assert r_core.status_code == 404, (
            f"el worker respondió {r_core.status_code} en una ruta del "
            "producer; los roles no son excluyentes"
        )

    def test_openapi_contains_only_routes_served_by_each_role(
        self, client, monkeypatch,
    ):
        from api.config import settings as app_settings

        expected = {
            "producer": {
                "/api/v1/knowledge-question",
                "/api/v2/handle-ticket",
            },
            # The internal task endpoint deliberately has
            # include_in_schema=False; probes are excluded as well.
            "worker": set(),
            "reconciler": set(),
        }
        forbidden = {
            "producer": {"/internal/tasks/ticket-job"},
            "worker": {"/api/v1/knowledge-question", "/api/v2/handle-ticket"},
            "reconciler": {
                "/internal/tasks/ticket-job",
                "/api/v1/knowledge-question",
                "/api/v2/handle-ticket",
            },
        }

        # Do not clear the cache between roles: this also proves a schema from
        # one process role cannot bleed into another role's documentation.
        client.app.openapi_schema = None
        for role in ("producer", "worker", "reconciler"):
            monkeypatch.setattr(app_settings, "APP_ROLE", role)
            paths = set(client.app.openapi()["paths"])
            assert expected[role] <= paths
            assert paths.isdisjoint(forbidden[role])


# ---------------------------------------------------------------------------
# Producción (Tarea 11) — readiness role-aware y sanitización de métricas
# ---------------------------------------------------------------------------

class TestRoleAwareReadiness:

    def test_participant_plan_validator_contract_requires_safe_health_probe(self):
        from api.participant_plan import ParticipantPlanValidator

        class _AuthorizeOnly:
            async def authorize(self, **_kwargs):
                return None

        assert not isinstance(_AuthorizeOnly(), ParticipantPlanValidator)

    def test_producer_disabled_actually_probes_core_pinecone(
            self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        client.app.state.pinecone_uploader.get_index_stats.side_effect = (
            RuntimeError("secret upstream detail")
        )

        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["unhealthy"] == ["pinecone"]
        assert "secret upstream detail" not in response.text

    def test_producer_disabled_ready_without_admission_deps(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        # Sin validador/cola/ForusBots: disabled sigue READY, pero conserva el
        # repositorio durable para polling de trabajos ya admitidos.
        client.app.state.participant_plan_validator = None
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["role"] == "producer"

    def test_readiness_uses_a_firestore_legal_job_id(self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.ticket_repo = repo

        response = client.get("/readyz")

        assert response.status_code == 200
        job_id = repo.get.await_args.args[0]
        assert len(job_id) == 32
        assert set(job_id) <= set("0123456789abcdef")

    def test_producer_disabled_requires_polling_repository(self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "disabled")
        client.app.state.ticket_repo = None

        response = client.get("/readyz")

        assert response.status_code == 503
        assert "ticket_repo" in response.json()["missing"]

    def test_producer_active_preserves_n8n_without_optional_validator(
            self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
        client.app.state.participant_plan_validator = None
        client.app.state.ticket_repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.ticket_queue = Mock(
            estimated_queue_delay_s=AsyncMock(return_value=0.0),
            aclose=AsyncMock(),
        )
        r = client.get("/readyz")
        assert r.status_code == 200
        assert "participant_plan_validator" not in r.json().get("missing", [])

    def test_producer_active_does_not_require_forusbots_client(
            self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
        client.app.state.participant_plan_validator = Mock(
            authorize=AsyncMock(),
            health=AsyncMock(return_value={"status": "ok"}),
        )
        client.app.state.ticket_repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.ticket_queue = Mock(
            estimated_queue_delay_s=AsyncMock(return_value=0.0),
            aclose=AsyncMock(),
        )
        client.app.state.forusbots_client = None
        r = client.get("/readyz")
        assert r.status_code == 200
        assert "forusbots_client" not in r.json().get("missing", [])

    def test_producer_active_probes_only_admission_dependencies(
            self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "producer")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
        client.app.state.participant_plan_validator = Mock(
            authorize=AsyncMock(),
            health=AsyncMock(return_value={"status": "ok"}),
        )
        client.app.state.ticket_repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.ticket_queue = Mock(
            estimated_queue_delay_s=AsyncMock(return_value=0.0),
            aclose=AsyncMock(),
        )
        client.app.state.forusbots_client = None

        response = client.get("/readyz")

        assert response.status_code == 200
        client.app.state.ticket_repo.get.assert_awaited_once()
        client.app.state.ticket_queue.estimated_queue_delay_s.assert_awaited_once()

    def test_reconciler_fails_readiness_when_queue_probe_fails(
            self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "reconciler")
        client.app.state.ticket_repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.ticket_queue = Mock(
            estimated_queue_delay_s=AsyncMock(
                side_effect=RuntimeError("credential material")
            ),
            aclose=AsyncMock(),
        )

        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["unhealthy"] == ["ticket_queue"]
        assert "credential material" not in response.text

    def test_worker_probes_execution_dependencies(self, client, monkeypatch):
        from api.config import settings as app_settings

        monkeypatch.setattr(app_settings, "APP_ROLE", "worker")
        monkeypatch.setattr(app_settings, "TICKET_HANDLER_MODE", "full")
        client.app.state.ticket_repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.forusbots_client = Mock(
            health=AsyncMock(side_effect=RuntimeError("private upstream")),
            aclose=AsyncMock(),
        )

        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["unhealthy"] == ["forusbots"]
        assert "private upstream" not in response.text
        client.app.state.ticket_repo.get.assert_awaited_once()
        client.app.state.pinecone_uploader.get_index_stats.assert_called()

    def test_reconciler_ready_without_llm_provider(self, client, monkeypatch):
        from api.config import settings as app_settings
        monkeypatch.setattr(app_settings, "APP_ROLE", "reconciler")
        # sin proveedor LLM el reconciliador sigue READY (no lo usa)
        monkeypatch.setattr(app_settings, "OPENAI_API_KEY", "")
        monkeypatch.setattr(app_settings, "GEMINI_API_KEY", "")
        monkeypatch.setattr(app_settings, "USE_VERTEX_AI", False)
        # This test mutates the role after the fixture's producer startup, so
        # inject the reconciler's own healthy dependencies explicitly. Actual
        # role-specific construction is covered by test_app_role_startup.py.
        client.app.state.ticket_repo = Mock(get=AsyncMock(return_value=None))
        client.app.state.ticket_queue = Mock(
            estimated_queue_delay_s=AsyncMock(return_value=0.0),
            aclose=AsyncMock(),
        )
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["role"] == "reconciler"

    def test_livez_has_no_external_io(self, client):
        assert client.get("/livez").json() == {"status": "ok"}


class TestMetricsSanitization:

    def test_emit_drops_non_allowlisted_labels(self):
        from api import metrics
        import logging

        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _Cap()
        h.setLevel(logging.INFO)
        metrics.logger.addHandler(h)
        prev_level = metrics.logger.level
        metrics.logger.setLevel(logging.INFO)
        job_hash = "a" * 64
        try:
            metrics.emit("ticket_job_terminal", 1, job_hash=job_hash,
                         trace_id="t1", state="succeeded", code="none",
                         participant_id="158948",  # PROHIBIDO: debe filtrarse
                         email="luke@example.com")  # PROHIBIDO
        finally:
            metrics.logger.removeHandler(h)
            metrics.logger.setLevel(prev_level)

        blob = "\n".join(records)
        assert "158948" not in blob, "un ID de participante llegó a las métricas"
        assert "luke@example.com" not in blob
        assert "succeeded" in blob and job_hash in blob
