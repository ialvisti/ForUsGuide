"""Stage 2 contracts for the read-only DevRev adapter.

Every test here runs against ``httpx.MockTransport``: no live DevRev request is
ever issued, and no DevRev, Firestore, GCP, or Pinecone write can happen.

The numbered section headers map 1:1 to the twenty-two required behaviors in
``tickets-development-plan/02-devrev-read-client.md``.

Two invariants are asserted repeatedly on purpose:

* the bearer token never appears in an exception, a repr, or a log record;
* the fixtures' synthetic PII (``sam.participant@example.invalid``, the phone
  number in the title) never appears in a warning, a diagnostic, or a log line.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from api.ticket_review_models import (
    DEFAULT_PAGE_SIZE,
    DEVREV_CONNECT_TIMEOUT_S,
    DEVREV_MAX_ENTRIES,
    DEVREV_MAX_PAGES,
    DEVREV_MAX_RESPONSE_BYTES,
    DEVREV_MAX_RETRIES,
    DEVREV_READ_TIMEOUT_S,
    DEVREV_RETRY_AFTER_CAP_S,
    MAX_ATTACHMENTS,
    MAX_CURSOR_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_LIST_ITEMS,
    MAX_MESSAGE_BODY_LENGTH,
    MAX_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    MAX_UPSTREAM_ERROR_BODY_BYTES,
    MAX_WARNINGS,
    CursorPage,
    DevRevActorType,
    DevRevTicketDetail,
    DevRevTicketFilters,
    DevRevTicketSummary,
    DevRevTimelineEntry,
    TimelineEntryKind,
    TimelinePage,
    TimelineVisibility,
)
from api.tickets_console_config import (
    DEVREV_OFFICIAL_API_BASE,
    DEVREV_PINNED_VERSION,
    TicketConsoleSettings,
)
from data_pipeline.devrev_client import (
    ENDPOINT_TIMELINE_LIST,
    ENDPOINT_WORKS_GET,
    ENDPOINT_WORKS_LIST,
    IDEMPOTENT_ENDPOINTS,
    LIST_MODES,
    NON_RETRYABLE_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    TIMELINE_LIST_MODE,
    WORKS_LIST_ALLOWED_TICKET_KEYS,
    WORKS_LIST_ALLOWED_TOP_LEVEL_KEYS,
    DevRevAuthenticationError,
    DevRevCallDiagnostics,
    DevRevClient,
    DevRevConfigurationError,
    DevRevConflictError,
    DevRevError,
    DevRevNotFoundError,
    DevRevPaginationError,
    DevRevPermissionError,
    DevRevProtocolError,
    DevRevRateLimitError,
    DevRevRequestError,
    DevRevResourceLimitError,
    DevRevScopeError,
    DevRevTimelineHydration,
    DevRevTransientError,
    cursor_digest,
    sort_timeline_entries,
)

# =====================================================================
# Constants, fixtures, and helpers
# =====================================================================

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "devrev"

SYNTHETIC_TOKEN = "SYNTHETIC-PAT-DO-NOT-LOG-0123456789"  # noqa: S105 - fake
SYNTHETIC_TICKET_DON = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1234"
SYNTHETIC_TICKET_DON_2 = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1235"
SYNTHETIC_TICKET_DON_3 = "don:core:dvrv-us-1:devo/SYNTHETIC00:ticket/1236"
SYNTHETIC_PART = "don:core:dvrv-us-1:devo/SYNTHETIC00:product/1"
SYNTHETIC_PART_2 = "don:core:dvrv-us-1:devo/SYNTHETIC00:product/2"
SYNTHETIC_PART_UNSCOPED = "don:core:dvrv-us-1:devo/SYNTHETIC00:product/99"
SYNTHETIC_DEV_USER = "don:identity:dvrv-us-1:devo/SYNTHETIC00:devu/11"
SYNTHETIC_REV_USER = "don:identity:dvrv-us-1:devo/SYNTHETIC00:revu/21"
SYNTHETIC_TAG = "don:core:dvrv-us-1:devo/SYNTHETIC00:tag/3"

# Synthetic PII that lives in the fixtures. It must never escape into a
# warning, a diagnostic field, an exception message, or a log record.
FIXTURE_PII = (
    "sam.participant@example.invalid",
    "555-0100",
    "ada.support@example.invalid",
    "Sam Participant",
    "prior employer",
)

FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _load_fixture(name: str) -> dict[str, Any]:
    """Return the ``response`` object of a Stage 1 DevRev fixture."""
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert "SYNTHETIC" in payload["_meta"]["provenance"], "fixtures must stay synthetic"
    return payload["response"]


WORKS_PAGE_1 = _load_fixture("works_list_page_1")
WORKS_PAGE_2 = _load_fixture("works_list_page_2")
WORK_GET = _load_fixture("work_get_ticket")
TIMELINE_EMPTY_WITH_CURSOR = _load_fixture("timeline_page_empty_with_cursor")
TIMELINE_FINAL = _load_fixture("timeline_page_final")


class _Recorder:
    """Ordered response player that records every outgoing request."""

    def __init__(self, *outcomes: Any) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._outcomes:
            raise AssertionError(f"unexpected extra request to {request.url.path}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome

    @property
    def count(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


class _Sleeper:
    """Deterministic async sleeper that records every requested delay."""

    def __init__(self, raises: Optional[BaseException] = None) -> None:
        self.delays: list[float] = []
        self._raises = raises

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self._raises is not None:
            raise self._raises


class _CountingStream:
    """Async byte stream that reports how many chunks were actually pulled."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.pulled = 0

    async def __aiter__(self):  # noqa: ANN204 - httpx duck-types this
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk


def _json_response(payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
    return httpx.Response(200, json=payload, **kwargs)


def _client(handler: Any, **overrides: Any) -> DevRevClient:
    """Build a client whose only transport is the supplied mock handler."""
    kwargs: dict[str, Any] = {
        "token": SYNTHETIC_TOKEN,
        "allowed_part_dons": [SYNTHETIC_PART, SYNTHETIC_PART_2],
        "allowed_ticket_visibility_ids": [1, 2],
        "allowed_timeline_visibilities": ["internal", "external"],
        "transport": httpx.MockTransport(handler) if handler is not None else None,
        "sleep": _Sleeper(),
        "jitter": lambda: 0.0,
        "clock": lambda: FIXED_NOW,
    }
    kwargs.update(overrides)
    return DevRevClient(**kwargs)


def _assert_no_secrets(*texts: str) -> None:
    for text in texts:
        assert SYNTHETIC_TOKEN not in text, "leaked the bearer token"
        for pii in FIXTURE_PII:
            assert pii not in text, f"leaked synthetic PII {pii!r} in {text!r}"


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        f"{record.getMessage()} {getattr(record, 'devrev', '')}" for record in caplog.records
    )


# =====================================================================
# 1. Every request carries auth, accept, pinned version, official origin
# =====================================================================


class TestRequestEnvelope:
    async def test_list_request_headers_and_origin(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()

        request = recorder.requests[0]
        assert request.headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
        assert request.headers["accept"] == "application/json"
        assert request.headers["x-devrev-version"] == DEVREV_PINNED_VERSION
        assert request.headers["x-devrev-version"] == "2022-10-20"
        assert str(request.url).startswith(f"{DEVREV_OFFICIAL_API_BASE}/")
        assert request.url.host == "api.devrev.ai"
        assert request.url.scheme == "https"

    async def test_get_and_timeline_requests_carry_the_same_envelope(self) -> None:
        recorder = _Recorder(_json_response(WORK_GET), _json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            await client.get_ticket(SYNTHETIC_TICKET_DON)
            await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

        assert recorder.count == 2
        for request in recorder.requests:
            assert request.headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
            assert request.headers["accept"] == "application/json"
            assert request.headers["x-devrev-version"] == DEVREV_PINNED_VERSION
            assert request.url.host == "api.devrev.ai"

    async def test_no_beta_scope_header_is_ever_sent(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert "x-devrev-scope" not in recorder.requests[0].headers

    async def test_pinned_version_cannot_be_changed_to_an_unknown_value(self) -> None:
        with pytest.raises(DevRevConfigurationError):
            _client(_Recorder(), api_version="2099-01-01")

    async def test_the_token_never_escapes_through_any_error_or_object_graph(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Drive every failure path and scan everything reachable for the token.

        The distributed ``_assert_no_secrets`` checks cover each path in place;
        this one exists so the invariant is stated once, exhaustively, and fails
        loudly if a future error class starts interpolating request state.
        """
        caplog.set_level(logging.DEBUG, logger="data_pipeline.devrev_client")
        failures: list[BaseException] = []
        outcomes: list[Any] = [
            httpx.Response(400, json={"message": SYNTHETIC_TOKEN}),
            httpx.Response(401, content=SYNTHETIC_TOKEN.encode()),
            httpx.Response(403, json={"message": SYNTHETIC_TOKEN}),
            httpx.Response(404),
            httpx.Response(409),
            httpx.Response(418),
            httpx.Response(302, headers={"Location": f"https://evil.invalid/{SYNTHETIC_TOKEN}"}),
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, json=[1]),
            httpx.ReadTimeout("boom"),
        ]
        for outcome in outcomes:
            recorder = _Recorder(*[outcome] * DEVREV_MAX_RETRIES)
            client = _client(recorder)
            try:
                try:
                    await client.list_tickets(DevRevTicketFilters())
                except DevRevError as exc:
                    failures.append(exc)
                # A closed-client refusal and a scope denial are caller-side.
                for call in (
                    lambda c: c.get_ticket("not a valid id"),
                    lambda c: c.list_tickets(DevRevTicketFilters(ticket_visibility=[99])),
                ):
                    try:
                        await call(client)
                    except DevRevError as exc:
                        failures.append(exc)
            finally:
                await client.aclose()

        assert len(failures) >= len(outcomes)
        surfaces: list[str] = [_log_text(caplog)]
        for error in failures:
            surfaces.extend([str(error), repr(error), json.dumps(error.__dict__, default=repr)])
            if error.__cause__ is not None:
                surfaces.append(repr(error.__cause__))
        client = _client(_Recorder())
        try:
            surfaces.extend([repr(client), str(client), repr(vars(client))])
        finally:
            await client.aclose()
        _assert_no_secrets(*surfaces)

    async def test_client_repr_never_contains_the_token(self) -> None:
        client = _client(_Recorder())
        try:
            assert SYNTHETIC_TOKEN not in repr(client)
            assert SYNTHETIC_TOKEN not in str(client)
        finally:
            await client.aclose()

    async def test_a_token_with_a_newline_is_rejected_before_any_request(self) -> None:
        # A CR/LF in a bearer value is header injection, not a typo.
        for bad in ("abc\ndef", "abc\rdef", "abc def\n", "  "):
            with pytest.raises(DevRevConfigurationError):
                _client(_Recorder(), token=bad)

    async def test_token_may_be_supplied_as_a_secret_str(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder, token=SecretStr(SYNTHETIC_TOKEN))
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.requests[0].headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"

    async def test_client_is_an_async_context_manager(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        async with _client(recorder) as client:
            page = await client.list_tickets(DevRevTicketFilters())
        assert len(page.items) == 2

    async def test_from_settings_wires_the_console_configuration(self, monkeypatch) -> None:
        for name in list(__import__("os").environ):
            if name.startswith("TICKETS_"):
                monkeypatch.delenv(name, raising=False)
        settings = TicketConsoleSettings(
            _env_file=None,
            ENVIRONMENT="local",
            AUTH_MODE="local",
            ALLOW_LOCAL_AUTH=True,
            DEVREV_TOKEN=SYNTHETIC_TOKEN,
            DEVREV_ALLOWED_PART_DONS=[SYNTHETIC_PART],
            DEVREV_ALLOWED_TICKET_VISIBILITY_IDS=[2],
            DEVREV_ALLOWED_TIMELINE_VISIBILITIES=["internal", "external"],
            DEVREV_PAGE_SIZE=25,
        )
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = DevRevClient.from_settings(
            settings,
            transport=httpx.MockTransport(recorder),
            sleep=_Sleeper(),
            jitter=lambda: 0.0,
        )
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.body(0)["limit"] == 25
        assert recorder.body(0)["applies_to_part"] == [SYNTHETIC_PART]


# =====================================================================
# 2. list_tickets always POSTs /works.list with a forced ticket type
# =====================================================================


class TestListTicketsEndpointAndForcedType:
    async def test_uses_post_works_list(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.requests[0].method == "POST"
        assert recorder.requests[0].url.path == "/works.list"
        assert recorder.requests[0].headers["content-type"] == "application/json"

    async def test_type_is_always_forced_to_ticket(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(state=["open"]))
        finally:
            await client.aclose()
        assert recorder.body(0)["type"] == ["ticket"]

    def test_the_filter_model_has_no_type_field_at_all(self) -> None:
        assert "type" not in DevRevTicketFilters.model_fields
        with pytest.raises(ValidationError):
            DevRevTicketFilters(type=["issue"])  # type: ignore[call-arg]

    async def test_a_mapping_filter_carrying_type_fails_before_network_io(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevRequestError):
                await client.list_tickets({"type": ["issue"], "state": ["open"]})
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_an_unknown_filter_key_fails_before_network_io(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevRequestError):
                await client.list_tickets({"title_contains": "rollover"})
        finally:
            await client.aclose()
        assert recorder.count == 0


# =====================================================================
# 3. Structured filters map only an allowlisted wire shape
# =====================================================================


class TestFilterWireShape:
    async def test_full_allowlisted_filter_set_maps_to_the_exact_wire_shape(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        filters = DevRevTicketFilters(
            stage=["triage", "in_development"],
            state=["open"],
            applies_to_part=[SYNTHETIC_PART],
            owned_by=[SYNTHETIC_DEV_USER],
            created_by=[SYNTHETIC_DEV_USER],
            reported_by=[SYNTHETIC_REV_USER],
            tags=[SYNTHETIC_TAG],
            created_date="last_30_days",
            modified_date="last_7_days",
            ticket_source_channel=["email"],
            ticket_subtype=["question"],
            ticket_visibility=[2],
        )
        try:
            await client.list_tickets(filters, cursor="opaque-cursor", mode="before", limit=25)
        finally:
            await client.aclose()

        body = recorder.body(0)
        assert body == {
            "type": ["ticket"],
            # `stage` is DevRev's stage-filter object, not a bare array.
            "stage": {"name": ["triage", "in_development"]},
            "state": ["open"],
            "applies_to_part": [SYNTHETIC_PART],
            "owned_by": [SYNTHETIC_DEV_USER],
            "created_by": [SYNTHETIC_DEV_USER],
            "reported_by": [SYNTHETIC_REV_USER],
            "tags": [SYNTHETIC_TAG],
            "created_date": "last_30_days",
            "modified_date": "last_7_days",
            "ticket": {
                "source_channel": ["email"],
                "subtype": ["question"],
                "visibility": [2],
            },
            "cursor": "opaque-cursor",
            "mode": "before",
            "limit": 25,
        }
        assert set(body) <= WORKS_LIST_ALLOWED_TOP_LEVEL_KEYS
        assert set(body["ticket"]) <= WORKS_LIST_ALLOWED_TICKET_KEYS

    async def test_empty_filters_send_only_scope_type_mode_and_limit(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        body = recorder.body(0)
        assert body["type"] == ["ticket"]
        assert body["mode"] == "after"
        assert body["limit"] == DEFAULT_PAGE_SIZE
        assert "cursor" not in body
        assert "stage" not in body
        # The configured scope is injected even when the caller filters nothing.
        assert body["applies_to_part"] == [SYNTHETIC_PART, SYNTHETIC_PART_2]
        assert body["ticket"] == {"visibility": [1, 2]}

    async def test_ticket_visibility_ids_stay_integers_on_the_wire(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(ticket_visibility=[2]))
        finally:
            await client.aclose()
        visibility = recorder.body(0)["ticket"]["visibility"]
        assert visibility == [2]
        assert all(isinstance(value, int) and not isinstance(value, bool) for value in visibility)

    def test_string_visibility_ids_are_rejected_by_the_filter_model(self) -> None:
        with pytest.raises(ValidationError):
            DevRevTicketFilters(ticket_visibility=["2"])  # type: ignore[list-item]

    async def test_a_ticket_only_filter_still_nests_under_ticket(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(ticket_subtype=["question"]))
        finally:
            await client.aclose()
        body = recorder.body(0)
        assert body["ticket"]["subtype"] == ["question"]
        assert "subtype" not in body
        assert "source_channel" not in body


# =====================================================================
# 4. mode is closed; both cursors survive a forward/back round trip
# =====================================================================


class TestListModeAndCursorRoundTrip:
    def test_mode_is_closed_to_after_and_before(self) -> None:
        assert LIST_MODES == frozenset({"after", "before"})

    async def test_an_unknown_mode_fails_before_network_io(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevRequestError):
                await client.list_tickets(DevRevTicketFilters(), mode="sideways")
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_a_list_page_preserves_both_cursors(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert isinstance(page, CursorPage)
        assert page.next_cursor == "cursor-works-page-2"
        assert page.prev_cursor is None
        assert page.truncated is False
        assert page.partial is False

    async def test_forward_then_backward_round_trip(self) -> None:
        recorder = _Recorder(
            _json_response(WORKS_PAGE_1),
            _json_response(WORKS_PAGE_2),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder)
        try:
            first = await client.list_tickets(DevRevTicketFilters())
            second = await client.list_tickets(
                DevRevTicketFilters(), cursor=first.next_cursor, mode="after"
            )
            back = await client.list_tickets(
                DevRevTicketFilters(), cursor=second.prev_cursor, mode="before"
            )
        finally:
            await client.aclose()

        assert [item.devrev_display_id for item in first.items] == ["TKT-1234", "TKT-1235"]
        assert [item.devrev_display_id for item in second.items] == ["TKT-1236"]
        assert second.next_cursor is None
        assert second.prev_cursor == "cursor-works-page-1"
        assert [item.devrev_display_id for item in back.items] == ["TKT-1234", "TKT-1235"]

        assert "cursor" not in recorder.body(0)
        assert recorder.body(1)["cursor"] == "cursor-works-page-2"
        assert recorder.body(1)["mode"] == "after"
        assert recorder.body(2)["cursor"] == "cursor-works-page-1"
        assert recorder.body(2)["mode"] == "before"

    async def test_cursors_are_opaque_and_never_parsed(self) -> None:
        opaque = "!!not-base64!!  {}"
        recorder = _Recorder(_json_response(WORKS_PAGE_2))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(), cursor=opaque)
        finally:
            await client.aclose()
        assert recorder.body(0)["cursor"] == opaque

    async def test_an_oversized_caller_cursor_fails_before_network_io(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevRequestError):
                await client.list_tickets(
                    DevRevTicketFilters(), cursor="c" * (MAX_CURSOR_LENGTH + 1)
                )
            with pytest.raises(DevRevRequestError):
                await client.list_tickets(DevRevTicketFilters(), cursor="  ")
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_an_oversized_remote_cursor_is_a_protocol_error(self) -> None:
        payload = {"works": [], "next_cursor": "c" * (MAX_CURSOR_LENGTH + 1)}
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()

    async def test_limit_is_bounded_by_the_canonical_maximum(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            for bad in (0, -1, MAX_PAGE_SIZE + 1):
                with pytest.raises(DevRevRequestError):
                    await client.list_tickets(DevRevTicketFilters(), limit=bad)
        finally:
            await client.aclose()
        assert recorder.count == 0


# =====================================================================
# 5. get_ticket: bounded identifier, GET /works.get, enforced scope
# =====================================================================


class TestGetTicket:
    async def test_accepts_a_don_and_calls_works_get(self) -> None:
        recorder = _Recorder(_json_response(WORK_GET))
        client = _client(recorder)
        try:
            detail = await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        request = recorder.requests[0]
        assert request.method == "GET"
        assert request.url.path == "/works.get"
        assert request.url.params["id"] == SYNTHETIC_TICKET_DON
        assert isinstance(detail, DevRevTicketDetail)
        assert detail.devrev_work_id == SYNTHETIC_TICKET_DON
        assert detail.devrev_display_id == "TKT-1234"

    async def test_accepts_a_display_id(self) -> None:
        recorder = _Recorder(_json_response(WORK_GET))
        client = _client(recorder)
        try:
            await client.get_ticket("TKT-1234")
        finally:
            await client.aclose()
        assert recorder.requests[0].url.params["id"] == "TKT-1234"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "don:" + "x" * 400,
            "T" * 200,
            "TKT-1234\nX-Injected: 1",
            "not a valid id",
            "don:core:dvrv-us-1:devo/SYNTHETIC00\x00:ticket/1",
            "../../etc/passwd",
            "https://evil.example.invalid/works.get",
        ],
    )
    async def test_rejects_unbounded_or_malformed_identifiers(self, bad: str) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevRequestError):
                await client.get_ticket(bad)
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_normalizes_the_full_detail_object(self) -> None:
        recorder = _Recorder(_json_response(WORK_GET))
        client = _client(recorder)
        try:
            detail = await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert detail.stage == "triage"
        assert detail.state == "open"
        assert detail.severity == "medium"
        assert detail.applies_to_part == SYNTHETIC_PART
        assert detail.ticket_visibility == 2
        assert detail.source_channel == "email"
        assert detail.subtype == "question"
        assert detail.object_version == 4
        assert detail.owner_ids == [SYNTHETIC_DEV_USER]
        assert detail.tag_ids == [SYNTHETIC_TAG]
        assert detail.reporter is not None
        assert detail.reporter.actor_id == SYNTHETIC_REV_USER
        assert detail.reporter.actor_type is DevRevActorType.REV_USER
        assert detail.created_at == datetime(2026, 5, 4, 14, 12, 7, tzinfo=timezone.utc)
        assert detail.modified_at == datetime(2026, 5, 6, 9, 31, 44, tzinfo=timezone.utc)
        assert detail.body is not None and detail.body.startswith("Participant asks")
        assert detail.attachments == []

    async def test_an_out_of_scope_part_is_a_typed_scope_denial(self) -> None:
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["applies_to_part"]["id"] = SYNTHETIC_PART_UNSCOPED
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError) as excinfo:
                await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert isinstance(excinfo.value, DevRevPermissionError)
        _assert_no_secrets(str(excinfo.value))

    async def test_an_out_of_scope_ticket_visibility_is_a_typed_scope_denial(self) -> None:
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["ticket"]["visibility"] = 4
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError):
                await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

    async def test_a_missing_part_or_visibility_fails_closed(self) -> None:
        for mutate in (
            lambda p: p["work"].pop("applies_to_part"),
            lambda p: p["work"]["ticket"].pop("visibility"),
            lambda p: p["work"].pop("ticket"),
        ):
            payload = json.loads(json.dumps(WORK_GET))
            mutate(payload)
            recorder = _Recorder(_json_response(payload))
            client = _client(recorder)
            try:
                with pytest.raises(DevRevScopeError):
                    await client.get_ticket(SYNTHETIC_TICKET_DON)
            finally:
                await client.aclose()

    async def test_a_non_ticket_work_object_is_out_of_scope(self) -> None:
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["type"] = "issue"
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError):
                await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

    async def test_an_oversized_title_and_body_are_truncated_not_dropped(self) -> None:
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["title"] = "T" * (MAX_TITLE_LENGTH + 500)
        payload["work"]["body"] = "B" * (MAX_MESSAGE_BODY_LENGTH + 500)
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            detail = await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert detail.title is not None and len(detail.title) == MAX_TITLE_LENGTH
        assert detail.body is not None and len(detail.body) == MAX_MESSAGE_BODY_LENGTH

    async def test_a_missing_work_object_is_a_protocol_error(self) -> None:
        recorder = _Recorder(_json_response({"not_work": {}}))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()


# =====================================================================
# 6. list_timeline_page: forward-only, bounded, typed page
# =====================================================================


class TestTimelinePage:
    def test_timeline_mode_is_frozen_forward_only(self) -> None:
        assert TIMELINE_LIST_MODE == "after"

    async def test_always_sends_mode_after_and_the_visibility_allowlist(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        request = recorder.requests[0]
        assert request.method == "GET"
        assert request.url.path == "/timeline-entries.list"
        assert request.url.params["object"] == SYNTHETIC_TICKET_DON
        assert request.url.params["mode"] == "after"
        assert request.url.params.get_list("visibility") == ["internal", "external"]
        assert request.url.params["limit"] == str(DEFAULT_PAGE_SIZE)
        assert "cursor" not in request.url.params

    async def test_list_timeline_page_has_no_mode_parameter(self) -> None:
        import inspect

        signature = inspect.signature(DevRevClient.list_timeline_page)
        assert "mode" not in signature.parameters

    async def test_returns_one_bounded_page_with_cursors_and_flags(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_EMPTY_WITH_CURSOR))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON, limit=10)
        finally:
            await client.aclose()
        assert isinstance(page, TimelinePage)
        assert page.items == []
        assert page.next_cursor == "cursor-timeline-page-2"
        assert page.page_size == 10
        assert page.truncated is False
        assert page.partial is False
        assert isinstance(page.warnings, list)
        assert len(page.warnings) <= MAX_WARNINGS

    async def test_normalizes_comment_change_event_and_unsupported_entries(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

        assert [entry.kind for entry in page.items] == [
            TimelineEntryKind.COMMENT,
            TimelineEntryKind.COMMENT,
            TimelineEntryKind.COMMENT,
            TimelineEntryKind.CHANGE_EVENT,
            TimelineEntryKind.UNSUPPORTED,
        ]
        first = page.items[0]
        assert first.entry_id.endswith("timeline_event/1")
        assert first.object_id == SYNTHETIC_TICKET_DON
        assert first.visibility is TimelineVisibility.EXTERNAL
        assert first.body_type == "text"
        assert first.author is not None
        assert first.author.actor_id == SYNTHETIC_REV_USER
        assert first.author.actor_type is DevRevActorType.REV_USER
        assert first.created_at == datetime(2026, 5, 4, 14, 12, 7, tzinfo=timezone.utc)

    async def test_a_change_event_is_never_an_authored_reply(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        change = next(e for e in page.items if e.kind is TimelineEntryKind.CHANGE_EVENT)
        assert change.body is None
        assert change.author is None
        assert change.change_summary is not None
        assert "stage" in change.change_summary

    async def test_the_client_never_classifies_human_ai_or_participant(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        # The synthetic "ForUs RAG Assistant" entry keeps its raw DevRev actor
        # type; Stage 4 owns AI/human/participant classification.
        assistant = page.items[2]
        assert assistant.author is not None
        assert assistant.author.actor_type is DevRevActorType.DEV_USER
        for entry in page.items:
            assert not hasattr(entry, "is_ai")
            assert not hasattr(entry, "is_participant")

    async def test_an_unknown_entry_type_is_preserved_as_bounded_unsupported(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        unsupported = page.items[-1]
        assert unsupported.kind is TimelineEntryKind.UNSUPPORTED
        assert unsupported.unsupported_type == "timeline_future_entry_type_v9"
        assert unsupported.body is None
        assert unsupported.author is None
        # No remote visibility -> the fail-closed default, and it is retained.
        assert unsupported.visibility is TimelineVisibility.PRIVATE

    async def test_an_entry_with_an_out_of_allowlist_visibility_is_dropped(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"][0]["visibility"] = "public"
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert len(page.items) == 4
        assert page.partial is True
        assert any("out_of_scope" in warning for warning in page.warnings)
        _assert_no_secrets(*page.warnings)

    async def test_an_entry_for_another_object_is_dropped(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"][0]["object"] = SYNTHETIC_TICKET_DON_2
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert len(page.items) == 4
        assert page.partial is True

    async def test_an_undated_entry_is_dropped_with_a_bounded_warning(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"][0].pop("created_date")
        payload["timeline_entries"][0].pop("modified_date")
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert len(page.items) == 4
        assert page.partial is True
        assert any("undated" in warning for warning in page.warnings)

    async def test_a_malformed_created_date_falls_back_to_modified_date(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"][0]["created_date"] = "not-a-date"
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert len(page.items) == 5
        assert page.items[0].created_at == datetime(2026, 5, 4, 14, 12, 7, tzinfo=timezone.utc)
        assert any("fallback" in warning for warning in page.warnings)

    async def test_the_timeline_cursor_is_forwarded_verbatim(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            await client.list_timeline_page(SYNTHETIC_TICKET_DON, cursor="cursor-timeline-page-2")
        finally:
            await client.aclose()
        assert recorder.requests[0].url.params["cursor"] == "cursor-timeline-page-2"

    async def test_page_order_is_the_remote_order(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"].reverse()
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        # The adapter never reorders a page; hydration sorting is separate.
        assert page.items[0].kind is TimelineEntryKind.UNSUPPORTED

    async def test_a_missing_timeline_entries_key_is_a_protocol_error(self) -> None:
        recorder = _Recorder(_json_response({"entries": []}))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

    async def test_more_remote_items_than_the_page_maximum_is_a_resource_limit(self) -> None:
        entry = TIMELINE_FINAL["timeline_entries"][0]
        entries = []
        for index in range(MAX_PAGE_SIZE + 1):
            clone = json.loads(json.dumps(entry))
            clone["id"] = f"{entry['id']}-{index}"
            entries.append(clone)
        recorder = _Recorder(_json_response({"timeline_entries": entries, "next_cursor": None}))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()


# =====================================================================
# 7 & 8. Iteration continues past an empty page and stops only on a
#        missing next_cursor
# =====================================================================


class TestIterationTermination:
    async def test_continues_after_an_empty_page_that_still_has_a_cursor(self) -> None:
        recorder = _Recorder(
            _json_response(TIMELINE_EMPTY_WITH_CURSOR),
            _json_response(TIMELINE_FINAL),
        )
        client = _client(recorder)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert recorder.count == 2
        assert len(entries) == 5
        assert recorder.requests[1].url.params["cursor"] == "cursor-timeline-page-2"

    async def test_a_short_page_with_a_cursor_is_not_terminal(self) -> None:
        short = {
            "timeline_entries": [TIMELINE_FINAL["timeline_entries"][0]],
            "next_cursor": "cursor-timeline-page-2",
        }
        recorder = _Recorder(_json_response(short), _json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert recorder.count == 2
        # entry/1 repeats on the second page and is deduplicated.
        assert len(entries) == 5

    @pytest.mark.parametrize("terminal", [None, "", {"omitted": True}])
    async def test_iteration_stops_only_when_next_cursor_is_absent(self, terminal: Any) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        if isinstance(terminal, dict):
            payload.pop("next_cursor")
        else:
            payload["next_cursor"] = terminal
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert recorder.count == 1
        assert len(entries) == 5

    async def test_an_all_empty_final_page_terminates(self) -> None:
        recorder = _Recorder(_json_response({"timeline_entries": [], "next_cursor": None}))
        client = _client(recorder)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert entries == []
        assert recorder.count == 1


# =====================================================================
# 9. A repeated cursor raises before an infinite loop
# =====================================================================


class TestRepeatedCursor:
    async def test_a_self_referential_cursor_raises_a_pagination_error(self) -> None:
        loop_page = {
            "timeline_entries": [TIMELINE_FINAL["timeline_entries"][0]],
            "next_cursor": "cursor-loop",
        }
        recorder = _Recorder(
            _json_response(loop_page),
            _json_response(loop_page),
            _json_response(loop_page),
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevPaginationError):
                async for _ in client.iter_timeline_entries(SYNTHETIC_TICKET_DON):
                    pass
        finally:
            await client.aclose()
        # Two requests: the first page, then the page fetched with "cursor-loop".
        # The third repeat is refused before any further network I/O.
        assert recorder.count == 2

    async def test_an_immediately_self_referential_first_cursor_raises(self) -> None:
        recorder = _Recorder(
            _json_response({"timeline_entries": [], "next_cursor": "cursor-A"}),
            _json_response({"timeline_entries": [], "next_cursor": "cursor-A"}),
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevPaginationError):
                async for _ in client.iter_timeline_entries(SYNTHETIC_TICKET_DON):
                    pass
        finally:
            await client.aclose()
        assert recorder.count == 2

    async def test_a_pagination_error_message_carries_no_cursor_value(self) -> None:
        loop_page = {"timeline_entries": [], "next_cursor": "cursor-secret-loop"}
        recorder = _Recorder(_json_response(loop_page), _json_response(loop_page))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevPaginationError) as excinfo:
                async for _ in client.iter_timeline_entries(SYNTHETIC_TICKET_DON):
                    pass
        finally:
            await client.aclose()
        assert "cursor-secret-loop" not in str(excinfo.value)


# =====================================================================
# 10. max_pages / max_entries yield a typed partial result, never
#     something a public model calls "complete"
# =====================================================================


class TestIteratorGuards:
    def test_the_canonical_guards_are_the_plan_defaults(self) -> None:
        assert DEVREV_MAX_PAGES == 100
        assert DEVREV_MAX_ENTRIES == 5_000

    async def test_max_pages_stops_the_iterator_and_marks_it_truncated(self) -> None:
        page = {
            "timeline_entries": [TIMELINE_FINAL["timeline_entries"][0]],
            "next_cursor": "cursor-next",
        }
        recorder = _Recorder(_json_response(page))
        client = _client(recorder, max_pages=1)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert recorder.count == 1
        assert len(entries) == 1
        diagnostics = client.last_diagnostics
        assert diagnostics is not None
        assert diagnostics.truncated is True
        assert diagnostics.pages == 1

    async def test_max_entries_stops_the_iterator(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder, max_entries=2)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert len(entries) == 2
        assert client.last_diagnostics is not None
        assert client.last_diagnostics.truncated is True

    async def test_load_timeline_returns_a_typed_partial_result(self) -> None:
        page = {
            "timeline_entries": [TIMELINE_FINAL["timeline_entries"][0]],
            "next_cursor": "cursor-next",
        }
        recorder = _Recorder(_json_response(page))
        client = _client(recorder, max_pages=1)
        try:
            hydration = await client.load_timeline(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert isinstance(hydration, DevRevTimelineHydration)
        assert hydration.truncated is True
        assert hydration.partial is True
        assert len(hydration.entries) == 1
        assert any("max_pages" in warning for warning in hydration.warnings)

    async def test_strict_mode_raises_a_typed_resource_limit_error(self) -> None:
        page = {
            "timeline_entries": [TIMELINE_FINAL["timeline_entries"][0]],
            "next_cursor": "cursor-next",
        }
        recorder = _Recorder(_json_response(page))
        client = _client(recorder, max_pages=1)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.load_timeline(SYNTHETIC_TICKET_DON, strict=True)
        finally:
            await client.aclose()

    async def test_strict_iteration_raises_when_max_entries_trips(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder, max_entries=2)
        try:
            with pytest.raises(DevRevResourceLimitError):
                async for _ in client.iter_timeline_entries(SYNTHETIC_TICKET_DON, strict=True):
                    pass
        finally:
            await client.aclose()

    @pytest.mark.parametrize("guard", ["max_pages", "max_entries"])
    @pytest.mark.parametrize("bad", [0, -1, True, 2.0, "5"])
    async def test_an_invalid_per_call_guard_is_refused_not_ignored(
        self, guard: str, bad: Any
    ) -> None:
        # Silently substituting the configured default for an explicit 0 would
        # turn "load nothing" into "load everything".
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevRequestError):
                async for _ in client.iter_timeline_entries(
                    SYNTHETIC_TICKET_DON, **{guard: bad}
                ):
                    pass
            with pytest.raises(DevRevRequestError):
                await client.load_timeline(SYNTHETIC_TICKET_DON, **{guard: bad})
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_a_per_call_guard_cannot_exceed_the_configured_ceiling(self) -> None:
        recorder = _Recorder()
        client = _client(recorder, max_pages=2, max_entries=10)
        try:
            with pytest.raises(DevRevRequestError):
                await client.load_timeline(SYNTHETIC_TICKET_DON, max_pages=3)
            with pytest.raises(DevRevRequestError):
                await client.load_timeline(SYNTHETIC_TICKET_DON, max_entries=11)
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_a_fully_loaded_timeline_is_not_partial(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            hydration = await client.load_timeline(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert hydration.truncated is False
        assert hydration.partial is False
        assert len(hydration.entries) == 5

    def test_no_public_model_labels_a_bounded_result_complete(self) -> None:
        import data_pipeline.devrev_client as module

        forbidden = {"complete", "is_complete", "completed", "all_loaded", "exhaustive"}
        for name in dir(module):
            if name.startswith("_"):
                continue
            candidate = getattr(module, name)
            fields: set[str] = set()
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                fields = set(candidate.model_fields)
            elif dataclasses.is_dataclass(candidate):
                fields = {f.name for f in dataclasses.fields(candidate)}
            else:
                continue
            assert not (fields & forbidden), f"{name} labels a bounded result complete"
            for attribute in forbidden:
                assert not isinstance(
                    getattr(candidate, attribute, None), property
                ), f"{name}.{attribute} labels a bounded result complete"

    async def test_duplicate_entry_ids_are_deduplicated_in_source_order(self) -> None:
        first = json.loads(json.dumps(TIMELINE_FINAL))
        first["next_cursor"] = "cursor-timeline-page-2"
        recorder = _Recorder(_json_response(first), _json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        ids = [entry.entry_id for entry in entries]
        assert len(ids) == len(set(ids)) == 5
        assert ids[0].endswith("timeline_event/1")
        assert ids[-1].endswith("timeline_event/5")
        assert client.last_diagnostics is not None
        assert client.last_diagnostics.duplicate_entries == 5


# =====================================================================
# 11. 429 honors integer and HTTP-date Retry-After, capped at 60 s
# =====================================================================


class TestRateLimitRetryAfter:
    def test_the_canonical_cap_is_sixty_seconds(self) -> None:
        assert DEVREV_RETRY_AFTER_CAP_S == 60

    async def test_an_integer_retry_after_is_honored(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder, sleep=sleeper)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [7.0]
        assert len(page.items) == 2
        assert client.last_diagnostics is not None
        assert client.last_diagnostics.attempts == 2

    async def test_an_http_date_retry_after_is_honored(self) -> None:
        sleeper = _Sleeper()
        # FIXED_NOW is 2026-05-10T12:00:00Z; the date below is 12 s later.
        recorder = _Recorder(
            httpx.Response(429, headers={"Retry-After": "Sun, 10 May 2026 12:00:12 GMT"}),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder, sleep=sleeper)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [12.0]

    async def test_retry_after_is_capped_at_the_canonical_sixty_seconds(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            httpx.Response(429, headers={"Retry-After": "9999"}),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder, sleep=sleeper)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [float(DEVREV_RETRY_AFTER_CAP_S)]

    async def test_a_past_http_date_retry_after_becomes_zero(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            httpx.Response(429, headers={"Retry-After": "Sun, 10 May 2026 11:59:00 GMT"}),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder, sleep=sleeper)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [0.0]

    async def test_a_garbage_retry_after_falls_back_to_computed_backoff(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            httpx.Response(429, headers={"Retry-After": "soon-ish"}),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder, sleep=sleeper, backoff_base_s=0.5)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [0.5]

    async def test_an_exhausted_429_raises_a_typed_rate_limit_error(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            *[httpx.Response(429, headers={"Retry-After": "1"}) for _ in range(DEVREV_MAX_RETRIES)]
        )
        client = _client(recorder, sleep=sleeper)
        try:
            with pytest.raises(DevRevRateLimitError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == DEVREV_MAX_RETRIES
        assert len(sleeper.delays) == DEVREV_MAX_RETRIES - 1
        assert excinfo.value.status == 429
        assert excinfo.value.retry_after_s == 1.0
        _assert_no_secrets(str(excinfo.value), repr(excinfo.value))

    async def test_a_429_without_retry_after_uses_backoff(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(httpx.Response(429), _json_response(WORKS_PAGE_1))
        client = _client(recorder, sleep=sleeper, backoff_base_s=0.25)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [0.25]


# =====================================================================
# 12. 500 / 503 retry with exponential backoff plus bounded jitter
# =====================================================================


class TestTransientRetries:
    def test_the_retryable_and_terminal_status_sets_are_closed(self) -> None:
        assert RETRYABLE_STATUS_CODES == frozenset({429, 500, 503})
        assert NON_RETRYABLE_STATUS_CODES == frozenset({400, 401, 403, 404, 409})
        assert not RETRYABLE_STATUS_CODES & NON_RETRYABLE_STATUS_CODES

    @pytest.mark.parametrize("status", [500, 503])
    async def test_a_transient_status_retries_then_succeeds(self, status: int) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(httpx.Response(status), _json_response(WORKS_PAGE_1))
        client = _client(recorder, sleep=sleeper)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert len(page.items) == 2
        assert recorder.count == 2
        assert len(sleeper.delays) == 1

    async def test_backoff_is_exponential_with_deterministic_jitter(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            httpx.Response(500), httpx.Response(500), _json_response(WORKS_PAGE_1)
        )
        client = _client(
            recorder, sleep=sleeper, backoff_base_s=1.0, jitter=lambda: 0.5, max_retries=3
        )
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        # base * 2**(attempt-1) + jitter_fraction * base * 2**(attempt-1)
        assert sleeper.delays == [1.5, 3.0]

    async def test_jitter_is_bounded_and_never_negative(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(httpx.Response(503), _json_response(WORKS_PAGE_1))
        client = _client(recorder, sleep=sleeper, backoff_base_s=1.0, jitter=lambda: 1.0)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [2.0]
        assert all(delay >= 0.0 for delay in sleeper.delays)

    async def test_backoff_never_exceeds_the_retry_after_cap(self) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(
            httpx.Response(500), httpx.Response(500), _json_response(WORKS_PAGE_1)
        )
        client = _client(
            recorder, sleep=sleeper, backoff_base_s=1000.0, jitter=lambda: 1.0, max_retries=3
        )
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert sleeper.delays == [float(DEVREV_RETRY_AFTER_CAP_S)] * 2

    async def test_an_exhausted_transient_status_raises_a_transient_error(self) -> None:
        recorder = _Recorder(*[httpx.Response(503) for _ in range(DEVREV_MAX_RETRIES)])
        client = _client(recorder)
        try:
            with pytest.raises(DevRevTransientError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert excinfo.value.status == 503
        assert excinfo.value.attempts == DEVREV_MAX_RETRIES

    async def test_an_unexpected_status_is_a_protocol_error(self) -> None:
        recorder = _Recorder(httpx.Response(418))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == 1


# =====================================================================
# 13. Terminal client errors map to typed exceptions with safe messages
# =====================================================================


class TestTerminalErrors:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, DevRevProtocolError),
            (401, DevRevAuthenticationError),
            (403, DevRevPermissionError),
            (404, DevRevNotFoundError),
            (409, DevRevConflictError),
        ],
    )
    async def test_each_terminal_status_is_typed_and_not_retried(
        self, status: int, expected: type[DevRevError]
    ) -> None:
        leaky = {"message": f"participant {FIXTURE_PII[0]} not allowed; token {SYNTHETIC_TOKEN}"}
        recorder = _Recorder(httpx.Response(status, json=leaky))
        client = _client(recorder)
        try:
            with pytest.raises(expected) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == 1
        error = excinfo.value
        assert error.status == status
        assert error.attempts == 1
        assert error.endpoint == ENDPOINT_WORKS_LIST
        assert error.upstream_error_redacted is True
        _assert_no_secrets(str(error), repr(error))

    async def test_terminal_errors_apply_to_every_mvp_endpoint(self) -> None:
        for call in (
            lambda c: c.get_ticket(SYNTHETIC_TICKET_DON),
            lambda c: c.list_timeline_page(SYNTHETIC_TICKET_DON),
        ):
            recorder = _Recorder(httpx.Response(401, json={"message": "bad token"}))
            client = _client(recorder)
            try:
                with pytest.raises(DevRevAuthenticationError):
                    await call(client)
            finally:
                await client.aclose()
            assert recorder.count == 1

    async def test_every_typed_error_descends_from_devrev_error(self) -> None:
        for cls in (
            DevRevAuthenticationError,
            DevRevPermissionError,
            DevRevNotFoundError,
            DevRevConflictError,
            DevRevRateLimitError,
            DevRevTransientError,
            DevRevProtocolError,
            DevRevPaginationError,
            DevRevResourceLimitError,
            DevRevScopeError,
            DevRevRequestError,
            DevRevConfigurationError,
        ):
            assert issubclass(cls, DevRevError)
            assert issubclass(cls, Exception)
        assert issubclass(DevRevScopeError, DevRevPermissionError)


# =====================================================================
# 14. Transport failures retry only for idempotent operations
# =====================================================================


class TestTransportRetries:
    def test_every_mvp_endpoint_is_declared_idempotent(self) -> None:
        assert IDEMPOTENT_ENDPOINTS == frozenset(
            {ENDPOINT_WORKS_LIST, ENDPOINT_WORKS_GET, ENDPOINT_TIMELINE_LIST}
        )

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.ConnectError("connection refused"),
            httpx.WriteTimeout("write timed out"),
            httpx.PoolTimeout("pool exhausted"),
            httpx.ReadError("reset"),
        ],
    )
    async def test_a_transport_failure_is_retried_for_a_read(
        self, failure: httpx.TransportError
    ) -> None:
        sleeper = _Sleeper()
        recorder = _Recorder(failure, _json_response(WORKS_PAGE_1))
        client = _client(recorder, sleep=sleeper)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert len(page.items) == 2
        assert recorder.count == 2
        assert len(sleeper.delays) == 1

    async def test_an_exhausted_transport_failure_is_a_transient_error(self) -> None:
        recorder = _Recorder(
            *[httpx.ReadTimeout("read timed out") for _ in range(DEVREV_MAX_RETRIES)]
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevTransientError) as excinfo:
                await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert excinfo.value.attempts == DEVREV_MAX_RETRIES
        assert excinfo.value.status is None
        _assert_no_secrets(str(excinfo.value))

    async def test_a_non_idempotent_operation_is_never_retried(self) -> None:
        # No MVP operation is a write. The invariant is locked at the transport
        # layer so a future non-read cannot inherit read retry semantics.
        recorder = _Recorder(httpx.ReadTimeout("read timed out"))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevTransientError):
                await client._request_json(
                    "POST",
                    "/works.create",
                    endpoint="works.create",
                    json_body={},
                    idempotent=False,
                )
        finally:
            await client.aclose()
        assert recorder.count == 1


# =====================================================================
# 15. Error bodies are capped at 4 KiB and never carry the token
# =====================================================================


class TestErrorBodyHandling:
    def test_the_canonical_error_body_cap(self) -> None:
        assert MAX_UPSTREAM_ERROR_BODY_BYTES == 4 * 1024

    async def test_an_oversized_error_body_is_truncated_and_never_retained(self) -> None:
        huge = {"message": FIXTURE_PII[0] + "x" * (MAX_UPSTREAM_ERROR_BODY_BYTES * 4)}
        recorder = _Recorder(httpx.Response(400, json=huge))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        error = excinfo.value
        assert error.upstream_error_bytes <= MAX_UPSTREAM_ERROR_BODY_BYTES
        assert error.upstream_error_truncated is True
        assert error.upstream_error_redacted is True
        _assert_no_secrets(str(error), repr(error))
        assert "xxxx" not in str(error)

    async def test_a_non_json_error_body_is_handled_safely(self) -> None:
        recorder = _Recorder(
            httpx.Response(
                403,
                content=b"<html><body>forbidden " + SYNTHETIC_TOKEN.encode() + b"</body></html>",
                headers={"content-type": "text/html"},
            )
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevPermissionError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        _assert_no_secrets(str(excinfo.value), repr(excinfo.value))
        assert "html" not in str(excinfo.value).lower()

    async def test_an_empty_error_body_is_handled_safely(self) -> None:
        recorder = _Recorder(httpx.Response(404))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevNotFoundError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert excinfo.value.upstream_error_bytes == 0
        assert excinfo.value.upstream_error_truncated is False

    async def test_no_log_record_contains_the_token_or_an_upstream_body(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="data_pipeline.devrev_client")
        recorder = _Recorder(
            httpx.Response(500),
            httpx.Response(403, json={"message": f"denied for {FIXTURE_PII[0]}"}),
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevPermissionError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        _assert_no_secrets(_log_text(caplog))


# =====================================================================
# 16. Redirects never forward credentials to another origin
# =====================================================================


class TestRedirects:
    async def test_redirects_are_disabled_on_the_shared_client(self) -> None:
        client = _client(_Recorder())
        try:
            assert client.follow_redirects is False
        finally:
            await client.aclose()

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    async def test_a_cross_origin_redirect_is_refused_without_a_second_request(
        self, status: int
    ) -> None:
        recorder = _Recorder(
            httpx.Response(status, headers={"Location": "https://evil.example.invalid/works.list"})
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == 1
        assert "evil.example.invalid" not in str(excinfo.value)
        _assert_no_secrets(str(excinfo.value))

    async def test_a_same_origin_redirect_is_also_refused(self) -> None:
        recorder = _Recorder(
            httpx.Response(302, headers={"Location": f"{DEVREV_OFFICIAL_API_BASE}/works.list"})
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == 1


# =====================================================================
# 17. Rate-limit headers become bounded diagnostics, never content logs
# =====================================================================


class TestDiagnostics:
    async def test_rate_limit_headers_are_captured(self) -> None:
        recorder = _Recorder(
            _json_response(
                WORKS_PAGE_1,
                headers={
                    "X-Ratelimit-Limit": "1000",
                    "X-Ratelimit-Remaining": "812",
                    "X-Ratelimit-Reset": "1778760000",
                    "X-Devrev-Request-Id": "req-abc-123",
                },
            )
        )
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        diagnostics = client.last_diagnostics
        assert isinstance(diagnostics, DevRevCallDiagnostics)
        assert diagnostics.endpoint == ENDPOINT_WORKS_LIST
        assert diagnostics.status == 200
        assert diagnostics.attempts == 1
        assert diagnostics.pages == 1
        assert diagnostics.items == 2
        assert diagnostics.rate_limit_limit == 1000
        assert diagnostics.rate_limit_remaining == 812
        assert diagnostics.rate_limit_reset == 1778760000
        assert diagnostics.request_id == "req-abc-123"

    async def test_malformed_rate_limit_headers_are_ignored_not_fatal(self) -> None:
        recorder = _Recorder(
            _json_response(
                WORKS_PAGE_1,
                headers={"X-Ratelimit-Remaining": "not-a-number", "X-Request-Id": "r" * 5000},
            )
        )
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        diagnostics = client.last_diagnostics
        assert diagnostics is not None
        assert diagnostics.rate_limit_remaining is None
        assert diagnostics.request_id is not None
        assert len(diagnostics.request_id) <= 200

    async def test_log_fields_match_the_documented_shape(self) -> None:
        recorder = _Recorder(
            _json_response(TIMELINE_EMPTY_WITH_CURSOR, headers={"X-Ratelimit-Remaining": "812"}),
            _json_response(TIMELINE_FINAL),
        )
        client = _client(recorder)
        try:
            [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        diagnostics = client.last_diagnostics
        assert diagnostics is not None
        fields = diagnostics.as_log_fields()
        assert fields["endpoint"] == ENDPOINT_TIMELINE_LIST
        assert fields["status"] == 200
        assert fields["attempts"] == 1
        assert fields["pages"] == 2
        assert fields["items"] == 5
        assert fields["rate_limit_remaining"] == 812
        assert "request_id" in fields
        assert json.dumps(fields)  # structured-log serializable

    async def test_diagnostics_never_contain_a_cursor_value(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_2))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(), cursor="cursor-works-page-2")
        finally:
            await client.aclose()
        diagnostics = client.last_diagnostics
        assert diagnostics is not None
        serialized = json.dumps(diagnostics.as_log_fields())
        assert "cursor-works-page-2" not in serialized
        assert diagnostics.cursor_digest == cursor_digest("cursor-works-page-2")
        assert diagnostics.cursor_digest is not None
        assert re.fullmatch(r"[0-9a-f]{16}", diagnostics.cursor_digest)

    def test_cursor_digest_is_a_stable_one_way_hash(self) -> None:
        assert cursor_digest(None) is None
        assert cursor_digest("a") == cursor_digest("a")
        assert cursor_digest("a") != cursor_digest("b")
        digest = cursor_digest("cursor-works-page-2")
        assert digest is not None
        assert re.fullmatch(r"[0-9a-f]{16}", digest)
        assert "cursor-works-page-2" not in digest

    async def test_info_logs_carry_no_ticket_content_and_no_cursor(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="data_pipeline.devrev_client")
        recorder = _Recorder(_json_response(WORKS_PAGE_1), _json_response(WORK_GET))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(), cursor="cursor-works-page-2")
            await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        text = _log_text(caplog)
        _assert_no_secrets(text)
        assert "cursor-works-page-2" not in text
        assert "Rollover question" not in text

    async def test_exactly_one_info_line_is_emitted_per_logical_call(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A multi-page walk must not emit one record per page or per entry: the
        # single line that matters would be buried.
        caplog.set_level(logging.INFO, logger="data_pipeline.devrev_client")
        recorder = _Recorder(
            _json_response(TIMELINE_EMPTY_WITH_CURSOR),
            _json_response(TIMELINE_FINAL),
        )
        client = _client(recorder)
        try:
            entries = [e async for e in client.iter_timeline_entries(SYNTHETIC_TICKET_DON)]
        finally:
            await client.aclose()
        assert len(entries) == 5
        info = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info) == 1
        fields = info[0].devrev  # type: ignore[attr-defined]
        assert fields["endpoint"] == ENDPOINT_TIMELINE_LIST
        assert fields["pages"] == 2
        assert fields["items"] == 5
        _assert_no_secrets(json.dumps(fields))

    async def test_an_abandoned_iterator_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="data_pipeline.devrev_client")
        page = {
            "timeline_entries": TIMELINE_FINAL["timeline_entries"],
            "next_cursor": "cursor-timeline-page-2",
        }
        recorder = _Recorder(_json_response(page))
        client = _client(recorder)
        try:
            iterator = client.iter_timeline_entries(SYNTHETIC_TICKET_DON)
            first = await iterator.__anext__()
            assert first is not None
            await iterator.aclose()
        finally:
            await client.aclose()
        assert [r for r in caplog.records if r.levelno == logging.INFO] == []
        # Diagnostics are still accurate for the work that did happen.
        assert client.last_diagnostics is not None
        assert client.last_diagnostics.items == 1

    async def test_a_retry_is_logged_at_warning_without_a_body_or_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="data_pipeline.devrev_client")
        recorder = _Recorder(
            httpx.Response(503, json={"message": f"upstream said {FIXTURE_PII[0]}"}),
            _json_response(WORKS_PAGE_1),
        )
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        _assert_no_secrets(json.dumps(warnings[0].devrev))  # type: ignore[attr-defined]

    async def test_diagnostics_are_a_frozen_snapshot(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters())
            diagnostics = client.last_diagnostics
        finally:
            await client.aclose()
        assert diagnostics is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            diagnostics.items = 99  # type: ignore[misc]


# =====================================================================
# 18. Cancellation propagates untouched
# =====================================================================


class TestCancellation:
    async def test_cancellation_from_the_transport_is_not_wrapped(self) -> None:
        recorder = _Recorder(asyncio.CancelledError())
        client = _client(recorder)
        try:
            with pytest.raises(asyncio.CancelledError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()

    async def test_cancellation_during_backoff_is_not_wrapped(self) -> None:
        recorder = _Recorder(httpx.Response(503), _json_response(WORKS_PAGE_1))
        client = _client(recorder, sleep=_Sleeper(raises=asyncio.CancelledError()))
        try:
            with pytest.raises(asyncio.CancelledError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == 1

    async def test_cancelling_an_iterator_task_does_not_become_a_devrev_error(self) -> None:
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

        client = _client(handler)
        task = asyncio.create_task(client.list_tickets(DevRevTicketFilters()))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.aclose()

    async def test_cancelled_error_is_not_a_devrev_error(self) -> None:
        assert not issubclass(asyncio.CancelledError, DevRevError)


# =====================================================================
# 20 & 21. Configured scope is injected and cannot be widened
# =====================================================================


class TestScopeEnforcement:
    async def test_a_caller_cannot_clear_the_configured_part_scope(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(applies_to_part=[]))
        finally:
            await client.aclose()
        assert recorder.body(0)["applies_to_part"] == [SYNTHETIC_PART, SYNTHETIC_PART_2]

    async def test_a_caller_cannot_broaden_the_configured_part_scope(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError):
                await client.list_tickets(
                    DevRevTicketFilters(applies_to_part=[SYNTHETIC_PART_UNSCOPED])
                )
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_a_caller_may_narrow_within_the_configured_part_scope(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(applies_to_part=[SYNTHETIC_PART_2]))
        finally:
            await client.aclose()
        assert recorder.body(0)["applies_to_part"] == [SYNTHETIC_PART_2]

    async def test_a_caller_cannot_broaden_ticket_visibility(self) -> None:
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError):
                await client.list_tickets(DevRevTicketFilters(ticket_visibility=[4]))
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_a_caller_cannot_clear_ticket_visibility(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(DevRevTicketFilters(ticket_visibility=[]))
        finally:
            await client.aclose()
        assert recorder.body(0)["ticket"]["visibility"] == [1, 2]

    async def test_an_out_of_scope_row_in_a_list_response_is_dropped(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][1]["applies_to_part"]["id"] = SYNTHETIC_PART_UNSCOPED
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert [item.devrev_display_id for item in page.items] == ["TKT-1234"]
        assert page.partial is True
        assert any("out_of_scope" in warning for warning in page.warnings)

    async def test_an_out_of_scope_visibility_row_is_dropped(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["ticket"]["visibility"] = 9
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert [item.devrev_display_id for item in page.items] == ["TKT-1235"]
        assert page.partial is True

    async def test_a_non_ticket_row_in_a_list_response_is_dropped(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["type"] = "issue"
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert [item.devrev_display_id for item in page.items] == ["TKT-1235"]

    async def test_timeline_visibility_allowlist_cannot_be_widened_by_a_caller(self) -> None:
        import inspect

        signature = inspect.signature(DevRevClient.list_timeline_page)
        assert "visibility" not in signature.parameters
        assert "visibilities" not in signature.parameters

    async def test_the_timeline_allowlist_is_separate_from_ticket_visibility(self) -> None:
        recorder = _Recorder(_json_response(TIMELINE_FINAL))
        client = _client(
            recorder,
            allowed_ticket_visibility_ids=[2],
            allowed_timeline_visibilities=["internal"],
        )
        try:
            await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert recorder.requests[0].url.params.get_list("visibility") == ["internal"]

    async def test_an_empty_configured_scope_fails_closed_at_construction(self) -> None:
        for override in (
            {"allowed_part_dons": []},
            {"allowed_ticket_visibility_ids": []},
            {"allowed_timeline_visibilities": []},
        ):
            with pytest.raises(DevRevConfigurationError):
                _client(_Recorder(), **override)

    async def test_an_unknown_timeline_visibility_in_config_is_rejected(self) -> None:
        with pytest.raises(DevRevConfigurationError):
            _client(_Recorder(), allowed_timeline_visibilities=["confidential"])

    async def test_a_repeated_but_allowed_scope_value_is_not_a_violation(self) -> None:
        # Narrowing to the same allowed part twice is a harmless duplicate, not
        # an attempt to widen the scope.
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            await client.list_tickets(
                DevRevTicketFilters(
                    applies_to_part=[SYNTHETIC_PART, SYNTHETIC_PART],
                    ticket_visibility=[2, 2],
                )
            )
        finally:
            await client.aclose()
        body = recorder.body(0)
        assert body["applies_to_part"] == [SYNTHETIC_PART]
        assert body["ticket"]["visibility"] == [2]

    async def test_a_partly_allowed_scope_request_is_refused_not_silently_narrowed(self) -> None:
        # Dropping the unknown value and returning results would let a caller
        # discover which parts exist by watching the result set shrink.
        recorder = _Recorder()
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError):
                await client.list_tickets(
                    DevRevTicketFilters(
                        applies_to_part=[SYNTHETIC_PART, SYNTHETIC_PART_UNSCOPED]
                    )
                )
        finally:
            await client.aclose()
        assert recorder.count == 0

    async def test_a_direct_id_cannot_bypass_the_list_filters(self) -> None:
        # An unscoped ticket fetched directly is denied even though works.list
        # would never have returned it.
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["applies_to_part"]["id"] = SYNTHETIC_PART_UNSCOPED
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevScopeError):
                await client.get_ticket(SYNTHETIC_TICKET_DON_3)
        finally:
            await client.aclose()


# =====================================================================
# 19 (cont.) & normalization edge cases
# =====================================================================


class TestNormalization:
    async def test_owner_and_tag_lists_are_bounded(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        work = payload["works"][0]
        work["owned_by"] = [
            {"type": "dev_user", "id": f"{SYNTHETIC_DEV_USER}-{i}"}
            for i in range(MAX_LIST_ITEMS + 5)
        ]
        work["tags"] = [{"id": f"{SYNTHETIC_TAG}-{i}"} for i in range(MAX_LIST_ITEMS + 5)]
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert len(page.items[0].owner_ids) == MAX_LIST_ITEMS
        assert len(page.items[0].tag_ids) == MAX_LIST_ITEMS
        assert page.truncated is True

    async def test_attachments_use_their_own_canonical_bound(self) -> None:
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["artifacts"] = [
            {"id": f"don:core:dvrv-us-1:devo/SYNTHETIC00:artifact/{i}"}
            for i in range(MAX_ATTACHMENTS + 5)
        ]
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            detail = await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert len(detail.attachments) == MAX_ATTACHMENTS
        # get_ticket returns a single object, so the bound is reported through
        # diagnostics rather than page warnings.
        assert client.last_diagnostics is not None
        assert client.last_diagnostics.truncated is True

    async def test_no_binary_attachment_body_is_ever_retained(self) -> None:
        payload = json.loads(json.dumps(WORK_GET))
        payload["work"]["artifacts"] = [
            {
                "id": "don:core:dvrv-us-1:devo/SYNTHETIC00:artifact/1",
                "file": {"name": "statement.pdf", "size": 1024},
                "url": "https://example.invalid/blob",
            }
        ]
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            detail = await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        # Only the identifier survives: no name, size, or URL.
        assert detail.attachments == ["don:core:dvrv-us-1:devo/SYNTHETIC00:artifact/1"]

    async def test_an_actor_display_name_is_bounded(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["reported_by"]["display_name"] = "N" * (MAX_DISPLAY_NAME_LENGTH + 50)
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        reporter = page.items[0].reporter
        assert reporter is not None
        assert reporter.display_name is not None
        assert len(reporter.display_name) == MAX_DISPLAY_NAME_LENGTH

    async def test_an_actor_email_is_never_carried_onto_a_model(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1), _json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
            timeline = await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        actors = [item.reporter for item in page.items] + [e.author for e in timeline.items]
        for actor in actors:
            if actor is None:
                continue
            assert "email" not in type(actor).model_fields
            assert "@" not in json.dumps(actor.model_dump())

    async def test_no_normalized_model_retains_a_raw_payload(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1), _json_response(WORK_GET))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
            detail = await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        for model in (*page.items, detail):
            assert "raw" not in type(model).model_fields
            assert not hasattr(model, "raw")
            dumped = model.model_dump()
            assert "raw" not in dumped

    async def test_internal_don_and_display_id_are_separate_fields(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        item = page.items[0]
        assert item.devrev_work_id == SYNTHETIC_TICKET_DON
        assert item.devrev_display_id == "TKT-1234"
        assert item.devrev_work_id != item.devrev_display_id

    async def test_a_row_without_an_id_or_display_id_is_dropped_not_fatal(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0].pop("display_id")
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert [item.devrev_display_id for item in page.items] == ["TKT-1235"]
        assert page.partial is True

    async def test_stage_and_state_accept_object_or_string_shapes(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["stage"] = "queued"
        payload["works"][0]["state"] = {"name": "in_progress"}
        payload["works"][1]["stage"] = {"display_name": "Triage"}
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert page.items[0].stage == "queued"
        assert page.items[0].state == "in_progress"
        assert page.items[1].stage == "Triage"

    async def test_applies_to_part_accepts_a_bare_string(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["applies_to_part"] = SYNTHETIC_PART
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert page.items[0].applies_to_part == SYNTHETIC_PART

    async def test_an_unparsable_date_becomes_none_on_a_ticket(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["created_date"] = "yesterday-ish"
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert page.items[0].created_at is None
        assert page.items[0].modified_at is not None

    async def test_a_naive_remote_date_is_normalized_to_utc(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        payload["works"][0]["created_date"] = "2026-05-04T14:12:07"
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        created = page.items[0].created_at
        assert created is not None
        assert created.tzinfo is not None
        assert created.utcoffset() == timezone.utc.utcoffset(None)

    async def test_a_non_object_json_body_is_a_protocol_error(self) -> None:
        recorder = _Recorder(httpx.Response(200, json=[1, 2, 3]))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()

    async def test_a_non_json_success_body_is_a_protocol_error(self) -> None:
        recorder = _Recorder(
            httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})
        )
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert "not json at all" not in str(excinfo.value)

    async def test_a_non_list_works_value_is_a_protocol_error(self) -> None:
        recorder = _Recorder(_json_response({"works": {"id": "x"}}))
        client = _client(recorder)
        try:
            with pytest.raises(DevRevProtocolError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()

    async def test_warnings_are_bounded_deduplicated_and_content_free(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        for work in payload["works"]:
            work["title"] = "T" * (MAX_TITLE_LENGTH + 10)
            work["owned_by"] = [
                {"type": "dev_user", "id": f"{SYNTHETIC_DEV_USER}-{i}"}
                for i in range(MAX_LIST_ITEMS + 2)
            ]
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert len(page.warnings) == len(set(page.warnings))
        assert len(page.warnings) <= MAX_WARNINGS
        for warning in page.warnings:
            assert re.fullmatch(r"[a-z0-9_]+(=\d+)?", warning), warning
        _assert_no_secrets(*page.warnings)

    async def test_a_page_size_larger_than_requested_is_flagged(self) -> None:
        payload = json.loads(json.dumps(WORKS_PAGE_1))
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            page = await client.list_tickets(DevRevTicketFilters(), limit=1)
        finally:
            await client.aclose()
        assert len(page.items) == 2
        assert page.page_size >= 2
        assert page.truncated is True
        assert any("more_items" in warning for warning in page.warnings)


# =====================================================================
# Bounded hydration and explicit sorting
# =====================================================================


class TestHydrationSorting:
    async def test_hydration_preserves_remote_order_by_default(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"].reverse()
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            hydration = await client.load_timeline(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert hydration.entries[0].kind is TimelineEntryKind.UNSUPPORTED

    async def test_hydration_can_sort_by_aware_timestamp(self) -> None:
        payload = json.loads(json.dumps(TIMELINE_FINAL))
        payload["timeline_entries"].reverse()
        recorder = _Recorder(_json_response(payload))
        client = _client(recorder)
        try:
            hydration = await client.load_timeline(SYNTHETIC_TICKET_DON, sort=True)
        finally:
            await client.aclose()
        timestamps = [entry.created_at for entry in hydration.entries]
        assert timestamps == sorted(timestamps)

    def test_sorting_is_stable_on_equal_timestamps(self) -> None:
        moment = datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc)
        entries = [
            DevRevTimelineEntry(
                entry_id=f"don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/{index}",
                object_id=SYNTHETIC_TICKET_DON,
                kind=TimelineEntryKind.COMMENT,
                created_at=moment,
            )
            for index in range(5)
        ]
        sorted_entries, warnings = sort_timeline_entries(entries)
        assert [entry.entry_id for entry in sorted_entries] == [e.entry_id for e in entries]
        assert warnings == ()

    def test_a_malformed_sort_timestamp_goes_last_with_a_warning(self) -> None:
        good = DevRevTimelineEntry(
            entry_id="don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/1",
            object_id=SYNTHETIC_TICKET_DON,
            kind=TimelineEntryKind.COMMENT,
            created_at=datetime(2026, 5, 4, 14, 0, 0, tzinfo=timezone.utc),
        )
        # model_construct bypasses validation, which is exactly how a naive or
        # missing timestamp could reach the helper.
        naive = DevRevTimelineEntry.model_construct(
            entry_id="don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/2",
            object_id=SYNTHETIC_TICKET_DON,
            kind=TimelineEntryKind.COMMENT,
            created_at=datetime(2020, 1, 1, 0, 0, 0),
        )
        missing = DevRevTimelineEntry.model_construct(
            entry_id="don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/3",
            object_id=SYNTHETIC_TICKET_DON,
            kind=TimelineEntryKind.COMMENT,
            created_at=None,
        )
        sorted_entries, warnings = sort_timeline_entries([naive, good, missing])
        assert [entry.entry_id for entry in sorted_entries] == [
            "don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/1",
            "don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/2",
            "don:core:dvrv-us-1:devo/SYNTHETIC00:timeline_event/3",
        ]
        assert any("malformed" in warning for warning in warnings)
        _assert_no_secrets(*warnings)

    async def test_hydration_diagnostics_report_pages_and_duplicates(self) -> None:
        first = json.loads(json.dumps(TIMELINE_FINAL))
        first["next_cursor"] = "cursor-timeline-page-2"
        recorder = _Recorder(_json_response(first), _json_response(TIMELINE_FINAL))
        client = _client(recorder)
        try:
            hydration = await client.load_timeline(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()
        assert hydration.pages == 2
        assert hydration.diagnostics.pages == 2
        assert hydration.diagnostics.duplicate_entries == 5
        assert len(hydration.entries) == 5


# =====================================================================
# 22. Success bodies are streamed and capped before JSON parsing
# =====================================================================


class TestResponseByteCaps:
    def test_the_canonical_success_cap(self) -> None:
        assert DEVREV_MAX_RESPONSE_BYTES == 4 * 1024 * 1024

    async def test_an_oversized_declared_content_length_is_refused_before_reading(self) -> None:
        stream = _CountingStream([b'{"works": []}'])
        recorder = _Recorder(
            httpx.Response(200, headers={"Content-Length": "9999999"}, content=stream)
        )
        client = _client(recorder, max_response_bytes=1024)
        try:
            with pytest.raises(DevRevResourceLimitError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert stream.pulled == 0, "the body must not be read once the declaration is oversized"
        assert excinfo.value.status == 200
        _assert_no_secrets(str(excinfo.value))

    async def test_an_oversized_chunked_list_response_aborts_mid_stream(self) -> None:
        chunks = [b"x" * 256 for _ in range(100)]
        stream = _CountingStream(chunks)
        recorder = _Recorder(httpx.Response(200, content=stream))
        client = _client(recorder, max_response_bytes=1024)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert 0 < stream.pulled <= 6, "the stream must abort immediately, not drain"

    async def test_an_oversized_chunked_get_response_is_refused(self) -> None:
        stream = _CountingStream([b"y" * 512 for _ in range(50)])
        recorder = _Recorder(httpx.Response(200, content=stream))
        client = _client(recorder, max_response_bytes=1024)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.get_ticket(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

    async def test_an_oversized_chunked_timeline_response_is_refused(self) -> None:
        stream = _CountingStream([b"z" * 512 for _ in range(50)])
        recorder = _Recorder(httpx.Response(200, content=stream))
        client = _client(recorder, max_response_bytes=1024)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.list_timeline_page(SYNTHETIC_TICKET_DON)
        finally:
            await client.aclose()

    async def test_decompression_expansion_is_counted_against_the_cap(self) -> None:
        # A small compressed body that expands far past the cap: the declared
        # Content-Length passes, so only decoded-byte counting catches it.
        plain = json.dumps({"works": [], "padding": "p" * 200_000}).encode()
        compressed = gzip.compress(plain)
        assert len(compressed) < 4096 < len(plain)
        recorder = _Recorder(
            httpx.Response(
                200,
                content=compressed,
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        )
        client = _client(recorder, max_response_bytes=4096)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()

    async def test_a_body_exactly_at_the_cap_is_accepted(self) -> None:
        body = json.dumps(WORKS_PAGE_1).encode()
        recorder = _Recorder(
            httpx.Response(
                200, content=body, headers={"Content-Type": "application/json"}
            )
        )
        client = _client(recorder, max_response_bytes=len(body))
        try:
            page = await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert len(page.items) == 2

    async def test_a_resource_limit_error_retains_no_body(self) -> None:
        plain = json.dumps({"works": [], "leak": FIXTURE_PII[0] * 500}).encode()
        recorder = _Recorder(
            httpx.Response(200, content=plain, headers={"Content-Type": "application/json"})
        )
        client = _client(recorder, max_response_bytes=256)
        try:
            with pytest.raises(DevRevResourceLimitError) as excinfo:
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        _assert_no_secrets(str(excinfo.value), repr(excinfo.value))

    async def test_an_oversized_response_is_not_retried(self) -> None:
        plain = b"x" * 4096
        recorder = _Recorder(httpx.Response(200, content=plain))
        client = _client(recorder, max_response_bytes=64)
        try:
            with pytest.raises(DevRevResourceLimitError):
                await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.count == 1

    def test_response_json_is_never_called_on_an_unbounded_body(self) -> None:
        source = Path(
            __import__("data_pipeline.devrev_client", fromlist=["__file__"]).__file__
        ).read_text(encoding="utf-8")
        # Only the bounded byte buffer is ever parsed.
        assert ".json()" not in source
        assert "await response.aread()" not in source
        assert "response.read()" not in source
        assert "aiter_bytes" in source
        assert "client.stream" in source or "self._client.stream" in source


# =====================================================================
# Base URL and configuration hardening
# =====================================================================


class TestBaseUrlHardening:
    @pytest.mark.parametrize(
        "bad",
        [
            "https://user:secret@api.devrev.ai",
            "https://api.devrev.ai/?token=abc",
            "https://api.devrev.ai/#frag",
            "http://api.devrev.ai",
            "ftp://api.devrev.ai",
            "api.devrev.ai",
            "",
            "https://",
        ],
    )
    async def test_an_unsafe_base_url_is_rejected(self, bad: str) -> None:
        with pytest.raises(DevRevConfigurationError) as excinfo:
            _client(_Recorder(), base_url=bad)
        assert "secret" not in str(excinfo.value)

    async def test_production_refuses_a_non_official_origin(self) -> None:
        with pytest.raises(DevRevConfigurationError):
            _client(
                _Recorder(),
                base_url="https://api.devrev.example.invalid",
                environment="production",
            )

    async def test_a_local_fixture_server_is_allowed_outside_production(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(
            recorder,
            base_url="http://127.0.0.1:8931",
            environment="local",
            allow_non_official_base=True,
        )
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.requests[0].url.host == "127.0.0.1"

    async def test_a_non_loopback_http_base_is_rejected_even_locally(self) -> None:
        with pytest.raises(DevRevConfigurationError):
            _client(
                _Recorder(),
                base_url="http://devrev.internal.example.invalid",
                environment="local",
                allow_non_official_base=True,
            )

    async def test_a_trailing_slash_base_url_does_not_double_the_path(self) -> None:
        recorder = _Recorder(_json_response(WORKS_PAGE_1))
        client = _client(recorder, base_url=f"{DEVREV_OFFICIAL_API_BASE}/")
        try:
            await client.list_tickets(DevRevTicketFilters())
        finally:
            await client.aclose()
        assert recorder.requests[0].url.path == "/works.list"

    @pytest.mark.parametrize(
        "override",
        [
            {"max_retries": 0},
            {"max_retries": DEVREV_MAX_RETRIES + 1},
            {"page_size": 0},
            {"page_size": MAX_PAGE_SIZE + 1},
            {"max_pages": 0},
            {"max_pages": DEVREV_MAX_PAGES + 1},
            {"max_entries": 0},
            {"max_entries": DEVREV_MAX_ENTRIES + 1},
            {"max_response_bytes": 0},
            {"max_response_bytes": DEVREV_MAX_RESPONSE_BYTES + 1},
            {"connect_timeout_s": 0},
            {"timeout_s": 0},
            {"retry_after_cap_s": DEVREV_RETRY_AFTER_CAP_S + 1},
        ],
    )
    async def test_a_bound_outside_the_canonical_table_is_rejected(
        self, override: dict[str, Any]
    ) -> None:
        with pytest.raises(DevRevConfigurationError):
            _client(_Recorder(), **override)

    async def test_all_four_timeouts_are_explicit(self) -> None:
        client = _client(_Recorder())
        try:
            timeout = client.timeout
            assert timeout.connect == DEVREV_CONNECT_TIMEOUT_S
            assert timeout.read == DEVREV_READ_TIMEOUT_S
            assert timeout.write == DEVREV_READ_TIMEOUT_S
            assert timeout.pool == DEVREV_READ_TIMEOUT_S
            assert None not in (timeout.connect, timeout.read, timeout.write, timeout.pool)
        finally:
            await client.aclose()

    async def test_aclose_is_idempotent_and_closes_the_shared_client(self) -> None:
        client = _client(_Recorder())
        await client.aclose()
        await client.aclose()
        assert client.is_closed is True

    async def test_a_closed_client_refuses_further_calls(self) -> None:
        client = _client(_Recorder())
        await client.aclose()
        with pytest.raises(DevRevRequestError):
            await client.list_tickets(DevRevTicketFilters())

    async def test_one_shared_async_client_backs_every_call(self) -> None:
        recorder = _Recorder(
            _json_response(WORKS_PAGE_1),
            _json_response(WORK_GET),
            _json_response(TIMELINE_FINAL),
        )
        client = _client(recorder)
        try:
            underlying = client._client
            await client.list_tickets(DevRevTicketFilters())
            await client.get_ticket(SYNTHETIC_TICKET_DON)
            await client.list_timeline_page(SYNTHETIC_TICKET_DON)
            assert client._client is underlying
        finally:
            await client.aclose()


# =====================================================================
# Owner/tag lookup remains feature-gated in the MVP
# =====================================================================


class TestNoLookupEndpoints:
    def test_the_client_exposes_no_owner_or_tag_lookup(self) -> None:
        for forbidden in (
            "list_dev_users",
            "list_tags",
            "resolve_owner",
            "resolve_tag",
            "search_core",
            "create_ticket",
            "update_ticket",
            "delete_ticket",
            "post_comment",
        ):
            assert not hasattr(DevRevClient, forbidden)

    def test_no_write_or_lookup_path_is_referenced_in_the_module(self) -> None:
        """No executable string in the module names a write or lookup endpoint.

        Docstrings are excluded deliberately: the module documents *that*
        owner/tag lookup is feature-gated, and that prose must not be mistaken
        for a call site. Every other string constant is fair game, because a
        request path can only reach httpx through one.
        """
        import ast

        source = Path(
            __import__("data_pipeline.devrev_client", fromlist=["__file__"]).__file__
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        for forbidden in (
            "dev-users.list",
            "tags.list",
            "works.create",
            "works.update",
            "works.delete",
            "timeline-entries.create",
            "search.core",
        ):
            offenders = [text for text in literals if forbidden in text]
            assert not offenders, f"{forbidden} appears in {offenders}"

        # And the only three request paths the module can name are the MVP
        # reads. A bare "/" is URL-normalization, not an endpoint.
        paths = sorted({text for text in literals if text.startswith("/") and len(text) > 1})
        assert paths == ["/timeline-entries.list", "/works.get", "/works.list"]

    def test_summary_and_detail_models_expose_ids_not_display_names(self) -> None:
        assert "owner_ids" in DevRevTicketSummary.model_fields
        assert "tag_ids" in DevRevTicketSummary.model_fields
        assert "owner_names" not in DevRevTicketSummary.model_fields
        assert "tag_names" not in DevRevTicketSummary.model_fields
        assert "owner_names" not in DevRevTicketDetail.model_fields
