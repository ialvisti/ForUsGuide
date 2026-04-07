"""
Firestore execution logger.

Logs structured API execution data to a Firestore ``execution_logs``
collection.  Logging failures are caught and never propagate to the
API response so that a Firestore outage cannot break the service.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

logger = logging.getLogger(__name__)


class ExecutionLogger:
    """Logs API execution details to Firestore."""

    def __init__(self, project_id: Optional[str] = None):
        self.db = firestore.AsyncClient(project=project_id)
        self.collection = self.db.collection("execution_logs")

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
        doc = {
            "request_id": request_id,
            "endpoint": endpoint,
            "timestamp": datetime.now(timezone.utc),
            "duration_ms": round(duration_ms, 1),
            "request": {
                "inquiry": str(request_data.get("inquiry", request_data.get("question", "")))[:500],
                "topic": request_data.get("topic", ""),
                "record_keeper": request_data.get("record_keeper"),
                "plan_type": request_data.get("plan_type", ""),
            },
            "response": {
                "decision": response_data.get("decision"),
                "confidence": response_data.get("confidence"),
                "outcome": response_data.get("response", {}).get("outcome") if isinstance(response_data.get("response"), dict) else None,
                "chunks_used": response_data.get("metadata", {}).get("chunks_used", 0) if isinstance(response_data.get("metadata"), dict) else 0,
                "coverage_gaps": response_data.get("coverage_gaps", []),
            },
            "llm_metadata": {
                "model": response_data.get("metadata", {}).get("model", "") if isinstance(response_data.get("metadata"), dict) else "",
                "prompt_tokens": response_data.get("metadata", {}).get("prompt_tokens", 0) if isinstance(response_data.get("metadata"), dict) else 0,
                "completion_tokens": response_data.get("metadata", {}).get("completion_tokens", 0) if isinstance(response_data.get("metadata"), dict) else 0,
                "total_tokens": response_data.get("metadata", {}).get("total_tokens", 0) if isinstance(response_data.get("metadata"), dict) else 0,
            },
            "source_articles": [
                sa.get("article_id", "") if isinstance(sa, dict) else str(sa)
                for sa in response_data.get("source_articles", [])
            ],
            "error": error,
        }

        try:
            await self.collection.add(doc)
        except Exception as e:
            logger.error(f"Failed to log execution to Firestore: {e}")
