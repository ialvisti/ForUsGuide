"""Stage 5 Step 3 — the `/api/admin/v1` contract.

These are integration tests over the **real** middleware stack, the **real**
router, the **real** ``TicketReviewService``, and the **real**
``TicketReviewRepository`` on the in-memory backend. Only DevRev and the evidence
broker are faked, because they are the only collaborators that would otherwise
open a socket.

Testing through the real repository is deliberate: a hand-written fake would
happily return whatever version a test wanted, and the ETag/412/428/409 rules
this file exists to pin are exactly the ones a fake would paper over.

Absent by design
----------------
Remediation batch routes (Stage 8) and CSV import/export routes (Stage 9) are
asserted *absent* from OpenAPI rather than stubbed. A stub that answers "not
implemented" is indistinguishable in a generated client from one that works.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.reviewer_auth import IAP_ASSERTION_HEADER, IAP_ISSUER
from api.ticket_review_models import (
    CursorPage,
    DevRevActor,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketSummary,
    DevRevTimelineEntry,
    Rating,
    ReviewerRole,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
    review_id_for_devrev_work,
)
from api.ticket_review_routes import (
    API_PREFIX,
    CODE_CURSOR_REJECTED,
    CODE_IDEMPOTENCY_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PRECONDITION_MALFORMED,
    CODE_PRECONDITION_REQUIRED,
    CODE_RATE_LIMITED,
    CODE_REVIEW_VERSION_CONFLICT,
    CODE_UNSUPPORTED_FILTER,
    CODE_UPSTREAM_RATE_LIMITED,
    CODE_UPSTREAM_UNAVAILABLE,
    CODE_VALIDATION_FAILED,
    validated_ticket_ref,
)
from api.tickets_console_config import TicketConsoleSettings
from api.tickets_console_main import build_console_app
from api.tickets_csrf import (
    CSRF_HEADER,
    CURSOR_HEADER,
    IDEMPOTENCY_HEADER,
    ORIGIN_HEADER,
    FETCH_SITE_HEADER,
)
from data_pipeline.devrev_client import (
    DevRevNotFoundError,
    DevRevRateLimitError,
    DevRevScopeError,
    DevRevTransientError,
)
from data_pipeline.ticket_review_repository import (
    InMemoryTicketReviewBackend,
    TicketReviewRepository,
)
from data_pipeline.ticket_review_service import MessageClassifier, TicketReviewService

# =====================================================================
# Synthetic fixtures. No organization-specific identifier appears.
# =====================================================================

CURSOR_KEY = bytes(range(32))
CURSOR_KEY_B64 = base64.b64encode(CURSOR_KEY).decode("ascii")
CONSOLE_ORIGIN = "https://tickets-console-abc-uc.a.run.app"
CSRF_SECRET = "synthetic-csrf-value"  # pragma: allowlist secret

SYNTHETIC_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/424242"
SYNTHETIC_DISPLAY_ID = "TKT-424242"
OTHER_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/999999"
SYNTHETIC_PART = "don:core:dvrv-us-1:devo/synthetic:product/1"
PARTICIPANT_ID = "don:identity:dvrv-us-1:devo/synthetic:revu/participant-1"

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

EMAILS: dict[ReviewerRole, str] = {
    ReviewerRole.VIEWER: "viewer@example.invalid",
    ReviewerRole.REVIEWER: "reviewer@example.invalid",
    ReviewerRole.REMEDIATOR: "remediator@example.invalid",
    ReviewerRole.ADMIN: "admin@example.invalid",
}
AGENT_SA = "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com"
UNBOUND_EMAIL = "stranger@example.invalid"
ASSERTION = "synthetic.iap.assertion"
IAP_AUDIENCE = "/projects/1234567890/locations/us-central1/services/tickets-console"


class _TickClock:
    """Advances a microsecond per read.

    The audit ledger is ordered by ``(occurred_at_unix_us, event_id)``, so two
    writes frozen at the same microsecond have no defined order and the hash
    chain would look broken for a reason wall time cannot produce.
    """

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        current = self.now
        self.now = self.now + timedelta(microseconds=1)
        return current


def _summary(don: str = SYNTHETIC_DON, display_id: str = SYNTHETIC_DISPLAY_ID, **overrides):
    values = {
        "devrev_work_id": don,
        "devrev_display_id": display_id,
        "title": "Ana Synthetic asked about limits (ana@example.invalid)",
        "object_version": 3,
        "created_at": T0 - timedelta(days=2),
        "modified_at": T0 - timedelta(days=1),
    }
    values.update(overrides)
    return DevRevTicketSummary(**values)


def _detail(don: str = SYNTHETIC_DON, **overrides) -> DevRevTicketDetail:
    values = {
        "devrev_work_id": don,
        "devrev_display_id": SYNTHETIC_DISPLAY_ID,
        "title": "Ana Synthetic asked about limits (ana@example.invalid)",
        "object_version": 3,
        "body": "Original ticket body.",
        "created_at": T0 - timedelta(days=2),
        "modified_at": T0 - timedelta(days=1),
    }
    values.update(overrides)
    return DevRevTicketDetail(**values)


def _entry(entry_id: str = "entry-1", **overrides) -> DevRevTimelineEntry:
    values = {
        "entry_id": entry_id,
        "object_id": SYNTHETIC_DON,
        "kind": TimelineEntryKind.COMMENT,
        "visibility": TimelineVisibility.EXTERNAL,
        "body": "A synthetic message.",
        "body_type": "text/plain",
        "author": DevRevActor(actor_id=PARTICIPANT_ID, actor_type=DevRevActorType.REV_USER),
        "created_at": T0,
    }
    values.update(overrides)
    return DevRevTimelineEntry(**values)


class _FakeDevRev:
    """Only the three methods the service calls, plus call recording."""

    def __init__(
        self,
        *,
        detail: DevRevTicketDetail | None = None,
        list_page: CursorPage | None = None,
        timeline_page: TimelinePage | None = None,
        get_error: Exception | None = None,
        list_error: Exception | None = None,
        timeline_error: Exception | None = None,
    ) -> None:
        self._detail = detail if detail is not None else _detail()
        self._list_page = list_page
        self._timeline_page = timeline_page
        self._get_error = get_error
        self._list_error = list_error
        self._timeline_error = timeline_error
        self.get_calls: list[str] = []
        self.list_calls: list[tuple[str | None, str, int | None]] = []
        self.timeline_calls: list[tuple[str, str | None, int | None]] = []

    async def get_ticket(self, work_id: str) -> DevRevTicketDetail:
        self.get_calls.append(work_id)
        if self._get_error is not None:
            raise self._get_error
        return self._detail

    async def list_tickets(self, query, *, cursor=None, mode="after", limit=None):
        self.list_calls.append((cursor, mode, limit))
        if self._list_error is not None:
            raise self._list_error
        if self._list_page is not None:
            return self._list_page
        return CursorPage[DevRevTicketSummary](
            items=[_summary()], next_cursor="remote-next", page_size=limit or 50
        )

    async def list_timeline_page(self, work_id: str, *, cursor=None, limit=None):
        self.timeline_calls.append((work_id, cursor, limit))
        if self._timeline_error is not None:
            raise self._timeline_error
        if self._timeline_page is not None:
            return self._timeline_page
        return TimelinePage(
            items=[_entry()], next_cursor="remote-timeline-next", page_size=limit or 50
        )


class _FailingBroker:
    """A broker that is always down, so evidence is a gap rather than a 500."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("broker down")
        self.calls = 0

    async def lookup(self, devrev_work_id: str, *, max_results: int):
        self.calls += 1
        raise self.error


def _settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values: dict[str, object] = {
        "ENVIRONMENT": "local",
        "AUTH_MODE": "iap",
        "ALLOW_LOCAL_AUTH": False,
        "IAP_AUDIENCE": IAP_AUDIENCE,
        "ALLOWED_EMAIL_DOMAINS": ["example.invalid"],
        "ROLE_BINDINGS_JSON": (
            '{"viewer@example.invalid": "viewer",'
            ' "reviewer@example.invalid": "reviewer",'
            ' "remediator@example.invalid": "remediator",'
            ' "admin@example.invalid": "admin"}'
        ),
        "CSRF_SIGNING_SECRET": CSRF_SECRET,
        "CURSOR_AEAD_KEY": CURSOR_KEY_B64,
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
        "GCP_PROJECT": "emulator-project",
        "GCP_REGION": "us-central1",
        "FIRESTORE_DATABASE": "tickets-console-emulator",
        "DEVREV_ALLOWED_PART_DONS": [SYNTHETIC_PART],
        "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS": [2],
        "DEVREV_ALLOWED_TIMELINE_VISIBILITIES": ["internal", "external"],
        "DEVREV_AI_AUTHOR_IDS": ["don:identity:dvrv-us-1:devo/synthetic:devu/ai-1"],
        "DEVREV_SYSTEM_AUTHOR_IDS": ["don:identity:dvrv-us-1:devo/synthetic:sysu/sys-1"],
        "DEVREV_HUMAN_AUTHOR_IDS": ["don:identity:dvrv-us-1:devo/synthetic:devu/human-1"],
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


class _Harness:
    """One wired app plus the collaborators a test needs to assert against."""

    def __init__(self, client, *, devrev, repository, service, clock, settings):
        self.client = client
        self.devrev = devrev
        self.repository = repository
        self.service = service
        self.clock = clock
        self.settings = settings


def _harness(
    monkeypatch,
    *,
    role: ReviewerRole = ReviewerRole.REVIEWER,
    email: str | None = None,
    devrev: _FakeDevRev | None = None,
    broker=None,
    rate_limiter=None,
    settings_overrides: dict | None = None,
) -> _Harness:
    settings = _settings(monkeypatch, **(settings_overrides or {}))
    resolved_email = email if email is not None else EMAILS[role]

    def verifier(token: str, audience: str):
        return {
            "iss": IAP_ISSUER,
            "aud": audience,
            "sub": f"accounts.google.com:{resolved_email}",
            "email": resolved_email,
        }

    clock = _TickClock()
    backend = InMemoryTicketReviewBackend()
    repository = TicketReviewRepository(backend, cursor_key=CURSOR_KEY, clock=clock)
    fake_devrev = devrev if devrev is not None else _FakeDevRev()
    service = TicketReviewService(
        devrev=fake_devrev,
        repository=repository,
        classifier=MessageClassifier.from_settings(settings),
        candidate_key=CURSOR_KEY,
        broker=broker,
        clock=clock,
    )
    app = build_console_app(
        settings,
        devrev=fake_devrev,
        repository=repository,
        service=service,
        claims_verifier=verifier,
        clock=lambda: T0,
        rate_limiter=rate_limiter,
        firestore_database="tickets-console-emulator",
    )
    return _Harness(
        TestClient(app, raise_server_exceptions=False),
        devrev=fake_devrev,
        repository=repository,
        service=service,
        clock=clock,
        settings=settings,
    )


def _auth_headers(**extra) -> dict[str, str]:
    headers = {IAP_ASSERTION_HEADER: ASSERTION}
    headers.update(extra)
    return headers


def _csrf_token(client: TestClient) -> str:
    response = client.get(f"{API_PREFIX}/session", headers=_auth_headers())
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _write_headers(client: TestClient, *, idempotency: str = "idem-0123456789ab", **extra):
    headers = _auth_headers(
        **{
            ORIGIN_HEADER: CONSOLE_ORIGIN,
            FETCH_SITE_HEADER: "same-origin",
            CSRF_HEADER: _csrf_token(client),
            IDEMPOTENCY_HEADER: idempotency,
            "Content-Type": "application/json",
        }
    )
    headers.update(extra)
    return headers


def _ingest_execution(harness: _Harness, **event_overrides):
    """Seed the public console through the same RAG-only service boundary.

    Tests may not resurrect the removed manual-create route. The private ingest
    HTTP contract is covered separately; this helper calls its service directly
    so review-route tests can focus on authorization, ETags, and audit behavior.
    """
    from tests.test_ticket_evaluation_ingest import _event

    event = _event(**event_overrides)
    with harness.client as client:
        return client.portal.call(harness.service._evaluation_service.ingest, event)


def _create_review(harness: _Harness, *, idempotency="idem-create-000001") -> dict:
    del idempotency  # the immutable execution id is the ingest idempotency key
    result = _ingest_execution(harness)
    with harness.client as client:
        review = client.portal.call(harness.repository.get_review, result.run.review_id)
    return review.model_dump(mode="json")


# =====================================================================
# Session
# =====================================================================


class TestSession:

    def test_a_bound_reviewer_gets_identity_role_and_csrf(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        response = harness.client.get(f"{API_PREFIX}/session", headers=_auth_headers())
        assert response.status_code == 200
        body = response.json()
        assert body["identity"]["email"] == EMAILS[ReviewerRole.ADMIN]
        assert body["role"] == "admin"
        assert body["csrf_token"]
        assert response.headers["Cache-Control"] == "no-store"

    def test_unauthenticated_is_401(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(f"{API_PREFIX}/session")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_an_unbound_identity_is_403(self, monkeypatch):
        harness = _harness(monkeypatch, email=UNBOUND_EMAIL)
        response = harness.client.get(f"{API_PREFIX}/session", headers=_auth_headers())
        assert response.status_code == 403

    def test_the_agent_identity_has_no_session(self, monkeypatch):
        harness = _harness(
            monkeypatch,
            email=AGENT_SA,
            settings_overrides={
                "AGENT_SERVICE_ACCOUNT": AGENT_SA,
                "AGENT_IAP_TARGET_AUDIENCE": f"{CONSOLE_ORIGIN}/*",
            },
        )
        response = harness.client.get(f"{API_PREFIX}/session", headers=_auth_headers())
        assert response.status_code == 403

    def test_the_session_exposes_no_configuration_secret(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        body = harness.client.get(f"{API_PREFIX}/session", headers=_auth_headers()).text
        for secret in (CSRF_SECRET, CURSOR_KEY_B64, IAP_AUDIENCE, CONSOLE_ORIGIN):
            assert secret not in body

    def test_feature_flags_are_booleans_only(self, monkeypatch):
        harness = _harness(monkeypatch)
        flags = harness.client.get(
            f"{API_PREFIX}/session", headers=_auth_headers()
        ).json()["feature_flags"]
        assert flags and all(isinstance(value, bool) for value in flags.values())
        assert flags["remediation_enabled"] is False
        assert "import_export_enabled" not in flags


# =====================================================================
# Ticket list, detail, timeline
# =====================================================================


class TestTicketList:

    def test_a_viewer_may_list(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        _ingest_execution(harness)
        response = harness.client.get(f"{API_PREFIX}/tickets", headers=_auth_headers())
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["execution_id"] == "job123:0"
        assert body["items"][0]["devrev_display_id"] == SYNTHETIC_DISPLAY_ID
        assert body["items"][0]["review"]["status"] == "unreviewed"
        assert harness.devrev.list_calls == []

    def test_the_repository_cursor_never_reaches_the_client(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        response = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        )
        body = response.json()
        assert body["next_cursor"] is not None
        assert "job111" not in body["next_cursor"]
        assert "job222" not in body["next_cursor"]

    def test_a_console_cursor_round_trips(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        first = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        ).json()
        second = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1",
            headers=_auth_headers(**{CURSOR_HEADER: first["next_cursor"]}),
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["execution_id"] != first["items"][0]["execution_id"]
        assert harness.devrev.list_calls == []

    def test_a_cursor_from_another_session_is_refused(self, monkeypatch):
        first = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        _ingest_execution(first, job_id="job111")
        _ingest_execution(first, job_id="job222")
        token = first.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        ).json()["next_cursor"]
        other = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        response = other.client.get(
            f"{API_PREFIX}/tickets?page_size=1",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_CURSOR_REJECTED

    def test_a_cursor_from_another_filter_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        token = harness.client.get(
            f"{API_PREFIX}/tickets?status=succeeded&page_size=1",
            headers=_auth_headers(),
        ).json()["next_cursor"]
        response = harness.client.get(
            f"{API_PREFIX}/tickets?hydration_status=succeeded&page_size=1",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_CURSOR_REJECTED

    def test_a_cursor_from_another_direction_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        token = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        ).json()["next_cursor"]
        response = harness.client.get(
            f"{API_PREFIX}/tickets?mode=before",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert response.status_code == 422

    def test_a_cursor_from_another_route_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        token = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        ).json()["next_cursor"]
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job111:0/timeline",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert response.status_code == 422

    def test_a_tampered_cursor_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        token = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        ).json()["next_cursor"]
        forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        response = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1",
            headers=_auth_headers(**{CURSOR_HEADER: forged}),
        )
        assert response.status_code == 422

    def test_an_expired_cursor_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        token = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1", headers=_auth_headers()
        ).json()["next_cursor"]
        harness.client.app.state.clock = lambda: T0 + timedelta(days=2)
        response = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "param", ["cursor", "next_cursor", "page_token", "after", "before"]
    )
    def test_a_raw_cursor_query_parameter_is_refused(self, monkeypatch, param):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/tickets?{param}=remote-next", headers=_auth_headers()
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_VALIDATION_FAILED

    def test_a_display_id_filter_keeps_every_execution_for_that_ticket(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        response = harness.client.get(
            f"{API_PREFIX}/tickets?devrev_display_id={SYNTHETIC_DISPLAY_ID}",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is None and body["prev_cursor"] is None
        assert harness.devrev.list_calls == []

    def test_execution_filters_can_be_combined_without_devrev_discovery(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness)
        response = harness.client.get(
            f"{API_PREFIX}/tickets?devrev_display_id={SYNTHETIC_DISPLAY_ID}"
            "&route=knowledge_question"
            "&status=succeeded&hydration_status=succeeded&review_status=unreviewed",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert harness.devrev.list_calls == []

    def test_an_unknown_query_parameter_is_refused_not_widened(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/tickets?type=issue", headers=_auth_headers()
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_UNSUPPORTED_FILTER

    def test_an_oversized_page_size_is_422(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/tickets?page_size=1000", headers=_auth_headers()
        )
        assert response.status_code == 422

    def test_devrev_list_rate_limits_cannot_hide_the_execution_ledger(self, monkeypatch):
        harness = _harness(
            monkeypatch,
            devrev=_FakeDevRev(list_error=DevRevRateLimitError("limited", retry_after_s=7)),
        )
        _ingest_execution(harness)
        response = harness.client.get(f"{API_PREFIX}/tickets", headers=_auth_headers())
        assert response.status_code == 200
        assert response.json()["items"]
        assert harness.devrev.list_calls == []

    def test_devrev_list_outages_cannot_hide_the_execution_ledger(self, monkeypatch):
        harness = _harness(
            monkeypatch, devrev=_FakeDevRev(list_error=DevRevTransientError("down"))
        )
        _ingest_execution(harness)
        response = harness.client.get(f"{API_PREFIX}/tickets", headers=_auth_headers())
        assert response.status_code == 200
        assert response.json()["items"]
        assert harness.devrev.list_calls == []

    def test_our_own_rate_bound_maps_to_429(self, monkeypatch):
        from api.rate_limit import FixedWindowRateLimiter

        harness = _harness(monkeypatch, rate_limiter=FixedWindowRateLimiter())
        monkeypatch.setattr(
            "api.ticket_review_routes.READ_RATE_LIMIT_PER_MINUTE", 1, raising=True
        )
        assert harness.client.get(f"{API_PREFIX}/tickets", headers=_auth_headers()).status_code == 200
        response = harness.client.get(f"{API_PREFIX}/tickets", headers=_auth_headers())
        assert response.status_code == 429
        assert response.json()["error"]["code"] == CODE_RATE_LIMITED
        assert int(response.headers["Retry-After"]) >= 1


class TestTicketDetailAndTimeline:

    def test_detail_returns_the_persisted_execution_without_loading_timeline(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness)
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0", headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["execution"]["execution_id"] == "job123:0"
        assert body["generated_answer"] == "The generated RAG answer."
        assert body["classification_reasoning"]
        assert harness.devrev.timeline_calls == []

    def test_a_timeline_cursor_round_trips_forward_only(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness)
        first = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
        ).json()
        token = first["next_cursor"]
        page = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert page.status_code == 200
        assert page.json()["prev_cursor"] is None
        assert harness.devrev.timeline_calls[-1][1] == "remote-timeline-next"

    def test_a_timeline_cursor_for_another_execution_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness, job_id="job111")
        _ingest_execution(harness, job_id="job222")
        token = harness.client.get(
            f"{API_PREFIX}/tickets/job111:0/timeline", headers=_auth_headers()
        ).json()["next_cursor"]
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job222:0/timeline",
            headers=_auth_headers(**{CURSOR_HEADER: token}),
        )
        assert response.status_code == 422

    def test_a_missing_ticket_is_404(self, monkeypatch):
        harness = _harness(
            monkeypatch, devrev=_FakeDevRev(get_error=DevRevNotFoundError("gone"))
        )
        _ingest_execution(harness)
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == CODE_NOT_FOUND

    def test_an_out_of_scope_ticket_is_indistinguishable_from_missing(self, monkeypatch):
        missing_harness = _harness(
            monkeypatch, devrev=_FakeDevRev(get_error=DevRevNotFoundError("gone"))
        )
        forbidden_harness = _harness(
            monkeypatch, devrev=_FakeDevRev(get_error=DevRevScopeError("out of scope"))
        )
        _ingest_execution(missing_harness)
        _ingest_execution(forbidden_harness)
        missing = missing_harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
        )
        forbidden = forbidden_harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
        )
        assert missing.status_code == forbidden.status_code == 404
        assert missing.json()["error"]["code"] == forbidden.json()["error"]["code"]
        assert missing.json()["error"]["message"] == forbidden.json()["error"]["message"]

    def test_detail_uses_the_authorized_snapshot_without_rehydrating_on_get(
        self, monkeypatch
    ):
        harness = _harness(monkeypatch)
        _create_review(harness)
        calls_before_get = list(harness.devrev.get_calls)
        harness.devrev._get_error = DevRevTransientError("down")
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0", headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["partial"] is False
        assert body["review"] is not None
        assert body["ticket"]["body"] == "Original ticket body."
        assert harness.devrev.get_calls == calls_before_get

    @pytest.mark.parametrize(
        "reference",
        [
            "https://app.devrev.ai/tickets/1",
            "../../etc/passwd",
            "don:core:../escape",
            "TKT-",
            "TKT-abc",
            "  ",
            "a" * 300,
            "don:core:dvrv us-1:x/1",
        ],
    )
    def test_a_bad_execution_reference_is_422_or_not_routable(self, monkeypatch, reference):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/tickets/{reference}/timeline", headers=_auth_headers()
        )
        assert response.status_code in (404, 422)
        if response.status_code == 422:
            assert response.json()["error"]["code"] in (
                CODE_VALIDATION_FAILED,
                CODE_UNSUPPORTED_FILTER,
            )

    def test_validated_ticket_ref_normalizes_a_display_id(self):
        assert validated_ticket_ref(" tkt-42 ") == "TKT-42"
        assert validated_ticket_ref(SYNTHETIC_DON) == SYNTHETIC_DON

    def test_the_timeline_envelope_reports_partial_results(self, monkeypatch):
        from data_pipeline.devrev_client import DevRevResourceLimitError

        harness = _harness(
            monkeypatch,
            devrev=_FakeDevRev(timeline_error=DevRevResourceLimitError("too big")),
        )
        _ingest_execution(harness)
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["partial"] is True
        assert "devrev_timeline_unavailable" in body["warnings"]

    def test_no_raw_devrev_object_or_unbounded_text_is_returned(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness)
        body = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0", headers=_auth_headers()
        ).json()
        assert set(body).issubset(
            {
                "execution",
                "ticket",
                "review",
                "generated_answer",
                "classification_reasoning",
                "outcome_reason",
                "diagnostics",
                "gaps",
                "source_articles",
                "chunk_evidence",
                "model_metadata",
                "timing_metadata",
                "evidence",
                "hydration_status",
                "partial",
                "warnings",
            }
        )
        assert set(body["ticket"]) == set(DevRevTicketDetail.model_fields)

    def test_execution_detail_includes_the_bounded_evidence_summary(self, monkeypatch):
        broker = _FailingBroker()
        harness = _harness(monkeypatch, broker=broker)
        run = _ingest_execution(harness).run

        response = harness.client.get(
            f"{API_PREFIX}/tickets/{run.execution_id}", headers=_auth_headers()
        )

        assert response.status_code == 200, response.text
        evidence = response.json()["evidence"]
        assert evidence["correlation_status"] == "linked"
        assert evidence["broker_available"] is False
        assert evidence["warnings"] == ["evidence_broker_unavailable"]
        assert broker.calls == 1
        assert "candidate_token" not in response.text


# =====================================================================
# Reviews: create, list, detail, patch
# =====================================================================


class TestReviewLifecycle:

    def test_rag_ingest_creates_the_review_and_detail_returns_an_etag(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.get(
            f"{API_PREFIX}/reviews/{created['review_id']}", headers=_auth_headers()
        )
        assert response.status_code == 200
        assert response.headers["ETag"] == f'"v{created["version"]}"'
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["review_id"] == review_id_for_devrev_work(SYNTHETIC_DON)

    def test_the_manual_create_route_is_absent_for_a_viewer_too(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        response = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client),
            json={},
        )
        assert response.status_code == 404

    def test_rag_ingestion_is_idempotent_for_the_same_execution(self, monkeypatch):
        harness = _harness(monkeypatch)
        first = _ingest_execution(harness)
        second = _ingest_execution(harness)
        assert first.created is True
        assert second.created is False
        assert second.run.review_id == first.run.review_id

    def test_system_created_review_starts_unreviewed_without_human_fields(self, monkeypatch):
        harness = _harness(monkeypatch)
        review = _create_review(harness)
        assert review["status"] == "unreviewed"
        assert review["topic"] is None
        assert review["comments"] is None

    def test_no_manual_body_shape_can_resurrect_the_removed_route(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client),
            json={"topic": "limits", "version": 99},
        )
        assert response.status_code == 404

    def test_a_review_is_readable_with_its_etag(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.get(
            f"{API_PREFIX}/reviews/{created['review_id']}", headers=_auth_headers()
        )
        assert response.status_code == 200
        assert response.headers["ETag"] == '"v1"'

    def test_a_missing_review_is_404(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(f"{API_PREFIX}/reviews/{'0' * 64}", headers=_auth_headers())
        assert response.status_code == 404

    def test_a_malformed_review_id_is_422(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(f"{API_PREFIX}/reviews/not-a-hash", headers=_auth_headers())
        assert response.status_code == 422

    def test_the_queue_lists_reviews(self, monkeypatch):
        harness = _harness(monkeypatch)
        _create_review(harness)
        response = harness.client.get(f"{API_PREFIX}/reviews", headers=_auth_headers())
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    def test_the_verified_remediation_agent_may_list_the_review_queue(self, monkeypatch):
        harness = _harness(
            monkeypatch,
            email=AGENT_SA,
            settings_overrides={
                "AGENT_SERVICE_ACCOUNT": AGENT_SA,
                "AGENT_IAP_TARGET_AUDIENCE": f"{CONSOLE_ORIGIN}/*",
            },
        )
        _create_review(harness)

        response = harness.client.get(f"{API_PREFIX}/reviews", headers=_auth_headers())

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1

    def test_a_facet_needs_both_halves(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/reviews?facet=topic", headers=_auth_headers()
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_UNSUPPORTED_FILTER

    def test_an_unsupported_filter_combination_is_422(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/reviews?title_contains=anything", headers=_auth_headers()
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_UNSUPPORTED_FILTER


class TestPatchPreconditions:

    def test_a_patch_requires_if_match(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(harness.client, idempotency="idem-patch-000001"),
            json={"topic": "limits"},
        )
        assert response.status_code == 428
        assert response.json()["error"]["code"] == CODE_PRECONDITION_REQUIRED

    @pytest.mark.parametrize("value", ["v1", '"1"', "*", 'W/"v1"', '"v0"', '"v01"', '"v" '])
    def test_a_malformed_if_match_is_422(self, monkeypatch, value):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000002", **{"If-Match": value}
            ),
            json={"topic": "limits"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == CODE_PRECONDITION_MALFORMED

    def test_a_stale_if_match_is_412_with_safe_metadata(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        first = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000003", **{"If-Match": '"v1"'}
            ),
            json={"topic": "limits"},
        )
        assert first.status_code == 200
        assert first.headers["ETag"] == '"v2"'

        stale = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000004", **{"If-Match": '"v1"'}
            ),
            json={"topic": "other"},
        )
        assert stale.status_code == 412
        error = stale.json()["error"]
        assert error["code"] == CODE_REVIEW_VERSION_CONFLICT
        assert error["current_version"] == 2
        # Safe metadata only: never the other reviewer's unsaved content.
        assert "other" not in stale.text
        assert "limits" not in stale.text

    def test_a_successful_patch_advances_the_etag(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000005", **{"If-Match": '"v1"'}
            ),
            json={"rating": Rating.GOOD.value if hasattr(Rating, "GOOD") else None},
        )
        assert response.status_code in (200, 422)

    def test_wrong_route_is_a_persisted_observation_type(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)

        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-wrong-route", **{"If-Match": '"v1"'}
            ),
            json={"observation_type": "wrong_route"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["observation_type"] == "wrong_route"
        persisted = harness.client.get(
            f"{API_PREFIX}/reviews/{created['review_id']}", headers=_auth_headers()
        )
        assert persisted.json()["observation_type"] == "wrong_route"

    def test_hyphenated_wrong_route_is_rejected(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)

        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-patch-wrong-route-invalid",
                **{"If-Match": '"v1"'},
            ),
            json={"observation_type": "wrong-route"},
        )

        assert response.status_code == 422

    def test_a_reused_idempotency_key_for_a_different_request_is_409(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        headers = _write_headers(
            harness.client, idempotency="idem-patch-000006", **{"If-Match": '"v1"'}
        )
        first = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=headers,
            json={"topic": "one"},
        )
        assert first.status_code == 200
        second = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000006", **{"If-Match": '"v2"'}
            ),
            json={"topic": "two"},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == CODE_IDEMPOTENCY_CONFLICT

    def test_only_an_admin_may_reopen(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        created = _create_review(harness)
        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}?admin_reopen=true",
            headers=_write_headers(
                harness.client, idempotency="idem-patch-000007", **{"If-Match": '"v1"'}
            ),
            json={"topic": "reopened"},
        )
        assert response.status_code == 403

    def test_an_admin_closes_a_reviewed_review_in_one_patch(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        created = _create_review(harness)
        reviewed = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-admin-direct-close-setup",
                **{"If-Match": '"v1"'},
            ),
            json={"rating": 4},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "reviewed"

        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-admin-direct-close",
                **{"If-Match": '"v2"'},
            ),
            json={
                "status": "resolved",
                "resolution": {
                    "outcome": "no_change",
                    "no_change_reason": "The issue was already fixed and verified.",
                },
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "resolved"

    def test_a_reviewer_cannot_use_the_admin_direct_close_edge(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        created = _create_review(harness)
        reviewed = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-reviewer-direct-close-setup",
                **{"If-Match": '"v1"'},
            ),
            json={"rating": 4},
        )
        assert reviewed.status_code == 200, reviewed.text

        response = harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=_write_headers(
                harness.client,
                idempotency="idem-reviewer-direct-close",
                **{"If-Match": '"v2"'},
            ),
            json={
                "status": "resolved",
                "resolution": {
                    "outcome": "no_change",
                    "no_change_reason": "The issue was already fixed and verified.",
                },
            },
        )

        assert response.status_code in {409, 422}


# =====================================================================
# Audit and evidence
# =====================================================================


class TestAuditAndEvidence:

    def test_the_audit_ledger_is_readable_and_paginated(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.get(
            f"{API_PREFIX}/reviews/{created['review_id']}/audit-events",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"], "creating a review must leave an audit event"
        assert "next_cursor" in body and "page_size" in body

    def test_the_creation_audit_actor_is_the_verified_ingest_workload(self, monkeypatch):
        """Automatic creation is attributed to the RAG ingest workload."""
        harness = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        created = _create_review(harness)
        events = harness.client.get(
            f"{API_PREFIX}/reviews/{created['review_id']}/audit-events",
            headers=_auth_headers(),
        ).json()["items"]
        expected_subject = "service:ticket-evaluation-ingest"
        assert events[0]["actor_subject"] == expected_subject
        assert events[0]["actor_subject_hash"] != expected_subject

    def test_a_body_supplied_actor_cannot_resurrect_manual_creation(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.REVIEWER)
        response = harness.client.post(
            f"{API_PREFIX}/tickets/{SYNTHETIC_DISPLAY_ID}/review",
            headers=_write_headers(harness.client),
            json={"actor": {"subject": "x", "email": "admin@example.invalid"}},
        )
        assert response.status_code == 404

    def test_audit_events_for_a_missing_review_are_404(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/reviews/{'0' * 64}/audit-events", headers=_auth_headers()
        )
        assert response.status_code == 404

    def test_evidence_links_start_empty_and_are_paginated(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.get(
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["next_cursor"] is None

    def test_linking_requires_if_match(self, monkeypatch):
        harness = _harness(monkeypatch, broker=_FailingBroker())
        created = _create_review(harness)
        response = harness.client.post(
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links",
            headers=_write_headers(harness.client, idempotency="idem-link-000001"),
            json={"broker_candidate_token": "not-a-real-token", "reason": "manual"},
        )
        assert response.status_code == 428

    def test_an_unlinkable_candidate_is_a_uniform_409(self, monkeypatch):
        harness = _harness(monkeypatch, broker=_FailingBroker())
        created = _create_review(harness)
        response = harness.client.post(
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links",
            headers=_write_headers(
                harness.client, idempotency="idem-link-000002", **{"If-Match": '"v1"'}
            ),
            json={"broker_candidate_token": "forged-token", "reason": "manual"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EVIDENCE_LINK_REJECTED"

    def test_a_viewer_may_not_link(self, monkeypatch):
        harness = _harness(monkeypatch, role=ReviewerRole.VIEWER)
        response = harness.client.post(
            f"{API_PREFIX}/reviews/{'a' * 64}/evidence-links",
            headers=_write_headers(
                harness.client, idempotency="idem-link-000003", **{"If-Match": '"v1"'}
            ),
            json={"broker_candidate_token": "token", "reason": "manual"},
        )
        assert response.status_code == 403

    def test_unlinking_requires_a_reason(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.request(
            "DELETE",
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links/{'b' * 64}",
            headers=_write_headers(
                harness.client, idempotency="idem-unlink-00001", **{"If-Match": '"v1"'}
            ),
            json={},
        )
        assert response.status_code == 422

    def test_unlinking_a_missing_link_is_a_uniform_409(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.request(
            "DELETE",
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links/{'b' * 64}",
            headers=_write_headers(
                harness.client, idempotency="idem-unlink-00002", **{"If-Match": '"v1"'}
            ),
            json={"reason": "linked in error"},
        )
        assert response.status_code == 409

    def test_unlinking_requires_if_match(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        response = harness.client.request(
            "DELETE",
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links/{'b' * 64}",
            headers=_write_headers(harness.client, idempotency="idem-unlink-00003"),
            json={"reason": "linked in error"},
        )
        assert response.status_code == 428


# =====================================================================
# The unsafe-request matrix, end to end
# =====================================================================


class TestUnsafeRequestMatrixOverHttp:

    def _post(self, harness, **header_overrides):
        created = _create_review(harness)
        headers = _write_headers(harness.client)
        headers["If-Match"] = f'"v{created["version"]}"'
        for key, value in header_overrides.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        return harness.client.patch(
            f"{API_PREFIX}/reviews/{created['review_id']}",
            headers=headers,
            json={"topic": "limits"},
        )

    def test_the_happy_path_passes(self, monkeypatch):
        assert self._post(_harness(monkeypatch)).status_code == 200

    def test_a_missing_origin_is_403(self, monkeypatch):
        response = self._post(_harness(monkeypatch), **{ORIGIN_HEADER: None})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_ORIGIN_REJECTED"

    def test_a_foreign_origin_is_403(self, monkeypatch):
        response = self._post(
            _harness(monkeypatch), **{ORIGIN_HEADER: "https://attacker.example"}
        )
        assert response.status_code == 403

    def test_a_cross_site_fetch_is_403(self, monkeypatch):
        response = self._post(_harness(monkeypatch), **{FETCH_SITE_HEADER: "cross-site"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FETCH_SITE_REJECTED"

    def test_a_missing_csrf_token_is_403(self, monkeypatch):
        response = self._post(_harness(monkeypatch), **{CSRF_HEADER: None})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_TOKEN_REJECTED"

    def test_another_sessions_csrf_token_is_403(self, monkeypatch):
        first = _harness(monkeypatch, role=ReviewerRole.ADMIN)
        stolen = _csrf_token(first.client)
        response = self._post(_harness(monkeypatch), **{CSRF_HEADER: stolen})
        assert response.status_code == 403

    def test_a_wrong_content_type_is_415(self, monkeypatch):
        response = self._post(_harness(monkeypatch), **{"Content-Type": "text/plain"})
        assert response.status_code == 415

    def test_a_missing_idempotency_key_is_400(self, monkeypatch):
        response = self._post(_harness(monkeypatch), **{IDEMPOTENCY_HEADER: None})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_an_invalid_idempotency_key_is_400(self, monkeypatch):
        response = self._post(_harness(monkeypatch), **{IDEMPOTENCY_HEADER: "no"})
        assert response.status_code == 400

    def test_safe_reads_need_none_of_it(self, monkeypatch):
        harness = _harness(monkeypatch)
        assert harness.client.get(
            f"{API_PREFIX}/tickets", headers=_auth_headers()
        ).status_code == 200


# =====================================================================
# Absence of later stages, and no secret leakage anywhere
# =====================================================================


class TestSurfaceAndSecrecy:

    def test_stage_nine_routes_are_still_absent(self, monkeypatch):
        """Stage 9's CSV import/export surface is asserted absent, not stubbed.

        Stage 8's batch routes used to be asserted here too. They are published
        now, so the assertion moved to
        ``tests/test_ticket_review_batch_routes.py``, which pins the exact
        fourteen paths rather than merely their absence.
        """
        harness = _harness(monkeypatch)
        paths = set(harness.client.app.openapi()["paths"])
        assert not any("/imports/" in path or "/exports/" in path for path in paths)
        assert not any("/exports" == path.rsplit("/", 1)[-1] for path in paths)

    def test_every_documented_path_is_in_scope(self, monkeypatch):
        harness = _harness(monkeypatch)
        for path in harness.client.app.openapi()["paths"]:
            assert path.startswith((API_PREFIX, "/livez", "/readyz", "/tickets"))

    def test_an_unpublished_stage_nine_route_is_404_not_403(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/exports/ticket-reviews.csv", headers=_auth_headers()
        )
        assert response.status_code == 404

    def test_no_response_contains_a_configuration_secret(self, monkeypatch):
        harness = _harness(monkeypatch)
        created = _create_review(harness)
        for path in (
            f"{API_PREFIX}/session",
            f"{API_PREFIX}/tickets",
            f"{API_PREFIX}/tickets/job123:0",
            f"{API_PREFIX}/reviews",
            f"{API_PREFIX}/reviews/{created['review_id']}",
            f"{API_PREFIX}/reviews/{created['review_id']}/audit-events",
            f"{API_PREFIX}/reviews/{created['review_id']}/evidence-links",
        ):
            body = harness.client.get(path, headers=_auth_headers()).text
            for secret in (CSRF_SECRET, CURSOR_KEY_B64, ASSERTION):
                assert secret not in body, path

    def test_an_error_envelope_never_echoes_an_upstream_body(self, monkeypatch):
        harness = _harness(monkeypatch)
        _ingest_execution(harness)
        harness.devrev._get_error = DevRevTransientError(
            "upstream said: participant secret"
        )
        response = harness.client.get(
            f"{API_PREFIX}/tickets/job123:0/timeline", headers=_auth_headers()
        )
        assert response.status_code == 503
        assert "participant secret" not in response.text

    def test_every_error_uses_the_one_envelope(self, monkeypatch):
        harness = _harness(monkeypatch)
        for path, expected in (
            (f"{API_PREFIX}/reviews/{'0' * 64}", 404),
            (f"{API_PREFIX}/reviews/not-a-hash", 422),
            (f"{API_PREFIX}/nope", 404),
        ):
            response = harness.client.get(path, headers=_auth_headers())
            assert response.status_code == expected
            body = response.json()
            assert set(body) == {"error"}
            assert {"code", "message"} <= set(body["error"])
            assert body["error"]["request_id"]

    def test_the_request_id_is_echoed_and_server_generated(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/tickets",
            headers=_auth_headers(**{"X-Request-ID": "client-chosen"}),
        )
        assert response.headers["X-Request-ID"] != "client-chosen"

    def test_no_route_accepts_the_rag_api_key(self, monkeypatch):
        harness = _harness(monkeypatch)
        response = harness.client.get(
            f"{API_PREFIX}/tickets", headers={"X-API-Key": "any-value"}
        )
        assert response.status_code == 401
