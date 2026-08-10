"""Stage 4 hydration service: live DevRev data joined to durable review state.

This is the layer the admin API calls. It owns four responsibilities and
deliberately owns nothing else:

**Bounded hydration.** One detail request fetches ``works.get`` plus *exactly
one* timeline page. Following ``next_cursor`` is an explicit second call from
the browser. There is no "just load the whole conversation" path, because a
timeline is unbounded and a page that silently loops is how a console becomes a
DevRev rate-limit incident.

**Classification by identity, never by name.** A timeline entry is attributed
using configured DevRev identity sets and DevRev actor *types*. A display name
is never sufficient evidence: a Rev user can be called "Support Bot" and an
automation can be called "Ana". When identity is ambiguous the entry is
``unknown`` and an operator warning is surfaced.

**Durable review data is never overwritten by a remote read.** A DevRev outage
produces a partial envelope, not an erased review. Live remote fields overlay
the *display*; they never touch rating, comments, or status.

**Correlation is offered, never asserted.** The broker's verified records are
reported as ``linked``. Anything weaker becomes a *candidate* carrying a
short-lived signed token bound to ticket, review, actor, sanitized reference,
digest, and expiry. Only an explicit reviewer action, revalidated against the
current broker result, turns a candidate into a stored ``manual`` link.

Ownership boundaries worth stating: the durable schema, the audit chain, and
the message cache belong to :mod:`data_pipeline.ticket_review_repository`; the
retry/pagination/scope rules belong to :mod:`data_pipeline.devrev_client`. This
module composes them and adds no second copy of either.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Protocol

from api.ticket_review_models import (
    DEFAULT_PAGE_SIZE,
    EVIDENCE_CANDIDATE_TTL_S,
    MAX_EVIDENCE_CANDIDATES,
    MAX_PAGE_SIZE,
    MAX_REASON_LENGTH,
    PLAIN_TEXT_BODY_TYPES,
    CacheState,
    ClassifiedTimelinePage,
    CorrelationStatus,
    CorrelationTrust,
    CursorError,
    CursorPage,
    DevRevAuthorizationStatus,
    DevRevActor,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketFilters,
    DevRevTicketSummary,
    DevRevTicketWithReviewSummary,
    DevRevTimelineEntry,
    EvidenceCandidateLink,
    MessageActorClass,
    MessageClassificationBasis,
    MessageRendering,
    NormalizedMessage,
    RagEvidenceEnvelope,
    RagEvidenceRecord,
    ReviewerIdentity,
    ReviewerRole,
    ReviewStatus,
    TicketDetailEnvelope,
    TicketEvaluationDetailEnvelope,
    TicketEvaluationRun,
    TicketEvaluationSummary,
    TicketEvidenceSummary,
    TicketReview,
    TicketReviewSummary,
    TimelineEntryKind,
    TimelinePage,
    open_cursor,
    review_id_for_devrev_work,
    seal_cursor,
    utc_now,
)
from api.tickets_console_config import (
    TicketConsoleSettings,
    classification_diagnostics,
)
from data_pipeline.devrev_client import (
    DevRevAuthenticationError,
    DevRevClient,
    DevRevConfigurationError,
    DevRevError,
    DevRevNotFoundError,
    DevRevRequestError,
    DevRevResourceLimitError,
    DevRevScopeError,
    DevRevRateLimitError,
    DevRevTransientError,
)
from data_pipeline.ticket_review_repository import (
    DevRevMessageCacheEntry,
    EvidenceCandidate,
    EvidenceCandidateRejected,
    EvaluationHydrationClaim,
    MutationContext,
    ReviewListQuery,
    ReviewNotFound,
    EvaluationRunNotFound,
    ReviewPatch,
    TicketReviewRepository,
    UnsupportedFilterCombination,
    normalize_display_id,
)

logger = logging.getLogger(__name__)

# AEAD associated-data context for a manual-evidence candidate token. Distinct
# from every cursor context, so a page cursor can never be replayed as a
# candidate and vice versa.
CANDIDATE_TOKEN_CONTEXT = "tickets:evidence-candidate:v1"  # noqa: S105 - AEAD context

# Bounded warning/diagnostic vocabulary. These cross into API envelopes.
WARNING_DEVREV_UNAVAILABLE = "devrev_unavailable"
WARNING_TIMELINE_UNAVAILABLE = "devrev_timeline_unavailable"
WARNING_REVIEW_UNAVAILABLE = "review_store_unavailable"
WARNING_CACHE_DEGRADED = "message_cache_degraded"
WARNING_BROKER_UNAVAILABLE = "evidence_broker_unavailable"
WARNING_TICKET_NOT_FOUND = "devrev_ticket_not_found"
WARNING_TICKET_OUT_OF_SCOPE = "devrev_ticket_out_of_scope"

PLACEHOLDER_UNSUPPORTED_BODY_TYPE = "unsupported_body_type"
PLACEHOLDER_UNSUPPORTED_ENTRY = "unsupported_entry_type"
PLACEHOLDER_NO_BODY = "no_body"

REASON_NO_DEFENSIBLE_IDENTIFIERS = "no_defensible_identifiers_exist"
REASON_BROKER_NOT_CONFIGURED = "evidence_broker_not_configured"

CANDIDATE_RATIONALE_UNVERIFIED = (
    "suggested from a stored execution whose correlation was not "
    "cryptographically verified; a reviewer must confirm it"
)

# How many concurrent works.get hydrations a single list page may issue. A list
# page must never fan out one request per row without a bound: 50 rows would be
# 50 simultaneous DevRev calls and an immediate rate-limit.
DEFAULT_HYDRATION_CONCURRENCY = 5
EVALUATION_HYDRATION_RETRY_BASE_S = 30
EVALUATION_HYDRATION_RETRY_CAP_S = 60 * 60
EVALUATION_HYDRATION_SLOW_RETRY_S = 15 * 60

SYSTEM_INGEST_ACTOR = ReviewerIdentity(
    subject="service:ticket-evaluation-ingest",
    email="ticket-evaluation-ingest@system.invalid",
    display_name="RAG ticket evaluation ingest",
)


class ServiceError(Exception):
    """Base class for hydration-service failures."""


class TicketNotFound(ServiceError):
    """The ticket does not exist, or is outside the configured scope.

    One type for both, deliberately: distinguishing them would tell a caller
    that a ticket it may not see nevertheless exists.
    """


class UnsupportedQuery(ServiceError):
    """The requested filter combination is not supported."""


class EvidenceLinkRejected(ServiceError):
    """A candidate token could not be validated against the current result.

    Carries one generic message for every cause — nonexistent, cross-ticket,
    cross-review, wrong actor, expired, tampered, or replayed — so a failure
    never discloses whether another ticket's evidence exists.
    """


class EvidenceBrokerClient(Protocol):
    """The console's view of the evidence broker.

    Only one method, and it takes the raw DON, because the console is the only
    caller allowed to hand the broker one. The transport (an authenticated
    service-to-service call) is Stage 5's concern.
    """

    async def lookup(self, devrev_work_id: str, *, max_results: int) -> RagEvidenceEnvelope:
        ...


@dataclass(frozen=True, slots=True)
class TicketEvaluationIngestResult:
    run: TicketEvaluationRun
    created: bool


class TicketEvaluationService:
    """Durable RAG-run ingestion and retrieval, with bounded DevRev hydration."""

    def __init__(
        self,
        *,
        devrev: DevRevClient,
        repository: TicketReviewRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._devrev = devrev
        self._repo = repository
        self._clock = clock

    @staticmethod
    def _system_context(execution_id: str) -> MutationContext:
        return MutationContext(
            actor=SYSTEM_INGEST_ACTOR,
            actor_role=ReviewerRole.ADMIN,
            request_id=None,
            idempotency_key=f"rag-evaluation:{execution_id}",
            reason_code="rag_execution_ingest",
        )

    async def ingest(self, event: Any) -> TicketEvaluationIngestResult:
        run, created = await self._repo.persist_ticket_evaluation(event)
        claim = await self._repo.claim_ticket_evaluation_hydration(run.execution_id)
        if claim is not None:
            run = await self._hydrate(claim)
        else:
            run = await self._repo.get_ticket_evaluation(run.execution_id)
        return TicketEvaluationIngestResult(run=run, created=created)

    async def _hydrate(self, claim: EvaluationHydrationClaim) -> TicketEvaluationRun:
        run = claim.run
        try:
            ticket = await self._devrev.get_ticket(run.event.ticket_id)
        except DevRevError as exc:
            authorization_denied = isinstance(
                exc, (DevRevNotFoundError, DevRevScopeError)
            )
            # A token/configuration/contract incident says nothing about this
            # ticket's scope. Keep the durable run quarantined and retry it at
            # a deliberately slow cadence so a repaired deployment recovers.
            retryable = not authorization_denied
            code = _hydration_error_code(exc)
            next_attempt = None
            if retryable:
                if isinstance(exc, (DevRevTransientError, DevRevRateLimitError)):
                    delay = min(
                        EVALUATION_HYDRATION_RETRY_CAP_S,
                        EVALUATION_HYDRATION_RETRY_BASE_S
                        * (2 ** min(max(run.hydration_attempts - 1, 0), 7)),
                    )
                else:
                    delay = EVALUATION_HYDRATION_SLOW_RETRY_S
                next_attempt = self._clock() + timedelta(seconds=delay)
            return await self._repo.record_ticket_evaluation_hydration_failure(
                run.execution_id,
                error_code=code,
                retryable=retryable,
                next_attempt_at=next_attempt,
                authorization_denied=authorization_denied,
                lease_token=claim.lease_token,
            )
        return await self._repo.link_ticket_evaluation(
            run.execution_id,
            ticket,
            context=self._system_context(run.execution_id),
            lease_token=claim.lease_token,
        )

    async def retry_due_hydrations(self, *, limit: int = 20) -> tuple[int, int]:
        due = await self._repo.list_due_ticket_evaluation_hydrations(limit=limit)
        attempted = 0
        succeeded = 0
        for run in due:
            claim = await self._repo.claim_ticket_evaluation_hydration(run.execution_id)
            if claim is None:
                continue
            attempted += 1
            hydrated = await self._hydrate(claim)
            if hydrated.hydration_status.value == "succeeded":
                succeeded += 1
        return attempted, succeeded

    async def list_runs(
        self,
        *,
        cursor: Optional[str] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        filters: Optional[Mapping[str, str]] = None,
    ) -> CursorPage[TicketEvaluationSummary]:
        page = await self._repo.list_ticket_evaluations(
            limit=limit,
            cursor=cursor,
            filters=filters,
        )
        rows: list[TicketEvaluationSummary] = []
        for run in page.items:
            review = None
            if run.review_id:
                try:
                    review = await self._repo.get_review(run.review_id)
                except ReviewNotFound:
                    review = None
            rows.append(TicketEvaluationSummary.of(run, review))
        return CursorPage[TicketEvaluationSummary](
            items=rows,
            next_cursor=page.next_cursor,
            page_size=page.page_size,
        )

    async def get_detail(self, execution_id: str) -> TicketEvaluationDetailEnvelope:
        # This read is deliberately first: a DevRev-only ticket can never be
        # discovered by guessing its id against the admin route.
        run = await self._repo.get_ticket_evaluation(execution_id)
        if run.authorization_status is not DevRevAuthorizationStatus.AUTHORIZED:
            # Quarantined and denied records share the same external absence.
            # The durable ingest remains available only to the private retry
            # plane, and a caller cannot probe whether an out-of-scope id exists.
            raise EvaluationRunNotFound("no RAG execution exists for that id")

        review = None
        if run.review_id:
            try:
                review = await self._repo.get_review(run.review_id)
            except ReviewNotFound:
                review = None

        # Reviewer GETs are pure reads. DevRev scope was validated by the
        # private hydration plane, whose bounded snapshot is sufficient for a
        # deterministic detail response and cannot trigger retries here.
        ticket = run.ticket_detail_snapshot
        if ticket is None and run.ticket_snapshot is not None:
            ticket = DevRevTicketDetail.model_validate(
                run.ticket_snapshot.model_dump(mode="python")
            )
        event = run.event
        structured = event.structured_response
        routed = structured.get(event.route.value)
        routed_map = routed if isinstance(routed, Mapping) else structured
        response = routed_map.get("response")
        response_map = response if isinstance(response, Mapping) else {}
        outcome_reason = (
            response_map.get("outcome_reason")
            or routed_map.get("outcome_reason")
            or structured.get("outcome_reason")
        )
        gaps: list[str] = []
        for container, key in (
            (routed_map, "gaps"),
            (routed_map, "coverage_gaps"),
            (response_map, "gaps"),
            (response_map, "data_gaps"),
        ):
            values = container.get(key)
            if not isinstance(values, list):
                continue
            for gap in values:
                if not isinstance(gap, str):
                    continue
                normalized_gap = gap.strip()[:MAX_REASON_LENGTH]
                if normalized_gap and normalized_gap not in gaps:
                    gaps.append(normalized_gap)
                if len(gaps) >= 20:
                    break
            if len(gaps) >= 20:
                break
        model_metadata = {
            key: value
            for key, value in event.retrieval_metadata.items()
            if key in {"model", "model_name", "model_version", "prompt_version"}
        }
        timing_metadata = {
            key: value
            for key, value in event.retrieval_metadata.items()
            if key in {"duration_ms", "latency_ms", "started_at", "completed_at"}
        }
        return TicketEvaluationDetailEnvelope(
            execution=run,
            review=review,
            ticket=ticket,
            generated_answer=event.answer,
            classification_reasoning=event.classification.reasoning,
            outcome_reason=(str(outcome_reason)[:20_000] if outcome_reason else None),
            diagnostics=event.diagnostics,
            gaps=gaps,
            source_articles=event.sources,
            chunk_evidence=event.chunks,
            model_metadata=model_metadata,
            timing_metadata=timing_metadata,
            hydration_status=run.hydration_status,
            partial=ticket is None,
            warnings=[],
        )


def _hydration_error_code(exc: DevRevError) -> str:
    if isinstance(exc, DevRevTransientError):
        return "devrev_transient"
    if isinstance(exc, DevRevRateLimitError):
        return "devrev_rate_limited"
    if isinstance(exc, (DevRevNotFoundError, DevRevScopeError)):
        return "devrev_not_found_or_out_of_scope"
    if isinstance(exc, DevRevAuthenticationError):
        return "devrev_authentication"
    if isinstance(exc, DevRevConfigurationError):
        return "devrev_configuration"
    return "devrev_unavailable"


# =====================================================================
# Message classification
# =====================================================================


class MessageClassifier:
    """Attributes timeline entries using identities and types only.

    Precedence is fixed and total:

    1. a change event is an ``EVENT`` — it has no author by construction;
    2. a configured AI author id wins;
    3. a configured system author id wins;
    4. a configured human author id wins (the explicit override);
    5. an explicitly external actor type (``rev_user``) is a ``PARTICIPANT``;
    6. a documented system actor type (``sys_user``/``automation``) is
       ``AI_OR_SYSTEM``;
    7. an internal actor type (``dev_user``) is a ``HUMAN_AGENT``;
    8. everything else is ``UNKNOWN``.

    Configured ids are checked *before* actor types so that an AI agent
    provisioned as a ``dev_user`` — the common case — is not reported as a human
    agent. Display names are not consulted at any step.
    """

    def __init__(
        self,
        *,
        ai_author_ids: Sequence[str] = (),
        system_author_ids: Sequence[str] = (),
        human_author_ids: Sequence[str] = (),
        diagnostics: Sequence[str] = (),
    ) -> None:
        self._ai = _identity_set(ai_author_ids)
        self._system = _identity_set(system_author_ids)
        self._human = _identity_set(human_author_ids)
        self._diagnostics = tuple(diagnostics)

    @classmethod
    def from_settings(cls, settings: TicketConsoleSettings) -> MessageClassifier:
        return cls(
            ai_author_ids=settings.DEVREV_AI_AUTHOR_IDS,
            system_author_ids=settings.DEVREV_SYSTEM_AUTHOR_IDS,
            human_author_ids=settings.DEVREV_HUMAN_AUTHOR_IDS,
            diagnostics=classification_diagnostics(settings),
        )

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Operator warnings, e.g. that no AI identities are configured."""
        return self._diagnostics

    def classify(
        self, entry: DevRevTimelineEntry
    ) -> tuple[MessageActorClass, MessageClassificationBasis]:
        if entry.kind is TimelineEntryKind.CHANGE_EVENT:
            return MessageActorClass.EVENT, MessageClassificationBasis.CHANGE_EVENT

        actor: Optional[DevRevActor] = entry.author
        if actor is None:
            return MessageActorClass.UNKNOWN, MessageClassificationBasis.NO_AUTHOR

        actor_id = actor.actor_id.strip()
        if actor_id in self._ai:
            return (
                MessageActorClass.AI_OR_SYSTEM,
                MessageClassificationBasis.CONFIGURED_AI_AUTHOR_ID,
            )
        if actor_id in self._system:
            return (
                MessageActorClass.AI_OR_SYSTEM,
                MessageClassificationBasis.CONFIGURED_SYSTEM_AUTHOR_ID,
            )
        if actor_id in self._human:
            return (
                MessageActorClass.HUMAN_AGENT,
                MessageClassificationBasis.CONFIGURED_HUMAN_AUTHOR_ID,
            )

        if actor.actor_type is DevRevActorType.REV_USER:
            return (
                MessageActorClass.PARTICIPANT,
                MessageClassificationBasis.EXTERNAL_ACTOR_TYPE,
            )
        if actor.actor_type in {DevRevActorType.SYS_USER, DevRevActorType.AUTOMATION}:
            return (
                MessageActorClass.AI_OR_SYSTEM,
                MessageClassificationBasis.SYSTEM_ACTOR_TYPE,
            )
        if actor.actor_type is DevRevActorType.DEV_USER:
            # A Dev user that is not in any configured set is a human agent only
            # because no AI/system identity claimed it. When those sets are
            # empty, `diagnostics` says so out loud.
            if not self._ai and not self._system:
                return MessageActorClass.UNKNOWN, MessageClassificationBasis.AMBIGUOUS
            return (
                MessageActorClass.HUMAN_AGENT,
                MessageClassificationBasis.INTERNAL_ACTOR_TYPE,
            )
        return MessageActorClass.UNKNOWN, MessageClassificationBasis.AMBIGUOUS

    def normalize(self, entry: DevRevTimelineEntry) -> NormalizedMessage:
        """Project one entry into a classified, render-safe message."""
        actor_class, basis = self.classify(entry)
        internal = _is_internal(entry)
        rendering, body, placeholder_reason = _render(entry)
        return NormalizedMessage(
            entry_id=entry.entry_id,
            object_id=entry.object_id,
            kind=entry.kind,
            visibility=entry.visibility,
            actor_class=actor_class,
            basis=basis,
            actor=entry.author,
            internal=internal,
            # Only an externally visible authored comment can be part of the
            # participant-facing conversation. An event is never a reply, and an
            # internal note is never spliced into one.
            participant_facing=(
                not internal
                and entry.kind is TimelineEntryKind.COMMENT
                and actor_class
                in {
                    MessageActorClass.PARTICIPANT,
                    MessageActorClass.HUMAN_AGENT,
                    MessageActorClass.AI_OR_SYSTEM,
                }
            ),
            rendering=rendering,
            body=body,
            body_type=entry.body_type,
            placeholder_reason=placeholder_reason,
            body_length=len(entry.body or ""),
            change_summary=entry.change_summary,
            unsupported_type=entry.unsupported_type,
            thread_id=entry.thread_id,
            in_reply_to=entry.in_reply_to,
            created_at=entry.created_at,
            modified_at=entry.modified_at,
        )


def _identity_set(values: Sequence[str]) -> frozenset[str]:
    return frozenset(value.strip() for value in values if value and value.strip())


def _is_internal(entry: DevRevTimelineEntry) -> bool:
    """Internal unless DevRev said the entry is externally visible.

    Fail-closed: the adapter defaults an absent ``visibility`` to ``private``,
    and treating anything other than an explicit external/public value as
    internal is what stops an agent-only note from reaching a participant.
    """
    return entry.visibility.value not in {"external", "public"}


def _render(
    entry: DevRevTimelineEntry,
) -> tuple[MessageRendering, Optional[str], Optional[str]]:
    """Decide how a body may be shown, without ever passing markup through."""
    if entry.kind is not TimelineEntryKind.COMMENT:
        return MessageRendering.PLACEHOLDER, None, (
            PLACEHOLDER_UNSUPPORTED_ENTRY
            if entry.kind is TimelineEntryKind.UNSUPPORTED
            else None
        )
    if not entry.body:
        return MessageRendering.PLACEHOLDER, None, PLACEHOLDER_NO_BODY
    body_type = (entry.body_type or "").strip().lower()
    # An empty body_type is DevRev's plain-text default. Anything else — HTML,
    # Markdown, an unmodelled value — is preserved as metadata plus a
    # placeholder rather than being handed to the browser.
    if body_type and body_type not in PLAIN_TEXT_BODY_TYPES:
        return MessageRendering.PLACEHOLDER, None, PLACEHOLDER_UNSUPPORTED_BODY_TYPE
    return MessageRendering.TEXT, entry.body, None


# =====================================================================
# The service
# =====================================================================


class TicketReviewService:
    """Composes the DevRev adapter, the review repository, and the broker."""

    def __init__(
        self,
        *,
        devrev: DevRevClient,
        repository: TicketReviewRepository,
        classifier: MessageClassifier,
        candidate_key: bytes,
        broker: Optional[EvidenceBrokerClient] = None,
        clock: Callable[[], datetime] = utc_now,
        page_size: int = DEFAULT_PAGE_SIZE,
        hydration_concurrency: int = DEFAULT_HYDRATION_CONCURRENCY,
        candidate_ttl_s: int = EVIDENCE_CANDIDATE_TTL_S,
    ) -> None:
        self._devrev = devrev
        self._repo = repository
        self._classifier = classifier
        self._candidate_key = candidate_key
        self._broker = broker
        self._clock = clock
        self._page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        self._hydration = asyncio.Semaphore(max(1, int(hydration_concurrency)))
        self._candidate_ttl_s = max(1, int(candidate_ttl_s))
        self._evaluation_service = TicketEvaluationService(
            devrev=devrev,
            repository=repository,
            clock=clock,
        )

    async def list_evaluation_runs(
        self,
        *,
        cursor: Optional[str] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        filters: Optional[Mapping[str, str]] = None,
    ) -> CursorPage[TicketEvaluationSummary]:
        return await self._evaluation_service.list_runs(
            cursor=cursor,
            limit=limit,
            filters=filters,
        )

    async def get_evaluation_detail(
        self, execution_id: str
    ) -> TicketEvaluationDetailEnvelope:
        return await self._evaluation_service.get_detail(execution_id)


    async def _review_or_none(self, review_id: str) -> Optional[TicketReview]:
        """Load a review, or ``None`` when there simply is not one.

        The repository raises :class:`ReviewNotFound` because "does this exist"
        and "give me this" are different questions there. The service asks the
        first one constantly — a live ticket with no review is the normal case,
        not an error — so the translation lives here rather than being repeated
        at every call site.
        """
        try:
            return await self._repo.get_review(review_id)
        except ReviewNotFound:
            return None

    # -----------------------------------------------------------------
    # Live listing
    # -----------------------------------------------------------------

    async def list_live_tickets(
        self,
        query: DevRevTicketFilters,
        *,
        cursor: Optional[str] = None,
        mode: str = "after",
        limit: Optional[int] = None,
        display_id: Optional[str] = None,
    ) -> CursorPage[DevRevTicketWithReviewSummary]:
        """One live ``works.list`` page overlaid with bounded review summaries.

        The overlay is a *summary* and never a timeline: a list page must not
        issue one hydration per row. When a review lookup fails the row still
        renders with ``review=None`` and the page is marked partial, because a
        review-store outage must not hide live DevRev tickets.
        """
        if display_id is not None:
            raise UnsupportedQuery(
                "an exact ticket id lookup is get_live_ticket_by_display_id and "
                "cannot be combined with list filters"
            )
        if _has_filters(query) and cursor is None and mode not in {"after", "before"}:
            raise UnsupportedQuery("unsupported pagination mode")

        page = await self._devrev.list_tickets(
            query, cursor=cursor, mode="before" if mode == "before" else "after",
            limit=limit or self._page_size,
        )
        rows, partial, warnings = await self._overlay_summaries(page.items)
        return CursorPage[DevRevTicketWithReviewSummary](
            items=rows,
            next_cursor=page.next_cursor,
            prev_cursor=page.prev_cursor,
            page_size=page.page_size,
            partial=page.partial or partial,
            truncated=page.truncated,
            warnings=_bounded_warnings([*page.warnings, *warnings]),
        )

    async def get_live_ticket_by_display_id(
        self, display_id: str, actor: ReviewerIdentity
    ) -> CursorPage[DevRevTicketWithReviewSummary]:
        """Exact display-id lookup: a scoped ``works.get``, never a list filter.

        ``works.list`` has no display-id filter in the allowlisted surface, so
        sending one would either be silently dropped or rejected. The adapter's
        ``works.get`` accepts a display id directly *and* re-checks the
        configured part/visibility scope, so a known id cannot be used to step
        around the list filters.

        The result is a singleton page with **no cursor**: there is nothing to
        page through, and returning one would invite a pointless second call.
        """
        del actor  # Authorization is the API layer's; scope is the adapter's.
        normalized = _normalized_display_id(display_id)
        try:
            detail = await self._devrev.get_ticket(normalized)
        except (DevRevNotFoundError, DevRevScopeError) as exc:
            raise TicketNotFound("that ticket is not available") from exc
        except DevRevRequestError as exc:
            raise UnsupportedQuery("that is not a valid ticket id") from exc

        rows, partial, warnings = await self._overlay_summaries([detail])
        return CursorPage[DevRevTicketWithReviewSummary](
            items=rows,
            next_cursor=None,
            prev_cursor=None,
            page_size=1,
            partial=partial,
            warnings=_bounded_warnings(warnings),
        )

    async def _overlay_summaries(
        self, tickets: Sequence[DevRevTicketSummary]
    ) -> tuple[list[DevRevTicketWithReviewSummary], bool, list[str]]:
        """Join each live row to at most one bounded review summary."""
        if not tickets:
            return [], False, []

        async def _summary(ticket: DevRevTicketSummary) -> Optional[TicketReviewSummary]:
            async with self._hydration:
                review = await self._review_or_none(
                    review_id_for_devrev_work(ticket.devrev_work_id)
                )
            return None if review is None else TicketReviewSummary.of(review)

        results = await asyncio.gather(
            *(_summary(ticket) for ticket in tickets), return_exceptions=True
        )
        rows: list[DevRevTicketWithReviewSummary] = []
        partial = False
        warnings: list[str] = []
        for ticket, result in zip(tickets, results, strict=True):
            if isinstance(result, BaseException):
                # Explicit partial failure: the live ticket is still shown, and
                # the missing overlay is reported rather than looking like
                # "this ticket has no review".
                partial = True
                if WARNING_REVIEW_UNAVAILABLE not in warnings:
                    warnings.append(WARNING_REVIEW_UNAVAILABLE)
                logger.warning(
                    "review overlay unavailable; error_type=%s", type(result).__name__
                )
                rows.append(DevRevTicketWithReviewSummary(ticket=ticket, review=None))
                continue
            rows.append(DevRevTicketWithReviewSummary(ticket=ticket, review=result))
        return rows, partial, warnings

    # -----------------------------------------------------------------
    # Detail and timeline
    # -----------------------------------------------------------------

    async def get_ticket_detail(
        self,
        ticket_ref: str,
        actor: ReviewerIdentity,
        *,
        timeline_cursor: Optional[str] = None,
        include_timeline: bool = True,
        include_evidence: bool = True,
    ) -> TicketDetailEnvelope:
        """One ``works.get`` plus **exactly one** timeline page.

        ``ticket_ref`` is a DevRev DON or display id — the thing a reviewer has
        in hand. The durable ``review_id`` is derived from the resolved DON, not
        accepted from a caller, so a caller cannot ask for one ticket's DevRev
        data joined to another ticket's review.

        Never loops the timeline. The browser follows ``next_cursor`` by calling
        :meth:`get_timeline_page`.
        """
        warnings: list[str] = []
        diagnostics = list(self._classifier.diagnostics)
        partial = False
        cache_state = CacheState.FRESH

        detail: Optional[DevRevTicketDetail] = None
        try:
            detail = await self._devrev.get_ticket(_validated_ref(ticket_ref))
        except (DevRevNotFoundError, DevRevScopeError):
            # Indistinguishable on purpose; the review is still returned below
            # so a reviewer never loses durable work to a remote failure.
            warnings.append(WARNING_TICKET_NOT_FOUND)
            partial = True
        except DevRevError as exc:
            logger.warning("devrev detail unavailable; error_type=%s", type(exc).__name__)
            warnings.append(WARNING_DEVREV_UNAVAILABLE)
            partial = True

        review = await self._review_for(detail, ticket_ref, warnings)
        if review is None and detail is None:
            # Nothing to show at all is a not-found, not an empty envelope.
            raise TicketNotFound("that ticket is not available")

        timeline: Optional[ClassifiedTimelinePage] = None
        if include_timeline and detail is not None:
            timeline, timeline_partial, cache_state = await self._timeline_page(
                detail, cursor=timeline_cursor, warnings=warnings
            )
            partial = partial or timeline_partial

        evidence = TicketEvidenceSummary(
            unavailable_reason=REASON_NO_DEFENSIBLE_IDENTIFIERS
        )
        if include_evidence:
            evidence = await self._evidence_for(detail, review, actor, warnings)

        return TicketDetailEnvelope(
            ticket_ref=ticket_ref[: 256],
            ticket=detail,
            review=review,
            timeline=timeline,
            evidence=evidence,
            partial=partial,
            cache_state=cache_state,
            warnings=_bounded_warnings(warnings),
            diagnostics=_bounded_warnings(diagnostics),
        )

    async def get_timeline_page(
        self,
        ticket_ref: str,
        cursor: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> ClassifiedTimelinePage:
        """One explicitly requested bounded timeline page.

        Resolves the ticket first. The adapter performs no ticket-level scope
        check on ``timeline-entries.list``, so calling it with a caller-supplied
        object id would read a conversation the configured part/visibility
        allowlist never approved. Resolving via ``works.get`` also converts a
        display id into the DON the timeline actually keys on — passing a
        display id straight through silently drops every entry.
        """
        try:
            detail = await self._devrev.get_ticket(_validated_ref(ticket_ref))
        except (DevRevNotFoundError, DevRevScopeError) as exc:
            raise TicketNotFound("that ticket is not available") from exc

        warnings: list[str] = []
        page, partial, cache = await self._timeline_page(
            detail, cursor=cursor, warnings=warnings, limit=limit
        )
        if page is None:
            # The ticket was already resolved and authorized above. A timeline
            # outage is therefore a partial result, not a false 404 that would
            # imply the persisted execution or DevRev ticket disappeared.
            return ClassifiedTimelinePage(
                items=[],
                messages=[],
                page_size=limit or self._page_size,
                partial=partial,
                warnings=_bounded_warnings(warnings),
                cache_state=cache,
                diagnostics=_bounded_warnings(list(self._classifier.diagnostics)),
            )
        return page

    async def _timeline_page(
        self,
        detail: DevRevTicketDetail,
        *,
        cursor: Optional[str],
        warnings: list[str],
        limit: Optional[int] = None,
    ) -> tuple[Optional[ClassifiedTimelinePage], bool, CacheState]:
        """Fetch, classify, and opportunistically cache exactly one page."""
        try:
            page: TimelinePage = await self._devrev.list_timeline_page(
                detail.devrev_work_id, cursor=cursor, limit=limit or self._page_size
            )
        except DevRevResourceLimitError:
            # A guard limit is a typed partial result, never a claim of
            # completeness and never an error that loses the review.
            warnings.append(WARNING_TIMELINE_UNAVAILABLE)
            return None, True, CacheState.FRESH
        except DevRevError as exc:
            logger.warning("devrev timeline unavailable; error_type=%s", type(exc).__name__)
            warnings.append(WARNING_TIMELINE_UNAVAILABLE)
            return None, True, CacheState.FRESH

        messages = [self._classifier.normalize(entry) for entry in page.items]
        cache_state = await self._cache_messages(detail, page.items, warnings)

        classified = ClassifiedTimelinePage(
            # Source order preserved: the adapter's order is DevRev's order, and
            # re-sorting a single page would misrepresent it. An explicitly
            # loaded multi-page set may be sorted; one page may not.
            items=list(page.items),
            messages=messages,
            next_cursor=page.next_cursor,
            prev_cursor=page.prev_cursor,
            page_size=page.page_size,
            partial=page.partial,
            truncated=page.truncated,
            # The adapter's warnings plus anything this layer observed (a
            # degraded cache write), so a caller reading only the page still
            # learns the read was imperfect.
            warnings=_bounded_warnings([*page.warnings, *warnings]),
            cache_state=cache_state,
            diagnostics=_bounded_warnings(list(self._classifier.diagnostics)),
        )
        return classified, page.partial, cache_state

    async def _cache_messages(
        self,
        detail: DevRevTicketDetail,
        entries: Sequence[DevRevTimelineEntry],
        warnings: list[str],
    ) -> CacheState:
        """Upsert bounded raw bodies into the TTL message cache.

        A failed cache write never fails an otherwise valid read: the cache is
        disposable by design, and its TTL expiring must never touch the durable
        human review. The state is reported as ``degraded`` instead.
        """
        if not entries:
            return CacheState.FRESH

        async def _one(entry: DevRevTimelineEntry) -> None:
            await self._repo.upsert_message_cache_entry(
                DevRevMessageCacheEntry(
                    remote_entry_id=entry.entry_id,
                    devrev_work_id=detail.devrev_work_id,
                    # The repository's own guard uses these to refuse letting an
                    # older snapshot overwrite a newer one.
                    object_version=detail.object_version,
                    remote_modified_at=entry.modified_at or entry.created_at,
                    body=entry.body,
                    body_type=entry.body_type,
                    author_id=entry.author.actor_id if entry.author else None,
                    visibility=entry.visibility.value,
                )
            )

        results = await asyncio.gather(
            *(_one(entry) for entry in entries), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            if WARNING_CACHE_DEGRADED not in warnings:
                warnings.append(WARNING_CACHE_DEGRADED)
            logger.warning("devrev message cache write degraded")
            return CacheState.DEGRADED
        return CacheState.FRESH

    async def _review_for(
        self,
        detail: Optional[DevRevTicketDetail],
        ticket_ref: str,
        warnings: list[str],
    ) -> Optional[TicketReview]:
        """Load the durable review, or ``None`` — never a fabricated document."""
        try:
            if detail is not None:
                return await self._review_or_none(
                    review_id_for_devrev_work(detail.devrev_work_id)
                )
            # Without live data, fall back to the exact-id lookup so a DevRev
            # outage still shows the reviewer their own work.
            try:
                return await self._repo.find_review_by_display_id(ticket_ref)
            except UnsupportedFilterCombination:
                return None
        except Exception as exc:  # noqa: BLE001 - a review outage is partial, not fatal
            logger.warning("review lookup failed; error_type=%s", type(exc).__name__)
            if WARNING_REVIEW_UNAVAILABLE not in warnings:
                warnings.append(WARNING_REVIEW_UNAVAILABLE)
            return None

    # -----------------------------------------------------------------
    # Evidence: linked, candidate, or an explicit gap
    # -----------------------------------------------------------------

    async def _evidence_for(
        self,
        detail: Optional[DevRevTicketDetail],
        review: Optional[TicketReview],
        actor: ReviewerIdentity,
        warnings: list[str],
    ) -> TicketEvidenceSummary:
        """Ask the broker, then report what can actually be defended."""
        if detail is None:
            # A historical ticket with no live identifiers has nothing to hash.
            return TicketEvidenceSummary(
                correlation_status=(
                    review.correlation_status if review else CorrelationStatus.UNAVAILABLE
                ),
                correlation_trust=(
                    review.correlation_trust if review else CorrelationTrust.NONE
                ),
                unavailable_reason=REASON_NO_DEFENSIBLE_IDENTIFIERS,
            )
        if self._broker is None:
            return TicketEvidenceSummary(
                correlation_status=(
                    review.correlation_status if review else CorrelationStatus.UNAVAILABLE
                ),
                unavailable_reason=REASON_BROKER_NOT_CONFIGURED,
            )

        try:
            envelope = await self._broker.lookup(
                detail.devrev_work_id, max_results=MAX_EVIDENCE_CANDIDATES
            )
        except Exception as exc:  # noqa: BLE001 - a broker outage is a gap, not a 500
            logger.warning("evidence broker unavailable; error_type=%s", type(exc).__name__)
            if WARNING_BROKER_UNAVAILABLE not in warnings:
                warnings.append(WARNING_BROKER_UNAVAILABLE)
            return TicketEvidenceSummary(
                correlation_status=(
                    review.correlation_status if review else CorrelationStatus.UNAVAILABLE
                ),
                unavailable_reason=REASON_NO_DEFENSIBLE_IDENTIFIERS,
                warnings=[WARNING_BROKER_UNAVAILABLE],
            )

        verified = [
            record
            for record in envelope.records
            if record.correlation_trust is CorrelationTrust.VERIFIED_WORKLOAD
        ]
        suggestions = [record for record in envelope.records if record not in verified]

        # An already-linked review keeps `manual`; otherwise only a verified
        # producer record may claim `linked`.
        if review is not None and review.correlation_status is CorrelationStatus.MANUAL:
            status = CorrelationStatus.MANUAL
            trust = CorrelationTrust.MANUAL_REVIEWER
        elif verified:
            status = CorrelationStatus.LINKED
            trust = CorrelationTrust.VERIFIED_WORKLOAD
        else:
            status = CorrelationStatus.UNAVAILABLE
            trust = CorrelationTrust.NONE

        candidates: list[EvidenceCandidateLink] = []
        if status is not CorrelationStatus.LINKED and review is not None:
            candidates = [
                self._mint_candidate(record, envelope, review=review, actor=actor)
                for record in suggestions[:MAX_EVIDENCE_CANDIDATES]
            ]

        return TicketEvidenceSummary(
            correlation_status=status,
            correlation_trust=trust,
            correlation_source=(verified[0].correlation_source if verified else None),
            unavailable_reason=(
                None
                if status is not CorrelationStatus.UNAVAILABLE
                else (envelope.unavailable_reason or REASON_NO_DEFENSIBLE_IDENTIFIERS)
            ),
            provenance=[record.provenance for record in envelope.records],
            # The whole sanitized record, not just its retrieval half. Every
            # field on it is already allowlisted by the broker — prompts,
            # responses, chunk text, participant data and raw external
            # identifiers are absent by construction — and a reviewer cannot
            # judge an answer without knowing which model on which route
            # produced it, when, and whether it failed.
            executions=list(envelope.records),
            linked_count=len(verified),
            candidate_links=candidates,
            broker_available=True,
            warnings=_bounded_warnings(list(envelope.warnings)),
        )

    def _mint_candidate(
        self,
        record: RagEvidenceRecord,
        envelope: RagEvidenceEnvelope,
        *,
        review: TicketReview,
        actor: ReviewerIdentity,
    ) -> EvidenceCandidateLink:
        """Seal a suggestion into a token the caller cannot edit.

        The payload binds ticket, review, actor subject, sanitized reference,
        content digest, and the whole broker result digest. Because it is
        AEAD-sealed under a server key with a distinct associated-data context,
        a caller can neither read it, retarget it at another ticket or review,
        nor extend its expiry — the expiry is inside the ciphertext.
        """
        now = self._clock()
        expires_at = now + timedelta(seconds=self._candidate_ttl_s)
        token = seal_cursor(
            self._candidate_key,
            {
                "w": review.devrev_work_id,
                "r": review.review_id,
                "s": actor.subject,
                "e": record.evidence_reference,
                "d": record.evidence_digest,
                "b": envelope.result_digest,
            },
            context=CANDIDATE_TOKEN_CONTEXT,
            ttl_s=self._candidate_ttl_s,
            now=now,
        )
        return EvidenceCandidateLink(
            candidate_token=token,
            evidence_reference=record.evidence_reference,
            evidence_digest=record.evidence_digest,
            broker_result_digest=envelope.result_digest,
            rationale=CANDIDATE_RATIONALE_UNVERIFIED,
            correlation_trust=CorrelationTrust.CANDIDATE,
            expires_at=expires_at,
        )

    async def create_evidence_link(
        self,
        review_id: str,
        *,
        candidate_token: str,
        reason: str,
        expected_version: Optional[int],
        actor: ReviewerIdentity,
        request_context: MutationContext,
    ) -> TicketReview:
        """Turn a validated candidate into a durable ``manual`` link.

        Validation order matters, and every failure raises the *same*
        :class:`EvidenceLinkRejected` message:

        1. open the sealed token — a nonexistent, tampered, wrong-context, or
           expired token fails here;
        2. the token's review must be the review in the path, and its actor must
           be the authenticated actor;
        3. re-run the broker lookup and require the record to still be present
           with the *same* content digest and the same result digest. A stale or
           replayed token therefore fails even though its ciphertext is intact;
        4. only then does the repository create the link and the audit event.

        The uniform message is deliberate: a distinguishable "that candidate
        belongs to another ticket" would confirm that another ticket's evidence
        exists, which is an IDOR disclosure even without returning the data.
        """
        _assert_same_actor(actor, request_context)
        rejected = EvidenceLinkRejected("that evidence candidate is not linkable")

        try:
            payload = open_cursor(
                self._candidate_key,
                candidate_token,
                context=CANDIDATE_TOKEN_CONTEXT,
                now=self._clock(),
            )
        except CursorError as exc:
            raise rejected from exc

        if payload.get("r") != review_id or payload.get("s") != actor.subject:
            raise rejected

        review = await self._review_or_none(review_id)
        if review is None or review.devrev_work_id != payload.get("w"):
            raise rejected
        if self._broker is None:
            raise rejected

        try:
            envelope = await self._broker.lookup(
                review.devrev_work_id, max_results=MAX_EVIDENCE_CANDIDATES
            )
        except Exception as exc:  # noqa: BLE001 - cannot revalidate, cannot link
            raise rejected from exc

        if envelope.result_digest != payload.get("b"):
            # The broker result moved on, so the reviewer would be linking
            # something other than what they were shown.
            raise rejected
        current = next(
            (
                record
                for record in envelope.records
                if record.evidence_reference == payload.get("e")
            ),
            None,
        )
        if current is None or current.evidence_digest != payload.get("d"):
            raise rejected

        candidate = EvidenceCandidate(
            review_id=review_id,
            evidence_reference=current.evidence_reference,
            evidence_digest=current.evidence_digest,
            broker_result_digest=envelope.result_digest,
            issued_to_subject=actor.subject,
            expires_at=self._clock() + timedelta(seconds=self._candidate_ttl_s),
            correlation_trust=CorrelationTrust.MANUAL_REVIEWER,
        )
        try:
            _link, linked_review = await self._repo.create_evidence_link(
                review_id,
                candidate=candidate,
                reason=reason,
                expected_version=expected_version,
                context=request_context,
            )
        except EvidenceCandidateRejected as exc:
            raise rejected from exc

        # An explicit reviewer link makes the durable correlation `manual`, with
        # its own audit event recording actor and reason. The repository owns the
        # generic, audited field-update primitive; Stage 4's scope does not
        # include editing the Stage 3 repository, so it is reused rather than
        # duplicated here. Promoting it to a public `set_correlation_state` is a
        # one-line Stage 5 follow-up.
        return await self._repo._simple_review_field_update(  # noqa: SLF001
            review_id,
            {
                "correlation_status": CorrelationStatus.MANUAL,
                "correlation_trust": CorrelationTrust.MANUAL_REVIEWER,
                "correlation_source": "reviewer_evidence_link",
            },
            event_type="correlation_status_changed",
            changed_fields=["correlation_status", "correlation_trust", "correlation_source"],
            expected_version=linked_review.version,
            context=MutationContext(
                actor=request_context.actor,
                actor_role=request_context.actor_role,
                request_id=request_context.request_id,
                idempotency_key=(
                    f"{request_context.idempotency_key}:correlation"
                    if request_context.idempotency_key
                    else None
                ),
                reason_code=request_context.reason_code,
            ),
        )

    # -----------------------------------------------------------------
    # Durable review operations
    # -----------------------------------------------------------------

    async def import_review(
        self,
        ticket_ref: str,
        actor: ReviewerIdentity,
        request_context: MutationContext,
    ) -> TicketReview:
        """Create the durable review for a live ticket, idempotently.

        The review is seeded from *identifiers only*. No DevRev title, body, or
        participant name is copied: those stay live/cache data, and the durable
        record carries structured judgment instead.
        """
        _assert_same_actor(actor, request_context)
        try:
            detail = await self._devrev.get_ticket(_validated_ref(ticket_ref))
        except (DevRevNotFoundError, DevRevScopeError) as exc:
            raise TicketNotFound("that ticket is not available") from exc

        review, _created = await self._repo.create_or_get_review(
            TicketReview(
                review_id=review_id_for_devrev_work(detail.devrev_work_id),
                devrev_work_id=detail.devrev_work_id,
                devrev_display_id=detail.devrev_display_id,
                devrev_object_version=detail.object_version,
                status=ReviewStatus.UNREVIEWED,
                last_devrev_sync_at=self._clock(),
            ),
            context=request_context,
        )
        return review

    async def list_reviews(self, query: ReviewListQuery) -> CursorPage[TicketReview]:
        """The Firestore review queue, mapped onto the console page shape."""
        try:
            page = await self._repo.list_reviews(query)
        except UnsupportedFilterCombination as exc:
            raise UnsupportedQuery(str(exc)) from exc
        items = [item for item in page.items if isinstance(item, TicketReview)]
        return CursorPage[TicketReview](
            items=items,
            next_cursor=page.next_cursor,
            page_size=max(page.page_size, len(items)),
        )

    async def patch_review(
        self,
        review_id: str,
        patch: ReviewPatch,
        expected_version: int,
        actor: ReviewerIdentity,
        request_context: MutationContext,
        *,
        admin_reopen: bool = False,
    ) -> TicketReview:
        """Update a review under optimistic concurrency.

        ``admin_reopen`` is threaded through rather than defaulted away: without
        it no admin could ever reopen a resolved or won't-fix review, because the
        transition table refuses those moves by design.
        """
        _assert_same_actor(actor, request_context)
        try:
            return await self._repo.patch_review(
                review_id,
                patch,
                expected_version=expected_version,
                context=request_context,
                admin_reopen=admin_reopen,
            )
        except ReviewNotFound as exc:
            raise TicketNotFound("that review does not exist") from exc


# =====================================================================
# Helpers
# =====================================================================


def _has_filters(query: DevRevTicketFilters) -> bool:
    return any(
        bool(value) for value in query.model_dump(exclude_none=True).values()
    )


def _validated_ref(ticket_ref: str) -> str:
    if not isinstance(ticket_ref, str) or not ticket_ref.strip():
        raise UnsupportedQuery("a ticket reference is required")
    return ticket_ref.strip()


def _normalized_display_id(display_id: str) -> str:
    try:
        return normalize_display_id(display_id)
    except ValueError as exc:
        raise UnsupportedQuery("an exact ticket id lookup needs a ticket id") from exc


def _assert_same_actor(actor: ReviewerIdentity, context: MutationContext) -> None:
    """Refuse a request whose actor and audit context disagree.

    The interface carries both because the API layer resolves them separately;
    letting them drift would record one identity in the audit ledger while
    authorizing another.
    """
    if actor.subject != context.actor.subject:
        raise ServiceError("the authenticated actor and the audit context disagree")


def _bounded_warnings(values: Sequence[str], limit: int = 20) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


__all__ = [
    "CANDIDATE_RATIONALE_UNVERIFIED",
    "CANDIDATE_TOKEN_CONTEXT",
    "DEFAULT_HYDRATION_CONCURRENCY",
    "PLACEHOLDER_NO_BODY",
    "PLACEHOLDER_UNSUPPORTED_BODY_TYPE",
    "PLACEHOLDER_UNSUPPORTED_ENTRY",
    "REASON_BROKER_NOT_CONFIGURED",
    "REASON_NO_DEFENSIBLE_IDENTIFIERS",
    "WARNING_BROKER_UNAVAILABLE",
    "WARNING_CACHE_DEGRADED",
    "WARNING_DEVREV_UNAVAILABLE",
    "WARNING_REVIEW_UNAVAILABLE",
    "WARNING_TICKET_NOT_FOUND",
    "WARNING_TIMELINE_UNAVAILABLE",
    "EvidenceBrokerClient",
    "EvidenceLinkRejected",
    "MessageClassifier",
    "ServiceError",
    "TicketNotFound",
    "TicketReviewService",
    "UnsupportedQuery",
]
