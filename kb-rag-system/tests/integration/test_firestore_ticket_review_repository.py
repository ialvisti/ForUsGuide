"""Review repository against the REAL Firestore emulator (Stage 3, Step 6).

Runs via ``scripts/run_firestore_emulator_tests.sh`` and the pinned
``firestore-emulator-tests`` Cloud Build step. If the emulator is unavailable
the whole module skips with an explicit reason -- it is never downgraded to a
mock, because the point of this file is the semantics the in-memory backend
cannot prove:

* the review write and its audit event really commit (or roll back) together;
* a stale ``expected_version`` rolls the whole transaction back;
* the hash chain stays linear under genuinely concurrent transactions, using
  the SDK's own retries;
* an idempotency key deduplicates across separate transactions;
* claim/heartbeat/reclaim behave under a race;
* the client is bound to a NAMED database, never ``(default)``.

What the emulator still does not prove: IAM, effective TTL deletion, index
availability, or retention automation. Those belong to the staging gate.

REMOTE_REQUIRED: this host has no Docker; Stage 11 cannot close until this file
passes in the pinned remote emulator step.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.ticket_review_models import (
    GENESIS_EVENT_HASH,
    REMEDIATION_LEASE_S,
    BatchStatus,
    ReviewerIdentity,
    ReviewerRole,
    ReviewPatch,
    ReviewStatus,
    TicketReview,
    compute_audit_event_hash,
    review_id_for_devrev_work,
)
from api.tickets_console_config import (
    LOCAL_FIRESTORE_DATABASE,
    PRODUCTION_FIRESTORE_DATABASE,
)
from data_pipeline.ticket_review_repository import (
    BatchAlreadyClaimed,
    BatchLeaseLost,
    FirestoreTicketReviewBackend,
    IdempotencyConflict,
    MutationContext,
    ReviewVersionConflict,
    TicketReviewRepository,
    sha256_hex,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("FIRESTORE_EMULATOR_HOST"),
        reason="requiere el emulador de Firestore "
               "(scripts/run_firestore_emulator_tests.sh)",
    ),
]

# The console's isolation boundary is the named database. The emulator gets the
# local console database id, never `(default)`.
EMULATOR_DATABASE = os.environ.get(
    "TICKETS_FIRESTORE_DATABASE", LOCAL_FIRESTORE_DATABASE
)
TEST_CURSOR_KEY = base64.b64decode("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

REVIEWER = ReviewerIdentity(
    subject="accounts.google.com:emulator-reviewer",
    email="reviewer@example.invalid",
    display_name="Emulator Reviewer",
)
ADMIN = ReviewerIdentity(
    subject="accounts.google.com:emulator-admin",
    email="admin@example.invalid",
)
AGENT = ReviewerIdentity(
    subject="accounts.google.com:emulator-agent",
    email="agent@example.invalid",
)


def _context(
    actor: ReviewerIdentity = REVIEWER,
    role: ReviewerRole = ReviewerRole.REVIEWER,
    **over,
) -> MutationContext:
    values = {"actor": actor, "actor_role": role, "request_id": uuid.uuid4().hex}
    values.update(over)
    return MutationContext(**values)


@pytest.fixture
def backend():
    return FirestoreTicketReviewBackend(
        project=os.environ.get("FIRESTORE_PROJECT_ID", "handle-ticket-emulator"),
        database=EMULATOR_DATABASE,
        environment="local",
    )


@pytest.fixture
def repo(backend):
    return TicketReviewRepository(backend, cursor_key=TEST_CURSOR_KEY)


def _review() -> TicketReview:
    # A unique synthetic DON per test keeps runs isolated without ever
    # deleting a collection.
    don = f"don:core:dvrv-us-1:devo/EMULATOR:ticket/{uuid.uuid4().hex}"
    return TicketReview(
        review_id=review_id_for_devrev_work(don),
        devrev_work_id=don,
        devrev_display_id=f"TKT-{uuid.uuid4().hex[:8].upper()}",
        topic="distributions",
        comments="emulator observation",
    )


class TestAtomicReviewAndAudit:
    async def test_the_review_and_its_first_event_commit_together(self, repo, backend):
        created, was_created = await repo.create_or_get_review(
            _review(), context=_context()
        )

        assert was_created is True
        stored = await repo.get_review(created.review_id)
        assert stored.version == 1
        events = (await repo.list_audit_events(created.review_id)).items
        assert [event.event_type for event in events] == ["review_created"]
        assert events[0].previous_event_hash == GENESIS_EVENT_HASH
        assert events[0].event_hash == compute_audit_event_hash(events[0])

    async def test_native_timestamps_survive_the_round_trip(self, repo, backend):
        created, _ = await repo.create_or_get_review(_review(), context=_context())

        raw = await backend.get_doc(("ticket_reviews", created.review_id))

        # Serialized strings would silently disable Firestore TTL and range
        # queries; every stored timestamp must come back as a datetime.
        for field in ("created_at", "updated_at", "retention_expires_at"):
            assert isinstance(raw[field], datetime), field
            assert raw[field].tzinfo is not None

    async def test_a_stale_version_rolls_the_whole_transaction_back(self, repo):
        created, _ = await repo.create_or_get_review(_review(), context=_context())
        await repo.patch_review(
            created.review_id,
            ReviewPatch(rating=4),
            expected_version=1,
            context=_context(),
        )
        before = await repo.get_review(created.review_id)
        events_before = len((await repo.list_audit_events(created.review_id)).items)

        with pytest.raises(ReviewVersionConflict) as excinfo:
            await repo.patch_review(
                created.review_id,
                ReviewPatch(rating=1, comments="clobber"),
                expected_version=1,
                context=_context(),
            )

        assert excinfo.value.current_version == 2
        after = await repo.get_review(created.review_id)
        assert after.version == before.version
        assert after.rating == 4
        assert after.comments == before.comments
        # No half-written event: the audit append is inside the same transaction.
        assert len((await repo.list_audit_events(created.review_id)).items) == events_before


class TestHashChainUnderConcurrency:
    async def test_concurrent_transactions_leave_one_linear_chain(self, repo):
        created, _ = await repo.create_or_get_review(_review(), context=_context())

        async def attempt(rating: int):
            try:
                return await repo.patch_review(
                    created.review_id,
                    ReviewPatch(rating=rating),
                    expected_version=1,
                    context=_context(),
                )
            except ReviewVersionConflict:
                return None

        results = await asyncio.gather(*(attempt(rating) for rating in (1, 2, 3, 4, 5)))

        assert sum(1 for item in results if item is not None) == 1
        assert (await repo.get_review(created.review_id)).version == 2
        events = (await repo.list_audit_events(created.review_id)).items
        assert len(events) == 2
        previous = GENESIS_EVENT_HASH
        for event in events:
            assert event.previous_event_hash == previous
            assert event.event_hash == compute_audit_event_hash(event)
            previous = event.event_hash
        assert (await repo.verify_audit_chain(created.review_id)).intact is True

    async def test_sequential_writers_extend_one_chain(self, repo):
        created, _ = await repo.create_or_get_review(_review(), context=_context())
        current = created
        for rating in (1, 2, 3, 4, 5):
            current = await repo.patch_review(
                created.review_id,
                ReviewPatch(rating=rating),
                expected_version=current.version,
                context=_context(),
            )

        report = await repo.verify_audit_chain(created.review_id)

        assert report.intact is True
        assert report.event_count == 6
        assert current.version == 6


class TestIdempotencyAcrossTransactions:
    async def test_a_repeated_key_does_not_apply_twice(self, repo):
        created, _ = await repo.create_or_get_review(_review(), context=_context())
        key = f"emulator-{uuid.uuid4().hex}"
        context = _context(idempotency_key=key)

        first = await repo.patch_review(
            created.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )
        replay = await repo.patch_review(
            created.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )

        assert first.version == replay.version == 2
        assert len((await repo.list_audit_events(created.review_id)).items) == 2

    async def test_the_same_key_with_a_different_request_conflicts(self, repo):
        created, _ = await repo.create_or_get_review(_review(), context=_context())
        key = f"emulator-{uuid.uuid4().hex}"
        context = _context(idempotency_key=key)
        await repo.patch_review(
            created.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )

        with pytest.raises(IdempotencyConflict):
            await repo.patch_review(
                created.review_id,
                ReviewPatch(rating=1),
                expected_version=2,
                context=context,
            )
        assert (await repo.get_review(created.review_id)).rating == 5

    async def test_the_raw_key_is_never_stored(self, repo, backend):
        created, _ = await repo.create_or_get_review(_review(), context=_context())
        key = f"emulator-{uuid.uuid4().hex}"
        await repo.patch_review(
            created.review_id,
            ReviewPatch(rating=3),
            expected_version=1,
            context=_context(idempotency_key=key),
        )

        stored = await backend.get_doc(("idempotency_keys", sha256_hex(key)))

        assert stored is not None
        assert key not in repr(stored)
        assert isinstance(stored["expires_at"], datetime)


class TestBatchLeaseRaces:
    async def _ready_batch(self, repo):
        review, _ = await repo.create_or_get_review(_review(), context=_context())
        admin = _context(ADMIN, ReviewerRole.ADMIN)
        batch = await repo.create_batch(
            review_refs=[{"review_id": review.review_id, "review_version": review.version}],
            context=admin,
        )
        return await repo.patch_batch(
            batch.batch_id,
            expected_version=batch.version,
            transition=BatchStatus.READY,
            context=admin,
        )

    async def test_only_one_of_two_racing_agents_claims(self, repo):
        batch = await self._ready_batch(repo)
        agents = [
            _context(
                ReviewerIdentity(
                    subject=f"accounts.google.com:emulator-agent-{index}",
                    email=f"agent{index}@example.invalid",
                ),
                ReviewerRole.AGENT,
            )
            for index in range(4)
        ]

        async def claim(index: int, context: MutationContext):
            try:
                return await repo.claim_batch(
                    batch.batch_id, lease_token=f"token-{index}", context=context
                )
            except (BatchAlreadyClaimed, ReviewVersionConflict):
                return None

        results = await asyncio.gather(
            *(claim(index, context) for index, context in enumerate(agents))
        )

        winners = [claim for claim in results if claim is not None]
        assert len(winners) == 1
        stored = await repo.get_batch(batch.batch_id)
        assert stored.status is BatchStatus.CLAIMED
        assert stored.lease is not None
        assert stored.lease.lease_token_hash == winners[0].batch.lease.lease_token_hash

    async def test_a_heartbeat_renews_and_a_foreign_token_is_lost(self, repo):
        batch = await self._ready_batch(repo)
        agent = _context(AGENT, ReviewerRole.AGENT)
        claim = await repo.claim_batch(
            batch.batch_id, lease_token="emulator-token", context=agent
        )

        renewed = await repo.heartbeat_batch(
            batch.batch_id,
            expected_version=claim.batch.version,
            lease_token="emulator-token",
            context=agent,
        )
        assert renewed.lease.expires_at > claim.batch.lease.expires_at or renewed.version > (
            claim.batch.version
        )

        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=renewed.version,
                lease_token="not-the-token",
                context=agent,
            )

    async def test_an_expired_lease_is_reclaimed_with_an_audit_event(self, repo, backend):
        batch = await self._ready_batch(repo)
        agent = _context(AGENT, ReviewerRole.AGENT)
        claim = await repo.claim_batch(
            batch.batch_id, lease_token="first-token", context=agent
        )

        # Age the lease in place rather than sleeping for fifteen minutes. This
        # writes through the backend deliberately, the way only a test may.
        raw = await backend.get_doc(("remediation_batches", batch.batch_id))
        elapsed = datetime.now(timezone.utc) - timedelta(seconds=REMEDIATION_LEASE_S + 60)
        raw["lease"]["acquired_at"] = elapsed
        raw["lease"]["expires_at"] = elapsed + timedelta(seconds=1)
        raw["lease"]["last_heartbeat_at"] = elapsed
        raw["lease"]["continuous_since"] = elapsed
        await backend._ref(("remediation_batches", batch.batch_id)).set(raw)

        other = _context(
            ReviewerIdentity(
                subject="accounts.google.com:emulator-agent-2",
                email="agent2@example.invalid",
            ),
            ReviewerRole.AGENT,
        )
        reclaim = await repo.claim_batch(
            batch.batch_id, lease_token="second-token", context=other
        )

        assert reclaim.reclaimed is True
        assert reclaim.batch.lease.holder == "agent2@example.invalid"
        types = [
            event.event_type
            for event in (await repo.list_batch_events(batch.batch_id)).items
        ]
        assert "batch_lease_expired" in types
        assert "batch_lease_reclaimed" in types
        assert claim.batch.lease.lease_token_hash != reclaim.batch.lease.lease_token_hash


class TestDatabaseSelection:
    def test_the_client_is_bound_to_a_named_database(self, backend):
        assert backend.database == EMULATOR_DATABASE
        assert backend.database != "(default)"
        assert backend._client._database == EMULATOR_DATABASE

    def test_a_blank_database_is_refused(self):
        with pytest.raises(ValueError, match="explicitly"):
            FirestoreTicketReviewBackend(
                project="handle-ticket-emulator", database="", environment="local"
            )

    def test_staging_and_production_refuse_the_default_database(self):
        for environment in ("staging", "production"):
            with pytest.raises(ValueError, match="never \\(default\\)"):
                FirestoreTicketReviewBackend(
                    project="handle-ticket-emulator",
                    database="(default)",
                    environment=environment,
                )

    def test_production_pins_its_own_database_name(self):
        with pytest.raises(ValueError, match="tickets-console-prod"):
            FirestoreTicketReviewBackend(
                project="handle-ticket-emulator",
                database="tickets-console-staging",
                environment="production",
            )
        assert PRODUCTION_FIRESTORE_DATABASE == "tickets-console-prod"


class TestStatusTransitionsOnTheEmulator:
    async def test_a_terminal_transition_needs_evidence_and_is_audited(self, repo):
        created, _ = await repo.create_or_get_review(_review(), context=_context())
        current = created
        for status in (
            ReviewStatus.REVIEWED,
            ReviewStatus.TRIAGED,
            ReviewStatus.PLANNED,
            ReviewStatus.IN_PROGRESS,
            ReviewStatus.CHANGES_PROPOSED,
            ReviewStatus.VERIFYING,
        ):
            current = await repo.patch_review(
                created.review_id,
                ReviewPatch(status=status),
                expected_version=current.version,
                context=_context(),
            )

        from api.ticket_review_models import (
            ResolutionOutcome,
            ReviewResolution,
            VerificationEvidence,
        )

        resolved = await repo.patch_review(
            created.review_id,
            ReviewPatch(
                status=ReviewStatus.RESOLVED,
                resolution=ReviewResolution(
                    outcome=ResolutionOutcome.FIXED,
                    test_evidence=[
                        VerificationEvidence(
                            command_label="pytest -q tests/integration",
                            exit_code=0,
                            passed=1,
                            output_sha256="a" * 64,
                            runtime_s=0.5,
                        )
                    ],
                ),
            ),
            expected_version=current.version,
            context=_context(),
        )

        assert resolved.status is ReviewStatus.RESOLVED
        assert resolved.resolved_at is not None
        assert (await repo.verify_audit_chain(created.review_id)).intact is True
