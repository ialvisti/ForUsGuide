"""Authenticated, idempotent publisher for RAG ticket-evaluation events."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import httpx

from api import metrics as ticket_metrics
from api.ticket_evaluation_models import TicketEvaluationEvent
from data_pipeline.ticket_job_models import utcnow
from data_pipeline.ticket_job_repository import TicketJobRepository


DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 5.0
HYDRATION_RETRY_TIMEOUT_S = 45.0
MAX_HYDRATION_RETRY_BATCH = 25
MAX_ACK_BYTES = 16 * 1024
_PERMANENT_HTTP_STATUSES = frozenset({400, 401, 403, 409, 422})
_ACK_HYDRATION_STATUSES = frozenset({"pending", "succeeded", "failed"})
_MAX_DELIVERY_LATENCY_S = 2_678_400.0
_SERVICE_ACCOUNT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}@[A-Za-z0-9.-]+\."
    r"iam\.gserviceaccount\.com$"
)

TokenFactory = Callable[[str], str]


class TicketEvaluationPublisherConfigurationError(ValueError):
    pass


def default_id_token_factory(
    audience: str,
    *,
    expected_service_account: str,
) -> str:
    """Mint a token and fail closed if ADC is not the configured identity."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    fetch_id_token: Callable[[Any, str], str] = id_token.fetch_id_token
    token = fetch_id_token(google_requests.Request(), audience)
    if not isinstance(token, str) or not token:
        raise RuntimeError("could not mint evaluation ingestion identity token")
    _assert_minted_token_identity(
        token,
        audience=audience,
        expected_service_account=expected_service_account,
    )
    return token


def _assert_minted_token_identity(
    token: str,
    *,
    audience: str,
    expected_service_account: str,
) -> None:
    """Check non-secret claims before sending; receiver verifies signature.

    ``fetch_id_token`` mints with ADC rather than accepting a service-account
    selector.  Comparing the minted token's audience and email claims makes
    the configured publisher identity an enforced invariant instead of
    documentation.  The private receiver remains the cryptographic verifier.
    """
    try:
        if len(token) > 16 * 1024:
            raise ValueError("oversized token")
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("invalid compact token")
        encoded_payload = parts[1]
        padding = "=" * (-len(encoded_payload) % 4)
        raw_payload = base64.b64decode(
            encoded_payload + padding,
            altchars=b"-_",
            validate=True,
        )
        claims = json.loads(raw_payload)
        if not isinstance(claims, dict):
            raise ValueError("invalid claims")
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise RuntimeError(
            "minted identity token claims could not be validated"
        ) from None
    if claims.get("aud") != audience or claims.get("email") != \
            expected_service_account:
        raise RuntimeError(
            "minted identity token does not match configured service account"
        )


class TicketEvaluationPublisher:
    """Drain pending outbox records into the private evaluation API.

    The same Google ID token is supplied to Cloud Run IAM in ``Authorization``
    and to application-level verification in
    ``X-ForUs-Workload-Authorization``.  Neither token nor response bodies are
    retained or logged.
    """

    def __init__(
        self,
        repo: TicketJobRepository,
        *,
        base_url: str,
        audience: str,
        service_account: str,
        client: Optional[httpx.AsyncClient] = None,
        token_factory: Optional[TokenFactory] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        if not isinstance(audience, str) or not audience.strip():
            raise TicketEvaluationPublisherConfigurationError(
                "ticket evaluation ingestion audience is required"
            )
        if not isinstance(service_account, str) or not _SERVICE_ACCOUNT_RE.fullmatch(
            service_account.strip()
        ):
            raise TicketEvaluationPublisherConfigurationError(
                "ticket evaluation publisher service account is invalid"
            )
        if isinstance(timeout_s, bool) or not 0.1 <= float(timeout_s) <= MAX_TIMEOUT_S:
            raise TicketEvaluationPublisherConfigurationError(
                "ticket evaluation publish timeout must be 0.1..60 seconds"
            )
        self.repo = repo
        self.audience = audience.strip()
        self.service_account = service_account.strip()
        self.timeout_s = float(timeout_s)
        if token_factory is None:
            self._token_factory = lambda token_audience: default_id_token_factory(
                token_audience,
                expected_service_account=self.service_account,
            )
        else:
            self._token_factory = token_factory
        self._request_timeout = _request_timeout(self.timeout_s)
        self._hydration_retry_request_timeout = _request_timeout(
            HYDRATION_RETRY_TIMEOUT_S
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self._request_timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def publish_pending(self, *, batch_size: int = 25) -> dict[str, int]:
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
            raise ValueError("ticket evaluation publish batch must be 1..100")
        documents = await self.repo.scan_ticket_evaluation_outbox(
            limit=batch_size,
        )
        return await self._publish_documents(documents)

    async def publish_execution(self, execution_id: str) -> dict[str, int]:
        """Best-effort delivery of one exact newly pending outbox event.

        Unlike the scheduled batch drain, this method runs on a participant-
        facing request.  Its deadline therefore includes OIDC token minting,
        HTTP transport, ACK parsing, and the local delivery transition.  If
        the budget expires, the event remains durable as ``pending``/``retry``
        and the reconciler owns recovery.
        """
        documents: list[tuple[str, dict[str, Any]]] = []
        try:
            async with asyncio.timeout(self.timeout_s):
                document = await self.repo.get_ticket_evaluation_outbox(
                    execution_id
                )
                documents = (
                    [(execution_id, document)]
                    if document is not None and document.get("state") == "pending"
                    else []
                )
                return await self._publish_documents(documents)
        except TimeoutError:
            return {
                "evaluation_scanned": len(documents),
                "evaluation_delivered": 0,
                # The invocation is known to have committed its outbox before
                # this method is called. Even if its point read timed out, one
                # exact event remains eligible for reconciler recovery.
                "evaluation_retried": 1,
                "evaluation_rejected": 0,
                "evaluation_errors": 0,
            }

    async def _publish_documents(
        self,
        documents: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, int]:
        """Publish an already bounded selection through one safe transport."""
        counts = {
            "evaluation_scanned": 0,
            "evaluation_delivered": 0,
            "evaluation_retried": 0,
            "evaluation_rejected": 0,
            "evaluation_errors": 0,
        }
        counts["evaluation_scanned"] = len(documents)
        if not documents:
            return counts

        try:
            token = await asyncio.to_thread(self._token_factory, self.audience)
            if not isinstance(token, str) or not token:
                raise RuntimeError("identity token factory returned no token")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - closed error code, no secret logging
            for execution_id, document in documents:
                await self._retry(
                    execution_id,
                    document,
                    error_code="OIDC_TOKEN_UNAVAILABLE",
                )
                counts["evaluation_retried"] += 1
            return counts

        headers = {
            "Authorization": f"Bearer {token}",
            "X-ForUs-Workload-Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        for execution_id, document in documents:
            digest = document.get("event_digest")
            try:
                event = TicketEvaluationEvent.model_validate(document.get("event"))
            except Exception:  # noqa: BLE001 - corrupt durable event
                await self._reject(
                    execution_id,
                    document,
                    error_code="INVALID_EVENT",
                )
                counts["evaluation_rejected"] += 1
                continue
            if event.execution_id != execution_id or not isinstance(digest, str):
                await self._reject(
                    execution_id,
                    document,
                    error_code="INVALID_EVENT_IDENTITY",
                )
                counts["evaluation_rejected"] += 1
                continue
            if event.canonical_digest() != digest:
                await self._reject(
                    execution_id,
                    document,
                    error_code="EVENT_DIGEST_MISMATCH",
                )
                counts["evaluation_rejected"] += 1
                continue

            endpoint = (
                f"{self.base_url}/internal/v1/ticket-evaluations/"
                f"{quote(execution_id, safe=':')}"
            )
            try:
                async with asyncio.timeout(self.timeout_s):
                    async with self._client.stream(
                        "PUT",
                        endpoint,
                        headers=headers,
                        json=event.model_dump(mode="json"),
                        timeout=self._request_timeout,
                    ) as response:
                        status_code = response.status_code
                        ack_body = (
                            await _read_bounded_ack(response)
                            if status_code in {200, 201}
                            else None
                        )
            except asyncio.CancelledError:
                raise
            except (httpx.RequestError, TimeoutError):
                await self._retry(
                    execution_id,
                    document,
                    error_code="DESTINATION_UNAVAILABLE",
                )
                counts["evaluation_retried"] += 1
                continue

            if status_code in {200, 201}:
                hydration_status = _ack_hydration_status(
                    ack_body, execution_id,
                )
                if hydration_status is not None:
                    _emit_delivery_ack_metrics(
                        hydration_status,
                        staged_at=document.get("created_at"),
                    )
                    try:
                        updated = await self.repo.mark_ticket_evaluation_delivered(
                            execution_id,
                            event_digest=digest,
                        )
                    except Exception:  # noqa: BLE001 - retried next invocation
                        counts["evaluation_errors"] += 1
                    else:
                        if updated.get("state") == "delivered":
                            counts["evaluation_delivered"] += 1
                        else:
                            counts["evaluation_errors"] += 1
                    continue
                await self._retry(
                    execution_id,
                    document,
                    error_code="INVALID_ACK",
                )
                counts["evaluation_retried"] += 1
                continue

            error_code = f"HTTP_{status_code}"
            if status_code in _PERMANENT_HTTP_STATUSES:
                await self._reject(
                    execution_id,
                    document,
                    error_code=error_code,
                )
                counts["evaluation_rejected"] += 1
            else:
                await self._retry(
                    execution_id,
                    document,
                    error_code=error_code,
                )
                counts["evaluation_retried"] += 1
        return counts

    async def retry_due_hydrations(self, *, limit: int = 20) -> dict[str, int]:
        """Ask the private receiver to retry its due DevRev enrichments.

        This runs independently of outbox delivery: a run is durably accepted
        even when DevRev is transiently unavailable, so a later reconciler tick
        must revisit hydration even if there are no new producer events.  Its
        reconciler-only deadline includes token minting and the complete ACK.
        """
        if isinstance(limit, bool) or not 1 <= limit <= MAX_HYDRATION_RETRY_BATCH:
            raise ValueError("ticket evaluation hydration retry limit must be 1..25")
        counts = {
            "evaluation_hydration_attempted": 0,
            "evaluation_hydration_succeeded": 0,
            "evaluation_hydration_errors": 0,
        }
        try:
            # This endpoint is called only by the scheduled reconciler.  Its
            # 25-item/5-way batch has a separate 45s budget so the 10s fast
            # publisher used by participant-facing routes never inherits it.
            async with asyncio.timeout(HYDRATION_RETRY_TIMEOUT_S):
                try:
                    token = await asyncio.to_thread(
                        self._token_factory, self.audience,
                    )
                    if not isinstance(token, str) or not token:
                        raise RuntimeError(
                            "identity token factory returned no token"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - no token/error logging
                    counts["evaluation_hydration_errors"] = 1
                    return counts

                authorization = f"Bearer {token}"
                async with self._client.stream(
                    "POST",
                    f"{self.base_url}/internal/v1/ticket-evaluations:retry-hydration",
                    params={"limit": limit},
                    headers={
                        "Authorization": authorization,
                        "X-ForUs-Workload-Authorization": authorization,
                        "Content-Type": "application/json",
                    },
                    content=b"",
                    timeout=self._hydration_retry_request_timeout,
                ) as response:
                    status_code = response.status_code
                    ack_body = (
                        await _read_bounded_ack(response)
                        if status_code == 200
                        else None
                    )
        except asyncio.CancelledError:
            raise
        except (httpx.RequestError, TimeoutError):
            counts["evaluation_hydration_errors"] = 1
            return counts

        acknowledgement = _valid_hydration_ack(ack_body)
        if status_code != 200 or acknowledgement is None:
            counts["evaluation_hydration_errors"] = 1
            return counts
        attempted, succeeded, errors = acknowledgement
        counts["evaluation_hydration_attempted"] = attempted
        counts["evaluation_hydration_succeeded"] = succeeded
        counts["evaluation_hydration_errors"] = errors
        return counts

    async def _retry(
        self,
        execution_id: str,
        document: dict[str, Any],
        *,
        error_code: str,
    ) -> None:
        now = utcnow()
        prior_attempts = int(document.get("attempt_count", 0))
        delay_s = min(3600, 5 * (2 ** min(prior_attempts, 9)))
        await self.repo.record_ticket_evaluation_delivery_failure(
            execution_id,
            event_digest=str(document.get("event_digest") or ""),
            error_code=error_code,
            retryable=True,
            next_attempt_at=now + timedelta(seconds=delay_s),
            observed_at=now,
        )

    async def _reject(
        self,
        execution_id: str,
        document: dict[str, Any],
        *,
        error_code: str,
    ) -> None:
        await self.repo.record_ticket_evaluation_delivery_failure(
            execution_id,
            event_digest=str(document.get("event_digest") or ""),
            error_code=error_code,
            retryable=False,
        )


async def _read_bounded_ack(response: httpx.Response) -> Optional[bytes]:
    raw_length = response.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError:
            return None
        if content_length < 0 or content_length > MAX_ACK_BYTES:
            return None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_ACK_BYTES:
            return None
        body.extend(chunk)
    return bytes(body)


def _ack_hydration_status(
    body: Optional[bytes], execution_id: str,
) -> Optional[str]:
    """Validate the private ACK and return one closed visibility state.

    ``unknown`` keeps rolling deploys compatible with an older ingestion
    revision that predates ``hydration_status``.  Any supplied value outside
    the receiver's closed enum invalidates the ACK instead of becoming a log
    label or allowing an ambiguous delivery to be terminalized locally.
    """
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    if not (
        isinstance(payload, dict)
        and payload.get("accepted") is True
        and payload.get("execution_id") == execution_id
        and isinstance(payload.get("replayed"), bool)
    ):
        return None
    hydration_status = payload.get("hydration_status", "unknown")
    if hydration_status == "unknown" and "hydration_status" not in payload:
        return "unknown"
    if not isinstance(hydration_status, str) or (
        hydration_status not in _ACK_HYDRATION_STATUSES
    ):
        return None
    return hydration_status


def _emit_delivery_ack_metrics(
    hydration_status: str,
    *,
    staged_at: Any,
) -> None:
    """Emit ACK status and commit-to-ACK latency without business data."""
    try:
        if not isinstance(staged_at, datetime):
            raise TypeError("outbox staging timestamp is unavailable")
        age_s = max(0.0, (utcnow() - staged_at).total_seconds())
    except (TypeError, ValueError):
        age_s = 0.0
    age_s = min(age_s, _MAX_DELIVERY_LATENCY_S)
    for metric, value in (
        ("ticket_evaluation_delivery_count", 1),
        ("ticket_evaluation_delivery_latency_seconds", age_s),
    ):
        try:
            ticket_metrics.emit(
                metric,
                value,
                hydration_status=hydration_status,
            )
        except (TypeError, ValueError):
            # Observability can never turn a valid durable ACK into a retry.
            continue


def _valid_hydration_ack(
    body: Optional[bytes],
) -> Optional[tuple[int, int, int]]:
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or frozenset(payload) not in {
        frozenset({"attempted", "succeeded"}),
        frozenset({"attempted", "succeeded", "errors"}),
    }:
        return None
    attempted = payload.get("attempted")
    succeeded = payload.get("succeeded")
    errors = payload.get("errors", 0)
    if (
        isinstance(attempted, bool)
        or isinstance(succeeded, bool)
        or isinstance(errors, bool)
        or not isinstance(attempted, int)
        or not isinstance(succeeded, int)
        or not isinstance(errors, int)
        or attempted < 0
        or succeeded < 0
        or errors < 0
        or succeeded > attempted
    ):
        return None
    return attempted, succeeded, errors


def _validated_base_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise TicketEvaluationPublisherConfigurationError(
            "ticket evaluation ingestion URL is required"
        )
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        raise TicketEvaluationPublisherConfigurationError(
            "ticket evaluation ingestion URL must be a canonical https origin"
        )
    return value


def _request_timeout(ack_timeout_s: float) -> httpx.Timeout:
    """Keep destination failures fast while allowing the bounded ingest ACK.

    HTTPX phase timeouts are inactivity bounds, not a total deadline.  The
    surrounding ``asyncio.timeout`` owns that wall-clock invariant; these
    values prevent DNS/connect/pool/write stalls from consuming its full ACK
    budget before the receiver has even started hydration.
    """
    short_phase_s = min(CONNECT_TIMEOUT_S, ack_timeout_s)
    return httpx.Timeout(
        connect=short_phase_s,
        read=ack_timeout_s,
        write=short_phase_s,
        pool=short_phase_s,
    )


__all__ = [
    "TicketEvaluationPublisher",
    "TicketEvaluationPublisherConfigurationError",
    "default_id_token_factory",
]
