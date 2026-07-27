"""
Rate limiting por principal (Task 6 del plan, HT-06 / OWASP API4).

Ventana fija en memoria de proceso: mitiga abuso/DoS por instancia. El
límite GLOBAL de ejecución lo impone la cola de Cloud Tasks (dispatch/rate
configurados según la capacidad real de ForusBots y cuotas LLM); este
limiter protege el endpoint productor, no lo sustituye.
"""

from __future__ import annotations

import time
from typing import Dict, Tuple


class FixedWindowRateLimiter:
    """Contador por (clave, ventana). Devuelve (permitido, retry_after_s)."""

    def __init__(self, window_s: float = 60.0, max_keys: int = 10_000):
        self._window_s = window_s
        self._max_keys = max_keys
        self._windows: Dict[Tuple[str, ...], Tuple[float, int]] = {}

    def check(self, key: Tuple[str, ...], limit: int) -> Tuple[bool, int]:
        now = time.monotonic()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= self._window_s:
            start, count = now, 0
        count += 1
        self._windows[key] = (start, count)
        if len(self._windows) > self._max_keys:
            self._prune(now)
        if limit > 0 and count > limit:
            return False, max(1, int(self._window_s - (now - start)) + 1)
        return True, 0

    def _prune(self, now: float) -> None:
        expired = [k for k, (start, _c) in self._windows.items()
                   if now - start >= self._window_s]
        for k in expired:
            self._windows.pop(k, None)
