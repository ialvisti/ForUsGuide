"""A browser-drivable console with no cloud, no network, and no real tickets.

This module exists so a person (or a browser automation tool) can exercise the
Stage 6 interface end to end without ever touching production. Everything that
would reach outside this process is removed *before* the application factory is
imported, and the removal is verified rather than assumed:

*   **fixture mode is set and checked at import.** Nothing below runs unless the
    marker is in place, so importing this module can never be a step on the way
    to a real revision;
*   **application default credentials and the metadata server are poisoned.** The
    credential path is made non-existent and the metadata endpoints are pointed
    at a closed loopback port, so a library that quietly reaches for an ambient
    identity fails immediately instead of authenticating as something real;
*   **outbound sockets are refused unless they are loopback.** That is the guard
    that actually holds: it does not matter which library tries, or whether it
    reads an environment variable at all;
*   **there is no real ticket client, repository, or broker.** The durable store
    is the in-memory backend, and the ticket system and evidence broker are
    deterministic in-process fakes. Neither is an HTTP client, so
    ``app.state.evidence_client`` stays ``None`` and no broker address is ever
    resolved, let alone dialled.

Authentication is solved by *injection*, not by widening the product's local-auth
path. That path needs a request header the browser cannot attach to a navigation,
and its five preconditions exist to keep a header-authenticated identity out of a
deployed environment. The application factory already accepts an authenticator,
so the fixture supplies one and the shipped code is untouched.

Deterministic scenarios are reached through the ``stage`` filter on the live tab
and the facet value on the queue tab, both of which a person can type into the
interface. A magic query value is the only trigger a browser can produce without
a header, and driving the failure through the real service, router, and error
envelope is what makes the exercise worth anything.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

FIXTURE_MODE_ENV = "TICKETS_FIXTURE_MODE"
FIXTURE_NONCE_ENV = "TICKETS_FIXTURE_NONCE"
FIXTURE_ROLE_ENV = "TICKETS_FIXTURE_ROLE"
#: Opt-in: start the fixture clock at the current instant instead of frozen.
FIXTURE_CLOCK_ENV = "TICKETS_FIXTURE_CLOCK"
FIXTURE_CLOCK_NOW = "now"
FIXTURE_MODE_VALUE = "1"

#: Where the fixture reports its identity. Present only in fixture mode.
FIXTURE_STATUS_PATH = "/__fixture/status"

#: A closed loopback port. Any library that consults these fails at once rather
#: than reaching a real metadata service.
DEAD_METADATA_ENDPOINT = "127.0.0.1:1"

LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "", "testclient"})


class FixtureIsolationError(RuntimeError):
    """Raised when the fixture is asked to reach something it must not."""


# ---------------------------------------------------------------------------
# Isolation, established before the application factory is imported
# ---------------------------------------------------------------------------


def activate_fixture_mode() -> None:
    """Set the marker, then verify it. Both halves matter.

    Setting it lets a developer import this module directly; verifying it means a
    future edit cannot leave the marker unset and still reach the wiring below.
    """
    os.environ.setdefault(FIXTURE_MODE_ENV, FIXTURE_MODE_VALUE)
    if os.environ.get(FIXTURE_MODE_ENV) != FIXTURE_MODE_VALUE:
        raise FixtureIsolationError(
            f"{FIXTURE_MODE_ENV} must be {FIXTURE_MODE_VALUE!r} before this module loads"
        )


def fixture_mode_active() -> bool:
    return os.environ.get(FIXTURE_MODE_ENV) == FIXTURE_MODE_VALUE


def poison_cloud_credentials() -> None:
    """Make every ambient-credential path fail closed."""
    unreachable = str(Path(os.devnull).parent / "tickets-fixture-absent-credentials.json")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = unreachable
    os.environ["GCE_METADATA_HOST"] = DEAD_METADATA_ENDPOINT
    os.environ["GCE_METADATA_IP"] = DEAD_METADATA_ENDPOINT
    os.environ["GCE_METADATA_ROOT"] = DEAD_METADATA_ENDPOINT
    os.environ["GOOGLE_CLOUD_DISABLE_GRPC"] = "true"
    os.environ["NO_GCE_CHECK"] = "true"


def credentials_are_poisoned() -> bool:
    """Whether the ambient-credential environment is in its refusing state."""
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if path == "" or Path(path).exists():
        return False
    return all(
        os.environ.get(name) == DEAD_METADATA_ENDPOINT
        for name in ("GCE_METADATA_HOST", "GCE_METADATA_IP", "GCE_METADATA_ROOT")
    )


def _host_is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def assert_loopback(address: object) -> None:
    """Refuse any address that is not this machine talking to itself."""
    if isinstance(address, (str, bytes, Path)):
        # A filesystem socket cannot leave the host, and asyncio uses one for
        # its own signalling on some platforms.
        return
    if isinstance(address, tuple) and address and _host_is_loopback(address[0]):
        return
    raise FixtureIsolationError(
        "the fixture console may only connect to loopback; refused a non-loopback address"
    )


_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_guard_installed = False


def install_egress_guard() -> None:
    """Wrap socket connection so a non-loopback destination raises.

    This is the guard that does not depend on any library reading any
    environment variable: the metadata service, a real ticket system, and an
    evidence broker are all unreachable by construction.
    """
    global _guard_installed
    if _guard_installed:
        return

    def connect(self, address):  # noqa: ANN001, ANN202 - a socket method shim
        assert_loopback(address)
        return _ORIGINAL_CONNECT(self, address)

    def connect_ex(self, address):  # noqa: ANN001, ANN202 - a socket method shim
        assert_loopback(address)
        return _ORIGINAL_CONNECT_EX(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    _guard_installed = True


def remove_egress_guard() -> None:
    """Restore the real socket methods. For tests that installed the guard."""
    global _guard_installed
    socket.socket.connect = _ORIGINAL_CONNECT
    socket.socket.connect_ex = _ORIGINAL_CONNECT_EX
    _guard_installed = False


def egress_guard_installed() -> bool:
    return _guard_installed


activate_fixture_mode()
poison_cloud_credentials()
install_egress_guard()

# Imported only after the isolation above is in place and verified.
from api.reviewer_auth import AuthenticatedReviewer, subject_hash  # noqa: E402
from api.ticket_review_models import (  # noqa: E402
    CorrelationStatus,
    CorrelationTrust,
    CursorPage,
    DevRevActor,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketSummary,
    DevRevTimelineEntry,
    ReviewerIdentity,
    ReviewerRole,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
)
from api.ticket_review_routes import (  # noqa: E402
    CODE_FORBIDDEN,
    CODE_RATE_LIMITED,
    CODE_UNAUTHENTICATED,
    CODE_UPSTREAM_PROTOCOL,
    CODE_UPSTREAM_RATE_LIMITED,
    CODE_UPSTREAM_UNAVAILABLE,
    ConsoleHTTPError,
)
from api.tickets_console_config import TicketConsoleSettings  # noqa: E402
from api.tickets_console_main import build_console_app  # noqa: E402
from data_pipeline.ticket_review_repository import (  # noqa: E402
    InMemoryTicketReviewBackend,
    MutationContext,
    TicketReviewRepository,
)
from data_pipeline.ticket_review_service import (  # noqa: E402
    MessageClassifier,
    TicketReviewService,
)

# ---------------------------------------------------------------------------
# Synthetic data. No real participant, ticket, or address appears.
# ---------------------------------------------------------------------------

CURSOR_KEY = bytes(range(32))
CONSOLE_ORIGIN = "http://127.0.0.1:8010"
FIXTURE_CSRF_SECRET = "fixture-csrf-value"  # noqa: S105 - synthetic, fixture only
FIXTURE_DOMAIN = "example.invalid"
FIXTURE_EMAIL = f"fixture-reviewer@{FIXTURE_DOMAIN}"
FIXTURE_SUBJECT = "accounts.google.com:fixture-reviewer"
SYNTHETIC_PART = "don:core:dvrv-us-1:devo/fixture:product/1"
PARTICIPANT_ACTOR = "don:identity:dvrv-us-1:devo/fixture:revu/participant-1"
#: Configured author identities. The service classifies on these and on actor
#: *types*, never on a display name — which is why one fixture entry is called
#: "Support Bot" and is still expected to come back unclassified.
AI_ACTOR = "don:identity:dvrv-us-1:devo/fixture:devu/assistant-1"
SYSTEM_ACTOR = "don:identity:dvrv-us-1:devo/fixture:sysu/workflow-1"
HUMAN_ACTOR = "don:identity:dvrv-us-1:devo/fixture:devu/agent-1"
T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

#: The page size the interface asks for. Mirrors ``DEFAULT_PAGE_SIZE`` in
#: ``ui/tickets/assets/state.js``, which is smaller than the API's own default; a
#: test asserts the two agree, because the whole point of the counts below is to
#: exceed whatever the browser actually requests.
UI_PAGE_SIZE = 25

#: More synthetic tickets than one page holds, so forward and backward paging can
#: actually be exercised in a browser rather than merely unit-tested.
FIXTURE_TICKET_COUNT = 60

#: Every other ticket gets a durable review. Interleaving rather than filling the
#: first block matters: it means *every* page of the live tab carries both an
#: imported row and an unimported one, so the `Add to review queue` action and the
#: `Not reviewed` state are reachable wherever a reviewer happens to be.
SEEDED_TICKET_STRIDE = 2

#: The magic filter values that select a failure. Typed into the interface.
SCENARIO_UNAUTHENTICATED = "fixture:unauthenticated"
SCENARIO_FORBIDDEN = "fixture:forbidden"
SCENARIO_RATE_LIMITED = "fixture:rate-limited"
SCENARIO_UPSTREAM_RATE_LIMITED = "fixture:upstream-rate-limited"
SCENARIO_UNAVAILABLE = "fixture:unavailable"
SCENARIO_PROTOCOL = "fixture:protocol"
SCENARIO_EMPTY = "fixture:empty"
SCENARIO_PARTIAL = "fixture:partial"

_FAILURES: dict[str, ConsoleHTTPError] = {
    SCENARIO_UNAUTHENTICATED: ConsoleHTTPError(
        401, CODE_UNAUTHENTICATED, "authentication is required"
    ),
    SCENARIO_FORBIDDEN: ConsoleHTTPError(403, CODE_FORBIDDEN, "that role may not do this"),
    SCENARIO_RATE_LIMITED: ConsoleHTTPError(
        429, CODE_RATE_LIMITED, "too many requests; slow down", headers={"Retry-After": "8"}
    ),
    # No Retry-After: the upstream bound only carries one when the upstream sent
    # it, which is why the interface needs a fallback backoff.
    SCENARIO_UPSTREAM_RATE_LIMITED: ConsoleHTTPError(
        429, CODE_UPSTREAM_RATE_LIMITED, "the ticket system is rate limiting this console"
    ),
    SCENARIO_UNAVAILABLE: ConsoleHTTPError(
        503, CODE_UPSTREAM_UNAVAILABLE, "the ticket system is unavailable"
    ),
    SCENARIO_PROTOCOL: ConsoleHTTPError(
        502, CODE_UPSTREAM_PROTOCOL, "the ticket system returned an unusable response"
    ),
}

SCENARIOS = tuple(_FAILURES) + (SCENARIO_EMPTY, SCENARIO_PARTIAL)


def _display_id(index: int) -> str:
    return f"FIX-{100 + index}"


def _work_id(index: int) -> str:
    return f"don:core:dvrv-us-1:devo/fixture:ticket/{100 + index}"


def fixture_ticket(index: int) -> DevRevTicketSummary:
    """One synthetic list row. Titles are invented, not borrowed."""
    return DevRevTicketSummary(
        devrev_work_id=_work_id(index),
        devrev_display_id=_display_id(index),
        title=f"Synthetic enquiry {index}: a participant asked about contribution limits",
        stage=("queued", "work_in_progress", "resolved")[index % 3],
        state=("open", "in_progress", "closed")[index % 3],
        applies_to_part=SYNTHETIC_PART,
        ticket_visibility=2,
        source_channel="email",
        subtype="question",
        object_version=1 + index,
        created_at=T0 - timedelta(days=index + 2),
        modified_at=T0 - timedelta(hours=index + 1),
    )


def fixture_detail(index: int) -> DevRevTicketDetail:
    summary = fixture_ticket(index).model_dump()
    summary["body"] = f"Synthetic ticket body {index}. No real participant text appears here."
    summary["timeline_entry_count"] = 2
    return DevRevTicketDetail(**summary)


def fixture_timeline_entry(index: int) -> DevRevTimelineEntry:
    return DevRevTimelineEntry(
        entry_id=f"fixture-entry-{index}",
        object_id=_work_id(index),
        kind=TimelineEntryKind.COMMENT,
        visibility=TimelineVisibility.EXTERNAL,
        body=f"Synthetic message {index}.",
        body_type="text/plain",
        author=DevRevActor(actor_id=PARTICIPANT_ACTOR, actor_type=DevRevActorType.REV_USER),
        created_at=T0 - timedelta(hours=index),
    )


def _conversation_entry(
    ordinal: int,
    *,
    actor_id: Optional[str],
    actor_type: DevRevActorType,
    display_name: Optional[str],
    visibility: TimelineVisibility,
    body: Optional[str],
    kind: TimelineEntryKind = TimelineEntryKind.COMMENT,
    body_type: Optional[str] = "text/plain",
    change_summary: Optional[str] = None,
    unsupported_type: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> DevRevTimelineEntry:
    return DevRevTimelineEntry(
        entry_id=f"fixture-entry-{ordinal}",
        object_id=_work_id(0),
        kind=kind,
        visibility=visibility,
        body=body,
        body_type=body_type,
        author=(
            None
            if actor_id is None
            else DevRevActor(
                actor_id=actor_id, actor_type=actor_type, display_name=display_name
            )
        ),
        in_reply_to=in_reply_to,
        change_summary=change_summary,
        unsupported_type=unsupported_type,
        created_at=T0 - timedelta(hours=20 - ordinal),
    )


#: A long body, so the workspace's collapse control has something to collapse.
_LONG_BODY = (
    "The participant wrote at length about their contribution schedule.\n\n"
    + ("This paragraph exists to exceed the collapse threshold. " * 24)
    + "\n\nAnd a closing paragraph, so paragraph breaks are visible too."
)


def fixture_conversation_pages() -> list[TimelinePage]:
    """Three pages that between them contain every case the UI must distinguish.

    Deliberately a *sequence* rather than one page repeated. Forward-only paging,
    the "more remain" wording, and the empty-page-that-still-has-a-cursor case are
    all properties of the sequence, and a single page proves none of them.

    Page three is empty and still offers a cursor. That is a real upstream answer,
    and a reader that treats it as the end silently hides everything after it.
    """
    return [
        TimelinePage(
            items=[
                _conversation_entry(
                    0,
                    actor_id=PARTICIPANT_ACTOR,
                    actor_type=DevRevActorType.REV_USER,
                    display_name="A Participant",
                    visibility=TimelineVisibility.EXTERNAL,
                    body="How much can I contribute this year?",
                ),
                _conversation_entry(
                    1,
                    actor_id=AI_ACTOR,
                    actor_type=DevRevActorType.DEV_USER,
                    display_name="Assistant",
                    visibility=TimelineVisibility.EXTERNAL,
                    body=_LONG_BODY,
                    in_reply_to="fixture-entry-0",
                ),
                _conversation_entry(
                    2,
                    actor_id=HUMAN_ACTOR,
                    actor_type=DevRevActorType.DEV_USER,
                    display_name="A Human Agent",
                    visibility=TimelineVisibility.INTERNAL,
                    body="Escalating: the limit quoted above is last year's.",
                ),
            ],
            next_cursor="1",
            page_size=25,
        ),
        TimelinePage(
            items=[
                _conversation_entry(
                    3,
                    actor_id=HUMAN_ACTOR,
                    actor_type=DevRevActorType.DEV_USER,
                    display_name="A Human Agent",
                    visibility=TimelineVisibility.EXTERNAL,
                    body="Apologies — the correct limit for this year is different.",
                ),
                _conversation_entry(
                    4,
                    actor_id=SYSTEM_ACTOR,
                    actor_type=DevRevActorType.SYS_USER,
                    display_name="Workflow",
                    visibility=TimelineVisibility.PRIVATE,
                    body=None,
                    kind=TimelineEntryKind.CHANGE_EVENT,
                    body_type=None,
                    change_summary="stage moved from work_in_progress to resolved",
                ),
                _conversation_entry(
                    5,
                    actor_id=None,
                    actor_type=DevRevActorType.UNKNOWN,
                    display_name=None,
                    visibility=TimelineVisibility.PRIVATE,
                    body=None,
                    kind=TimelineEntryKind.UNSUPPORTED,
                    body_type=None,
                    unsupported_type="timeline_shell",
                ),
                _conversation_entry(
                    6,
                    actor_id="don:identity:dvrv-us-1:devo/fixture:devu/ambiguous-1",
                    actor_type=DevRevActorType.UNKNOWN,
                    display_name="Support Bot",
                    visibility=TimelineVisibility.INTERNAL,
                    body="An entry whose author type does not decide who wrote it.",
                ),
            ],
            next_cursor="2",
            page_size=25,
        ),
        TimelinePage(items=[], next_cursor="3", page_size=25),
    ]


class FixtureClock:
    """A monotonic clock. The audit ledger orders by microsecond, so two writes
    frozen at the same instant would produce an undefined order.

    Frozen at :data:`T0` by default, which is what makes every seeded review,
    audit hash and page identical between runs.

    ``TICKETS_FIXTURE_CLOCK=now`` starts it at the current instant instead. That
    exists for one reason: a broker suggestion is sealed with a ten-minute
    expiry, and against a clock frozen in the past *every* suggestion is already
    expired — so the confirmation flow is unreachable from a browser, which
    compares it to the real time. Opt-in, so nothing deterministic changes
    unless a browser session asks for it.
    """

    def __init__(self, start: Optional[datetime] = None) -> None:
        if start is None:
            start = (
                datetime.now(timezone.utc)
                if os.environ.get(FIXTURE_CLOCK_ENV) == FIXTURE_CLOCK_NOW
                else T0
            )
        self.now = start

    def __call__(self) -> datetime:
        current = self.now
        self.now = self.now + timedelta(microseconds=1)
        return current


class FixtureDevRev:
    """A deterministic stand-in for the ticket system. Opens no socket.

    The cursor is a decimal offset, which is enough to prove the console's own
    sealed-token handling: the real remote token is opaque to the console anyway,
    and the sealing, direction binding, and subject binding all happen in the
    shipped router rather than here.
    """

    def __init__(self, count: int = FIXTURE_TICKET_COUNT) -> None:
        self.count = count
        self.list_calls: list[tuple[Optional[str], str, Optional[int]]] = []
        self.timeline_calls: list[tuple[str, Optional[str], Optional[int]]] = []

    async def get_ticket(self, work_id: str) -> DevRevTicketDetail:
        for index in range(self.count):
            if _work_id(index) == work_id or _display_id(index) == work_id:
                return fixture_detail(index)
        from data_pipeline.devrev_client import DevRevNotFoundError

        raise DevRevNotFoundError("no fixture ticket has that reference")

    async def list_tickets(
        self, query: Any, *, cursor: Optional[str] = None, mode: str = "after", limit=None
    ) -> CursorPage[DevRevTicketSummary]:
        self.list_calls.append((cursor, mode, limit))
        size = max(1, min(int(limit or 25), 100))
        stage = list(getattr(query, "stage", []) or [])
        if SCENARIO_EMPTY in stage:
            return CursorPage[DevRevTicketSummary](items=[], page_size=size)
        if SCENARIO_PARTIAL in stage:
            return CursorPage[DevRevTicketSummary](
                items=[fixture_ticket(0)],
                page_size=size,
                partial=True,
                warnings=["devrev_unavailable"],
            )
        offset = _decoded_offset(cursor)
        if mode == "before":
            end = max(0, offset)
            start = max(0, end - size)
        else:
            start = max(0, offset)
            end = min(self.count, start + size)
        items = [fixture_ticket(index) for index in range(start, end)]
        return CursorPage[DevRevTicketSummary](
            items=items,
            page_size=size,
            next_cursor=str(end) if end < self.count else None,
            prev_cursor=str(start) if start > 0 else None,
        )

    async def list_timeline_page(
        self, work_id: str, *, cursor: Optional[str] = None, limit=None
    ) -> TimelinePage:
        """One page of a three-page synthetic conversation.

        Only the first fixture ticket carries the full conversation; every other
        one gets a single entry. That keeps the list view's sixty rows cheap while
        still giving the detail view something worth paging through — and it means
        a browser check has one known ticket to assert exact counts against.
        """
        self.timeline_calls.append((work_id, cursor, limit))
        size = max(1, min(int(limit or 25), 100))
        if work_id not in {_work_id(0), _display_id(0)}:
            return TimelinePage(items=[fixture_timeline_entry(0)], page_size=size)
        pages = fixture_conversation_pages()
        index = _decoded_offset(cursor)
        if index >= len(pages):
            return TimelinePage(items=[], page_size=size)
        page = pages[index]
        return page.model_copy(update={"page_size": size})


class FixtureEvidenceBroker:
    """An in-process stand-in for the evidence broker. Opens no socket.

    It is an object with a ``lookup`` method rather than an HTTP client on
    purpose: ``app.state.evidence_client`` stays ``None``, so the guarantee that
    no real broker client exists in fixture mode is untouched, while the service
    still has something to project into the response.

    Three tickets are treated specially so the panel's three interesting states
    are all reachable from a browser:

    * ``FIX-100`` — one record the producer signed, so the correlation is
      **linked** and the whole execution is on screen;
    * ``FIX-102`` — records with no verified correlation, so they arrive as
      **suggestions** that a reviewer must confirm with a reason;
    * everything else — no records at all, which is the **gap** state, and the one
      most tickets are really in.
    """

    #: 64 hex characters. Recognisable on screen as synthetic, still a valid digest.
    _DIGESTS = {
        "prompt": "a1" * 32,
        "response": "b2" * 32,
        "content": "c3" * 32,
        "request": "d4" * 32,
        "trace": "e5" * 32,
        "result": "f6" * 32,
        "reference": "17" * 32,
    }

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _provenance(self, *, complete: bool):
        from api.ticket_review_models import ObservedChunkRef, RagProvenance

        if not complete:
            return RagProvenance()
        return RagProvenance(
            correlation_status=CorrelationStatus.LINKED,
            correlation_trust=CorrelationTrust.VERIFIED_WORKLOAD,
            correlation_source="ticket_execution_hmac",
            missing_provenance=False,
            index_name="kb-fixture-main",
            # Left unset on purpose: an unknown index version has to render as
            # unknown rather than as a blank beside populated rows.
            index_version=None,
            namespace="articles",
            deployed_revision="kb-rag-fixture-00042",
            prompt_template_id="ticket_answer.v7",
            prompt_template_sha256=self._DIGESTS["prompt"],
            response_sha256=self._DIGESTS["response"],
            observed_chunks=[
                ObservedChunkRef(
                    observed_vector_id=f"fixture-vector-{ordinal}",
                    article_id=f"fixture-article-{200 + ordinal}",
                    content_sha256=self._DIGESTS["content"],
                    chunk_ordinal=ordinal,
                    namespace="articles",
                    score=0.9 - (ordinal / 20),
                )
                for ordinal in range(3)
            ],
        )

    def _record(self, *, verified: bool):
        from api.ticket_review_models import (
            EvidenceSourceCollection,
            MissingProvenance,
            RagEvidenceRecord,
        )

        if verified:
            return RagEvidenceRecord(
                evidence_reference=f"fixture-evidence-{self._DIGESTS['reference'][:24]}",
                evidence_digest=self._DIGESTS["prompt"],
                source_collection=EvidenceSourceCollection.TICKET_EXECUTIONS,
                schema_version=1,
                occurred_at=T0 - timedelta(minutes=42),
                endpoint="/answer",
                route="ticket_answer",
                correlation_source="ticket_execution_hmac",
                correlation_trust=CorrelationTrust.VERIFIED_WORKLOAD,
                lookup_key_version=1,
                ingress_key_version=1,
                internal_job_id="fixture-job-7",
                request_id_hash=self._DIGESTS["request"],
                model="claude-opus-5",
                provider="anthropic",
                config_version="fixture-cfg-12",
                rendered_prompt_trace_sha256=self._DIGESTS["trace"],
                deployed_commit_sha="0" * 40,
                provenance=self._provenance(complete=True),
                source_article_ids=["fixture-article-200", "fixture-article-201"],
                duration_ms=1842.5,
                failed=False,
                missing=[MissingProvenance.INDEX_VERSION],
            )
        return RagEvidenceRecord(
            evidence_reference=f"fixture-legacy-{self._DIGESTS['response'][:24]}",
            evidence_digest=self._DIGESTS["response"],
            source_collection=EvidenceSourceCollection.EXECUTION_LOGS,
            schema_version=0,
            occurred_at=T0 - timedelta(days=200),
            endpoint="/answer",
            correlation_trust=CorrelationTrust.NONE,
            provenance=self._provenance(complete=False),
            duration_ms=990.0,
            failed=True,
            missing=[MissingProvenance.LEGACY_SCHEMA, MissingProvenance.OBSERVED_CHUNKS],
        )

    async def lookup(self, devrev_work_id: str, *, max_results: int):
        from api.ticket_review_models import RagEvidenceEnvelope

        self.calls.append(devrev_work_id)
        if devrev_work_id == _work_id(0):
            records = [self._record(verified=True)]
            status = CorrelationStatus.LINKED
        elif devrev_work_id == _work_id(2):
            records = [self._record(verified=False)]
            status = CorrelationStatus.UNAVAILABLE
        else:
            records = []
            status = CorrelationStatus.UNAVAILABLE
        return RagEvidenceEnvelope(
            correlation_status=status,
            records=records[:max_results],
            result_digest=self._DIGESTS["result"],
            key_versions_queried=[1],
            unavailable_reason=(
                None if records else "no_defensible_identifiers_exist"
            ),
        )


def _decoded_offset(cursor: Optional[str]) -> int:
    if cursor is None:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


class FixtureAuthenticator:
    """Return one fixed verified reviewer, for any request, with no headers.

    This is the seam the application factory already exposes. It exists here
    rather than as a change to the product's local-auth path because that path
    requires a request header a browser cannot send on a navigation, and its
    preconditions are what keep header-supplied identity out of a deployed
    revision.
    """

    def __init__(self, *, email: str = FIXTURE_EMAIL, role: Optional[str] = None) -> None:
        resolved = role or os.environ.get(FIXTURE_ROLE_ENV, ReviewerRole.REVIEWER.value)
        self.role = ReviewerRole(resolved)
        self.identity = ReviewerIdentity(
            subject=FIXTURE_SUBJECT, email=email, display_name="Fixture Reviewer"
        )

    def authenticate(
        self,
        headers: Mapping[str, str],
        *,
        client_host: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> AuthenticatedReviewer:
        del headers, client_host, request_id
        return AuthenticatedReviewer(
            identity=self.identity,
            role=self.role,
            local=True,
            _subject_hash=subject_hash(self.identity.subject),
        )


class ScenarioService:
    """Wrap the real service and raise the requested failure before delegating.

    Wrapping the service rather than adding middleware keeps every scenario on
    the shipped path: the router's exception mapping, the one error envelope, the
    security headers, and the access log all behave exactly as they do in
    production.
    """

    def __init__(self, inner: TicketReviewService) -> None:
        self._inner = inner

    @staticmethod
    def _selected(values: object) -> Optional[ConsoleHTTPError]:
        for value in values if isinstance(values, (list, tuple)) else [values]:
            failure = _FAILURES.get(value if isinstance(value, str) else "")
            if failure is not None:
                return failure
        return None

    async def list_live_tickets(self, query, **kwargs):
        failure = self._selected(list(getattr(query, "stage", []) or []))
        if failure is not None:
            raise failure
        return await self._inner.list_live_tickets(query, **kwargs)

    async def list_reviews(self, query):
        failure = self._selected(list(getattr(query, "facets", {}).values()))
        if failure is not None:
            raise failure
        return await self._inner.list_reviews(query)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def fixture_settings(**overrides: Any) -> TicketConsoleSettings:
    """Settings built from explicit values, never from a file.

    ``_env_file=None`` matters: the repository carries an ignored ``.env`` for
    another service, and letting it load here would make the fixture's behaviour
    depend on an untracked file.
    """
    values: dict[str, Any] = {
        "ENVIRONMENT": "local",
        "AUTH_MODE": "local",
        "ALLOW_LOCAL_AUTH": True,
        "ALLOW_UNBOUND_VIEWERS": False,
        "ENABLE_SYNTHETIC_VERIFICATION": False,
        "ALLOWED_EMAIL_DOMAINS": [FIXTURE_DOMAIN],
        "ROLE_BINDINGS_JSON": f'{{"{FIXTURE_EMAIL}": "admin"}}',
        "CSRF_SIGNING_SECRET": FIXTURE_CSRF_SECRET,
        "CURSOR_AEAD_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
        "GCP_PROJECT": "fixture-project",
        "GCP_REGION": "us-central1",
        "FIRESTORE_DATABASE": "tickets-console-emulator",
        "DEVREV_API_BASE": "http://127.0.0.1:1",
        "ALLOW_NON_OFFICIAL_DEVREV_BASE": True,
        "DEVREV_ALLOWED_PART_DONS": [SYNTHETIC_PART],
        "DEVREV_ALLOWED_TICKET_VISIBILITY_IDS": [2],
        "DEVREV_ALLOWED_TIMELINE_VISIBILITIES": ["internal", "external"],
        # Configured so the classifier can tell the assistant, the workflow and a
        # human agent apart. Without these it reports itself unconfigured and
        # every authored entry collapses into one class, which is exactly the
        # conflation the conversation panel exists to prevent.
        "DEVREV_AI_AUTHOR_IDS": [AI_ACTOR],
        "DEVREV_SYSTEM_AUTHOR_IDS": [SYSTEM_ACTOR],
        "DEVREV_HUMAN_AUTHOR_IDS": [HUMAN_ACTOR],
        "EVIDENCE_BROKER_URL": "",
        "EVIDENCE_BROKER_AUDIENCE": "",
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


#: The status each seeded review ends on. Each one is reached by walking the
#: product's own transition table, one audited patch per step: the table refuses
#: a jump such as `unreviewed` -> `resolved`, and a fixture that faked its way
#: past that would be testing a state the product cannot actually produce.
_SEEDED_STATUSES = (
    ("unreviewed",),
    ("reviewed",),
    ("reviewed", "triaged", "planned", "in_progress"),
    ("reviewed", "triaged", "planned", "in_progress", "changes_proposed", "verifying", "resolved"),
)


def _fixture_actor() -> ReviewerIdentity:
    return ReviewerIdentity(
        subject=FIXTURE_SUBJECT, email=FIXTURE_EMAIL, display_name="Fixture Reviewer"
    )


def _context(label: str) -> MutationContext:
    return MutationContext(
        actor=_fixture_actor(),
        actor_role=ReviewerRole.ADMIN,
        request_id=f"fixture-{label}",
        idempotency_key=f"fixture-key-{label}",
    )


def seeded_ticket_indexes() -> list[int]:
    """Which synthetic tickets have a durable review."""
    return list(range(0, FIXTURE_TICKET_COUNT, SEEDED_TICKET_STRIDE))


async def seed_reviews(
    repository: TicketReviewRepository, indexes: Optional[list[int]] = None
) -> None:
    """Give the queue tab something durable to show.

    Deliberately fewer reviews than tickets, so the live tab carries both
    imported and unimported rows and the ``Add to review queue`` action has a
    subject to act on.
    """
    from api.ticket_review_models import (
        ResolutionOutcome,
        ReviewPatch,
        ReviewResolution,
        TicketReview,
        review_id_for_devrev_work,
    )

    ratings = (1, 3, 5, 2)
    topics = ("Contributions", "Withdrawals", "Enrollment", "Loans")
    observations = ("retrieval_miss", "knowledge_gap", "correct", "source_data")
    severities = ("critical", "medium", "high", "low")

    # The variations cycle on the *position* in the seeded set, not on the ticket
    # index: with a stride of two, cycling on the index would only ever reach the
    # even-numbered patterns and half the states would never be seeded.
    for ordinal, index in enumerate(
        seeded_ticket_indexes() if indexes is None else indexes
    ):
        work_id = _work_id(index)
        review, _ = await repository.create_or_get_review(
            TicketReview(
                review_id=review_id_for_devrev_work(work_id),
                devrev_work_id=work_id,
                devrev_display_id=_display_id(index),
            ),
            context=_context(f"create-{index:02d}"),
        )
        review = await repository.patch_review(
            review.review_id,
            ReviewPatch(
                topic=topics[ordinal % len(topics)],
                legacy_type=("Answer quality", "Missing content")[ordinal % 2],
                rating=ratings[ordinal % len(ratings)],
                comments=(
                    f"Synthetic reviewer note {index}. The assistant answered from the "
                    "wrong article and the participant had to ask twice, which is the "
                    "kind of length this column has to survive without breaking the row."
                ),
                observation_type=observations[ordinal % len(observations)],
                severity=severities[ordinal % len(severities)],
                expected_behavior="Answer from the current plan document on the first reply.",
                # Half the rows carry a name migrated from the spreadsheet and no
                # account, so the interface's labelled legacy fallback is visible
                # next to rows that have a real assignment.
                legacy_reviewer_display_name=None if ordinal % 2 else "Migrated Sheet Reviewer",
                assigned_reviewer=_fixture_actor() if ordinal % 2 else None,
            ),
            expected_version=review.version,
            context=_context(f"fields-{index:02d}"),
        )
        for step, status in enumerate(_SEEDED_STATUSES[ordinal % len(_SEEDED_STATUSES)]):
            if status == "unreviewed":
                continue
            patch = ReviewPatch(status=status)
            if status in {"resolved", "wont_fix"}:
                patch = ReviewPatch(
                    status=status,
                    resolution=ReviewResolution(
                        outcome=ResolutionOutcome.NO_CHANGE,
                        no_change_reason="Synthetic fixture resolution.",
                    ),
                )
            review = await repository.patch_review(
                review.review_id,
                patch,
                expected_version=review.version,
                context=_context(f"status-{index:02d}-{step:02d}"),
            )


def build_fixture_app(**overrides: Any):
    """The whole console, wired to fakes, with a fixture identity injected."""
    if not fixture_mode_active():
        raise FixtureIsolationError("fixture mode is not active")
    if not credentials_are_poisoned():
        raise FixtureIsolationError("ambient cloud credentials are not neutralized")
    if not egress_guard_installed():
        raise FixtureIsolationError("the loopback-only egress guard is not installed")

    settings = fixture_settings(**overrides)
    clock = FixtureClock()
    backend = InMemoryTicketReviewBackend()
    repository = TicketReviewRepository(backend, cursor_key=CURSOR_KEY, clock=clock)
    devrev = FixtureDevRev()
    broker = FixtureEvidenceBroker()
    service = ScenarioService(
        TicketReviewService(
            devrev=devrev,
            repository=repository,
            classifier=MessageClassifier.from_settings(settings),
            candidate_key=CURSOR_KEY,
            # An in-process stand-in, not an HTTP client: `app.state.evidence_client`
            # stays None, so the guarantee that fixture mode reaches no real broker
            # is intact, while the evidence panel still has all three of its states
            # to render — linked, suggested, and an explicit gap.
            broker=broker,
            clock=clock,
        )
    )
    app = build_console_app(
        settings,
        devrev=devrev,
        repository=repository,
        service=service,
        authenticator=FixtureAuthenticator(),
        clock=clock,
        firestore_database="tickets-console-emulator",
    )
    app.state.fixture_repository = repository
    app.state.fixture_devrev = devrev
    app.state.fixture_broker = broker
    _install_fixture_status(app)

    @asynccontextmanager
    async def _fixture_lifespan(_app: Any):
        await seed_reviews(repository)
        yield

    # Set rather than passed: ``build_console_app`` deliberately builds the app
    # without the production lifespan, which is what keeps a real client out of
    # every test.
    app.router.lifespan_context = _fixture_lifespan
    return app


def _install_fixture_status(app: Any) -> None:
    """Publish the nonce the runner uses to prove it found *its* process.

    Only reachable in fixture mode, and it returns nothing but the nonce the
    parent generated: a liveness probe on the right port is not proof of
    identity, because a stale or unrelated listener answers that too.
    """
    if not fixture_mode_active():  # pragma: no cover - guarded by the caller
        return

    from fastapi.responses import JSONResponse

    @app.get(FIXTURE_STATUS_PATH, include_in_schema=False)
    async def fixture_status() -> JSONResponse:
        return JSONResponse(
            {
                "fixture": True,
                "nonce": os.environ.get(FIXTURE_NONCE_ENV, ""),
                "scenarios": list(SCENARIOS),
            },
            headers={"Cache-Control": "no-store"},
        )


__all__ = [
    "CONSOLE_ORIGIN",
    "FIXTURE_MODE_ENV",
    "FIXTURE_NONCE_ENV",
    "FIXTURE_ROLE_ENV",
    "FIXTURE_STATUS_PATH",
    "FIXTURE_TICKET_COUNT",
    "SEEDED_TICKET_STRIDE",
    "UI_PAGE_SIZE",
    "SCENARIOS",
    "SCENARIO_EMPTY",
    "SCENARIO_FORBIDDEN",
    "SCENARIO_PARTIAL",
    "SCENARIO_PROTOCOL",
    "SCENARIO_RATE_LIMITED",
    "SCENARIO_UNAUTHENTICATED",
    "SCENARIO_UNAVAILABLE",
    "SCENARIO_UPSTREAM_RATE_LIMITED",
    "FixtureAuthenticator",
    "FixtureClock",
    "FixtureDevRev",
    "FixtureIsolationError",
    "ScenarioService",
    "assert_loopback",
    "build_fixture_app",
    "credentials_are_poisoned",
    "egress_guard_installed",
    "fixture_mode_active",
    "fixture_settings",
    "install_egress_guard",
    "remove_egress_guard",
    "seed_reviews",
    "seeded_ticket_indexes",
]
