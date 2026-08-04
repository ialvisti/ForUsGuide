"""Stage 5 — the console's authenticated client for the evidence broker.

Every test is offline: the HTTP layer is an ``httpx.MockTransport`` and the ID
token factory is injected, so nothing here mints a real Google token or reaches
Cloud Run.

The invariants that matter, and why:

* the client calls **one** URL — the configured broker plus the pinned lookup
  path — and never a caller-supplied one;
* the ID token audience is exactly ``TICKETS_EVIDENCE_BROKER_AUDIENCE``;
* the decoded response is capped at the canonical 512 KiB *before* JSON parsing,
  so an oversized or compression-bombed body cannot be materialized;
* a redirect is refused rather than followed, because a 302 is how an attacker
  moves a bearer token to a host of their choosing;
* there is no Firestore fallback of any kind — the whole reason the broker
  exists is that the console may not read production ``(default)``;
* a broker outage *raises*, and does not return an empty envelope: the
  difference between "no evidence exists" and "we could not look" is the whole
  point of the correlation contract.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
from datetime import datetime, timezone

import httpx
import pytest

from api.ticket_review_models import (
    EVIDENCE_BROKER_MAX_RESPONSE_BYTES,
    CorrelationStatus,
    RagEvidenceEnvelope,
)
from api.tickets_console_config import TicketConsoleSettings
from data_pipeline.ticket_evidence_client import (
    EVIDENCE_LOOKUP_PATH,
    EVIDENCE_UNAVAILABLE_REASON,
    EvidenceClientConfigurationError,
    EvidenceClientError,
    EvidenceBrokerUnavailable,
    EvidenceProtocolError,
    EvidenceResponseTooLarge,
    TicketEvidenceClient,
    unavailable_envelope,
)

BROKER_URL = "https://tickets-evidence-broker-abc-uc.a.run.app"
BROKER_AUDIENCE = BROKER_URL
SYNTHETIC_DON = "don:core:dvrv-us-1:devo/synthetic:ticket/1234"
TEST_AEAD_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")
FAKE_TOKEN = "synthetic.id.token"  # pragma: allowlist secret
NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)

DIGEST = "a" * 64


def _envelope_json(**overrides) -> dict:
    payload = {
        "correlation_status": CorrelationStatus.UNAVAILABLE.value,
        "records": [],
        "result_digest": DIGEST,
        "key_versions_queried": [1],
        "truncated": False,
        "unavailable_reason": None,
        "warnings": [],
    }
    payload.update(overrides)
    return payload


class _RecordingTokenFactory:
    def __init__(self, token: str = FAKE_TOKEN, error: BaseException | None = None):
        self.token = token
        self.error = error
        self.audiences: list[str] = []

    def __call__(self, audience: str) -> str:
        self.audiences.append(audience)
        if self.error is not None:
            raise self.error
        return self.token


def _client(handler, *, token_factory=None, **overrides) -> TicketEvidenceClient:
    transport = httpx.MockTransport(handler)
    values: dict[str, object] = {
        "base_url": BROKER_URL,
        "audience": BROKER_AUDIENCE,
        "token_factory": token_factory or _RecordingTokenFactory(),
        "client": httpx.AsyncClient(transport=transport, follow_redirects=False),
    }
    values.update(overrides)
    return TicketEvidenceClient(**values)  # type: ignore[arg-type]


def _ok(payload: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload if payload is not None else _envelope_json())

    return handler


def _settings(monkeypatch, **overrides) -> TicketConsoleSettings:
    for name in list(os.environ):
        if name.startswith("TICKETS_"):
            monkeypatch.delenv(name, raising=False)
    values: dict[str, object] = {
        "ENVIRONMENT": "production",
        "GCP_PROJECT": "rag-kb-system",
        "GCP_REGION": "us-central1",
        "EVIDENCE_BROKER_URL": BROKER_URL,
        "EVIDENCE_BROKER_AUDIENCE": BROKER_AUDIENCE,
        "CURSOR_AEAD_KEY": TEST_AEAD_KEY_B64,
    }
    values.update(overrides)
    return TicketConsoleSettings(_env_file=None, **values)


# =====================================================================
# The request the client makes
# =====================================================================


class TestRequestShape:

    async def test_the_pinned_path_matches_the_broker_route(self):
        """Pinned by value, not by import.

        Importing ``api.tickets_evidence_broker_main`` at runtime would construct
        the broker's FastAPI app inside the console process. The contract is
        asserted here instead, where importing it is harmless.
        """
        from api.tickets_evidence_broker_main import LOOKUP_PATH

        assert EVIDENCE_LOOKUP_PATH == LOOKUP_PATH

    async def test_it_posts_to_exactly_one_url(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_envelope_json())

        client = _client(handler)
        await client.lookup(SYNTHETIC_DON, max_results=10)
        await client.aclose()
        assert len(seen) == 1
        assert seen[0].method == "POST"
        assert str(seen[0].url) == f"{BROKER_URL}{EVIDENCE_LOOKUP_PATH}"

    async def test_it_presents_the_id_token_for_the_configured_audience(self):
        factory = _RecordingTokenFactory()
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_envelope_json())

        client = _client(handler, token_factory=factory)
        await client.lookup(SYNTHETIC_DON, max_results=10)
        await client.aclose()
        assert factory.audiences == [BROKER_AUDIENCE]
        assert seen[0].headers["authorization"] == f"Bearer {FAKE_TOKEN}"

    async def test_the_body_carries_the_plain_don_not_a_masked_secret(self):
        """``SecretStr`` serializes to ``'**********'``.

        Dumping the request model would therefore send a literal row of asterisks
        and every lookup would silently return no evidence, so the body is built
        explicitly.
        """
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content)
            return httpx.Response(200, json=_envelope_json())

        client = _client(handler)
        await client.lookup(SYNTHETIC_DON, max_results=7)
        await client.aclose()
        body = json.loads(seen[0])
        assert body == {"devrev_work_id": SYNTHETIC_DON, "max_results": 7}
        assert "*" not in seen[0].decode("utf-8")

    async def test_it_sends_strict_json_and_no_cookies(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_envelope_json())

        client = _client(handler)
        await client.lookup(SYNTHETIC_DON, max_results=1)
        await client.aclose()
        assert seen[0].headers["content-type"] == "application/json"
        assert "cookie" not in seen[0].headers

    async def test_max_results_is_bounded(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_envelope_json())

        client = _client(handler)
        await client.lookup(SYNTHETIC_DON, max_results=10_000)
        await client.lookup(SYNTHETIC_DON, max_results=0)
        await client.aclose()
        assert all(1 <= body["max_results"] <= 25 for body in seen)

    async def test_a_blank_work_id_is_refused_before_any_call(self):
        called = False

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            nonlocal called
            called = True
            return httpx.Response(200, json=_envelope_json())

        client = _client(handler)
        with pytest.raises(EvidenceClientError):
            await client.lookup("   ", max_results=5)
        await client.aclose()
        assert called is False

    async def test_an_oversized_work_id_is_refused_before_any_call(self):
        client = _client(_ok())
        with pytest.raises(EvidenceClientError):
            await client.lookup("d" * 5_000, max_results=5)
        await client.aclose()


# =====================================================================
# The response the client accepts
# =====================================================================


class TestResponseHandling:

    async def test_a_valid_envelope_round_trips(self):
        client = _client(_ok())
        envelope = await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        assert isinstance(envelope, RagEvidenceEnvelope)
        assert envelope.result_digest == DIGEST
        assert envelope.correlation_status is CorrelationStatus.UNAVAILABLE

    async def test_a_brokers_own_unavailable_answer_is_returned_not_raised(self):
        """The broker answered. That is different from the broker being down."""
        payload = _envelope_json(unavailable_reason="no correlated execution exists")
        client = _client(_ok(payload))
        envelope = await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        assert envelope.unavailable_reason == "no correlated execution exists"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500, 502, 503, 504])
    async def test_every_error_status_raises(self, status):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": "nope"})

        client = _client(handler)
        with pytest.raises(EvidenceClientError):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    @pytest.mark.parametrize("status", [503, 504])
    async def test_upstream_unavailability_is_typed(self, status):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": "unavailable"})

        client = _client(handler)
        with pytest.raises(EvidenceBrokerUnavailable):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    async def test_a_redirect_is_refused_and_never_followed(self, status):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(status, headers={"Location": "https://attacker.example/x"})

        client = _client(handler)
        with pytest.raises(EvidenceProtocolError):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        assert seen == [f"{BROKER_URL}{EVIDENCE_LOOKUP_PATH}"]

    async def test_the_client_this_module_builds_does_not_follow_redirects(
        self, monkeypatch
    ):
        """Asserted on the client ``from_settings`` builds, not on a fixture.

        Checking an injected test client only proves the test set the flag. The
        production path is the one that matters: flipping ``follow_redirects`` to
        ``True`` there would hand the broker's bearer token to whatever host a
        302 named, and nothing else in this suite would notice.
        """
        client = TicketEvidenceClient.from_settings(
            _settings(monkeypatch), token_factory=_RecordingTokenFactory()
        )
        assert client.follows_redirects is False
        await client.aclose()

    async def test_a_declared_oversize_body_is_refused_before_reading(self):
        chunks = 0

        def handler(request: httpx.Request) -> httpx.Response:
            async def stream():
                nonlocal chunks
                chunks += 1
                yield b"{}"

            return httpx.Response(
                200,
                headers={"Content-Length": str(EVIDENCE_BROKER_MAX_RESPONSE_BYTES + 1)},
                content=stream(),
            )

        client = _client(handler)
        with pytest.raises(EvidenceResponseTooLarge):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        assert chunks == 0

    async def test_a_chunked_oversize_body_is_refused_while_streaming(self):
        def handler(request: httpx.Request) -> httpx.Response:
            async def stream():
                block = b"x" * 64_000
                for _ in range(20):
                    yield block

            return httpx.Response(200, content=stream())

        client = _client(handler)
        with pytest.raises(EvidenceResponseTooLarge):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_a_compressed_body_that_expands_past_the_cap_is_refused(self):
        """The counter tallies *decoded* bytes, so gzip expansion is caught."""
        raw = json.dumps({"padding": "x" * (EVIDENCE_BROKER_MAX_RESPONSE_BYTES * 2)})
        compressed = gzip.compress(raw.encode("utf-8"))
        assert len(compressed) < EVIDENCE_BROKER_MAX_RESPONSE_BYTES

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed)),
                    "Content-Type": "application/json",
                },
                content=compressed,
            )

        client = _client(handler)
        with pytest.raises(EvidenceResponseTooLarge):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_a_body_at_the_cap_is_still_accepted(self):
        payload = _envelope_json()
        client = _client(_ok(payload), max_response_bytes=len(json.dumps(payload)))
        assert await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_the_cap_cannot_be_raised_above_the_canonical_limit(self):
        with pytest.raises(EvidenceClientConfigurationError):
            _client(_ok(), max_response_bytes=EVIDENCE_BROKER_MAX_RESPONSE_BYTES + 1)

    @pytest.mark.parametrize(
        "body", [b"not json", b"[]", b'"a string"', b"null", b"{", b"\xff\xfe"]
    )
    async def test_a_non_object_or_unparseable_body_is_a_protocol_error(self, body):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        client = _client(handler)
        with pytest.raises(EvidenceProtocolError):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_an_envelope_failing_validation_is_a_protocol_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # A non-empty record set with a 'linked' status but no records is
            # exactly what the envelope's own validator forbids.
            return httpx.Response(
                200, json=_envelope_json(correlation_status="linked", records=[])
            )

        client = _client(handler)
        with pytest.raises(EvidenceProtocolError):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_an_unknown_envelope_field_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope_json(surprise="value"))

        client = _client(handler)
        with pytest.raises(EvidenceProtocolError):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_a_transport_failure_is_typed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("broker unreachable")

        client = _client(handler)
        with pytest.raises(EvidenceBrokerUnavailable):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_a_timeout_is_typed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow broker")

        client = _client(handler)
        with pytest.raises(EvidenceBrokerUnavailable):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()

    async def test_a_token_minting_failure_is_typed_and_makes_no_call(self):
        called = False

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            nonlocal called
            called = True
            return httpx.Response(200, json=_envelope_json())

        factory = _RecordingTokenFactory(error=RuntimeError("no metadata server"))
        client = _client(handler, token_factory=factory)
        with pytest.raises(EvidenceBrokerUnavailable):
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        assert called is False


# =====================================================================
# Secrets never travel outward
# =====================================================================


class TestSecrecy:

    async def test_no_error_message_contains_the_token_or_the_work_id(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "upstream detail"})

        client = _client(handler)
        with pytest.raises(EvidenceClientError) as caught:
            await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        message = str(caught.value)
        assert FAKE_TOKEN not in message
        assert SYNTHETIC_DON not in message
        assert "upstream detail" not in message

    async def test_no_log_line_contains_the_token_or_the_work_id(self, caplog):
        import logging

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "upstream detail"})

        client = _client(handler)
        with caplog.at_level(logging.DEBUG, logger="data_pipeline.ticket_evidence_client"):
            with pytest.raises(EvidenceClientError):
                await client.lookup(SYNTHETIC_DON, max_results=5)
        await client.aclose()
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert FAKE_TOKEN not in text
        assert SYNTHETIC_DON not in text

    async def test_the_repr_hides_the_audience_and_token_factory_state(self):
        client = _client(_ok())
        assert FAKE_TOKEN not in repr(client)
        await client.aclose()


# =====================================================================
# Construction, configuration, and the absence of a Firestore fallback
# =====================================================================


class TestConstruction:

    def test_from_settings_uses_the_configured_broker(self, monkeypatch):
        client = TicketEvidenceClient.from_settings(
            _settings(monkeypatch), token_factory=_RecordingTokenFactory()
        )
        assert client.base_url == BROKER_URL
        assert client.audience == BROKER_AUDIENCE

    def test_from_settings_refuses_a_missing_broker_url(self, monkeypatch):
        with pytest.raises(EvidenceClientConfigurationError):
            TicketEvidenceClient.from_settings(_settings(monkeypatch, EVIDENCE_BROKER_URL=""))

    def test_from_settings_refuses_a_missing_audience(self, monkeypatch):
        with pytest.raises(EvidenceClientConfigurationError):
            TicketEvidenceClient.from_settings(
                _settings(monkeypatch, EVIDENCE_BROKER_AUDIENCE="")
            )

    @pytest.mark.parametrize(
        "url",
        [
            "http://tickets-evidence-broker-abc-uc.a.run.app",
            "ftp://broker.example",
            "not-a-url",
            f"{BROKER_URL}{EVIDENCE_LOOKUP_PATH}",
            f"{BROKER_URL}?x=1",
        ],
    )
    def test_from_settings_refuses_an_unusable_broker_url(self, monkeypatch, url):
        with pytest.raises(EvidenceClientConfigurationError):
            TicketEvidenceClient.from_settings(_settings(monkeypatch, EVIDENCE_BROKER_URL=url))

    def test_a_trailing_slash_on_the_broker_url_is_tolerated(self, monkeypatch):
        client = TicketEvidenceClient.from_settings(
            _settings(monkeypatch, EVIDENCE_BROKER_URL=f"{BROKER_URL}/"),
            token_factory=_RecordingTokenFactory(),
        )
        assert client.base_url == BROKER_URL

    def test_the_module_never_imports_firestore_or_the_broker_app(self):
        """No fallback path to production ``(default)`` may exist here.

        Asserted over the parsed import graph rather than over the raw text: the
        module's docstring names ``google.cloud.firestore`` precisely to explain
        why it is absent, and a substring scan would fail on the explanation
        instead of on a real import. Lazy imports inside functions are included,
        which is the case that actually matters — that is where a "just this
        once" Firestore fallback would be written.
        """
        import ast
        import inspect

        import data_pipeline.ticket_evidence_client as module

        imported: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)

        for forbidden in (
            "google.cloud",
            "firestore",
            "tickets_evidence_broker_main",
            "ticket_evidence_broker",
            "ticket_review_repository",
            "pinecone",
            "openai",
        ):
            offenders = [name for name in imported if forbidden in name]
            assert offenders == [], offenders

    async def test_aclose_is_idempotent_and_closes_the_transport(self):
        client = _client(_ok())
        await client.aclose()
        await client.aclose()
        assert client.is_closed is True

    async def test_a_closed_client_refuses_to_look_up(self):
        client = _client(_ok())
        await client.aclose()
        with pytest.raises(EvidenceClientError):
            await client.lookup(SYNTHETIC_DON, max_results=5)

    async def test_it_works_as_an_async_context_manager(self):
        async with _client(_ok()) as client:
            assert await client.lookup(SYNTHETIC_DON, max_results=5)
        assert client.is_closed is True


class TestUnavailableEnvelope:
    """The explicit degradation shape the API layer renders."""

    def test_it_is_a_valid_unavailable_envelope(self):
        envelope = unavailable_envelope()
        assert isinstance(envelope, RagEvidenceEnvelope)
        assert envelope.correlation_status is CorrelationStatus.UNAVAILABLE
        assert envelope.records == []
        assert envelope.unavailable_reason == EVIDENCE_UNAVAILABLE_REASON

    def test_its_digest_is_stable_and_cannot_match_a_real_result(self):
        first = unavailable_envelope()
        assert first.result_digest == unavailable_envelope().result_digest
        assert len(first.result_digest) == 64
        assert first.result_digest != DIGEST

    def test_a_custom_reason_is_bounded(self):
        envelope = unavailable_envelope(reason="x" * 5_000)
        assert len(envelope.unavailable_reason or "") <= 1_000
