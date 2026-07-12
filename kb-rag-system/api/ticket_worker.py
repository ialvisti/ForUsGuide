"""
Worker durable de ticket jobs (Tasks 4 y 7 del plan de remediación).

ÚNICA implementación de ejecución (no hay divergencia inline/background):
- Claim transaccional del job (tolera delivery at-least-once de Cloud Tasks).
- Checkpoint por inquiry: cada resultado se persiste inmediatamente; un
  timeout o crash posterior no borra lo ya completado (HT-08).
- Deadlines distinguidos: INQUIRY_TIMEOUT vs TOTAL_JOB_TIMEOUT (nunca se
  etiqueta un timeout parcial como total).
- Agregación exhaustiva: cualquier degradación (scrape partial/failed/
  timeout, inquiry no procesada, fallback técnico) produce ``partial``;
  ``succeeded`` sólo si TODO terminó sin degradación (HT-07, invariante 7).
- Los resultados públicos se minimizan (sin ``used_chunks``) para caber en
  el documento durable y no exponer contenido innecesario a n8n.

El endpoint HTTP ``POST /internal/tasks/ticket-job`` es invocable sólo por
la service account de Cloud Tasks vía OIDC (producción). La cola inline de
dev llama a ``run_ticket_job`` directamente, sin HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from api.config import settings
from api.models import (
    GenerateResponseResult,
    HandleTicketRequest,
    InquiryResult,
    KnowledgeQuestionResponse,
    RouteDecision,
    SourceArticle,
    UsedChunk,
)
from data_pipeline.ticket_job_models import (
    NextAction,
    PublicErrorCode,
    TicketJobRecord,
    TicketJobState,
)
from data_pipeline.ticket_job_repository import TicketJobRepository
from data_pipeline.ticket_orchestrator import InquiryOutcome

logger = logging.getLogger(__name__)

router = APIRouter()

_TICKET_GREETING = "Could you share a bit more detail about what you'd like help with?"

# scrape_status que NO degradan el resultado (invariante 7).
_SCRAPE_OK_STATES = {None, "ok", "skipped"}


# ---------------------------------------------------------------------------
# Gating por modo (compartido con el productor)
# ---------------------------------------------------------------------------

def apply_ticket_handler_mode(route: str, mode: str) -> Tuple[str, Optional[str]]:
    """knowledge_only coerce generate_response → needs_more_info."""
    if mode == "knowledge_only" and route == "generate_response":
        return "needs_more_info", "ticket_handler_mode=knowledge_only coerced generate_response"
    return route, None


# ---------------------------------------------------------------------------
# Conversión outcome → modelos públicos (minimizados)
# ---------------------------------------------------------------------------

def _knowledge_answer_model(r: Any) -> KnowledgeQuestionResponse:
    return KnowledgeQuestionResponse(
        answer=r.answer,
        key_points=r.key_points,
        source_articles=[SourceArticle(**sa) for sa in r.source_articles],
        used_chunks=[UsedChunk(**uc) for uc in r.used_chunks],
        confidence_note=r.confidence_note,
        metadata=r.metadata,
    )


def _generate_result_model(r: Any) -> GenerateResponseResult:
    return GenerateResponseResult(
        decision=r.decision,
        confidence=r.confidence,
        response=r.response,
        source_articles=[SourceArticle(**sa) for sa in r.source_articles],
        used_chunks=[UsedChunk(**uc) for uc in r.used_chunks],
        coverage_gaps=r.coverage_gaps,
        metadata=r.metadata,
    )


def outcome_to_inquiry_result(o: InquiryOutcome) -> InquiryResult:
    return InquiryResult(
        inquiry=o.inquiry,
        topic=o.topic,
        record_keeper=o.record_keeper,
        plan_type=o.plan_type,
        route=RouteDecision(o.route),
        scrape_status=o.scrape_status,
        knowledge_answer=_knowledge_answer_model(o.knowledge_result)
        if o.knowledge_result is not None else None,
        generate_response=_generate_result_model(o.generate_result)
        if o.generate_result is not None else None,
        needs_more_info_message=o.needs_more_info_message,
        diagnostics=o.diagnostics,
    )


def nmi_outcome(ext: Any, message: str,
                diagnostics: Optional[Dict[str, Any]] = None) -> InquiryOutcome:
    return InquiryOutcome(
        inquiry=ext.inquiry,
        topic=ext.topic,
        route="needs_more_info",
        record_keeper=ext.record_keeper,
        plan_type=ext.plan_type,
        needs_more_info_message=message,
        diagnostics=diagnostics or {},
    )


def minimize_inquiry_result(result: InquiryResult) -> Dict[str, Any]:
    """Los used_chunks completos pueden exceder el límite del documento
    durable y no son necesarios para n8n: se eliminan del registro público."""
    data = result.model_dump(mode="json")
    for key in ("knowledge_answer", "generate_response"):
        block = data.get(key)
        if block and block.get("used_chunks"):
            block["used_chunks"] = []
    return data


# ---------------------------------------------------------------------------
# Degradación y agregación exhaustiva (Task 7)
# ---------------------------------------------------------------------------

def outcome_is_degraded(o: InquiryOutcome) -> Tuple[bool, Optional[str]]:
    """(degradado?, código público). Cualquier señal técnica cuenta."""
    if o.scrape_status not in _SCRAPE_OK_STATES:
        code = (PublicErrorCode.FORUSBOTS_TIMEOUT
                if o.scrape_status == "timeout"
                else PublicErrorCode.PLAN_SCRAPE_FAILED)
        return True, code.value
    diag = o.diagnostics or {}
    if diag.get("gr_body_build_failed"):
        return True, PublicErrorCode.LLM_FAILURE.value
    if diag.get("error"):
        return True, PublicErrorCode.INTERNAL_ERROR.value
    return False, None


def aggregate_states(entries: List[Dict[str, Any]],
                     unprocessed: int) -> Tuple[TicketJobState, NextAction]:
    """Agregación exhaustiva sobre per_inquiry_status (invariantes 7/8/11)."""
    statuses = [e.get("execution_status") for e in entries]
    if not statuses:
        return TicketJobState.FAILED, NextAction.USE_LEGACY_OR_HUMAN
    all_ok = all(s == "succeeded" for s in statuses)
    none_ok = all(s in ("failed", "timeout", "unprocessed") for s in statuses)
    degraded = any(e.get("degraded") for e in entries)
    if all_ok and not degraded and unprocessed == 0:
        return TicketJobState.SUCCEEDED, NextAction.SEND_PARTICIPANT_REPLY
    if none_ok:
        if all(s == "timeout" for s in statuses):
            return TicketJobState.TIMEOUT, NextAction.USE_LEGACY_OR_HUMAN
        return TicketJobState.FAILED, NextAction.USE_LEGACY_OR_HUMAN
    return TicketJobState.PARTIAL, NextAction.USE_LEGACY_OR_HUMAN


def _collect_forusbots_ids(outcomes: List[InquiryOutcome]) -> List[str]:
    """Todos los job IDs de ForusBots (participant Y plan) para trazabilidad
    completa (HT-25)."""
    ids: List[str] = []
    for o in outcomes:
        diag = o.diagnostics or {}
        for key in ("forusbots_participant_job_id", "forusbots_plan_job_id",
                    "forusbots_job_id"):
            value = diag.get(key)
            if value and value not in ids:
                ids.append(value)
    return ids


# ---------------------------------------------------------------------------
# Ejecución durable (única implementación)
# ---------------------------------------------------------------------------

async def run_ticket_job(app: Any, job_id: str,
                         *, worker_id: Optional[str] = None) -> Optional[TicketJobRecord]:
    """Ejecuta un job aceptado. Idempotente frente a re-entregas (claim).

    Devuelve el record final, o None si el claim no procede (duplicado)."""
    repo: TicketJobRepository = app.state.ticket_repo
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"

    record = await repo.get(job_id)
    if record is None:
        logger.warning("ticket job %s no existe (¿expirado?)", job_id)
        return None
    if not await repo.claim(job_id, worker_id=worker_id):
        logger.info("ticket job %s ya reclamado/terminal — delivery duplicado", job_id)
        return None

    try:
        return await _execute(app, repo, job_id, worker_id)
    except asyncio.CancelledError:
        # Deadline del task / shutdown: dejar el job retryable, nunca en
        # running eterno. Cloud Tasks reintentará; attempt/lease acotan.
        try:
            await repo.update(
                job_id,
                state=TicketJobState.QUEUED,
                public_error_code=PublicErrorCode.WORKER_CANCELLED.value,
                retryable=True,
                claimed_by=None,
                claimed_at=None,
            )
        except Exception:
            logger.exception("no se pudo re-encolar el job %s tras cancelación", job_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("ticket job %s falló", job_id)
        try:
            await repo.update(
                job_id,
                state=TicketJobState.FAILED,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=PublicErrorCode.INTERNAL_ERROR.value,
                retryable=False,
                current_step="failed",
            )
        except Exception:
            logger.exception("no se pudo marcar failed el job %s", job_id)
        return await repo.get(job_id)


async def _execute(app: Any, repo: TicketJobRepository, job_id: str,
                   worker_id: str) -> TicketJobRecord:
    record = await repo.get(job_id)
    assert record is not None
    req = HandleTicketRequest.model_validate(record.request_payload)
    mode = record.mode or settings.TICKET_HANDLER_MODE
    orchestrator = app.state.ticket_orchestrator_factory()
    started = time.monotonic()
    deadline = started + settings.TICKET_TOTAL_BUDGET_S

    # -- extracción -------------------------------------------------------
    await repo.update(job_id, current_step="extracting")
    extracted = await orchestrator.extract_inquiries(req)

    if not extracted:
        entry = _entry_from_outcome(
            0,
            nmi_outcome(
                type("E", (), {"inquiry": (req.ticket.email_body or req.ticket.email_subject or "(empty)")[:1000],
                               "topic": "general", "record_keeper": req.record_keeper,
                               "plan_type": "401(k)"})(),
                _TICKET_GREETING,
                {"reason": "no_actionable_inquiry"},
            ),
        )
        await repo.record_inquiry_result(job_id, 0, entry)
        return await repo.update(
            job_id,
            state=TicketJobState.SUCCEEDED,
            next_action=NextAction.SEND_PARTICIPANT_REPLY,
            total_inquiries=0,
            current_step="done",
            public_result={"route_taken": "needs_more_info",
                           "metadata": {"ticket_handler_mode": mode,
                                        "reason": "no_actionable_inquiry"}},
        )

    total = len(extracted)
    capped = extracted[: 1 + settings.TICKET_MAX_RELATED]
    unprocessed = total - len(capped)
    await repo.update(job_id, total_inquiries=total,
                      unprocessed_inquiries=unprocessed,
                      current_step="classifying")

    classifications = [await orchestrator.classify(e.inquiry) for e in capped]

    # -- shadow REAL y muestreado (HT-11/Task 10): clasifica siempre; cuando
    # el job cae en la muestra, ejecuta el pipeline completo SIN exponer su
    # respuesta (sólo un resumen sanitizado para el differential harness).
    if mode == "shadow":
        sampled = _shadow_sampled(record.job_id)
        shadow_summary: List[Dict[str, Any]] = []
        for i, (ext, cls) in enumerate(zip(capped, classifications)):
            if sampled:
                try:
                    real = await asyncio.wait_for(
                        orchestrator.handle_inquiry(
                            ext, req, total_inquiries=total, classification=cls
                        ),
                        timeout=settings.TICKET_INQUIRY_BUDGET_S,
                    )
                    shadow_summary.append({
                        "index": i,
                        "route": real.route,
                        "scrape_status": real.scrape_status,
                        "decision": getattr(real.generate_result, "decision", None),
                        "confidence": getattr(real.generate_result, "confidence", None),
                    })
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("shadow pipeline failed (job %s, inquiry %d)",
                                     job_id, i)
                    shadow_summary.append({"index": i,
                                           "route": getattr(cls, "route", None),
                                           "error": "shadow_pipeline_failed"})
            outcome = nmi_outcome(
                ext, getattr(cls, "user_message", None) or _TICKET_GREETING,
                {"classifier": {"route": getattr(cls, "route", None),
                                "confidence": getattr(cls, "confidence", None)},
                 "shadow": True},
            )
            entry = _entry_from_outcome(i, outcome)
            entry["participant_reply_safe"] = False   # shadow NUNCA publica
            await repo.record_inquiry_result(job_id, i, entry)
        return await repo.update(
            job_id,
            state=TicketJobState.SUCCEEDED,
            next_action=NextAction.USE_LEGACY,
            current_step="done",
            public_result={
                "route_taken": "needs_more_info",
                "metadata": {"ticket_handler_mode": "shadow", "fallback": True,
                             "shadow_routes": [getattr(c, "route", None)
                                               for c in classifications],
                             "shadow_sampled": sampled,
                             # sanitizado: rutas/estados/decisiones, sin texto
                             "shadow_summary": shadow_summary},
            },
        )

    gated = [
        apply_ticket_handler_mode(getattr(c, "route", "needs_more_info"), mode)
        for c in classifications
    ]

    # -- ejecución por inquiry con checkpoints ------------------------------
    await repo.update(job_id, current_step="processing")
    outcomes: List[InquiryOutcome] = []
    for i, (ext, cls, (_route, override_reason)) in enumerate(
        zip(capped, classifications, gated)
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Presupuesto TOTAL agotado: lo que falta queda explícitamente
            # sin procesar; lo ya persistido sobrevive (invariante 8).
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "unprocessed",
                "participant_reply_safe": False,
                "degraded": True,
                "error": {"code": PublicErrorCode.TOTAL_JOB_TIMEOUT.value,
                          "retryable": True},
            })
            continue

        try:
            if override_reason is not None:
                # Coerción de rollout (knowledge_only): NO es un outcome de
                # negocio — el NMI resultante es un artefacto de gating y no
                # debe publicarse; el ticket va a legacy (Task 10/HT-11).
                message = getattr(cls, "user_message", None) or _TICKET_GREETING
                outcome = nmi_outcome(ext, message, {
                    "classifier": {"route": getattr(cls, "route", None),
                                   "confidence": getattr(cls, "confidence", None)},
                    "ticket_handler_override": override_reason,
                })
                entry = _entry_from_outcome(i, outcome)
                entry["participant_reply_safe"] = False
                entry["coerced_by_mode"] = True
                outcomes.append(outcome)
                await repo.record_inquiry_result(job_id, i, entry)
                continue
            outcome = await asyncio.wait_for(
                orchestrator.handle_inquiry(
                    ext, req, total_inquiries=total, classification=cls
                ),
                timeout=min(settings.TICKET_INQUIRY_BUDGET_S, remaining),
            )
            outcomes.append(outcome)
            await repo.record_inquiry_result(job_id, i, _entry_from_outcome(i, outcome))
        except asyncio.TimeoutError:
            timed_out_total = (deadline - time.monotonic()) <= 0
            code = (PublicErrorCode.TOTAL_JOB_TIMEOUT if timed_out_total
                    else PublicErrorCode.INQUIRY_TIMEOUT)
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "timeout",
                "participant_reply_safe": False,
                "degraded": True,
                "error": {"code": code.value, "retryable": True},
            })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("inquiry %d del job %s falló", i, job_id)
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "failed",
                "participant_reply_safe": False,
                "degraded": True,
                "error": {"code": PublicErrorCode.INTERNAL_ERROR.value,
                          "retryable": False},
            })

    # -- agregación + cierre ------------------------------------------------
    current = await repo.get(job_id)
    entries = current.per_inquiry_status if current else []
    state, next_action = aggregate_states(entries, unprocessed)
    # Un ticket con alguna inquiry coercida por el modo se resuelve por
    # legacy: el gating no es un resultado de negocio publicable.
    if (state == TicketJobState.SUCCEEDED
            and any(e.get("coerced_by_mode") for e in entries)):
        next_action = NextAction.USE_LEGACY
    error_code = None
    if state != TicketJobState.SUCCEEDED:
        codes = [e.get("error", {}).get("code") for e in entries
                 if e.get("error")]
        if unprocessed > 0:
            codes.append(PublicErrorCode.UNPROCESSED_INQUIRIES.value)
        error_code = codes[0] if codes else None

    final = await repo.update(
        job_id,
        state=state,
        next_action=next_action,
        current_step="done",
        forusbots_job_ids=_collect_forusbots_ids(outcomes),
        public_error_code=error_code,
        retryable=any(e.get("error", {}).get("retryable") for e in entries) or None,
        public_result={
            "route_taken": (entries[0].get("result") or {}).get("route")
            if entries else None,
            "metadata": {"ticket_handler_mode": mode},
        },
    )
    await _log_execution_safe(app, record, final)
    return final


def _shadow_sampled(job_id: str) -> bool:
    """Muestreo determinístico por job (reproducible, sin RNG): controla el
    costo del shadow real vía TICKET_SHADOW_SAMPLE_RATE (0.0 = sólo
    clasificación, 1.0 = pipeline completo en todos los jobs shadow)."""
    rate = max(0.0, min(1.0, settings.TICKET_SHADOW_SAMPLE_RATE))
    if rate <= 0.0:
        return False
    bucket = int(job_id[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def _entry_from_outcome(index: int, outcome: InquiryOutcome) -> Dict[str, Any]:
    degraded, code = outcome_is_degraded(outcome)
    result = outcome_to_inquiry_result(outcome)
    entry: Dict[str, Any] = {
        "route": outcome.route,
        "execution_status": "succeeded",
        "participant_reply_safe": not degraded,
        "degraded": degraded,
        "scrape_status": outcome.scrape_status,
        "result": minimize_inquiry_result(result),
    }
    if degraded and code:
        entry["error"] = {"code": code, "retryable": code in (
            PublicErrorCode.FORUSBOTS_TIMEOUT.value,
            PublicErrorCode.PINECONE_TRANSIENT_FAILURE.value,
        )}
    return entry


async def _log_execution_safe(app: Any, record: TicketJobRecord,
                              final: TicketJobRecord) -> None:
    exec_logger = getattr(app.state, "execution_logger", None)
    if not exec_logger:
        return
    try:
        route_summary = [
            {"route": e.get("route"), "execution_status": e.get("execution_status"),
             "scrape_status": e.get("scrape_status")}
            for e in final.per_inquiry_status
        ]
        await exec_logger.log_ticket_execution(
            request_id=final.trace_id or "unknown",
            ticket_job_id=final.job_id,
            mode=final.mode,
            route_summary=route_summary,
            total_inquiries=final.total_inquiries or 0,
            forusbots_job_ids=final.forusbots_job_ids,
            duration_ms=(final.elapsed_s or 0) * 1000,
            error=final.public_error_code,
            # nunca la key raw: sólo el hash (Task 11 redacción)
            idempotency_key=final.idempotency_key_hash,
        )
    except Exception:
        logger.exception("ticket execution logging failed")


# ---------------------------------------------------------------------------
# Endpoint interno para Cloud Tasks (OIDC)
# ---------------------------------------------------------------------------

class _TaskBody(BaseModel):
    job_id: str


async def verify_task_oidc(request: Request) -> None:
    """Sólo la service account de Cloud Tasks puede invocar el worker."""
    if not settings.TICKET_WORKER_REQUIRE_OIDC:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    try:
        from google.auth.transport import requests as garequests
        from google.oauth2 import id_token as gid

        claims = id_token_claims = gid.verify_oauth2_token(
            token, garequests.Request(), audience=settings.TICKET_WORKER_URL or None
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="invalid task token") from exc
    expected_sa = settings.TICKET_WORKER_SERVICE_ACCOUNT
    if expected_sa and claims.get("email") != expected_sa:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="unexpected caller identity")


@router.post("/internal/tasks/ticket-job", include_in_schema=False)
async def ticket_job_task(body: _TaskBody, request: Request) -> Dict[str, Any]:
    """Handler del task de Cloud Tasks. La request queda abierta durante toda
    la ejecución (Cloud Run mantiene CPU asignada). Respuestas:

    - 200: job ejecutado o delivery duplicado (no reintentar)
    - 404: job desconocido (no reintentar)
    """
    await verify_task_oidc(request)
    retry_count = request.headers.get("X-CloudTasks-TaskRetryCount", "0")
    task_name = request.headers.get("X-CloudTasks-TaskName", "")
    worker_id = f"{task_name or 'task'}#{retry_count}"

    repo: TicketJobRepository = request.app.state.ticket_repo
    if await repo.get(body.job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="unknown ticket job")

    final = await run_ticket_job(request.app, body.job_id, worker_id=worker_id)
    if final is not None:
        return {"job_id": body.job_id, "state": final.state.value}

    # Claim rechazado. Terminal → delivery duplicado benigno (200, no retry).
    # No-terminal → otro attempt tiene el lease: pedir retry DESPUÉS del
    # lease para que un attempt crasheado no deje el job en running eterno.
    current = await repo.get(body.job_id)
    if current is not None and current.state not in (
        TicketJobState.SUCCEEDED, TicketJobState.PARTIAL, TicketJobState.FAILED,
        TicketJobState.TIMEOUT, TicketJobState.CANCELLED,
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "JOB_CLAIMED_ELSEWHERE", "retryable": True},
            headers={"Retry-After": "60"},
        )
    return {"job_id": body.job_id, "state": "duplicate_delivery"}
