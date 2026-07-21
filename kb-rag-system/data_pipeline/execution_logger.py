"""
Firestore execution logger.

Logs structured API execution data to a Firestore ``execution_logs``
collection.  Logging failures are caught and never propagate to the
API response so that a Firestore outage cannot break the service.
"""

import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

logger = logging.getLogger(__name__)


def _safe_nonnegative_int(value: Any) -> int:
    """Normaliza contadores de telemetry sin propagar tipos del payload."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_duration_ms(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(duration):
        return 0.0
    return round(max(0.0, duration), 1)


def _safe_route_summary(value: Any) -> list[Dict[str, Optional[str]]]:
    if not isinstance(value, list):
        return []
    allowed_routes = {
        "knowledge_question", "generate_response", "needs_more_info",
    }
    allowed_execution = {
        "pending", "running", "succeeded", "failed", "timeout",
        "cancelled", "unprocessed",
    }
    allowed_scrape = {"ok", "partial", "failed", "timeout", "skipped"}
    result: list[Dict[str, Optional[str]]] = []
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        route = item.get("route")
        execution = item.get("execution_status")
        scrape = item.get("scrape_status")
        result.append({
            "route": (
                route if isinstance(route, str) and route in allowed_routes
                else "unknown"
            ),
            "execution_status": (
                execution
                if isinstance(execution, str) and execution in allowed_execution
                else "unknown"
            ),
            "scrape_status": (
                scrape
                if isinstance(scrape, str) and scrape in allowed_scrape
                else None
            ),
        })
    return result


class ExecutionLogger:
    """Logs API execution details to Firestore."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        *,
        database: str = "(default)",
        retention_days: int = 90,
    ):
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        if not database:
            raise ValueError("database must be explicit")
        self.db = firestore.AsyncClient(project=project_id, database=database)
        self.collection = self.db.collection("execution_logs")
        self.retention_days = retention_days

    async def log_execution(
        self,
        request_id: str,
        endpoint: str,
        duration_ms: float,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        error: Optional[str] = None,
    ) -> None:
        """Log a single API execution to Firestore.

        Parameters
        ----------
        request_id : str
            The unique request ID (from the ``X-Request-ID`` header).
        endpoint : str
            Logical name: ``"required_data"``, ``"generate_response"``,
            or ``"knowledge_question"``.
        duration_ms : float
            Wall-clock time for the request in milliseconds.
        request_data : dict
            Deserialized request body.
        response_data : dict
            Deserialized response body (or partial data available at
            logging time).
        error : str | None
            Error message if the request failed; ``None`` on success.
        """
        now = datetime.now(timezone.utc)
        metadata = response_data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        coverage_gaps = response_data.get("coverage_gaps")
        source_articles = response_data.get("source_articles")
        confidence = response_data.get("confidence")
        doc = {
            # X-Request-ID puede venir del caller. Sólo se conserva un hash
            # correlacionable; nunca texto, IDs externos ni errores raw.
            "request_id_hash": hashlib.sha256(
                request_id.encode("utf-8")
            ).hexdigest()[:24],
            "endpoint": endpoint if endpoint in {
                "required_data", "generate_response", "knowledge_question",
            } else "other",
            "timestamp": now,
            "expires_at": now + timedelta(days=self.retention_days),
            "duration_ms": _safe_duration_ms(duration_ms),
            "request_shape": {
                "has_inquiry": bool(
                    request_data.get("inquiry") or request_data.get("question")
                ),
                "has_topic": bool(request_data.get("topic")),
                "has_record_keeper": bool(request_data.get("record_keeper")),
                "has_plan_type": bool(request_data.get("plan_type")),
            },
            "response": {
                "confidence": confidence
                if isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                else None,
                "chunks_used": _safe_nonnegative_int(
                    metadata.get("chunks_used", 0)
                ),
                "coverage_gap_count": len(coverage_gaps)
                if isinstance(coverage_gaps, list)
                else 0,
                "source_article_count": len(source_articles)
                if isinstance(source_articles, list)
                else 0,
            },
            "llm_metadata": {
                "prompt_tokens": _safe_nonnegative_int(
                    metadata.get("prompt_tokens", 0)
                ),
                "completion_tokens": _safe_nonnegative_int(
                    metadata.get("completion_tokens", 0)
                ),
                "total_tokens": _safe_nonnegative_int(
                    metadata.get("total_tokens", 0)
                ),
            },
            "failed": error is not None,
        }

        try:
            await self.collection.add(doc)
        except Exception as e:
            logger.error(
                "Failed to log execution to Firestore; error_type=%s",
                type(e).__name__,
            )

    async def log_ticket_execution(
        self,
        request_id: str,
        ticket_job_id: Optional[str],
        mode: str,
        route_summary: list[Any],
        total_inquiries: int,
        forusbots_job_ids: list[Any],
        duration_ms: float,
        error: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        """Log one end-to-end ticket orchestration to the ``ticket_executions``
        collection. Like ``log_execution`` it never propagates failures."""
        now = datetime.now(timezone.utc)
        # This collection is operational telemetry, not the durable job or
        # reconciliation ledger. Keep only bounded aggregates: copying job,
        # idempotency, or upstream IDs here would create a second unbounded
        # PII-bearing retention path outside the ticket payload TTL.
        doc = {
            "ticket_handler_mode": mode if mode in {
                "disabled", "shadow", "knowledge_only", "full",
            } else "unknown",
            "timestamp": now,
            "expires_at": now + timedelta(days=self.retention_days),
            "duration_ms": _safe_duration_ms(duration_ms),
            "total_inquiries": _safe_nonnegative_int(total_inquiries),
            "route_summary": _safe_route_summary(route_summary),
            "forusbots_job_count": len({
                item for item in forusbots_job_ids
                if isinstance(item, str) and item
            }) if isinstance(forusbots_job_ids, list) else 0,
            "failed": error is not None,
        }
        try:
            await self.db.collection("ticket_executions").add(doc)
        except Exception as e:
            logger.error(
                "Failed to log ticket execution to Firestore; error_type=%s",
                type(e).__name__,
            )
