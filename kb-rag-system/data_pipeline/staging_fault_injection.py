"""
Fault injection determinístico SÓLO para staging (plan de finalización,
Tarea 7 Paso 7a). La SA E2E llama al **producer** con un contrato de test
autenticado; sólo ``APP_ENV=staging`` persiste un ``fault_plan``
server-signed en el job sintético. Cloud Tasks invoca el worker interno y
éste valida firma/env/config antes de inyectar. NO hay endpoint de fault en
el worker. Producción rechaza tanto el header de test como el fault_plan.

Puntos de inyección soportados:

- ``post_checkpoint``     crash determinístico tras el checkpoint de una inquiry
- ``timeout_reset``       timeout/reset del cliente externo
- ``dependency_down``     dependencia caída (LLM/Pinecone/ForusBots)
- ``lease_lost``          pérdida de lease (simula fencing por el reconciliador)

La firma HMAC usa ``TICKET_FAULT_SIGNING_SECRET`` (secret staging-only). El
worker recomputa la firma; una firma alterada o un principal incorrecto se
rechazan. El secreto NUNCA se loggea.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FAULT_TEST_HEADER = "X-ForUs-Fault-Plan"
_VALID_POINTS = frozenset({
    "post_checkpoint", "timeout_reset", "dependency_down", "lease_lost",
})


class FaultInjectionRejected(Exception):
    """El fault plan no es válido para este entorno/configuración/firma."""


class InjectedFault(Exception):
    """Fallo inyectado deliberadamente (staging): el worker lo trata como el
    fallo real que simula."""

    def __init__(self, point: str):
        super().__init__(f"injected fault: {point}")
        self.point = point


def _canonical(plan: Dict[str, Any]) -> bytes:
    body = {k: plan[k] for k in sorted(plan) if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_fault_plan(plan: Dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), _canonical(plan),
                    hashlib.sha256).hexdigest()


def build_signed_fault_plan(*, point: str, inquiry_index: int,
                            principal_id: str, secret: str) -> Dict[str, Any]:
    """Construye un fault plan firmado (lado producer/E2E, staging)."""
    if point not in _VALID_POINTS:
        raise FaultInjectionRejected(f"punto de inyección desconocido: {point}")
    plan = {
        "point": point,
        "inquiry_index": inquiry_index,
        "principal_id": principal_id,
        "env": "staging",
    }
    plan["signature"] = sign_fault_plan(plan, secret)
    return plan


def accept_fault_plan_from_request(
    *, app_env: str, header_value: Optional[str], principal_id: str,
    secret: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Lado producer: valida el header de test y devuelve el plan a persistir
    en el job sintético, o None si no hay plan. Producción rechaza SIEMPRE."""
    if header_value is None:
        return None
    if app_env != "staging":
        raise FaultInjectionRejected(
            "fault injection sólo está permitido en APP_ENV=staging")
    if not secret:
        raise FaultInjectionRejected("TICKET_FAULT_SIGNING_SECRET no configurado")
    try:
        plan = json.loads(header_value)
    except (ValueError, TypeError) as exc:
        raise FaultInjectionRejected("fault plan no es JSON válido") from exc
    _verify(plan, principal_id=principal_id, secret=secret, app_env=app_env)
    return plan


def maybe_raise(
    plan: Optional[Dict[str, Any]], *, point: str, inquiry_index: Optional[int],
    app_env: str, principal_id: str, secret: Optional[str],
) -> None:
    """Lado worker: valida firma/env/principal ANTES de inyectar. Un plan sin
    firma válida, de otro entorno o principal, se rechaza (no se inyecta)."""
    if plan is None:
        return
    if app_env != "staging":
        raise FaultInjectionRejected("producción no inyecta faults")
    if not secret:
        raise FaultInjectionRejected("TICKET_FAULT_SIGNING_SECRET no configurado")
    _verify(plan, principal_id=principal_id, secret=secret, app_env=app_env)
    if plan.get("point") != point:
        return
    if inquiry_index is not None and plan.get("inquiry_index") != inquiry_index:
        return
    logger.warning("inyectando fault staging point=%s inquiry=%s",
                   point, inquiry_index)
    raise InjectedFault(point)


def _verify(plan: Dict[str, Any], *, principal_id: str, secret: str,
            app_env: str) -> None:
    if plan.get("env") != "staging" or app_env != "staging":
        raise FaultInjectionRejected("fault plan fuera de staging")
    if plan.get("principal_id") != principal_id:
        raise FaultInjectionRejected("principal del fault plan no coincide")
    if plan.get("point") not in _VALID_POINTS:
        raise FaultInjectionRejected("punto de inyección desconocido")
    expected = sign_fault_plan(plan, secret)
    if not hmac.compare_digest(str(plan.get("signature", "")), expected):
        raise FaultInjectionRejected("firma del fault plan inválida")
