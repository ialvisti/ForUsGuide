"""The console's authenticated client for the ticket evidence broker.

The console cannot read production ``(default)``. That is not an oversight to be
worked around here: Firestore IAM is database-scoped, so the only way to express
"may read ``execution_logs`` and nothing else" is a separate service with its own
service account. This module is the console's *only* door to that service, and
there is deliberately no second door — no ``google.cloud.firestore`` import, no
``(default)`` fallback, no direct query. A test asserts the absence by reading
this file's source.

Why a broker outage raises instead of returning an empty envelope
-----------------------------------------------------------------
``RagEvidenceEnvelope`` can legitimately say "no correlated execution exists".
That claim is evidence in its own right — it is what makes a ticket's provenance
gap *defensible* rather than unknown. If a connection failure also produced that
envelope, the two would be indistinguishable, and
``TicketReviewService._evidence_for`` would report ``broker_available=True`` for a
broker that never answered. So: the broker answering "unavailable" is returned
verbatim; the broker failing to answer raises. The service already treats an
exception as a gap and marks the page partial, and the API layer renders
:func:`unavailable_envelope` when it needs the explicit degraded shape.

Response bounding
-----------------
The decoded body is capped at the canonical 512 KiB *before* ``json.loads`` sees
it. A declared ``Content-Length`` over the cap is refused before the first byte
is read; everything else is tallied as it streams, which is what catches a
chunked body with no declaration and a compressed body that expands past the cap
(``aiter_bytes`` yields decoded bytes). Redirects are refused rather than
followed: a 302 is the cheapest way to make a client hand its bearer token to
another host.
"""

from __future__ import annotations

import hashlib
import json
import logging
from types import TracebackType
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from api.ticket_review_models import (
    EVIDENCE_BROKER_MAX_RESPONSE_BYTES,
    MAX_BROKER_RESULTS,
    MAX_ID_LENGTH,
    MAX_REASON_LENGTH,
    CorrelationStatus,
    RagEvidenceEnvelope,
)

logger = logging.getLogger(__name__)

#: The broker's single route. Pinned by value rather than imported: importing
#: ``api.tickets_evidence_broker_main`` would construct the broker's FastAPI app
#: — and, at startup, its Firestore client — inside the console process.
#: ``tests/test_ticket_evidence_client.py`` asserts the two agree.
EVIDENCE_LOOKUP_PATH = "/internal/v1/ticket-evidence:lookup"

#: The bounded reason the API layer surfaces when the broker could not answer.
EVIDENCE_UNAVAILABLE_REASON = "the evidence broker is unavailable"

#: A fixed, deterministic digest for the degraded envelope. It is derived from a
#: sentinel rather than from content so it can never collide with a real broker
#: result digest, which is what stops a degraded envelope from validating a
#: pending manual-evidence candidate.
_UNAVAILABLE_DIGEST_SENTINEL = b"tickets-console/evidence-unavailable/v1"

DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_READ_TIMEOUT_S = 15.0

#: A DON is bounded by the same rule the broker's request model applies.
_MAX_WORK_ID_LENGTH = MAX_ID_LENGTH


class EvidenceClientError(Exception):
    """Base class for every evidence-client failure.

    Messages are always safe to log: they name the condition, never the DON, the
    bearer token, or any part of the upstream body.
    """

    code = "EVIDENCE_LOOKUP_FAILED"


class EvidenceClientConfigurationError(EvidenceClientError):
    """The client cannot be constructed safely from this configuration."""

    code = "EVIDENCE_BROKER_MISCONFIGURED"


class EvidenceBrokerUnavailable(EvidenceClientError):
    """The broker could not be reached, timed out, or answered 5xx."""

    code = "EVIDENCE_BROKER_UNAVAILABLE"


class EvidenceAuthorizationError(EvidenceClientError):
    """The broker refused this caller's identity."""

    code = "EVIDENCE_BROKER_FORBIDDEN"


class EvidenceProtocolError(EvidenceClientError):
    """The broker's answer violated the contract, or a redirect was refused."""

    code = "EVIDENCE_BROKER_PROTOCOL"


class EvidenceResponseTooLarge(EvidenceClientError):
    """The response exceeded the canonical byte cap."""

    code = "EVIDENCE_RESPONSE_TOO_LARGE"


TokenFactory = Callable[[str], str]


def default_id_token_factory(audience: str) -> str:
    """Mint a Google-signed service-to-service ID token for ``audience``.

    Imported lazily so that importing this module — and therefore the console app
    — never requires ADC or a metadata server. On Cloud Run this reaches the
    metadata server with the revision's own service account; no key file exists
    or is accepted.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    token = id_token.fetch_id_token(google_requests.Request(), audience)
    if not isinstance(token, str) or not token:
        raise EvidenceBrokerUnavailable("could not mint a broker identity token")
    return token


def unavailable_envelope(reason: Optional[str] = None) -> RagEvidenceEnvelope:
    """The explicit "evidence unavailable" envelope for the API boundary."""
    text = (reason or EVIDENCE_UNAVAILABLE_REASON).strip() or EVIDENCE_UNAVAILABLE_REASON
    return RagEvidenceEnvelope(
        correlation_status=CorrelationStatus.UNAVAILABLE,
        records=[],
        result_digest=hashlib.sha256(_UNAVAILABLE_DIGEST_SENTINEL).hexdigest(),
        key_versions_queried=[],
        truncated=False,
        unavailable_reason=text[:MAX_REASON_LENGTH],
        warnings=[],
    )


def _validated_base_url(raw: object) -> str:
    """Require an https origin with no path, query, or fragment."""
    if not isinstance(raw, str) or not raw.strip():
        raise EvidenceClientConfigurationError("EVIDENCE_BROKER_URL is required")
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise EvidenceClientConfigurationError("EVIDENCE_BROKER_URL must be an https URL")
    if parsed.path or parsed.query or parsed.fragment or parsed.params:
        raise EvidenceClientConfigurationError(
            "EVIDENCE_BROKER_URL must be the broker origin with no path or query"
        )
    return value


class TicketEvidenceClient:
    """One bounded, authenticated lookup against the configured broker.

    Satisfies ``data_pipeline.ticket_review_service.EvidenceBrokerClient``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        audience: str,
        client: httpx.AsyncClient,
        token_factory: Optional[TokenFactory] = None,
        max_response_bytes: int = EVIDENCE_BROKER_MAX_RESPONSE_BYTES,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        if not isinstance(audience, str) or not audience.strip():
            raise EvidenceClientConfigurationError("EVIDENCE_BROKER_AUDIENCE is required")
        self._audience = audience.strip()
        if max_response_bytes <= 0 or max_response_bytes > EVIDENCE_BROKER_MAX_RESPONSE_BYTES:
            # Configuration may tighten the canonical cap, never raise it.
            raise EvidenceClientConfigurationError(
                f"the evidence response cap must be 1..{EVIDENCE_BROKER_MAX_RESPONSE_BYTES}"
            )
        self._max_response_bytes = int(max_response_bytes)
        self._client = client
        self._token_factory = token_factory or default_id_token_factory
        self._closed = False

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        client: Optional[httpx.AsyncClient] = None,
        token_factory: Optional[TokenFactory] = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> "TicketEvidenceClient":
        """Build a client from validated console settings.

        ``client`` is injectable so a test never opens a socket, and so the app
        can own a single shared connection pool and close it on shutdown.
        """
        base_url = _validated_base_url(getattr(settings, "EVIDENCE_BROKER_URL", ""))
        audience = (getattr(settings, "EVIDENCE_BROKER_AUDIENCE", "") or "").strip()
        if not audience:
            raise EvidenceClientConfigurationError("EVIDENCE_BROKER_AUDIENCE is required")
        resolved = client or httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            # A redirect is refused, not followed: it would move a bearer token
            # to a host this client never validated.
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return cls(
            base_url=base_url,
            audience=audience,
            client=resolved,
            token_factory=token_factory,
        )

    # -- lifecycle -----------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def audience(self) -> str:
        return self._audience

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def follows_redirects(self) -> bool:
        return bool(getattr(self._client, "follow_redirects", False))

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"TicketEvidenceClient(base_url={self._base_url!r}, closed={self._closed})"

    async def __aenter__(self) -> "TicketEvidenceClient":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the shared pool. Idempotent: shutdown may run twice."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    # -- the one call --------------------------------------------------

    async def lookup(self, devrev_work_id: str, *, max_results: int) -> RagEvidenceEnvelope:
        """Map one DevRev DON to a bounded, sanitized provenance envelope."""
        if self._closed:
            raise EvidenceClientError("the evidence client is closed")
        work_id = devrev_work_id.strip() if isinstance(devrev_work_id, str) else ""
        if not work_id:
            raise EvidenceClientError("a DevRev work id is required")
        if len(work_id) > _MAX_WORK_ID_LENGTH:
            raise EvidenceClientError("the DevRev work id is too long")

        bounded_results = max(1, min(int(max_results), MAX_BROKER_RESULTS))

        try:
            token = self._token_factory(self._audience)
        except Exception as exc:  # noqa: BLE001 - no token, no call
            logger.error(
                "evidence broker identity unavailable; error_type=%s", type(exc).__name__
            )
            raise EvidenceBrokerUnavailable(
                "could not authenticate to the evidence broker"
            ) from exc

        url = f"{self._base_url}{EVIDENCE_LOOKUP_PATH}"
        # Built explicitly rather than by dumping the request model: the model's
        # ``devrev_work_id`` is a SecretStr, and serializing one emits a mask, so
        # a model_dump would send a row of asterisks and every lookup would come
        # back empty.
        body = {"devrev_work_id": work_id, "max_results": bounded_results}

        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            ) as response:
                if 300 <= response.status_code < 400:
                    # Never read, never follow: the body is already off-contract.
                    raise EvidenceProtocolError(
                        f"the evidence broker returned a redirect ({response.status_code})"
                    )
                if response.status_code != 200:
                    raise self._status_error(response.status_code)
                payload = await self._read_capped(response)
        except EvidenceClientError:
            raise
        except httpx.HTTPError as exc:
            logger.warning(
                "evidence broker transport failed; error_type=%s", type(exc).__name__
            )
            raise EvidenceBrokerUnavailable("the evidence broker is unavailable") from exc

        return self._parsed(payload)

    # -- internals -----------------------------------------------------

    @staticmethod
    def _status_error(status: int) -> EvidenceClientError:
        """Map a status to a typed error, never echoing the upstream body."""
        if status in (401, 403):
            logger.error("evidence broker refused the console identity; status=%d", status)
            return EvidenceAuthorizationError(
                "the evidence broker refused this service's identity"
            )
        if status >= 500:
            logger.warning("evidence broker degraded; status=%d", status)
            return EvidenceBrokerUnavailable("the evidence broker is unavailable")
        logger.error("evidence broker rejected the lookup; status=%d", status)
        return EvidenceProtocolError(f"the evidence broker rejected the lookup ({status})")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        """Stream the body, refusing anything past the cap.

        A declared size over the cap is refused before the first byte is read.
        Everything else is tallied as decoded bytes arrive, so a chunked body and
        a compressed body that expands past the cap both abort mid-stream.
        """
        cap = self._max_response_bytes
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > cap:
                    raise EvidenceResponseTooLarge(
                        f"the evidence response declares more than the {cap}-byte cap"
                    )
            except ValueError:
                # An unparseable declaration proves nothing; the counter below is
                # the real guard.
                pass

        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > cap:
                # Drop the buffer before raising so an oversized body is never
                # retained, let alone logged.
                buffer.clear()
                raise EvidenceResponseTooLarge(
                    f"the evidence response exceeds the {cap}-byte cap"
                )
        return bytes(buffer)

    @staticmethod
    def _parsed(payload: bytes) -> RagEvidenceEnvelope:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceProtocolError(
                "the evidence broker returned an unreadable body"
            ) from exc
        if not isinstance(decoded, dict):
            raise EvidenceProtocolError(
                "the evidence broker returned a body that is not an object"
            )
        try:
            return RagEvidenceEnvelope.model_validate(decoded)
        except ValidationError as exc:
            # The validation detail can quote field values, so only the count of
            # problems is logged and nothing is put in the message.
            logger.error(
                "evidence broker envelope failed validation; error_count=%d",
                exc.error_count(),
            )
            raise EvidenceProtocolError(
                "the evidence broker returned an envelope that failed validation"
            ) from exc


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "EVIDENCE_LOOKUP_PATH",
    "EVIDENCE_UNAVAILABLE_REASON",
    "EvidenceAuthorizationError",
    "EvidenceBrokerUnavailable",
    "EvidenceClientConfigurationError",
    "EvidenceClientError",
    "EvidenceProtocolError",
    "EvidenceResponseTooLarge",
    "TicketEvidenceClient",
    "TokenFactory",
    "default_id_token_factory",
    "unavailable_envelope",
]
