"""Contract tests for the durable /tickets review repository (Stage 3).

These tests define the semantics the Firestore backend must satisfy. They run
against the in-memory backend, which shares every business invariant with the
Firestore one: the repository facade owns the logic and the backends only
supply the transaction primitive and bounded queries.

What is deliberately NOT proven here: real Firestore atomicity, contention,
and TTL. That lives in ``tests/integration/test_firestore_ticket_review_repository.py``
and is a mandatory remote gate (no Docker on this host).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from api.ticket_review_models import (
    AUDIT_RETENTION_DAYS,
    DEFAULT_PAGE_SIZE,
    GENESIS_EVENT_HASH,
    HASH_SCHEMA_VERSION,
    MAX_BATCH_REVIEWS,
    MAX_MESSAGE_BODY_LENGTH,
    MAX_PAGE_SIZE,
    MESSAGE_CACHE_TTL_S,
    REMEDIATION_LEASE_S,
    REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    REVIEW_RETENTION_DAYS,
    AuditEvent,
    BatchStatus,
    CorrelationTrust,
    CursorError,
    ImportState,
    ImportStatus,
    RemediationBatchItem,
    ResolutionOutcome,
    ReviewerIdentity,
    ReviewerRole,
    ReviewPatch,
    ReviewResolution,
    ReviewStatus,
    TicketImport,
    TicketReview,
    VerificationEvidence,
    compute_audit_event_hash,
    review_id_for_devrev_work,
)
from api.tickets_console_config import (
    LOCAL_FIRESTORE_DATABASE,
    PRODUCTION_FIRESTORE_DATABASE,
    STAGING_FIRESTORE_DATABASE,
    resolve_tickets_firestore_database,
)
from data_pipeline.ticket_review_repository import (
    ALLOWED_REVIEW_FACETS,
    AUDIT_EVENTS_SUBCOLLECTION,
    BATCH_EVENTS_SUBCOLLECTION,
    BATCH_ITEMS_SUBCOLLECTION,
    BATCHES_COLLECTION,
    CONSOLE_CACHE_COLLECTION,
    DEVREV_MESSAGE_CACHE_COLLECTION,
    EVIDENCE_LINKS_SUBCOLLECTION,
    EXPORTS_COLLECTION,
    GLOBAL_AUDIT_EVENTS_COLLECTION,
    GLOBAL_CHAIN_HEAD_DOC_ID,
    IDEMPOTENCY_KEYS_COLLECTION,
    IMPORT_ROWS_SUBCOLLECTION,
    IMPORT_STAGING_COLLECTION,
    IMPORTS_COLLECTION,
    PURGED_TOMBSTONE_KIND,
    RETENTION_FIELD,
    REVIEW_LIST_CURSOR_CONTEXT,
    REVIEWS_COLLECTION,
    TOMBSTONE_FIELDS,
    TTL_COLLECTIONS,
    TTL_FIELD,
    BatchAlreadyClaimed,
    BatchLeaseLost,
    BatchNotFound,
    BatchReleaseRefused,
    BatchVersionConflict,
    ConsoleCacheEntry,
    DevRevMessageCacheEntry,
    EvidenceCandidate,
    EvidenceCandidateRejected,
    FirestoreTicketReviewBackend,
    IdempotencyConflict,
    ImportRowSpec,
    InMemoryTicketReviewBackend,
    InvalidBatchTransition,
    InvalidReviewTransition,
    LeaseExtensionRefused,
    MutationContext,
    ReviewIdentityConflict,
    ReviewListQuery,
    ReviewNotFound,
    ReviewPatchSpec,
    ReviewRepositoryError,
    ReviewVersionConflict,
    TicketExportSummary,
    TicketReviewRepository,
    UnsupportedFilterCombination,
    canonical_ttl_declarations,
    review_index_declarations,
    sha256_hex,
)

# A deterministic, obviously synthetic 32-byte AEAD key. Tests only.
TEST_CURSOR_KEY = base64.b64decode("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
SYNTHETIC_DON = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234"
OTHER_DON = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/9999"
# A title carrying an email and a phone number. It must never reach a durable
# review, audit event, export, or tombstone -- only TTL cache data.
SYNTHETIC_TITLE = "Participant leak@example.invalid called +1-555-0100 about 401k"

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class _Clock:
    """A monotonic, injectable server-side clock."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


class _Ids:
    """Deterministic identifier factory (audit event ids, link ids, batches)."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.n:032x}"


def _identity(email: str = "reviewer@example.invalid") -> ReviewerIdentity:
    return ReviewerIdentity(
        subject=f"accounts.google.com:synthetic-{email.split('@')[0]}",
        email=email,
        display_name="Synthetic Reviewer",
    )


ADMIN = _identity("admin@example.invalid")
REVIEWER = _identity("reviewer@example.invalid")
OTHER_REVIEWER = _identity("other@example.invalid")
AGENT = _identity("agent@example.invalid")


def _context(
    actor: ReviewerIdentity = REVIEWER,
    role: ReviewerRole = ReviewerRole.REVIEWER,
    *,
    request_id: str | None = "req-1",
    idempotency_key: str | None = None,
    reason_code: str | None = None,
) -> MutationContext:
    return MutationContext(
        actor=actor,
        actor_role=role,
        request_id=request_id,
        idempotency_key=idempotency_key,
        reason_code=reason_code,
    )


ADMIN_CONTEXT = _context(ADMIN, ReviewerRole.ADMIN)
AGENT_CONTEXT = _context(AGENT, ReviewerRole.AGENT, request_id="req-agent")


def _review(don: str = SYNTHETIC_DON, **overrides) -> TicketReview:
    values = {
        "review_id": review_id_for_devrev_work(don),
        "devrev_work_id": don,
        "devrev_display_id": "TKT-1234",
        "topic": "distributions",
        "comments": "reviewer observation",
    }
    values.update(overrides)
    return TicketReview(**values)


def _resolution(**overrides) -> ReviewResolution:
    values = {
        "outcome": ResolutionOutcome.FIXED,
        "test_evidence": [
            VerificationEvidence(
                command_label="pytest -q tests/test_ticket_review_repository.py",
                exit_code=0,
                passed=12,
                output_sha256="a" * 64,
                runtime_s=1.5,
                occurred_at=T0,
            )
        ],
        "verification_summary": "suite green",
        "verified_by": ADMIN,
        "verified_at": T0,
    }
    values.update(overrides)
    return ReviewResolution(**values)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def backend() -> InMemoryTicketReviewBackend:
    return InMemoryTicketReviewBackend()


@pytest.fixture
def repo(backend: InMemoryTicketReviewBackend, clock: _Clock) -> TicketReviewRepository:
    return TicketReviewRepository(
        backend,
        cursor_key=TEST_CURSOR_KEY,
        clock=clock,
        id_factory=_Ids(),
    )


async def _seed(repo: TicketReviewRepository, don: str = SYNTHETIC_DON, **overrides):
    review, _created = await repo.create_or_get_review(
        _review(don, **overrides), context=_context()
    )
    return review


async def _advance_to(repo: TicketReviewRepository, review_id: str, target: ReviewStatus):
    """Walk a review to ``target`` through legal transitions only."""
    path = {
        ReviewStatus.REVIEWED: [ReviewStatus.REVIEWED],
        ReviewStatus.TRIAGED: [ReviewStatus.REVIEWED, ReviewStatus.TRIAGED],
        ReviewStatus.PLANNED: [
            ReviewStatus.REVIEWED,
            ReviewStatus.TRIAGED,
            ReviewStatus.PLANNED,
        ],
        ReviewStatus.IN_PROGRESS: [
            ReviewStatus.REVIEWED,
            ReviewStatus.TRIAGED,
            ReviewStatus.PLANNED,
            ReviewStatus.IN_PROGRESS,
        ],
        ReviewStatus.CHANGES_PROPOSED: [
            ReviewStatus.REVIEWED,
            ReviewStatus.TRIAGED,
            ReviewStatus.PLANNED,
            ReviewStatus.IN_PROGRESS,
            ReviewStatus.CHANGES_PROPOSED,
        ],
        ReviewStatus.VERIFYING: [
            ReviewStatus.REVIEWED,
            ReviewStatus.TRIAGED,
            ReviewStatus.PLANNED,
            ReviewStatus.IN_PROGRESS,
            ReviewStatus.CHANGES_PROPOSED,
            ReviewStatus.VERIFYING,
        ],
    }[target]
    current = await repo.get_review(review_id)
    for status in path:
        current = await repo.patch_review(
            review_id,
            ReviewPatch(status=status),
            expected_version=current.version,
            context=_context(),
        )
    return current


# =====================================================================
# 1-2. Deterministic identity and create-or-get idempotence
# =====================================================================


class TestReviewCreation:
    async def test_create_or_get_review_is_idempotent_by_deterministic_id(self, repo):
        first, created_first = await repo.create_or_get_review(_review(), context=_context())
        second, created_second = await repo.create_or_get_review(
            _review(comments="a different draft"), context=_context()
        )

        assert created_first is True
        assert created_second is False
        assert first.review_id == second.review_id == review_id_for_devrev_work(SYNTHETIC_DON)
        assert second.version == 1
        # The second call must not overwrite the stored record.
        assert second.comments == "reviewer observation"

    async def test_creation_appends_exactly_one_audit_event(self, repo):
        review, _ = await repo.create_or_get_review(_review(), context=_context())
        await repo.create_or_get_review(_review(), context=_context())

        page = await repo.list_audit_events(review.review_id)

        assert [event.event_type for event in page.items] == ["review_created"]

    async def test_a_different_don_gets_a_different_document(self, repo):
        first = await _seed(repo, SYNTHETIC_DON)
        second = await _seed(repo, OTHER_DON, devrev_display_id="TKT-9999")

        assert first.review_id != second.review_id
        assert (await repo.get_review(second.review_id)).devrev_work_id == OTHER_DON

    async def test_a_foreign_don_cannot_collide_with_an_existing_review(self, repo):
        await _seed(repo)
        # A caller that reuses another ticket's deterministic id must be refused
        # rather than silently adopting the stored document.
        impostor = _review().model_copy(update={"devrev_work_id": OTHER_DON})

        with pytest.raises(ReviewIdentityConflict):
            await repo.create_or_get_review(impostor, context=_context())

    async def test_a_mismatched_review_id_is_refused_before_any_write(self, repo, backend):
        bogus = _review().model_copy(update={"review_id": "b" * 64})

        with pytest.raises(ReviewRepositoryError):
            await repo.create_or_get_review(bogus, context=_context())
        assert await backend.dump_collection(REVIEWS_COLLECTION) == {}

    async def test_display_id_is_normalized_for_exact_lookup(self, repo):
        await _seed(repo, devrev_display_id=" tkt-1234 ")

        found = await repo.find_review_by_display_id("TKT-1234")

        assert found is not None
        assert found.devrev_display_id == "TKT-1234"
        assert await repo.find_review_by_display_id("tkt-1234") is not None
        assert await repo.find_review_by_display_id("TKT-0000") is None

    async def test_get_review_raises_a_typed_not_found(self, repo):
        with pytest.raises(ReviewNotFound):
            await repo.get_review("c" * 64)


# =====================================================================
# 3. Optimistic concurrency
# =====================================================================


class TestOptimisticConcurrency:
    async def test_patch_at_the_current_version_increments_exactly_once(self, repo, clock):
        review = await _seed(repo)
        clock.advance(minutes=5)

        patched = await repo.patch_review(
            review.review_id,
            ReviewPatch(rating=4, comments="second pass"),
            expected_version=1,
            context=_context(),
        )

        assert patched.version == 2
        assert patched.rating == 4
        assert patched.updated_at == clock.now
        assert (await repo.get_review(review.review_id)).version == 2

    async def test_a_stale_version_is_refused_with_safe_metadata_only(self, repo, clock):
        review = await _seed(repo)
        clock.advance(minutes=1)
        await repo.patch_review(
            review.review_id,
            ReviewPatch(rating=5),
            expected_version=1,
            context=_context(),
        )

        with pytest.raises(ReviewVersionConflict) as excinfo:
            await repo.patch_review(
                review.review_id,
                ReviewPatch(rating=1, comments="clobber"),
                expected_version=1,
                context=_context(OTHER_REVIEWER),
            )

        error = excinfo.value
        assert error.current_version == 2
        assert error.changed_at == clock.now
        # Only the two safe integers/timestamps travel; never another
        # reviewer's unsaved content.
        rendered = f"{error!s} {error.args!r}"
        assert "clobber" not in rendered
        assert OTHER_REVIEWER.email not in rendered
        assert error.supplied_version == 1

    async def test_a_stale_patch_leaves_the_document_and_ledger_unchanged(self, repo):
        review = await _seed(repo)
        await repo.patch_review(
            review.review_id,
            ReviewPatch(rating=5),
            expected_version=1,
            context=_context(),
        )
        before = await repo.get_review(review.review_id)
        events_before = len((await repo.list_audit_events(review.review_id)).items)

        with pytest.raises(ReviewVersionConflict):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(rating=1),
                expected_version=1,
                context=_context(),
            )

        assert (await repo.get_review(review.review_id)) == before
        assert len((await repo.list_audit_events(review.review_id)).items) == events_before

    async def test_immutable_identifiers_and_creation_time_never_move(self, repo, clock):
        review = await _seed(repo)
        created_at = review.created_at
        clock.advance(days=3)

        patched = await repo.patch_review(
            review.review_id,
            ReviewPatch(topic="loans"),
            expected_version=1,
            context=_context(),
        )

        assert patched.review_id == review.review_id
        assert patched.devrev_work_id == SYNTHETIC_DON
        assert patched.devrev_display_id == "TKT-1234"
        assert patched.created_at == created_at

    async def test_the_patch_surface_cannot_carry_identifiers_or_versions(self):
        for forbidden in ("review_id", "devrev_work_id", "version", "created_at"):
            with pytest.raises(ValueError):
                ReviewPatch(**{forbidden: "x"})

    async def test_patch_on_a_missing_review_is_not_found(self, repo):
        with pytest.raises(ReviewNotFound):
            await repo.patch_review(
                "d" * 64,
                ReviewPatch(rating=3),
                expected_version=1,
                context=_context(),
            )

    async def test_concurrent_patches_produce_one_winner(self, repo):
        review = await _seed(repo)

        async def attempt(rating: int):
            try:
                return await repo.patch_review(
                    review.review_id,
                    ReviewPatch(rating=rating),
                    expected_version=1,
                    context=_context(),
                )
            except ReviewVersionConflict:
                return None

        results = await asyncio.gather(*(attempt(rating) for rating in (1, 2, 3, 4, 5)))

        assert sum(1 for item in results if item is not None) == 1
        assert (await repo.get_review(review.review_id)).version == 2


# =====================================================================
# 4 + 19. Status transitions, admin reopen, resolution evidence
# =====================================================================


class TestStatusTransitions:
    async def test_an_illegal_transition_is_refused(self, repo):
        review = await _seed(repo)

        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.RESOLVED),
                expected_version=1,
                context=_context(),
            )

    async def test_a_viewer_may_not_change_status(self, repo):
        review = await _seed(repo)

        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.REVIEWED),
                expected_version=1,
                context=_context(REVIEWER, ReviewerRole.VIEWER),
            )

    async def test_the_repository_error_is_also_the_contract_error(self):
        from api.ticket_review_models import InvalidReviewTransition as ContractError

        # Stage 1 already shipped an InvalidReviewTransition. Stage 3's required
        # class must not fork the taxonomy: the API layer catches either name.
        assert issubclass(InvalidReviewTransition, ContractError)
        assert issubclass(InvalidReviewTransition, ReviewRepositoryError)

    async def test_resolving_requires_verification_evidence(self, repo):
        review = await _seed(repo)
        current = await _advance_to(repo, review.review_id, ReviewStatus.VERIFYING)
        empty = ReviewResolution(
            outcome=ResolutionOutcome.FIXED, verification_summary="trust me"
        )

        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.RESOLVED, resolution=empty),
                expected_version=current.version,
                context=_context(),
            )
        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.RESOLVED),
                expected_version=current.version,
                context=_context(),
            )
        assert (await repo.get_review(review.review_id)).status is ReviewStatus.VERIFYING

    async def test_resolution_with_evidence_is_terminal_and_stamped(self, repo, clock):
        review = await _seed(repo)
        current = await _advance_to(repo, review.review_id, ReviewStatus.VERIFYING)
        clock.advance(hours=1)

        resolved = await repo.patch_review(
            review.review_id,
            ReviewPatch(status=ReviewStatus.RESOLVED, resolution=_resolution()),
            expected_version=current.version,
            context=_context(),
        )

        assert resolved.status is ReviewStatus.RESOLVED
        assert resolved.resolved_at == clock.now
        assert resolved.retention_expires_at == clock.now + timedelta(
            days=REVIEW_RETENTION_DAYS
        )

    async def test_only_an_admin_may_reopen_a_terminal_review_and_only_to_triaged(
        self, repo
    ):
        review = await _seed(repo)
        current = await _advance_to(repo, review.review_id, ReviewStatus.VERIFYING)
        resolved = await repo.patch_review(
            review.review_id,
            ReviewPatch(status=ReviewStatus.RESOLVED, resolution=_resolution()),
            expected_version=current.version,
            context=_context(),
        )

        # No implicit escape from a terminal status.
        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.TRIAGED),
                expected_version=resolved.version,
                context=_context(),
            )
        # A reviewer cannot reopen even with the explicit flag.
        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.TRIAGED),
                expected_version=resolved.version,
                context=_context(),
                admin_reopen=True,
            )
        # An admin may only reopen to triaged.
        with pytest.raises(InvalidReviewTransition):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(status=ReviewStatus.IN_PROGRESS),
                expected_version=resolved.version,
                context=ADMIN_CONTEXT,
                admin_reopen=True,
            )

        reopened = await repo.patch_review(
            review.review_id,
            ReviewPatch(status=ReviewStatus.TRIAGED),
            expected_version=resolved.version,
            context=ADMIN_CONTEXT,
            admin_reopen=True,
        )

        assert reopened.status is ReviewStatus.TRIAGED
        assert reopened.resolved_at is None
        events = (await repo.list_audit_events(review.review_id)).items
        assert events[-1].event_type == "review_reopened"
        assert events[-1].actor_subject == ADMIN.subject


# =====================================================================
# 5. Reviewer assignment vs authenticated actor
# =====================================================================


class TestReviewerAssignment:
    async def test_a_reviewer_may_self_assign(self, repo):
        review = await _seed(repo)

        patched = await repo.patch_review(
            review.review_id,
            ReviewPatch(assigned_reviewer=REVIEWER),
            expected_version=1,
            context=_context(REVIEWER),
        )

        assert patched.assigned_reviewer == REVIEWER

    async def test_a_reviewer_may_not_assign_somebody_else(self, repo):
        review = await _seed(repo)

        with pytest.raises(ReviewRepositoryError):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(assigned_reviewer=OTHER_REVIEWER),
                expected_version=1,
                context=_context(REVIEWER),
            )
        assert (await repo.get_review(review.review_id)).assigned_reviewer is None

    async def test_an_admin_may_reassign_a_validated_identity(self, repo):
        review = await _seed(repo)
        assigned = await repo.patch_review(
            review.review_id,
            ReviewPatch(assigned_reviewer=REVIEWER),
            expected_version=1,
            context=_context(REVIEWER),
        )

        reassigned = await repo.patch_review(
            review.review_id,
            ReviewPatch(assigned_reviewer=OTHER_REVIEWER),
            expected_version=assigned.version,
            context=ADMIN_CONTEXT,
        )

        assert reassigned.assigned_reviewer == OTHER_REVIEWER

    async def test_csv_reviewer_text_only_reaches_the_legacy_display_field(self, repo):
        review = await _seed(repo)

        patched = await repo.patch_review(
            review.review_id,
            ReviewPatch(legacy_reviewer_display_name="Jane From The Sheet"),
            expected_version=1,
            context=ADMIN_CONTEXT,
        )

        assert patched.legacy_reviewer_display_name == "Jane From The Sheet"
        assert patched.assigned_reviewer is None

    async def test_the_audit_actor_is_independent_of_the_assignee(self, repo):
        review = await _seed(repo)

        await repo.patch_review(
            review.review_id,
            ReviewPatch(assigned_reviewer=OTHER_REVIEWER),
            expected_version=1,
            context=ADMIN_CONTEXT,
        )

        event = (await repo.list_audit_events(review.review_id)).items[-1]
        assert event.actor_subject == ADMIN.subject
        assert event.actor_email == ADMIN.email
        assert "assigned_reviewer" in event.changed_fields


# =====================================================================
# 6-8. Hash-chained, append-only audit ledger
# =====================================================================


class TestAuditChain:
    async def test_the_genesis_event_hash_is_recomputable_byte_for_byte(self, repo):
        review = await _seed(repo)

        event = (await repo.list_audit_events(review.review_id)).items[0]

        assert event.previous_event_hash == GENESIS_EVENT_HASH == "0" * 64
        canonical = (
            "{"
            f'"actor_subject_hash":"{sha256_hex(REVIEWER.subject)}",'
            f'"changed_fields":[],'
            f'"event_id":"{event.event_id}",'
            f'"event_type":"review_created",'
            f'"hash_schema_version":{HASH_SCHEMA_VERSION},'
            f'"idempotency_key_hash":null,'
            f'"new_version":1,'
            f'"occurred_at_unix_us":{event.occurred_at_unix_us},'
            f'"parent_id":"{review.review_id}",'
            f'"parent_kind":"review",'
            f'"previous_event_hash":"{GENESIS_EVENT_HASH}",'
            f'"previous_version":null,'
            f'"reason_code":null,'
            f'"request_id_hash":"{sha256_hex("req-1")}"'
            "}"
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert event.event_hash == expected
        assert event.event_hash == compute_audit_event_hash(event)
        assert event.occurred_at_unix_us == int(T0.timestamp() * 1_000_000)

    async def test_every_successful_mutation_appends_exactly_one_linked_event(
        self, repo, clock
    ):
        review = await _seed(repo)
        current = await repo.get_review(review.review_id)
        for rating in (1, 2, 3):
            clock.advance(minutes=1)
            current = await repo.patch_review(
                review.review_id,
                ReviewPatch(rating=rating),
                expected_version=current.version,
                context=_context(idempotency_key=f"key-{rating}"),
            )

        events = (await repo.list_audit_events(review.review_id)).items

        assert len(events) == 4
        assert [event.new_version for event in events] == [1, 2, 3, 4]
        assert [event.previous_version for event in events] == [None, 1, 2, 3]
        previous = GENESIS_EVENT_HASH
        for event in events:
            assert event.previous_event_hash == previous
            assert event.event_hash == compute_audit_event_hash(event)
            previous = event.event_hash
        assert events[1].idempotency_key_hash == sha256_hex("key-1")
        assert (await repo.verify_audit_chain(review.review_id)).intact is True

    async def test_a_mutated_or_reordered_ledger_fails_verification(self, repo, backend):
        review = await _seed(repo)
        await repo.patch_review(
            review.review_id,
            ReviewPatch(rating=4),
            expected_version=1,
            context=_context(),
        )
        events = await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        )
        target = sorted(events)[1]

        await backend.force_write(
            (REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION, target),
            {**events[target], "changed_fields": ["rating", "severity"]},
        )

        report = await repo.verify_audit_chain(review.review_id)
        assert report.intact is False
        assert report.broken_event_id == target

    async def test_duplicate_changed_fields_cannot_change_the_hash(self):
        base = AuditEvent(
            event_id="e1",
            parent_kind="review",
            parent_id="a" * 64,
            event_type="review_updated",
            actor_subject=REVIEWER.subject,
            actor_subject_hash=sha256_hex(REVIEWER.subject),
            occurred_at_unix_us=1,
            changed_fields=["rating", "comments"],
        )
        noisy = base.model_copy(
            update={"changed_fields": ["comments", "rating", "rating", "comments"]}
        )

        assert compute_audit_event_hash(base) == compute_audit_event_hash(noisy)

    async def test_the_repository_exposes_no_audit_update_or_delete_path(self, repo):
        for forbidden in (
            "update_audit_event",
            "delete_audit_event",
            "patch_audit_event",
            "purge_audit_event",
        ):
            assert not hasattr(repo, forbidden)
        assert AuditEvent.model_config["frozen"] is True

    async def test_an_audit_event_carries_no_review_content(self, repo):
        review = await _seed(repo, comments="secret participant note", topic="loans")
        await repo.patch_review(
            review.review_id,
            ReviewPatch(comments="another secret note"),
            expected_version=1,
            context=_context(reason_code="reviewer_edit"),
        )

        events = (await repo.list_audit_events(review.review_id)).items

        rendered = json.dumps([event.model_dump(mode="json") for event in events])
        assert "secret participant note" not in rendered
        assert "another secret note" not in rendered
        assert events[-1].metadata == {"reason_code": "reviewer_edit"}
        assert events[-1].changed_fields == ["comments"]

    async def test_concurrent_mutations_leave_one_linear_chain(self, repo):
        review = await _seed(repo)
        current = await repo.get_review(review.review_id)

        async def attempt(rating: int, version: int):
            try:
                return await repo.patch_review(
                    review.review_id,
                    ReviewPatch(rating=rating),
                    expected_version=version,
                    context=_context(),
                )
            except ReviewVersionConflict:
                return None

        await asyncio.gather(
            *(attempt(rating, current.version) for rating in (1, 2, 3, 4, 5))
        )
        events = (await repo.list_audit_events(review.review_id)).items

        assert len(events) == 2
        hashes = [event.event_hash for event in events]
        assert len(set(hashes)) == 2
        assert events[1].previous_event_hash == events[0].event_hash
        assert (await repo.verify_audit_chain(review.review_id)).intact is True


# =====================================================================
# 9-11 + 25. Disposable DevRev cache, size limits, TTL separation
# =====================================================================


def _cache_entry(**overrides) -> DevRevMessageCacheEntry:
    values = {
        "remote_entry_id": "don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_entry/1",
        "devrev_work_id": SYNTHETIC_DON,
        "object_version": 1,
        "remote_modified_at": T0,
        "body": "participant asked about a rollover",
        "author_id": "don:identity:dvrv-us-1:devo/SYNTHETIC00:devu/1",
        "title": SYNTHETIC_TITLE,
    }
    values.update(overrides)
    return DevRevMessageCacheEntry(**values)


class TestDevRevMessageCache:
    async def test_upsert_is_idempotent_by_hashed_remote_entry_id(self, repo, backend):
        entry = _cache_entry()

        await repo.upsert_message_cache_entry(entry)
        await repo.upsert_message_cache_entry(entry)

        stored = await backend.dump_collection(DEVREV_MESSAGE_CACHE_COLLECTION)
        assert list(stored) == [sha256_hex(entry.remote_entry_id)]
        # The raw remote id is never a document id: a DON contains '/'.
        assert entry.remote_entry_id not in stored

    async def test_a_newer_object_version_replaces_an_older_snapshot(self, repo, clock):
        await repo.upsert_message_cache_entry(_cache_entry(body="first"))
        clock.advance(minutes=1)

        await repo.upsert_message_cache_entry(
            _cache_entry(body="second", object_version=2, remote_modified_at=clock.now)
        )

        cached = await repo.get_message_cache_entry(_cache_entry().remote_entry_id)
        assert cached is not None
        assert cached.body == "second"
        assert cached.object_version == 2

    async def test_an_older_snapshot_never_overwrites_a_newer_one(self, repo, clock):
        clock.advance(minutes=5)
        await repo.upsert_message_cache_entry(
            _cache_entry(body="newer", object_version=7, remote_modified_at=clock.now)
        )

        await repo.upsert_message_cache_entry(
            _cache_entry(body="older", object_version=2, remote_modified_at=T0)
        )

        cached = await repo.get_message_cache_entry(_cache_entry().remote_entry_id)
        assert cached.body == "newer"
        assert cached.object_version == 7

    async def test_a_missing_object_version_falls_back_to_the_modified_date(
        self, repo, clock
    ):
        await repo.upsert_message_cache_entry(
            _cache_entry(object_version=None, body="first", remote_modified_at=T0)
        )
        newer = clock.advance(hours=2)

        await repo.upsert_message_cache_entry(
            _cache_entry(object_version=None, body="second", remote_modified_at=newer)
        )
        await repo.upsert_message_cache_entry(
            _cache_entry(
                object_version=None, body="stale", remote_modified_at=T0 - timedelta(days=1)
            )
        )

        cached = await repo.get_message_cache_entry(_cache_entry().remote_entry_id)
        assert cached.body == "second"

    async def test_bodies_artifacts_and_lists_are_bounded(self):
        with pytest.raises(ValueError):
            _cache_entry(body="x" * (MAX_MESSAGE_BODY_LENGTH + 1))
        with pytest.raises(ValueError):
            _cache_entry(attachments=[f"att-{n}" for n in range(21)])
        with pytest.raises(ValueError):
            _cache_entry(title="t" * 513)
        with pytest.raises(ValueError):
            _cache_entry(remote_entry_id="")

    async def test_cache_documents_carry_ttl_and_never_retention(self, repo, backend, clock):
        await repo.upsert_message_cache_entry(_cache_entry())
        await repo.put_console_cache_entry(
            ConsoleCacheEntry(cache_key="reviews:list:v1", payload={"count": "3"})
        )

        for collection in (DEVREV_MESSAGE_CACHE_COLLECTION, CONSOLE_CACHE_COLLECTION):
            docs = await backend.dump_collection(collection)
            assert docs
            for doc in docs.values():
                assert isinstance(doc[TTL_FIELD], datetime)
                assert RETENTION_FIELD not in doc
                assert "legal_hold" not in doc
        message_doc = next(
            iter((await backend.dump_collection(DEVREV_MESSAGE_CACHE_COLLECTION)).values())
        )
        assert message_doc[TTL_FIELD] == clock.now + timedelta(seconds=MESSAGE_CACHE_TTL_S)

    async def test_review_and_audit_documents_never_inherit_a_cache_ttl(self, repo, backend):
        review = await _seed(repo)

        review_doc = (await backend.dump_collection(REVIEWS_COLLECTION))[review.review_id]
        events = await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        )

        assert TTL_FIELD not in review_doc
        # 730 days after the last activity, refreshed on every durable write --
        # never a 15-minute or 24-hour cache window.
        assert review_doc[RETENTION_FIELD] == T0 + timedelta(days=REVIEW_RETENTION_DAYS)
        for event in events.values():
            assert TTL_FIELD not in event
            assert isinstance(event[RETENTION_FIELD], datetime)

    async def test_an_elapsed_cache_entry_is_absent_at_the_boundary(self, repo, clock):
        await repo.upsert_message_cache_entry(_cache_entry())

        clock.advance(seconds=MESSAGE_CACHE_TTL_S + 1)

        assert await repo.get_message_cache_entry(_cache_entry().remote_entry_id) is None

    async def test_a_synthetic_title_stays_confined_to_cache_data(self, repo, backend):
        review = await _seed(repo)
        await repo.upsert_message_cache_entry(_cache_entry())
        await repo.put_console_cache_entry(
            ConsoleCacheEntry(cache_key="detail:TKT-1234", title=SYNTHETIC_TITLE)
        )
        await repo.patch_review(
            review.review_id,
            ReviewPatch(comments="clean note"),
            expected_version=1,
            context=_context(),
        )
        await repo.create_export(
            TicketExportSummary(
                export_id="exp-1",
                created_by=ADMIN,
                row_count=1,
                file_sha256="f" * 64,
                filter_fingerprint="e" * 64,
            ),
            context=ADMIN_CONTEXT,
        )

        durable = json.dumps(
            {
                "reviews": await backend.dump_collection(REVIEWS_COLLECTION),
                "audit": await backend.dump_subcollection(
                    REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
                ),
                "global": await backend.dump_collection(GLOBAL_AUDIT_EVENTS_COLLECTION),
                "exports": await backend.dump_collection(EXPORTS_COLLECTION),
            },
            default=str,
        )

        assert SYNTHETIC_TITLE not in durable
        assert "leak@example.invalid" not in durable
        assert "+1-555-0100" not in durable
        assert "title" not in TicketReview.model_fields
        cache = json.dumps(
            await backend.dump_collection(DEVREV_MESSAGE_CACHE_COLLECTION), default=str
        )
        assert SYNTHETIC_TITLE in cache


# =====================================================================
# 12. Manual evidence links
# =====================================================================


def _candidate(review_id: str, **overrides) -> EvidenceCandidate:
    values = {
        "review_id": review_id,
        "evidence_reference": "ticket-job:0123456789abcdef",
        "evidence_digest": "b" * 64,
        "correlation_trust": CorrelationTrust.MANUAL_REVIEWER,
        "issued_to_subject": REVIEWER.subject,
        "expires_at": T0 + timedelta(minutes=5),
        "broker_result_digest": "c" * 64,
    }
    values.update(overrides)
    return EvidenceCandidate(**values)


class TestEvidenceLinks:
    async def test_linking_requires_a_service_validated_candidate(self, repo):
        review = await _seed(repo)

        with pytest.raises(TypeError):
            await repo.create_evidence_link(
                review.review_id,
                candidate="ticket-job:whatever",  # type: ignore[arg-type]
                reason="looks related",
                expected_version=1,
                context=_context(),
            )

    async def test_a_link_is_versioned_and_audited(self, repo, clock):
        review = await _seed(repo)
        clock.advance(minutes=1)

        link, updated = await repo.create_evidence_link(
            review.review_id,
            candidate=_candidate(review.review_id),
            reason="matches the retrieval trace",
            expected_version=1,
            context=_context(),
        )

        assert updated.version == 2
        assert link.review_id == review.review_id
        assert link.evidence_reference == "ticket-job:0123456789abcdef"
        assert link.linked_by == REVIEWER
        assert link.version == 1
        events = (await repo.list_audit_events(review.review_id)).items
        assert events[-1].event_type == "evidence_linked"
        assert "evidence_links" in events[-1].changed_fields

    async def test_linking_the_same_candidate_twice_is_idempotent(self, repo):
        review = await _seed(repo)
        candidate = _candidate(review.review_id)
        first, updated = await repo.create_evidence_link(
            review.review_id,
            candidate=candidate,
            reason="matches",
            expected_version=1,
            context=_context(),
        )

        second, unchanged = await repo.create_evidence_link(
            review.review_id,
            candidate=candidate,
            reason="matches",
            expected_version=updated.version,
            context=_context(),
        )

        assert second.link_id == first.link_id
        assert unchanged.version == updated.version
        assert len((await repo.list_evidence_links(review.review_id)).items) == 1

    async def test_a_cross_review_expired_or_foreign_candidate_fails_safely(self, repo, clock):
        review = await _seed(repo)
        other = await _seed(repo, OTHER_DON, devrev_display_id="TKT-9999")

        with pytest.raises(EvidenceCandidateRejected):
            await repo.create_evidence_link(
                review.review_id,
                candidate=_candidate(other.review_id),
                reason="cross review",
                expected_version=1,
                context=_context(),
            )
        with pytest.raises(EvidenceCandidateRejected):
            await repo.create_evidence_link(
                review.review_id,
                candidate=_candidate(review.review_id, issued_to_subject=ADMIN.subject),
                reason="not my token",
                expected_version=1,
                context=_context(),
            )
        clock.advance(minutes=10)
        with pytest.raises(EvidenceCandidateRejected):
            await repo.create_evidence_link(
                review.review_id,
                candidate=_candidate(review.review_id),
                reason="expired",
                expected_version=1,
                context=_context(),
            )
        assert (await repo.get_review(review.review_id)).version == 1
        assert (await repo.list_evidence_links(review.review_id)).items == []

    async def test_a_viewer_may_not_link_or_unlink(self, repo):
        review = await _seed(repo)

        with pytest.raises(ReviewRepositoryError):
            await repo.create_evidence_link(
                review.review_id,
                candidate=_candidate(review.review_id),
                reason="viewer",
                expected_version=1,
                context=_context(REVIEWER, ReviewerRole.VIEWER),
            )

    async def test_unlinking_is_versioned_reasoned_and_audited(self, repo):
        review = await _seed(repo)
        link, updated = await repo.create_evidence_link(
            review.review_id,
            candidate=_candidate(review.review_id),
            reason="matches",
            expected_version=1,
            context=_context(),
        )

        with pytest.raises(ReviewVersionConflict):
            await repo.unlink_evidence_link(
                review.review_id,
                link.link_id,
                reason="stale attempt",
                expected_version=1,
                context=_context(),
            )

        after = await repo.unlink_evidence_link(
            review.review_id,
            link.link_id,
            reason="wrong trace",
            expected_version=updated.version,
            context=_context(),
        )

        assert after.version == updated.version + 1
        assert (await repo.list_evidence_links(review.review_id)).items == []
        events = (await repo.list_audit_events(review.review_id)).items
        assert events[-1].event_type == "evidence_unlinked"
        # The reviewer's free-text reason is durable product state on the link,
        # not audit-ledger content.
        assert "wrong trace" not in json.dumps(events[-1].model_dump(mode="json"))
        assert "evidence_links" in events[-1].changed_fields

    async def test_evidence_links_carry_retention_but_no_ttl(self, repo, backend, clock):
        review = await _seed(repo)
        await repo.create_evidence_link(
            review.review_id,
            candidate=_candidate(review.review_id),
            reason="matches",
            expected_version=1,
            context=_context(),
        )

        links = await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, EVIDENCE_LINKS_SUBCOLLECTION
        )

        for doc in links.values():
            assert TTL_FIELD not in doc
            assert doc[RETENTION_FIELD] == clock.now + timedelta(days=REVIEW_RETENTION_DAYS)
            assert doc["legal_hold"] is False


# =====================================================================
# 13-14. Master query grammar and opaque cursors
# =====================================================================


class _RefusingBackend(InMemoryTicketReviewBackend):
    """Proves a rejected filter never reaches the backend."""

    def __init__(self) -> None:
        super().__init__()
        self.queries = 0

    async def query_reviews(self, **kwargs):
        self.queries += 1
        return await super().query_reviews(**kwargs)


class TestQueryGrammar:
    @pytest.fixture
    def loaded(self, clock):
        async def _build():
            backend = _RefusingBackend()
            repo = TicketReviewRepository(
                backend, cursor_key=TEST_CURSOR_KEY, clock=clock, id_factory=_Ids()
            )
            for index in range(5):
                don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{100 + index}"
                clock.advance(minutes=1)
                review = await repo.create_or_get_review(
                    _review(
                        don,
                        review_id=review_id_for_devrev_work(don),
                        devrev_work_id=don,
                        devrev_display_id=f"TKT-{100 + index}",
                        topic="distributions" if index % 2 else "loans",
                    ),
                    context=_context(),
                )
                await repo.patch_review(
                    review[0].review_id,
                    ReviewPatch(status=ReviewStatus.REVIEWED),
                    expected_version=1,
                    context=_context(),
                )
            return repo, backend

        return _build

    async def test_the_default_queue_query_is_deterministic(self, loaded):
        repo, _backend = await loaded()

        page = await repo.list_reviews(
            ReviewListQuery(statuses=[ReviewStatus.REVIEWED], page_size=10)
        )

        ordered = [(item.updated_at, item.review_id) for item in page.items]
        assert ordered == sorted(ordered, key=lambda pair: (-pair[0].timestamp(), pair[1]))
        assert len(page.items) == 5

    async def test_a_stable_tiebreaker_orders_identical_timestamps(self, repo, clock):
        ids = []
        for index in range(3):
            don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{200 + index}"
            review = await _seed(
                repo, don, devrev_display_id=f"TKT-{200 + index}"
            )
            ids.append(review.review_id)

        page = await repo.list_reviews(
            ReviewListQuery(statuses=[ReviewStatus.UNREVIEWED], page_size=10)
        )

        assert [item.updated_at for item in page.items] == [clock.now] * 3
        assert [item.review_id for item in page.items] == sorted(ids)

    async def test_one_allowed_facet_is_accepted(self, loaded):
        repo, _backend = await loaded()

        page = await repo.list_reviews(
            ReviewListQuery(statuses=[ReviewStatus.REVIEWED], facets={"topic": "loans"})
        )

        assert page.items
        assert {item.topic for item in page.items} == {"loans"}

    async def test_a_second_facet_fails_before_the_backend_call(self, loaded):
        repo, backend = await loaded()
        backend.queries = 0

        with pytest.raises(UnsupportedFilterCombination):
            await repo.list_reviews(
                ReviewListQuery(
                    statuses=[ReviewStatus.REVIEWED],
                    facets={"topic": "loans", "severity": "high"},
                )
            )

        assert backend.queries == 0

    async def test_a_title_substring_query_fails_before_the_backend_call(self, loaded):
        repo, backend = await loaded()
        backend.queries = 0

        with pytest.raises(UnsupportedFilterCombination):
            await repo.list_reviews(ReviewListQuery(title_contains="401k"))

        assert backend.queries == 0

    async def test_an_unknown_facet_is_refused(self, loaded):
        repo, backend = await loaded()
        backend.queries = 0

        with pytest.raises(UnsupportedFilterCombination):
            await repo.list_reviews(ReviewListQuery(facets={"comments": "secret"}))
        assert backend.queries == 0
        assert ALLOWED_REVIEW_FACETS == frozenset(
            {
                "assigned_reviewer.email",
                "topic",
                "rating",
                "observation_type",
                "remediation_target",
                "severity",
            }
        )

    async def test_an_exact_ticket_id_lookup_cannot_be_combined_with_facets(self, loaded):
        repo, backend = await loaded()
        backend.queries = 0

        with pytest.raises(UnsupportedFilterCombination):
            await repo.list_reviews(
                ReviewListQuery(
                    devrev_display_id="TKT-101", facets={"topic": "loans"}
                )
            )
        with pytest.raises(UnsupportedFilterCombination):
            await repo.list_reviews(
                ReviewListQuery(
                    devrev_display_id="TKT-101", statuses=[ReviewStatus.REVIEWED]
                )
            )
        assert backend.queries == 0

    async def test_the_page_size_is_bounded(self):
        with pytest.raises(ValueError):
            ReviewListQuery(page_size=MAX_PAGE_SIZE + 1)
        with pytest.raises(ValueError):
            ReviewListQuery(page_size=0)
        assert ReviewListQuery().page_size == DEFAULT_PAGE_SIZE

    async def test_reversed_imports_are_hidden_from_the_default_queue(self, repo):
        review = await _seed(repo)
        await repo.mark_import_state(
            review.review_id,
            ImportState.REVERSED,
            expected_version=1,
            context=ADMIN_CONTEXT,
        )

        default = await repo.list_reviews(ReviewListQuery())
        explicit = await repo.list_reviews(ReviewListQuery(include_reversed=True))

        assert [item.review_id for item in default.items] == []
        assert [item.review_id for item in explicit.items] == [review.review_id]


class TestCursors:
    async def test_pagination_walks_every_review_exactly_once(self, repo, clock):
        expected = []
        for index in range(7):
            don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{300 + index}"
            clock.advance(minutes=1)
            expected.append(
                (await _seed(repo, don, devrev_display_id=f"TKT-{300 + index}")).review_id
            )

        seen = []
        cursor = None
        for _ in range(10):
            page = await repo.list_reviews(ReviewListQuery(page_size=3, cursor=cursor))
            seen.extend(item.review_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert sorted(seen) == sorted(expected)
        assert len(seen) == len(set(seen)) == 7

    async def test_a_cursor_is_authenticated_and_carries_no_pii(self, repo, clock):
        # Two reviews really assigned to the reviewer, so a facet-filtered page
        # of size 1 emits a REAL cursor. Asserting on a hand-minted token only
        # would never exercise what list_reviews actually hands to a client.
        for index in range(2):
            don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{600 + index}"
            clock.advance(minutes=1)
            review = await _seed(
                repo, don, devrev_display_id=f"TKT-{600 + index}"
            )
            await repo.patch_review(
                review.review_id,
                ReviewPatch(assigned_reviewer=REVIEWER),
                expected_version=1,
                context=_context(REVIEWER),
            )
        query = ReviewListQuery(
            page_size=1, facets={"assigned_reviewer.email": REVIEWER.email}
        )
        page = await repo.list_reviews(query)

        assert len(page.items) == 1
        assert isinstance(page.next_cursor, str) and page.next_cursor
        token = page.next_cursor
        decoded = json.dumps(await repo.decode_review_cursor_payload(token))
        assert REVIEWER.email not in decoded
        assert "distributions" not in decoded
        assert set(json.loads(decoded)) == {"v", "f", "t", "i"}
        # A raw base64 payload is not a cursor: the token must not decode.
        naive = base64.urlsafe_b64encode(b'{"i":"' + b"a" * 64 + b'"}').decode().rstrip("=")
        with pytest.raises(CursorError):
            await repo.decode_review_cursor_payload(naive)

    async def test_a_cursor_from_another_filter_is_refused(self, repo, clock):
        for index in range(4):
            don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{400 + index}"
            clock.advance(minutes=1)
            await _seed(repo, don, devrev_display_id=f"TKT-{400 + index}")
        first = await repo.list_reviews(ReviewListQuery(page_size=2, facets={"topic": "distributions"}))
        assert first.next_cursor

        with pytest.raises(CursorError):
            await repo.list_reviews(
                ReviewListQuery(page_size=2, facets={"topic": "loans"}, cursor=first.next_cursor)
            )
        with pytest.raises(CursorError):
            await repo.list_reviews(
                ReviewListQuery(page_size=2, statuses=[ReviewStatus.REVIEWED], cursor=first.next_cursor)
            )

    async def test_a_tampered_foreign_or_oversized_cursor_fails_safely(self, repo):
        token = await repo.encode_review_cursor(
            ReviewListQuery(), last_updated_at=T0, last_review_id="a" * 64
        )
        tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")

        for bad in (tampered, "!" * 32, "x" * 4096, ""):
            with pytest.raises(CursorError):
                await repo.list_reviews(ReviewListQuery(cursor=bad))
        # A cursor sealed for another cursor type never opens as a review cursor.
        foreign = await repo.encode_batch_items_cursor("batch-1", last_review_id="a" * 64)
        with pytest.raises(CursorError):
            await repo.list_reviews(ReviewListQuery(cursor=foreign))

    async def test_the_cursor_context_is_domain_separated(self):
        assert REVIEW_LIST_CURSOR_CONTEXT.startswith("tickets-firestore:")
        assert "devrev" not in REVIEW_LIST_CURSOR_CONTEXT


# =====================================================================
# 15-18 + 26. Remediation batches, freezing, leases
# =====================================================================


async def _ready_batch(repo, *, count: int = 2, clock=None):
    refs = []
    for index in range(count):
        don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{500 + index}"
        review = await _seed(repo, don, devrev_display_id=f"TKT-{500 + index}")
        refs.append((review.review_id, review.version))
    batch = await repo.create_batch(
        review_refs=[{"review_id": rid, "review_version": ver} for rid, ver in refs],
        context=ADMIN_CONTEXT,
    )
    batch = await repo.patch_batch(
        batch.batch_id,
        expected_version=batch.version,
        transition=BatchStatus.READY,
        context=ADMIN_CONTEXT,
    )
    return batch, refs


class TestRemediationBatches:
    async def test_creation_freezes_pairs_into_item_documents(self, repo, backend):
        batch, refs = await _ready_batch(repo, count=3)

        items = await backend.dump_subcollection(
            BATCHES_COLLECTION, batch.batch_id, BATCH_ITEMS_SUBCOLLECTION
        )
        parent = (await backend.dump_collection(BATCHES_COLLECTION))[batch.batch_id]

        assert set(items) == {rid for rid, _ in refs}
        assert parent["item_count"] == 3
        assert parent["item_set_digest"] == hashlib.sha256(
            json.dumps(
                sorted([[rid, ver] for rid, ver in refs]),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert "items" not in parent
        assert "review_refs" not in parent
        for review_id, version in refs:
            assert items[review_id]["review_version"] == version

    async def test_empty_duplicate_and_oversized_batches_are_refused_before_any_write(
        self, repo, backend
    ):
        review = await _seed(repo)

        with pytest.raises(ReviewRepositoryError):
            await repo.create_batch(review_refs=[], context=ADMIN_CONTEXT)
        with pytest.raises(ReviewRepositoryError):
            await repo.create_batch(
                review_refs=[
                    {"review_id": review.review_id, "review_version": 1},
                    {"review_id": review.review_id, "review_version": 1},
                ],
                context=ADMIN_CONTEXT,
            )
        with pytest.raises(ReviewRepositoryError):
            await repo.create_batch(
                review_refs=[
                    {"review_id": f"{n:064x}", "review_version": 1}
                    for n in range(MAX_BATCH_REVIEWS + 1)
                ],
                context=ADMIN_CONTEXT,
            )
        assert await backend.dump_collection(BATCHES_COLLECTION) == {}

    async def test_a_frozen_version_must_match_the_stored_review(self, repo):
        review = await _seed(repo)

        with pytest.raises(ReviewVersionConflict):
            await repo.create_batch(
                review_refs=[{"review_id": review.review_id, "review_version": 7}],
                context=ADMIN_CONTEXT,
            )

    async def test_a_hundred_item_batch_stays_within_the_document_and_write_budget(
        self, repo, backend
    ):
        from data_pipeline.ticket_review_repository import (
            FIRESTORE_MAX_DOCUMENT_BYTES,
            FIRESTORE_MAX_WRITES_PER_TRANSACTION,
        )

        refs = []
        for index in range(MAX_BATCH_REVIEWS):
            don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{700 + index}"
            review = await _seed(repo, don, devrev_display_id=f"TKT-{700 + index}")
            refs.append({"review_id": review.review_id, "review_version": review.version})

        batch = await repo.create_batch(review_refs=refs, context=ADMIN_CONTEXT)

        parent = (await backend.dump_collection(BATCHES_COLLECTION))[batch.batch_id]
        assert len(json.dumps(parent, default=str).encode("utf-8")) < FIRESTORE_MAX_DOCUMENT_BYTES
        items = await backend.dump_subcollection(
            BATCHES_COLLECTION, batch.batch_id, BATCH_ITEMS_SUBCOLLECTION
        )
        assert len(items) == MAX_BATCH_REVIEWS
        for doc in items.values():
            assert (
                len(json.dumps(doc, default=str).encode("utf-8"))
                < FIRESTORE_MAX_DOCUMENT_BYTES
            )
        assert backend.max_writes_in_one_transaction <= FIRESTORE_MAX_WRITES_PER_TRANSACTION

        seen, cursor = [], None
        for _ in range(MAX_BATCH_REVIEWS):
            page = await repo.list_batch_items(batch.batch_id, page_size=25, cursor=cursor)
            seen.extend(item.review_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == MAX_BATCH_REVIEWS
        assert all(isinstance(item, RemediationBatchItem) for item in page.items)

    async def test_claim_is_atomic_lease_based_and_idempotent_for_one_agent(
        self, repo, clock
    ):
        batch, _refs = await _ready_batch(repo)

        claim = await repo.claim_batch(
            batch.batch_id, lease_token="lease-token-1", context=AGENT_CONTEXT
        )

        assert claim.batch.status is BatchStatus.CLAIMED
        assert claim.batch.lease is not None
        assert claim.batch.lease.lease_token_hash == sha256_hex("lease-token-1")
        assert claim.batch.lease.holder == AGENT.email
        assert claim.lease_expires_at == clock.now + timedelta(seconds=REMEDIATION_LEASE_S)

        again = await repo.claim_batch(
            batch.batch_id, lease_token="lease-token-1", context=AGENT_CONTEXT
        )
        assert again.batch.version == claim.batch.version
        events = await repo.list_batch_events(batch.batch_id)
        assert [event.event_type for event in events.items].count("batch_claimed") == 1

    async def test_the_raw_lease_token_is_never_persisted(self, repo, backend):
        batch, _refs = await _ready_batch(repo)
        await repo.claim_batch(
            batch.batch_id, lease_token="lease-token-secret", context=AGENT_CONTEXT
        )

        dumped = json.dumps(
            {
                "batches": await backend.dump_collection(BATCHES_COLLECTION),
                "events": await backend.dump_subcollection(
                    BATCHES_COLLECTION, batch.batch_id, BATCH_EVENTS_SUBCOLLECTION
                ),
            },
            default=str,
        )

        assert "lease-token-secret" not in dumped
        assert sha256_hex("lease-token-secret") in dumped

    async def test_a_second_claimant_is_refused_while_the_lease_is_live(self, repo):
        batch, _refs = await _ready_batch(repo)
        await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)

        with pytest.raises(BatchAlreadyClaimed):
            await repo.claim_batch(
                batch.batch_id,
                lease_token="b",
                context=_context(_identity("agent2@example.invalid"), ReviewerRole.AGENT),
            )
        # Even the same agent may not mint a second token; it must heartbeat.
        with pytest.raises(BatchAlreadyClaimed):
            await repo.claim_batch(batch.batch_id, lease_token="c", context=AGENT_CONTEXT)

    async def test_an_expired_claim_can_be_reclaimed_and_is_audited(self, repo, clock):
        batch, _refs = await _ready_batch(repo)
        await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        clock.advance(seconds=REMEDIATION_LEASE_S + 1)

        other = _context(_identity("agent2@example.invalid"), ReviewerRole.AGENT)
        reclaim = await repo.claim_batch(batch.batch_id, lease_token="b", context=other)

        assert reclaim.batch.lease.holder == "agent2@example.invalid"
        assert reclaim.batch.lease.lease_token_hash == sha256_hex("b")
        types = [event.event_type for event in (await repo.list_batch_events(batch.batch_id)).items]
        assert "batch_lease_reclaimed" in types

    async def test_a_heartbeat_enforces_token_owner_expiry_and_version(self, repo, clock):
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        clock.advance(minutes=1)

        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=claim.batch.version,
                lease_token="wrong",
                context=AGENT_CONTEXT,
            )
        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=claim.batch.version,
                lease_token="a",
                context=_context(_identity("agent2@example.invalid"), ReviewerRole.AGENT),
            )
        with pytest.raises(BatchVersionConflict):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=claim.batch.version + 5,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

        renewed = await repo.heartbeat_batch(
            batch.batch_id,
            expected_version=claim.batch.version,
            lease_token="a",
            context=AGENT_CONTEXT,
        )
        assert renewed.lease.expires_at == clock.now + timedelta(seconds=REMEDIATION_LEASE_S)
        assert renewed.lease.last_heartbeat_at == clock.now
        assert renewed.lease.continuous_since == claim.batch.lease.continuous_since

    async def test_a_heartbeat_after_the_lease_expired_is_lost(self, repo, clock):
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        clock.advance(seconds=REMEDIATION_LEASE_S + 1)

        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=claim.batch.version,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

    async def test_the_two_hour_continuous_cap_stops_renewal(self, repo, clock):
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        current = claim.batch

        elapsed = 0
        while elapsed + 300 < REMEDIATION_MAX_CONTINUOUS_LEASE_S:
            clock.advance(seconds=300)
            elapsed += 300
            current = await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=current.version,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

        clock.advance(seconds=300)
        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=current.version,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

    async def test_an_admin_may_grant_exactly_one_bounded_extension(self, repo, clock):
        # The real flow: the agent heartbeats up to the continuous cap, so its
        # 15-minute lease is still LIVE when the admin grants the extension.
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        current = claim.batch
        elapsed = 0
        while elapsed + 300 < REMEDIATION_MAX_CONTINUOUS_LEASE_S:
            clock.advance(seconds=300)
            elapsed += 300
            current = await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=current.version,
                lease_token="a",
                context=AGENT_CONTEXT,
            )
        clock.advance(seconds=300)
        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=current.version,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

        extended = await repo.extend_lease(
            batch.batch_id,
            expected_version=current.version,
            additional_minutes=10,
            reason="release window",
            context=ADMIN_CONTEXT,
        )

        assert extended.version == current.version + 1
        # The extension is worth exactly the minutes it audited: renewal works
        # inside the granted window and stops at its end.
        clock.advance(seconds=300)
        renewed = await repo.heartbeat_batch(
            batch.batch_id,
            expected_version=extended.version,
            lease_token="a",
            context=AGENT_CONTEXT,
        )
        clock.advance(seconds=301)
        with pytest.raises(BatchLeaseLost):
            await repo.heartbeat_batch(
                batch.batch_id,
                expected_version=renewed.version,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

        with pytest.raises(LeaseExtensionRefused):
            await repo.extend_lease(
                batch.batch_id,
                expected_version=renewed.version,
                additional_minutes=10,
                reason="again",
                context=ADMIN_CONTEXT,
            )
        with pytest.raises(LeaseExtensionRefused):
            await repo.extend_lease(
                batch.batch_id,
                expected_version=renewed.version,
                additional_minutes=121,
                reason="too long",
                context=ADMIN_CONTEXT,
            )
        with pytest.raises(LeaseExtensionRefused):
            await repo.extend_lease(
                batch.batch_id,
                expected_version=renewed.version,
                additional_minutes=30,
                reason="not an admin",
                context=AGENT_CONTEXT,
            )

    async def test_progress_and_results_require_the_lease(self, repo, clock):
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)

        planning = await repo.patch_batch(
            batch.batch_id,
            expected_version=claim.batch.version,
            transition=BatchStatus.PLANNING,
            lease_token="a",
            plan_artifact="plan: fix the retrieval gap",
            context=AGENT_CONTEXT,
        )
        assert planning.status is BatchStatus.PLANNING
        assert planning.plan_artifact == "plan: fix the retrieval gap"

        with pytest.raises(BatchLeaseLost):
            await repo.patch_batch(
                batch.batch_id,
                expected_version=planning.version,
                transition=BatchStatus.IN_PROGRESS,
                lease_token="wrong",
                context=AGENT_CONTEXT,
            )
        with pytest.raises(BatchVersionConflict):
            await repo.patch_batch(
                batch.batch_id,
                expected_version=planning.version + 3,
                lease_token="a",
                branch="fix/gap",
                context=AGENT_CONTEXT,
            )
        with pytest.raises(InvalidBatchTransition):
            await repo.patch_batch(
                batch.batch_id,
                expected_version=planning.version,
                transition=BatchStatus.COMPLETED,
                lease_token="a",
                context=AGENT_CONTEXT,
            )

    async def test_release_to_ready_is_refused_once_durable_work_exists(self, repo):
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        planned = await repo.patch_batch(
            batch.batch_id,
            expected_version=claim.batch.version,
            transition=BatchStatus.PLANNING,
            lease_token="a",
            plan_artifact="plan",
            context=AGENT_CONTEXT,
        )

        with pytest.raises(BatchReleaseRefused):
            await repo.release_batch(
                batch.batch_id,
                expected_version=planned.version,
                lease_token="a",
                disposition=BatchStatus.READY,
                reason="give it back",
                context=AGENT_CONTEXT,
            )

        blocked = await repo.release_batch(
            batch.batch_id,
            expected_version=planned.version,
            lease_token="a",
            disposition=BatchStatus.BLOCKED,
            reason="agent stopped",
            context=AGENT_CONTEXT,
        )

        assert blocked.status is BatchStatus.BLOCKED
        assert blocked.lease is None

    async def test_release_before_any_work_returns_the_batch_to_ready(self, repo):
        batch, _refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)

        released = await repo.release_batch(
            batch.batch_id,
            expected_version=claim.batch.version,
            lease_token="a",
            disposition=BatchStatus.READY,
            reason="wrong agent",
            context=AGENT_CONTEXT,
        )

        assert released.status is BatchStatus.READY
        assert released.lease is None

    async def test_release_refuses_ready_when_a_frozen_version_drifted(self, repo):
        batch, refs = await _ready_batch(repo)
        claim = await repo.claim_batch(batch.batch_id, lease_token="a", context=AGENT_CONTEXT)
        await repo.patch_review(
            refs[0][0],
            ReviewPatch(rating=2),
            expected_version=refs[0][1],
            context=_context(),
        )

        with pytest.raises(BatchReleaseRefused):
            await repo.release_batch(
                batch.batch_id,
                expected_version=claim.batch.version,
                lease_token="a",
                disposition=BatchStatus.READY,
                reason="drifted",
                context=AGENT_CONTEXT,
            )

    async def test_a_missing_batch_raises_a_typed_not_found(self, repo):
        with pytest.raises(BatchNotFound):
            await repo.get_batch("nope")
        with pytest.raises(BatchNotFound):
            await repo.claim_batch("nope", lease_token="a", context=AGENT_CONTEXT)

    async def test_batch_documents_carry_retention_not_ttl(self, repo, backend, clock):
        batch, _refs = await _ready_batch(repo)

        parent = (await backend.dump_collection(BATCHES_COLLECTION))[batch.batch_id]
        items = await backend.dump_subcollection(
            BATCHES_COLLECTION, batch.batch_id, BATCH_ITEMS_SUBCOLLECTION
        )
        events = await backend.dump_subcollection(
            BATCHES_COLLECTION, batch.batch_id, BATCH_EVENTS_SUBCOLLECTION
        )

        assert TTL_FIELD not in parent
        assert parent[RETENTION_FIELD] == clock.now + timedelta(days=REVIEW_RETENTION_DAYS)
        for doc in items.values():
            assert TTL_FIELD not in doc
            assert doc[RETENTION_FIELD] == clock.now + timedelta(days=REVIEW_RETENTION_DAYS)
        for doc in events.values():
            assert TTL_FIELD not in doc
            assert doc[RETENTION_FIELD] == clock.now + timedelta(days=AUDIT_RETENTION_DAYS)


# =====================================================================
# 20. Partial multi-review updates
# =====================================================================


class TestMultiReviewUpdates:
    async def test_a_partial_update_never_resolves_an_unaffected_review(self, repo):
        first = await _seed(repo)
        second = await _seed(repo, OTHER_DON, devrev_display_id="TKT-9999")
        current = await _advance_to(repo, first.review_id, ReviewStatus.VERIFYING)

        result = await repo.patch_reviews(
            [
                ReviewPatchSpec(
                    review_id=first.review_id,
                    patch=ReviewPatch(
                        status=ReviewStatus.RESOLVED, resolution=_resolution()
                    ),
                    expected_version=current.version,
                ),
                ReviewPatchSpec(
                    review_id=second.review_id,
                    patch=ReviewPatch(status=ReviewStatus.RESOLVED, resolution=_resolution()),
                    expected_version=second.version,
                ),
            ],
            context=_context(),
        )

        assert [item.review_id for item in result.applied] == [first.review_id]
        assert [failure.review_id for failure in result.failures] == [second.review_id]
        assert (await repo.get_review(first.review_id)).status is ReviewStatus.RESOLVED
        # The unaffected review keeps its status and its version.
        untouched = await repo.get_review(second.review_id)
        assert untouched.status is ReviewStatus.UNREVIEWED
        assert untouched.version == second.version

    async def test_a_version_conflict_in_one_spec_does_not_block_the_others(self, repo):
        first = await _seed(repo)
        second = await _seed(repo, OTHER_DON, devrev_display_id="TKT-9999")

        result = await repo.patch_reviews(
            [
                ReviewPatchSpec(
                    review_id=first.review_id,
                    patch=ReviewPatch(rating=3),
                    expected_version=99,
                ),
                ReviewPatchSpec(
                    review_id=second.review_id,
                    patch=ReviewPatch(rating=4),
                    expected_version=second.version,
                ),
            ],
            context=_context(),
        )

        assert [item.review_id for item in result.applied] == [second.review_id]
        assert result.conflicts[0].review_id == first.review_id
        assert result.conflicts[0].current_version == 1
        assert (await repo.get_review(first.review_id)).rating is None
        assert (await repo.get_review(second.review_id)).rating == 4


# =====================================================================
# 21. Idempotency keys
# =====================================================================


class TestIdempotency:
    async def test_a_repeated_key_is_deduplicated(self, repo, backend):
        review = await _seed(repo)
        context = _context(idempotency_key="patch-1")

        first = await repo.patch_review(
            review.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )
        replay = await repo.patch_review(
            review.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )

        assert replay.version == first.version == 2
        assert len((await repo.list_audit_events(review.review_id)).items) == 2
        keys = await backend.dump_collection(IDEMPOTENCY_KEYS_COLLECTION)
        assert list(keys) == [sha256_hex("patch-1")]
        assert "patch-1" not in json.dumps(keys, default=str)
        assert isinstance(keys[sha256_hex("patch-1")][TTL_FIELD], datetime)

    async def test_the_same_key_with_a_different_request_is_refused(self, repo):
        review = await _seed(repo)
        context = _context(idempotency_key="patch-1")
        await repo.patch_review(
            review.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )

        with pytest.raises(IdempotencyConflict):
            await repo.patch_review(
                review.review_id,
                ReviewPatch(rating=1),
                expected_version=2,
                context=context,
            )
        assert (await repo.get_review(review.review_id)).rating == 5

    async def test_every_unsafe_operation_honours_the_key(self, repo):
        review = await _seed(repo)
        batch_context = _context(ADMIN, ReviewerRole.ADMIN, idempotency_key="batch-1")

        first = await repo.create_batch(
            review_refs=[{"review_id": review.review_id, "review_version": 1}],
            context=batch_context,
        )
        replay = await repo.create_batch(
            review_refs=[{"review_id": review.review_id, "review_version": 1}],
            context=batch_context,
        )

        assert replay.batch_id == first.batch_id
        assert replay.version == first.version

        link_context = _context(idempotency_key="link-1")
        link, updated = await repo.create_evidence_link(
            review.review_id,
            candidate=_candidate(review.review_id),
            reason="matches",
            expected_version=1,
            context=link_context,
        )
        again, unchanged = await repo.create_evidence_link(
            review.review_id,
            candidate=_candidate(review.review_id),
            reason="matches",
            expected_version=1,
            context=link_context,
        )
        assert again.link_id == link.link_id
        assert unchanged.version == updated.version

    async def test_an_expired_idempotency_record_does_not_replay(self, repo, clock):
        from api.ticket_review_models import IDEMPOTENCY_TTL_S

        review = await _seed(repo)
        context = _context(idempotency_key="patch-1")
        await repo.patch_review(
            review.review_id, ReviewPatch(rating=5), expected_version=1, context=context
        )
        clock.advance(seconds=IDEMPOTENCY_TTL_S + 1)

        with pytest.raises(ReviewVersionConflict):
            await repo.patch_review(
                review.review_id, ReviewPatch(rating=5), expected_version=1, context=context
            )


# =====================================================================
# 22. Import apply/reverse and global events
# =====================================================================


def _import(**overrides) -> TicketImport:
    values = {
        "import_id": "imp-1",
        "file_sha256": "1" * 64,
        "plan_sha256": "2" * 64,
        "created_by": ADMIN,
        "total_rows": 2,
    }
    values.update(overrides)
    return TicketImport(**values)


class TestImportAndExport:
    async def test_apply_follows_expected_versions_and_preserves_conflicts(self, repo):
        review = await _seed(repo)
        stale = await _seed(repo, OTHER_DON, devrev_display_id="TKT-9999")
        await repo.patch_review(
            stale.review_id, ReviewPatch(rating=2), expected_version=1, context=_context()
        )
        record = await repo.create_import(_import(), context=ADMIN_CONTEXT)

        result = await repo.apply_import_rows(
            record.import_id,
            [
                ImportRowSpec(
                    row_number=1,
                    review_id=review.review_id,
                    expected_review_version=1,
                    patch=ReviewPatch(topic="imported topic", rating=3),
                ),
                ImportRowSpec(
                    row_number=2,
                    review_id=stale.review_id,
                    expected_review_version=1,
                    patch=ReviewPatch(rating=5),
                ),
            ],
            context=ADMIN_CONTEXT,
        )

        assert result.applied_rows == 1
        assert result.conflicted_rows == 1
        assert (await repo.get_review(review.review_id)).topic == "imported topic"
        assert (await repo.get_review(stale.review_id)).rating == 2
        summary = await repo.get_import(record.import_id)
        assert summary.applied_rows == 1
        assert summary.conflicted_rows == 1
        rows = await repo.list_import_rows(record.import_id)
        assert {row.row_number for row in rows.items} == {1, 2}
        conflicted = next(row for row in rows.items if row.row_number == 2)
        assert conflicted.error_code == "review_version_conflict"

    async def test_reversal_never_deletes_history(self, repo, backend):
        review = await _seed(repo)
        record = await repo.create_import(_import(total_rows=1), context=ADMIN_CONTEXT)
        await repo.apply_import_rows(
            record.import_id,
            [
                ImportRowSpec(
                    row_number=1,
                    review_id=review.review_id,
                    expected_review_version=1,
                    patch=ReviewPatch(rating=4),
                    created_by_import=True,
                )
            ],
            context=ADMIN_CONTEXT,
        )
        applied = await repo.get_review(review.review_id)

        reversal = await repo.reverse_import_rows(
            record.import_id,
            [
                ImportRowSpec(
                    row_number=1,
                    review_id=review.review_id,
                    expected_review_version=applied.version,
                    patch=ReviewPatch(),
                    created_by_import=True,
                )
            ],
            context=ADMIN_CONTEXT,
        )

        assert reversal.reversed_rows == 1
        reversed_review = await repo.get_review(review.review_id)
        assert reversed_review.import_state is ImportState.REVERSED
        assert reversed_review.version == applied.version + 1
        # History is preserved: nothing is deleted and the ledger grows.
        assert review.review_id in await backend.dump_collection(REVIEWS_COLLECTION)
        events = (await repo.list_audit_events(review.review_id)).items
        assert [event.event_type for event in events][-1] == "review_import_reversed"
        assert (await repo.verify_audit_chain(review.review_id)).intact is True

    async def test_import_and_export_events_enter_the_global_ledger(self, repo, backend):
        record = await repo.create_import(_import(), context=ADMIN_CONTEXT)
        await repo.create_export(
            TicketExportSummary(
                export_id="exp-1",
                created_by=ADMIN,
                row_count=7,
                file_sha256="a" * 64,
                filter_fingerprint="b" * 64,
            ),
            context=ADMIN_CONTEXT,
        )

        page = await repo.list_global_audit_events()

        types = [event.event_type for event in page.items]
        assert "import_created" in types
        assert "export_created" in types
        assert {event.parent_kind for event in page.items} == {"import", "export"}
        previous = GENESIS_EVENT_HASH
        for event in page.items:
            assert event.previous_event_hash == previous
            assert event.event_hash == compute_audit_event_hash(event)
            previous = event.event_hash
        # The chain head bookkeeping document is never returned as an event.
        assert GLOBAL_CHAIN_HEAD_DOC_ID in await backend.dump_collection(
            GLOBAL_AUDIT_EVENTS_COLLECTION
        )
        assert all(event.event_id != GLOBAL_CHAIN_HEAD_DOC_ID for event in page.items)
        assert (await repo.verify_global_audit_chain()).intact is True
        assert record.import_id == "imp-1"

    async def test_staged_import_rows_are_disposable_and_durable_rows_are_not(
        self, repo, backend, clock
    ):
        from api.ticket_review_models import IMPORT_STAGING_TTL_S

        record = await repo.create_import(_import(), context=ADMIN_CONTEXT)
        await repo.stage_import_rows(
            record.import_id,
            [{"row_number": 1, "raw_ticket_id": "TKT-1234", "rating": 3}],
        )

        staged = await backend.dump_collection(IMPORT_STAGING_COLLECTION)
        assert staged
        for doc in staged.values():
            assert doc[TTL_FIELD] == clock.now + timedelta(seconds=IMPORT_STAGING_TTL_S)
            assert RETENTION_FIELD not in doc

        imports = await backend.dump_collection(IMPORTS_COLLECTION)
        assert TTL_FIELD not in imports[record.import_id]
        assert imports[record.import_id][RETENTION_FIELD] == clock.now + timedelta(
            days=REVIEW_RETENTION_DAYS
        )

    async def test_an_import_status_change_follows_the_closed_table(self, repo):
        record = await repo.create_import(_import(), context=ADMIN_CONTEXT)

        planned = await repo.patch_import(
            record.import_id,
            expected_version=record.version,
            transition=ImportStatus.PLANNED,
            context=ADMIN_CONTEXT,
        )
        assert planned.status is ImportStatus.PLANNED

        with pytest.raises(ReviewRepositoryError):
            await repo.patch_import(
                record.import_id,
                expected_version=planned.version,
                transition=ImportStatus.REVERSED,
                context=ADMIN_CONTEXT,
            )


# =====================================================================
# 23-24. TTL vs retention separation, database selection
# =====================================================================


class TestStorageSeparation:
    def test_only_disposable_collections_declare_a_ttl(self):
        assert TTL_COLLECTIONS == frozenset(
            {
                CONSOLE_CACHE_COLLECTION,
                DEVREV_MESSAGE_CACHE_COLLECTION,
                IMPORT_STAGING_COLLECTION,
                IDEMPOTENCY_KEYS_COLLECTION,
            }
        )
        assert canonical_ttl_declarations() == [
            {"collectionGroup": collection, "fieldPath": TTL_FIELD, "ttl": True}
            for collection in sorted(TTL_COLLECTIONS)
        ]
        for durable in (
            REVIEWS_COLLECTION,
            BATCHES_COLLECTION,
            IMPORTS_COLLECTION,
            EXPORTS_COLLECTION,
            GLOBAL_AUDIT_EVENTS_COLLECTION,
            AUDIT_EVENTS_SUBCOLLECTION,
            EVIDENCE_LINKS_SUBCOLLECTION,
            BATCH_ITEMS_SUBCOLLECTION,
            BATCH_EVENTS_SUBCOLLECTION,
            IMPORT_ROWS_SUBCOLLECTION,
        ):
            assert durable not in TTL_COLLECTIONS

    def test_the_declared_indexes_match_only_the_master_query_grammar(self):
        declarations = review_index_declarations()

        assert declarations[0] == {
            "collectionGroup": REVIEWS_COLLECTION,
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": "status", "order": "ASCENDING"},
                {"fieldPath": "updated_at", "order": "DESCENDING"},
                {"fieldPath": "review_id", "order": "ASCENDING"},
            ],
        }
        facet_indexes = declarations[1:]
        assert len(facet_indexes) == len(ALLOWED_REVIEW_FACETS)
        for declaration in facet_indexes:
            paths = [field["fieldPath"] for field in declaration["fields"]]
            assert paths[0] == "status"
            assert paths[1] in ALLOWED_REVIEW_FACETS
            assert paths[2:] == ["updated_at", "review_id"]
            orders = [field["order"] for field in declaration["fields"]]
            assert orders == ["ASCENDING", "ASCENDING", "DESCENDING", "ASCENDING"]
        # No Cartesian product, no title/prefix tokens.
        assert len(declarations) == 1 + len(ALLOWED_REVIEW_FACETS)
        rendered = json.dumps(declarations)
        for forbidden in ("title", "comments", "prefix", "tokens", "__name__"):
            assert forbidden not in rendered

    def test_the_declared_indexes_are_mirrored_in_the_canonical_json(self):
        from pathlib import Path

        mirror = json.loads(
            (Path(__file__).resolve().parents[1] / "firestore.indexes.json").read_text()
        )
        console = [
            {key: value for key, value in index.items() if not key.startswith("__")}
            for index in mirror["indexes"]
            if index["collectionGroup"] == REVIEWS_COLLECTION
        ]

        assert console == review_index_declarations()
        ttls = {
            (field["collectionGroup"], field["fieldPath"])
            for field in mirror["fieldOverrides"]
            if field.get("ttl") is True
        }
        for collection in TTL_COLLECTIONS:
            assert (collection, TTL_FIELD) in ttls
        for durable in (REVIEWS_COLLECTION, BATCHES_COLLECTION, EXPORTS_COLLECTION):
            assert (durable, TTL_FIELD) not in ttls
            assert (durable, RETENTION_FIELD) not in ttls
        # The ticket handler's declarations survive.
        handler = {
            tuple((field["fieldPath"], field["order"]) for field in index["fields"])
            for index in mirror["indexes"]
            if index["collectionGroup"] == "ticket_jobs"
        }
        assert (("state", "ASCENDING"), ("lease_expires_at", "ASCENDING")) in handler
        assert len(handler) == 4


class TestDatabaseSelection:
    def test_the_console_databases_are_frozen_per_environment(self):
        assert STAGING_FIRESTORE_DATABASE == "tickets-console-staging"
        assert PRODUCTION_FIRESTORE_DATABASE == "tickets-console-prod"
        assert LOCAL_FIRESTORE_DATABASE == "tickets-console-emulator"

    def test_staging_and_production_refuse_the_default_database(self):
        for environment in ("staging", "production"):
            with pytest.raises(ValueError, match="never \\(default\\)"):
                resolve_tickets_firestore_database("(default)", environment=environment)

    def test_a_blank_database_is_refused_in_every_environment(self):
        for environment in ("local", "staging", "production"):
            with pytest.raises(ValueError, match="explicitly"):
                resolve_tickets_firestore_database("", environment=environment)

    def test_each_strict_environment_pins_its_own_database(self):
        assert (
            resolve_tickets_firestore_database(
                STAGING_FIRESTORE_DATABASE, environment="staging"
            )
            == STAGING_FIRESTORE_DATABASE
        )
        with pytest.raises(ValueError):
            resolve_tickets_firestore_database(
                STAGING_FIRESTORE_DATABASE, environment="production"
            )

    def test_the_firestore_backend_refuses_the_default_database(self):
        for environment in ("staging", "production"):
            with pytest.raises(ValueError, match="never \\(default\\)"):
                FirestoreTicketReviewBackend(
                    project="rag-kb-system",
                    database="(default)",
                    environment=environment,
                )
        with pytest.raises(ValueError):
            FirestoreTicketReviewBackend(
                project="rag-kb-system", database="", environment="local"
            )

    def test_the_backend_never_emulates_isolation_with_a_prefix(self):
        from pathlib import Path

        assert not hasattr(FirestoreTicketReviewBackend, "collection_prefix")
        # Read the module source, not a docstring: __init__ has none, so the
        # obvious assertion would be vacuously true. A prefix could only be
        # introduced as an attribute or a path-building concatenation, and
        # neither may exist anywhere in this module.
        source = Path("data_pipeline/ticket_review_repository.py").read_text()
        assert "collection_prefix" not in source
        assert "_prefix" not in source
        # The class docstring is where the rule is stated.
        assert "prefix" in (FirestoreTicketReviewBackend.__doc__ or "").lower()


# =====================================================================
# 27-28. Bounded, non-cascading retention
# =====================================================================


class TestRetention:
    async def _aged_review(self, repo, clock, don=SYNTHETIC_DON, display="TKT-1234"):
        # Populate every PII-bearing durable field, or a tombstone that leaked
        # one of them would still pass the leak assertions below.
        review = await _seed(
            repo,
            don,
            devrev_display_id=display,
            assigned_reviewer=REVIEWER,
            legacy_reviewer_display_name="Jane From The Sheet",
            expected_behavior="the bot should have cited the 401k article",
        )
        current = await _advance_to(repo, review.review_id, ReviewStatus.VERIFYING)
        resolved = await repo.patch_review(
            review.review_id,
            ReviewPatch(status=ReviewStatus.RESOLVED, resolution=_resolution()),
            expected_version=current.version,
            context=_context(),
        )
        await repo.create_evidence_link(
            review.review_id,
            candidate=_candidate(review.review_id),
            reason="matches",
            expected_version=resolved.version,
            context=_context(),
        )
        return await repo.get_review(review.review_id)

    async def test_preview_is_bounded_and_reports_nothing_before_the_horizon(
        self, repo, clock
    ):
        await self._aged_review(repo, clock)

        preview = await repo.preview_expired(max_documents=10)
        assert preview.product_candidates == []
        assert preview.ledger_candidates == []

        clock.advance(days=REVIEW_RETENTION_DAYS + 1)
        preview = await repo.preview_expired(max_documents=10)
        assert len(preview.product_candidates) == 1
        assert preview.ledger_candidates == []
        assert preview.truncated is False

    async def test_preview_is_truncated_rather_than_unbounded(self, repo, clock):
        for index in range(4):
            don = f"don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/{800 + index}"
            await self._aged_review(repo, clock, don, f"TKT-{800 + index}")
        clock.advance(days=REVIEW_RETENTION_DAYS + 1)

        preview = await repo.preview_expired(max_documents=2)

        assert len(preview.product_candidates) == 2
        assert preview.truncated is True

    async def test_a_product_purge_leaves_a_content_free_tombstone(
        self, repo, backend, clock
    ):
        review = await self._aged_review(repo, clock, SYNTHETIC_DON)
        clock.advance(days=REVIEW_RETENTION_DAYS + 1)

        report = await repo.purge_expired(max_documents=10, run_id="run-1", context=ADMIN_CONTEXT)

        assert report.tombstoned == [review.review_id]
        assert report.parents_deleted == []
        doc = (await backend.dump_collection(REVIEWS_COLLECTION))[review.review_id]
        assert doc["doc_kind"] == PURGED_TOMBSTONE_KIND
        # A LITERAL allowlist, not the module constant the implementation builds
        # from: otherwise adding a field to both sides in one edit still passes.
        assert set(doc) == {
            "doc_kind",
            "parent_id_hash",
            "schema_version",
            "purged_at",
            "ledger_expires_at",
            "legal_hold",
            "audit_chain_head",
        }
        assert set(doc) == TOMBSTONE_FIELDS
        rendered = json.dumps(doc, default=str)
        for leak in (
            SYNTHETIC_DON,
            "TKT-1234",
            "distributions",
            "reviewer observation",
            "the bot should have cited the 401k article",
            "Jane From The Sheet",
            REVIEWER.email,
            REVIEWER.subject,
            "Synthetic Reviewer",
            "ticket-job:0123456789abcdef",
            "suite green",
        ):
            assert leak not in rendered
        with pytest.raises(ReviewNotFound):
            await repo.get_review(review.review_id)

    async def test_the_audit_ledger_survives_the_product_purge(self, repo, backend, clock):
        review = await self._aged_review(repo, clock)
        events_before = await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        )
        clock.advance(days=REVIEW_RETENTION_DAYS + 1)

        await repo.purge_expired(max_documents=10, run_id="run-1", context=ADMIN_CONTEXT)

        events_after = await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        )
        assert set(events_after) == set(events_before)
        links = await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, EVIDENCE_LINKS_SUBCOLLECTION
        )
        assert links == {}
        assert (await repo.list_audit_events(review.review_id)).items

    async def test_a_legal_hold_suppresses_both_policies(self, repo, backend, clock):
        review = await self._aged_review(repo, clock)
        await repo.set_legal_hold(review.review_id, True, context=ADMIN_CONTEXT)
        clock.advance(days=AUDIT_RETENTION_DAYS + 1)

        preview = await repo.preview_expired(max_documents=10)
        report = await repo.purge_expired(max_documents=10, run_id="run-1", context=ADMIN_CONTEXT)

        assert preview.skipped_legal_hold == [review.review_id]
        assert report.tombstoned == []
        assert report.ledger_events_deleted == []
        assert review.review_id in await backend.dump_collection(REVIEWS_COLLECTION)
        assert await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        )

    async def test_the_ledger_is_deleted_only_after_the_audit_horizon(
        self, repo, backend, clock
    ):
        review = await self._aged_review(repo, clock)
        clock.advance(days=REVIEW_RETENTION_DAYS + 1)
        await repo.purge_expired(max_documents=50, run_id="run-1", context=ADMIN_CONTEXT)

        # Between 730 and 2,555 days the ledger is still untouchable.
        clock.advance(days=AUDIT_RETENTION_DAYS - REVIEW_RETENTION_DAYS - 5)
        mid = await repo.purge_expired(max_documents=50, run_id="run-2", context=ADMIN_CONTEXT)
        assert mid.ledger_events_deleted == []
        assert await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        )

        clock.advance(days=10)
        final = await repo.purge_expired(max_documents=50, run_id="run-3", context=ADMIN_CONTEXT)

        assert final.ledger_events_deleted
        assert final.parents_deleted == [review.review_id]
        assert await backend.dump_subcollection(
            REVIEWS_COLLECTION, review.review_id, AUDIT_EVENTS_SUBCOLLECTION
        ) == {}
        assert review.review_id not in await backend.dump_collection(REVIEWS_COLLECTION)

    async def test_the_tombstone_is_removed_after_its_events_never_before(
        self, repo, backend, clock
    ):
        review = await self._aged_review(repo, clock)
        clock.advance(days=AUDIT_RETENTION_DAYS + 1)

        report = await repo.purge_expired(
            max_documents=2, run_id="run-1", context=ADMIN_CONTEXT
        )

        # A bounded run deletes exact event ids first; the tombstone waits for
        # a later run, so a truncated pass can never orphan the ledger.
        assert report.truncated is True
        assert report.parents_deleted == []
        assert review.review_id in await backend.dump_collection(REVIEWS_COLLECTION)

        for run in range(2, 12):
            report = await repo.purge_expired(
                max_documents=2, run_id=f"run-{run}", context=ADMIN_CONTEXT
            )
            if not report.truncated:
                break

        assert review.review_id not in await backend.dump_collection(REVIEWS_COLLECTION)

    async def test_purging_is_idempotent(self, repo, backend, clock):
        await self._aged_review(repo, clock)
        clock.advance(days=REVIEW_RETENTION_DAYS + 1)

        first = await repo.purge_expired(max_documents=10, run_id="run-1", context=ADMIN_CONTEXT)
        snapshot = await backend.dump_collection(REVIEWS_COLLECTION)
        second = await repo.purge_expired(max_documents=10, run_id="run-1", context=ADMIN_CONTEXT)
        third = await repo.purge_expired(max_documents=10, run_id="run-2", context=ADMIN_CONTEXT)

        assert first.tombstoned
        assert second.tombstoned == []
        assert third.tombstoned == []
        assert await backend.dump_collection(REVIEWS_COLLECTION) == snapshot

    async def test_the_purge_appends_a_content_free_global_event(self, repo, clock):
        review = await self._aged_review(repo, clock)
        clock.advance(days=REVIEW_RETENTION_DAYS + 1)

        await repo.purge_expired(max_documents=10, run_id="run-1", context=ADMIN_CONTEXT)

        page = await repo.list_global_audit_events()
        event = page.items[-1]
        assert event.event_type == "retention_purged"
        assert event.parent_kind == "retention"
        rendered = json.dumps(event.model_dump(mode="json"))
        for leak in (SYNTHETIC_DON, "TKT-1234", review.review_id, REVIEWER.email):
            assert leak not in rendered
        assert event.event_hash == compute_audit_event_hash(event)

    async def test_retention_never_uses_a_recursive_delete(self, repo, backend, clock):
        await self._aged_review(repo, clock)
        clock.advance(days=AUDIT_RETENTION_DAYS + 1)

        await repo.purge_expired(max_documents=200, run_id="run-1", context=ADMIN_CONTEXT)

        assert not hasattr(backend, "delete_collection")
        assert not hasattr(backend, "recursive_delete")
        assert backend.deleted_paths
        for path in backend.deleted_paths:
            # Every delete names an exact document, never a collection.
            assert len(path) % 2 == 0

    async def test_disposable_documents_are_swept_by_exact_id(self, repo, backend, clock):
        await repo.upsert_message_cache_entry(_cache_entry())
        review = await _seed(repo)
        clock.advance(seconds=MESSAGE_CACHE_TTL_S + 1)

        report = await repo.purge_expired(
            max_documents=10, run_id="run-1", context=ADMIN_CONTEXT
        )

        assert report.disposable_deleted
        assert await backend.dump_collection(DEVREV_MESSAGE_CACHE_COLLECTION) == {}
        # A cache sweep never touches the durable review or its ledger.
        assert review.review_id in await backend.dump_collection(REVIEWS_COLLECTION)
        assert (await repo.list_audit_events(review.review_id)).items
