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

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from api import metrics as ticket_metrics
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
    TERMINAL_STATES,
    NextAction,
    PublicErrorCode,
    TicketJobRecord,
    TicketJobState,
)
from data_pipeline.ticket_job_repository import (
    StaleEnqueueGeneration,
    StaleLeaseEpoch,
    TicketJobRepository,
)
from data_pipeline.ticket_orchestrator import (
    ExtractionInvalidOutput,
    ExtractionUnavailable,
    InquiryOutcome,
)
from data_pipeline.staging_fault_injection import (
    FaultInjectionRejected,
    InjectedFault,
    maybe_raise,
    validate_fault_plan,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_TICKET_GREETING = "Could you share a bit more detail about what you'd like help with?"

# scrape_status que NO degradan el resultado (invariante 7).
_SCRAPE_OK_STATES = {None, "ok", "skipped"}


class _ForusBotsSubmitIntentAlreadyExists(RuntimeError):
    """Un attempt anterior pudo enviar el POST; nunca se reenvía a ciegas."""


def _install_forusbots_intent_guard(
    orchestrator: Any,
    repo: TicketJobRepository,
    *,
    job_id: str,
    inquiry_index: int,
    worker_id: str,
    lease_epoch: int,
    route: str,
) -> None:
    """Liga el hook del orquestador al CAS durable del job actual."""
    setter = getattr(orchestrator, "set_forusbots_intent_guard", None)
    if setter is None:
        # Los dobles de tests de rutas sin ForusBots no implementan el hook.
        # La factoría productiva siempre construye TicketOrchestrator.
        return

    async def _guard() -> None:
        reserved = await repo.reserve_forusbots_submit_intent(
            job_id,
            inquiry_index,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            route=route,
        )
        if not reserved:
            raise _ForusBotsSubmitIntentAlreadyExists(
                "intent ForusBots ya persistido; requiere reconciliación"
            )

    setter(_guard)


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
    if diag.get("kq_synthesis_failed"):
        # fallo técnico de síntesis KQ (Tarea 6 Paso 2): nunca publicable
        return True, PublicErrorCode.LLM_FAILURE.value
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
    all_reply_safe = all(e.get("participant_reply_safe") is True for e in entries)
    if all_ok and not degraded and unprocessed == 0:
        if all_reply_safe:
            return TicketJobState.SUCCEEDED, NextAction.SEND_PARTICIPANT_REPLY
        # ``knowledge_only`` puede terminar técnicamente bien después de
        # clasificar una ruta GR, pero su NMI es un artefacto de rollout y no
        # una respuesta publicable. Es un éxito procesado que vuelve a legacy,
        # no un ``partial`` técnico. Cualquier otro succeeded+unsafe continúa
        # fail-closed más abajo.
        if (any(e.get("coerced_by_mode") is True for e in entries)
                and all(e.get("participant_reply_safe") is True
                        or e.get("coerced_by_mode") is True for e in entries)):
            return TicketJobState.SUCCEEDED, NextAction.USE_LEGACY
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


def _entry_forusbots_ids(entry: Dict[str, Any]) -> List[str]:
    """IDs de ForusBots de UN checkpoint, mirando tanto el bloque
    ``forusbots_job_ids`` explícito (checkpoints degradados: timeout/failed/
    unprocessed que YA hicieron scrape) como el ``result.diagnostics`` de los
    outcomes exitosos (P1 review: un scrape real cuya inquiry luego degrada no
    puede perder su job_id — la reconciliación lo necesita)."""
    ids: List[str] = list(entry.get("forusbots_job_ids") or [])
    diag = ((entry.get("result") or {}).get("diagnostics")) or {}
    for key in ("forusbots_participant_job_id", "forusbots_plan_job_id",
                "forusbots_job_id"):
        value = diag.get(key)
        if value:
            ids.append(value)
    return ids


def _collect_forusbots_ids_from_entries(entries: List[Dict[str, Any]]) -> List[str]:
    """IDs de ForusBots desde TODOS los checkpoints persistidos (Tarea 6
    Paso 4): la agregación final no puede descartar los efectos de attempts
    anteriores ni de inquiries degradadas — perderlos rompe la trazabilidad y
    la reconciliación (P1 review)."""
    ids: List[str] = []
    for entry in entries:
        for value in _entry_forusbots_ids(entry):
            if value not in ids:
                ids.append(value)
    return ids


def _emit_manual_reconciliation_metric(
    record: TicketJobRecord, entries: List[Dict[str, Any]]
) -> None:
    """Emite una señal agregada y sanitizada para reconciliación manual.

    Los checkpoints contienen contexto operativo que no debe convertirse en
    labels ni texto de Cloud Monitoring. Sólo se publica el total, el hash
    irreversible del job y el trace_id ya admitido por el contrato de
    observabilidad.
    """
    required = sum(
        entry.get("manual_reconciliation_required") is True
        for entry in entries
    )
    if required == 0:
        return

    import hashlib as _hashlib

    job_hash = _hashlib.sha256(record.job_id.encode()).hexdigest()[:16]
    try:
        ticket_metrics.emit(
            "ticket_manual_reconciliation_required",
            required,
            job_hash=job_hash,
            trace_id=record.trace_id,
            code="manual_reconciliation",
        )
    except Exception:  # noqa: BLE001 - observabilidad nunca rompe el job
        logger.exception("manual reconciliation metric emission failed")


def _emit_terminal_metric(record: TicketJobRecord) -> None:
    """Registra exactamente una terminalización reclamada por este worker.

    Se invoca después de la escritura durable, desde ``run_ticket_job``, para
    cubrir tanto los retornos tempranos de ``_execute`` como su camino normal.
    Re-entregas que no obtienen claim no pasan por aquí y no duplican el
    denominador.
    """
    if record.state not in TERMINAL_STATES:
        return

    import hashlib as _hashlib

    job_hash = _hashlib.sha256(record.job_id.encode()).hexdigest()[:16]
    try:
        ticket_metrics.increment("ticket_jobs_terminal", state=record.state.value)
        ticket_metrics.emit(
            "ticket_job_terminal",
            1,
            job_hash=job_hash,
            trace_id=record.trace_id,
            state=record.state.value,
            code=record.public_error_code or "none",
        )
    except Exception:  # noqa: BLE001 - métricas jamás cambian el outcome
        logger.exception("terminal metric emission failed")


# ---------------------------------------------------------------------------
# Ejecución durable (única implementación)
# ---------------------------------------------------------------------------

async def run_ticket_job(app: Any, job_id: str,
                         *, worker_id: Optional[str] = None,
                         expected_generation: Optional[int] = None,
                         ) -> Optional[TicketJobRecord]:
    """Ejecuta un job aceptado. Idempotente frente a re-entregas (claim) y
    fenced por lease_epoch (Tarea 6 Paso 4a): un intento viejo que despierta
    después de perder su lease no puede enviar, guardar ni publicar.

    Devuelve el record final, o None si el claim no procede (duplicado) o si
    este intento quedó fenced."""
    repo: TicketJobRepository = app.state.ticket_repo
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"

    record = await repo.get(job_id)
    if record is None:
        logger.warning("ticket job %s no existe (¿expirado?)", job_id)
        return None
    lease_epoch = await repo.claim(
        job_id,
        worker_id=worker_id,
        lease_s=settings.TICKET_WORKER_LEASE_S,
        expected_generation=expected_generation,
    )
    if lease_epoch is None:
        logger.info("ticket job %s ya reclamado/terminal — delivery duplicado", job_id)
        return None

    heartbeat = asyncio.create_task(
        _heartbeat_loop(repo, job_id, worker_id, lease_epoch))
    try:
        final = await _execute(app, repo, job_id, worker_id, lease_epoch)
        _emit_terminal_metric(final)
        return final
    except StaleLeaseEpoch:
        # Fenced: otro worker/reconciliador posee ya el job. Este intento no
        # escribe NADA más; el dueño actual completa o re-encola.
        logger.info("ticket job %s: intento con epoch %d fenced", job_id,
                    lease_epoch)
        return None
    except InjectedFault:
        # A post-checkpoint crash must surface as a failed Cloud Tasks
        # delivery.  The durable checkpoint remains intact and the next
        # attempt skips that completed inquiry, making the fault one-shot.
        raise
    except asyncio.CancelledError:
        # Deadline del task / shutdown: dejar el job retryable, nunca en
        # running eterno. La escritura es condicional al epoch: si otro
        # worker ya tomó el job, no se pisa su estado.
        try:
            await repo.update(
                job_id,
                state=TicketJobState.QUEUED,
                expected_lease_epoch=lease_epoch,
                public_error_code=PublicErrorCode.WORKER_CANCELLED.value,
                retryable=True,
                claimed_by=None,
                claimed_at=None,
                lease_owner=None,
                lease_expires_at=None,
            )
        except StaleLeaseEpoch:
            pass
        except Exception:
            logger.exception("no se pudo re-encolar el job %s tras cancelación", job_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("ticket job %s falló", job_id)
        try:
            await repo.update(
                job_id,
                state=TicketJobState.FAILED,
                expected_lease_epoch=lease_epoch,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=PublicErrorCode.INTERNAL_ERROR.value,
                retryable=False,
                current_step="failed",
            )
        except StaleLeaseEpoch:
            return None
        except Exception:
            logger.exception("no se pudo marcar failed el job %s", job_id)
        final = await repo.get(job_id)
        if final is not None:
            _emit_terminal_metric(final)
        return final
    finally:
        heartbeat.cancel()


async def _heartbeat_loop(repo: TicketJobRepository, job_id: str,
                          worker_id: str, lease_epoch: int) -> None:
    """Renueva el lease cada TICKET_WORKER_HEARTBEAT_S mientras el intento
    ejecuta. Si la renovación falla (fenced), no interrumpe: la próxima
    escritura condicional del intento lanzará StaleLeaseEpoch."""
    try:
        while True:
            await asyncio.sleep(settings.TICKET_WORKER_HEARTBEAT_S)
            renewed = await repo.renew_lease(
                job_id, worker_id=worker_id, lease_epoch=lease_epoch,
                lease_s=settings.TICKET_WORKER_LEASE_S)
            if not renewed:
                logger.warning("ticket job %s: heartbeat perdió el lease "
                               "(epoch %d)", job_id, lease_epoch)
                return
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("heartbeat del job %s falló", job_id)


async def _ensure_lease(repo: TicketJobRepository, job_id: str,
                        worker_id: str, lease_epoch: int) -> None:
    """Verificación previa a cada efecto externo: owner+epoch vigentes."""
    await repo.assert_active_lease(
        job_id,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
    )


class _InjectedDependencyDown(RuntimeError):
    """Staging-only stand-in for a downstream availability failure."""


def _inject_staging_fault(
    plan: Optional[Dict[str, Any]], *, point: str, inquiry_index: int,
    record: TicketJobRecord,
) -> None:
    """Invoke the signed fault contract and map it to the real failure type."""
    if plan is None:
        return
    # A lease-loss fault models one fencing event.  Without this attempt gate
    # every recovered delivery would lose the lease forever.
    if point == "lease_lost" and record.attempt > 1:
        return
    try:
        maybe_raise(
            plan,
            point=point,
            inquiry_index=inquiry_index,
            app_env=settings.APP_ENV,
            principal_id=record.principal_id,
            secret=settings.TICKET_FAULT_SIGNING_SECRET,
        )
    except InjectedFault as exc:
        if exc.point == "timeout_reset":
            raise asyncio.TimeoutError("injected timeout/reset") from exc
        if exc.point == "dependency_down":
            raise _InjectedDependencyDown("injected dependency outage") from exc
        if exc.point == "lease_lost":
            raise StaleLeaseEpoch("injected lease loss") from exc
        raise


class _PlanClassification:
    """Clasificación rehidratada desde el execution_plan persistido."""

    def __init__(self, route: Optional[str] = None,
                 confidence: Optional[float] = None,
                 reasoning: Optional[str] = None,
                 user_message: Optional[str] = None):
        self.route = route
        self.confidence = confidence
        self.reasoning = reasoning
        self.user_message = user_message


def _build_execution_plan(capped: List[Any], classifications: List[Any],
                          gated: List[Tuple[str, Optional[str]]],
                          total: int, unprocessed: int) -> Dict[str, Any]:
    return {
        "version": 1,
        "total_inquiries": total,
        "unprocessed_inquiries": unprocessed,
        "inquiries": [
            {"inquiry": e.inquiry, "record_keeper": e.record_keeper,
             "plan_type": e.plan_type, "topic": e.topic,
             "related_inquiries": e.related_inquiries}
            for e in capped
        ],
        "classifications": [
            {"route": getattr(c, "route", None),
             "confidence": getattr(c, "confidence", None),
             "reasoning": getattr(c, "reasoning", None),
             "user_message": getattr(c, "user_message", None)}
            for c in classifications
        ],
        "gating": [
            {"route": route, "override_reason": reason}
            for route, reason in gated
        ],
    }


def _rehydrate_plan(plan: Dict[str, Any]):
    from data_pipeline.ticket_orchestrator import ExtractedInquiry
    capped = [ExtractedInquiry(**item) for item in plan["inquiries"]]
    classifications = [_PlanClassification(**c) for c in plan["classifications"]]
    gated = [(g.get("route"), g.get("override_reason"))
             for g in plan["gating"]]
    return capped, classifications, gated


async def _execute(app: Any, repo: TicketJobRepository, job_id: str,
                   worker_id: str, lease_epoch: int) -> TicketJobRecord:
    record = await repo.get(job_id)
    assert record is not None
    fault_plan = None
    if record.fault_plan is not None:
        # Validate before constructing the orchestrator or beginning any
        # external effect. A persisted plan is rejected outside staging even
        # if Firestore was tampered with after producer validation.
        fault_plan = validate_fault_plan(
            record.fault_plan,
            app_env=settings.APP_ENV,
            principal_id=record.principal_id,
            secret=settings.TICKET_FAULT_SIGNING_SECRET,
        )
    req = HandleTicketRequest.model_validate(record.request_payload)
    mode = record.mode or settings.TICKET_HANDLER_MODE
    orchestrator = app.state.ticket_orchestrator_factory()
    started = time.monotonic()

    # Presupuesto del intento: min(TICKET_ATTEMPT_BUDGET_S, lo que reste del
    # deadline ABSOLUTO del job). Un intento no inicia un efecto que no cabe.
    budget = settings.TICKET_ATTEMPT_BUDGET_S
    if record.job_deadline_at is not None:
        from data_pipeline.ticket_job_models import utcnow as _utcnow
        remaining_abs = (record.job_deadline_at - _utcnow()).total_seconds()
        if remaining_abs <= 0:
            return await repo.update(
                job_id,
                state=TicketJobState.TIMEOUT,
                expected_lease_epoch=lease_epoch,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=PublicErrorCode.TOTAL_JOB_TIMEOUT.value,
                retryable=False,
                current_step="done",
            )
        budget = min(budget, remaining_abs)
    deadline = started + budget

    # -- plan de ejecución: persistir UNA vez, reutilizar en retry ---------
    if record.execution_plan:
        capped, classifications, gated = _rehydrate_plan(record.execution_plan)
        total = record.execution_plan["total_inquiries"]
        unprocessed = record.execution_plan["unprocessed_inquiries"]
    else:
        await repo.update(job_id, current_step="extracting",
                          expected_lease_epoch=lease_epoch)
        try:
            extracted = await orchestrator.extract_inquiries(req)
        except ExtractionUnavailable:
            # Fallo TÉCNICO del proveedor: jamás un saludo publicable
            # (bloqueo 4). n8n resuelve por legacy/humano.
            return await repo.update(
                job_id,
                state=TicketJobState.FAILED,
                expected_lease_epoch=lease_epoch,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=PublicErrorCode.LLM_FAILURE.value,
                retryable=True,
                current_step="failed",
                public_result={"metadata": {"ticket_handler_mode": mode,
                                            "reason": "extract_llm_failure"}},
            )
        except ExtractionInvalidOutput:
            return await repo.update(
                job_id,
                state=TicketJobState.FAILED,
                expected_lease_epoch=lease_epoch,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                public_error_code=PublicErrorCode.LLM_FAILURE.value,
                retryable=False,
                current_step="failed",
                public_result={"metadata": {"ticket_handler_mode": mode,
                                            "reason": "extract_invalid_output"}},
            )

        if not extracted:
            # Extracción VÁLIDA y vacía: elegible para legacy/humano; nunca
            # se sintetiza un saludo publicable (Tarea 6 Paso 1).
            return await repo.update(
                job_id,
                state=TicketJobState.SUCCEEDED,
                expected_lease_epoch=lease_epoch,
                next_action=NextAction.USE_LEGACY_OR_HUMAN,
                total_inquiries=0,
                current_step="done",
                public_result={"route_taken": "needs_more_info",
                               "metadata": {"ticket_handler_mode": mode,
                                            "reason": "no_actionable_inquiry"}},
            )

        total = len(extracted)
        capped = extracted[: 1 + settings.TICKET_MAX_RELATED]
        unprocessed = total - len(capped)
        classifications = [await orchestrator.classify(e.inquiry) for e in capped]
        gated = [
            apply_ticket_handler_mode(getattr(c, "route", "needs_more_info"), mode)
            for c in classifications
        ]
        await repo.update(
            job_id,
            expected_lease_epoch=lease_epoch,
            execution_plan=_build_execution_plan(
                capped, classifications, gated, total, unprocessed),
            total_inquiries=total,
            unprocessed_inquiries=unprocessed,
            current_step="processing",
        )

    # checkpoints terminales de attempts anteriores: NO se repiten efectos.
    # 'unprocessed' NO es terminal (presupuesto agotado, retryable=True): un
    # retry con presupuesto fresco DEBE reprocesarla, no saltarla para
    # siempre (P2 review).
    current = await repo.get(job_id)
    done_indexes = {
        e.get("index") for e in (current.per_inquiry_status if current else [])
        if e.get("execution_status") not in
        (None, "pending", "running", "unprocessed")
    }

    # -- shadow REAL y muestreado (HT-11/Task 10) ---------------------------
    if mode == "shadow":
        sampled = _shadow_sampled(record.job_id)
        shadow_summary: List[Dict[str, Any]] = []
        for i, (ext, cls) in enumerate(zip(capped, classifications)):
            if i in done_indexes:
                continue
            shadow_remaining = deadline - time.monotonic()
            intent_blocked = False
            if sampled and shadow_remaining > 0:
                try:
                    await _ensure_lease(repo, job_id, worker_id, lease_epoch)
                    _install_forusbots_intent_guard(
                        orchestrator,
                        repo,
                        job_id=job_id,
                        inquiry_index=i,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        route=getattr(cls, "route", "needs_more_info"),
                    )
                    for fault_point in (
                        "lease_lost", "timeout_reset", "dependency_down",
                    ):
                        _inject_staging_fault(
                            fault_plan,
                            point=fault_point,
                            inquiry_index=i,
                            record=record,
                        )
                    real = await asyncio.wait_for(
                        orchestrator.handle_inquiry(
                            ext, req, total_inquiries=total, classification=cls
                        ),
                        timeout=min(settings.TICKET_INQUIRY_BUDGET_S,
                                    shadow_remaining),
                    )
                    # el shadow muestreado hace scrapes REALES de ForusBots:
                    # sus job_ids deben trazarse aunque shadow no publique
                    # (P2 review; reconciliación).
                    shadow_ids = _collect_forusbots_ids([real])
                    shadow_summary.append({
                        "index": i,
                        "route": real.route,
                        "scrape_status": real.scrape_status,
                        "decision": getattr(real.generate_result, "decision", None),
                        "confidence": getattr(real.generate_result, "confidence", None),
                        "forusbots_job_ids": shadow_ids,
                    })
                except asyncio.CancelledError:
                    raise
                except StaleLeaseEpoch:
                    raise
                except _ForusBotsSubmitIntentAlreadyExists:
                    intent_blocked = True
                    shadow_summary.append({
                        "index": i,
                        "route": getattr(cls, "route", None),
                        "error": "forusbots_needs_reconciliation",
                    })
                except (FaultInjectionRejected, InjectedFault):
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
            if intent_blocked:
                entry.update({
                    "degraded": True,
                    "forusbots_submit_intent": True,
                    "manual_reconciliation_required": True,
                    "error": {
                        "code": PublicErrorCode.FORUSBOTS_NEEDS_RECONCILIATION.value,
                        "retryable": False,
                    },
                })
            # trazar los job_ids del scrape real del shadow (P2 review)
            shadow_entry_ids = next(
                (s.get("forusbots_job_ids") for s in shadow_summary
                 if s.get("index") == i and s.get("forusbots_job_ids")), None)
            if shadow_entry_ids:
                entry["forusbots_job_ids"] = shadow_entry_ids
            await repo.record_inquiry_result(job_id, i, entry,
                                             lease_epoch=lease_epoch)
            _inject_staging_fault(
                fault_plan,
                point="post_checkpoint",
                inquiry_index=i,
                record=record,
            )
        shadow_current = await repo.get(job_id)
        shadow_entries = shadow_current.per_inquiry_status if shadow_current else []
        _emit_manual_reconciliation_metric(record, shadow_entries)
        return await repo.update(
            job_id,
            state=TicketJobState.SUCCEEDED,
            expected_lease_epoch=lease_epoch,
            next_action=NextAction.USE_LEGACY,
            current_step="done",
            # los scrapes reales del shadow muestreado se trazan para
            # reconciliación aunque shadow no publique (P2 review)
            forusbots_job_ids=_collect_forusbots_ids_from_entries(shadow_entries),
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

    # -- ejecución por inquiry con checkpoints (reanuda: omite terminales) --
    outcomes: List[InquiryOutcome] = []
    for i, (ext, cls, (_route, override_reason)) in enumerate(
        zip(capped, classifications, gated)
    ):
        if i in done_indexes:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Presupuesto agotado: lo que falta queda explícitamente
            # sin procesar; lo ya persistido sobrevive (invariante 8).
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "unprocessed",
                "participant_reply_safe": False,
                "degraded": True,
                "error": {"code": PublicErrorCode.TOTAL_JOB_TIMEOUT.value,
                          "retryable": True},
            }, lease_epoch=lease_epoch)
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
                await repo.record_inquiry_result(job_id, i, entry,
                                                 lease_epoch=lease_epoch)
                _inject_staging_fault(
                    fault_plan,
                    point="post_checkpoint",
                    inquiry_index=i,
                    record=record,
                )
                continue
            # verificación de lease ANTES del efecto externo (Paso 4a)
            await _ensure_lease(repo, job_id, worker_id, lease_epoch)
            _install_forusbots_intent_guard(
                orchestrator,
                repo,
                job_id=job_id,
                inquiry_index=i,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                route=getattr(cls, "route", "needs_more_info"),
            )
            for fault_point in (
                "lease_lost", "timeout_reset", "dependency_down",
            ):
                _inject_staging_fault(
                    fault_plan,
                    point=fault_point,
                    inquiry_index=i,
                    record=record,
                )
            outcome = await asyncio.wait_for(
                orchestrator.handle_inquiry(
                    ext, req, total_inquiries=total, classification=cls
                ),
                timeout=min(settings.TICKET_INQUIRY_BUDGET_S, remaining),
            )
            outcomes.append(outcome)
            # el checkpoint es la verificación DESPUÉS del efecto: escritura
            # condicional al epoch (un intento fenced no puede guardar)
            await repo.record_inquiry_result(
                job_id, i, _entry_from_outcome(i, outcome),
                lease_epoch=lease_epoch)
            _inject_staging_fault(
                fault_plan,
                point="post_checkpoint",
                inquiry_index=i,
                record=record,
            )
        except _ForusBotsSubmitIntentAlreadyExists:
            # No sabemos si el attempt anterior alcanzó ForusBots y perdió
            # la respuesta. Sin idempotencia/reconcile upstream, la única
            # opción segura es no reenviar y pedir reconciliación manual.
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "failed",
                "participant_reply_safe": False,
                "degraded": True,
                "forusbots_submit_intent": True,
                "manual_reconciliation_required": True,
                "error": {
                    "code": PublicErrorCode.FORUSBOTS_NEEDS_RECONCILIATION.value,
                    "retryable": False,
                },
            }, lease_epoch=lease_epoch)
        except asyncio.TimeoutError:
            timed_out_total = (deadline - time.monotonic()) <= 0
            code = (PublicErrorCode.TOTAL_JOB_TIMEOUT if timed_out_total
                    else PublicErrorCode.INQUIRY_TIMEOUT)
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "timeout",
                "participant_reply_safe": False,
                "degraded": True,
                # una ruta GR pudo lanzar un scrape ANTES del timeout: el
                # job_id se perdió con la corrutina cancelada, así que NO se
                # afirma "sin efectos" — queda para reconciliación manual
                # (P1 review; plan Tarea 6 Paso 5).
                "manual_reconciliation_required":
                    getattr(cls, "route", None) == "generate_response",
                "error": {"code": code.value, "retryable": True},
            }, lease_epoch=lease_epoch)
        except asyncio.CancelledError:
            raise
        except StaleLeaseEpoch:
            raise
        except (FaultInjectionRejected, InjectedFault):
            raise
        except Exception:  # noqa: BLE001
            logger.exception("inquiry %d del job %s falló", i, job_id)
            await repo.record_inquiry_result(job_id, i, {
                "route": getattr(cls, "route", None),
                "execution_status": "failed",
                "participant_reply_safe": False,
                "degraded": True,
                "manual_reconciliation_required":
                    getattr(cls, "route", None) == "generate_response",
                "error": {"code": PublicErrorCode.INTERNAL_ERROR.value,
                          "retryable": False},
            }, lease_epoch=lease_epoch)

    # -- agregación + cierre: SIEMPRE desde los checkpoints persistidos ----
    current = await repo.get(job_id)
    entries = current.per_inquiry_status if current else []
    _emit_manual_reconciliation_metric(record, entries)
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
        expected_lease_epoch=lease_epoch,
        next_action=next_action,
        current_step="done",
        # trazabilidad COMPLETA: IDs de ForusBots de TODOS los attempts
        # persistidos, no sólo de los outcomes de este intento (Paso 4)
        forusbots_job_ids=_collect_forusbots_ids_from_entries(entries),
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
    if (outcome.diagnostics or {}).get("manual_reconciliation_required"):
        entry["manual_reconciliation_required"] = True
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
    # generación del outbox (Tarea 7 Paso 3); tasks viejas (pre-generación)
    # llegan sin él → generación 0 por compat.
    enqueue_generation: int = 0


async def verify_task_oidc(request: Request) -> None:
    """Sólo la service account de Cloud Tasks puede invocar el worker."""
    if not settings.TICKET_WORKER_REQUIRE_OIDC:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    audience = settings.TICKET_WORKER_URL
    expected_sa = settings.TICKET_WORKER_SERVICE_ACCOUNT
    if not audience or not expected_sa:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="task identity verifier is not configured",
        )
    try:
        from google.auth.transport import requests as garequests
        from google.oauth2 import id_token as gid

        claims = await asyncio.to_thread(
            gid.verify_oauth2_token,
            token,
            garequests.Request(),
            audience,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="invalid task token") from exc
    except Exception as exc:  # noqa: BLE001 - cert transport is retryable
        logger.exception("task identity verifier unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="task identity verifier unavailable",
        ) from exc
    if claims.get("email_verified") is not True \
            or claims.get("email") != expected_sa:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="unexpected caller identity")


@router.post("/internal/tasks/ticket-job", include_in_schema=False)
async def ticket_job_task(body: _TaskBody, request: Request):
    """Handler del task de Cloud Tasks. La request queda abierta durante toda
    la ejecución (Cloud Run mantiene CPU asignada). Respuestas:

    - 200: job ejecutado o delivery duplicado (no reintentar)
    - 204: generación stale, job desconocido/terminal o schema permanente —
      sin efecto (Cloud Tasks NO reintenta un 2xx; un 4xx SÍ se reintentaría)
    - 503: otro attempt tiene el lease (retry tras el lease)

    Sólo la generación ACTUAL pasa a running; sólo fallos realmente
    transitorios devuelven non-2xx (Tarea 7 Paso 3)."""
    await verify_task_oidc(request)
    retry_count = request.headers.get("X-CloudTasks-TaskRetryCount", "0")
    task_name = request.headers.get("X-CloudTasks-TaskName", "")
    worker_id = f"{task_name or 'task'}#{retry_count}"

    repo: TicketJobRepository = request.app.state.ticket_repo
    current = await repo.get(body.job_id)

    # Generación stale / job desconocido o terminal → 204 sin efecto. Cloud
    # Tasks reintenta CUALQUIER non-2xx (incluso 4xx), así que un job que no
    # debe ejecutarse debe devolver 2xx, no 404/409.
    if current is None:
        logger.info("ticket job %s desconocido: 204 sin efecto", body.job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if current.state in TERMINAL_STATES:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if body.enqueue_generation != current.enqueue_generation:
        logger.info(
            "ticket job %s: generación stale g%d != g%d — 204 sin efecto",
            body.job_id, body.enqueue_generation, current.enqueue_generation)
        ticket_metrics.increment("ticket_stale_generation")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        final = await run_ticket_job(
            request.app,
            body.job_id,
            worker_id=worker_id,
            expected_generation=body.enqueue_generation,
        )
    except StaleEnqueueGeneration:
        # La generación pudo cambiar después del pre-check y antes del
        # claim. El CAS del repositorio es la autoridad y no adquirió lease.
        ticket_metrics.increment("ticket_stale_generation")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if final is not None:
        return {"job_id": body.job_id, "state": final.state.value}

    # Claim rechazado. Terminal → delivery duplicado benigno (200, no retry).
    # No-terminal → otro attempt tiene el lease: pedir retry DESPUÉS del
    # lease para que un attempt crasheado no deje el job en running eterno.
    current = await repo.get(body.job_id)
    if current is not None and current.state not in TERMINAL_STATES:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "JOB_CLAIMED_ELSEWHERE", "retryable": True},
            headers={"Retry-After": "60"},
        )
    return {"job_id": body.job_id, "state": "duplicate_delivery"}
