"""
Tests del modo shadow y del fallback de rollout (Task 10, HT-11).

Invariantes:
- shadow clasifica siempre; con sampling ejecuta el pipeline REAL pero jamás
  expone su respuesta (sólo resumen sanitizado sin texto).
- shadow y coerciones de modo nunca son publicables: next_action=use_legacy.
- un error técnico/gating no se disfraza de resultado de negocio.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from data_pipeline.ticket_job_models import (
    fingerprint_request,
    new_job_record,
)
from data_pipeline.ticket_job_repository import (
    InMemoryTicketJobBackend,
    TicketJobRepository,
)
from data_pipeline.ticket_orchestrator import ExtractedInquiry, InquiryOutcome


def _body():
    return dict(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing",
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": "401k", "email_body": "quiero retirar mi 401k"},
    )


class RecordingOrch:
    """Orchestrator fake que registra si el pipeline real se ejecutó."""

    def __init__(self, route="generate_response"):
        self.route = route
        self.handled = 0

    async def extract_inquiries(self, req):
        return [ExtractedInquiry("cash out 401k", "LT Trust", "401(k)", "rollover")]

    async def classify(self, inquiry):
        return SimpleNamespace(route=self.route, confidence=0.9,
                               reasoning="r", user_message=None)

    async def handle_inquiry(self, ext, req, *, total_inquiries, classification=None):
        self.handled += 1
        return InquiryOutcome(
            inquiry=ext.inquiry, topic=ext.topic, route="generate_response",
            scrape_status="ok",
            generate_result=SimpleNamespace(
                decision="can_proceed", confidence=0.8,
                response={"outcome": "can_proceed"}, source_articles=[],
                used_chunks=[], coverage_gaps=[], metadata={}),
        )


def _app(repo, orch):
    return SimpleNamespace(state=SimpleNamespace(
        ticket_repo=repo, ticket_orchestrator_factory=lambda: orch,
        execution_logger=None,
    ))


async def _seed(repo, mode):
    payload = _body()
    fp = fingerprint_request(payload)
    rec, _ = await repo.create_or_get(
        principal_id="default", idempotency_key=None, request_fingerprint=fp,
        candidate=new_job_record(principal_id="default", request_fingerprint=fp,
                                 mode=mode, request_payload=payload),
    )
    return rec


class TestShadowMode:

    async def test_shadow_unsampled_classifies_only(self, monkeypatch):
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        monkeypatch.setattr(app_settings, "TICKET_SHADOW_SAMPLE_RATE", 0.0)

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        orch = RecordingOrch()
        rec = await _seed(repo, "shadow")

        final = await run_ticket_job(_app(repo, orch), rec.job_id)

        assert orch.handled == 0, "sin sampling el pipeline real no debe correr"
        assert final.next_action.value == "use_legacy"
        meta = final.public_result["metadata"]
        assert meta["fallback"] is True
        assert meta["shadow_sampled"] is False
        assert meta["shadow_routes"] == ["generate_response"]

    async def test_shadow_sampled_runs_pipeline_but_never_publishes(self, monkeypatch):
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        monkeypatch.setattr(app_settings, "TICKET_SHADOW_SAMPLE_RATE", 1.0)

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        orch = RecordingOrch()
        rec = await _seed(repo, "shadow")

        final = await run_ticket_job(_app(repo, orch), rec.job_id)

        assert orch.handled == 1, "con rate=1.0 el pipeline completo debe correr"
        assert final.next_action.value == "use_legacy"
        meta = final.public_result["metadata"]
        assert meta["shadow_sampled"] is True
        # resumen SANITIZADO: rutas/estados/decisión, nunca texto del ticket
        summary = meta["shadow_summary"]
        assert summary and summary[0]["decision"] == "can_proceed"
        assert "cash out" not in str(summary)
        # ninguna entry es publicable
        assert all(e["participant_reply_safe"] is False
                   for e in final.per_inquiry_status)

    async def test_shadow_pipeline_failure_does_not_fail_job(self, monkeypatch):
        from api.config import settings as app_settings
        from api.ticket_worker import run_ticket_job
        monkeypatch.setattr(app_settings, "TICKET_SHADOW_SAMPLE_RATE", 1.0)

        class ExplodingOrch(RecordingOrch):
            async def handle_inquiry(self, *a, **kw):
                raise RuntimeError("boom")

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        rec = await _seed(repo, "shadow")

        final = await run_ticket_job(_app(repo, ExplodingOrch()), rec.job_id)

        assert final.state.value == "succeeded"        # shadow no rompe el job
        summary = final.public_result["metadata"]["shadow_summary"]
        assert summary[0]["error"] == "shadow_pipeline_failed"
        assert final.next_action.value == "use_legacy"


class TestKnowledgeOnlyCoercion:

    async def test_coerced_gr_goes_to_legacy_not_participant(self):
        """knowledge_only + ruta GR: el NMI de gating no es publicable; el
        job entero se resuelve por legacy (HT-11)."""
        from api.ticket_worker import run_ticket_job

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        orch = RecordingOrch(route="generate_response")
        rec = await _seed(repo, "knowledge_only")

        final = await run_ticket_job(_app(repo, orch), rec.job_id)

        assert orch.handled == 0                      # GR coercido: no corre
        assert final.state.value == "succeeded"
        assert final.next_action.value == "use_legacy"
        entry = final.per_inquiry_status[0]
        assert entry["coerced_by_mode"] is True
        assert entry["participant_reply_safe"] is False

    async def test_kq_completes_normally_under_knowledge_only(self):
        from api.ticket_worker import run_ticket_job

        class KQOrch(RecordingOrch):
            def __init__(self):
                super().__init__(route="knowledge_question")

            async def handle_inquiry(self, ext, req, *, total_inquiries,
                                     classification=None):
                self.handled += 1
                return InquiryOutcome(
                    inquiry=ext.inquiry, topic=ext.topic,
                    route="knowledge_question",
                    knowledge_result=SimpleNamespace(
                        answer="A", key_points=[], source_articles=[],
                        used_chunks=[], confidence_note="ok", metadata={}),
                )

        repo = TicketJobRepository(InMemoryTicketJobBackend())
        orch = KQOrch()
        rec = await _seed(repo, "knowledge_only")

        final = await run_ticket_job(_app(repo, orch), rec.job_id)

        assert orch.handled == 1
        assert final.state.value == "succeeded"
        assert final.next_action.value == "send_participant_reply"
        assert final.per_inquiry_status[0]["participant_reply_safe"] is True
