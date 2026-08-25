"""Regression coverage for ticket-associated calls to legacy RAG endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from data_pipeline.ticket_job_repository import (
    RAG_INVOCATIONS_COLLECTION,
    TICKET_EVALUATION_OUTBOX_COLLECTION,
    InMemoryTicketJobBackend,
    TicketJobRepository,
)


def _request_cases(ticket_id: str | None):
    from api.models import (
        GenerateResponseRequest,
        KnowledgeQuestionRequest,
        RequiredDataRequest,
        RouteInquiryRequest,
    )

    return (
        RequiredDataRequest(
            inquiry="How can I roll over this account?",
            plan_type="401(k)",
            topic="rollover",
            ticket_id=ticket_id,
        ),
        GenerateResponseRequest(
            inquiry="How can I roll over this account?",
            plan_type="401(k)",
            topic="rollover",
            collected_data={},
            ticket_id=ticket_id,
        ),
        KnowledgeQuestionRequest(
            question="What are the rollover rules for this plan?",
            ticket_id=ticket_id,
        ),
        RouteInquiryRequest(
            inquiry="What are the rollover rules for this plan?",
            ticket_id=ticket_id,
        ),
    )


def test_direct_rag_models_accept_and_normalize_optional_devrev_ticket_identity():
    for request in _request_cases(" tkt-1234 "):
        assert request.ticket_id == "TKT-1234"

    for request in _request_cases(None):
        assert request.ticket_id is None


def test_generate_response_rejects_context_only_ticket_identity():
    from api.models import GenerateResponseRequest

    with pytest.raises(ValidationError, match="top-level ticket_id"):
        GenerateResponseRequest(
            inquiry="How can I roll over this account?",
            plan_type="401(k)",
            topic="rollover",
            collected_data={},
            context={"ticket_id": "tkt-1234"},
        )


def test_generate_response_accepts_matching_top_level_and_context_identity():
    from api.models import GenerateResponseRequest

    request = GenerateResponseRequest(
        inquiry="How can I roll over this account?",
        plan_type="401(k)",
        topic="rollover",
        collected_data={},
        ticket_id="TKT-1234",
        context={"ticket_id": "tkt-1234"},
    )

    assert request.ticket_id == "TKT-1234"


def test_generate_response_rejects_conflicting_top_level_and_context_identity():
    from api.models import GenerateResponseRequest

    with pytest.raises(ValidationError, match="ticket_id"):
        GenerateResponseRequest(
            inquiry="How can I roll over this account?",
            plan_type="401(k)",
            topic="rollover",
            collected_data={},
            ticket_id="TKT-1234",
            context={"ticket_id": "TKT-9999"},
        )


def test_ticket_builder_prompts_do_not_entrust_identity_to_the_llm():
    repo_root = Path(__file__).resolve().parents[2]
    knowledge_prompts = (
        repo_root / "External agents/Knowledge Question Inquiry Generator.md",
        repo_root / "PA/n8n prompts/PROMPT_KNOWLEDGE_QUESTION_BUILDER.md",
        Path(__file__).resolve().parents[1]
        / "data_pipeline/agent_prompts/kb_question_synthesis.md",
    )
    generate_prompts = (
        repo_root / "External agents/Generate Response Body Builder.md",
        repo_root / "PA/n8n prompts/PROMPT_GENERATE_RESPONSE_BODY_BUILDER.md",
        Path(__file__).resolve().parents[1]
        / "data_pipeline/agent_prompts/gr_body_build.md",
    )

    for prompt_path in knowledge_prompts:
        prompt = prompt_path.read_text(encoding="utf-8")
        assert '"ticketId"' not in prompt
        assert '"ticket_id": "TKT-XXXXXX"' not in prompt
        assert "copy `ticketData.ticketId` exactly" not in prompt
    for prompt_path in generate_prompts:
        prompt = prompt_path.read_text(encoding="utf-8")
        assert '"ticket_id"' not in prompt
        assert "ticketData.ticketId" not in prompt
        assert "top-level `ticket_id`" not in prompt


def test_runtime_kq_output_contract_rejects_model_owned_ticket_identity():
    from data_pipeline.llm_output_models import KBQuestionSynthesisOut

    with pytest.raises(ValidationError, match="ticket_id"):
        KBQuestionSynthesisOut.model_validate({
            "ticket_id": "TKT-9999",
            "question": "What are the rollover timing rules?",
        })


@pytest.mark.parametrize(
    "invalid",
    (
        "not a DevRev id",
        "https://example.invalid/TKT-1234",
        "TKT-1234\nX-Injected: value",
        "TKT-NOT-NUMERIC",
    ),
)
def test_direct_rag_models_reject_unusable_ticket_identity(invalid):
    with pytest.raises(ValidationError, match="ticket"):
        _request_cases(invalid)


@pytest.mark.parametrize(
    ("route", "expected_path"),
    (
        ("knowledge_question", "/api/v1/knowledge-question"),
        ("generate_response", "/api/v1/generate-response"),
        ("needs_more_info", "/api/v1/required-data"),
    ),
)
def test_router_propagates_ticket_identity_to_the_downstream_call(
    route,
    expected_path,
):
    from api.main import _build_suggested_call

    path, payload = _build_suggested_call(
        "What are the rollover rules for this plan?",
        route,
        ticket_id="TKT-1234",
    )

    assert path == expected_path
    assert payload["ticket_id"] == "TKT-1234"


async def test_standalone_direct_invocation_is_durable_before_completion():
    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)

    invocation_id = await repo.begin_direct_rag_invocation(
        request_id="00000000-0000-4000-8000-000000000001",
        ticket_id="TKT-1234",
        route="knowledge_question",
        inquiry="What are the rollover rules for this plan?",
        topic="general",
        tenant_id="tenant-a",
    )

    journal = backend._data[RAG_INVOCATIONS_COLLECTION][invocation_id]
    assert journal["state"] == "started"
    assert journal["event_seed"]["ticket_id"] == "TKT-1234"
    assert journal["event_seed"]["tenant_id"] == "tenant-a"
    assert not backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION)

    await repo.complete_rag_invocation(
        invocation_id,
        {
            "route": "knowledge_question",
            "execution_status": "succeeded",
            "participant_reply_safe": True,
            "degraded": False,
            "result": {
                "inquiry": "What are the rollover rules for this plan?",
                "topic": "general",
                "knowledge_answer": {
                    "answer": "A bounded answer",
                    "source_articles": [],
                    "metadata": {},
                },
            },
        },
    )

    assert journal is not backend._data[RAG_INVOCATIONS_COLLECTION][invocation_id]
    completed = backend._data[RAG_INVOCATIONS_COLLECTION][invocation_id]
    assert completed["state"] == "completed"
    outbox = backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION][invocation_id]
    assert outbox["state"] == "pending"
    assert outbox["event"]["answer"] == "A bounded answer"


async def test_direct_success_strips_full_chunk_content_before_durable_event():
    from api.direct_ticket_evaluation import direct_success_entry
    from api.models import KnowledgeQuestionResponse, UsedChunk

    private_chunk = "participant-private-chunk-" * 20_000
    response = KnowledgeQuestionResponse(
        answer="A bounded answer",
        key_points=[],
        source_articles=[],
        used_chunks=[UsedChunk(
            chunk_id="chunk-1",
            score=0.91,
            chunk_type="business_rules",
            chunk_tier="high",
            article_id="article-1",
            article_title="Rollover rules",
            content_preview="Bounded safe preview",
            content=private_chunk,
        )],
        confidence_note="well_covered",
        metadata={"model": "test-model"},
    )

    entry = direct_success_entry(
        route="knowledge_question",
        inquiry="What are the rollover timing rules?",
        topic="general",
        response=response,
    )

    assert entry["result"]["knowledge_answer"]["used_chunks"] == []
    assert private_chunk not in repr(entry)
    evidence = entry["evaluation_evidence"]["chunks"][0]
    assert evidence["preview"] == "Bounded safe preview"
    assert len(evidence["content_hash"]) == 64

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    invocation_id = await repo.begin_direct_rag_invocation(
        request_id="00000000-0000-4000-8000-000000000002",
        ticket_id="TKT-1234",
        route="knowledge_question",
        inquiry="What are the rollover timing rules?",
        topic="general",
    )
    await repo.complete_rag_invocation(invocation_id, entry)

    outbox = backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION][invocation_id]
    assert private_chunk not in repr(outbox)
    assert outbox["event"]["chunks"][0]["preview"] == "Bounded safe preview"


async def test_direct_fallback_response_is_recorded_as_partial_not_clean_success():
    from api.direct_ticket_evaluation import direct_success_entry

    private_error = "private participant/provider detail must not persist"
    entry = direct_success_entry(
        route="knowledge_question",
        inquiry="What are the rollover timing rules?",
        topic="general",
        response={
            "answer": "A conservative fallback answer",
            "used_chunks": [],
            "metadata": {
                "error": private_error,
                "details": private_error,
                "retrieval_failure_kind": "timeout",
                "retrieval_retryable": True,
                "model": "test-model",
            },
        },
    )

    assert entry["execution_status"] == "partial"
    assert entry["degraded"] is True
    assert entry["participant_reply_safe"] is False
    assert entry["error"] == {
        "code": "DIRECT_RAG_DEGRADED",
        "retryable": True,
    }
    assert private_error not in repr(entry)
    durable_metadata = entry["result"]["knowledge_answer"]["metadata"]
    assert durable_metadata == {
        "retrieval_failure_kind": "timeout",
        "retrieval_retryable": True,
        "model": "test-model",
    }

    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    invocation_id = await repo.begin_direct_rag_invocation(
        request_id="00000000-0000-4000-8000-000000000003",
        ticket_id="TKT-1234",
        route="knowledge_question",
        inquiry="What are the rollover timing rules?",
        topic="general",
    )
    await repo.complete_rag_invocation(invocation_id, entry)

    outbox = backend._data[TICKET_EVALUATION_OUTBOX_COLLECTION][invocation_id]
    assert outbox["event"]["status"] == "partial"
    assert private_error not in repr(outbox)


@pytest.fixture
def producer_client(monkeypatch):
    from api.config import settings

    monkeypatch.setattr(settings, "APP_ROLE", "producer")
    monkeypatch.setattr(settings, "API_KEY", "test-api-key")
    monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", "disabled")
    monkeypatch.setattr(settings, "TICKET_EVALUATION_PUBLISH_ENABLED", False)

    mock_engine = Mock()
    mock_pinecone = Mock()
    mock_pinecone.get_index_stats.return_value = {"total_vectors": 0}
    mock_pinecone.query_chunks.return_value = []
    mock_router = Mock()

    with patch("api.main.validate_settings"), \
            patch("api.main.RAGEngine", return_value=mock_engine), \
            patch("api.main.PineconeUploader", return_value=mock_pinecone), \
            patch("api.main.InquiryRouterEngine", return_value=mock_router):
        from api.main import app

        with TestClient(app) as client:
            backend = InMemoryTicketJobBackend()
            client.app.state.ticket_repo = TicketJobRepository(backend)
            client.app.state.ticket_evaluation_publisher = SimpleNamespace(
                publish_execution=AsyncMock(return_value={
                    "evaluation_scanned": 1,
                    "evaluation_delivered": 1,
                    "evaluation_retried": 0,
                    "evaluation_rejected": 0,
                    "evaluation_errors": 0,
                })
            )
            yield client, backend


def _knowledge_result():
    return SimpleNamespace(
        answer="A rollover can preserve tax deferral.",
        key_points=["Keep the transfer direct."],
        source_articles=[],
        used_chunks=[],
        confidence_note="well_covered",
        metadata={"model": "test-model"},
    )


def test_ticket_associated_knowledge_call_journals_before_effect_and_fast_publishes(
    producer_client,
):
    client, backend = producer_client

    async def answer(*, question):
        assert question
        journals = backend._data.get(RAG_INVOCATIONS_COLLECTION, {})
        assert len(journals) == 1
        assert next(iter(journals.values()))["state"] == "started"
        assert not backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION)
        return _knowledge_result()

    client.app.state.rag_engine.ask_knowledge_question = AsyncMock(
        side_effect=answer
    )

    response = client.post(
        "/api/v1/knowledge-question",
        json={
            "question": "What are the rollover rules for this plan?",
            "ticket_id": "TKT-1234",
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 200
    outboxes = backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION, {})
    assert len(outboxes) == 1
    invocation_id, outbox = next(iter(outboxes.items()))
    assert outbox["event"]["ticket_id"] == "TKT-1234"
    assert outbox["event"]["answer"] == "A rollover can preserve tax deferral."
    client.app.state.ticket_evaluation_publisher.publish_execution.assert_awaited_once_with(
        invocation_id
    )


def test_ticket_associated_knowledge_call_replays_a_stable_idempotency_key(
    producer_client,
):
    client, backend = producer_client
    client.app.state.rag_engine.ask_knowledge_question = AsyncMock(
        return_value=_knowledge_result()
    )
    request = {
        "question": "What are the rollover rules for this plan?",
        "ticket_id": "TKT-1234",
    }
    headers = {
        "X-API-Key": "test-api-key",
        "Idempotency-Key": "n8n-kq-stable-event-0001",
    }

    first = client.post(
        "/api/v1/knowledge-question", json=request, headers=headers,
    )
    replay = client.post(
        "/api/v1/knowledge-question", json=request, headers=headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    client.app.state.rag_engine.ask_knowledge_question.assert_awaited_once()
    assert len(backend._data.get(RAG_INVOCATIONS_COLLECTION, {})) == 1
    assert len(backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION, {})) == 1


def test_the_initial_idempotent_response_matches_its_minimized_replay(
    producer_client,
):
    client, _backend = producer_client
    result = _knowledge_result()
    result.used_chunks = [{
        "chunk_id": "chunk-1",
        "score": 0.91,
        "chunk_type": "business_rules",
        "chunk_tier": "high",
        "article_id": "article-1",
        "article_title": "Rollover rules",
        "content_preview": "Bounded preview",
        "content": "retrieved content must not enter the replay ledger",
    }]
    result.metadata = {
        "model": "test-model",
        "error": "provider detail must not enter the replay ledger",
    }
    client.app.state.rag_engine.ask_knowledge_question = AsyncMock(
        return_value=result
    )
    request = {
        "question": "What are the rollover rules for this plan?",
        "ticket_id": "TKT-1234",
    }
    headers = {
        "X-API-Key": "test-api-key",
        "Idempotency-Key": "n8n-kq-stable-event-0002",
    }

    first = client.post(
        "/api/v1/knowledge-question", json=request, headers=headers,
    )
    replay = client.post(
        "/api/v1/knowledge-question", json=request, headers=headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["used_chunks"] == []
    assert first.json()["metadata"] == {"model": "test-model"}


def test_ticket_associated_knowledge_call_requires_api_client_auth(
    producer_client,
):
    client, backend = producer_client
    client.app.state.rag_engine.ask_knowledge_question = AsyncMock(
        return_value=_knowledge_result()
    )

    response = client.post(
        "/api/v1/knowledge-question",
        json={
            "question": "What are the rollover rules for this plan?",
            "ticket_id": "TKT-1234",
        },
    )

    assert response.status_code == 401
    client.app.state.rag_engine.ask_knowledge_question.assert_not_awaited()
    assert not backend._data.get(RAG_INVOCATIONS_COLLECTION)


def test_general_knowledge_call_without_ticket_remains_outside_ticket_ledger(
    producer_client,
):
    client, backend = producer_client
    client.app.state.rag_engine.ask_knowledge_question = AsyncMock(
        return_value=_knowledge_result()
    )

    response = client.post(
        "/api/v1/knowledge-question",
        json={"question": "What are the general rollover rules?"},
    )

    assert response.status_code == 200
    assert not backend._data.get(RAG_INVOCATIONS_COLLECTION)
    assert not backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION)
    client.app.state.ticket_evaluation_publisher.publish_execution.assert_not_awaited()


def test_ticket_associated_generate_response_creates_an_evaluation(
    producer_client,
):
    client, backend = producer_client
    engine_result = SimpleNamespace(
        decision="can_proceed",
        confidence=0.9,
        response={
            "outcome": "can_proceed",
            "outcome_reason": "The plan permits a direct rollover.",
            "response_to_participant": {
                "opening": "You can request a direct rollover.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [],
            "data_gaps": [],
        },
        source_articles=[],
        used_chunks=[],
        coverage_gaps=[],
        metadata={"model": "test-model"},
    )

    async def generate(**_kwargs):
        journals = backend._data.get(RAG_INVOCATIONS_COLLECTION, {})
        assert len(journals) == 1
        assert next(iter(journals.values()))["state"] == "started"
        return engine_result

    client.app.state.rag_engine.generate_response = AsyncMock(side_effect=generate)

    response = client.post(
        "/api/v1/generate-response",
        json={
            "inquiry": "How can I roll over this account?",
            "plan_type": "401(k)",
            "topic": "rollover",
            "collected_data": {},
            "ticket_id": "TKT-1234",
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 200
    outboxes = backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION, {})
    assert len(outboxes) == 1
    invocation_id, outbox = next(iter(outboxes.items()))
    assert outbox["event"]["route"] == "generate_response"
    assert "direct rollover" in outbox["event"]["answer"]
    client.app.state.ticket_evaluation_publisher.publish_execution.assert_awaited_once_with(
        invocation_id
    )


def test_direct_provider_failure_is_a_sanitized_durable_evaluation(
    producer_client,
):
    client, backend = producer_client
    private_error = "private participant content must not be persisted"
    client.app.state.rag_engine.ask_knowledge_question = AsyncMock(
        side_effect=RuntimeError(private_error)
    )

    response = client.post(
        "/api/v1/knowledge-question",
        json={
            "question": "What are the rollover rules for this plan?",
            "ticket_id": "TKT-1234",
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 500
    outboxes = backend._data.get(TICKET_EVALUATION_OUTBOX_COLLECTION, {})
    assert len(outboxes) == 1
    invocation_id, outbox = next(iter(outboxes.items()))
    assert outbox["event"]["status"] == "failed"
    assert outbox["event"]["error"] == {
        "code": "DIRECT_RAG_FAILED",
        "retryable": False,
    }
    assert private_error not in repr(outbox)
    client.app.state.ticket_evaluation_publisher.publish_execution.assert_awaited_once_with(
        invocation_id
    )
