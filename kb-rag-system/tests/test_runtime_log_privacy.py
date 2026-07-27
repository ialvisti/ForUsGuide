"""Privacy boundary for logs emitted by API and ticket runtime modules."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.middleware import log_requests
from data_pipeline.inquiry_router import _safe_parse_classifier_json
from data_pipeline.json_parsing import parse_json_array, parse_json_object


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = (ROOT / "api", ROOT / "data_pipeline")
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
RAW_LOG_FRAGMENTS = (
    "content[:",
    "question[:",
    "outcome_reason",
    "filter_dict",
    "raw_chunk.get",
    "job_id",
    "participant_id",
    "plan_id",
    "record_keeper",
    "Topic: {topic}",
    "article_id={article_id}",
    "article_id={best_article_id}",
    "{primary_article_id}",
    "promoted['id']",
    "primary={primary_article_id}",
    "top={top_aid}",
    "{coverage_gaps}",
    "list(parsed.keys())",
    "q.get(\"question\")",
    "str(q)",
    "best['metadata'].get('article_id')",
    "best['metadata'].get('topic')",
    "{resolved_topics}",
    "{topic_label}",
)


def _logger_calls(path: Path) -> list[tuple[int, str, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        rendered = ast.get_source_segment(source, node) or ""
        calls.append((node.lineno, node.func.attr, rendered))
    return calls


def test_runtime_logs_never_serialize_tracebacks_raw_errors_or_pii_fields() -> None:
    violations: list[str] = []
    for directory in RUNTIME_DIRS:
        for path in directory.rglob("*.py"):
            for line, method, rendered in _logger_calls(path):
                compact = rendered.replace(" ", "")
                if method == "exception":
                    violations.append(f"{path.relative_to(ROOT)}:{line}: traceback")
                if any(fragment in compact for fragment in ("str(e)", "str(exc)")):
                    violations.append(f"{path.relative_to(ROOT)}:{line}: raw exception")
                if any(fragment in rendered for fragment in RAW_LOG_FRAGMENTS):
                    violations.append(f"{path.relative_to(ROOT)}:{line}: raw field")
                # Raw f-string exception interpolation. `type(exc).__name__`
                # remains allowed because it is a stable technical category.
                if "{e}" in rendered or "{exc}" in rendered:
                    violations.append(f"{path.relative_to(ROOT)}:{line}: raw exception")
    assert violations == []


@pytest.mark.asyncio
async def test_request_log_uses_route_template_and_omits_client_ip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_job = "0123456789abcdef0123456789abcdef"
    raw_ip = "203.0.113.77"
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v2/ticket-jobs/{raw_job}",
            "headers": [],
            "client": (raw_ip, 1234),
        }
    )

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200)

    with caplog.at_level(logging.INFO, logger="api.middleware"):
        await log_requests(request, call_next)

    assert raw_job not in caplog.text
    assert raw_ip not in caplog.text
    assert "/api/v2/ticket-jobs/{ticket_job_id}" in caplog.text


def test_llm_parse_failures_never_log_or_reflect_raw_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "Jane Doe jane@example.com 123 Main Street participant-158948"

    with caplog.at_level(logging.WARNING):
        assert parse_json_object(sentinel) is None
        assert parse_json_array(sentinel) is None
        parsed, parse_ok = _safe_parse_classifier_json(
            '{"route":"' + sentinel + '","confidence":1}'
        )

    assert parse_ok is False
    assert parsed["route"] == "needs_more_info"
    assert parsed["reasoning"] == "Classifier output invalid"
    assert sentinel not in caplog.text
    assert sentinel not in repr(parsed)


@pytest.mark.asyncio
async def test_worker_rag_path_never_logs_ticket_topic_email_or_participant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api.ticket_worker import run_ticket_job
    from data_pipeline.rag_engine import RAGEngine
    from data_pipeline.ticket_job_models import (
        fingerprint_request,
        new_job_record,
    )
    from data_pipeline.ticket_job_repository import (
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )
    from data_pipeline.ticket_orchestrator import ExtractedInquiry, InquiryOutcome

    sentinel = "Jane.Doe@example.com participant-158948 123 Main Street"
    engine = RAGEngine(
        llm_router=SimpleNamespace(), pinecone_uploader=SimpleNamespace()
    )
    engine._decompose_question = AsyncMock(return_value=[sentinel])
    engine._detect_advisory_concepts = lambda **_kwargs: {}
    engine._build_retrieval_profile = lambda **_kwargs: {}
    engine._expand_queries_with_advisory_concepts = lambda **_kwargs: [sentinel]
    engine._search_for_response_parallel_cascade = AsyncMock(
        return_value=([], {})
    )

    class _Orchestrator:
        async def extract_inquiries(self, _request):
            return [ExtractedInquiry(sentinel, None, "401(k)", sentinel)]

        async def classify(self, _inquiry):
            return SimpleNamespace(
                route="knowledge_question",
                confidence=0.9,
                reasoning="covered",
                user_message=None,
            )

        async def handle_inquiry(
            self, ext, _request, *, total_inquiries, classification=None,
        ):
            del total_inquiries, classification
            await engine.generate_response(
                inquiry=ext.inquiry,
                record_keeper=ext.record_keeper,
                plan_type=ext.plan_type,
                topic=ext.topic,
                collected_data={},
                max_response_tokens=512,
            )
            return InquiryOutcome(
                inquiry=ext.inquiry,
                topic=ext.topic,
                route="knowledge_question",
                knowledge_result=SimpleNamespace(
                    answer="Safe generic answer",
                    key_points=[],
                    source_articles=[],
                    used_chunks=[],
                    confidence_note="well_covered",
                    metadata={},
                ),
            )

    payload = {
        "participant_id": "158948",
        "plan_id": "580",
        "company_name": "Synthetic",
        "company_status": "Ongoing",
        "ticket": {
            "username": "Jane Doe",
            "user_email": "Jane.Doe@example.com",
            "email_subject": sentinel,
            "email_body": sentinel,
        },
    }
    fingerprint = fingerprint_request(payload)
    repo = TicketJobRepository(InMemoryTicketJobBackend())
    record, _ = await repo.create_or_get(
        principal_id="privacy-test",
        idempotency_key=None,
        request_fingerprint=fingerprint,
        candidate=new_job_record(
            principal_id="privacy-test",
            request_fingerprint=fingerprint,
            request_payload=payload,
            mode="full",
        ),
    )
    app = SimpleNamespace(state=SimpleNamespace(
        ticket_repo=repo,
        ticket_orchestrator_factory=_Orchestrator,
        execution_logger=None,
    ))

    with caplog.at_level(logging.INFO):
        final = await run_ticket_job(app, record.job_id)

    assert final is not None
    assert sentinel not in caplog.text
    assert "Jane.Doe@example.com" not in caplog.text
    assert "participant-158948" not in caplog.text


def test_participant_plan_factory_error_never_contains_raw_source() -> None:
    from api.participant_plan import build_validator_from_settings

    secret_source = "https://user:secret@internal.example.test/directory"
    settings = SimpleNamespace(PARTICIPANT_PLAN_SOURCE=secret_source)

    with pytest.raises(ValueError) as caught:
        build_validator_from_settings(settings)

    assert secret_source not in str(caught.value)
    assert "user:secret" not in str(caught.value)
