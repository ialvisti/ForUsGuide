"""
Firestore execution logger.

Logs structured API execution data to a Firestore ``execution_logs``
collection.  Logging failures are caught and never propagate to the
API response so that a Firestore outage cannot break the service.

Schema versioning (Stage 4)
---------------------------
Both documents carry ``schema_version``. Every field added since v0 is
*additive*: a legacy document simply has no ``schema_version`` and no
``provenance``, and
:func:`data_pipeline.ticket_review_provenance.execution_schema_version`
reads that absence as v0 rather than as a corrupt v1 record.

Provenance arrives as one explicit ``provenance`` argument built by trusted
server-side code, never by inspecting ``request_data``/``response_data``. That
is the whole point: a caller controls its own request body and can put a
participant's name in ``metadata.model`` or ``source_articles[].article_id``,
so scraping provenance out of a payload would quietly turn this collection
into a second PII retention path. The existing aggregate-only rule still
holds — no request body, response text, chunk text, participant field, token,
or raw external identifier is ever written here.
"""

import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from google.cloud import firestore

logger = logging.getLogger(__name__)

# The additive schema version both collections stamp. Kept as a literal rather
# than imported so this hot-path module stays free of the console contracts.
EXECUTION_LOG_SCHEMA_VERSION = 1

# A fixed, parseable prefix so a failed write is a log-based metric rather than
# free text. The reviewed ``api.metrics`` catalog is closed and belongs to the
# ticket flow, so widening it is not this module's call.
WRITE_FAILURE_METRIC = "execution_log_write_failed"

# Only these keys may cross from a provenance fragment into a document. An
# unknown key is dropped, so a future caller cannot widen the schema by
# accident.
_PROVENANCE_KEYS = frozenset(
    {
        "provenance_schema_version",
        "correlation",
        "job",
        "pipeline",
        "retrieval",
        "response_sha256",
        "missing_provenance",
    }
)


def _safe_provenance(value: Any) -> Optional[Dict[str, Any]]:
    """Keep only the allowlisted provenance keys, or nothing at all.

    Deliberately total: it cannot raise, because document construction runs
    outside the write's ``try`` block and an exception here would break the
    "logging never changes the API result" property.
    """
    if not isinstance(value, Mapping):
        return None
    kept = {
        key: value[key]
        for key in _PROVENANCE_KEYS
        if key in value and not isinstance(value[key], (bytes, bytearray))
    }
    return kept or None


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
        metrics_sink: Optional[Callable[[str, str], None]] = None,
    ):
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        if not database:
            raise ValueError("database must be explicit")
        self.db = firestore.AsyncClient(project=project_id, database=database)
        self.collection = self.db.collection("execution_logs")
        self.retention_days = retention_days
        self._metrics_sink = metrics_sink

    def _record_write_failure(self, collection: str, error: BaseException) -> None:
        """Report a failed write as a structured metric, then swallow it.

        Read through ``getattr`` because the runtime-safety tests construct this
        class with ``__new__`` and set only the two attributes they need; an
        unconditional ``self._metrics_sink`` would turn those tests into
        ``AttributeError`` and, worse, would make a Firestore outage raise here.
        """
        error_type = type(error).__name__
        sink = getattr(self, "_metrics_sink", None)
        if sink is not None:
            try:
                sink(collection, error_type)
            except Exception:  # noqa: BLE001 - a metric must never break a request
                logger.debug("execution log metric sink failed", exc_info=False)
        logger.error(
            "%s collection=%s error_type=%s",
            WRITE_FAILURE_METRIC,
            collection,
            error_type,
        )

    async def log_execution(
        self,
        request_id: str,
        endpoint: str,
        duration_ms: float,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        error: Optional[str] = None,
        provenance: Optional[Mapping[str, Any]] = None,
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
        provenance : Mapping | None
            An already-sanitized fragment from
            :meth:`data_pipeline.ticket_review_provenance.ExecutionProvenance.as_document_fields`.
            Never derived from ``request_data``/``response_data``.
        """
        now = datetime.now(timezone.utc)
        metadata = response_data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        coverage_gaps = response_data.get("coverage_gaps")
        source_articles = response_data.get("source_articles")
        confidence = response_data.get("confidence")
        doc = {
            # Additive: v0 documents simply have no schema_version.
            "schema_version": EXECUTION_LOG_SCHEMA_VERSION,
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
        sanitized = _safe_provenance(provenance)
        if sanitized is not None:
            doc["provenance"] = sanitized

        try:
            await self.collection.add(doc)
        except Exception as e:
            self._record_write_failure("execution_logs", e)

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
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Log one end-to-end ticket orchestration to the ``ticket_executions``
        collection. Like ``log_execution`` it never propagates failures."""
        now = datetime.now(timezone.utc)
        # This collection is operational telemetry, not the durable job or
        # reconciliation ledger. Keep only bounded aggregates: copying job,
        # idempotency, or upstream IDs here would create a second unbounded
        # PII-bearing retention path outside the ticket payload TTL.
        #
        # Stage 4 does not relax that. ``request_id``, ``ticket_job_id`` and
        # ``idempotency_key`` are still never written from these parameters:
        # they are caller-influenced strings. The only identifiers that may
        # appear are inside ``provenance``, where the correlation reference is
        # a keyed HMAC of the DON and the job id has already been checked
        # against the server-minted shape.
        doc = {
            "schema_version": EXECUTION_LOG_SCHEMA_VERSION,
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
        sanitized = _safe_provenance(provenance)
        if sanitized is not None:
            doc["provenance"] = sanitized
        try:
            await self.db.collection("ticket_executions").add(doc)
        except Exception as e:
            self._record_write_failure("ticket_executions", e)
