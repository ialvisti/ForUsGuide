"""
Tests del fault injection SÓLO staging (plan Tarea 7 Paso 7a).

Cubre: firma HMAC válida/alterada, rechazo en producción, principal
incorrecto y cada punto de inyección.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from data_pipeline.staging_fault_injection import (
    FAULT_TEST_HEADER,
    FaultInjectionRejected,
    InjectedFault,
    accept_fault_plan_from_request,
    build_signed_fault_plan,
    maybe_raise,
    sign_fault_plan,
)

SECRET = "staging-only-fault-secret"


class TestFaultPlanSigning:

    def test_signed_plan_roundtrips_and_injects(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(InjectedFault) as exc:
            maybe_raise(plan, point="post_checkpoint", inquiry_index=0,
                        app_env="staging", principal_id="e2e", secret=SECRET)
        assert exc.value.point == "post_checkpoint"

    def test_tampered_signature_rejected(self):
        plan = build_signed_fault_plan(
            point="lease_lost", inquiry_index=1,
            principal_id="e2e", secret=SECRET)
        plan["signature"] = "0" * 64
        with pytest.raises(FaultInjectionRejected):
            maybe_raise(plan, point="lease_lost", inquiry_index=1,
                        app_env="staging", principal_id="e2e", secret=SECRET)

    def test_wrong_principal_rejected(self):
        plan = build_signed_fault_plan(
            point="timeout_reset", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            maybe_raise(plan, point="timeout_reset", inquiry_index=0,
                        app_env="staging", principal_id="attacker", secret=SECRET)

    def test_production_never_injects(self):
        plan = build_signed_fault_plan(
            point="dependency_down", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            maybe_raise(plan, point="dependency_down", inquiry_index=0,
                        app_env="production", principal_id="e2e", secret=SECRET)

    def test_non_matching_point_does_not_inject(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        # punto distinto: no lanza
        maybe_raise(plan, point="lease_lost", inquiry_index=0,
                    app_env="staging", principal_id="e2e", secret=SECRET)


class TestProducerHeaderAcceptance:

    def test_producer_rejects_header_in_production(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            accept_fault_plan_from_request(
                app_env="production", header_value=json.dumps(plan),
                principal_id="e2e", secret=SECRET)

    def test_producer_accepts_signed_header_in_staging(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        accepted = accept_fault_plan_from_request(
            app_env="staging", header_value=json.dumps(plan),
            principal_id="e2e", secret=SECRET)
        assert accepted["point"] == "post_checkpoint"

    def test_producer_no_header_returns_none(self):
        assert accept_fault_plan_from_request(
            app_env="staging", header_value=None,
            principal_id="e2e", secret=SECRET) is None

    def test_producer_rejects_without_secret(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        with pytest.raises(FaultInjectionRejected):
            accept_fault_plan_from_request(
                app_env="staging", header_value=json.dumps(plan),
                principal_id="e2e", secret="")

    def test_header_name_is_stable(self):
        assert FAULT_TEST_HEADER == "X-ForUs-Fault-Plan"

    def test_signed_plan_with_extra_fields_is_rejected_not_sanitized_late(self):
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        plan["unexpected"] = {"raw_secret": "must-not-be-persisted"}
        plan["signature"] = sign_fault_plan(plan, SECRET)

        with pytest.raises(FaultInjectionRejected):
            accept_fault_plan_from_request(
                app_env="staging", header_value=json.dumps(plan),
                principal_id="e2e", secret=SECRET)


class _AllowValidator:
    async def authorize(self, *, tenant_id, participant_id, plan_id):
        from api.participant_plan import AuthorizedParticipantPlan

        return AuthorizedParticipantPlan(
            tenant_id=tenant_id,
            participant_id=participant_id,
            plan_id=plan_id,
            record_keeper=None,
        )


class _CapturingQueue:
    def __init__(self):
        self.enqueued = []

    async def estimated_queue_delay_s(self):
        return 0.0

    async def ensure_enqueued(self, job_id, generation=0):
        self.enqueued.append((job_id, generation))
        return f"fault-test/{job_id}-g{generation}"


class _FaultTestOrchestrator:
    def __init__(self):
        self.extract_calls = 0
        self.handle_calls = 0

    async def extract_inquiries(self, _request):
        from data_pipeline.ticket_orchestrator import ExtractedInquiry

        self.extract_calls += 1
        return [ExtractedInquiry("q", "LT Trust", "401(k)", "general")]

    async def classify(self, _inquiry):
        return SimpleNamespace(
            route="needs_more_info", confidence=0.9,
            reasoning="test", user_message="More details?",
        )

    async def handle_inquiry(
        self, ext, _request, *, total_inquiries, classification=None,
    ):
        from data_pipeline.ticket_orchestrator import InquiryOutcome

        del total_inquiries, classification
        self.handle_calls += 1
        return InquiryOutcome(
            inquiry=ext.inquiry,
            topic=ext.topic,
            route="needs_more_info",
            needs_more_info_message="More details?",
        )


def _producer_body():
    from api.models import HandleTicketRequest

    return HandleTicketRequest.model_validate({
        "participant_id": "synthetic-participant",
        "plan_id": "synthetic-plan",
        "company_name": "Synthetic Company",
        "company_status": "Ongoing",
        "ticket": {
            "username": "Synthetic User",
            "user_email": "synthetic@example.test",
            "email_subject": "Plan question",
            "email_body": "How does this work?",
        },
        "record_keeper": "caller-owned-value-is-replaced",
    })


def _producer_request(plan):
    from api.rate_limit import FixedWindowRateLimiter

    headers = []
    if plan is not None:
        headers.append((
            FAULT_TEST_HEADER.lower().encode("ascii"),
            json.dumps(plan).encode("utf-8"),
        ))
    app = SimpleNamespace(state=SimpleNamespace(
        participant_plan_validator=_AllowValidator(),
        ticket_queue=_CapturingQueue(),
        ticket_rate_limiter=FixedWindowRateLimiter(),
    ))
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v2/handle-ticket",
        "headers": headers,
        "app": app,
    })
    request.state.principal_id = "e2e"
    request.state.tenant_id = "tenant-e2e"
    return request


async def _accept_fault_job(monkeypatch, *, point, app_env="staging",
                            tamper_signature=False):
    from api import main as main_module
    from api.config import settings
    from data_pipeline.ticket_job_repository import (
        InMemoryTicketJobBackend,
        TicketJobRepository,
    )

    monkeypatch.setattr(settings, "APP_ENV", app_env)
    monkeypatch.setattr(settings, "TICKET_FAULT_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", "full")
    plan = build_signed_fault_plan(
        point=point, inquiry_index=0, principal_id="e2e", secret=SECRET)
    if tamper_signature:
        plan["signature"] = "0" * 64
    backend = InMemoryTicketJobBackend()
    repo = TicketJobRepository(backend)
    request = _producer_request(plan)
    record, replayed = await main_module._accept_ticket_job(
        _producer_body(), request, repo, api_version="v2",
        allow_body_idem=False,
    )
    assert replayed is False
    return repo, backend, request, record, plan


class TestFaultInjectionEndToEnd:

    async def test_producer_persists_only_structured_plan_in_payload(
        self, monkeypatch,
    ):
        from data_pipeline.ticket_job_repository import (
            JOBS_COLLECTION,
            PAYLOADS_COLLECTION,
        )

        repo, backend, _request, record, plan = await _accept_fault_job(
            monkeypatch, point="post_checkpoint")

        persisted = await repo.get(record.job_id)
        control = await backend.get_doc(JOBS_COLLECTION, record.job_id)
        payload = await backend.get_doc(PAYLOADS_COLLECTION, record.job_id)
        assert persisted.fault_plan == plan
        assert "fault_plan" not in control
        assert payload["fault_plan"] == plan
        assert set(payload["fault_plan"]) == {
            "point", "inquiry_index", "principal_id", "env", "signature",
        }

    async def test_producer_rejects_invalid_signature_before_creating_job(
        self, monkeypatch,
    ):
        with pytest.raises(HTTPException) as exc:
            await _accept_fault_job(
                monkeypatch, point="post_checkpoint", tamper_signature=True)

        assert exc.value.status_code == 403
        assert exc.value.detail == {"code": "FAULT_INJECTION_REJECTED"}

    async def test_producer_rejects_fault_header_in_production(
        self, monkeypatch,
    ):
        with pytest.raises(HTTPException) as exc:
            await _accept_fault_job(
                monkeypatch, point="post_checkpoint", app_env="production")

        assert exc.value.status_code == 403
        assert exc.value.detail == {"code": "FAULT_INJECTION_REJECTED"}

    async def test_producer_rejects_signed_header_without_authenticated_state(
        self, monkeypatch,
    ):
        from api import main as main_module
        from api.config import settings
        from data_pipeline.ticket_job_repository import (
            InMemoryTicketJobBackend,
            TicketJobRepository,
        )

        monkeypatch.setattr(settings, "APP_ENV", "staging")
        monkeypatch.setattr(settings, "TICKET_FAULT_SIGNING_SECRET", SECRET)
        monkeypatch.setattr(settings, "TICKET_HANDLER_MODE", "full")
        plan = build_signed_fault_plan(
            point="post_checkpoint", inquiry_index=0,
            principal_id="e2e", secret=SECRET)
        request = _producer_request(plan)
        request.state.principal_id = None
        repo = TicketJobRepository(InMemoryTicketJobBackend())

        with pytest.raises(HTTPException) as exc:
            await main_module._accept_ticket_job(
                _producer_body(), request, repo, api_version="v2",
                allow_body_idem=False,
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == {"code": "FAULT_INJECTION_REJECTED"}

    @pytest.mark.parametrize(
        ("point", "expected_state", "expected_status", "expected_calls"),
        (
            ("timeout_reset", "timeout", "timeout", 0),
            ("dependency_down", "failed", "failed", 0),
            ("lease_lost", "running", None, 0),
        ),
    )
    async def test_signed_plan_flows_producer_to_worker_checkpoint(
        self, monkeypatch, point, expected_state, expected_status,
        expected_calls,
    ):
        from api.ticket_worker import run_ticket_job

        repo, _backend, request, record, _plan = await _accept_fault_job(
            monkeypatch, point=point)
        orchestrator = _FaultTestOrchestrator()
        request.app.state.ticket_repo = repo
        request.app.state.ticket_orchestrator_factory = lambda: orchestrator
        request.app.state.execution_logger = None

        final = await run_ticket_job(
            request.app, record.job_id, worker_id="fault-worker")
        persisted = await repo.get(record.job_id)

        assert persisted.state.value == expected_state
        assert orchestrator.handle_calls == expected_calls
        if expected_status is None:
            assert final is None
            assert persisted.per_inquiry_status == []
        else:
            assert final is not None
            assert persisted.per_inquiry_status[0]["execution_status"] == \
                expected_status

    async def test_post_checkpoint_crash_preserves_checkpoint_for_retry(
        self, monkeypatch,
    ):
        from api.ticket_worker import run_ticket_job

        repo, _backend, request, record, _plan = await _accept_fault_job(
            monkeypatch, point="post_checkpoint")
        orchestrator = _FaultTestOrchestrator()
        request.app.state.ticket_repo = repo
        request.app.state.ticket_orchestrator_factory = lambda: orchestrator
        request.app.state.execution_logger = None

        with pytest.raises(InjectedFault) as exc:
            await run_ticket_job(
                request.app, record.job_id, worker_id="fault-worker")

        persisted = await repo.get(record.job_id)
        assert exc.value.point == "post_checkpoint"
        assert persisted.state.value == "running"
        assert persisted.per_inquiry_status[0]["execution_status"] == "succeeded"
        assert orchestrator.handle_calls == 1

    async def test_worker_rejects_persisted_plan_in_production_before_effects(
        self, monkeypatch,
    ):
        from api.config import settings
        from api.ticket_worker import run_ticket_job

        repo, _backend, request, record, _plan = await _accept_fault_job(
            monkeypatch, point="dependency_down")
        orchestrator = _FaultTestOrchestrator()
        request.app.state.ticket_repo = repo
        request.app.state.ticket_orchestrator_factory = lambda: orchestrator
        request.app.state.execution_logger = None
        monkeypatch.setattr(settings, "APP_ENV", "production")

        final = await run_ticket_job(
            request.app, record.job_id, worker_id="production-worker")

        assert final is not None and final.state.value == "failed"
        assert orchestrator.extract_calls == 0
        assert orchestrator.handle_calls == 0
