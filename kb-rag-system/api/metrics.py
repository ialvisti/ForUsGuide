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

import logging
from collections import Counter
from typing import Dict

logger = logging.getLogger("ticket_metrics")

_counters: Counter = Counter()


def increment(name: str, **labels: str) -> None:
    key = name if not labels else (
        name + "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
    )
    _counters[key] += 1
    logger.info("ticket_metric %s=%d", key, _counters[key])


def snapshot() -> Dict[str, int]:
    return dict(_counters)
