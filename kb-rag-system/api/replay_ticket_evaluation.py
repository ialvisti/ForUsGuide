"""Audited manual replay for a durable ticket-evaluation dead letter.

This module deliberately lives in ``api`` because the hardened runtime image
copies that package and excludes the development-only ``scripts`` tree.

Usage::

    APP_ROLE=reconciler python -m api.replay_ticket_evaluation \
        --execution-id JOB_ID:INQUIRY_INDEX \
        --event-digest SHA256

The command never sends the event itself. It atomically returns the existing,
immutable outbox record to ``pending`` so the normal authenticated reconciler
delivery path handles it on the next tick.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from typing import Optional, Sequence

from data_pipeline.ticket_job_repository import (
    JobNotFound,
    TicketEvaluationConflict,
    TicketEvaluationReplayNotAllowed,
)

logger = logging.getLogger("replay_ticket_evaluation")


def _execution_hash(execution_id: str) -> str:
    return hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]


def resolve_authenticated_operator(expected_service_account: str) -> str:
    """Refresh ADC and require the configured reconciler service account.

    The CLI deliberately has no operator argument: an arbitrary string is not
    authentication. User ADC therefore fails closed; operators invoke this
    command through the reviewed reconciler workload identity (including
    explicit service-account impersonation).
    """
    try:
        import google.auth
        from google.auth.transport import requests as google_requests

        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        credentials.refresh(google_requests.Request())
        observed = getattr(credentials, "service_account_email", None)
    except Exception:  # noqa: BLE001 - never surface credential/token details
        raise RuntimeError(
            "could not authenticate evaluation replay operator"
        ) from None
    if observed != expected_service_account:
        raise RuntimeError(
            "authenticated operator is not the configured service account"
        )
    return expected_service_account


async def replay_dead_letter(
    repo,
    *,
    execution_id: str,
    event_digest: str,
    authenticated_operator: str,
) -> int:
    try:
        await repo.replay_ticket_evaluation_dead_letter(
            execution_id,
            event_digest=event_digest,
            operator_id=authenticated_operator,
        )
    except JobNotFound:
        logger.error(
            "evaluation replay refused: execution_hash=%s not_found",
            _execution_hash(execution_id),
        )
        return 2
    except TicketEvaluationConflict:
        logger.error(
            "evaluation replay refused: execution_hash=%s digest_conflict",
            _execution_hash(execution_id),
        )
        return 3
    except TicketEvaluationReplayNotAllowed:
        logger.error(
            "evaluation replay refused: execution_hash=%s not_dead_letter",
            _execution_hash(execution_id),
        )
        return 4
    except ValueError:
        logger.error(
            "evaluation replay refused: execution_hash=%s invalid_input",
            _execution_hash(execution_id),
        )
        return 5
    logger.info(
        "evaluation replay queued: execution_hash=%s",
        _execution_hash(execution_id),
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay one durable ticket-evaluation dead letter",
    )
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--event-digest", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    from data_pipeline.ticket_reconciler import _build_from_settings

    repo, queue, publisher = _build_from_settings()
    from api.config import settings

    async def _run() -> int:
        try:
            authenticated_operator = await asyncio.to_thread(
                resolve_authenticated_operator,
                settings.TICKET_EVALUATION_PUBLISHER_SERVICE_ACCOUNT,
            )
            return await replay_dead_letter(
                repo,
                execution_id=args.execution_id,
                event_digest=args.event_digest,
                authenticated_operator=authenticated_operator,
            )
        finally:
            await queue.aclose()
            if publisher is not None:
                await publisher.aclose()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
