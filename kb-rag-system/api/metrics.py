"""
Contadores in-process del ticket handler (Task 11).

Fuente barata para dashboards/alertas vía logs estructurados: cada increment
emite una línea ``ticket_metric`` que Cloud Logging convierte en log-based
metric. El snapshot en memoria alimenta tests y debugging local; NO es un
sistema de métricas distribuido (cada instancia cuenta lo suyo).

Nombres emitidos (ver HANDLE_TICKET_RUNBOOK.md):
  ticket_jobs_accepted / replayed / conflicted
  ticket_jobs_terminal{state}
  ticket_poll_not_found / ticket_poll_forbidden
  ticket_rate_limited / ticket_outstanding_capped
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Dict, Optional

logger = logging.getLogger("ticket_metrics")

_counters: Counter = Counter()

# Labels aprobados: nunca se emite texto del ticket, IDs de participante/plan,
# errores sin procesar, resultados del LLM ni bodies del upstream (Tarea 11
# Paso 1). job_hash y trace_id son los únicos identificadores.
_ALLOWED_LABELS = frozenset({
    "state", "step", "route", "code", "env", "role", "reason",
})


def increment(name: str, **labels: str) -> None:
    safe = {k: v for k, v in labels.items() if k in _ALLOWED_LABELS}
    key = name if not safe else (
        name + "{" + ",".join(f"{k}={v}" for k, v in sorted(safe.items())) + "}"
    )
    _counters[key] += 1
    logger.info("ticket_metric %s=%d", key, _counters[key])


def emit(metric: str, value: float, *, job_hash: Optional[str] = None,
         trace_id: Optional[str] = None, **labels: Any) -> None:
    """Evento de métrica ESTRUCTURADO (Tarea 11 Paso 1): campos JSON estables
    para log-based metrics. Filtra labels a la allowlist; nunca PII/texto."""
    safe = {k: v for k, v in labels.items() if k in _ALLOWED_LABELS}
    event = {
        "metric": metric,
        "value": value,
        "labels": safe,
    }
    if job_hash is not None:
        event["job_hash"] = job_hash
    if trace_id is not None:
        event["trace_id"] = trace_id
    logger.info("ticket_metric_event %s", json.dumps(event, sort_keys=True))


def snapshot() -> Dict[str, int]:
    return dict(_counters)
