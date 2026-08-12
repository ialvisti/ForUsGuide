"""Stage 8 Step 1 — the remediation-batch contract of ``/api/admin/v1``.

Integration tests over the **real** middleware stack, the **real** router, the
**real** ``TicketReviewRepository`` on the in-memory backend, and the **real**
unsafe-request guard. Only DevRev is faked, because it is the only collaborator
that would otherwise open a socket.

What these tests are actually defending
---------------------------------------
The batch surface is the one place where an automated caller writes to durable,
customer-linked records, so most of this file is about the boundaries rather than
the happy path:

*   **the agent cannot finish the job.** It authors up to ``changes_proposed``
    and stops. A second human starts verification and completes the batch, and a
    review only becomes ``resolved`` with recorded verification evidence;
*   **the lease is a credential.** It is minted server-side, returned exactly
    once, stored only as a SHA-256, and never appears in any other response, any
    error, or any audit record;
*   **roles do not ladder here.** A remediator curates, the agent authors, an
    independent reviewer judges, and an admin owns the one lease extension.
    Seniority does not substitute for phase;
*   **nothing leaks.** The copied prompt carries identifiers and instructions,
    never records. Materialization is bounded, paged, audited, and excludes
    conversation unless it is asked for explicitly.

Testing through the real repository is deliberate. A hand-written fake would
return whatever version a test wanted, and the version/lease/transition rules
this file exists to pin are exactly the ones a fake would paper over.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.reviewer_auth import IAP_ASSERTION_HEADER, IAP_ISSUER
from api.ticket_review_models import (
    MAX_BATCH_REVIEWS,
    REMEDIATION_HEARTBEAT_S,
    REMEDIATION_LEASE_S,
    REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    BatchStatus,
    ReviewPatch,
    ReviewStatus,
    ReviewerRole,
    TicketReview,
    review_id_for_devrev_work,
)
from api.ticket_review_routes import (
    AGENT_ROUTE_TEMPLATES,
    API_PREFIX,
    BATCHES_PATH,
    CODE_BATCH_ALREADY_CLAIMED,
    CODE_BATCH_CONFLICT,
    CODE_BATCH_LEASE_LOST,
    CODE_BATCH_REJECTED,
    CODE_BATCH_VERSION_CONFLICT,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
)
from api.ticket_review_models import LEASE_TOKEN_HEADER
from api.tickets_console_config import TicketConsoleSettings
from api.tickets_console_main import build_console_app, safe_route_template
from api.tickets_csrf import (
    CSRF_HEADER,
    FETCH_SITE_HEADER,
    IDEMPOTENCY_HEADER,
    ORIGIN_HEADER,
    agent_route_allowlisted,
    policy_from_settings,
)
from data_pipeline.ticket_review_remediation_prompt import (
    PROMPT_TEMPLATE_VERSION,
    template_sha256,
)
from data_pipeline.ticket_review_repository import (
    BATCHES_COLLECTION,
    BATCH_ITEMS_SUBCOLLECTION,
    InMemoryTicketReviewBackend,
    MutationContext,
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
AGENT_SA = "tickets-remediation-agent@rag-kb-system.iam.gserviceaccount.com"
REPO_ID = "synthetic-repo"
BASE_REF = "main"

SYNTHETIC_PART = "don:core:dvrv-us-1:devo/synthetic:product/1"
ASSERTION = "synthetic.iap.assertion"
IAP_AUDIENCE = "/projects/1234567890/locations/us-central1/services/tickets-console"

T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)

EMAILS: dict[ReviewerRole, str] = {
    ReviewerRole.VIEWER: "viewer@example.invalid",
    ReviewerRole.REVIEWER: "reviewer@example.invalid",
    ReviewerRole.REMEDIATOR: "remediator@example.invalid",
    ReviewerRole.ADMIN: "admin@example.invalid",
    ReviewerRole.AGENT: AGENT_SA,
}
#: A second reviewer, so "somebody else verifies" can be told apart from "the
#: same person with a different hat".
OTHER_REVIEWER = "second-reviewer@example.invalid"


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

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class _FakeDevRev:
    """The service is constructed but never reached by a batch route."""

    async def get_ticket(self, work_id: str):  # pragma: no cover - unused here
        raise AssertionError("a batch route must not call DevRev")

    async def list_tickets(self, query, **kwargs):  # pragma: no cover - unused
        raise AssertionError("a batch route must not call DevRev")

    async def list_timeline_page(self, work_id, **kwargs):  # pragma: no cover
        raise AssertionError("a batch route must not call DevRev")


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
            ' "second-reviewer@example.invalid": "reviewer",'
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
        # The agent identity plus the repository contract: all four are required
        # before `remediation_enabled` reports true.
        "AGENT_SERVICE_ACCOUNT": AGENT_SA,
        "AGENT_IAP_TARGET_AUDIENCE": f"{CONSOLE_ORIGIN}/*",
        "REPO_ID": REPO_ID,
        "EXPECTED_BASE_REF": BASE_REF,
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


class _Harness:
    """One wired app plus the collaborators a test asserts against."""

    def __init__(self, client, *, repository, clock, settings):
        self.client = client
        self.repository = repository
        self.clock = clock
        self.settings = settings
        self._identity = EMAILS[ReviewerRole.REMEDIATOR]

    def act_as(self, role: ReviewerRole | str) -> "_Harness":
        """Switch the identity the IAP verifier will report."""
        self._identity = EMAILS[role] if isinstance(role, ReviewerRole) else role
        return self

    @property
    def email(self) -> str:
        return self._identity


def _harness(monkeypatch, **settings_overrides) -> _Harness:
    settings = _settings(monkeypatch, **settings_overrides)
    holder: dict[str, str] = {"email": EMAILS[ReviewerRole.REMEDIATOR]}

    def verifier(token: str, audience: str):
        email = holder["email"]
        return {
            "iss": IAP_ISSUER,
            "aud": audience,
            "sub": f"accounts.google.com:{email}",
            "email": email,
        }

    clock = _TickClock()
    backend = InMemoryTicketReviewBackend()
    repository = TicketReviewRepository(backend, cursor_key=CURSOR_KEY, clock=clock)
    devrev = _FakeDevRev()
    service = TicketReviewService(
        devrev=devrev,
        repository=repository,
        classifier=MessageClassifier.from_settings(settings),
        candidate_key=CURSOR_KEY,
        broker=None,
        clock=clock,
    )
    app = build_console_app(
        settings,
        devrev=devrev,
        repository=repository,
        service=service,
        claims_verifier=verifier,
        clock=clock,
        firestore_database="tickets-console-emulator",
    )
    harness = _Harness(
        TestClient(app, raise_server_exceptions=False),
        repository=repository,
        clock=clock,
        settings=settings,
    )

    # The verifier closes over `holder`, so `act_as` retargets the identity that
    # every subsequent request authenticates as.
    original_act_as = harness.act_as

    def act_as(role):
        result = original_act_as(role)
        holder["email"] = harness.email
        return result

    harness.act_as = act_as  # type: ignore[method-assign]
    return harness


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _auth(**extra) -> dict[str, str]:
    headers = {IAP_ASSERTION_HEADER: ASSERTION}
    headers.update(extra)
    return headers


def _csrf(harness: _Harness) -> str:
    response = harness.client.get(f"{API_PREFIX}/session", headers=_auth())
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _human_write_headers(harness: _Harness, *, idempotency: str, **extra):
    """The four headers a browser supplies, plus the token from the session.

    Origin and Sec-Fetch-Site are set explicitly because a test client is not a
    browser. Production code must never try: they are forbidden headers in a
    browser fetch.
    """
    headers = _auth(
        **{
            ORIGIN_HEADER: CONSOLE_ORIGIN,
            FETCH_SITE_HEADER: "same-origin",
            CSRF_HEADER: _csrf(harness),
            IDEMPOTENCY_HEADER: idempotency,
            "Content-Type": "application/json",
        }
    )
    headers.update(extra)
    return headers


def _agent_write_headers(*, idempotency: str, **extra):
    """What the CLI actually sends: no Origin, no Fetch Metadata, no CSRF.

    The agent exemption waives exactly those three. Strict content type and a
    valid ``Idempotency-Key`` are still required, which is the point of asserting
    it here rather than trusting the middleware's own unit tests.
    """
    headers = _auth(
        **{IDEMPOTENCY_HEADER: idempotency, "Content-Type": "application/json"}
    )
    headers.update(extra)
    return headers


def _work_id(index: int) -> str:
    return f"don:core:dvrv-us-1:devo/synthetic:ticket/{index:06d}"


def _context(harness: _Harness, email: str, role: ReviewerRole, key: str):
    from api.ticket_review_models import ReviewerIdentity

    return MutationContext(
        actor=ReviewerIdentity(subject=f"accounts.google.com:{email}", email=email),
        actor_role=role,
        request_id=f"seed-{key}",
        idempotency_key=f"seed-key-{key}",
    )


async def _seed_review(
    harness: _Harness, index: int, *, status: str = "in_progress"
) -> tuple[str, int]:
    """Create one review and walk it up the real transition table."""
    work = _work_id(index)
    review_id = review_id_for_devrev_work(work)
    context = _context(
        harness, EMAILS[ReviewerRole.REMEDIATOR], ReviewerRole.REMEDIATOR, f"c{index}"
    )
    review, _ = await harness.repository.create_or_get_review(
        TicketReview(
            review_id=review_id,
            devrev_work_id=work,
            devrev_display_id=f"TICKET-{index:06d}",
        ),
        context=context,
    )
    ladder = ["reviewed", "triaged", "planned", "in_progress", "changes_proposed", "verifying"]
    for step, target in enumerate(ladder):
        review = await harness.repository.patch_review(
            review_id,
            ReviewPatch(status=target),
            expected_version=review.version,
            context=_context(
                harness,
                EMAILS[ReviewerRole.REMEDIATOR],
                ReviewerRole.REMEDIATOR,
                f"s{index}-{step}",
            ),
        )
        if target == status:
            break
    return review_id, review.version


def _batch_url(batch_id: str, suffix: str = "") -> str:
    return f"{API_PREFIX}{BATCHES_PATH}/{batch_id}{suffix}"


async def _create_batch(
    harness: _Harness, refs: list[tuple[str, int]], *, key="idem-create-batch-1", **body
) -> dict:
    harness.act_as(ReviewerRole.REMEDIATOR)
    payload = {
        "review_refs": [
            {"review_id": review_id, "review_version": version}
            for review_id, version in refs
        ]
    }
    payload.update(body)
    response = harness.client.post(
        f"{API_PREFIX}{BATCHES_PATH}",
        headers=_human_write_headers(harness, idempotency=key),
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _ready(harness: _Harness, batch: dict, *, key="idem-ready-batch-1") -> dict:
    harness.act_as(ReviewerRole.REMEDIATOR)
    response = harness.client.post(
        _batch_url(batch["batch_id"], ":ready"),
        headers=_human_write_headers(harness, idempotency=key),
        json={"expected_version": batch["version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _claim(harness: _Harness, batch: dict, *, key="idem-claim-batch-1") -> dict:
    """Claim, and fold the header-borne token into the returned mapping.

    The plaintext token is a response *header*, not a body field — see
    ``LEASE_TOKEN_HEADER``. Every test reads it through this helper so the split
    is stated once.
    """
    harness.act_as(ReviewerRole.AGENT)
    response = harness.client.post(
        _batch_url(batch["batch_id"], "/claim"),
        headers=_agent_write_headers(idempotency=key),
        json=None,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The body never carries it, whatever else changes.
    assert body["lease_token"] == "**********"
    body["lease_token"] = response.headers[LEASE_TOKEN_HEADER]
    return body


async def _agent_patch(
    harness: _Harness, batch_id: str, body: dict, *, key: str, expect: int = 200
):
    harness.act_as(ReviewerRole.AGENT)
    response = harness.client.patch(
        _batch_url(batch_id),
        headers=_agent_write_headers(idempotency=key),
        json=body,
    )
    assert response.status_code == expect, response.text
    return response.json()


async def _advance_reviews_to_verifying(harness: _Harness, review_ids: list[str]) -> None:
    """Walk each review to ``verifying`` the way a reviewer actually would.

    ``in_progress -> changes_proposed -> verifying``, one audited step at a time,
    through the review's own closed transition table.
    """
    for review_id in review_ids:
        review = await harness.repository.get_review(review_id)
        for step, target in enumerate(("changes_proposed", "verifying")):
            if review.status.value == target:
                continue
            review = await harness.repository.patch_review(
                review_id,
                ReviewPatch(status=target),
                expected_version=review.version,
                context=_context(
                    harness,
                    EMAILS[ReviewerRole.REVIEWER],
                    ReviewerRole.REVIEWER,
                    f"adv-{review_id[:8]}-{step}",
                ),
            )


def _evidence(label: str = "pytest -q") -> dict:
    return {
        "command_label": label,
        "exit_code": 0,
        "passed": 12,
        "failed": 0,
        "skipped": 0,
        "output_sha256": "b" * 64,
        "runtime_s": 3.5,
        "occurred_at": T0.isoformat(),
    }


async def _submitted(harness: _Harness, count: int = 2) -> tuple[dict, str, list[str]]:
    """Drive one batch all the way to ``changes_proposed``.

    Returns the submitted batch body, the lease token, and the frozen review ids.
    """
    refs = [await _seed_review(harness, index) for index in range(1, count + 1)]
    created = await _create_batch(harness, refs)
    batch = await _ready(harness, created["batch"])
    claim = await _claim(harness, batch)
    token = claim["lease_token"]
    batch_id = batch["batch_id"]

    body = await _agent_patch(
        harness,
        batch_id,
        {
            "expected_version": claim["batch"]["version"],
            "lease_token": token,
            "transition": BatchStatus.PLANNING.value,
            "plan_artifact": "docs/plans/synthetic.md",
        },
        key="idem-plan-batch-001",
    )
    body = await _agent_patch(
        harness,
        batch_id,
        {
            "expected_version": body["version"],
            "lease_token": token,
            "transition": BatchStatus.IN_PROGRESS.value,
        },
        key="idem-progress-batch-01",
    )
    body = await _agent_patch(
        harness,
        batch_id,
        {
            "expected_version": body["version"],
            "lease_token": token,
            "transition": BatchStatus.CHANGES_PROPOSED.value,
            "branch": "fix/synthetic-observation",
            "commit_sha": "c" * 40,
            "changed_files": ["kb-rag-system/data_pipeline/rag_engine.py"],
            "test_evidence": [_evidence()],
            "summary": "Widened the retrieval filter so the article is reachable.",
            "per_review_outcomes": [
                {"review_id": review_id, "outcome": "fixed", "summary": "covered"}
                for review_id, _ in refs
            ],
        },
        key="idem-submit-batch-001",
    )
    return body, token, [review_id for review_id, _ in refs]


# =====================================================================
# 1. Creation freezes current versions
# =====================================================================


class TestBatchCreation:

    async def test_creation_freezes_the_current_version_of_each_review(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, index) for index in (1, 2, 3)]
        body = await _create_batch(harness, refs)

        assert body["batch"]["status"] == BatchStatus.DRAFT.value
        assert body["batch"]["item_count"] == 3
        items = await harness.repository.list_batch_items(body["batch"]["batch_id"])
        frozen = {item.review_id: item.review_version for item in items.items}
        assert frozen == {review_id: version for review_id, version in refs}

    async def test_a_stale_review_version_fails_the_whole_creation(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-stale-create-1"),
            json={"review_refs": [{"review_id": review_id, "review_version": version + 5}]},
        )
        assert response.status_code == 412
        assert response.json()["error"]["code"] == "REVIEW_VERSION_CONFLICT"

    async def test_a_review_that_does_not_exist_cannot_be_frozen(self, monkeypatch):
        """No BOLA/IDOR: an id the caller invented is not a way in."""
        harness = _harness(monkeypatch)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-ghost-create-1"),
            json={"review_refs": [{"review_id": "f" * 64, "review_version": 1}]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == CODE_NOT_FOUND

    async def test_a_duplicate_review_is_refused_before_any_write(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-dup-create-1"),
            json={
                "review_refs": [
                    {"review_id": review_id, "review_version": version},
                    {"review_id": review_id, "review_version": version},
                ]
            },
        )
        assert response.status_code == 422

    async def test_an_empty_batch_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-empty-create-1"),
            json={"review_refs": []},
        )
        assert response.status_code == 422

    async def test_more_than_one_hundred_reviews_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        harness.act_as(ReviewerRole.REMEDIATOR)
        refs = [
            {"review_id": f"{index:064x}", "review_version": 1}
            for index in range(MAX_BATCH_REVIEWS + 1)
        ]
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-over-create-1"),
            json={"review_refs": refs},
        )
        assert response.status_code == 422

    async def test_the_snapshot_carries_references_not_conversation(self, monkeypatch):
        """Item 3: bounded references, never a copied conversation body."""
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        secret = "Participant said something confidential and long."
        review = await harness.repository.get_review(review_id)
        await harness.repository.patch_review(
            review_id,
            ReviewPatch(comments=secret),
            expected_version=review.version,
            context=_context(
                harness, EMAILS[ReviewerRole.ADMIN], ReviewerRole.ADMIN, "comment-1"
            ),
        )
        refreshed = await harness.repository.get_review(review_id)
        body = await _create_batch(harness, [(review_id, refreshed.version)])

        raw = await harness.repository.backend.dump_collection(BATCHES_COLLECTION)
        assert secret not in str(raw)
        items = await harness.repository.list_batch_items(body["batch"]["batch_id"])
        assert all(item.comments_excerpt is None for item in items.items)

    async def test_transition_to_planned_is_reported_not_assumed(self, monkeypatch):
        """Item 4: eligible reviews move; the rest are named as unchanged."""
        harness = _harness(monkeypatch)
        eligible = await _seed_review(harness, 1, status="triaged")
        already_past = await _seed_review(harness, 2, status="in_progress")
        body = await _create_batch(
            harness, [eligible, already_past], transition_to_planned=True
        )

        assert body["planned_review_ids"] == [eligible[0]]
        assert body["unchanged_review_ids"] == [already_past[0]]
        planned = await harness.repository.get_review(eligible[0])
        assert planned.status is ReviewStatus.PLANNED
        assert planned.assigned_reviewer is None
        assert (
            await harness.repository.get_review(already_past[0])
        ).status is ReviewStatus.IN_PROGRESS

    async def test_without_the_flag_no_review_status_moves(self, monkeypatch):
        harness = _harness(monkeypatch)
        ref = await _seed_review(harness, 1, status="triaged")
        body = await _create_batch(harness, [ref])
        assert body["planned_review_ids"] == []
        assert body["unchanged_review_ids"] == [ref[0]]
        assert (await harness.repository.get_review(ref[0])).status is ReviewStatus.TRIAGED

    async def test_creation_records_the_prompt_template_digest(self, monkeypatch):
        harness = _harness(monkeypatch)
        ref = await _seed_review(harness, 1)
        body = await _create_batch(harness, [ref])
        assert body["batch"]["prompt_template_sha256"] == template_sha256()
        assert body["batch"]["prompt_template_version"] == PROMPT_TEMPLATE_VERSION


# =====================================================================
# 7. Role separation
# =====================================================================


class TestRoleSeparation:

    @pytest.mark.parametrize("role", [ReviewerRole.VIEWER, ReviewerRole.REVIEWER])
    async def test_a_viewer_or_reviewer_cannot_create_a_batch(self, monkeypatch, role):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        harness.act_as(role)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-role-create-1"),
            json={"review_refs": [{"review_id": review_id, "review_version": version}]},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == CODE_FORBIDDEN

    @pytest.mark.parametrize("role", [ReviewerRole.REMEDIATOR, ReviewerRole.ADMIN])
    async def test_a_remediator_or_admin_can_create_a_batch(self, monkeypatch, role):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        harness.act_as(role)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-role-create-2"),
            json={
                "review_refs": [
                    {"review_id": review_id, "review_version": version}
                    for review_id, version in refs
                ]
            },
        )
        assert response.status_code == 201, response.text

    @pytest.mark.parametrize(
        "role",
        [
            ReviewerRole.VIEWER,
            ReviewerRole.REVIEWER,
            ReviewerRole.REMEDIATOR,
            ReviewerRole.ADMIN,
        ],
    )
    async def test_no_human_may_claim_a_batch(self, monkeypatch, role):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        harness.act_as(role)
        response = harness.client.post(
            _batch_url(batch["batch_id"], "/claim"),
            headers=_human_write_headers(harness, idempotency="idem-human-claim-1"),
            json=None,
        )
        assert response.status_code == 403

    async def test_the_agent_cannot_ready_or_cancel_a_batch(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        for suffix, body in (
            (":ready", {"expected_version": created["batch"]["version"]}),
            (
                ":cancel",
                {"expected_version": created["batch"]["version"], "reason": "no"},
            ),
        ):
            harness.act_as(ReviewerRole.AGENT)
            response = harness.client.post(
                _batch_url(created["batch"]["batch_id"], suffix),
                headers=_agent_write_headers(idempotency=f"idem-agent-{suffix[1:]}-1"),
                json=body,
            )
            assert response.status_code == 403, (suffix, response.text)

    async def test_a_viewer_cannot_read_a_batch(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        harness.act_as(ReviewerRole.VIEWER)
        response = harness.client.get(
            _batch_url(created["batch"]["batch_id"]), headers=_auth()
        )
        assert response.status_code == 403

    async def test_only_an_admin_may_extend_a_lease(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":extend-lease"),
            headers=_human_write_headers(harness, idempotency="idem-extend-deny-1"),
            json={
                "expected_version": claim["batch"]["version"],
                "additional_minutes": 30,
                "reason": "long run",
            },
        )
        assert response.status_code == 403


# =====================================================================
# 8. Claim, lease token, and canonical values
# =====================================================================


class TestClaimAndLease:

    async def test_the_claim_returns_a_token_and_stores_only_its_hash(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        token = claim["lease_token"]
        assert token and isinstance(token, str)
        assert claim["batch"]["status"] == BatchStatus.CLAIMED.value

        stored = await harness.repository.backend.dump_collection(BATCHES_COLLECTION)
        assert token not in str(stored)
        import hashlib

        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert expected in str(stored)

    async def test_the_claim_publishes_the_canonical_lease_values(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        assert claim["heartbeat_interval_s"] == REMEDIATION_HEARTBEAT_S
        assert claim["max_continuous_lease_s"] == REMEDIATION_MAX_CONTINUOUS_LEASE_S
        acquired = datetime.fromisoformat(claim["batch"]["lease"]["acquired_at"])
        expires = datetime.fromisoformat(claim["lease_expires_at"])
        assert (expires - acquired).total_seconds() == pytest.approx(
            REMEDIATION_LEASE_S, abs=1
        )

    async def test_no_later_response_repeats_the_lease_token(self, monkeypatch):
        """The plaintext exists exactly once, in the claim response."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        token = claim["lease_token"]

        harness.act_as(ReviewerRole.AGENT)
        beat = harness.client.post(
            _batch_url(batch["batch_id"], "/heartbeat"),
            headers=_agent_write_headers(idempotency="idem-beat-token-1"),
            json={"expected_version": claim["batch"]["version"], "lease_token": token},
        )
        assert beat.status_code == 200, beat.text
        assert token not in beat.text

        harness.act_as(ReviewerRole.REVIEWER)
        read = harness.client.get(_batch_url(batch["batch_id"]), headers=_auth())
        assert read.status_code == 200
        assert token not in read.text

    async def test_a_human_read_never_exposes_the_lease_token_hash(self, monkeypatch):
        """Item 12: an object-scoped reviewer sees the window, not the credential."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        await _claim(harness, batch)

        harness.act_as(ReviewerRole.REVIEWER)
        read = harness.client.get(_batch_url(batch["batch_id"]), headers=_auth())
        assert read.status_code == 200
        lease = read.json()["lease"]
        assert set(lease) == {
            "holder",
            "acquired_at",
            "expires_at",
            "last_heartbeat_at",
            "continuous_since",
        }
        assert "lease_token_hash" not in read.text

    async def test_a_second_claim_while_the_lease_lives_is_a_conflict(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        await _claim(harness, batch)

        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], "/claim"),
            headers=_agent_write_headers(idempotency="idem-second-claim-01"),
            json=None,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == CODE_BATCH_ALREADY_CLAIMED

    async def test_a_replayed_claim_returns_the_same_usable_token(self, monkeypatch):
        """The token is derived, so an idempotent retry is not a dead credential."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        first = await _claim(harness, batch, key="idem-replay-claim-01")
        second = await _claim(harness, batch, key="idem-replay-claim-01")
        assert first["lease_token"] == second["lease_token"]
        assert second["batch"]["version"] == first["batch"]["version"]

    async def test_a_wrong_lease_token_cannot_write(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": "not-the-real-token",
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-wrong-token-001",
            expect=409,
        )
        assert body["error"]["code"] == CODE_BATCH_LEASE_LOST

    async def test_an_expired_lease_cannot_write(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        harness.clock.advance(timedelta(seconds=REMEDIATION_LEASE_S + 1))
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-expired-lease-01",
            expect=409,
        )
        assert body["error"]["code"] == CODE_BATCH_LEASE_LOST

    async def test_the_extension_is_admin_only_bounded_and_once(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.ADMIN)
        first = harness.client.post(
            _batch_url(batch["batch_id"], ":extend-lease"),
            headers=_human_write_headers(harness, idempotency="idem-extend-once-01"),
            json={
                "expected_version": claim["batch"]["version"],
                "additional_minutes": 30,
                "reason": "the suite is slow today",
            },
        )
        assert first.status_code == 200, first.text

        harness.act_as(ReviewerRole.ADMIN)
        second = harness.client.post(
            _batch_url(batch["batch_id"], ":extend-lease"),
            headers=_human_write_headers(harness, idempotency="idem-extend-twice-1"),
            json={
                "expected_version": first.json()["version"],
                "additional_minutes": 30,
                "reason": "again",
            },
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == CODE_BATCH_REJECTED


# =====================================================================
# 9, 10, 16. The agent's write path
# =====================================================================


class TestAgentWritePath:

    async def test_a_stale_expected_version_is_412_with_the_current_one(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"] + 7,
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-stale-version-01",
            expect=412,
        )
        assert body["error"]["code"] == CODE_BATCH_VERSION_CONFLICT
        assert body["error"]["current_version"] == claim["batch"]["version"]

    async def test_a_patch_without_a_claim_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": batch["version"],
                "lease_token": "invented-token",
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-noclaim-patch-1",
            expect=409,
        )
        assert body["error"]["code"] == CODE_BATCH_LEASE_LOST

    @pytest.mark.parametrize(
        "missing",
        ["branch", "commit_sha", "changed_files", "test_evidence", "summary"],
    )
    async def test_submission_requires_the_whole_handoff(self, monkeypatch, missing):
        """Item 10: each part is something the verifier cannot reconstruct."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-handoff-plan-01",
        )
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.IN_PROGRESS.value,
            },
            key="idem-handoff-prog-01",
        )
        complete = {
            "branch": "fix/x",
            "commit_sha": "c" * 40,
            "changed_files": ["kb-rag-system/api/x.py"],
            "test_evidence": [_evidence()],
            "summary": "did the thing",
        }
        complete.pop(missing)
        result = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.CHANGES_PROPOSED.value,
                "per_review_outcomes": [
                    {"review_id": review_id, "outcome": "fixed"}
                    for review_id, _ in refs
                ],
                **complete,
            },
            key="idem-handoff-sub-001",
            expect=409,
        )
        assert result["error"]["code"] == CODE_BATCH_CONFLICT

    async def test_an_uncommitted_reason_substitutes_for_a_commit(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-uncommit-plan-1",
        )
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.IN_PROGRESS.value,
            },
            key="idem-uncommit-prog-1",
        )
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.CHANGES_PROPOSED.value,
                "branch": "fix/x",
                "uncommitted_reason": "the suite fails; the diff is left in the tree",
                "changed_files": ["kb-rag-system/api/x.py"],
                "test_evidence": [_evidence()],
                "summary": "left uncommitted deliberately",
                "per_review_outcomes": [
                    {"review_id": review_id, "outcome": "blocked"}
                    for review_id, _ in refs
                ],
            },
            key="idem-uncommit-sub-01",
        )
        assert body["status"] == BatchStatus.CHANGES_PROPOSED.value
        assert body["uncommitted_reason"]

    async def test_a_silent_review_fails_the_submission(self, monkeypatch):
        """Every frozen review needs a verdict; silence is not a verdict."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, index) for index in (1, 2)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
            },
            key="idem-silent-plan-001",
        )
        body = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.IN_PROGRESS.value,
            },
            key="idem-silent-prog-001",
        )
        result = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.CHANGES_PROPOSED.value,
                "branch": "fix/x",
                "commit_sha": "c" * 40,
                "changed_files": ["kb-rag-system/api/x.py"],
                "test_evidence": [_evidence()],
                "summary": "did one of them",
                "per_review_outcomes": [
                    {"review_id": refs[0][0], "outcome": "fixed"}
                ],
            },
            key="idem-silent-sub-0001",
            expect=409,
        )
        assert "no per-review outcome" in result["error"]["message"]

    async def test_submission_records_outcomes_and_invalidates_the_lease(self, monkeypatch):
        """Item 16: atomic results, one transition, and the claim ends."""
        harness = _harness(monkeypatch)
        body, token, review_ids = await _submitted(harness)

        assert body["status"] == BatchStatus.CHANGES_PROPOSED.value
        assert body["lease"] is None
        assert body["changed_files"] == ["kb-rag-system/data_pipeline/rag_engine.py"]

        items = await harness.repository.list_batch_items(body["batch_id"])
        assert {item.outcome for item in items.items} == {"fixed"}
        assert all(item.outcome_summary == "covered" for item in items.items)

        # No review was resolved by the submission alone.
        for review_id in review_ids:
            review = await harness.repository.get_review(review_id)
            assert review.status is not ReviewStatus.RESOLVED
            assert review.resolution is None

        # The invalidated lease really is gone.
        follow_up = await _agent_patch(
            harness,
            body["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": token,
                "transition": BatchStatus.IN_PROGRESS.value,
            },
            key="idem-after-submit-01",
            expect=409,
        )
        assert follow_up["error"]["code"] == CODE_BATCH_LEASE_LOST

    @pytest.mark.parametrize(
        "target", [BatchStatus.VERIFYING.value, BatchStatus.COMPLETED.value]
    )
    async def test_the_agent_cannot_verify_or_complete(self, monkeypatch, target):
        """Item 15/16: the agent's path stops at ``changes_proposed``."""
        harness = _harness(monkeypatch)
        body, token, _ = await _submitted(harness)
        result = await _agent_patch(
            harness,
            body["batch_id"],
            {
                "expected_version": body["version"],
                "lease_token": token,
                "transition": target,
            },
            key=f"idem-agent-{target[:8]}-1",
            expect=409,
        )
        assert result["error"]["code"] == CODE_BATCH_CONFLICT

    async def test_an_unapproved_transition_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        # claimed -> in_progress skips `planning`, which the table refuses.
        result = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.IN_PROGRESS.value,
            },
            key="idem-skip-planning-1",
            expect=409,
        )
        assert result["error"]["code"] == CODE_BATCH_CONFLICT


# =====================================================================
# 15. Release
# =====================================================================


class TestRelease:

    async def test_release_to_ready_is_available_before_durable_work(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":release"),
            headers=_agent_write_headers(idempotency="idem-release-ready-1"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "disposition": BatchStatus.READY.value,
                "reason": "another agent should take this",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == BatchStatus.READY.value
        assert body["lease"] is None

    async def test_release_to_ready_is_refused_once_work_exists(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        planned = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
                "plan_artifact": "docs/plans/synthetic.md",
            },
            key="idem-release-plan-01",
        )
        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":release"),
            headers=_agent_write_headers(idempotency="idem-release-deny-01"),
            json={
                "expected_version": planned["version"],
                "lease_token": claim["lease_token"],
                "disposition": BatchStatus.READY.value,
                "reason": "changed my mind",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == CODE_BATCH_REJECTED

    async def test_release_to_blocked_always_works_and_leaves_no_lease(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        planned = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
                "plan_artifact": "docs/plans/synthetic.md",
            },
            key="idem-block-plan-0001",
        )
        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":release"),
            headers=_agent_write_headers(idempotency="idem-block-release-1"),
            json={
                "expected_version": planned["version"],
                "lease_token": claim["lease_token"],
                "disposition": BatchStatus.BLOCKED.value,
                "reason": "needs a product decision about the plan document",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == BatchStatus.BLOCKED.value
        assert body["lease"] is None

        # A blocked batch is reclaimable: no active state is stranded.
        harness.act_as(ReviewerRole.REMEDIATOR)
        again = harness.client.post(
            _batch_url(batch["batch_id"], ":ready"),
            headers=_human_write_headers(harness, idempotency="idem-reready-block-1"),
            json={"expected_version": body["version"]},
        )
        assert again.status_code == 200, again.text
        assert again.json()["status"] == BatchStatus.READY.value

    async def test_a_disposition_outside_the_closed_set_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":release"),
            headers=_agent_write_headers(idempotency="idem-bad-disposition"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "disposition": BatchStatus.COMPLETED.value,
                "reason": "nice try",
            },
        )
        assert response.status_code == 422


# =====================================================================
# 11, 17. Materialization
# =====================================================================


class TestMaterialization:

    async def test_materialization_needs_a_valid_lease(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":materialize"),
            headers=_agent_write_headers(idempotency="idem-mat-nolease-01"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": "wrong-token",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == CODE_BATCH_LEASE_LOST

    async def test_materialization_excludes_conversation_by_default(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, _ = await _seed_review(harness, 1)
        secret = "A confidential participant sentence."
        review = await harness.repository.get_review(review_id)
        await harness.repository.patch_review(
            review_id,
            ReviewPatch(comments=secret),
            expected_version=review.version,
            context=_context(
                harness, EMAILS[ReviewerRole.ADMIN], ReviewerRole.ADMIN, "mat-comment"
            ),
        )
        refreshed = await harness.repository.get_review(review_id)
        created = await _create_batch(harness, [(review_id, refreshed.version)])
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":materialize"),
            headers=_agent_write_headers(idempotency="idem-mat-default-01"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert body["conversation_included"] is False
        assert secret not in response.text

        harness.act_as(ReviewerRole.AGENT)
        widened = harness.client.post(
            _batch_url(batch["batch_id"], ":materialize"),
            headers=_agent_write_headers(idempotency="idem-mat-widened-01"),
            json={
                "expected_version": body["batch_version"],
                "lease_token": claim["lease_token"],
                "include_conversation": True,
            },
        )
        assert widened.status_code == 200, widened.text
        assert widened.json()["conversation_included"] is True
        assert secret in widened.text

    async def test_materialization_emits_a_read_audit_event(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":materialize"),
            headers=_agent_write_headers(idempotency="idem-mat-audit-001"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
            },
        )
        assert response.status_code == 200, response.text
        events = await harness.repository.list_batch_events(batch["batch_id"])
        assert "batch_materialized" in [event.event_type for event in events.items]
        # A read does not move the business version.
        assert response.json()["batch_version"] == claim["batch"]["version"]

    async def test_a_drifted_review_is_reported_not_hidden(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        created = await _create_batch(harness, [(review_id, version)])
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        # Move the review out from under the frozen snapshot.
        await harness.repository.patch_review(
            review_id,
            ReviewPatch(comments="edited after freezing"),
            expected_version=version,
            context=_context(
                harness, EMAILS[ReviewerRole.ADMIN], ReviewerRole.ADMIN, "drift-1"
            ),
        )

        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(batch["batch_id"], ":materialize"),
            headers=_agent_write_headers(idempotency="idem-mat-drift-001"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["drifted_review_ids"] == [review_id]
        assert any("drift" in warning for warning in body["warnings"])

    async def test_materialization_pages_a_hundred_items_within_the_byte_budget(
        self, monkeypatch
    ):
        """Item 17: the worst case is paged, and no page approaches the limit."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, index) for index in range(1, 101)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)
        assert created["batch"]["item_count"] == MAX_BATCH_REVIEWS

        harness.act_as(ReviewerRole.AGENT)
        seen: list[str] = []
        cursor = None
        version = claim["batch"]["version"]
        for page_index in range(10):
            headers = _agent_write_headers(idempotency=f"idem-mat-page-{page_index:04d}")
            if cursor is not None:
                headers["X-Tickets-Cursor"] = cursor
            response = harness.client.post(
                f"{_batch_url(batch['batch_id'], ':materialize')}?page_size=25",
                headers=headers,
                json={"expected_version": version, "lease_token": claim["lease_token"]},
            )
            assert response.status_code == 200, response.text
            # Well inside Firestore's 1 MiB document limit and any sane API bound.
            assert len(response.content) < 512 * 1024
            body = response.json()
            seen.extend(item["review_id"] for item in body["items"])
            cursor = body["next_cursor"]
            assert body["partial"] is (cursor is not None)
            if cursor is None:
                break
            harness.act_as(ReviewerRole.AGENT)
        assert len(seen) == MAX_BATCH_REVIEWS
        assert len(set(seen)) == MAX_BATCH_REVIEWS

    async def test_a_hundred_frozen_items_never_grow_the_parent_document(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, index) for index in range(1, 101)]
        created = await _create_batch(harness, refs)
        parents = await harness.repository.backend.dump_collection(BATCHES_COLLECTION)
        parent = parents[created["batch"]["batch_id"]]
        assert "items" not in parent and "review_refs" not in parent
        items = await harness.repository.backend.list_subcollection(
            (BATCHES_COLLECTION, created["batch"]["batch_id"]),
            BATCH_ITEMS_SUBCOLLECTION,
            limit=MAX_BATCH_REVIEWS + 1,
        )
        assert len(items) == MAX_BATCH_REVIEWS


# =====================================================================
# 12. Independent verification owns completion
# =====================================================================


class TestIndependentVerification:

    async def test_verification_and_completion_close_the_loop(self, monkeypatch):
        harness = _harness(monkeypatch)
        body, _, review_ids = await _submitted(harness)

        # The reviews travel their own lifecycle, through the review endpoint the
        # reviewer already has. The batch does not drag them: a review reaches
        # `verifying` because a person moved it there, which is what makes the
        # final `resolved` step defensible.
        await _advance_reviews_to_verifying(harness, review_ids)

        harness.act_as(ReviewerRole.REVIEWER)
        started = harness.client.post(
            _batch_url(body["batch_id"], ":start-verification"),
            headers=_human_write_headers(harness, idempotency="idem-verify-start-1"),
            json={
                "expected_version": body["version"],
                "independent_verifier_attestation": "I did not author these changes.",
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["status"] == BatchStatus.VERIFYING.value
        assert started.json()["verification_attestation"]

        harness.act_as(ReviewerRole.REVIEWER)
        completed = harness.client.post(
            _batch_url(body["batch_id"], ":complete"),
            headers=_human_write_headers(harness, idempotency="idem-verify-done-01"),
            json={
                "expected_version": started.json()["version"],
                "decision": "accepted",
                "verification_evidence": [_evidence("pytest tests/ -q")],
                "per_review_decisions": [
                    {
                        "review_id": review_id,
                        "decision": "fixed",
                        "resolution": {
                            "outcome": "fixed",
                            "verification_summary": "Re-ran the suite on the branch.",
                        },
                    }
                    for review_id in review_ids
                ],
            },
        )
        assert completed.status_code == 200, completed.text
        result = completed.json()
        assert result["batch"]["status"] == BatchStatus.COMPLETED.value
        assert sorted(result["planned_review_ids"]) == sorted(review_ids)

        for review_id in review_ids:
            review = await harness.repository.get_review(review_id)
            assert review.status is ReviewStatus.RESOLVED
            assert review.resolution is not None
            assert review.resolution.batch_id == body["batch_id"]
            assert review.resolution.verified_by is not None
            assert review.resolution.test_evidence, "resolution carries the evidence"

    async def test_a_review_the_batch_cannot_legally_close_is_reported_not_forced(
        self, monkeypatch
    ):
        """The batch's verdict does not outrank a review's own lifecycle.

        A review still at ``in_progress`` cannot reach ``resolved`` in one step, so
        completion records the batch decision, marks the item's verified outcome,
        and names the review as unchanged. Forcing it would put a review into a
        terminal state it never legally entered — and the audit trail would then
        show a resolution with no path to it.
        """
        harness = _harness(monkeypatch)
        body, _, review_ids = await _submitted(harness)

        harness.act_as(ReviewerRole.REVIEWER)
        started = harness.client.post(
            _batch_url(body["batch_id"], ":start-verification"),
            headers=_human_write_headers(harness, idempotency="idem-unforced-st-01"),
            json={
                "expected_version": body["version"],
                "independent_verifier_attestation": "I read the diff on the branch.",
            },
        )
        assert started.status_code == 200, started.text

        harness.act_as(ReviewerRole.REVIEWER)
        completed = harness.client.post(
            _batch_url(body["batch_id"], ":complete"),
            headers=_human_write_headers(harness, idempotency="idem-unforced-dn-01"),
            json={
                "expected_version": started.json()["version"],
                "decision": "accepted",
                "verification_evidence": [_evidence()],
                "per_review_decisions": [
                    {
                        "review_id": review_id,
                        "decision": "fixed",
                        "resolution": {"outcome": "fixed", "verification_summary": "ok"},
                    }
                    for review_id in review_ids
                ],
            },
        )
        assert completed.status_code == 200, completed.text
        result = completed.json()
        assert result["batch"]["status"] == BatchStatus.COMPLETED.value
        assert result["planned_review_ids"] == []
        assert sorted(result["unchanged_review_ids"]) == sorted(review_ids)
        for review_id in review_ids:
            review = await harness.repository.get_review(review_id)
            assert review.status is ReviewStatus.IN_PROGRESS
            assert review.resolution is None
        # The human verdict is still recorded against each frozen item.
        items = await harness.repository.list_batch_items(body["batch_id"])
        assert {item.verified_outcome for item in items.items} == {"fixed"}

    async def test_the_claimer_may_not_verify_its_own_batch(self, monkeypatch):
        """The agent is the claimer, and no human may borrow that identity."""
        harness = _harness(monkeypatch)
        body, _, _ = await _submitted(harness)
        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.post(
            _batch_url(body["batch_id"], ":start-verification"),
            headers=_agent_write_headers(idempotency="idem-agent-verify-01"),
            json={
                "expected_version": body["version"],
                "independent_verifier_attestation": "trust me",
            },
        )
        assert response.status_code == 403

    async def test_completion_requires_verification_first(self, monkeypatch):
        harness = _harness(monkeypatch)
        body, _, review_ids = await _submitted(harness)
        harness.act_as(ReviewerRole.REVIEWER)
        response = harness.client.post(
            _batch_url(body["batch_id"], ":complete"),
            headers=_human_write_headers(harness, idempotency="idem-skip-verify-01"),
            json={
                "expected_version": body["version"],
                "decision": "accepted",
                "verification_evidence": [_evidence()],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == CODE_BATCH_CONFLICT

    async def test_a_resolved_decision_without_evidence_is_refused(self, monkeypatch):
        """Item 12: ``resolved`` is never implied by the agent's own claim."""
        harness = _harness(monkeypatch)
        body, _, review_ids = await _submitted(harness)

        harness.act_as(ReviewerRole.REVIEWER)
        started = harness.client.post(
            _batch_url(body["batch_id"], ":start-verification"),
            headers=_human_write_headers(harness, idempotency="idem-noev-start-01"),
            json={
                "expected_version": body["version"],
                "independent_verifier_attestation": "I reviewed the diff.",
            },
        )
        assert started.status_code == 200, started.text

        harness.act_as(ReviewerRole.REVIEWER)
        response = harness.client.post(
            _batch_url(body["batch_id"], ":complete"),
            headers=_human_write_headers(harness, idempotency="idem-noev-done-001"),
            json={
                "expected_version": started.json()["version"],
                "decision": "accepted",
                "verification_evidence": [],
                "per_review_decisions": [
                    {
                        "review_id": review_ids[0],
                        "decision": "fixed",
                        "resolution": {
                            "outcome": "fixed",
                            "verification_summary": "looks right",
                        },
                    }
                ],
            },
        )
        assert response.status_code == 422

    async def test_a_reviewer_reading_a_batch_sees_the_items_but_no_conversation(
        self, monkeypatch
    ):
        harness = _harness(monkeypatch)
        body, _, _ = await _submitted(harness)
        harness.act_as(ReviewerRole.REVIEWER)
        response = harness.client.get(
            f"{_batch_url(body['batch_id'], '/items')}", headers=_auth()
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert items and all(item["comments_excerpt"] is None for item in items)
        assert response.headers["Cache-Control"] == "no-store"


# =====================================================================
# 5, 6. The copied prompt
# =====================================================================


class TestPromptEndpoint:

    async def test_the_prompt_is_plain_text_uncacheable_and_identifying(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.get(
            _batch_url(created["batch"]["batch_id"], "/prompt"), headers=_auth()
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert response.headers["Cache-Control"] == "no-store"
        text = response.text
        for expected in (
            CONSOLE_ORIGIN,
            "local",
            created["batch"]["batch_id"],
            REPO_ID,
            BASE_REF,
            "scripts/ticket_review_cli.py",
        ):
            assert expected in text, expected

    async def test_the_prompt_names_no_local_path_the_server_inferred(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        harness.act_as(ReviewerRole.REMEDIATOR)
        text = harness.client.get(
            _batch_url(created["batch"]["batch_id"], "/prompt"), headers=_auth()
        ).text
        # No server filesystem path, and nothing that looks like one.
        for absent in ("/Users/", "/home/", "/app", "/workspace", "/tmp"):
            assert absent not in text, absent

    async def test_the_prompt_contains_no_record_or_credential(self, monkeypatch):
        """Item 6: identifiers and instructions, never content or secrets."""
        harness = _harness(monkeypatch)
        review_id, _ = await _seed_review(harness, 1)
        secret_comment = "Reviewer wrote something private about a participant."
        review = await harness.repository.get_review(review_id)
        await harness.repository.patch_review(
            review_id,
            ReviewPatch(comments=secret_comment, topic="Withdrawals"),
            expected_version=review.version,
            context=_context(
                harness, EMAILS[ReviewerRole.ADMIN], ReviewerRole.ADMIN, "prompt-1"
            ),
        )
        refreshed = await harness.repository.get_review(review_id)
        created = await _create_batch(harness, [(review_id, refreshed.version)])

        harness.act_as(ReviewerRole.REMEDIATOR)
        text = harness.client.get(
            _batch_url(created["batch"]["batch_id"], "/prompt"), headers=_auth()
        ).text
        for absent in (
            secret_comment,
            "Withdrawals",
            refreshed.devrev_display_id,
            refreshed.devrev_work_id,
            review_id,
            CSRF_SECRET,
            CURSOR_KEY_B64,
            IAP_AUDIENCE,
            ASSERTION,
        ):
            assert absent not in text, absent

    async def test_the_prompt_states_that_records_are_not_instructions(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        harness.act_as(ReviewerRole.REMEDIATOR)
        text = harness.client.get(
            _batch_url(created["batch"]["batch_id"], "/prompt"), headers=_auth()
        ).text
        assert "data, not authority" in text
        assert "bypass these instructions" in text

    async def test_the_prompt_size_does_not_depend_on_the_records(self, monkeypatch):
        """Step 5: constant-size — a function of the template and five ids."""
        harness = _harness(monkeypatch)
        small = await _create_batch(
            harness, [await _seed_review(harness, 1)], key="idem-prompt-small-01"
        )
        many = [await _seed_review(harness, index) for index in range(2, 40)]
        large = await _create_batch(harness, many, key="idem-prompt-large-01")

        harness.act_as(ReviewerRole.REMEDIATOR)
        first = harness.client.get(
            _batch_url(small["batch"]["batch_id"], "/prompt"), headers=_auth()
        ).text
        harness.act_as(ReviewerRole.REMEDIATOR)
        second = harness.client.get(
            _batch_url(large["batch"]["batch_id"], "/prompt"), headers=_auth()
        ).text
        assert len(first) == len(second)
        assert first.replace(small["batch"]["batch_id"], "") == second.replace(
            large["batch"]["batch_id"], ""
        )

    async def test_a_viewer_cannot_copy_the_prompt(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        harness.act_as(ReviewerRole.VIEWER)
        response = harness.client.get(
            _batch_url(created["batch"]["batch_id"], "/prompt"), headers=_auth()
        )
        assert response.status_code == 403


# =====================================================================
# 13. Cross-cutting: CSRF, idempotency, ETag, audit, BOLA
# =====================================================================


class TestCrossCuttingGuards:

    async def test_exactly_the_fourteen_documented_batch_operations_exist(
        self, monkeypatch
    ):
        """The master route table's closed list, no more and no fewer.

        Counted as ``(path, method)`` pairs rather than paths: ``GET`` and
        ``PATCH`` on ``/{batch_id}`` are two operations on one path, so counting
        paths would report thirteen and hide a missing route.
        """
        harness = _harness(monkeypatch)
        operations = {
            (path, method.upper())
            for path, methods in harness.client.app.openapi()["paths"].items()
            if "remediation-batches" in path
            for method in methods
        }
        base = f"{API_PREFIX}{BATCHES_PATH}"
        assert operations == {
            (base, "POST"),
            (f"{base}/{{batch_id}}", "GET"),
            (f"{base}/{{batch_id}}", "PATCH"),
            (f"{base}/{{batch_id}}/items", "GET"),
            (f"{base}/{{batch_id}}/prompt", "GET"),
            (f"{base}/{{batch_id}}/claim", "POST"),
            (f"{base}/{{batch_id}}/heartbeat", "POST"),
            (f"{base}/{{batch_id}}:ready", "POST"),
            (f"{base}/{{batch_id}}:cancel", "POST"),
            (f"{base}/{{batch_id}}:materialize", "POST"),
            (f"{base}/{{batch_id}}:release", "POST"),
            (f"{base}/{{batch_id}}:start-verification", "POST"),
            (f"{base}/{{batch_id}}:complete", "POST"),
            (f"{base}/{{batch_id}}:extend-lease", "POST"),
        }

    async def test_a_browser_write_without_csrf_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        harness.act_as(ReviewerRole.REMEDIATOR)
        headers = _human_write_headers(harness, idempotency="idem-nocsrf-create1")
        headers.pop(CSRF_HEADER)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=headers,
            json={"review_refs": [{"review_id": review_id, "review_version": version}]},
        )
        assert response.status_code == 403

    async def test_a_write_without_an_idempotency_key_is_refused(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        harness.act_as(ReviewerRole.REMEDIATOR)
        headers = _human_write_headers(harness, idempotency="idem-noidem-create1")
        headers.pop(IDEMPOTENCY_HEADER)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=headers,
            json={"review_refs": [{"review_id": review_id, "review_version": version}]},
        )
        assert response.status_code == 400

    async def test_the_agent_still_needs_an_idempotency_key(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        harness.act_as(ReviewerRole.AGENT)
        headers = _agent_write_headers(idempotency="placeholder")
        headers.pop(IDEMPOTENCY_HEADER)
        response = harness.client.post(
            _batch_url(batch["batch_id"], "/claim"), headers=headers, json=None
        )
        assert response.status_code == 400

    async def test_the_agent_exemption_covers_only_the_agent_routes(self, monkeypatch):
        """A human-only action never inherits the CLI's waiver."""
        policy = policy_from_settings(_settings(monkeypatch))
        from dataclasses import replace

        policy = replace(policy, agent_route_templates=AGENT_ROUTE_TEMPLATES)
        batch_id = "a" * 32
        for allowed in ("/claim", "/heartbeat", ":materialize", ":release", ""):
            assert agent_route_allowlisted(
                policy, _batch_url(batch_id, allowed)
            ), allowed
        for denied in (":ready", ":cancel", ":start-verification", ":complete", ":extend-lease"):
            assert not agent_route_allowlisted(
                policy, _batch_url(batch_id, denied)
            ), denied
        assert not agent_route_allowlisted(policy, f"{API_PREFIX}/reviews")

    async def test_an_agent_route_template_matches_only_a_real_batch_id(self, monkeypatch):
        policy = policy_from_settings(_settings(monkeypatch))
        from dataclasses import replace

        policy = replace(policy, agent_route_templates=AGENT_ROUTE_TEMPLATES)
        assert agent_route_allowlisted(policy, _batch_url("b" * 32, "/claim"))
        for bogus in ("../../reviews", "not-a-batch-id", "b" * 31, "B" * 32):
            assert not agent_route_allowlisted(policy, _batch_url(bogus, "/claim")), bogus

    async def test_a_write_response_carries_a_quoted_etag(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-etag-create-1"),
            json={
                "review_refs": [
                    {"review_id": review_id, "review_version": version}
                    for review_id, version in refs
                ]
            },
        )
        assert response.status_code == 201
        assert response.headers["ETag"] == '"v1"'
        assert response.headers["Cache-Control"] == "no-store"

    async def test_a_reused_idempotency_key_with_a_different_body_conflicts(
        self, monkeypatch
    ):
        harness = _harness(monkeypatch)
        first = await _seed_review(harness, 1)
        second = await _seed_review(harness, 2)
        await _create_batch(harness, [first], key="idem-shared-key-0001")
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-shared-key-0001"),
            json={
                "review_refs": [
                    {"review_id": second[0], "review_version": second[1]}
                ]
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async def test_every_batch_mutation_appends_an_audit_event(self, monkeypatch):
        harness = _harness(monkeypatch)
        body, _, _ = await _submitted(harness)
        events = await harness.repository.list_batch_events(body["batch_id"], page_size=50)
        types = [event.event_type for event in events.items]
        assert types[:1] == ["batch_created"]
        for expected in (
            "batch_readied",
            "batch_claimed",
            "batch_updated",
            "batch_submitted",
        ):
            assert expected in types, expected
        report = await harness.repository.verify_audit_chain(body["batch_id"])
        assert report.intact

    async def test_no_audit_event_records_the_lease_token(self, monkeypatch):
        harness = _harness(monkeypatch)
        body, token, _ = await _submitted(harness)
        events = await harness.repository.list_batch_events(body["batch_id"], page_size=50)
        assert token not in str([event.model_dump() for event in events.items])

    async def test_a_batch_id_that_is_not_one_is_refused_before_any_lookup(
        self, monkeypatch
    ):
        harness = _harness(monkeypatch)
        harness.act_as(ReviewerRole.REMEDIATOR)
        for bogus in ("..", "not-a-batch", "a" * 31, "%2e%2e"):
            response = harness.client.get(_batch_url(bogus), headers=_auth())
            assert response.status_code in {404, 422}, (bogus, response.status_code)

    async def test_an_absent_batch_is_404_for_every_role(self, monkeypatch):
        harness = _harness(monkeypatch)
        for role in (ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR, ReviewerRole.ADMIN):
            harness.act_as(role)
            response = harness.client.get(_batch_url("d" * 32), headers=_auth())
            assert response.status_code == 404, role

    async def test_an_agent_may_read_an_unclaimed_batch(self, monkeypatch):
        """There is no holder to protect, and it has to see what it will claim.

        The documented order is ``batch show`` then ``batch claim``; refusing here
        would make the first command of every agent run fail.
        """
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        harness.act_as(ReviewerRole.AGENT)
        response = harness.client.get(_batch_url(batch["batch_id"]), headers=_auth())
        assert response.status_code == 200, response.text
        assert response.json()["status"] == BatchStatus.READY.value

    async def test_an_agent_cannot_read_a_batch_another_holder_claimed(self, monkeypatch):
        """Object scoping: a 404, so an id cannot be probed for existence."""
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        await _claim(harness, batch)

        # A second, differently-named agent identity: same role, different holder.
        other = "other-remediation-agent@rag-kb-system.iam.gserviceaccount.com"
        harness.client.app.state.settings.AGENT_SERVICE_ACCOUNT = other
        harness.client.app.state.authenticator._agent_account = other  # noqa: SLF001
        harness.act_as(other)
        response = harness.client.get(_batch_url(batch["batch_id"]), headers=_auth())
        assert response.status_code == 404

    async def test_the_access_log_template_never_carries_a_batch_id(self, monkeypatch):
        batch_id = "e" * 32
        for suffix in ("", "/items", "/prompt", "/claim", ":materialize", ":complete"):
            template = safe_route_template(_batch_url(batch_id, suffix))
            assert batch_id not in template, suffix
            assert "remediation-batches" in template, suffix

    async def test_no_batch_response_contains_a_configuration_secret(self, monkeypatch):
        harness = _harness(monkeypatch)
        body, _, _ = await _submitted(harness)
        harness.act_as(ReviewerRole.ADMIN)
        for path in (_batch_url(body["batch_id"]), _batch_url(body["batch_id"], "/items")):
            response = harness.client.get(path, headers=_auth())
            assert response.status_code == 200, path
            for secret in (CSRF_SECRET, CURSOR_KEY_B64, IAP_AUDIENCE, ASSERTION):
                assert secret not in response.text, (path, secret)


# =====================================================================
# 14. Human transitions
# =====================================================================


class TestHumanTransitions:

    async def test_ready_requires_the_expected_version(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            _batch_url(created["batch"]["batch_id"], ":ready"),
            headers=_human_write_headers(harness, idempotency="idem-ready-stale-01"),
            json={"expected_version": created["batch"]["version"] + 3},
        )
        assert response.status_code == 412
        assert response.json()["error"]["code"] == CODE_BATCH_VERSION_CONFLICT

    async def test_ready_refuses_a_batch_whose_reviews_drifted(self, monkeypatch):
        harness = _harness(monkeypatch)
        review_id, version = await _seed_review(harness, 1)
        created = await _create_batch(harness, [(review_id, version)])
        await harness.repository.patch_review(
            review_id,
            ReviewPatch(comments="moved after freezing"),
            expected_version=version,
            context=_context(
                harness, EMAILS[ReviewerRole.ADMIN], ReviewerRole.ADMIN, "ready-drift"
            ),
        )
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            _batch_url(created["batch"]["batch_id"], ":ready"),
            headers=_human_write_headers(harness, idempotency="idem-ready-drift-01"),
            json={"expected_version": created["batch"]["version"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == CODE_BATCH_REJECTED

    async def test_cancel_requires_a_reason_and_is_terminal(self, monkeypatch):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)

        harness.act_as(ReviewerRole.REMEDIATOR)
        empty = harness.client.post(
            _batch_url(created["batch"]["batch_id"], ":cancel"),
            headers=_human_write_headers(harness, idempotency="idem-cancel-empty-1"),
            json={"expected_version": created["batch"]["version"], "reason": ""},
        )
        assert empty.status_code == 422

        harness.act_as(ReviewerRole.REMEDIATOR)
        cancelled = harness.client.post(
            _batch_url(created["batch"]["batch_id"], ":cancel"),
            headers=_human_write_headers(harness, idempotency="idem-cancel-ok-0001"),
            json={
                "expected_version": created["batch"]["version"],
                "reason": "the observation was a misreading",
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == BatchStatus.CANCELLED.value

        harness.act_as(ReviewerRole.REMEDIATOR)
        again = harness.client.post(
            _batch_url(created["batch"]["batch_id"], ":ready"),
            headers=_human_write_headers(harness, idempotency="idem-cancel-ready-1"),
            json={"expected_version": cancelled.json()["version"]},
        )
        assert again.status_code == 409

    async def test_a_claimed_batch_cannot_be_cancelled_out_from_under_the_agent(
        self, monkeypatch
    ):
        """The master state machine has no ``claimed -> cancelled`` edge.

        This is deliberate rather than an omission. Cancelling a live claim would
        let a human delete the ground under an agent that is mid-write, and the
        agent would discover it only as an unexplained lease failure. The bound
        that makes this safe is the lease itself: it is fifteen minutes, so a
        stuck agent's claim lapses on its own, and ``expired`` *does* have an edge
        to ``cancelled``. A ``blocked`` batch can be cancelled immediately.
        """
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.ADMIN)
        cancelled = harness.client.post(
            _batch_url(batch["batch_id"], ":cancel"),
            headers=_human_write_headers(harness, idempotency="idem-cancel-claim-1"),
            json={
                "expected_version": claim["batch"]["version"],
                "reason": "the agent has been stuck for an hour",
            },
        )
        assert cancelled.status_code == 409
        assert cancelled.json()["error"]["code"] == CODE_BATCH_CONFLICT
        # The claim is untouched, so the agent's own write still works.
        proceeding = await _agent_patch(
            harness,
            batch["batch_id"],
            {
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "transition": BatchStatus.PLANNING.value,
                "plan_artifact": "docs/plans/synthetic.md",
            },
            key="idem-cancel-denied-1",
        )
        assert proceeding["status"] == BatchStatus.PLANNING.value

    async def test_a_blocked_batch_can_be_cancelled_and_its_lease_is_gone(
        self, monkeypatch
    ):
        harness = _harness(monkeypatch)
        refs = [await _seed_review(harness, 1)]
        created = await _create_batch(harness, refs)
        batch = await _ready(harness, created["batch"])
        claim = await _claim(harness, batch)

        harness.act_as(ReviewerRole.AGENT)
        blocked = harness.client.post(
            _batch_url(batch["batch_id"], ":release"),
            headers=_agent_write_headers(idempotency="idem-cancel-block-01"),
            json={
                "expected_version": claim["batch"]["version"],
                "lease_token": claim["lease_token"],
                "disposition": BatchStatus.BLOCKED.value,
                "reason": "the observation needs a product decision first",
            },
        )
        assert blocked.status_code == 200, blocked.text

        harness.act_as(ReviewerRole.ADMIN)
        cancelled = harness.client.post(
            _batch_url(batch["batch_id"], ":cancel"),
            headers=_human_write_headers(harness, idempotency="idem-cancel-blocked1"),
            json={
                "expected_version": blocked.json()["version"],
                "reason": "we decided not to pursue this",
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == BatchStatus.CANCELLED.value
        assert cancelled.json()["lease"] is None


# =====================================================================
# The feature is configuration-gated
# =====================================================================


class TestConfigurationGate:

    async def test_without_an_agent_identity_the_routes_report_disabled(self, monkeypatch):
        harness = _harness(
            monkeypatch, AGENT_SERVICE_ACCOUNT="", AGENT_IAP_TARGET_AUDIENCE=""
        )
        review_id, version = await _seed_review(harness, 1)
        harness.act_as(ReviewerRole.REMEDIATOR)
        response = harness.client.post(
            f"{API_PREFIX}{BATCHES_PATH}",
            headers=_human_write_headers(harness, idempotency="idem-disabled-cr-1"),
            json={"review_refs": [{"review_id": review_id, "review_version": version}]},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "REMEDIATION_DISABLED"

    async def test_the_session_flag_follows_the_configuration(self, monkeypatch):
        enabled = _harness(monkeypatch)
        enabled.act_as(ReviewerRole.REMEDIATOR)
        flags = enabled.client.get(f"{API_PREFIX}/session", headers=_auth()).json()[
            "feature_flags"
        ]
        assert flags["remediation_enabled"] is True

        disabled = _harness(monkeypatch, REPO_ID="")
        disabled.act_as(ReviewerRole.REMEDIATOR)
        flags = disabled.client.get(f"{API_PREFIX}/session", headers=_auth()).json()[
            "feature_flags"
        ]
        assert flags["remediation_enabled"] is False
