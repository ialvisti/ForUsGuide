"""Stage 4 hydration/classification contracts for the /tickets service.

These tests pin the properties that keep the console honest and bounded:

* one detail call is one ``works.get`` plus exactly one timeline page;
* classification comes from configured identities and DevRev actor types, never
  from a display-name string match;
* internal notes are labelled and never folded into the participant reply;
* an unmodelled body type becomes a placeholder, so no remote markup reaches
  the UI;
* a DevRev outage yields an explicit partial result and never erases durable
  review data;
* a suggestion stays a suggestion until a reviewer confirms it against the
  current broker result;
* exact display-id lookup uses a scoped ``works.get`` and never invents a
  ``works.list`` filter that does not exist.

Every identifier is synthetic, and no organization-specific id appears.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from api.ticket_review_models import (
    CacheState,
    ClassifiedTimelinePage,
    CorrelationStatus,
    CorrelationTrust,
    CursorPage,
    DevRevActor,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketFilters,
    DevRevTicketSummary,
    DevRevTimelineEntry,
    EvidenceSourceCollection,
    MessageActorClass,
    MessageClassificationBasis,
    MessageRendering,
    RagEvidenceEnvelope,
    RagEvidenceRecord,
    RagProvenance,
    Rating,
    ResolutionOutcome,
    ReviewerIdentity,
    ReviewerRole,
    ReviewPatch,
    ReviewResolution,
    ReviewStatus,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
    review_id_for_devrev_work,
)
from api.tickets_console_config import (
    DIAGNOSTIC_AUTHOR_ID_OVERLAP,
    DIAGNOSTIC_NO_AI_AUTHOR_IDS,
    DIAGNOSTIC_NO_SYSTEM_AUTHOR_IDS,
    TicketConsoleSettings,
    classification_diagnostics,
    validate_ticket_console_settings,
)
from data_pipeline import ticket_review_provenance as prov
from data_pipeline.devrev_client import (
    DevRevNotFoundError,
    DevRevResourceLimitError,
    DevRevScopeError,
    DevRevTransientError,
)
from data_pipeline.ticket_review_repository import (
    InMemoryTicketReviewBackend,
    MutationContext,
    ReviewListQuery,
    TicketReviewRepository,
)
from data_pipeline.ticket_review_service import (
    CANDIDATE_TOKEN_CONTEXT,
    PLACEHOLDER_NO_BODY,
    PLACEHOLDER_UNSUPPORTED_BODY_TYPE,
    PLACEHOLDER_UNSUPPORTED_ENTRY,
    REASON_NO_DEFENSIBLE_IDENTIFIERS,
    WARNING_BROKER_UNAVAILABLE,
    WARNING_CACHE_DEGRADED,
    WARNING_TICKET_NOT_FOUND,
    WARNING_TIMELINE_UNAVAILABLE,
    EvidenceLinkRejected,
    MessageClassifier,
    TicketNotFound,
    TicketReviewService,
    UnsupportedQuery,
)

# =====================================================================
# Synthetic identities and fixtures
# =====================================================================

SYNTHETIC_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/424242"
SYNTHETIC_DISPLAY_ID = "TKT-424242"
OTHER_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/999999"

# Synthetic DevRev identities. Note the deliberately misleading display names:
# the AI agent is called "Ana Reviewer" and the human is called "Support Bot",
# so any test that passes by matching names would fail here.
AI_AGENT_ID = "don:identity:dvrv-us-1:devo/synthetic:devu/ai-1"
SYSTEM_ACTOR_ID = "don:identity:dvrv-us-1:devo/synthetic:sysu/sys-1"
HUMAN_AGENT_ID = "don:identity:dvrv-us-1:devo/synthetic:devu/human-1"
PARTICIPANT_ID = "don:identity:dvrv-us-1:devo/synthetic:revu/participant-1"
UNKNOWN_ACTOR_ID = "don:identity:dvrv-us-1:devo/synthetic:devu/unlisted-1"

CANDIDATE_KEY = bytes(range(32))
T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

ACTOR = ReviewerIdentity(
    subject="accounts.google.com:synthetic-reviewer",
    email="reviewer@example.invalid",
    display_name="Reviewer",
)
OTHER_ACTOR = ReviewerIdentity(
    subject="accounts.google.com:synthetic-other",
    email="other@example.invalid",
)


class _Clock:
    """A frozen clock for the service, so candidate expiry is exact."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class _TickClock:
    """A clock that advances one microsecond per read.

    The repository stamps every audit event with its own clock and the ledger is
    ordered by ``(occurred_at_unix_us, event_id)``. Two writes frozen at the
    identical microsecond therefore have no defined order, and the hash chain
    would appear broken for a reason that cannot happen against real wall time.
    Advancing here models distinct instants without making the service's
    candidate-expiry assertions non-deterministic.
    """

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        current = self.now
        self.now = self.now + timedelta(microseconds=1)
        return current


def _context(actor: ReviewerIdentity = ACTOR, role=ReviewerRole.REVIEWER, **kwargs):
    return MutationContext(actor=actor, actor_role=role, **kwargs)


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


def _entry(
    entry_id: str,
    *,
    kind: TimelineEntryKind = TimelineEntryKind.COMMENT,
    visibility: TimelineVisibility = TimelineVisibility.EXTERNAL,
    author_id: str | None = PARTICIPANT_ID,
    actor_type: DevRevActorType = DevRevActorType.REV_USER,
    display_name: str | None = None,
    body: str | None = "A synthetic message.",
    body_type: str | None = "text/plain",
    created_at: datetime | None = None,
    object_id: str = SYNTHETIC_DON,
    **overrides,
) -> DevRevTimelineEntry:
    author = (
        None
        if author_id is None
        else DevRevActor(
            actor_id=author_id, actor_type=actor_type, display_name=display_name
        )
    )
    values = {
        "entry_id": entry_id,
        "object_id": object_id,
        "kind": kind,
        "visibility": visibility,
        "body": body,
        "body_type": body_type,
        "author": author,
        "created_at": created_at or T0,
    }
    values.update(overrides)
    return DevRevTimelineEntry(**values)


class _FakeDevRev:
    """Records every call, so "exactly one page" is provable, not assumed."""

    def __init__(
        self,
        *,
        detail: DevRevTicketDetail | None = None,
        pages: list[TimelinePage] | None = None,
        list_page: CursorPage | None = None,
        get_error: Exception | None = None,
        timeline_error: Exception | None = None,
    ) -> None:
        self._detail = detail if detail is not None else _detail()
        self._pages = pages or []
        self._list_page = list_page
        self._get_error = get_error
        self._timeline_error = timeline_error
        self.get_calls: list[str] = []
        self.timeline_calls: list[tuple[str, str | None, int | None]] = []
        self.list_calls: list[tuple[dict, str | None, str, int | None]] = []

    async def get_ticket(self, work_id: str) -> DevRevTicketDetail:
        self.get_calls.append(work_id)
        if self._get_error is not None:
            raise self._get_error
        # Answer for the ticket actually requested, so a test that resolves two
        # different DONs really gets two different reviews.
        if work_id.startswith("don:") and work_id != self._detail.devrev_work_id:
            return self._detail.model_copy(
                update={
                    "devrev_work_id": work_id,
                    "devrev_display_id": f"TKT-{work_id.rsplit('/', 1)[-1]}",
                }
            )
        return self._detail

    async def list_timeline_page(self, object_id, *, cursor=None, limit=None) -> TimelinePage:
        self.timeline_calls.append((object_id, cursor, limit))
        if self._timeline_error is not None:
            raise self._timeline_error
        index = len(self.timeline_calls) - 1
        if index < len(self._pages):
            return self._pages[index]
        return TimelinePage(items=[], page_size=50)

    async def list_tickets(self, filters, *, cursor=None, mode="after", limit=None):
        self.list_calls.append(
            (
                filters.model_dump() if hasattr(filters, "model_dump") else dict(filters),
                cursor,
                mode,
                limit,
            )
        )
        if self._list_page is not None:
            return self._list_page
        return CursorPage[DevRevTicketSummary](items=[_summary()], page_size=50)


class _FakeBroker:
    def __init__(self, envelope: RagEvidenceEnvelope | None = None, error: Exception | None = None):
        self.envelope = envelope
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def lookup(self, devrev_work_id: str, *, max_results: int) -> RagEvidenceEnvelope:
        self.calls.append((devrev_work_id, max_results))
        if self.error is not None:
            raise self.error
        if self.envelope is None:
            return RagEvidenceEnvelope(
                result_digest=prov.envelope_result_digest([]),
                unavailable_reason=REASON_NO_DEFENSIBLE_IDENTIFIERS,
            )
        return self.envelope


def _record(
    reference: str = "synthetic-evidence-1",
    *,
    trust: CorrelationTrust = CorrelationTrust.VERIFIED_WORKLOAD,
    model: str = "synthetic-model",
) -> RagEvidenceRecord:
    record = RagEvidenceRecord(
        evidence_reference=reference,
        evidence_digest="0" * 64,
        source_collection=EvidenceSourceCollection.EXECUTION_LOGS,
        schema_version=1,
        correlation_trust=trust,
        correlation_source=prov.CORRELATION_SOURCE_N8N_SIGNED,
        lookup_key_version=7,
        model=model,
        provenance=RagProvenance(
            correlation_status=(
                CorrelationStatus.LINKED
                if trust is CorrelationTrust.VERIFIED_WORKLOAD
                else CorrelationStatus.UNAVAILABLE
            ),
            correlation_trust=trust,
        ),
    )
    return record.model_copy(update={"evidence_digest": prov.record_content_digest(record)})


def _envelope(*records: RagEvidenceRecord, **overrides) -> RagEvidenceEnvelope:
    linked = any(r.correlation_trust is CorrelationTrust.VERIFIED_WORKLOAD for r in records)
    values = {
        "correlation_status": (
            CorrelationStatus.LINKED if linked and records else CorrelationStatus.UNAVAILABLE
        ),
        "records": list(records),
        "result_digest": prov.envelope_result_digest(r.evidence_reference for r in records),
        "key_versions_queried": [7],
        "unavailable_reason": None if (linked and records) else REASON_NO_DEFENSIBLE_IDENTIFIERS,
    }
    values.update(overrides)
    return RagEvidenceEnvelope(**values)


def _classifier(**overrides) -> MessageClassifier:
    values = {
        "ai_author_ids": [AI_AGENT_ID],
        "system_author_ids": [SYSTEM_ACTOR_ID],
        "human_author_ids": [HUMAN_AGENT_ID],
    }
    values.update(overrides)
    return MessageClassifier(**values)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def repo() -> TicketReviewRepository:
    return TicketReviewRepository(
        InMemoryTicketReviewBackend(),
        cursor_key=bytes(range(32, 64)),
        clock=_TickClock(),
    )


def _service(devrev, repo, clock, *, broker=None, classifier=None, **kwargs):
    return TicketReviewService(
        devrev=devrev,
        repository=repo,
        classifier=classifier or _classifier(),
        candidate_key=CANDIDATE_KEY,
        broker=broker,
        clock=clock,
        **kwargs,
    )


# =====================================================================
# 1-2. Bounded, explicit pagination
# =====================================================================


class TestBoundedHydration:
    async def test_one_detail_call_is_one_works_get_plus_one_timeline_page(
        self, repo, clock
    ):
        devrev = _FakeDevRev(
            pages=[
                TimelinePage(items=[_entry("e1")], next_cursor="cursor-2", page_size=50),
                TimelinePage(items=[_entry("e2")], page_size=50),
            ]
        )
        service = _service(devrev, repo, clock)

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert len(devrev.get_calls) == 1
        assert len(devrev.timeline_calls) == 1
        assert envelope.timeline is not None
        # The next page exists and is advertised, but was NOT fetched.
        assert envelope.timeline.next_cursor == "cursor-2"
        assert [m.entry_id for m in envelope.timeline.messages] == ["e1"]

    async def test_following_the_cursor_is_an_explicit_second_call(self, repo, clock):
        devrev = _FakeDevRev(
            pages=[
                TimelinePage(items=[_entry("e1")], next_cursor="cursor-2", page_size=50),
                TimelinePage(items=[_entry("e2")], page_size=50),
            ]
        )
        service = _service(devrev, repo, clock)

        first = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)
        second = await service.get_timeline_page(
            SYNTHETIC_DON, first.timeline.next_cursor
        )

        assert [m.entry_id for m in second.messages] == ["e2"]
        assert second.next_cursor is None
        assert devrev.timeline_calls[1][1] == "cursor-2"

    async def test_the_timeline_is_always_requested_with_the_don_not_the_display_id(
        self, repo, clock
    ):
        # Passing a display id to timeline-entries.list silently drops every
        # entry, because DevRev returns the DON in `object`.
        devrev = _FakeDevRev(pages=[TimelinePage(items=[_entry("e1")], page_size=50)])
        service = _service(devrev, repo, clock)

        await service.get_timeline_page(SYNTHETIC_DISPLAY_ID)

        assert devrev.get_calls == [SYNTHETIC_DISPLAY_ID]
        assert devrev.timeline_calls[0][0] == SYNTHETIC_DON

    async def test_a_page_preserves_source_order_and_carries_every_flag(self, repo, clock):
        # Deliberately out of chronological order: one page must be shown in the
        # order DevRev returned it, not silently re-sorted.
        newest = _entry("e-newest", created_at=T0)
        oldest = _entry("e-oldest", created_at=T0 - timedelta(days=1))
        devrev = _FakeDevRev(
            pages=[
                TimelinePage(
                    items=[newest, oldest],
                    next_cursor="cursor-2",
                    page_size=50,
                    partial=True,
                    truncated=True,
                    warnings=["body_truncated"],
                )
            ]
        )
        service = _service(devrev, repo, clock)

        page = await service.get_timeline_page(SYNTHETIC_DON)

        assert [e.entry_id for e in page.items] == ["e-newest", "e-oldest"]
        assert [m.entry_id for m in page.messages] == ["e-newest", "e-oldest"]
        assert page.next_cursor == "cursor-2"
        assert page.truncated is True
        assert page.partial is True
        assert "body_truncated" in page.warnings

    def test_a_classified_page_needs_one_message_per_entry(self):
        with pytest.raises(Exception):
            ClassifiedTimelinePage(items=[_entry("e1"), _entry("e2")], messages=[], page_size=50)

    async def test_a_list_page_never_fetches_a_timeline_per_row(self, repo, clock):
        rows = [_summary(f"{SYNTHETIC_DON}-{i}", f"TKT-{i}") for i in range(10)]
        devrev = _FakeDevRev(
            list_page=CursorPage[DevRevTicketSummary](items=rows, page_size=50)
        )
        service = _service(devrev, repo, clock)

        page = await service.list_live_tickets(DevRevTicketFilters())

        assert len(page.items) == 10
        assert devrev.timeline_calls == []
        assert devrev.get_calls == []


# =====================================================================
# 3-5. Classification by identity and type, never by name
# =====================================================================


class TestClassification:
    @pytest.mark.parametrize(
        ("author_id", "actor_type", "display_name", "expected_class", "expected_basis"),
        [
            (
                AI_AGENT_ID,
                DevRevActorType.DEV_USER,
                "Ana Reviewer",
                MessageActorClass.AI_OR_SYSTEM,
                MessageClassificationBasis.CONFIGURED_AI_AUTHOR_ID,
            ),
            (
                SYSTEM_ACTOR_ID,
                DevRevActorType.SYS_USER,
                "Ana Reviewer",
                MessageActorClass.AI_OR_SYSTEM,
                MessageClassificationBasis.CONFIGURED_SYSTEM_AUTHOR_ID,
            ),
            (
                HUMAN_AGENT_ID,
                DevRevActorType.DEV_USER,
                "Support Bot",
                MessageActorClass.HUMAN_AGENT,
                MessageClassificationBasis.CONFIGURED_HUMAN_AUTHOR_ID,
            ),
            (
                PARTICIPANT_ID,
                DevRevActorType.REV_USER,
                "Support Bot",
                MessageActorClass.PARTICIPANT,
                MessageClassificationBasis.EXTERNAL_ACTOR_TYPE,
            ),
            (
                UNKNOWN_ACTOR_ID,
                DevRevActorType.DEV_USER,
                "Ana Reviewer",
                MessageActorClass.HUMAN_AGENT,
                MessageClassificationBasis.INTERNAL_ACTOR_TYPE,
            ),
            (
                UNKNOWN_ACTOR_ID,
                DevRevActorType.UNKNOWN,
                "Ana Reviewer",
                MessageActorClass.UNKNOWN,
                MessageClassificationBasis.AMBIGUOUS,
            ),
        ],
    )
    def test_actor_classes_come_from_ids_and_types(
        self, author_id, actor_type, display_name, expected_class, expected_basis
    ):
        message = _classifier().normalize(
            _entry("e1", author_id=author_id, actor_type=actor_type, display_name=display_name)
        )

        assert message.actor_class is expected_class
        assert message.basis is expected_basis

    def test_a_display_name_alone_never_decides_a_class(self):
        classifier = _classifier()

        # Same misleading name, three different identities, three different
        # classes. A name-based implementation could not produce this.
        classes = {
            classifier.classify(
                _entry(
                    "e",
                    author_id=author_id,
                    actor_type=actor_type,
                    display_name="Support Bot",
                )
            )[0]
            for author_id, actor_type in (
                (AI_AGENT_ID, DevRevActorType.DEV_USER),
                (HUMAN_AGENT_ID, DevRevActorType.DEV_USER),
                (PARTICIPANT_ID, DevRevActorType.REV_USER),
            )
        }
        assert classes == {
            MessageActorClass.AI_OR_SYSTEM,
            MessageActorClass.HUMAN_AGENT,
            MessageActorClass.PARTICIPANT,
        }

    def test_a_change_event_is_an_event_and_never_an_authored_reply(self):
        message = _classifier().normalize(
            _entry(
                "e1",
                kind=TimelineEntryKind.CHANGE_EVENT,
                author_id=None,
                body=None,
                body_type=None,
                visibility=TimelineVisibility.INTERNAL,
                change_summary="stage changed",
            )
        )

        assert message.actor_class is MessageActorClass.EVENT
        assert message.basis is MessageClassificationBasis.CHANGE_EVENT
        assert message.participant_facing is False
        assert message.rendering is MessageRendering.PLACEHOLDER
        assert message.body is None

    def test_configured_ids_take_precedence_over_an_ambiguous_actor_type(self):
        # The AI agent is provisioned as a dev_user, which is the common case.
        # Type-first ordering would mislabel it a human agent.
        message = _classifier().normalize(
            _entry("e1", author_id=AI_AGENT_ID, actor_type=DevRevActorType.DEV_USER)
        )

        assert message.actor_class is MessageActorClass.AI_OR_SYSTEM

    def test_empty_ai_and_system_configuration_yields_unknown_plus_a_warning(self):
        classifier = MessageClassifier(
            diagnostics=[DIAGNOSTIC_NO_AI_AUTHOR_IDS, DIAGNOSTIC_NO_SYSTEM_AUTHOR_IDS]
        )

        message = classifier.normalize(
            _entry("e1", author_id=UNKNOWN_ACTOR_ID, actor_type=DevRevActorType.DEV_USER)
        )

        assert message.actor_class is MessageActorClass.UNKNOWN
        assert message.basis is MessageClassificationBasis.AMBIGUOUS
        assert DIAGNOSTIC_NO_AI_AUTHOR_IDS in classifier.diagnostics
        assert DIAGNOSTIC_NO_SYSTEM_AUTHOR_IDS in classifier.diagnostics

    async def test_operator_diagnostics_reach_the_detail_envelope(self, repo, clock):
        devrev = _FakeDevRev(pages=[TimelinePage(items=[_entry("e1")], page_size=50)])
        service = _service(
            devrev,
            repo,
            clock,
            classifier=MessageClassifier(diagnostics=[DIAGNOSTIC_NO_AI_AUTHOR_IDS]),
        )

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert DIAGNOSTIC_NO_AI_AUTHOR_IDS in envelope.diagnostics
        assert DIAGNOSTIC_NO_AI_AUTHOR_IDS in envelope.timeline.diagnostics


class TestClassificationConfiguration:
    def test_author_ids_reject_a_display_name_or_an_email(self, monkeypatch):
        for hostile in (["Support Bot"], ["ana@example.invalid"], [""], ["  "]):
            settings = TicketConsoleSettings(
                _env_file=None,
                ENVIRONMENT="local",
                AUTH_MODE="local",
                ALLOW_LOCAL_AUTH=True,
                DEVREV_AI_AUTHOR_IDS=hostile,
            )
            with pytest.raises(ValueError) as excinfo:
                validate_ticket_console_settings(settings, env={})
            assert "DEVREV_AI_AUTHOR_IDS" in str(excinfo.value)

    def test_a_don_or_opaque_object_id_is_accepted(self):
        settings = TicketConsoleSettings(
            _env_file=None,
            ENVIRONMENT="local",
            AUTH_MODE="local",
            ALLOW_LOCAL_AUTH=True,
            DEVREV_AI_AUTHOR_IDS=[AI_AGENT_ID],
            DEVREV_SYSTEM_AUTHOR_IDS=["SYSU-1234"],
            DEVREV_HUMAN_AUTHOR_IDS=[HUMAN_AGENT_ID],
        )

        assert validate_ticket_console_settings(settings, env={}) is True

    def test_a_rejection_never_echoes_the_configured_value(self):
        settings = TicketConsoleSettings(
            _env_file=None,
            ENVIRONMENT="local",
            AUTH_MODE="local",
            ALLOW_LOCAL_AUTH=True,
            DEVREV_AI_AUTHOR_IDS=["Ana Real Name"],
        )

        with pytest.raises(ValueError) as excinfo:
            validate_ticket_console_settings(settings, env={})

        assert "Ana Real Name" not in str(excinfo.value)

    def test_empty_configuration_is_a_diagnostic_not_a_startup_failure(self):
        settings = TicketConsoleSettings(
            _env_file=None,
            ENVIRONMENT="local",
            AUTH_MODE="local",
            ALLOW_LOCAL_AUTH=True,
        )

        assert validate_ticket_console_settings(settings, env={}) is True
        assert set(classification_diagnostics(settings)) == {
            DIAGNOSTIC_NO_AI_AUTHOR_IDS,
            DIAGNOSTIC_NO_SYSTEM_AUTHOR_IDS,
        }

    def test_an_identity_in_two_sets_is_reported_rather_than_silently_ranked(self):
        settings = TicketConsoleSettings(
            _env_file=None,
            ENVIRONMENT="local",
            AUTH_MODE="local",
            ALLOW_LOCAL_AUTH=True,
            DEVREV_AI_AUTHOR_IDS=[AI_AGENT_ID],
            DEVREV_SYSTEM_AUTHOR_IDS=[SYSTEM_ACTOR_ID],
            DEVREV_HUMAN_AUTHOR_IDS=[AI_AGENT_ID],
        )

        assert DIAGNOSTIC_AUTHOR_ID_OVERLAP in classification_diagnostics(settings)

    def test_no_organization_specific_identifier_is_hardcoded(self):
        import pathlib
        import re

        # Matches a real DON literal, not prose or a guard that merely mentions
        # the prefix.
        don_literal = re.compile(r"don:[a-z_]+:[A-Za-z0-9_.-]+:devo/([A-Za-z0-9_.<>-]+)")

        for path in (
            "data_pipeline/ticket_review_service.py",
            "data_pipeline/ticket_review_provenance.py",
            "data_pipeline/ticket_evidence_broker.py",
            "api/tickets_console_config.py",
            "tests/test_ticket_review_service.py",
            "tests/test_ticket_review_provenance.py",
            "tests/test_ticket_evidence_broker.py",
        ):
            for org in don_literal.findall(pathlib.Path(path).read_text()):
                # Every DON in this codebase must be an obvious synthetic
                # fixture or a documentation placeholder.
                assert org in {"synthetic", "<org>"}, (path, org)


# =====================================================================
# 6-7. Internal notes stay internal; unknown bodies become placeholders
# =====================================================================


class TestRenderingSafety:
    @pytest.mark.parametrize(
        ("visibility", "internal", "participant_facing"),
        [
            (TimelineVisibility.EXTERNAL, False, True),
            (TimelineVisibility.PUBLIC, False, True),
            (TimelineVisibility.INTERNAL, True, False),
            (TimelineVisibility.PRIVATE, True, False),
        ],
    )
    def test_internal_and_private_entries_are_labelled_and_never_participant_facing(
        self, visibility, internal, participant_facing
    ):
        message = _classifier().normalize(_entry("e1", visibility=visibility))

        assert message.internal is internal
        assert message.participant_facing is participant_facing
        assert message.visibility is visibility

    def test_the_participant_reply_never_contains_an_internal_note(self):
        classifier = _classifier()
        entries = [
            _entry("public-1", visibility=TimelineVisibility.EXTERNAL, body="Visible reply."),
            _entry(
                "internal-1",
                visibility=TimelineVisibility.INTERNAL,
                author_id=HUMAN_AGENT_ID,
                actor_type=DevRevActorType.DEV_USER,
                body="Internal: do not tell the participant.",
            ),
        ]

        messages = [classifier.normalize(entry) for entry in entries]
        reply = "\n".join(m.body or "" for m in messages if m.participant_facing)

        assert "Visible reply." in reply
        assert "do not tell the participant" not in reply
        # The internal note is still present and labelled, not discarded.
        assert messages[1].internal is True
        assert messages[1].body == "Internal: do not tell the participant."

    @pytest.mark.parametrize(
        "body_type", ["text/html", "html", "markdown", "text/markdown", "application/json", "???"]
    )
    def test_an_unmodelled_body_type_renders_a_placeholder_with_metadata(self, body_type):
        markup = '<script>alert(1)</script><b>bold</b>'
        message = _classifier().normalize(
            _entry("e1", body=markup, body_type=body_type)
        )

        assert message.rendering is MessageRendering.PLACEHOLDER
        assert message.placeholder_reason == PLACEHOLDER_UNSUPPORTED_BODY_TYPE
        # No raw markup crosses to the UI, but the metadata survives.
        assert message.body is None
        assert message.body_type == body_type
        assert message.body_length == len(markup)

    @pytest.mark.parametrize("body_type", ["text/plain", "text", "plain", "plaintext", None, ""])
    def test_a_plain_text_body_renders_as_text(self, body_type):
        message = _classifier().normalize(
            _entry("e1", body="Plain reply.", body_type=body_type)
        )

        assert message.rendering is MessageRendering.TEXT
        assert message.body == "Plain reply."
        assert message.placeholder_reason is None

    def test_an_unsupported_entry_type_is_preserved_as_a_placeholder(self):
        message = _classifier().normalize(
            _entry(
                "e1",
                kind=TimelineEntryKind.UNSUPPORTED,
                unsupported_type="timeline_something_new",
                body=None,
                body_type=None,
            )
        )

        assert message.rendering is MessageRendering.PLACEHOLDER
        assert message.placeholder_reason == PLACEHOLDER_UNSUPPORTED_ENTRY
        assert message.unsupported_type == "timeline_something_new"
        assert message.participant_facing is False

    def test_an_empty_body_is_a_placeholder_rather_than_an_empty_reply(self):
        message = _classifier().normalize(_entry("e1", body=None))

        assert message.rendering is MessageRendering.PLACEHOLDER
        assert message.placeholder_reason == PLACEHOLDER_NO_BODY


# =====================================================================
# 8-9. The bounded message cache never touches the durable review
# =====================================================================


class TestMessageCache:
    async def test_caching_a_page_does_not_alter_the_durable_review(self, repo, clock):
        devrev = _FakeDevRev(pages=[TimelinePage(items=[_entry("e1")], page_size=50)])
        service = _service(devrev, repo, clock)
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())
        patched = await service.patch_review(
            review.review_id,
            ReviewPatch(rating=Rating(2), comments="reviewer observation"),
            review.version,
            ACTOR,
            _context(),
        )

        await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)
        after = await repo.get_review(review.review_id)

        assert after.rating == patched.rating
        assert after.comments == "reviewer observation"
        assert after.version == patched.version
        # And the cache really was written.
        cached = await repo.get_message_cache_entry("e1")
        assert cached is not None
        assert cached.body == "A synthetic message."

    async def test_an_older_snapshot_does_not_overwrite_newer_cache_data(self, repo, clock):
        newer = _entry("e1", body="newer body", modified_at=T0)
        older = _entry("e1", body="older body", modified_at=T0 - timedelta(days=1))

        devrev = _FakeDevRev(
            detail=_detail(object_version=5),
            pages=[TimelinePage(items=[newer], page_size=50)],
        )
        await _service(devrev, repo, clock).get_timeline_page(SYNTHETIC_DON)

        stale = _FakeDevRev(
            detail=_detail(object_version=1),
            pages=[TimelinePage(items=[older], page_size=50)],
        )
        await _service(stale, repo, clock).get_timeline_page(SYNTHETIC_DON)

        cached = await repo.get_message_cache_entry("e1")
        assert cached.body == "newer body"

    async def test_a_failed_cache_write_degrades_but_never_fails_the_read(
        self, repo, clock
    ):
        devrev = _FakeDevRev(pages=[TimelinePage(items=[_entry("e1")], page_size=50)])
        service = _service(devrev, repo, clock)

        async def _explode(entry):
            raise RuntimeError("firestore is unavailable")

        repo.upsert_message_cache_entry = _explode  # type: ignore[assignment]

        page = await service.get_timeline_page(SYNTHETIC_DON)

        # The read still succeeded in full…
        assert [m.entry_id for m in page.messages] == ["e1"]
        # …and the degradation is reported rather than swallowed.
        assert page.cache_state is CacheState.DEGRADED
        assert WARNING_CACHE_DEGRADED in page.warnings


# =====================================================================
# 10-12. Partial failure never erases or fabricates a review
# =====================================================================


class TestPartialFailure:
    async def test_a_missing_devrev_detail_returns_a_partial_result_with_the_review(
        self, repo, clock
    ):
        service = _service(_FakeDevRev(), repo, clock)
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())
        await service.patch_review(
            review.review_id,
            ReviewPatch(rating=Rating(1), comments="the answer was wrong"),
            review.version,
            ACTOR,
            _context(),
        )

        broken = _FakeDevRev(get_error=DevRevNotFoundError("gone"))
        envelope = await _service(broken, repo, clock).get_ticket_detail(
            SYNTHETIC_DISPLAY_ID, ACTOR
        )

        assert envelope.partial is True
        assert WARNING_TICKET_NOT_FOUND in envelope.warnings
        assert envelope.ticket is None
        # The durable work survives the remote failure intact.
        assert envelope.review is not None
        assert envelope.review.comments == "the answer was wrong"
        assert envelope.review.rating == Rating(1)

    async def test_a_missing_timeline_is_partial_and_does_not_lose_the_ticket(
        self, repo, clock
    ):
        devrev = _FakeDevRev(timeline_error=DevRevTransientError("upstream 503"))
        envelope = await _service(devrev, repo, clock).get_ticket_detail(
            SYNTHETIC_DON, ACTOR
        )

        assert envelope.partial is True
        assert WARNING_TIMELINE_UNAVAILABLE in envelope.warnings
        assert envelope.ticket is not None
        assert envelope.timeline is None

    async def test_a_guard_limit_is_a_typed_partial_result_not_a_crash(self, repo, clock):
        devrev = _FakeDevRev(
            timeline_error=DevRevResourceLimitError("max pages guard reached")
        )
        envelope = await _service(devrev, repo, clock).get_ticket_detail(
            SYNTHETIC_DON, ACTOR
        )

        assert envelope.partial is True
        assert envelope.timeline is None
        assert WARNING_TIMELINE_UNAVAILABLE in envelope.warnings

    async def test_a_ticket_with_neither_live_data_nor_a_review_is_not_found(
        self, repo, clock
    ):
        devrev = _FakeDevRev(get_error=DevRevScopeError("out of scope"))

        with pytest.raises(TicketNotFound):
            await _service(devrev, repo, clock).get_ticket_detail(SYNTHETIC_DON, ACTOR)

    async def test_a_remote_read_never_overwrites_rating_comments_or_status(
        self, repo, clock
    ):
        service = _service(_FakeDevRev(), repo, clock)
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())
        judged = await service.patch_review(
            review.review_id,
            ReviewPatch(
                rating=Rating(2),
                comments="reviewer judgment",
                status=ReviewStatus.REVIEWED,
            ),
            review.version,
            ACTOR,
            _context(),
        )

        # A later live read reports a *changed* remote ticket.
        moved = _FakeDevRev(
            detail=_detail(object_version=99, title="A completely different title")
        )
        envelope = await _service(moved, repo, clock).get_ticket_detail(
            SYNTHETIC_DON, ACTOR
        )

        assert envelope.review.rating == judged.rating
        assert envelope.review.comments == "reviewer judgment"
        assert envelope.review.status is ReviewStatus.REVIEWED
        assert envelope.review.version == judged.version
        # The live overlay is visible on the ticket, not merged into the review.
        assert envelope.ticket.object_version == 99

    async def test_a_live_ticket_with_no_review_returns_review_none(self, repo, clock):
        envelope = await _service(_FakeDevRev(), repo, clock).get_ticket_detail(
            SYNTHETIC_DON, ACTOR
        )

        assert envelope.ticket is not None
        # Explicitly absent, never a fabricated unreviewed document.
        assert envelope.review is None
        assert envelope.partial is False

    async def test_a_list_row_without_a_review_carries_review_none(self, repo, clock):
        page = await _service(_FakeDevRev(), repo, clock).list_live_tickets(
            DevRevTicketFilters()
        )

        assert page.items[0].review is None

    async def test_a_review_store_outage_still_shows_the_live_tickets(self, repo, clock):
        service = _service(_FakeDevRev(), repo, clock)

        async def _explode(review_id):
            raise RuntimeError("firestore is unavailable")

        repo.get_review = _explode  # type: ignore[assignment]

        page = await service.list_live_tickets(DevRevTicketFilters())

        assert len(page.items) == 1
        assert page.items[0].review is None
        assert page.partial is True


# =====================================================================
# 13-14. A gap is a gap; similarity is a suggestion
# =====================================================================


class TestCorrelationHonesty:
    async def test_a_historical_ticket_with_no_identifiers_is_unavailable(
        self, repo, clock
    ):
        service = _service(_FakeDevRev(), repo, clock, broker=_FakeBroker())

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert envelope.evidence.correlation_status is CorrelationStatus.UNAVAILABLE
        assert envelope.evidence.unavailable_reason == REASON_NO_DEFENSIBLE_IDENTIFIERS
        assert envelope.evidence.candidate_links == []

    async def test_a_verified_producer_record_is_reported_as_linked(self, repo, clock):
        broker = _FakeBroker(_envelope(_record()))
        service = _service(_FakeDevRev(), repo, clock, broker=broker)

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert envelope.evidence.correlation_status is CorrelationStatus.LINKED
        assert envelope.evidence.correlation_trust is CorrelationTrust.VERIFIED_WORKLOAD
        assert envelope.evidence.linked_count == 1
        assert envelope.evidence.result_digest == prov.envelope_result_digest(
            ["synthetic-evidence-1"]
        )
        assert envelope.evidence.key_versions_queried == [7]
        assert envelope.evidence.truncated is False
        # A link and a suggestion are mutually exclusive.
        assert envelope.evidence.candidate_links == []

    async def test_broker_envelope_metadata_is_not_discarded(self, repo, clock):
        broker = _FakeBroker(
            _envelope(_record(), key_versions_queried=[7, 8], truncated=True)
        )
        service = _service(_FakeDevRev(), repo, clock, broker=broker)

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert envelope.evidence.key_versions_queried == [7, 8]
        assert envelope.evidence.truncated is True

    async def test_an_unverified_record_becomes_a_candidate_and_never_linked(
        self, repo, clock
    ):
        service = _service(
            _FakeDevRev(),
            repo,
            clock,
            broker=_FakeBroker(_envelope(_record(trust=CorrelationTrust.CANDIDATE))),
        )
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert envelope.evidence.correlation_status is CorrelationStatus.UNAVAILABLE
        assert len(envelope.evidence.candidate_links) == 1
        candidate = envelope.evidence.candidate_links[0]
        assert candidate.correlation_trust is CorrelationTrust.CANDIDATE
        # Nothing was stored.
        assert (await repo.get_review(review.review_id)).correlation_status is (
            CorrelationStatus.UNAVAILABLE
        )

    async def test_a_broker_outage_is_a_gap_not_a_five_hundred(self, repo, clock):
        service = _service(
            _FakeDevRev(), repo, clock, broker=_FakeBroker(error=RuntimeError("down"))
        )

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert envelope.evidence.correlation_status is CorrelationStatus.UNAVAILABLE
        assert WARNING_BROKER_UNAVAILABLE in envelope.warnings


# =====================================================================
# 15-17. Candidate tokens and explicit manual linking
# =====================================================================


class TestManualEvidenceLinking:
    async def _prepare(self, repo, clock, *, trust=CorrelationTrust.CANDIDATE):
        broker = _FakeBroker(_envelope(_record(trust=trust)))
        service = _service(_FakeDevRev(), repo, clock, broker=broker)
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())
        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)
        return service, broker, review, envelope.evidence.candidate_links[0]

    async def test_a_candidate_token_binds_ticket_review_actor_digest_and_expiry(
        self, repo, clock
    ):
        _service_, _broker, review, candidate = await self._prepare(repo, clock)

        from api.ticket_review_models import open_cursor

        payload = open_cursor(
            CANDIDATE_KEY, candidate.candidate_token,
            context=CANDIDATE_TOKEN_CONTEXT, now=clock(),
        )

        assert payload["w"] == SYNTHETIC_DON
        assert payload["r"] == review.review_id
        assert payload["s"] == ACTOR.subject
        assert payload["e"] == candidate.evidence_reference
        assert payload["d"] == candidate.evidence_digest
        assert payload["b"] == candidate.broker_result_digest
        assert candidate.expires_at == clock() + timedelta(minutes=10)
        # The token is opaque: it is not a caller-editable internal reference.
        assert review.review_id not in candidate.candidate_token
        assert SYNTHETIC_DON not in candidate.candidate_token

    async def test_a_valid_link_makes_the_correlation_manual_with_an_audit_event(
        self, repo, clock
    ):
        service, _broker, review, candidate = await self._prepare(repo, clock)

        linked = await service.create_evidence_link(
            review.review_id,
            candidate_token=candidate.candidate_token,
            reason="the retrieved chunk was the wrong article",
            expected_version=review.version,
            actor=ACTOR,
            request_context=_context(idempotency_key="synthetic-idem-1"),
        )

        assert linked.correlation_status is CorrelationStatus.MANUAL
        assert linked.correlation_trust is CorrelationTrust.MANUAL_REVIEWER
        assert linked.correlation_source == "reviewer_evidence_link"

        links = await repo.list_evidence_links(review.review_id)
        assert len(links.items) == 1
        assert links.items[0].reason == "the retrieved chunk was the wrong article"
        assert links.items[0].linked_by.subject == ACTOR.subject

        events = await repo.list_audit_events(review.review_id, page_size=50)
        types = [event.event_type for event in events.items]
        assert "evidence_linked" in types
        assert "correlation_status_changed" in types
        chain = await repo.verify_audit_chain(review.review_id)
        assert chain.intact is True

    @pytest.mark.parametrize("mutation", ["tampered", "nonexistent", "wrong_context"])
    async def test_a_tampered_or_nonexistent_token_is_refused(
        self, repo, clock, mutation
    ):
        service, _broker, review, candidate = await self._prepare(repo, clock)
        from api.ticket_review_models import seal_cursor

        if mutation == "tampered":
            token = candidate.candidate_token[:-2] + (
                "AA" if not candidate.candidate_token.endswith("AA") else "BB"
            )
        elif mutation == "nonexistent":
            token = base64.urlsafe_b64encode(b"x" * 40).decode().rstrip("=")
        else:
            token = seal_cursor(
                CANDIDATE_KEY,
                {"w": SYNTHETIC_DON, "r": review.review_id, "s": ACTOR.subject,
                 "e": candidate.evidence_reference, "d": candidate.evidence_digest,
                 "b": candidate.broker_result_digest},
                context="tickets:review-list:v1",
                now=clock(),
            )

        with pytest.raises(EvidenceLinkRejected) as excinfo:
            await service.create_evidence_link(
                review.review_id,
                candidate_token=token,
                reason="reason",
                expected_version=review.version,
                actor=ACTOR,
                request_context=_context(),
            )

        assert "not linkable" in str(excinfo.value)

    async def test_an_expired_token_is_refused(self, repo, clock):
        service, _broker, review, candidate = await self._prepare(repo, clock)
        clock.advance(minutes=11)

        with pytest.raises(EvidenceLinkRejected):
            await service.create_evidence_link(
                review.review_id,
                candidate_token=candidate.candidate_token,
                reason="reason",
                expected_version=review.version,
                actor=ACTOR,
                request_context=_context(),
            )

    async def test_a_cross_review_token_fails_without_disclosing_anything(
        self, repo, clock
    ):
        service, _broker, review, candidate = await self._prepare(repo, clock)
        other = await service.import_review(OTHER_DON, ACTOR, _context())

        with pytest.raises(EvidenceLinkRejected) as cross:
            await service.create_evidence_link(
                other.review_id,
                candidate_token=candidate.candidate_token,
                reason="reason",
                expected_version=other.version,
                actor=ACTOR,
                request_context=_context(),
            )
        with pytest.raises(EvidenceLinkRejected) as missing:
            await service.create_evidence_link(
                review_id_for_devrev_work("don:core:dvrv-us-1:devo/synthetic:ticket/1"),
                candidate_token=candidate.candidate_token,
                reason="reason",
                expected_version=1,
                actor=ACTOR,
                request_context=_context(),
            )

        # Identical messages: a caller cannot tell "wrong ticket" from
        # "no such ticket", so neither confirms another ticket's evidence.
        assert str(cross.value) == str(missing.value)

    async def test_another_actors_token_is_refused(self, repo, clock):
        service, _broker, review, candidate = await self._prepare(repo, clock)

        with pytest.raises(EvidenceLinkRejected):
            await service.create_evidence_link(
                review.review_id,
                candidate_token=candidate.candidate_token,
                reason="reason",
                expected_version=review.version,
                actor=OTHER_ACTOR,
                request_context=_context(actor=OTHER_ACTOR),
            )

    async def test_a_token_is_revalidated_against_the_current_broker_result(
        self, repo, clock
    ):
        service, broker, review, candidate = await self._prepare(repo, clock)

        # The broker result moved on: the reviewer would be linking something
        # other than what they were shown.
        broker.envelope = _envelope(
            _record(trust=CorrelationTrust.CANDIDATE, model="a-different-model")
        )

        with pytest.raises(EvidenceLinkRejected):
            await service.create_evidence_link(
                review.review_id,
                candidate_token=candidate.candidate_token,
                reason="reason",
                expected_version=review.version,
                actor=ACTOR,
                request_context=_context(),
            )

    async def test_a_vanished_record_cannot_be_linked(self, repo, clock):
        service, broker, review, candidate = await self._prepare(repo, clock)
        broker.envelope = _envelope()

        with pytest.raises(EvidenceLinkRejected):
            await service.create_evidence_link(
                review.review_id,
                candidate_token=candidate.candidate_token,
                reason="reason",
                expected_version=review.version,
                actor=ACTOR,
                request_context=_context(),
            )

    async def test_an_already_manual_review_keeps_manual_and_offers_no_candidates(
        self, repo, clock
    ):
        service, _broker, review, candidate = await self._prepare(repo, clock)
        await service.create_evidence_link(
            review.review_id,
            candidate_token=candidate.candidate_token,
            reason="reason",
            expected_version=review.version,
            actor=ACTOR,
            request_context=_context(idempotency_key="synthetic-idem-2"),
        )

        envelope = await service.get_ticket_detail(SYNTHETIC_DON, ACTOR)

        assert envelope.evidence.correlation_status is CorrelationStatus.MANUAL
        assert envelope.evidence.correlation_trust is CorrelationTrust.MANUAL_REVIEWER


# =====================================================================
# 18. Exact display-id mode
# =====================================================================


class TestExactDisplayIdMode:
    async def test_it_uses_a_scoped_works_get_and_never_a_works_list_filter(
        self, repo, clock
    ):
        devrev = _FakeDevRev()
        service = _service(devrev, repo, clock)

        page = await service.get_live_ticket_by_display_id(SYNTHETIC_DISPLAY_ID, ACTOR)

        assert devrev.get_calls == [SYNTHETIC_DISPLAY_ID]
        # works.list has no display-id filter in the allowlisted surface, so it
        # must not be called at all.
        assert devrev.list_calls == []
        assert len(page.items) == 1

    async def test_it_returns_a_singleton_page_without_a_cursor(self, repo, clock):
        page = await _service(_FakeDevRev(), repo, clock).get_live_ticket_by_display_id(
            SYNTHETIC_DISPLAY_ID, ACTOR
        )

        assert len(page.items) == 1
        assert page.next_cursor is None
        assert page.prev_cursor is None
        assert page.page_size == 1

    async def test_it_overlays_at_most_one_review_summary(self, repo, clock):
        service = _service(_FakeDevRev(), repo, clock)
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())

        page = await service.get_live_ticket_by_display_id(SYNTHETIC_DISPLAY_ID, ACTOR)

        assert page.items[0].review is not None
        assert page.items[0].review.review_id == review.review_id
        # A summary, not the whole review: no comments, resolution, or chunks.
        assert not hasattr(page.items[0].review, "comments")
        assert not hasattr(page.items[0].review, "chunk_refs")

    async def test_it_is_mutually_exclusive_with_list_filters(self, repo, clock):
        service = _service(_FakeDevRev(), repo, clock)

        with pytest.raises(UnsupportedQuery):
            await service.list_live_tickets(
                DevRevTicketFilters(stage=["queued"]), display_id=SYNTHETIC_DISPLAY_ID
            )

    async def test_an_out_of_scope_or_missing_ticket_is_indistinguishable(
        self, repo, clock
    ):
        scoped = _FakeDevRev(get_error=DevRevScopeError("out of scope"))
        missing = _FakeDevRev(get_error=DevRevNotFoundError("no such ticket"))

        errors = []
        for devrev in (scoped, missing):
            with pytest.raises(TicketNotFound) as excinfo:
                await _service(devrev, repo, clock).get_live_ticket_by_display_id(
                    SYNTHETIC_DISPLAY_ID, ACTOR
                )
            errors.append(str(excinfo.value))

        assert errors[0] == errors[1]

    async def test_a_blank_ticket_id_is_a_rejected_filter_not_a_crash(self, repo, clock):
        service = _service(_FakeDevRev(), repo, clock)

        with pytest.raises(UnsupportedQuery):
            await service.get_live_ticket_by_display_id("   ", ACTOR)


# =====================================================================
# Durable review operations and actor consistency
# =====================================================================


class TestReviewOperations:
    async def test_import_review_is_idempotent_and_stores_no_title_or_body(
        self, repo, clock
    ):
        service = _service(_FakeDevRev(), repo, clock)

        first = await service.import_review(SYNTHETIC_DON, ACTOR, _context())
        second = await service.import_review(SYNTHETIC_DON, ACTOR, _context())

        assert first.review_id == second.review_id == review_id_for_devrev_work(SYNTHETIC_DON)
        stored = repr(await repo.get_review(first.review_id))
        # The live title carries a name and an email; neither may go durable.
        assert "Ana Synthetic" not in stored
        assert "ana@example.invalid" not in stored
        assert "Original ticket body." not in stored

    async def test_list_reviews_maps_the_repository_page_onto_the_console_shape(
        self, repo, clock
    ):
        service = _service(_FakeDevRev(), repo, clock)
        await service.import_review(SYNTHETIC_DON, ACTOR, _context())

        page = await service.list_reviews(ReviewListQuery(statuses=[ReviewStatus.UNREVIEWED]))

        assert isinstance(page, CursorPage)
        assert len(page.items) == 1
        assert page.items[0].devrev_work_id == SYNTHETIC_DON

    async def test_an_unsupported_filter_combination_is_reported_as_such(self, repo, clock):
        service = _service(_FakeDevRev(), repo, clock)

        with pytest.raises(UnsupportedQuery):
            await service.list_reviews(ReviewListQuery(title_contains="substring"))

    async def test_admin_reopen_is_threaded_through_rather_than_defaulted_away(
        self, repo, clock
    ):
        service = _service(_FakeDevRev(), repo, clock)
        review = await service.import_review(SYNTHETIC_DON, ACTOR, _context())

        current = review
        for status in (ReviewStatus.REVIEWED, ReviewStatus.TRIAGED):
            current = await service.patch_review(
                review.review_id,
                ReviewPatch(status=status),
                current.version,
                ACTOR,
                _context(role=ReviewerRole.ADMIN),
                admin_reopen=False,
            )
        # A terminal status needs a closed resolution object; the transition
        # table refuses it otherwise.
        current = await service.patch_review(
            review.review_id,
            ReviewPatch(
                status=ReviewStatus.WONT_FIX,
                resolution=ReviewResolution(
                    outcome=ResolutionOutcome.NO_CHANGE,
                    no_change_reason="accepted as working as intended",
                ),
            ),
            current.version,
            ACTOR,
            _context(role=ReviewerRole.ADMIN),
            admin_reopen=False,
        )

        reopened = await service.patch_review(
            review.review_id,
            ReviewPatch(status=ReviewStatus.TRIAGED),
            current.version,
            ACTOR,
            _context(role=ReviewerRole.ADMIN),
            admin_reopen=True,
        )

        assert reopened.status is ReviewStatus.TRIAGED

    async def test_a_drifting_actor_and_audit_context_is_refused(self, repo, clock):
        service = _service(_FakeDevRev(), repo, clock)

        # Recording one identity in the ledger while authorizing another is
        # exactly the drift MutationContext exists to prevent.
        with pytest.raises(Exception):
            await service.import_review(
                SYNTHETIC_DON, ACTOR, _context(actor=OTHER_ACTOR)
            )
