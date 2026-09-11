"""Durable evaluation recording for ticket-associated legacy RAG calls.

The legacy endpoints remain useful for general, non-ticket questions.  When a
caller supplies a validated DevRev ticket identity, this module gives those
endpoints the same journal -> transactional outbox -> private ingestion
contract as the durable ticket worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException, Request, status

from api.ticket_evaluation_models import chunk_evidence_from_result
from data_pipeline.ticket_job_repository import (
    DirectRagInvocationFailed,
    DirectRagInvocationInProgress,
    TicketEvaluationConflict,
)


logger = logging.getLogger(__name__)


_PRIVATE_DIAGNOSTIC_METADATA_KEYS = frozenset(
    {
        "detail",
        "details",
        "error",
        "error_message",
        "exception",
        "exception_message",
        "stack",
        "stack_trace",
        "traceback",
    }
)


@dataclass(frozen=True)
class DirectTicketEvaluationReservation:
    invocation_id: Optional[str]
    replay_response: Optional[dict[str, Any]] = None


def _durable_metadata(value: object) -> dict[str, Any]:
    """Drop raw diagnostic text while retaining bounded operational signals."""
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str)
        and key.strip().lower() not in _PRIVATE_DIAGNOSTIC_METADATA_KEYS
    }


async def begin_direct_ticket_evaluation(
    request: Request,
    *,
    ticket_id: Optional[str],
    route: str,
    inquiry: str,
    topic: str,
    context_fingerprint: Optional[str] = None,
) -> DirectTicketEvaluationReservation:
    """Persist intent before the endpoint invokes the RAG engine."""
    if ticket_id is None:
        return DirectTicketEvaluationReservation(invocation_id=None)
    repo = getattr(request.app.state, "ticket_repo", None)
    if repo is None:
        raise RuntimeError("ticket evaluation repository is unavailable")
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("ticket evaluation request identity is unavailable")
    tenant_id = getattr(request.state, "tenant_id", None)
    principal_id = getattr(request.state, "principal_id", None)
    idempotency_key = request.headers.get("Idempotency-Key")
    try:
        invocation_id, replay_response = (
            await repo.reserve_direct_rag_invocation(
                request_id=request_id,
                ticket_id=ticket_id,
                route=route,
                inquiry=inquiry,
                topic=topic,
                tenant_id=tenant_id if isinstance(tenant_id, str) else None,
                principal_id=(
                    principal_id if isinstance(principal_id, str) else None
                ),
                idempotency_key=idempotency_key,
                **({"context_fingerprint": context_fingerprint} if context_fingerprint is not None else {}),
            )
        )
    except DirectRagInvocationInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENT_REQUEST_IN_PROGRESS",
                "message": "The same ticket request is still processing.",
            },
            headers={"Retry-After": "2"},
        ) from exc
    except DirectRagInvocationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENT_REQUEST_FAILED",
                "message": "The same ticket request already failed.",
            },
        ) from exc
    except TicketEvaluationConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_PAYLOAD_CONFLICT",
                "message": (
                    "Idempotency-Key was already used for different input."
                ),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "IDEMPOTENCY_KEY_INVALID",
                "message": (
                    "Idempotency-Key must be 8-128 characters using letters, "
                    "digits, '.', '_', ':' or '-'."
                ),
            },
        ) from exc
    if not isinstance(invocation_id, str) or not invocation_id:
        raise RuntimeError("ticket evaluation journal returned no identity")
    return DirectTicketEvaluationReservation(
        invocation_id=invocation_id,
        replay_response=replay_response,
    )


def direct_replay_response(response: Any) -> dict[str, Any]:
    """Minimize a successful public response for durable replay.

    Full retrieved chunk content remains response-only data and is never put
    in the idempotency ledger.  n8n consumes the answer/decision fields, which
    remain identical on a replay.
    """
    if hasattr(response, "model_dump"):
        raw = response.model_dump(mode="python")
    elif isinstance(response, Mapping):
        raw = dict(response)
    else:
        raise TypeError("direct RAG response is not serializable")
    replay = dict(raw)
    if "used_chunks" in replay:
        replay["used_chunks"] = []
    metadata = replay.get("metadata")
    if isinstance(metadata, Mapping):
        replay["metadata"] = _durable_metadata(metadata)
    return replay


def direct_success_entry(
    *,
    route: str,
    inquiry: str,
    topic: str,
    response: Any,
) -> dict[str, Any]:
    """Convert a public response into the existing bounded event input."""
    from data_pipeline.response_handoff import response_requires_review

    if hasattr(response, "model_dump"):
        raw = response.model_dump(mode="python")
    elif isinstance(response, Mapping):
        raw = dict(response)
    else:
        raise TypeError("direct RAG response is not serializable")
    result_key = (
        "knowledge_answer" if route == "knowledge_question"
        else "generate_response"
    )
    raw_chunks = raw.get("used_chunks")
    # Full retrieval content belongs in the HTTP response, not in Firestore.
    # Preserve only the bounded evidence projection built below, mirroring the
    # durable worker's checkpoint minimization contract.
    durable_response = dict(raw)
    if "used_chunks" in durable_response:
        durable_response["used_chunks"] = []
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        durable_response["metadata"] = _durable_metadata(metadata)
    degraded = bool(
        isinstance(metadata, Mapping)
        and metadata.get("error") is not None
    )
    review_required = response_requires_review(raw.get("response"), metadata)
    entry: dict[str, Any] = {
        "route": route,
        "execution_status": "partial" if degraded else "succeeded",
        "participant_reply_safe": not degraded and not review_required,
        "degraded": degraded,
        "result": {
            "inquiry": inquiry,
            "topic": topic,
            result_key: durable_response,
        },
    }
    if review_required:
        entry["human_review_required"] = True
    if degraded:
        raw_failure_kind = (
            metadata.get("retrieval_failure_kind")
            if isinstance(metadata, Mapping) else None
        )
        allowed_failure_kinds = {
            "timeout", "transport", "rate_limit", "server_error",
            "circuit_open", "unsafe_query", "unknown",
        }
        failure_kind = (
            raw_failure_kind
            if raw_failure_kind in allowed_failure_kinds else "unknown"
        )
        retryable = bool(
            isinstance(metadata, Mapping)
            and metadata.get("retrieval_retryable") is True
        )
        entry["diagnostics"] = {
            "failure_phase": "direct_rag_response",
            "failure_kind": failure_kind,
        }
        entry["error"] = {
            "code": "DIRECT_RAG_DEGRADED",
            "retryable": retryable,
        }
    chunks: list[dict[str, Any]] = []
    if isinstance(raw_chunks, list):
        for raw_chunk in raw_chunks[:20]:
            if not isinstance(raw_chunk, Mapping):
                continue
            try:
                evidence = chunk_evidence_from_result(dict(raw_chunk))
            except (TypeError, ValueError):
                continue
            chunks.append(evidence.model_dump(mode="python"))
    if chunks:
        entry["evaluation_evidence"] = {"chunks": chunks}
    return entry


def direct_failure_entry(
    *,
    route: str,
    inquiry: str,
    topic: str,
    code: str = "DIRECT_RAG_FAILED",
) -> dict[str, Any]:
    """Return a content-free terminal failure entry; never persist exceptions."""
    result_key = (
        "knowledge_answer" if route == "knowledge_question"
        else "generate_response"
    )
    return {
        "route": route,
        "execution_status": "failed",
        "participant_reply_safe": False,
        "degraded": True,
        "result": {
            "inquiry": inquiry,
            "topic": topic,
            result_key: {},
        },
        "diagnostics": {"failure_phase": "direct_rag_endpoint"},
        "error": {"code": code, "retryable": False},
    }


async def complete_direct_ticket_evaluation(
    request: Request,
    invocation_id: Optional[str],
    entry: dict[str, Any],
    *,
    replay_response: Optional[dict[str, Any]] = None,
    replay_error_code: Optional[str] = None,
) -> None:
    """Commit journal/outbox, then offer that exact event for delivery.

    Delivery is deliberately best-effort.  The durable outbox is already
    committed, so any token, network, receiver, or acknowledgement failure is
    repaired by the scheduled reconciler.
    """
    if invocation_id is None:
        return
    repo = getattr(request.app.state, "ticket_repo", None)
    if repo is None:
        raise RuntimeError("ticket evaluation repository is unavailable")
    await repo.complete_rag_invocation(
        invocation_id,
        entry,
        replay_response=replay_response,
        replay_error_code=replay_error_code,
    )

    publisher = getattr(
        request.app.state, "ticket_evaluation_publisher", None,
    )
    if publisher is None:
        return
    try:
        await publisher.publish_execution(invocation_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - outbox reconciler owns recovery
        logger.warning("immediate ticket evaluation delivery deferred")


async def record_direct_failure_best_effort(
    request: Request,
    invocation_id: Optional[str],
    *,
    route: str,
    inquiry: str,
    topic: str,
) -> None:
    """Try to close a failed provider call without replacing its error."""
    if invocation_id is None:
        return
    try:
        await complete_direct_ticket_evaluation(
            request,
            invocation_id,
            direct_failure_entry(
                route=route,
                inquiry=inquiry,
                topic=topic,
            ),
            replay_error_code="DIRECT_RAG_FAILED",
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - stale intent recovery is authoritative
        logger.error("direct ticket evaluation failure recording deferred")


__all__ = [
    "begin_direct_ticket_evaluation",
    "complete_direct_ticket_evaluation",
    "DirectTicketEvaluationReservation",
    "direct_failure_entry",
    "direct_replay_response",
    "direct_success_entry",
    "record_direct_failure_best_effort",
]
