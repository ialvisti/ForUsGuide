"""Frozen contracts for the /tickets review console (Stage 1).

This module freezes the domain enums, bounds, state machines, ETag/precondition
semantics, cursor authenticated-encryption, and the three isolated settings
boundaries BEFORE any network, Firestore, UI, or infrastructure code exists.

Every numeric/string/list bound asserted here comes from the "Canonical limits
and defaults" table of the master plan. Changing one requires an ADR and
cross-layer contract tests, so these assertions are deliberately literal.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api.ticket_review_models import (
    AUDIT_RETENTION_DAYS,
    CACHE_TTL_S,
    CSRF_TOKEN_TTL_S,
    CURSOR_AEAD_KEY_BYTES,
    CURSOR_DEFAULT_TTL_S,
    CURSOR_EXPIRY_KEY,
    DEFAULT_PAGE_SIZE,
    DEVREV_CONNECT_TIMEOUT_S,
    DEVREV_MAX_ENTRIES,
    DEVREV_MAX_PAGES,
    DEVREV_MAX_RESPONSE_BYTES,
    DEVREV_MAX_RETRIES,
    DEVREV_READ_TIMEOUT_S,
    DEVREV_RETRY_AFTER_CAP_S,
    EVIDENCE_BROKER_MAX_RESPONSE_BYTES,
    FIRESTORE_MAX_DOCUMENT_BYTES,
    GENESIS_EVENT_HASH,
    HASH_SCHEMA_VERSION,
    IDEMPOTENCY_TTL_S,
    IMPORT_STAGING_TTL_S,
    MAX_ATTACHMENTS,
    MAX_BATCH_REVIEWS,
    MAX_COMMENTS_LENGTH,
    MAX_CSV_REQUEST_BYTES,
    MAX_CSV_ROWS,
    MAX_CURSOR_LENGTH,
    MAX_DISPLAY_ID_LENGTH,
    MAX_EVIDENCE_REFS_PER_REVIEW,
    MAX_EXPECTED_BEHAVIOR_LENGTH,
    MAX_ID_LENGTH,
    MAX_IF_MATCH_HEADER_LENGTH,
    MAX_JSON_REQUEST_BYTES,
    MAX_LEASE_EXTENSION_MINUTES,
    MAX_LEGACY_TYPE_LENGTH,
    MAX_LIST_ITEMS,
    MAX_MESSAGE_BODY_LENGTH,
    MAX_METADATA_KEYS,
    MAX_METADATA_VALUE_LENGTH,
    MAX_PAGE_SIZE,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TOPIC_LENGTH,
    MAX_UPSTREAM_ERROR_BODY_BYTES,
    MAX_URL_LENGTH,
    MESSAGE_CACHE_TTL_S,
    MIN_TOUCH_TARGET_PX,
    RESPONSIVE_BREAKPOINT_PX,
    REMEDIATION_HEARTBEAT_S,
    REMEDIATION_LEASE_S,
    REMEDIATION_MAX_CONTINUOUS_LEASE_S,
    REVIEW_RETENTION_DAYS,
    SCHEMA_VERSION,
    AuditEvent,
    BatchLease,
    BatchOutcome,
    BatchStatus,
    CancelBatchRequest,
    ClaimBatchResponse,
    CompleteBatchRequest,
    CorrelationStatus,
    CorrelationTrust,
    CreateEvidenceLinkRequest,
    CreateRemediationBatchRequest,
    CreateReviewRequest,
    CursorError,
    CursorPage,
    DeleteEvidenceLinkRequest,
    DevRevActor,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketFilters,
    DevRevTicketSummary,
    DevRevTimelineEntry,
    ErrorBody,
    ErrorResponse,
    EvidenceLink,
    ExtendLeaseRequest,
    HeartbeatBatchRequest,
    ImportApplyOrReverseRequest,
    ImportChunkResponse,
    ImportDryRunResponse,
    ImportState,
    ImportStatus,
    InvalidBatchTransition,
    InvalidImportTransition,
    InvalidReviewTransition,
    MalformedPreconditionError,
    MaterializeBatchRequest,
    MaterializeBatchResponse,
    MissingPreconditionError,
    ObservationType,
    ObservedChunkRef,
    PatchBatchRequest,
    RagProvenance,
    ReadyBatchRequest,
    ReleaseBatchRequest,
    RemediationBatch,
    RemediationBatchItem,
    RemediationTarget,
    ResolutionOutcome,
    ReviewerIdentity,
    ReviewerRole,
    ReviewPatch,
    ReviewRef,
    ReviewResolution,
    ReviewStatus,
    SessionResponse,
    Severity,
    StalePreconditionError,
    StartVerificationRequest,
    TicketImport,
    TicketImportRow,
    TicketReview,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
    VerificationEvidence,
    allowed_batch_transitions,
    allowed_import_transitions,
    allowed_review_transitions,
    assert_batch_transition,
    assert_import_transition,
    assert_review_transition,
    audit_event_hash_payload,
    can_assign_reviewer,
    compute_audit_event_hash,
    ensure_if_match,
    format_etag,
    http_status_for_precondition_error,
    open_cursor,
    parse_if_match,
    review_id_for_devrev_work,
    seal_cursor,
    utc_now,
)
from api.tickets_console_config import (
    BROKER_FORBIDDEN_ENV_VARS,
    CONSOLE_FORBIDDEN_ENV_VARS,
    DEFAULT_FIRESTORE_DATABASE,
    DEVREV_OFFICIAL_API_BASE,
    EXPECTED_FIRESTORE_DATABASES,
    FAIL_CLOSED_ENVIRONMENT,
    LOCAL_FIRESTORE_DATABASE,
    PRODUCER_FORBIDDEN_ENV_VARS,
    PRODUCTION_FIRESTORE_DATABASE,
    STAGING_FIRESTORE_DATABASE,
    STRICT_ENVIRONMENTS,
    VALID_ENVIRONMENTS,
    EvidenceBrokerSettings,
    ProducerCorrelationSettings,
    TicketConsoleSettings,
    decode_cursor_aead_key,
    validate_evidence_broker_settings,
    resolve_tickets_firestore_database,
    validate_producer_correlation_settings,
    validate_ticket_console_settings,
)

FIXTURES = Path(__file__).parent / "fixtures" / "devrev"

# A deterministic, obviously-synthetic 32-byte AEAD key for tests only.
TEST_AEAD_KEY_B64 = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
SYNTHETIC_DON = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234"
SYNTHETIC_PART = "don:core:dvrv-us-1:devo/SYNTHETIC00:product/1"
PROD_BROKER_URL = "https://tickets-evidence-broker-000000.us-central1.run.app"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _identity(email: str = "reviewer@example.invalid") -> ReviewerIdentity:
    # The IAP subject, not the email, is the identity key, so each synthetic
    # person must get a distinct subject or the assignment rules are untested.
    return ReviewerIdentity(
        subject=f"accounts.google.com:synthetic-{email.split('@')[0]}",
        email=email,
        display_name="Synthetic Reviewer",
    )


def _review(**overrides) -> TicketReview:
    values = {
        "review_id": review_id_for_devrev_work(SYNTHETIC_DON),
        "devrev_work_id": SYNTHETIC_DON,
        "devrev_display_id": "TKT-1234",
        "topic": "distributions",
        "comments": "reviewer observation",
    }
    values.update(overrides)
    return TicketReview(**values)


def _console_settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    """Build console settings with ZERO ambient environment leakage.

    Every TICKETS_* variable present in the real process environment is removed
    and the .env file is disabled, so an assertion can never pass or fail
    because of a developer's shell.
    """
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values = {
        "ENVIRONMENT": "local",
        "AUTH_MODE": "local",
        "ALLOW_LOCAL_AUTH": True,
        "IAP_AUDIENCE": "local-fixture-audience",
        "ALLOWED_EMAIL_DOMAINS": ["example.invalid"],
        "GCP_PROJECT": "emulator-project",
        "FIRESTORE_DATABASE": "tickets-console-emulator",
        "DEVREV_ALLOWED_PART_DONS": [SYNTHETIC_PART],
        "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS": [2],
        "DEVREV_ALLOWED_TIMELINE_VISIBILITIES": ["internal", "external"],
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


def _production_settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    values = {
        "ENVIRONMENT": "production",
        "AUTH_MODE": "iap",
        "ALLOW_LOCAL_AUTH": False,
        "ENABLE_SYNTHETIC_VERIFICATION": False,
        "ALLOW_UNBOUND_VIEWERS": False,
        "IAP_AUDIENCE": "/projects/1/global/backendServices/2",
        "ALLOWED_EMAIL_DOMAINS": ["example.invalid"],
        "ROLE_BINDINGS_JSON": '{"admin@example.invalid": "admin"}',
        "CSRF_SIGNING_SECRET": "synthetic-csrf-value",
        "CURSOR_AEAD_KEY": TEST_AEAD_KEY_B64,
        "GCP_PROJECT": "rag-kb-system",
        "GCP_REGION": "us-central1",
        "FIRESTORE_DATABASE": "tickets-console-prod",
        # A production broker must be the Cloud Run host in the configured
        # project/region, not an arbitrary origin.
        "EVIDENCE_BROKER_URL": PROD_BROKER_URL,
        "EVIDENCE_BROKER_AUDIENCE": PROD_BROKER_URL,
        "DEVREV_TOKEN": "synthetic-devrev-value",
        "DEVREV_API_BASE": DEVREV_OFFICIAL_API_BASE,
    }
    values.update(overrides)
    return _console_settings(monkeypatch, **values)


# =====================================================================
# 1. Deterministic review IDs
# =====================================================================


class TestReviewId:

    def test_hash_is_stable_lowercase_sha256_hex(self):
        first = review_id_for_devrev_work(SYNTHETIC_DON)
        second = review_id_for_devrev_work(SYNTHETIC_DON)
        assert first == second
        assert len(first) == 64
        assert first == first.lower()
        assert all(c in "0123456789abcdef" for c in first)

    def test_review_id_never_contains_a_path_separator(self):
        # A DON contains ':' and '/', which cannot be a Firestore document ID.
        assert "/" in SYNTHETIC_DON
        assert "/" not in review_id_for_devrev_work(SYNTHETIC_DON)

    def test_surrounding_whitespace_is_normalized(self):
        assert review_id_for_devrev_work(f"  {SYNTHETIC_DON}\n") == review_id_for_devrev_work(
            SYNTHETIC_DON
        )

    def test_distinct_dons_do_not_collide(self):
        other = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1235"
        assert review_id_for_devrev_work(other) != review_id_for_devrev_work(SYNTHETIC_DON)

    @pytest.mark.parametrize("value", ("", "   ", "\t\n"))
    def test_empty_work_id_is_rejected(self, value):
        with pytest.raises(ValueError, match="devrev_work_id"):
            review_id_for_devrev_work(value)


# =====================================================================
# 2. Rating
# =====================================================================


class TestRating:

    @pytest.mark.parametrize("rating", (1, 2, 3, 4, 5, None))
    def test_accepts_one_through_five_and_none(self, rating):
        assert _review(rating=rating).rating == rating

    @pytest.mark.parametrize("rating", (0, 6, -1, 100))
    def test_rejects_out_of_range_integers(self, rating):
        with pytest.raises(ValueError):
            _review(rating=rating)

    @pytest.mark.parametrize("rating", (True, False))
    def test_rejects_booleans(self, rating):
        # bool is a subclass of int; a checkbox must never become a rating.
        with pytest.raises(ValueError):
            _review(rating=rating)

    @pytest.mark.parametrize("rating", ("3", "three", 3.0, 3.5, [3]))
    def test_rejects_strings_and_floats(self, rating):
        with pytest.raises(ValueError):
            _review(rating=rating)


# =====================================================================
# 3. Closed enums
# =====================================================================


class TestClosedEnums:

    def test_review_status_values(self):
        assert [s.value for s in ReviewStatus] == [
            "unreviewed",
            "reviewed",
            "triaged",
            "planned",
            "in_progress",
            "changes_proposed",
            "verifying",
            "resolved",
            "blocked",
            "wont_fix",
        ]

    def test_observation_type_values(self):
        assert [o.value for o in ObservationType] == [
            "correct",
            "knowledge_gap",
            "knowledge_conflict",
            "retrieval_miss",
            "chunking_or_metadata",
            "prompt_instruction",
            "orchestration_logic",
            "source_data",
            "privacy_or_compliance",
            "other",
        ]

    def test_severity_values(self):
        assert [s.value for s in Severity] == ["low", "medium", "high", "critical"]

    def test_remediation_target_values(self):
        assert [t.value for t in RemediationTarget] == [
            "kb",
            "prompt",
            "code",
            "workflow",
            "source_data",
            "none",
            "unknown",
        ]

    def test_correlation_enums(self):
        assert [c.value for c in CorrelationStatus] == ["linked", "manual", "unavailable"]
        assert [c.value for c in CorrelationTrust] == [
            "none",
            "candidate",
            "verified_workload",
            "manual_reviewer",
        ]

    def test_import_state_values(self):
        assert [i.value for i in ImportState] == ["active", "reversed"]

    def test_reviewer_role_values(self):
        assert [r.value for r in ReviewerRole] == [
            "viewer",
            "reviewer",
            "remediator",
            "admin",
            "agent",
        ]

    def test_batch_status_values(self):
        assert [b.value for b in BatchStatus] == [
            "draft",
            "ready",
            "claimed",
            "planning",
            "in_progress",
            "changes_proposed",
            "verifying",
            "completed",
            "blocked",
            "cancelled",
            "expired",
        ]

    def test_import_status_values(self):
        assert [i.value for i in ImportStatus] == [
            "uploaded",
            "planned",
            "approved",
            "applying",
            "applied",
            "partial",
            "reversing",
            "reversed",
            "failed",
            "cancelled",
        ]

    def test_resolution_outcome_values(self):
        assert [o.value for o in ResolutionOutcome] == [
            "fixed",
            "no_change",
            "duplicate",
            "accepted_risk",
        ]

    @pytest.mark.parametrize(
        "field,value",
        (
            ("status", "almost_done"),
            ("observation_type", "vibes"),
            ("severity", "apocalyptic"),
            ("remediation_target", "everything"),
            ("correlation_status", "probably"),
            ("correlation_trust", "trust_me"),
            ("import_state", "half"),
        ),
    )
    def test_unknown_enum_strings_fail(self, field, value):
        with pytest.raises(ValueError):
            _review(**{field: value})


# =====================================================================
# 4. Reviewer identity vs legacy reviewer vs audit actor
# =====================================================================


class TestReviewerIdentity:

    def test_email_is_normalized_to_lowercase_and_trimmed(self):
        who = ReviewerIdentity(
            subject="accounts.google.com:synthetic-000",
            email="  Reviewer@Example.Invalid  ",
        )
        assert who.email == "reviewer@example.invalid"

    @pytest.mark.parametrize("subject", ("", "   "))
    def test_empty_iap_subject_is_rejected(self, subject):
        with pytest.raises(ValueError):
            ReviewerIdentity(subject=subject, email="reviewer@example.invalid")

    @pytest.mark.parametrize("email", ("", "not-an-email", "a@b", "@example.invalid"))
    def test_malformed_email_is_rejected(self, email):
        with pytest.raises(ValueError):
            ReviewerIdentity(subject="accounts.google.com:synthetic-000", email=email)

    def test_assigned_reviewer_legacy_name_and_audit_actor_are_separate_fields(self):
        review = _review(
            assigned_reviewer=_identity(),
            legacy_reviewer_display_name="Jamie From The Sheet",
        )
        assert review.assigned_reviewer is not None
        assert review.assigned_reviewer.email == "reviewer@example.invalid"
        assert review.legacy_reviewer_display_name == "Jamie From The Sheet"
        # The durable review carries no audit actor at all; the actor lives on
        # the append-only audit event.
        assert "actor_subject" not in TicketReview.model_fields
        assert "actor_email" not in TicketReview.model_fields
        assert "actor_subject" in AuditEvent.model_fields

    def test_legacy_reviewer_text_is_bounded(self):
        with pytest.raises(ValueError):
            _review(legacy_reviewer_display_name="x" * 1000)


class TestAssignmentRules:

    def test_reviewer_may_self_assign(self):
        who = _identity()
        assert can_assign_reviewer(
            actor_role=ReviewerRole.REVIEWER, actor=who, target=who, current_assignee=None
        )

    def test_reviewer_may_not_assign_someone_else(self):
        actor = _identity("a@example.invalid")
        other = _identity("b@example.invalid")
        assert not can_assign_reviewer(
            actor_role=ReviewerRole.REVIEWER, actor=actor, target=other, current_assignee=None
        )

    def test_reviewer_may_not_steal_an_existing_assignment(self):
        actor = _identity("a@example.invalid")
        held = _identity("b@example.invalid")
        assert not can_assign_reviewer(
            actor_role=ReviewerRole.REVIEWER, actor=actor, target=actor, current_assignee=held
        )

    def test_admin_may_reassign_anyone(self):
        actor = _identity("admin@example.invalid")
        target = _identity("b@example.invalid")
        held = _identity("c@example.invalid")
        assert can_assign_reviewer(
            actor_role=ReviewerRole.ADMIN, actor=actor, target=target, current_assignee=held
        )

    @pytest.mark.parametrize("role", (ReviewerRole.VIEWER, ReviewerRole.AGENT))
    def test_viewer_and_agent_may_never_assign(self, role):
        who = _identity()
        assert not can_assign_reviewer(
            actor_role=role, actor=who, target=who, current_assignee=None
        )


# =====================================================================
# 5. Strictness and versioning
# =====================================================================


class TestTicketReviewStrictness:

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValueError):
            _review(sneaky_field="nope")

    def test_version_starts_at_one(self):
        assert _review().version == 1

    def test_schema_version_is_pinned(self):
        assert _review().schema_version == SCHEMA_VERSION == "1.0"

    def test_defaults_are_fail_closed(self):
        review = _review()
        assert review.status is ReviewStatus.UNREVIEWED
        assert review.correlation_status is CorrelationStatus.UNAVAILABLE
        assert review.correlation_trust is CorrelationTrust.NONE
        assert review.import_state is ImportState.ACTIVE
        assert review.legal_hold is False
        assert review.resolution is None
        assert review.rating is None

    def test_timestamps_are_timezone_aware_utc(self):
        review = _review()
        assert review.created_at.tzinfo is not None
        assert review.created_at.utcoffset() == timedelta(0)
        assert utc_now().tzinfo is timezone.utc

    def test_naive_datetimes_are_rejected(self):
        with pytest.raises(ValueError):
            _review(created_at=datetime(2026, 1, 1))  # noqa: DTZ001 - intentionally naive


# =====================================================================
# 6. Remote string bounds
# =====================================================================


class TestStringBounds:

    def test_comments_at_limit_pass_and_over_limit_fail(self):
        assert len(_review(comments="c" * MAX_COMMENTS_LENGTH).comments or "") == (
            MAX_COMMENTS_LENGTH
        )
        with pytest.raises(ValueError):
            _review(comments="c" * (MAX_COMMENTS_LENGTH + 1))

    def test_expected_behavior_is_bounded(self):
        with pytest.raises(ValueError):
            _review(expected_behavior="e" * (MAX_EXPECTED_BEHAVIOR_LENGTH + 1))

    def test_topic_and_legacy_type_are_bounded(self):
        with pytest.raises(ValueError):
            _review(topic="t" * (MAX_TOPIC_LENGTH + 1))
        with pytest.raises(ValueError):
            _review(legacy_type="t" * (MAX_LEGACY_TYPE_LENGTH + 1))

    def test_identifiers_are_bounded(self):
        with pytest.raises(ValueError):
            _review(devrev_work_id="d" * (MAX_ID_LENGTH + 1))
        with pytest.raises(ValueError):
            _review(devrev_display_id="T" * (MAX_DISPLAY_ID_LENGTH + 1))

    def test_live_title_is_bounded(self):
        with pytest.raises(ValueError):
            DevRevTicketSummary(
                devrev_work_id=SYNTHETIC_DON,
                devrev_display_id="TKT-1234",
                title="t" * (MAX_TITLE_LENGTH + 1),
            )

    def test_url_must_be_https_and_bounded(self):
        with pytest.raises(ValueError):
            EvidenceLink(
                link_id="link-1",
                review_id=review_id_for_devrev_work(SYNTHETIC_DON),
                evidence_reference="ref-1",
                evidence_digest="a" * 64,
                reason="manual link",
                linked_by=_identity(),
                source_url="http://insecure.example.invalid",
            )
        with pytest.raises(ValueError):
            EvidenceLink(
                link_id="link-1",
                review_id=review_id_for_devrev_work(SYNTHETIC_DON),
                evidence_reference="ref-1",
                evidence_digest="a" * 64,
                reason="manual link",
                linked_by=_identity(),
                source_url="https://example.invalid/" + "p" * MAX_URL_LENGTH,
            )

    def test_message_body_is_bounded(self):
        with pytest.raises(ValueError):
            DevRevTimelineEntry(
                entry_id="entry-1",
                object_id=SYNTHETIC_DON,
                kind=TimelineEntryKind.COMMENT,
                visibility=TimelineVisibility.EXTERNAL,
                body="b" * (MAX_MESSAGE_BODY_LENGTH + 1),
                created_at=utc_now(),
            )

    def test_attachment_metadata_count_is_bounded(self):
        with pytest.raises(ValueError):
            DevRevTicketDetail(
                devrev_work_id=SYNTHETIC_DON,
                devrev_display_id="TKT-1234",
                title="synthetic",
                attachments=[f"artifact-{i}" for i in range(MAX_ATTACHMENTS + 1)],
            )

    def test_evidence_refs_per_review_is_bounded(self):
        with pytest.raises(ValueError):
            _review(source_article_ids=[f"a-{i}" for i in range(MAX_EVIDENCE_REFS_PER_REVIEW + 1)])


# =====================================================================
# 7. ReviewPatch mutability
# =====================================================================


class TestReviewPatch:

    def test_has_mutable_fields(self):
        mutable = set(ReviewPatch.model_fields)
        assert mutable
        assert {
            "topic",
            "legacy_type",
            "observation_type",
            "rating",
            "comments",
            "expected_behavior",
            "severity",
            "status",
            "remediation_target",
            "assigned_reviewer",
        } <= mutable

    def test_all_patch_fields_are_optional(self):
        assert ReviewPatch() is not None

    @pytest.mark.parametrize(
        "field",
        (
            "review_id",
            "devrev_work_id",
            "devrev_display_id",
            "version",
            "created_at",
            "updated_at",
            "schema_version",
            "resolved_at",
            "legal_hold",
            "retention_expires_at",
        ),
    )
    def test_immutable_identifiers_and_versions_are_rejected_in_the_body(self, field):
        # Assert the field is genuinely UNDECLARED, not merely mistyped: passing
        # a string to an int field would also raise, which would let this test
        # pass even if the field were part of the mutable surface.
        assert field not in ReviewPatch.model_fields
        with pytest.raises(ValueError, match="extra"):
            ReviewPatch(**{field: "anything"})

    def test_patch_respects_the_same_bounds(self):
        with pytest.raises(ValueError):
            ReviewPatch(comments="c" * (MAX_COMMENTS_LENGTH + 1))


# =====================================================================
# 8. JSON round trips
# =====================================================================


def _summary() -> DevRevTicketSummary:
    return DevRevTicketSummary(
        devrev_work_id=SYNTHETIC_DON,
        devrev_display_id="TKT-1234",
        title="Rollover question",
        stage="triage",
        state="open",
        severity="medium",
        applies_to_part=SYNTHETIC_PART,
        ticket_visibility=2,
        source_channel="email",
        object_version=4,
        created_at=utc_now(),
        modified_at=utc_now(),
    )


def _timeline_entry() -> DevRevTimelineEntry:
    return DevRevTimelineEntry(
        entry_id="don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234:timeline_event/1",
        object_id=SYNTHETIC_DON,
        kind=TimelineEntryKind.COMMENT,
        visibility=TimelineVisibility.EXTERNAL,
        body="Synthetic participant question.",
        body_type="text",
        author=DevRevActor(
            actor_id="don:identity:dvrv-us-1:devo/SYNTHETIC00:revu/21",
            actor_type=DevRevActorType.REV_USER,
            display_name="Sam Participant",
        ),
        created_at=utc_now(),
    )


def _verification() -> VerificationEvidence:
    return VerificationEvidence(
        command_label="pytest tests/test_ticket_review_models.py",
        exit_code=0,
        passed=42,
        failed=0,
        skipped=1,
        output_sha256="b" * 64,
        runtime_s=12.5,
        occurred_at=utc_now(),
    )


def _resolution(**overrides) -> ReviewResolution:
    values = {
        "outcome": ResolutionOutcome.FIXED,
        "batch_id": "batch-0001",
        "plan_artifact": "plan-0001",
        "branch": "codex/tickets-remediation-0001",
        "commit_sha": "0" * 40,
        "test_evidence": [_verification()],
        "verification_summary": "Suite green on the remediation branch.",
        "verified_by": _identity("verifier@example.invalid"),
        "verified_at": utc_now(),
    }
    values.update(overrides)
    return ReviewResolution(**values)


def _audit_event(**overrides) -> AuditEvent:
    values = {
        "event_id": "event-0001",
        "parent_kind": "review",
        "parent_id": review_id_for_devrev_work(SYNTHETIC_DON),
        "event_type": "review_updated",
        "actor_subject": "accounts.google.com:synthetic-000",
        "actor_email": "reviewer@example.invalid",
        "actor_subject_hash": "c" * 64,
        "request_id_hash": "d" * 64,
        "idempotency_key_hash": "e" * 64,
        "occurred_at_unix_us": 1_700_000_000_000_000,
        "previous_version": 2,
        "new_version": 3,
        "changed_fields": ["rating", "comments"],
        "previous_event_hash": GENESIS_EVENT_HASH,
        "event_hash": "f" * 64,
    }
    values.update(overrides)
    return AuditEvent(**values)


def _batch_item() -> RemediationBatchItem:
    return RemediationBatchItem(
        review_id=review_id_for_devrev_work(SYNTHETIC_DON),
        review_version=3,
        devrev_display_id="TKT-1234",
        observation_type=ObservationType.KNOWLEDGE_GAP,
        severity=Severity.HIGH,
        remediation_target=RemediationTarget.KB,
    )


def _batch(**overrides) -> RemediationBatch:
    values = {
        "batch_id": "batch-0001",
        "created_by": _identity("remediator@example.invalid"),
        "item_count": 1,
        "item_set_digest": "a" * 64,
    }
    values.update(overrides)
    return RemediationBatch(**values)


def _ticket_import(**overrides) -> TicketImport:
    values = {
        "import_id": "import-0001",
        "file_sha256": "a" * 64,
        "created_by": _identity("admin@example.invalid"),
        "total_rows": 3,
    }
    values.update(overrides)
    return TicketImport(**values)


class TestJsonRoundTrips:

    @pytest.mark.parametrize(
        "factory",
        (
            _summary,
            _timeline_entry,
            _verification,
            _resolution,
            _audit_event,
            _batch_item,
            _batch,
            _ticket_import,
            _review,
        ),
        ids=(
            "DevRevTicketSummary",
            "DevRevTimelineEntry",
            "VerificationEvidence",
            "ReviewResolution",
            "AuditEvent",
            "RemediationBatchItem",
            "RemediationBatch",
            "TicketImport",
            "TicketReview",
        ),
    )
    def test_model_round_trips_through_json_mode(self, factory):
        original = factory()
        payload = original.model_dump(mode="json")
        # Must be genuinely JSON-serializable, not merely dict-shaped.
        encoded = json.dumps(payload)
        restored = type(original).model_validate(json.loads(encoded))
        assert restored == original

    def test_evidence_link_round_trips(self):
        link = EvidenceLink(
            link_id="link-1",
            review_id=review_id_for_devrev_work(SYNTHETIC_DON),
            evidence_reference="exec-ref-1",
            evidence_digest="a" * 64,
            reason="Reviewer confirmed the retrieval trace.",
            linked_by=_identity(),
            correlation_trust=CorrelationTrust.MANUAL_REVIEWER,
        )
        restored = EvidenceLink.model_validate(json.loads(json.dumps(link.model_dump(mode="json"))))
        assert restored == link

    def test_forward_and_backward_cursor_envelopes_round_trip(self):
        page: CursorPage[DevRevTicketSummary] = CursorPage[DevRevTicketSummary](
            items=[_summary()],
            next_cursor="opaque-forward",
            prev_cursor="opaque-backward",
            page_size=DEFAULT_PAGE_SIZE,
            partial=False,
            truncated=False,
        )
        encoded = json.dumps(page.model_dump(mode="json"))
        restored = CursorPage[DevRevTicketSummary].model_validate(json.loads(encoded))
        assert restored == page
        assert restored.next_cursor == "opaque-forward"
        assert restored.prev_cursor == "opaque-backward"

    def test_timeline_page_carries_partial_and_truncated_markers(self):
        page = TimelinePage(
            items=[_timeline_entry()],
            next_cursor=None,
            page_size=DEFAULT_PAGE_SIZE,
            partial=True,
            truncated=True,
            warnings=["max_pages_guard_reached"],
        )
        restored = TimelinePage.model_validate(json.loads(json.dumps(page.model_dump(mode="json"))))
        assert restored.partial is True
        assert restored.truncated is True
        assert restored.warnings == ["max_pages_guard_reached"]

    def test_cursor_length_is_bounded(self):
        with pytest.raises(ValueError):
            CursorPage[DevRevTicketSummary](
                items=[], next_cursor="c" * (MAX_CURSOR_LENGTH + 1), page_size=DEFAULT_PAGE_SIZE
            )

    def test_page_size_is_bounded_by_the_canonical_maximum(self):
        with pytest.raises(ValueError):
            CursorPage[DevRevTicketSummary](items=[], page_size=MAX_PAGE_SIZE + 1)


# =====================================================================
# 9 + 10. Console settings defaults and production rejections
# =====================================================================


class TestConsoleSettingsDefaults:

    def test_defaults_are_disabled_and_fail_closed(self, monkeypatch):
        """Defaults are safe for tests, but never *permissive*.

        ``ENVIRONMENT`` is the switch that enables every hardening rule, so its
        default is the strictest value. A revision whose Terraform forgot
        ``TICKETS_ENVIRONMENT`` must refuse to start rather than skip the checks.
        """
        for name in list(os.environ):
            if name.startswith("TICKETS_"):
                monkeypatch.delenv(name, raising=False)
        cfg = TicketConsoleSettings(_env_file=None)
        assert cfg.ENABLED is False
        assert cfg.ENVIRONMENT == FAIL_CLOSED_ENVIRONMENT == "production"
        assert cfg.AUTH_MODE == "iap"
        assert cfg.ALLOW_LOCAL_AUTH is False
        assert cfg.ENABLE_SYNTHETIC_VERIFICATION is False
        assert cfg.ALLOW_UNBOUND_VIEWERS is False
        assert cfg.DEFAULT_ROLE is None
        assert cfg.RETENTION_JOB_ENABLED is False
        assert cfg.ALLOW_NON_OFFICIAL_DEVREV_BASE is False

    def test_a_misprovisioned_revision_fails_closed(self, monkeypatch):
        """The regression this guards: no TICKETS_ENVIRONMENT must not mean
        'skip every production check'."""
        for name in list(os.environ):
            if name.startswith("TICKETS_"):
                monkeypatch.delenv(name, raising=False)
        cfg = TicketConsoleSettings(_env_file=None)
        with pytest.raises(ValueError) as caught:
            validate_ticket_console_settings(cfg, env={})
        message = str(caught.value)
        for expected in ("IAP_AUDIENCE", "DEVREV_TOKEN", "CSRF_SIGNING_SECRET",
                         "CURSOR_AEAD_KEY", "FIRESTORE_DATABASE", "ROLE_BINDINGS_JSON"):
            assert expected in message

    def test_only_the_three_planned_environments_are_valid(self, monkeypatch):
        assert VALID_ENVIRONMENTS == frozenset({"local", "staging", "production"})
        cfg = _console_settings(monkeypatch, ENVIRONMENT="development")
        with pytest.raises(ValueError, match="ENVIRONMENT"):
            validate_ticket_console_settings(cfg, env={})

    def test_role_bindings_never_leak_through_repr_or_dump(self, monkeypatch):
        cfg = _console_settings(
            monkeypatch, ROLE_BINDINGS_JSON='{"admin@example.invalid": "admin"}'
        )
        assert "admin@example.invalid" not in repr(cfg)
        assert "admin@example.invalid" not in str(cfg.model_dump())

    def test_no_secret_has_a_usable_default(self, monkeypatch):
        for name in list(os.environ):
            if name.startswith("TICKETS_"):
                monkeypatch.delenv(name, raising=False)
        cfg = TicketConsoleSettings(_env_file=None)
        assert cfg.DEVREV_TOKEN.get_secret_value() == ""
        assert cfg.CSRF_SIGNING_SECRET.get_secret_value() == ""
        assert cfg.CURSOR_AEAD_KEY.get_secret_value() == ""

    def test_secrets_are_masked_in_repr(self, monkeypatch):
        cfg = _console_settings(monkeypatch, DEVREV_TOKEN="synthetic-devrev-value")
        assert "synthetic-devrev-value" not in repr(cfg)
        assert "synthetic-devrev-value" not in str(cfg.DEVREV_TOKEN)

    def test_devrev_version_default_is_pinned(self, monkeypatch):
        assert _console_settings(monkeypatch).DEVREV_VERSION == "2022-10-20"

    def test_local_defaults_validate_cleanly(self, monkeypatch):
        cfg = _console_settings(monkeypatch)
        assert validate_ticket_console_settings(cfg, env={}) is True

    def test_url_template_is_optional(self, monkeypatch):
        cfg = _console_settings(monkeypatch)
        assert cfg.DEVREV_TICKET_URL_TEMPLATE is None

    def test_settings_module_does_not_import_the_rag_singleton(self):
        import api.tickets_console_config as module

        source = Path(module.__file__).read_text()
        assert "from api.config import" not in source
        assert "import api.config" not in source


class TestProductionSettingsRejections:

    def test_a_correct_production_configuration_passes(self, monkeypatch):
        cfg = _production_settings(monkeypatch)
        assert validate_ticket_console_settings(cfg, env={}) is True

    def test_missing_devrev_token_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, DEVREV_TOKEN="")
        with pytest.raises(ValueError, match="DEVREV_TOKEN"):
            validate_ticket_console_settings(cfg, env={})

    def test_local_auth_mode_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, AUTH_MODE="local")
        with pytest.raises(ValueError, match="AUTH_MODE"):
            validate_ticket_console_settings(cfg, env={})

    def test_allow_local_auth_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, ALLOW_LOCAL_AUTH=True)
        with pytest.raises(ValueError, match="ALLOW_LOCAL_AUTH"):
            validate_ticket_console_settings(cfg, env={})

    def test_synthetic_verification_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, ENABLE_SYNTHETIC_VERIFICATION=True)
        with pytest.raises(ValueError, match="ENABLE_SYNTHETIC_VERIFICATION"):
            validate_ticket_console_settings(cfg, env={})

    @pytest.mark.parametrize("domains", ([], ["*"], ["example.invalid", "*"], [""]))
    def test_wildcard_or_empty_email_domains_are_rejected(self, monkeypatch, domains):
        cfg = _production_settings(monkeypatch, ALLOWED_EMAIL_DOMAINS=domains)
        with pytest.raises(ValueError, match="ALLOWED_EMAIL_DOMAINS"):
            validate_ticket_console_settings(cfg, env={})

    def test_missing_iap_audience_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, IAP_AUDIENCE="")
        with pytest.raises(ValueError, match="IAP_AUDIENCE"):
            validate_ticket_console_settings(cfg, env={})

    def test_non_official_devrev_base_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, DEVREV_API_BASE="https://evil.example.invalid")
        with pytest.raises(ValueError, match="DEVREV_API_BASE"):
            validate_ticket_console_settings(cfg, env={})

    def test_non_official_devrev_base_needs_an_explicit_override_outside_production(
        self, monkeypatch
    ):
        staging_base = {
            "ENVIRONMENT": "staging",
            "FIRESTORE_DATABASE": "tickets-console-staging",
            "DEVREV_API_BASE": "https://devrev.mock.example.invalid",
        }
        staging = _production_settings(monkeypatch, **staging_base)
        with pytest.raises(ValueError, match="DEVREV_API_BASE"):
            validate_ticket_console_settings(staging, env={})

        allowed = _production_settings(
            monkeypatch, **staging_base, ALLOW_NON_OFFICIAL_DEVREV_BASE=True
        )
        assert validate_ticket_console_settings(allowed, env={}) is True

    def test_a_plain_http_fixture_server_is_local_only(self, monkeypatch):
        """The canonical matrix gives local a fixture server, which may be
        loopback http; staging and production may not be."""
        local = _console_settings(
            monkeypatch,
            DEVREV_API_BASE="http://127.0.0.1:8931",
            ALLOW_NON_OFFICIAL_DEVREV_BASE=True,
        )
        assert validate_ticket_console_settings(local, env={}) is True

        staging = _production_settings(
            monkeypatch,
            ENVIRONMENT="staging",
            FIRESTORE_DATABASE="tickets-console-staging",
            DEVREV_API_BASE="http://127.0.0.1:8931",
            ALLOW_NON_OFFICIAL_DEVREV_BASE=True,
        )
        with pytest.raises(ValueError, match="DEVREV_API_BASE"):
            validate_ticket_console_settings(staging, env={})

    def test_missing_gcp_region_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, GCP_REGION="")
        with pytest.raises(ValueError, match="GCP_REGION"):
            validate_ticket_console_settings(cfg, env={})

    @pytest.mark.parametrize(
        "url",
        (
            "https://evil.example.invalid",
            "https://tickets-evidence-broker-000000.europe-west1.run.app",
            "https://tickets-evidence-broker.us-central1.example.invalid",
        ),
        ids=("external-host", "wrong-region", "not-cloud-run"),
    )
    def test_a_broker_outside_the_configured_project_region_is_rejected(self, monkeypatch, url):
        cfg = _production_settings(monkeypatch, EVIDENCE_BROKER_URL=url)
        with pytest.raises(ValueError, match="EVIDENCE_BROKER_URL"):
            validate_ticket_console_settings(cfg, env={})

    @pytest.mark.parametrize(
        "domains",
        (["example.*"], ["*.example.invalid"], ["invalid"], ["exam*ple.invalid"]),
        ids=("suffix-wildcard", "prefix-wildcard", "bare-tld", "infix-wildcard"),
    )
    def test_disguised_wildcard_domains_are_rejected(self, monkeypatch, domains):
        cfg = _production_settings(monkeypatch, ALLOWED_EMAIL_DOMAINS=domains)
        with pytest.raises(ValueError, match="ALLOWED_EMAIL_DOMAINS"):
            validate_ticket_console_settings(cfg, env={})

    def test_role_bindings_must_be_a_non_empty_map(self, monkeypatch):
        for value in ("[]", "{}", '"admin"', "null"):
            cfg = _production_settings(monkeypatch, ROLE_BINDINGS_JSON=value)
            with pytest.raises(ValueError, match="ROLE_BINDINGS_JSON"):
                validate_ticket_console_settings(cfg, env={})

    @pytest.mark.parametrize(
        "override",
        (
            {"DEVREV_MAX_RESPONSE_BYTES": 1024 * 1024 * 1024},
            {"DEVREV_PAGE_SIZE": MAX_PAGE_SIZE + 1},
            {"DEVREV_MAX_PAGES": DEVREV_MAX_PAGES + 1},
            {"MAX_TIMELINE_ENTRIES": DEVREV_MAX_ENTRIES * 10},
            {"REMEDIATION_LEASE_S": 86_400},
            {"REMEDIATION_MAX_CONTINUOUS_LEASE_S": 86_400},
            {"AUDIT_RETENTION_DAYS": 1_000_000},
            {"REVIEW_RETENTION_DAYS": 1_000_000},
            {"CSRF_TOKEN_TTL_S": 86_400},
            {"MESSAGE_CACHE_TTL_S": 86_400 * 30},
            {"MAX_CSV_ROWS": 1_000_000},
            {"MAX_BATCH_REVIEWS": 10_000},
        ),
        ids=lambda o: next(iter(o)),
    )
    def test_a_revision_cannot_loosen_a_canonical_limit(self, monkeypatch, override):
        """Canonical values are the single source of truth; an environment
        variable must not be able to widen a security or retention bound."""
        cfg = _production_settings(monkeypatch, **override)
        with pytest.raises(ValueError, match=next(iter(override))):
            validate_ticket_console_settings(cfg, env={})

    @pytest.mark.parametrize("field", ("DEVREV_PAGE_SIZE", "REMEDIATION_LEASE_S", "CACHE_TTL_S"))
    def test_a_non_positive_limit_is_rejected(self, monkeypatch, field):
        cfg = _production_settings(monkeypatch, **{field: 0})
        with pytest.raises(ValueError, match=field):
            validate_ticket_console_settings(cfg, env={})

    def test_heartbeat_must_be_shorter_than_the_lease(self, monkeypatch):
        cfg = _production_settings(monkeypatch, REMEDIATION_HEARTBEAT_S=REMEDIATION_LEASE_S)
        with pytest.raises(ValueError, match="REMEDIATION_HEARTBEAT_S"):
            validate_ticket_console_settings(cfg, env={})

    def test_production_may_never_enable_the_non_official_base_override(self, monkeypatch):
        cfg = _production_settings(
            monkeypatch,
            DEVREV_API_BASE="https://devrev.mock.example.invalid",
            ALLOW_NON_OFFICIAL_DEVREV_BASE=True,
        )
        with pytest.raises(ValueError, match="ALLOW_NON_OFFICIAL_DEVREV_BASE"):
            validate_ticket_console_settings(cfg, env={})

    def test_default_firestore_database_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, FIRESTORE_DATABASE="(default)")
        with pytest.raises(ValueError, match="FIRESTORE_DATABASE"):
            validate_ticket_console_settings(cfg, env={})

    def test_unbound_viewers_are_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, ALLOW_UNBOUND_VIEWERS=True)
        with pytest.raises(ValueError, match="ALLOW_UNBOUND_VIEWERS"):
            validate_ticket_console_settings(cfg, env={})

    def test_missing_role_bindings_are_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, ROLE_BINDINGS_JSON="")
        with pytest.raises(ValueError, match="ROLE_BINDINGS_JSON"):
            validate_ticket_console_settings(cfg, env={})

    def test_a_default_role_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, DEFAULT_ROLE="viewer")
        with pytest.raises(ValueError, match="DEFAULT_ROLE"):
            validate_ticket_console_settings(cfg, env={})

    @pytest.mark.parametrize("field", ("EVIDENCE_BROKER_URL", "EVIDENCE_BROKER_AUDIENCE"))
    def test_missing_broker_configuration_is_rejected(self, monkeypatch, field):
        cfg = _production_settings(monkeypatch, **{field: ""})
        with pytest.raises(ValueError, match=field):
            validate_ticket_console_settings(cfg, env={})

    def test_non_https_broker_url_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, EVIDENCE_BROKER_URL="http://broker.example.invalid")
        with pytest.raises(ValueError, match="EVIDENCE_BROKER_URL"):
            validate_ticket_console_settings(cfg, env={})

    def test_missing_csrf_secret_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, CSRF_SIGNING_SECRET="")
        with pytest.raises(ValueError, match="CSRF_SIGNING_SECRET"):
            validate_ticket_console_settings(cfg, env={})

    def test_missing_or_malformed_cursor_key_is_rejected(self, monkeypatch):
        cfg = _production_settings(monkeypatch, CURSOR_AEAD_KEY="")
        with pytest.raises(ValueError, match="CURSOR_AEAD_KEY"):
            validate_ticket_console_settings(cfg, env={})
        short = _production_settings(monkeypatch, CURSOR_AEAD_KEY="c2hvcnQ=")
        with pytest.raises(ValueError, match="CURSOR_AEAD_KEY"):
            validate_ticket_console_settings(short, env={})

    @pytest.mark.parametrize(
        "field",
        (
            "DEVREV_ALLOWED_PART_DONS",
            "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS",
            "DEVREV_ALLOWED_TIMELINE_VISIBILITIES",
        ),
    )
    def test_empty_devrev_scope_allowlists_are_rejected(self, monkeypatch, field):
        cfg = _production_settings(monkeypatch, **{field: []})
        with pytest.raises(ValueError, match=field):
            validate_ticket_console_settings(cfg, env={})

    def test_staging_enforces_the_same_fail_closed_rules(self, monkeypatch):
        cfg = _production_settings(
            monkeypatch,
            ENVIRONMENT="staging",
            FIRESTORE_DATABASE="tickets-console-staging",
            AUTH_MODE="local",
        )
        with pytest.raises(ValueError, match="AUTH_MODE"):
            validate_ticket_console_settings(cfg, env={})

    def test_all_errors_are_reported_together(self, monkeypatch):
        cfg = _production_settings(
            monkeypatch, DEVREV_TOKEN="", IAP_AUDIENCE="", CSRF_SIGNING_SECRET=""
        )
        with pytest.raises(ValueError) as caught:
            validate_ticket_console_settings(cfg, env={})
        message = str(caught.value)
        assert "DEVREV_TOKEN" in message
        assert "IAP_AUDIENCE" in message
        assert "CSRF_SIGNING_SECRET" in message

    def test_validation_errors_never_leak_a_secret_value(self, monkeypatch):
        cfg = _production_settings(
            monkeypatch,
            CURSOR_AEAD_KEY="c2hvcnQ=",
            DEVREV_TOKEN="synthetic-devrev-value",
            FIRESTORE_DATABASE="(default)",
        )
        with pytest.raises(ValueError) as caught:
            validate_ticket_console_settings(cfg, env={})
        message = str(caught.value)
        assert "synthetic-devrev-value" not in message
        assert "c2hvcnQ=" not in message


# =====================================================================
# 11. Canonical limits table
# =====================================================================


class TestCanonicalLimits:
    """Literal mirror of the master plan's canonical limits table."""

    def test_pagination_and_iterator_guards(self):
        assert DEFAULT_PAGE_SIZE == 50
        assert MAX_PAGE_SIZE == 100
        assert DEVREV_MAX_PAGES == 100
        assert DEVREV_MAX_ENTRIES == 5_000

    def test_http_timeouts_and_retries(self):
        assert DEVREV_CONNECT_TIMEOUT_S == 5.0
        assert DEVREV_READ_TIMEOUT_S == 20.0
        assert DEVREV_MAX_RETRIES == 3
        assert DEVREV_RETRY_AFTER_CAP_S == 60

    def test_cache_and_retention_windows(self):
        assert CACHE_TTL_S == 15 * 60
        assert MESSAGE_CACHE_TTL_S == 24 * 60 * 60
        assert IDEMPOTENCY_TTL_S == 7 * 24 * 60 * 60
        assert IMPORT_STAGING_TTL_S == 7 * 24 * 60 * 60
        assert CSRF_TOKEN_TTL_S == 60 * 60
        assert REVIEW_RETENTION_DAYS == 730
        assert AUDIT_RETENTION_DAYS == 2_555

    def test_string_bounds(self):
        assert MAX_ID_LENGTH == 256
        assert MAX_DISPLAY_ID_LENGTH == 64
        assert MAX_CURSOR_LENGTH == 2_048
        assert MAX_TITLE_LENGTH == 512
        assert MAX_LEGACY_TYPE_LENGTH == 80
        assert MAX_TOPIC_LENGTH == 80
        assert MAX_COMMENTS_LENGTH == 10_000
        assert MAX_EXPECTED_BEHAVIOR_LENGTH == 10_000
        assert MAX_SUMMARY_LENGTH == 5_000
        assert MAX_MESSAGE_BODY_LENGTH == 50_000
        assert MAX_URL_LENGTH == 2_048

    def test_collection_and_request_bounds(self):
        assert MAX_ATTACHMENTS == 20
        assert MAX_EVIDENCE_REFS_PER_REVIEW == 200
        assert MAX_BATCH_REVIEWS == 100
        assert MAX_CSV_ROWS == 10_000
        assert MAX_JSON_REQUEST_BYTES == 1 * 1024 * 1024
        assert MAX_CSV_REQUEST_BYTES == 10 * 1024 * 1024
        assert MAX_UPSTREAM_ERROR_BODY_BYTES == 4 * 1024
        assert DEVREV_MAX_RESPONSE_BYTES == 4 * 1024 * 1024
        assert EVIDENCE_BROKER_MAX_RESPONSE_BYTES == 512 * 1024

    def test_lease_windows(self):
        assert REMEDIATION_LEASE_S == 15 * 60
        assert REMEDIATION_HEARTBEAT_S == 5 * 60
        assert REMEDIATION_MAX_CONTINUOUS_LEASE_S == 2 * 60 * 60
        assert MAX_LEASE_EXTENSION_MINUTES == 120

    def test_settings_defaults_equal_the_canonical_table(self, monkeypatch):
        cfg = _console_settings(monkeypatch)
        assert cfg.DEVREV_PAGE_SIZE == DEFAULT_PAGE_SIZE
        assert cfg.DEVREV_MAX_PAGES == DEVREV_MAX_PAGES
        assert cfg.DEVREV_MAX_RETRIES == DEVREV_MAX_RETRIES
        assert cfg.DEVREV_MAX_RESPONSE_BYTES == DEVREV_MAX_RESPONSE_BYTES
        assert cfg.DEVREV_TIMEOUT_S == DEVREV_READ_TIMEOUT_S
        assert cfg.DEVREV_CONNECT_TIMEOUT_S == DEVREV_CONNECT_TIMEOUT_S
        assert cfg.CACHE_TTL_S == CACHE_TTL_S
        assert cfg.MESSAGE_CACHE_TTL_S == MESSAGE_CACHE_TTL_S
        assert cfg.IDEMPOTENCY_TTL_S == IDEMPOTENCY_TTL_S
        assert cfg.IMPORT_STAGING_TTL_S == IMPORT_STAGING_TTL_S
        assert cfg.CSRF_TOKEN_TTL_S == CSRF_TOKEN_TTL_S
        assert cfg.REVIEW_RETENTION_DAYS == REVIEW_RETENTION_DAYS
        assert cfg.AUDIT_RETENTION_DAYS == AUDIT_RETENTION_DAYS
        assert cfg.MAX_TIMELINE_ENTRIES == DEVREV_MAX_ENTRIES
        assert cfg.MAX_CSV_BYTES == MAX_CSV_REQUEST_BYTES
        assert cfg.MAX_JSON_BYTES == MAX_JSON_REQUEST_BYTES
        assert cfg.MAX_CSV_ROWS == MAX_CSV_ROWS
        assert cfg.MAX_BATCH_REVIEWS == MAX_BATCH_REVIEWS
        assert cfg.REMEDIATION_LEASE_S == REMEDIATION_LEASE_S
        assert cfg.REMEDIATION_HEARTBEAT_S == REMEDIATION_HEARTBEAT_S
        assert cfg.REMEDIATION_MAX_CONTINUOUS_LEASE_S == REMEDIATION_MAX_CONTINUOUS_LEASE_S


# =====================================================================
# 12. legacy_type vs observation_type
# =====================================================================


class TestLegacyTypeSeparation:

    def test_both_fields_exist_and_serialize_independently(self):
        review = _review(
            legacy_type="Knowledge - Sheet Wording",
            observation_type=ObservationType.KNOWLEDGE_GAP,
        )
        payload = review.model_dump(mode="json")
        assert payload["legacy_type"] == "Knowledge - Sheet Wording"
        assert payload["observation_type"] == "knowledge_gap"
        assert payload["legacy_type"] != payload["observation_type"]

    def test_legacy_type_is_free_text_and_never_coerced_to_the_taxonomy(self):
        review = _review(legacy_type="anything the sheet contained")
        assert review.legacy_type == "anything the sheet contained"
        assert review.observation_type is None

    def test_observation_type_is_closed(self):
        with pytest.raises(ValueError):
            _review(observation_type="anything the sheet contained")


# =====================================================================
# 13. ETag / If-Match precondition semantics
# =====================================================================


class TestPreconditions:

    def test_format_etag_is_quoted(self):
        assert format_etag(1) == '"v1"'
        assert format_etag(42) == '"v42"'

    def test_parse_accepts_only_quoted_versions(self):
        assert parse_if_match('"v3"') == 3

    @pytest.mark.parametrize(
        "header",
        ("v3", "3", "'v3'", 'W/"v3"', '"3"', '"v"', '"v0"', '"v-1"', '""', '"v03"', '"v3 "', "*"),
    )
    def test_malformed_preconditions_are_422(self, header):
        with pytest.raises(MalformedPreconditionError):
            parse_if_match(header)
        assert http_status_for_precondition_error(MalformedPreconditionError("x")) == 422

    def test_missing_precondition_is_428(self):
        with pytest.raises(MissingPreconditionError):
            parse_if_match(None)
        assert http_status_for_precondition_error(MissingPreconditionError("x")) == 428

    def test_stale_precondition_is_412(self):
        assert ensure_if_match('"v3"', current_version=3) == 3
        with pytest.raises(StalePreconditionError):
            ensure_if_match('"v2"', current_version=3)
        assert http_status_for_precondition_error(StalePreconditionError("x")) == 412

    def test_missing_and_stale_are_distinguishable(self):
        with pytest.raises(MissingPreconditionError):
            ensure_if_match(None, current_version=3)
        with pytest.raises(StalePreconditionError):
            ensure_if_match('"v1"', current_version=3)

    def test_stale_error_exposes_only_safe_version_metadata(self):
        with pytest.raises(StalePreconditionError) as caught:
            ensure_if_match('"v1"', current_version=7)
        assert caught.value.current_version == 7
        assert caught.value.supplied_version == 1


# =====================================================================
# 14. State machines
# =====================================================================

REVIEW_TABLE = {
    ReviewStatus.UNREVIEWED: {ReviewStatus.REVIEWED, ReviewStatus.BLOCKED},
    ReviewStatus.REVIEWED: {ReviewStatus.TRIAGED, ReviewStatus.BLOCKED, ReviewStatus.WONT_FIX},
    ReviewStatus.TRIAGED: {ReviewStatus.PLANNED, ReviewStatus.BLOCKED, ReviewStatus.WONT_FIX},
    ReviewStatus.PLANNED: {ReviewStatus.IN_PROGRESS, ReviewStatus.BLOCKED, ReviewStatus.WONT_FIX},
    ReviewStatus.IN_PROGRESS: {ReviewStatus.CHANGES_PROPOSED, ReviewStatus.BLOCKED},
    ReviewStatus.CHANGES_PROPOSED: {
        ReviewStatus.VERIFYING,
        ReviewStatus.IN_PROGRESS,
        ReviewStatus.BLOCKED,
    },
    ReviewStatus.VERIFYING: {
        ReviewStatus.RESOLVED,
        ReviewStatus.IN_PROGRESS,
        ReviewStatus.BLOCKED,
    },
    ReviewStatus.BLOCKED: {
        ReviewStatus.TRIAGED,
        ReviewStatus.PLANNED,
        ReviewStatus.IN_PROGRESS,
        ReviewStatus.WONT_FIX,
    },
    ReviewStatus.RESOLVED: set(),
    ReviewStatus.WONT_FIX: set(),
}

BATCH_TABLE = {
    BatchStatus.DRAFT: {BatchStatus.READY, BatchStatus.CANCELLED},
    BatchStatus.READY: {BatchStatus.CLAIMED, BatchStatus.CANCELLED},
    BatchStatus.CLAIMED: {BatchStatus.PLANNING, BatchStatus.BLOCKED, BatchStatus.EXPIRED},
    BatchStatus.PLANNING: {BatchStatus.IN_PROGRESS, BatchStatus.BLOCKED, BatchStatus.EXPIRED},
    BatchStatus.IN_PROGRESS: {
        BatchStatus.CHANGES_PROPOSED,
        BatchStatus.BLOCKED,
        BatchStatus.EXPIRED,
    },
    BatchStatus.CHANGES_PROPOSED: {
        BatchStatus.VERIFYING,
        BatchStatus.IN_PROGRESS,
        BatchStatus.BLOCKED,
    },
    BatchStatus.VERIFYING: {BatchStatus.COMPLETED, BatchStatus.IN_PROGRESS, BatchStatus.BLOCKED},
    BatchStatus.BLOCKED: {BatchStatus.READY, BatchStatus.CLAIMED, BatchStatus.CANCELLED},
    BatchStatus.EXPIRED: {BatchStatus.CLAIMED, BatchStatus.CANCELLED},
    BatchStatus.COMPLETED: set(),
    BatchStatus.CANCELLED: set(),
}

IMPORT_TABLE = {
    ImportStatus.UPLOADED: {ImportStatus.PLANNED, ImportStatus.FAILED, ImportStatus.CANCELLED},
    ImportStatus.PLANNED: {ImportStatus.APPROVED, ImportStatus.FAILED, ImportStatus.CANCELLED},
    ImportStatus.APPROVED: {ImportStatus.APPLYING, ImportStatus.CANCELLED},
    ImportStatus.APPLYING: {ImportStatus.APPLIED, ImportStatus.PARTIAL, ImportStatus.FAILED},
    ImportStatus.PARTIAL: {
        ImportStatus.APPLYING,
        ImportStatus.REVERSING,
        ImportStatus.CANCELLED,
    },
    ImportStatus.APPLIED: {ImportStatus.REVERSING},
    ImportStatus.REVERSING: {ImportStatus.REVERSED, ImportStatus.PARTIAL, ImportStatus.FAILED},
    ImportStatus.FAILED: {
        ImportStatus.PLANNED,
        ImportStatus.APPLYING,
        ImportStatus.REVERSING,
        ImportStatus.CANCELLED,
    },
    ImportStatus.REVERSED: set(),
    ImportStatus.CANCELLED: set(),
}


class TestReviewTransitions:

    @pytest.mark.parametrize("current", list(ReviewStatus), ids=lambda s: s.value)
    def test_table_matches_the_master_plan_exactly(self, current):
        assert set(allowed_review_transitions(current)) == REVIEW_TABLE[current]

    def test_every_allowed_edge_is_accepted(self):
        terminal = {ReviewStatus.RESOLVED, ReviewStatus.WONT_FIX}
        for current, targets in REVIEW_TABLE.items():
            for target in targets:
                assert_review_transition(
                    current,
                    target,
                    actor_role=ReviewerRole.REVIEWER,
                    # A terminal target additionally requires a closed resolution;
                    # that rule is exercised on its own below.
                    resolution=_resolution() if target in terminal else None,
                )

    def test_every_disallowed_edge_is_rejected(self):
        for current, targets in REVIEW_TABLE.items():
            for target in ReviewStatus:
                if target in targets or target is current:
                    continue
                with pytest.raises(InvalidReviewTransition):
                    assert_review_transition(current, target, actor_role=ReviewerRole.ADMIN)

    def test_resolved_and_wont_fix_are_terminal(self):
        assert allowed_review_transitions(ReviewStatus.RESOLVED) == frozenset()
        assert allowed_review_transitions(ReviewStatus.WONT_FIX) == frozenset()

    @pytest.mark.parametrize("terminal", (ReviewStatus.RESOLVED, ReviewStatus.WONT_FIX))
    def test_a_reopen_must_be_explicit(self, terminal):
        """A terminal review never reopens as a side effect of a normal patch."""
        with pytest.raises(InvalidReviewTransition):
            assert_review_transition(terminal, ReviewStatus.TRIAGED, actor_role=ReviewerRole.ADMIN)

    @pytest.mark.parametrize("terminal", (ReviewStatus.RESOLVED, ReviewStatus.WONT_FIX))
    def test_only_an_admin_may_reopen_a_terminal_review_to_triaged(self, terminal):
        assert_review_transition(
            terminal, ReviewStatus.TRIAGED, actor_role=ReviewerRole.ADMIN, admin_reopen=True
        )
        for role in (ReviewerRole.VIEWER, ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR):
            with pytest.raises(InvalidReviewTransition):
                assert_review_transition(
                    terminal, ReviewStatus.TRIAGED, actor_role=role, admin_reopen=True
                )

    @pytest.mark.parametrize("terminal", (ReviewStatus.RESOLVED, ReviewStatus.WONT_FIX))
    def test_an_admin_reopen_may_only_target_triaged(self, terminal):
        with pytest.raises(InvalidReviewTransition):
            assert_review_transition(
                terminal,
                ReviewStatus.IN_PROGRESS,
                actor_role=ReviewerRole.ADMIN,
                admin_reopen=True,
            )

    def test_resolving_requires_a_structured_resolution(self):
        with pytest.raises(InvalidReviewTransition, match="resolution"):
            assert_review_transition(
                ReviewStatus.VERIFYING, ReviewStatus.RESOLVED, actor_role=ReviewerRole.REVIEWER
            )

    def test_resolving_accepts_structured_verification(self):
        assert_review_transition(
            ReviewStatus.VERIFYING,
            ReviewStatus.RESOLVED,
            actor_role=ReviewerRole.REVIEWER,
            resolution=_resolution(),
        )

    def test_resolving_accepts_a_no_change_reason_without_test_evidence(self):
        assert_review_transition(
            ReviewStatus.VERIFYING,
            ReviewStatus.RESOLVED,
            actor_role=ReviewerRole.REVIEWER,
            resolution=_resolution(
                outcome=ResolutionOutcome.ACCEPTED_RISK,
                test_evidence=[],
                no_change_reason="Documented as expected behavior by Compliance.",
            ),
        )

    def test_resolving_rejects_a_resolution_with_neither_evidence_nor_reason(self):
        empty = _resolution(outcome=ResolutionOutcome.FIXED, test_evidence=[])
        with pytest.raises(InvalidReviewTransition, match="verification"):
            assert_review_transition(
                ReviewStatus.VERIFYING,
                ReviewStatus.RESOLVED,
                actor_role=ReviewerRole.REVIEWER,
                resolution=empty,
            )

    def test_wont_fix_also_requires_a_resolution(self):
        with pytest.raises(InvalidReviewTransition):
            assert_review_transition(
                ReviewStatus.TRIAGED, ReviewStatus.WONT_FIX, actor_role=ReviewerRole.REVIEWER
            )
        assert_review_transition(
            ReviewStatus.TRIAGED,
            ReviewStatus.WONT_FIX,
            actor_role=ReviewerRole.REVIEWER,
            resolution=_resolution(
                outcome=ResolutionOutcome.NO_CHANGE,
                test_evidence=[],
                no_change_reason="Duplicate of an already-tracked KB gap.",
            ),
        )

    def test_a_non_fixed_outcome_requires_a_no_change_reason(self):
        with pytest.raises(ValueError, match="no_change_reason"):
            _resolution(outcome=ResolutionOutcome.DUPLICATE, no_change_reason=None)

    def test_self_transition_is_not_a_transition(self):
        with pytest.raises(InvalidReviewTransition):
            assert_review_transition(
                ReviewStatus.TRIAGED, ReviewStatus.TRIAGED, actor_role=ReviewerRole.ADMIN
            )

    def test_viewers_may_never_transition(self):
        with pytest.raises(InvalidReviewTransition):
            assert_review_transition(
                ReviewStatus.UNREVIEWED, ReviewStatus.REVIEWED, actor_role=ReviewerRole.VIEWER
            )


class TestBatchTransitions:

    @pytest.mark.parametrize("current", list(BatchStatus), ids=lambda s: s.value)
    def test_table_matches_the_master_plan_exactly(self, current):
        assert set(allowed_batch_transitions(current)) == BATCH_TABLE[current]

    def test_completed_and_cancelled_are_terminal(self):
        assert allowed_batch_transitions(BatchStatus.COMPLETED) == frozenset()
        assert allowed_batch_transitions(BatchStatus.CANCELLED) == frozenset()

    def test_disallowed_edges_raise(self):
        with pytest.raises(InvalidBatchTransition):
            assert_batch_transition(BatchStatus.DRAFT, BatchStatus.COMPLETED)
        with pytest.raises(InvalidBatchTransition):
            assert_batch_transition(BatchStatus.COMPLETED, BatchStatus.READY)

    def test_only_a_leased_agent_may_drive_the_authoring_path(self):
        for current, target in (
            (BatchStatus.CLAIMED, BatchStatus.PLANNING),
            (BatchStatus.PLANNING, BatchStatus.IN_PROGRESS),
            (BatchStatus.IN_PROGRESS, BatchStatus.CHANGES_PROPOSED),
        ):
            assert_batch_transition(current, target, actor_role=ReviewerRole.AGENT, has_lease=True)
            with pytest.raises(InvalidBatchTransition, match="lease"):
                assert_batch_transition(
                    current, target, actor_role=ReviewerRole.AGENT, has_lease=False
                )
            with pytest.raises(InvalidBatchTransition):
                assert_batch_transition(
                    current, target, actor_role=ReviewerRole.REVIEWER, has_lease=True
                )

    def test_an_independent_human_owns_verification_and_completion(self):
        for current, target in (
            (BatchStatus.CHANGES_PROPOSED, BatchStatus.VERIFYING),
            (BatchStatus.VERIFYING, BatchStatus.COMPLETED),
        ):
            assert_batch_transition(current, target, actor_role=ReviewerRole.REVIEWER)
            assert_batch_transition(current, target, actor_role=ReviewerRole.ADMIN)
            with pytest.raises(InvalidBatchTransition):
                assert_batch_transition(
                    current, target, actor_role=ReviewerRole.AGENT, has_lease=True
                )


class TestBatchLeaseInvariants:

    def test_canonical_lease_windows(self):
        now = utc_now()
        lease = BatchLease(
            lease_token_hash="a" * 64,
            holder="agent@example.invalid",
            acquired_at=now,
            expires_at=now + timedelta(seconds=REMEDIATION_LEASE_S),
            last_heartbeat_at=now,
        )
        assert (lease.expires_at - lease.acquired_at).total_seconds() == REMEDIATION_LEASE_S

    def test_expiry_must_be_after_acquisition(self):
        now = utc_now()
        with pytest.raises(ValueError):
            BatchLease(
                lease_token_hash="a" * 64,
                holder="agent@example.invalid",
                acquired_at=now,
                expires_at=now - timedelta(seconds=1),
                last_heartbeat_at=now,
            )

    def test_a_lease_may_not_exceed_the_canonical_window(self):
        now = utc_now()
        with pytest.raises(ValueError):
            BatchLease(
                lease_token_hash="a" * 64,
                holder="agent@example.invalid",
                acquired_at=now,
                expires_at=now + timedelta(seconds=REMEDIATION_LEASE_S + 1),
                last_heartbeat_at=now,
            )

    def test_continuous_lease_cap_is_enforced(self):
        now = utc_now()
        lease = BatchLease(
            lease_token_hash="a" * 64,
            holder="agent@example.invalid",
            acquired_at=now,
            expires_at=now + timedelta(seconds=REMEDIATION_LEASE_S),
            last_heartbeat_at=now,
            continuous_since=now - timedelta(seconds=REMEDIATION_MAX_CONTINUOUS_LEASE_S),
        )
        assert lease.continuous_cap_reached(at=now) is True
        assert lease.continuous_cap_reached(at=now - timedelta(seconds=1)) is False

    def test_the_raw_lease_token_is_never_a_model_field(self):
        assert "lease_token" not in BatchLease.model_fields
        assert "lease_token_hash" in BatchLease.model_fields

    def test_extension_is_bounded(self):
        with pytest.raises(ValueError):
            BatchOutcome(
                decision="completed",
                summary="done",
                extension_minutes=MAX_LEASE_EXTENSION_MINUTES + 1,
            )


class TestImportTransitions:

    @pytest.mark.parametrize("current", list(ImportStatus), ids=lambda s: s.value)
    def test_table_matches_the_master_plan_exactly(self, current):
        assert set(allowed_import_transitions(current)) == IMPORT_TABLE[current]

    def test_reversed_and_cancelled_are_terminal(self):
        assert allowed_import_transitions(ImportStatus.REVERSED) == frozenset()
        assert allowed_import_transitions(ImportStatus.CANCELLED) == frozenset()

    def test_disallowed_edges_raise(self):
        with pytest.raises(InvalidImportTransition):
            assert_import_transition(ImportStatus.UPLOADED, ImportStatus.APPLIED)
        with pytest.raises(InvalidImportTransition):
            assert_import_transition(ImportStatus.REVERSED, ImportStatus.APPLYING)

    def test_recovery_from_failed_requires_an_explicit_admin_reason(self):
        assert_import_transition(
            ImportStatus.FAILED, ImportStatus.PLANNED, reason="Corrected the source file."
        )
        with pytest.raises(InvalidImportTransition, match="reason"):
            assert_import_transition(ImportStatus.FAILED, ImportStatus.PLANNED)

    def test_reversal_rows_carry_the_expected_review_version(self):
        row = TicketImportRow(
            row_number=1,
            raw_ticket_id="TKT-1234",
            expected_review_version=3,
        )
        assert row.expected_review_version == 3

    def test_a_reversal_never_deletes_history(self):
        # A reversed import flips the review's import_state; it does not remove
        # the review, so the field must exist and be settable to "reversed".
        assert _review(import_state=ImportState.REVERSED).import_state is ImportState.REVERSED


# =====================================================================
# 16. No DevRev title in the durable record
# =====================================================================

PII_TITLE = "Rollover question from sam.participant@example.invalid (555-0100)"


class TestTitleConfinement:

    def test_durable_review_has_no_title_field(self):
        assert "title" not in TicketReview.model_fields
        assert "devrev_title" not in TicketReview.model_fields

    def test_a_title_cannot_be_smuggled_into_the_durable_review(self):
        with pytest.raises(ValueError):
            _review(title=PII_TITLE)

    def test_a_pii_bearing_title_is_allowed_on_live_and_cache_models(self):
        summary = DevRevTicketSummary(
            devrev_work_id=SYNTHETIC_DON, devrev_display_id="TKT-1234", title=PII_TITLE
        )
        detail = DevRevTicketDetail(
            devrev_work_id=SYNTHETIC_DON, devrev_display_id="TKT-1234", title=PII_TITLE
        )
        assert summary.title == PII_TITLE
        assert detail.title == PII_TITLE

    def test_no_title_survives_a_durable_review_serialization(self):
        payload = json.dumps(_review().model_dump(mode="json"))
        assert "title" not in payload

    def test_the_fixture_supplies_the_pii_title_material(self):
        page = _load("works_list_page_1.json")["response"]
        titles = [w["title"] for w in page["works"]]
        assert any("@example.invalid" in t for t in titles)


# =====================================================================
# 17. Batch parent/item split and the 1 MiB document limit
# =====================================================================


class TestBatchDocumentBudget:

    def test_the_parent_stores_no_item_array(self):
        fields = set(RemediationBatch.model_fields)
        assert "items" not in fields
        assert "review_refs" not in fields
        assert {"item_count", "item_set_digest"} <= fields

    def test_parent_and_items_round_trip_separately(self):
        parent = _batch()
        item = _batch_item()
        assert RemediationBatch.model_validate(
            json.loads(json.dumps(parent.model_dump(mode="json")))
        ) == parent
        assert RemediationBatchItem.model_validate(
            json.loads(json.dumps(item.model_dump(mode="json")))
        ) == item

    def test_frozen_review_reference_is_immutable(self):
        item = _batch_item()
        with pytest.raises(ValueError):
            item.review_id = "something-else"
        with pytest.raises(ValueError):
            item.review_version = 99

    def test_item_count_is_bounded_by_the_canonical_maximum(self):
        assert _batch(item_count=MAX_BATCH_REVIEWS).item_count == MAX_BATCH_REVIEWS
        with pytest.raises(ValueError):
            _batch(item_count=MAX_BATCH_REVIEWS + 1)

    def test_worst_case_batch_cannot_create_a_one_mebibyte_parent(self):
        parent = _batch(
            item_count=MAX_BATCH_REVIEWS,
            plan_artifact="p" * MAX_SUMMARY_LENGTH,
            verification_summary="v" * MAX_SUMMARY_LENGTH,
        )
        parent_bytes = len(json.dumps(parent.model_dump(mode="json")).encode("utf-8"))
        assert parent_bytes < FIRESTORE_MAX_DOCUMENT_BYTES
        # A generous safety margin: the parent must stay far below the limit
        # regardless of how many items the batch freezes.
        assert parent_bytes < FIRESTORE_MAX_DOCUMENT_BYTES // 4

    def test_each_worst_case_item_document_stays_within_the_limit(self):
        item = RemediationBatchItem(
            review_id=review_id_for_devrev_work(SYNTHETIC_DON),
            review_version=3,
            devrev_display_id="T" * MAX_DISPLAY_ID_LENGTH,
            observation_type=ObservationType.KNOWLEDGE_GAP,
            severity=Severity.CRITICAL,
            remediation_target=RemediationTarget.KB,
            comments_excerpt="c" * MAX_SUMMARY_LENGTH,
        )
        item_bytes = len(json.dumps(item.model_dump(mode="json")).encode("utf-8"))
        assert item_bytes < FIRESTORE_MAX_DOCUMENT_BYTES

    def test_embedding_one_hundred_reviews_would_have_overflowed_a_parent(self):
        """Justifies the parent/item split with arithmetic rather than assertion.

        The naive design — one batch document holding its constituent reviews —
        provably cannot hold a worst-case 100-review batch, which is why each
        frozen review is its own ``items/{review_id}`` document.
        """
        worst_review = _review(
            comments="c" * MAX_COMMENTS_LENGTH,
            expected_behavior="e" * MAX_EXPECTED_BEHAVIOR_LENGTH,
        )
        review_bytes = len(json.dumps(worst_review.model_dump(mode="json")).encode("utf-8"))
        assert review_bytes * MAX_BATCH_REVIEWS > FIRESTORE_MAX_DOCUMENT_BYTES

    def test_each_frozen_item_is_a_separate_bounded_document(self):
        worst = RemediationBatchItem(
            review_id=review_id_for_devrev_work(SYNTHETIC_DON),
            review_version=3,
            devrev_display_id="TKT-1234",
            comments_excerpt="c" * MAX_SUMMARY_LENGTH,
        )
        worst_bytes = len(json.dumps(worst.model_dump(mode="json")).encode("utf-8"))
        assert 0 < worst_bytes < FIRESTORE_MAX_DOCUMENT_BYTES
        # The parent never grows with the item count, so the batch as a whole
        # scales by documents rather than by parent bytes.
        small = len(json.dumps(_batch(item_count=1).model_dump(mode="json")).encode("utf-8"))
        large = len(
            json.dumps(_batch(item_count=MAX_BATCH_REVIEWS).model_dump(mode="json")).encode("utf-8")
        )
        assert large - small <= 2


# =====================================================================
# 18. Service-boundary secret isolation
# =====================================================================


class TestServiceBoundaryIsolation:

    def test_the_console_never_declares_a_correlation_secret_field(self):
        fields = set(TicketConsoleSettings.model_fields)
        assert not [f for f in fields if "CORRELATION" in f]

    @pytest.mark.parametrize("forbidden", sorted(CONSOLE_FORBIDDEN_ENV_VARS))
    def test_a_correlation_secret_in_the_console_revision_fails_validation(
        self, monkeypatch, forbidden
    ):
        cfg = _production_settings(monkeypatch)
        with pytest.raises(ValueError, match=forbidden):
            validate_ticket_console_settings(cfg, env={forbidden: "synthetic-value"})

    def test_the_broker_holds_only_its_own_keyring(self):
        fields = set(EvidenceBrokerSettings.model_fields)
        assert "CORRELATION_LOOKUP_KEYRING_JSON" in fields
        assert "CORRELATION_INGRESS_KEY" not in fields
        assert "DEVREV_TOKEN" not in fields

    def test_the_broker_rejects_an_ingress_key(self):
        broker = EvidenceBrokerSettings(
            _env_file=None,
            ENVIRONMENT="production",
            FIRESTORE_DATABASE="(default)",
            CONSOLE_SERVICE_ACCOUNT="console@example.invalid",
            AUDIENCE="https://broker.example.invalid",
            CORRELATION_LOOKUP_KEYRING_JSON='{"1": "synthetic-keyring-value"}',
            CORRELATION_ALLOWED_KEY_VERSIONS=[1],
        )
        assert validate_evidence_broker_settings(broker, env={}) is True
        with pytest.raises(ValueError, match="TICKETS_CORRELATION_INGRESS_KEY"):
            validate_evidence_broker_settings(
                broker, env={"TICKETS_CORRELATION_INGRESS_KEY": "synthetic-value"}
            )

    def test_the_producer_holds_only_ingress_and_current_lookup_keys(self):
        fields = set(ProducerCorrelationSettings.model_fields)
        assert {
            "CORRELATION_INGRESS_KEY",
            "CORRELATION_INGRESS_KEY_VERSION",
            "CORRELATION_LOOKUP_KEY",
            "CORRELATION_LOOKUP_KEY_VERSION",
        } <= fields
        assert "CORRELATION_LOOKUP_KEYRING_JSON" not in fields
        assert "DEVREV_TOKEN" not in fields

    def test_the_producer_rejects_the_broker_keyring(self):
        producer = ProducerCorrelationSettings(
            _env_file=None,
            CORRELATION_INGRESS_KEY="synthetic-ingress-value",
            CORRELATION_INGRESS_KEY_VERSION=1,
            CORRELATION_LOOKUP_KEY="synthetic-lookup-value",
            CORRELATION_LOOKUP_KEY_VERSION=1,
        )
        assert validate_producer_correlation_settings(producer, env={}) is True
        with pytest.raises(ValueError, match="TICKETS_CORRELATION_LOOKUP_KEYRING_JSON"):
            validate_producer_correlation_settings(
                producer, env={"TICKETS_CORRELATION_LOOKUP_KEYRING_JSON": "{}"}
            )

    def test_ingress_and_lookup_keys_must_differ(self):
        producer = ProducerCorrelationSettings(
            _env_file=None,
            CORRELATION_INGRESS_KEY="identical-synthetic-value",
            CORRELATION_INGRESS_KEY_VERSION=1,
            CORRELATION_LOOKUP_KEY="identical-synthetic-value",
            CORRELATION_LOOKUP_KEY_VERSION=1,
        )
        with pytest.raises(ValueError, match="must differ"):
            validate_producer_correlation_settings(producer, env={})

    def test_broker_response_cap_is_canonical(self):
        broker = EvidenceBrokerSettings(_env_file=None)
        assert broker.MAX_RESPONSE_BYTES == EVIDENCE_BROKER_MAX_RESPONSE_BYTES


# =====================================================================
# 19. Cursor authenticated encryption
# =====================================================================

CURSOR_CONTEXT = "devrev-tickets:after:v1"
CURSOR_PAYLOAD = {
    "v": 1,
    "devrev_cursor": "opaque-remote-cursor-value",
    "filter_fingerprint": "a" * 32,
    "subject": "accounts.google.com:synthetic-000",
}


class TestCursorAead:

    def test_key_decodes_to_exactly_thirty_two_bytes(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        assert isinstance(key, bytes)
        assert len(key) == CURSOR_AEAD_KEY_BYTES == 32

    @pytest.mark.parametrize(
        "value",
        ("", "not-base64!!", "c2hvcnQ=", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWZmZg=="),
        ids=("empty", "not-base64", "too-short", "too-long"),
    )
    def test_a_key_that_is_not_thirty_two_bytes_is_rejected(self, value):
        with pytest.raises(ValueError, match="32"):
            decode_cursor_aead_key(value)

    def test_round_trip_returns_the_original_payload(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        assert open_cursor(key, token, context=CURSOR_CONTEXT) == CURSOR_PAYLOAD

    def test_the_token_hides_every_plaintext_field(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        assert "opaque-remote-cursor-value" not in token
        assert "accounts.google.com" not in token
        assert "filter_fingerprint" not in token
        assert CURSOR_PAYLOAD["filter_fingerprint"] not in token

    def test_the_token_is_url_safe_and_bounded(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        assert len(token) <= MAX_CURSOR_LENGTH
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_encryption_is_randomized(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        first = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        second = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        assert first != second
        assert open_cursor(key, first, context=CURSOR_CONTEXT) == open_cursor(
            key, second, context=CURSOR_CONTEXT
        )

    def test_a_deterministic_nonce_and_clock_are_injectable_for_tests(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        nonce = b"\x00" * 12
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        first = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT, nonce=nonce, now=now)
        second = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT, nonce=nonce, now=now)
        assert first == second

    def test_a_tampered_ciphertext_is_rejected(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        # Flip the LAST character, and assert the mutation actually happened.
        # Mutating a character chosen by a different character's value would
        # silently be a no-op for ~2.5% of random nonces, making this test flaky
        # and, worse, vacuous when it passed.
        flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert flipped != token
        with pytest.raises(CursorError):
            open_cursor(key, flipped, context=CURSOR_CONTEXT)

    def test_tampering_is_rejected_at_every_offset(self):
        """Nonce bytes, ciphertext bytes, and tag bytes are all authenticated."""
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT, nonce=b"\x01" * 12)
        for index in range(len(token)):
            mutated = (
                token[:index] + ("A" if token[index] != "A" else "B") + token[index + 1 :]
            )
            assert mutated != token
            with pytest.raises(CursorError):
                open_cursor(key, mutated, context=CURSOR_CONTEXT)

    @pytest.mark.parametrize(
        "mangle",
        (
            lambda t: t[:5] + " " + t[5:],
            lambda t: t[:5] + "\n" + t[5:],
            lambda t: t.replace("-", "+").replace("_", "/"),
            lambda t: t + "==",
            lambda t: t + "!",
        ),
        ids=("space", "newline", "standard-alphabet", "padding", "non-alphabet"),
    )
    def test_a_non_canonical_encoding_is_rejected(self, mangle):
        """One cursor must have exactly one encoding.

        ``base64.urlsafe_b64decode`` silently discards non-alphabet bytes, so
        without a strict check an unbounded set of distinct token strings would
        all decrypt to the same cursor.
        """
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        variant = mangle(token)
        if variant == token:
            pytest.skip("this token has no character to mangle for this variant")
        with pytest.raises(CursorError):
            open_cursor(key, variant, context=CURSOR_CONTEXT)

    def test_a_cursor_expires(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT, ttl_s=60, now=now)
        assert open_cursor(key, token, context=CURSOR_CONTEXT, now=now) == CURSOR_PAYLOAD
        assert (
            open_cursor(key, token, context=CURSOR_CONTEXT, now=now + timedelta(seconds=59))
            == CURSOR_PAYLOAD
        )
        with pytest.raises(CursorError, match="expired"):
            open_cursor(key, token, context=CURSOR_CONTEXT, now=now + timedelta(seconds=60))

    def test_the_expiry_is_sealed_inside_the_ciphertext(self):
        """A caller must not be able to read or extend the expiry."""
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        assert CURSOR_EXPIRY_KEY not in token
        # The expiry is stripped on the way out, so callers never see it.
        assert CURSOR_EXPIRY_KEY not in open_cursor(key, token, context=CURSOR_CONTEXT)

    def test_the_reserved_expiry_key_cannot_be_supplied_by_a_caller(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        with pytest.raises(ValueError, match=CURSOR_EXPIRY_KEY):
            seal_cursor(key, {CURSOR_EXPIRY_KEY: 1}, context=CURSOR_CONTEXT)

    def test_a_default_ttl_is_applied_without_an_explicit_argument(self):
        assert CURSOR_DEFAULT_TTL_S == CACHE_TTL_S
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT, now=now)
        with pytest.raises(CursorError, match="expired"):
            open_cursor(
                key,
                token,
                context=CURSOR_CONTEXT,
                now=now + timedelta(seconds=CURSOR_DEFAULT_TTL_S),
            )

    def test_a_truncated_token_is_rejected(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        with pytest.raises(CursorError):
            open_cursor(key, token[:8], context=CURSOR_CONTEXT)

    def test_a_replayed_token_from_another_context_is_rejected(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        for other in (
            "devrev-tickets:before:v1",
            "ticket-reviews:after:v1",
            "devrev-tickets:after:v2",
        ):
            with pytest.raises(CursorError):
                open_cursor(key, token, context=other)

    def test_another_key_cannot_open_the_token(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        other = decode_cursor_aead_key("Zm9vYmFyMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODk=")
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        with pytest.raises(CursorError):
            open_cursor(other, token, context=CURSOR_CONTEXT)

    def test_the_error_never_discloses_the_plaintext_or_the_key(self):
        key = decode_cursor_aead_key(TEST_AEAD_KEY_B64)
        token = seal_cursor(key, CURSOR_PAYLOAD, context=CURSOR_CONTEXT)
        with pytest.raises(CursorError) as caught:
            open_cursor(key, token, context="ticket-reviews:after:v1")
        message = str(caught.value)
        assert "opaque-remote-cursor-value" not in message
        assert TEST_AEAD_KEY_B64 not in message

    def test_the_approved_aead_implementation_is_present_in_the_runtime_lock(self):
        """The plan forbids an ad hoc cipher; AESGCM must come from the lock."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        assert AESGCM is not None
        lock = Path(__file__).resolve().parents[1] / "requirements.lock"
        assert lock.exists()
        assert "cryptography==" in lock.read_text()


# =====================================================================
# Audit hash chain
# =====================================================================


class TestAuditEventHashChain:

    def test_genesis_hash_is_sixty_four_ascii_zeroes(self):
        assert GENESIS_EVENT_HASH == "0" * 64
        assert len(GENESIS_EVENT_HASH) == 64

    def test_hash_is_stable_and_lowercase_hex(self):
        event = _audit_event()
        first = compute_audit_event_hash(event)
        second = compute_audit_event_hash(event)
        assert first == second
        assert len(first) == 64
        assert first == first.lower()

    def test_hash_ignores_changed_field_order_and_duplicates(self):
        base = compute_audit_event_hash(_audit_event(changed_fields=["comments", "rating"]))
        assert compute_audit_event_hash(_audit_event(changed_fields=["rating", "comments"])) == base
        assert (
            compute_audit_event_hash(_audit_event(changed_fields=["rating", "comments", "rating"]))
            == base
        )

    @pytest.mark.parametrize(
        "override",
        (
            {"event_id": "event-0002"},
            {"parent_kind": "batch"},
            {"event_type": "review_created"},
            {"actor_subject_hash": "9" * 64},
            {"request_id_hash": "9" * 64},
            {"idempotency_key_hash": "9" * 64},
            {"previous_version": 1},
            {"new_version": 4},
            {"changed_fields": ["severity"]},
            {"previous_event_hash": "1" * 64},
            {"occurred_at_unix_us": 1_700_000_000_000_001},
        ),
    )
    def test_mutating_any_hashed_field_changes_the_hash(self, override):
        assert compute_audit_event_hash(_audit_event(**override)) != compute_audit_event_hash(
            _audit_event()
        )

    def test_raw_actor_identity_is_excluded_from_the_hash(self):
        base = compute_audit_event_hash(_audit_event())
        renamed = compute_audit_event_hash(
            _audit_event(actor_email="someone.else@example.invalid")
        )
        assert renamed == base

    def test_hash_schema_version_is_pinned(self):
        assert HASH_SCHEMA_VERSION == 1
        assert _audit_event().hash_schema_version == 1

    def test_audit_events_are_immutable(self):
        event = _audit_event()
        with pytest.raises(ValueError):
            event.new_version = 99


# =====================================================================
# DevRev live models, filters, and provenance
# =====================================================================


class TestDevRevContracts:

    def test_ticket_filters_are_closed(self):
        with pytest.raises(ValueError):
            DevRevTicketFilters(unknown_filter=["x"])

    def test_caller_supplied_type_is_rejected(self):
        assert "type" not in DevRevTicketFilters.model_fields
        with pytest.raises(ValueError):
            DevRevTicketFilters(type=["issue"])

    def test_filters_expose_the_allowlisted_wire_fields(self):
        fields = set(DevRevTicketFilters.model_fields)
        assert {
            "stage",
            "state",
            "applies_to_part",
            "owned_by",
            "created_by",
            "reported_by",
            "tags",
            "created_date",
            "modified_date",
            "ticket_source_channel",
            "ticket_subtype",
            "ticket_visibility",
        } <= fields

    def test_unknown_timeline_entry_types_are_preserved_as_unsupported(self):
        entry = DevRevTimelineEntry(
            entry_id="don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234:timeline_event/5",
            object_id=SYNTHETIC_DON,
            kind=TimelineEntryKind.UNSUPPORTED,
            visibility=TimelineVisibility.INTERNAL,
            created_at=utc_now(),
            unsupported_type="timeline_future_entry_type_v9",
        )
        assert entry.kind is TimelineEntryKind.UNSUPPORTED
        assert entry.unsupported_type == "timeline_future_entry_type_v9"

    def test_change_events_are_not_authored_replies(self):
        entry = DevRevTimelineEntry(
            entry_id="don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234:timeline_event/4",
            object_id=SYNTHETIC_DON,
            kind=TimelineEntryKind.CHANGE_EVENT,
            visibility=TimelineVisibility.INTERNAL,
            created_at=utc_now(),
        )
        assert entry.body is None
        assert entry.author is None

    def test_no_model_stores_a_raw_devrev_payload(self):
        for model in (DevRevTicketSummary, DevRevTicketDetail, DevRevTimelineEntry, TicketReview):
            assert "raw" not in model.model_fields
            assert model.model_config.get("extra") == "forbid"

    def test_provenance_never_fabricates_a_link(self):
        provenance = RagProvenance()
        assert provenance.correlation_status is CorrelationStatus.UNAVAILABLE
        assert provenance.correlation_trust is CorrelationTrust.NONE
        assert provenance.index_version is None
        assert provenance.missing_provenance is True

    def test_observed_chunk_ref_references_an_existing_vector_id(self):
        ref = ObservedChunkRef(
            observed_vector_id="pa-distributions-0001-c3",
            article_id="pa-distributions-0001",
            content_sha256="a" * 64,
            chunk_ordinal=3,
            namespace="pa-production",
        )
        assert ref.chunk_ordinal == 3
        with pytest.raises(ValueError):
            ObservedChunkRef(
                observed_vector_id="pa-distributions-0001-c3",
                article_id="pa-distributions-0001",
                content_sha256="not-a-sha",
                chunk_ordinal=3,
                namespace="pa-production",
            )
        with pytest.raises(ValueError):
            ObservedChunkRef(
                observed_vector_id="pa-distributions-0001-c3",
                article_id="pa-distributions-0001",
                content_sha256="a" * 64,
                chunk_ordinal=-1,
                namespace="pa-production",
            )


# =====================================================================
# Fixture contract
# =====================================================================


class TestDevRevFixtures:

    @pytest.mark.parametrize(
        "name",
        (
            "works_list_page_1.json",
            "works_list_page_2.json",
            "work_get_ticket.json",
            "timeline_page_empty_with_cursor.json",
            "timeline_page_final.json",
        ),
    )
    def test_every_fixture_declares_synthetic_provenance(self, name):
        fixture = _load(name)
        assert "SYNTHETIC" in fixture["_meta"]["provenance"].upper()
        assert fixture["_meta"]["sanitization"]
        assert fixture["_meta"]["devrev_version"] == "2022-10-20"
        assert "response" in fixture

    def test_list_pages_carry_forward_and_backward_cursors(self):
        first = _load("works_list_page_1.json")["response"]
        second = _load("works_list_page_2.json")["response"]
        assert first["next_cursor"]
        assert first["prev_cursor"] is None
        assert second["next_cursor"] is None
        assert second["prev_cursor"]

    def test_an_empty_timeline_page_still_carries_a_next_cursor(self):
        """The documented DevRev invariant: empty page != end of iteration."""
        page = _load("timeline_page_empty_with_cursor.json")["response"]
        assert page["timeline_entries"] == []
        assert page["next_cursor"]

    def test_the_final_timeline_page_ends_iteration(self):
        page = _load("timeline_page_final.json")["response"]
        assert page["timeline_entries"]
        assert page["next_cursor"] is None

    def test_the_timeline_covers_every_required_author_class_and_an_unknown_type(self):
        entries = _load("timeline_page_final.json")["response"]["timeline_entries"]
        types = [e["type"] for e in entries]
        assert types.count("timeline_comment") >= 3
        assert "timeline_change_event" in types
        assert any(t not in {"timeline_comment", "timeline_change_event"} for t in types)
        visibilities = {e.get("visibility") for e in entries if "visibility" in e}
        assert {"external", "internal"} <= visibilities

    @pytest.mark.parametrize(
        "name",
        (
            "works_list_page_1.json",
            "works_list_page_2.json",
            "work_get_ticket.json",
            "timeline_page_empty_with_cursor.json",
            "timeline_page_final.json",
        ),
    )
    def test_no_fixture_contains_a_real_data_fingerprint(self, name):
        text = (FIXTURES / name).read_text().lower()
        for fingerprint in ("bearer", "forusall.com", "api_key", "apikey", "api-key"):
            assert fingerprint not in text
        # Every DON present must carry the unmistakably-fake tenant segment. The
        # previous form ended in `or "timeline_entries" in text`, which was
        # unconditionally true for the timeline fixtures and exempted them
        # entirely. The empty-page fixture legitimately contains no DON at all,
        # so the positive assertion is conditional and the negative is absolute.
        assert re.search(r"don:[^\"]*devo/(?!synthetic00)", text) is None
        if "don:" in text:
            assert "devo/synthetic00" in text

    @pytest.mark.parametrize(
        "name",
        (
            "works_list_page_1.json",
            "works_list_page_2.json",
            "work_get_ticket.json",
            "timeline_page_final.json",
        ),
    )
    def test_every_display_id_is_in_the_synthetic_range(self, name):
        text = (FIXTURES / name).read_text()
        found = re.findall(r"TKT-\d+", text)
        assert found
        for display_id in found:
            assert re.fullmatch(r"TKT-\d{4}", display_id), display_id

    def test_the_unknown_entry_type_is_representable_by_the_frozen_model(self):
        """The mandated 'unknown type' fixture entry carries no visibility.

        The durable model must still be able to hold it, fail-closed, without
        Stage 2 inventing a shape.
        """
        entries = _load("timeline_page_final.json")["response"]["timeline_entries"]
        unknown = entries[-1]
        assert "visibility" not in unknown
        entry = DevRevTimelineEntry(
            entry_id=unknown["id"],
            object_id=unknown["object"],
            kind=TimelineEntryKind.UNSUPPORTED,
            unsupported_type=unknown["type"],
            created_at=utc_now(),
        )
        assert entry.visibility is TimelineVisibility.PRIVATE
        assert entry.body is None


# =====================================================================
# Hardening added after the Stage 1 adversarial review
# =====================================================================


class TestAuditMetadataBounds:
    """`metadata` is the only free-text map on a durable audit record.

    Only `reason_code` enters the frozen hash payload, so an unbounded map here
    would be both a PII route into the ledger and a field the tamper-evident
    chain does not cover.
    """

    def test_key_count_is_bounded(self):
        _audit_event(metadata={f"k{i}": "v" for i in range(MAX_METADATA_KEYS)})
        with pytest.raises(ValueError, match="metadata"):
            _audit_event(metadata={f"k{i}": "v" for i in range(MAX_METADATA_KEYS + 1)})

    def test_value_length_is_bounded(self):
        _audit_event(metadata={"reason_code": "v" * MAX_METADATA_VALUE_LENGTH})
        with pytest.raises(ValueError, match="metadata"):
            _audit_event(metadata={"reason_code": "v" * (MAX_METADATA_VALUE_LENGTH + 1)})

    def test_a_ticket_body_cannot_be_smuggled_into_the_ledger(self):
        with pytest.raises(ValueError, match="metadata"):
            _audit_event(metadata={"note": "b" * MAX_COMMENTS_LENGTH})

    @pytest.mark.parametrize("value", ("Z" * 64, "0" * 63, "0" * 65, "ABCDEF" + "0" * 58))
    def test_the_chain_link_must_be_lowercase_hex(self, value):
        with pytest.raises(ValueError):
            _audit_event(previous_event_hash=value)

    def test_genesis_is_a_valid_chain_link(self):
        assert _audit_event(previous_event_hash=GENESIS_EVENT_HASH).previous_event_hash == (
            GENESIS_EVENT_HASH
        )

    def test_the_frozen_hash_payload_key_set_is_exactly_the_planned_one(self):
        """The master plan freezes this key list verbatim; a key must never be
        added, removed, or omitted (absent values are JSON null)."""
        assert set(audit_event_hash_payload(_audit_event())) == {
            "hash_schema_version",
            "event_id",
            "parent_kind",
            "parent_id",
            "event_type",
            "actor_subject_hash",
            "request_id_hash",
            "idempotency_key_hash",
            "previous_version",
            "new_version",
            "changed_fields",
            "reason_code",
            "previous_event_hash",
            "occurred_at_unix_us",
        }

    def test_absent_values_are_null_rather_than_omitted(self):
        payload = audit_event_hash_payload(
            _audit_event(
                request_id_hash=None,
                idempotency_key_hash=None,
                previous_version=None,
                new_version=None,
                metadata={},
            )
        )
        for key in (
            "request_id_hash",
            "idempotency_key_hash",
            "previous_version",
            "new_version",
            "reason_code",
        ):
            assert key in payload
            assert payload[key] is None

    def test_the_payload_serializes_deterministically(self):
        payload = audit_event_hash_payload(_audit_event())
        assert json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )


class TestPageBounds:

    def test_items_cannot_exceed_the_canonical_maximum(self):
        with pytest.raises(ValueError):
            CursorPage[DevRevTicketSummary](
                items=[_summary() for _ in range(MAX_PAGE_SIZE + 1)],
                page_size=MAX_PAGE_SIZE,
            )

    def test_items_cannot_exceed_the_declared_page_size(self):
        with pytest.raises(ValueError, match="page_size"):
            CursorPage[DevRevTicketSummary](items=[_summary(), _summary()], page_size=1)

    def test_a_full_page_is_accepted(self):
        page = CursorPage[DevRevTicketSummary](items=[_summary()], page_size=1)
        assert len(page.items) == 1


class TestSeparatedListBounds:

    def test_the_attachment_bound_is_not_reused_for_unrelated_lists(self):
        """MAX_ATTACHMENTS is canonical; MAX_LIST_ITEMS is not.

        They may share a value today, but tightening the attachment rule must
        not silently reshape filter or owner lists.
        """
        assert MAX_ATTACHMENTS == 20
        assert MAX_LIST_ITEMS == 20
        import api.ticket_review_models as module

        source = Path(module.__file__).read_text()
        # Exactly one field may use the canonical attachment bound.
        assert source.count("max_length=MAX_ATTACHMENTS") == 1

    def test_the_responsive_contract_is_frozen(self):
        assert RESPONSIVE_BREAKPOINT_PX == 768
        assert MIN_TOUCH_TARGET_PX == 44


class TestUtcNormalization:

    def test_a_non_utc_aware_datetime_is_converted(self):
        """The `_Base` normalizer must actually run, not be a silent no-op."""
        eastern = timezone(timedelta(hours=-5))
        local_noon = datetime(2026, 7, 27, 12, 0, tzinfo=eastern)
        review = _review(created_at=local_noon)
        assert review.created_at.utcoffset() == timedelta(0)
        assert review.created_at.hour == 17
        assert review.created_at == local_noon

    def test_normalization_reaches_a_nested_model(self):
        eastern = timezone(timedelta(hours=-5))
        resolution = _resolution(verified_at=datetime(2026, 7, 27, 12, 0, tzinfo=eastern))
        review = _review(resolution=resolution)
        assert review.resolution is not None
        assert review.resolution.verified_at is not None
        assert review.resolution.verified_at.utcoffset() == timedelta(0)


class TestPreconditionEdgeCases:

    def test_a_trailing_newline_is_not_a_valid_validator(self):
        """`$` also matches before a trailing newline in Python; `\\Z` does not."""
        with pytest.raises(MalformedPreconditionError):
            parse_if_match('"v3"\n')

    def test_an_overlong_header_is_malformed_not_a_crash(self):
        with pytest.raises(MalformedPreconditionError):
            parse_if_match('"v' + "1" * 5000 + '"')
        with pytest.raises(MalformedPreconditionError):
            parse_if_match('"v' + "1" * MAX_IF_MATCH_HEADER_LENGTH + '"')

    @pytest.mark.parametrize("header", ("", "   ", "\t"))
    def test_a_present_but_empty_header_counts_as_missing(self, header):
        """Deliberate: an empty header conveys no version, so 428 (supply one)
        is more actionable than 422 (malformed)."""
        with pytest.raises(MissingPreconditionError):
            parse_if_match(header)

    def test_multiple_validators_are_rejected(self):
        with pytest.raises(MalformedPreconditionError):
            parse_if_match('"v1", "v2"')


class TestRoleCoverage:

    @pytest.mark.parametrize("role", list(ReviewerRole), ids=lambda r: r.value)
    def test_assignment_truth_table_is_complete(self, role):
        actor = _identity("a@example.invalid")
        other = _identity("b@example.invalid")
        may_self_assign_unassigned = can_assign_reviewer(
            actor_role=role, actor=actor, target=actor, current_assignee=None
        )
        may_assign_other = can_assign_reviewer(
            actor_role=role, actor=actor, target=other, current_assignee=None
        )
        may_steal = can_assign_reviewer(
            actor_role=role, actor=actor, target=actor, current_assignee=other
        )
        if role is ReviewerRole.ADMIN:
            assert (may_self_assign_unassigned, may_assign_other, may_steal) == (
                True,
                True,
                True,
            )
        elif role in {ReviewerRole.REVIEWER, ReviewerRole.REMEDIATOR}:
            assert (may_self_assign_unassigned, may_assign_other, may_steal) == (
                True,
                False,
                False,
            )
        else:  # viewer, agent
            assert (may_self_assign_unassigned, may_assign_other, may_steal) == (
                False,
                False,
                False,
            )

    def test_reassigning_to_the_current_holder_is_idempotent(self):
        actor = _identity("a@example.invalid")
        assert can_assign_reviewer(
            actor_role=ReviewerRole.REVIEWER, actor=actor, target=actor, current_assignee=actor
        )

    @pytest.mark.parametrize("role", (ReviewerRole.VIEWER, ReviewerRole.AGENT))
    def test_viewers_and_agents_may_never_change_a_review_status(self, role):
        """Agents act on batches, never directly on a review's status."""
        with pytest.raises(InvalidReviewTransition, match="may not change"):
            assert_review_transition(
                ReviewStatus.UNREVIEWED, ReviewStatus.REVIEWED, actor_role=role
            )

    @pytest.mark.parametrize(
        "current", (ReviewStatus.TRIAGED, ReviewStatus.PLANNED, ReviewStatus.BLOCKED)
    )
    def test_admin_reopen_on_a_non_terminal_review_is_refused(self, current):
        with pytest.raises(InvalidReviewTransition, match="not terminal"):
            assert_review_transition(
                current, ReviewStatus.TRIAGED, actor_role=ReviewerRole.ADMIN, admin_reopen=True
            )


class TestBatchEvidence:

    def test_the_parent_stores_bounded_test_evidence(self):
        """The master plan's Firestore contract requires it alongside the
        plan/branch/commit references."""
        assert "test_evidence" in RemediationBatch.model_fields
        batch = _batch(test_evidence=[_verification()])
        assert len(batch.test_evidence) == 1
        with pytest.raises(ValueError):
            _batch(test_evidence=[_verification() for _ in range(MAX_LIST_ITEMS + 1)])

    def test_evidence_does_not_let_the_parent_approach_the_document_limit(self):
        batch = _batch(
            item_count=MAX_BATCH_REVIEWS,
            test_evidence=[_verification() for _ in range(MAX_LIST_ITEMS)],
            plan_artifact="p" * MAX_SUMMARY_LENGTH,
            verification_summary="v" * MAX_SUMMARY_LENGTH,
        )
        size = len(json.dumps(batch.model_dump(mode="json")).encode("utf-8"))
        assert size < FIRESTORE_MAX_DOCUMENT_BYTES // 4


class TestServiceBoundaryCompleteness:

    def test_the_broker_refuses_every_secret_it_does_not_own(self):
        assert {
            "TICKETS_CORRELATION_INGRESS_KEY",
            "TICKETS_CORRELATION_LOOKUP_KEY",
            "TICKETS_DEVREV_TOKEN",
            "TICKETS_CSRF_SIGNING_SECRET",
            "TICKETS_CURSOR_AEAD_KEY",
        } <= BROKER_FORBIDDEN_ENV_VARS

    def test_the_producer_refuses_every_secret_it_does_not_own(self):
        assert {
            "TICKETS_CORRELATION_LOOKUP_KEYRING_JSON",
            "TICKETS_DEVREV_TOKEN",
            "TICKETS_CSRF_SIGNING_SECRET",
            "TICKETS_CURSOR_AEAD_KEY",
        } <= PRODUCER_FORBIDDEN_ENV_VARS

    def test_no_plane_forbids_a_secret_it_actually_needs(self):
        assert "TICKETS_DEVREV_TOKEN" not in CONSOLE_FORBIDDEN_ENV_VARS
        assert "TICKETS_CORRELATION_LOOKUP_KEYRING_JSON" not in BROKER_FORBIDDEN_ENV_VARS
        assert "TICKETS_CORRELATION_INGRESS_KEY" not in PRODUCER_FORBIDDEN_ENV_VARS

    @pytest.mark.parametrize("forbidden", sorted(BROKER_FORBIDDEN_ENV_VARS))
    def test_a_foreign_secret_fails_broker_validation(self, forbidden):
        broker = EvidenceBrokerSettings(_env_file=None, ENVIRONMENT="local")
        with pytest.raises(ValueError, match=forbidden):
            validate_evidence_broker_settings(broker, env={forbidden: "synthetic-value"})

    @pytest.mark.parametrize("forbidden", sorted(PRODUCER_FORBIDDEN_ENV_VARS))
    def test_a_foreign_secret_fails_producer_validation(self, forbidden):
        producer = ProducerCorrelationSettings(
            _env_file=None,
            CORRELATION_INGRESS_KEY="synthetic-ingress-value",
            CORRELATION_INGRESS_KEY_VERSION=1,
            CORRELATION_LOOKUP_KEY="synthetic-lookup-value",
            CORRELATION_LOOKUP_KEY_VERSION=1,
        )
        with pytest.raises(ValueError, match=forbidden):
            validate_producer_correlation_settings(producer, env={forbidden: "synthetic-value"})


class TestClosedMutationEnvelopes:
    """The master plan's "Closed mutation envelopes" block, frozen here.

    Stage 5's commit allowlist cannot reach this module, so the envelopes must
    exist before the routes that use them.
    """

    ENVELOPES = (
        CreateReviewRequest,
        CreateEvidenceLinkRequest,
        DeleteEvidenceLinkRequest,
        CreateRemediationBatchRequest,
        ClaimBatchResponse,
        HeartbeatBatchRequest,
        MaterializeBatchRequest,
        MaterializeBatchResponse,
        PatchBatchRequest,
        ReleaseBatchRequest,
        ReadyBatchRequest,
        StartVerificationRequest,
        CompleteBatchRequest,
        ExtendLeaseRequest,
        CancelBatchRequest,
        ImportDryRunResponse,
        ImportApplyOrReverseRequest,
        ImportChunkResponse,
        SessionResponse,
        ErrorResponse,
    )

    @pytest.mark.parametrize("envelope", ENVELOPES, ids=lambda e: e.__name__)
    def test_unknown_envelope_fields_fail_validation(self, envelope):
        with pytest.raises(ValueError, match="extra"):
            envelope.model_validate({"definitely_not_a_field": "x"})

    def test_evidence_link_creation_never_accepts_a_caller_chosen_reference(self):
        fields = set(CreateEvidenceLinkRequest.model_fields)
        assert fields == {"broker_candidate_token", "reason"}
        assert "evidence_reference" not in fields
        assert "execution_id" not in fields

    def test_batch_creation_freezes_review_version_pairs(self):
        request = CreateRemediationBatchRequest(
            review_refs=[ReviewRef(review_id="a" * 64, review_version=3)],
            transition_to_planned=True,
        )
        assert request.review_refs[0].review_version == 3
        with pytest.raises(ValueError):
            request.review_refs[0].review_id = "b" * 64

    def test_batch_creation_rejects_an_empty_or_duplicate_set(self):
        with pytest.raises(ValueError):
            CreateRemediationBatchRequest(review_refs=[])
        with pytest.raises(ValueError, match="duplicate"):
            CreateRemediationBatchRequest(
                review_refs=[
                    ReviewRef(review_id="a" * 64, review_version=1),
                    ReviewRef(review_id="a" * 64, review_version=2),
                ]
            )

    def test_batch_creation_is_bounded_by_the_canonical_maximum(self):
        refs = [
            ReviewRef(review_id=f"{i:064x}", review_version=1) for i in range(MAX_BATCH_REVIEWS)
        ]
        assert len(CreateRemediationBatchRequest(review_refs=refs).review_refs) == (
            MAX_BATCH_REVIEWS
        )
        with pytest.raises(ValueError):
            CreateRemediationBatchRequest(
                review_refs=refs + [ReviewRef(review_id="f" * 64, review_version=1)]
            )

    def test_a_lease_token_is_never_serialized_in_the_clear(self):
        response = ClaimBatchResponse(
            batch=_batch(status=BatchStatus.CLAIMED),
            lease_token="synthetic-lease-value",
            lease_expires_at=utc_now(),
        )
        assert "synthetic-lease-value" not in repr(response)
        assert "synthetic-lease-value" not in str(response.model_dump())
        assert "synthetic-lease-value" not in response.model_dump_json()

    def test_release_disposition_is_closed(self):
        for disposition in ("ready", "blocked"):
            ReleaseBatchRequest(
                expected_version=1,
                lease_token="synthetic-lease-value",
                disposition=disposition,
                reason="done",
            )
        for disposition in ("completed", "cancelled", "anything"):
            with pytest.raises(ValueError, match="disposition"):
                ReleaseBatchRequest(
                    expected_version=1,
                    lease_token="synthetic-lease-value",
                    disposition=disposition,
                    reason="done",
                )

    def test_lease_extension_is_capped_at_the_canonical_two_hours(self):
        ExtendLeaseRequest(
            expected_version=1,
            additional_minutes=MAX_LEASE_EXTENSION_MINUTES,
            reason="agent needs one more window",
        )
        with pytest.raises(ValueError):
            ExtendLeaseRequest(
                expected_version=1,
                additional_minutes=MAX_LEASE_EXTENSION_MINUTES + 1,
                reason="unbounded",
            )

    def test_an_import_apply_requires_explicit_approval(self):
        ImportApplyOrReverseRequest(plan_sha256="a" * 64, approval_confirmed=True)
        with pytest.raises(ValueError, match="approval_confirmed"):
            ImportApplyOrReverseRequest(plan_sha256="a" * 64, approval_confirmed=False)

    def test_import_apply_never_accepts_a_raw_offset(self):
        fields = set(ImportApplyOrReverseRequest.model_fields)
        assert "resume_cursor" in fields
        assert "offset" not in fields
        assert "start_row" not in fields

    def test_materialization_defaults_to_excluding_conversation(self):
        request = MaterializeBatchRequest(
            expected_version=1, lease_token="synthetic-lease-value"
        )
        assert request.include_conversation is False

    def test_materialization_declares_drift_and_truncation(self):
        fields = set(MaterializeBatchResponse.model_fields)
        assert {"drifted_review_ids", "partial", "truncated", "warnings"} <= fields

    def test_the_error_envelope_exposes_only_safe_conflict_metadata(self):
        body = ErrorResponse(
            error=ErrorBody(
                code="REVIEW_VERSION_CONFLICT",
                message="The review changed. Reload before saving.",
                request_id="req-1",
                current_version=7,
            )
        )
        payload = body.model_dump(mode="json")
        assert payload["error"]["current_version"] == 7
        assert "comments" not in payload["error"]
        assert "assigned_reviewer" not in payload["error"]

    def test_every_envelope_round_trips_through_json_mode(self):
        for envelope in (
            ReadyBatchRequest(expected_version=1, reason="ready"),
            CancelBatchRequest(expected_version=1, reason="stale"),
            HeartbeatBatchRequest(expected_version=1, lease_token="synthetic-lease-value"),
            ImportChunkResponse(import_id="i-1", status=ImportStatus.APPLIED),
            ImportDryRunResponse(import_id="i-1", file_sha256="a" * 64, plan_sha256="b" * 64),
            StartVerificationRequest(
                expected_version=1, independent_verifier_attestation="I verified independently."
            ),
            CompleteBatchRequest(expected_version=1, decision="accepted"),
        ):
            encoded = json.dumps(envelope.model_dump(mode="json"))
            assert type(envelope).model_validate(json.loads(encoded))


# =====================================================================
# 18. The console's named Firestore databases (Stage 3)
# =====================================================================


class TestNamedFirestoreDatabases:
    """The database, not a collection prefix, is the isolation boundary.

    ``roles/datastore.user`` is database-scoped, so a console revision that can
    reach the wrong database is a privacy incident, not a config typo.
    """

    def test_the_database_names_are_frozen_per_environment(self):
        assert STAGING_FIRESTORE_DATABASE == "tickets-console-staging"
        assert PRODUCTION_FIRESTORE_DATABASE == "tickets-console-prod"
        assert LOCAL_FIRESTORE_DATABASE == "tickets-console-emulator"
        assert EXPECTED_FIRESTORE_DATABASES == {
            "staging": STAGING_FIRESTORE_DATABASE,
            "production": PRODUCTION_FIRESTORE_DATABASE,
        }
        assert set(EXPECTED_FIRESTORE_DATABASES) == STRICT_ENVIRONMENTS

    def test_no_environment_may_fall_back_to_a_default_database(self):
        for environment in sorted(VALID_ENVIRONMENTS):
            with pytest.raises(ValueError, match="explicitly"):
                resolve_tickets_firestore_database("", environment=environment)
            with pytest.raises(ValueError, match="explicitly"):
                resolve_tickets_firestore_database("   ", environment=environment)

    def test_strict_environments_refuse_the_default_database(self):
        for environment in sorted(STRICT_ENVIRONMENTS):
            with pytest.raises(ValueError, match=r"never \(default\)"):
                resolve_tickets_firestore_database(
                    DEFAULT_FIRESTORE_DATABASE, environment=environment
                )

    def test_each_strict_environment_pins_exactly_its_own_database(self):
        for environment, expected in EXPECTED_FIRESTORE_DATABASES.items():
            assert (
                resolve_tickets_firestore_database(expected, environment=environment)
                == expected
            )
            other = next(
                name
                for env, name in EXPECTED_FIRESTORE_DATABASES.items()
                if env != environment
            )
            with pytest.raises(ValueError, match=expected):
                resolve_tickets_firestore_database(other, environment=environment)

    def test_an_unknown_environment_fails_closed(self):
        # "prod" is not "production": a near-miss must never resolve to a
        # permissive path just because it looks like one.
        for environment in ("prod", "dev", "Production", "LOCAL"):
            with pytest.raises(ValueError, match="ENVIRONMENT must be one of"):
                resolve_tickets_firestore_database(
                    STAGING_FIRESTORE_DATABASE, environment=environment
                )

    def test_surrounding_whitespace_is_normalized_not_trusted(self):
        # Cloud Run environment values routinely carry stray whitespace; the
        # normalized value is still checked against the strict rules.
        assert (
            resolve_tickets_firestore_database(
                f" {STAGING_FIRESTORE_DATABASE} ", environment=" staging "
            )
            == STAGING_FIRESTORE_DATABASE
        )
        with pytest.raises(ValueError, match=r"never \(default\)"):
            resolve_tickets_firestore_database(" (default) ", environment=" production ")

    def test_a_blank_environment_falls_back_to_the_fail_closed_one(self):
        # An unset ENVIRONMENT must behave like production, never like local.
        with pytest.raises(ValueError, match=r"never \(default\)"):
            resolve_tickets_firestore_database(DEFAULT_FIRESTORE_DATABASE, environment="")
        assert (
            resolve_tickets_firestore_database(
                EXPECTED_FIRESTORE_DATABASES[FAIL_CLOSED_ENVIRONMENT], environment=""
            )
            == PRODUCTION_FIRESTORE_DATABASE
        )

    def test_local_may_name_any_database_but_must_name_one(self, monkeypatch):
        assert (
            resolve_tickets_firestore_database(
                LOCAL_FIRESTORE_DATABASE, environment="local"
            )
            == LOCAL_FIRESTORE_DATABASE
        )
        # The emulator's own default database is allowed locally only because it
        # was declared explicitly.
        assert (
            resolve_tickets_firestore_database(
                DEFAULT_FIRESTORE_DATABASE, environment="local"
            )
            == DEFAULT_FIRESTORE_DATABASE
        )
        cfg = _console_settings(monkeypatch, FIRESTORE_DATABASE=LOCAL_FIRESTORE_DATABASE)
        assert validate_ticket_console_settings(cfg, env={}) is True

    def test_the_resolver_agrees_with_startup_validation(self, monkeypatch):
        # Both gates must reject the same production database, so a revision
        # cannot start with one and then reach the other.
        cfg = _production_settings(
            monkeypatch, FIRESTORE_DATABASE=DEFAULT_FIRESTORE_DATABASE
        )
        with pytest.raises(ValueError, match=r"never \(default\)"):
            validate_ticket_console_settings(cfg, env={})
        with pytest.raises(ValueError, match=r"never \(default\)"):
            resolve_tickets_firestore_database(
                cfg.FIRESTORE_DATABASE, environment=cfg.ENVIRONMENT
            )

    def test_the_new_config_surface_keeps_the_service_boundary(self):
        # Stage 3 must not smuggle a correlation secret into the console plane.
        assert not any(
            "CORRELATION" in field for field in TicketConsoleSettings.model_fields
        )
        source = Path("api/tickets_console_config.py").read_text()
        assert "from api.config import" not in source
        assert "import api.config" not in source

    def test_the_resolver_never_echoes_a_secret_or_a_value_it_rejects(self):
        for bad in ("super-secret-database-name", "(default)"):
            try:
                resolve_tickets_firestore_database(bad, environment="production")
            except ValueError as error:
                assert "super-secret-database-name" not in str(error)
