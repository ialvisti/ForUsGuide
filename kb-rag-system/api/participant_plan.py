"""
Autorización canónica participant-plan-tenant (plan Tarea 4 Paso 1).

El resultado de autorización es SERVER-OWNED: ``tenant_id``, participante,
plan y record keeper provienen de la fuente canónica, nunca del texto del
ticket ni de metadatos que aporte n8n. El adaptador concreto se cablea cuando
el equipo propietario entregue el contrato de la Tarea 1
(docs/verification/handle-ticket/01-external-contracts.md §1).

Semántica fail-closed:

- fuente no disponible / timeout → ``ParticipantPlanUnavailable`` → 503;
- mismatch → ``authorize`` devuelve ``None`` → 403;
- validador sin configurar en un modo ACTIVO → error de arranque
  (``validate_settings``) y 503 en request (nunca autorización implícita).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ParticipantPlanError(Exception):
    """Base de errores del validador participant-plan."""


class ParticipantPlanUnavailable(ParticipantPlanError):
    """La fuente canónica no está disponible o expiró su timeout: el request
    se rechaza con 503; jamás se degrada a autorización implícita."""


class AuthorizedParticipantPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    participant_id: str
    plan_id: str
    record_keeper: Optional[str] = None


@runtime_checkable
class ParticipantPlanValidator(Protocol):
    async def authorize(
        self, *, tenant_id: str, participant_id: str, plan_id: str
    ) -> AuthorizedParticipantPlan | None:
        """Devuelve el registro autorizado o ``None`` ante mismatch.

        Lanza ``ParticipantPlanUnavailable`` si la fuente no puede responder
        (timeout/5xx); un error inesperado del adaptador se trata igual que
        indisponibilidad (503), nunca como autorización."""
        ...


def build_validator_from_settings(settings) -> ParticipantPlanValidator | None:
    """Factory del adaptador concreto según ``PARTICIPANT_PLAN_SOURCE``.

    Hoy no existe contrato externo (Tarea 1 pendiente), así que la única
    fuente válida es ninguna: devuelve ``None`` y los modos activos no pueden
    arrancar (``validate_settings``) ni autorizar (503 en request). Cuando el
    equipo owner entregue endpoint/schema/SLA, este factory construye el
    cliente real; los tests inyectan dobles vía ``app.state``.
    """
    source = (getattr(settings, "PARTICIPANT_PLAN_SOURCE", "") or "").strip()
    if not source:
        return None
    raise ValueError(
        f"PARTICIPANT_PLAN_SOURCE={source!r} no tiene adaptador implementado: "
        "el contrato canónico de la Tarea 1 sigue pendiente y no se inventa"
    )
