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
        assert pack.chunks == []

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
