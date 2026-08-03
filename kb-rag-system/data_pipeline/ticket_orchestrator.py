"""
Ticket orchestrator — runs the end-to-end ticket flow in-process.

Replaces the n8n graph: extract inquiries → classify → branch into
knowledge_question (fast) / generate_response (slow: required-data → ForusBots
scrape → body build → generate-response) / needs_more_info.

LLM-first: the four n8n agents are internal LLM calls via ``LLMRouter`` using the
prompts ported in Stage 3. The deterministic glue lives here.

Layering: this module depends only on ``data_pipeline`` (engines, client, prompts)
and returns plain dataclasses holding the RAW engine results. The API layer
(main.py) converts those to Pydantic response models at the boundary, exactly as
the existing per-endpoint handlers do.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from data_pipeline import forusbots_catalog, gr_payload_builder, prompts
from data_pipeline.retrieval_privacy import (
    UnsafeRetrievalQuery,
    redact_retrieval_context,
    sanitize_retrieval_query,
)
from data_pipeline.forusbots_client import (
    ForusBotsCircuitOpen,
    ForusBotsError,
    ForusBotsJobFailed,
    ForusBotsPollFailed,
    ForusBotsTimeout,
)
from data_pipeline.json_parsing import parse_json_array, parse_json_object
from data_pipeline.durable_document import validate_durable_document
from data_pipeline.llm_output_models import (
    ExtractedInquiryOut,
    FieldMapOut,
    GRBodyDraftOut,
    KBQuestionSynthesisOut,
    TicketFieldExtractOut,
)

logger = logging.getLogger(__name__)

_SAFE_FORUSBOTS_ERROR_CODES = frozenset({
    "FORUSBOTS_ERROR",
    "FORUSBOTS_TIMEOUT",
    "FORUSBOTS_JOB_FAILED",
    "FORUSBOTS_AMBIGUOUS_SUBMIT",
    "FORUSBOTS_POLL_FAILED",
    "FORUSBOTS_CIRCUIT_OPEN",
    "FORUSBOTS_CHECKPOINT_FAILED",
    "FORUSBOTS_NEEDS_RECONCILIATION",
})


class _ForusBotsOperationNeedsReconciliation(ForusBotsError):
    code = "FORUSBOTS_NEEDS_RECONCILIATION"
    needs_reconciliation = True


def _safe_nonnegative_number(value: Any) -> Optional[float]:
    """Accept numeric upstream metadata without reflecting arbitrary values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if numeric >= 0 and math.isfinite(numeric) else None


# ---------------------------------------------------------------------------
# Errores tipificados de la extracción (plan Tarea 6 Paso 1): un fallo del
# proveedor o un output fuera de contrato NUNCA se degrada a "[]" — eso
# convertía un incidente técnico en un saludo publicable (bloqueo 4).
# ---------------------------------------------------------------------------

class ExtractionError(Exception):
    """Base de fallos técnicos de extract_inquiries."""


class ExtractionUnavailable(ExtractionError):
    """El proveedor LLM falló o expiró: retryable, jamás publicable."""


class ExtractionInvalidOutput(ExtractionError):
    """El proveedor respondió algo que no es el JSON del contrato."""


class KQSynthesisError(Exception):
    """Fallo técnico (proveedor/parseo/schema) en la síntesis KQ: el
    resultado debe degradar a legacy/humano, nunca a un NMI publicable."""


# Per-agent completion budgets (the router scales these for GPT-5 reasoning).
_EXTRACT_MAX_TOKENS = 1500
_KB_SYNTH_MAX_TOKENS = 800
_FIELD_MAP_MAX_TOKENS = 2000
_GR_BODY_MAX_TOKENS = 6000
_TICKET_EXTRACT_MAX_TOKENS = 1500

_DEFAULT_PLAN_TYPE = "401(k)"
_DEFAULT_GREETING = "Could you share a bit more detail about what you'd like help with?"

# ---------------------------------------------------------------------------
# Validación semántica de evidencia (Task 5 Step 4)
# ---------------------------------------------------------------------------

_NUM_TOKEN_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _numbers_in(text: str) -> set[float]:
    values: set[float] = set()
    for token in _NUM_TOKEN_RE.findall(text):
        try:
            values.add(float(token.replace("$", "").replace(",", "")))
        except ValueError:
            continue
    return values


def _evidence_supports_value(value: Any, evidence: str,
                             data_type: Optional[str]) -> bool:
    """La presencia literal de una cita no prueba que el VALOR corresponda.
    Para tipos numéricos/fecha el valor debe derivarse de la evidencia; lo no
    verificable se trata como no extraído (fail-closed)."""
    dt = str(data_type or "").lower()
    if dt in ("currency", "number") or dt.startswith("list[currency]") \
            or dt.startswith("list[number]"):
        try:
            numeric = float(str(value).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            return False
        candidates = _numbers_in(evidence)
        return any(abs(numeric - c) < 0.005 for c in candidates)
    if dt == "date":
        value_nums = {int(t) for t in re.findall(r"\d+", str(value))}
        if not value_nums:
            return False
        ev_lower = evidence.lower()
        ev_nums = {int(t) for t in re.findall(r"\d+", evidence)}
        ev_nums.update(num for name, num in _MONTHS.items() if name in ev_lower)
        ev_nums.update(num for name, num in _MONTHS.items()
                       if name[:3] in re.findall(r"[a-z]{3,}", ev_lower))
        return value_nums <= ev_nums
    # boolean/text: la capa literal (cita presente en el ticket) es el gate.
    return True


# ============================================================================
# Data carriers (plain dataclasses; API layer converts to Pydantic)
# ============================================================================

@dataclass
class ExtractedInquiry:
    inquiry: str
    record_keeper: Optional[str]
    plan_type: str
    topic: str
    related_inquiries: Optional[List[str]] = None


@dataclass
class InquiryOutcome:
    """Result of handling one inquiry. Exactly one of the result fields is set."""

    inquiry: str
    topic: str
    route: str                              # knowledge_question|generate_response|needs_more_info
    record_keeper: Optional[str] = None
    plan_type: str = _DEFAULT_PLAN_TYPE
    scrape_status: Optional[str] = None     # ok|partial|failed|timeout|skipped (GR only)
    knowledge_result: Any = None            # rag_engine.ask_knowledge_question dataclass
    generate_result: Any = None             # rag_engine.generate_response dataclass
    needs_more_info_message: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestratorDeps:
    rag_engine: Any
    inquiry_router: Any
    llm_router: Any
    forusbots: Any
    execution_logger: Any = None


# ============================================================================
# F3 — deterministic account_access split guard
# ============================================================================
# A security/account-access blocker (cannot log in, unsolicited password reset,
# stale email on file, MFA problem) mixed with a financial request must become
# its own ``account_access`` inquiry. The extraction LLM does not do this
# reliably (eval 2026-06-22, cases C4/C5: the security half is dropped or folded
# into the financial response), so we detect the signal deterministically and
# inject the inquiry when the model omitted it. Matching is conservative —
# compound phrases only — so a bare "email" or "verification code" never fires
# on its own (prefer a missed split to a spurious one).


def _contains_phrase(text: str, phrase: str) -> bool:
    """Word-bounded substring match (mirrors rag_engine._contains_bounded_phrase)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _detect_account_access_signal(text: str) -> Optional[str]:
    """Return a third-person description of the security/access blocker present in
    ``text`` (already lowercased), or None. Each clause requires a compound phrase
    so single tokens cannot trigger a false split."""
    reasons: List[str] = []

    # Unsolicited password reset.
    if _contains_phrase(text, "password reset") and any(
        _contains_phrase(text, p)
        for p in (
            "did not request", "didn't request", "did not ask", "didn't ask",
            "not request", "never requested", "unsolicited", "did not initiate",
            "didn't initiate",
        )
    ):
        reasons.append("received a password-reset email they did not request")

    # Cannot log in / access the account. "log into"/"log in to"/"sign in"/"log
    # on" are listed explicitly because the word-bounded match does NOT find
    # "can't log in" inside "can't log into" (the trailing "to" breaks the
    # boundary). Only NEGATED verbs are listed so a benign "log into my account
    # to update X" never fires; invalid-credential phrases are unambiguous
    # access blockers and stand on their own.
    if any(
        _contains_phrase(text, p)
        for p in (
            "can't log in", "cannot log in", "can't login", "cannot login",
            "can't log into", "cannot log into", "can't log in to", "cannot log in to",
            "unable to log in", "unable to login", "unable to log into", "unable to log in to",
            "can't log on", "cannot log on",
            "can't sign in", "cannot sign in", "can't sign into", "cannot sign into",
            "unable to sign in",
            "can't access", "cannot access", "can't get into", "cannot get into",
            "locked out", "account locked", "login inaccessible", "lost access",
            "invalid credentials", "credentials are invalid", "credentials are not valid",
            "invalid login credentials",
        )
    ):
        reasons.append("cannot log in to or access their account")

    # Email on file is no longer valid / usable. The bare token "no longer works"
    # was removed: it matched the EMPLOYMENT phrase "no longer works there" and,
    # paired with any "email" token, produced a spurious split on plain financial
    # tickets. A genuine "my email no longer works" still fires via the
    # email-adjacent phrases below.
    email_invalid = (
        _contains_phrase(text, "email") and any(
            _contains_phrase(text, p)
            for p in (
                "no longer valid", "no longer have access", "old email",
                "former work email", "former email", "previous email",
                "invalid email",
            )
        )
    ) or any(
        _contains_phrase(text, p)
        for p in (
            "email no longer works", "email is no longer working",
            "email no longer working",
        )
    )
    if email_invalid:
        reasons.append("the email on file is no longer valid or accessible")

    # MFA / two-factor problem — requires a problem context, not a bare mention.
    if any(
        _contains_phrase(text, p)
        for p in ("mfa", "two-factor", "two factor", "authenticator")
    ) and any(
        _contains_phrase(text, p)
        for p in (
            "can't", "cannot", "not working", "not receiving", "didn't receive",
            "did not receive", "issue", "problem", "trouble", "reset", "locked",
        )
    ):
        reasons.append("is having a multi-factor authentication (MFA) problem")

    if not reasons:
        return None
    return (
        "Participant reports a security/account-access issue: "
        f"{'; '.join(reasons)}. They are concerned about account access and "
        "possible unauthorized activity and need help regaining secure access."
    )


def _inject_account_access_guard(
    extracted: List[ExtractedInquiry], req: Any
) -> List[ExtractedInquiry]:
    """If the ticket shows a security/account-access blocker but the extractor did
    not emit an ``account_access`` inquiry, inject one (cross-linked with the
    financial inquiries). Idempotent: never injects when an ``account_access``
    inquiry is already present."""
    if any((e.topic or "").strip().lower() == "account_access" for e in extracted):
        return extracted

    ticket = getattr(req, "ticket", None)
    subject = getattr(ticket, "email_subject", "") or ""
    body = getattr(ticket, "email_body", "") or ""
    signal = _detect_account_access_signal(f"{subject} {body}".lower())
    if not signal:
        return extracted

    access = ExtractedInquiry(
        inquiry=signal,
        record_keeper=getattr(req, "record_keeper", None),
        plan_type=_DEFAULT_PLAN_TYPE,
        topic="account_access",
        related_inquiries=[e.inquiry for e in extracted] or None,
    )
    for e in extracted:
        rel = list(e.related_inquiries or [])
        if access.inquiry not in rel:
            rel.append(access.inquiry)
        e.related_inquiries = rel
    # Insert right after the primary inquiry so it survives the dispatch cap
    # (extracted[: 1 + _max_related]); appending could fall outside the slice.
    extracted.insert(1, access)
    logger.info(
        "account_access guard injected a synthetic security inquiry "
        "(extractor omitted the split; total now %d)",
        len(extracted),
    )
    return extracted


# ============================================================================
# Orchestrator
# ============================================================================

class TicketOrchestrator:
    def __init__(self, deps: OrchestratorDeps, settings: Any):
        self.deps = deps
        self._max_related = getattr(settings, "TICKET_MAX_RELATED", 3)
        self._inquiry_budget_s = getattr(settings, "TICKET_INQUIRY_BUDGET_S", 300.0)
        self._forusbots_intent_guard: Optional[
            Callable[[], Awaitable[None]]
        ] = None
        self._forusbots_prepare_operation: Optional[
            Callable[[str], Awaitable[Any]]
        ] = None
        self._forusbots_submitted_observer: Optional[
            Callable[[str, str], Awaitable[None]]
        ] = None
        # ForusBotsClient es singleton de proceso, pero cada orchestrator
        # pertenece a un ticket job. El scope aleatorio impide que caché o
        # in-flight dedupe crucen tickets/tenants con IDs coincidentes.
        self._forusbots_dedupe_scope = uuid.uuid4().hex

    def set_forusbots_intent_guard(
        self, guard: Callable[[], Awaitable[None]]
    ) -> None:
        """Instala el fence durable que el worker liga a job/inquiry/lease.

        El orquestador lo invoca sólo cuando el mapping produjo módulos que
        realmente pueden disparar ForusBots, inmediatamente antes del submit.
        """
        self._forusbots_intent_guard = guard

    def set_forusbots_operation_hooks(
        self,
        prepare: Callable[[str], Awaitable[Any]],
        submitted: Callable[[str, str], Awaitable[None]],
        *,
        dedupe_scope: str,
    ) -> None:
        """Install durable per-operation submit/resume hooks for one inquiry."""
        self._forusbots_prepare_operation = prepare
        self._forusbots_submitted_observer = submitted
        self._forusbots_dedupe_scope = dedupe_scope

    # ------------------------------------------------------------------
    # Step 1 — extraction
    # ------------------------------------------------------------------

    async def extract_inquiries(self, req: Any) -> List[ExtractedInquiry]:
        """Extracción con fallos TIPIFICADOS (Tarea 6 Paso 1): un fallo del
        proveedor lanza ``ExtractionUnavailable``; un output no-JSON lanza
        ``ExtractionInvalidOutput``. Sólo un array JSON válido y vacío
        devuelve ``[]`` (extracción legítimamente vacía)."""
        agent_input = self._build_case_data(req)
        system, user = prompts.build_extract_inquiries_prompt(agent_input)
        try:
            resp = await self.deps.llm_router.call(
                "extract_inquiries", system, user, max_tokens=_EXTRACT_MAX_TOKENS
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("extract_inquiries LLM call failed")
            raise ExtractionUnavailable(str(type(exc).__name__)) from exc

        items = parse_json_array(resp.content)
        if items is None:
            # None = no parseable (contrato roto); [] = array válido vacío
            raise ExtractionInvalidOutput("extract_inquiries devolvió no-JSON")
        if not items:
            return []

        extracted: List[ExtractedInquiry] = []
        dropped = 0
        for it in items:
            if not isinstance(it, dict):
                dropped += 1
                continue
            # normalización tolerante ANTES del schema estricto: defaults que
            # el contrato permite omitir
            normalized = {
                "inquiry": (it.get("inquiry") or "").strip(),
                "topic": (str(it.get("topic") or "general").strip() or "general"),
                "record_keeper": it.get("record_keeper"),
                "plan_type": it.get("plan_type") or _DEFAULT_PLAN_TYPE,
                "related_inquiries": it.get("related_inquiries") or [],
            }
            try:
                out = ExtractedInquiryOut.model_validate(normalized)
            except ValidationError:
                dropped += 1
                continue
            # Identity-adjacent fields are server-owned. The extractor may
            # paraphrase the inquiry/topic, but cannot replace the canonical
            # record keeper or invent a plan type from untrusted ticket text.
            rk = req.record_keeper
            extracted.append(ExtractedInquiry(
                inquiry=out.inquiry,
                record_keeper=rk,
                plan_type=_DEFAULT_PLAN_TYPE,
                topic=out.topic,
                related_inquiries=out.related_inquiries or None,
            ))
        if dropped:
            logger.warning("extract_inquiries: %d items inválidos descartados", dropped)
        return _inject_account_access_guard(extracted, req)

    # ------------------------------------------------------------------
    # Step 2 — classify + branch
    # ------------------------------------------------------------------

    async def classify(self, inquiry: str) -> Any:
        return await self.deps.inquiry_router.classify(inquiry)

    async def handle_inquiry(
        self,
        ext: ExtractedInquiry,
        req: Any,
        *,
        total_inquiries: int,
        classification: Any = None,
    ) -> InquiryOutcome:
        if classification is None:
            classification = await self.classify(ext.inquiry)
        route = getattr(classification, "route", "needs_more_info")

        if route == "knowledge_question":
            return await self._handle_kq(ext, req, classification)
        if route == "generate_response":
            return await self._handle_gr(ext, req, classification, total_inquiries)
        return self._needs_more_info(ext, classification)

    async def run_ticket(self, req: Any) -> List[InquiryOutcome]:
        """Conveniencia para tests/harness: extract → handle each (capped).

        NO es un motor de ejecución de producción: la ÚNICA implementación
        con budgets, checkpoints y agregación es ``api.ticket_worker`` (Task
        7). Por eso aquí no hay timeouts: un timeout técnico jamás debe
        disfrazarse de needs_more_info (invariante 6)."""
        extracted = await self.extract_inquiries(req)
        if not extracted:
            return []
        total = len(extracted)
        outcomes: List[InquiryOutcome] = []
        for ext in extracted[: 1 + self._max_related]:
            outcomes.append(
                await self.handle_inquiry(ext, req, total_inquiries=total)
            )
        return outcomes

    # ------------------------------------------------------------------
    # Branch handlers
    # ------------------------------------------------------------------

    async def _handle_kq(self, ext: ExtractedInquiry, req: Any, classification: Any) -> InquiryOutcome:
        # Synthesize the KB question from THIS inquiry, not the whole ticket.
        # ``_build_ticket_data`` returns the full email body + thread, so when a
        # ticket yields two knowledge_question inquiries (e.g. a financial request
        # + an account_access blocker) both would synthesize the same question and
        # the dominant topic would hijack both answers. Feeding the extractor's
        # per-inquiry text (and clearing the subject/thread) scopes the synthesis
        # to this inquiry while leaving the parity-locked prompt untouched.
        focused = {
            **self._build_ticket_data(req),
            "emailSubject": "",
            "emailBody": ext.inquiry,
            "ticket_messages": {},
        }
        agent_input = {"ticketData": focused}
        system, user = prompts.build_kb_question_synthesis_prompt(agent_input)
        diag = self._classifier_diag(classification)
        synthesis_failed = False
        parsed = None
        try:
            resp = await self.deps.llm_router.call(
                "kb_question_synthesis", system, user, max_tokens=_KB_SYNTH_MAX_TOKENS
            )
            parsed = parse_json_object(resp.content)
            if parsed is None:
                synthesis_failed = True
                diag["kb_synthesis_invalid_output"] = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("kb_question_synthesis failed")
            synthesis_failed = True
            diag["kb_synthesis_provider_failed"] = True

        synthesis: Optional[KBQuestionSynthesisOut] = None
        if isinstance(parsed, dict):
            try:
                synthesis = KBQuestionSynthesisOut.model_validate(parsed)
            except ValidationError:
                synthesis_failed = True
                diag["kb_synthesis_invalid_output"] = True

        if synthesis_failed:
            # Fallo TÉCNICO (proveedor/parseo/schema): degradado a legacy/
            # humano, jamás un needs_more_info publicable (Tarea 6 Paso 2).
            return InquiryOutcome(
                inquiry=ext.inquiry, topic=ext.topic, route="needs_more_info",
                record_keeper=ext.record_keeper, plan_type=ext.plan_type,
                needs_more_info_message=None,
                diagnostics={**diag, "kq_synthesis_failed": True,
                             "error": "KQ_SYNTHESIS_FAILED"},
            )

        question = synthesis.question if synthesis else None
        if synthesis is None or synthesis.insufficient_inquiry or not question:
            # Resultado de NEGOCIO válido: la síntesis funcionó y declaró la
            # inquiry insuficiente — un NMI legítimo sí es publicable.
            return InquiryOutcome(
                inquiry=ext.inquiry, topic=ext.topic, route="needs_more_info",
                record_keeper=ext.record_keeper, plan_type=ext.plan_type,
                needs_more_info_message=getattr(classification, "user_message", None) or _DEFAULT_GREETING,
                diagnostics={**diag, "kb_insufficient": True},
            )

        question = redact_retrieval_context(
            question,
            sensitive_literals=self._participant_sensitive_literals(req),
        )
        # The rich redacted question is useful to the answer LLM.  Prove that
        # the final Pinecone boundary can derive a reviewed concept from it;
        # only when synthesis produced no domain signal do we append a neutral,
        # server-owned fallback instead of forwarding arbitrary text alone.
        try:
            sanitize_retrieval_query(question)
        except UnsafeRetrievalQuery:
            fallback = sanitize_retrieval_query(
                f"{ext.topic} {ext.plan_type} retirement plan guidance"
            )
            question = f"{question} ({fallback})"

        kq = await self.deps.rag_engine.ask_knowledge_question(question=question)
        return InquiryOutcome(
            inquiry=ext.inquiry, topic=ext.topic, route="knowledge_question",
            record_keeper=ext.record_keeper, plan_type=ext.plan_type,
            knowledge_result=kq,
            diagnostics={**diag, "synthesized_question": question},
        )

    async def _handle_gr(
        self, ext: ExtractedInquiry, req: Any, classification: Any, total_inquiries: int
    ) -> InquiryOutcome:
        diag: Dict[str, Any] = self._classifier_diag(classification)

        # 1) required data → flatten → field map → modules
        safe_inquiry = redact_retrieval_context(
            ext.inquiry,
            sensitive_literals=self._participant_sensitive_literals(req),
        )
        rd = await self.deps.rag_engine.get_required_data(
            inquiry=safe_inquiry, record_keeper=ext.record_keeper,
            plan_type=ext.plan_type, topic=ext.topic,
            related_inquiries=ext.related_inquiries,
        )
        rd_metadata = getattr(rd, "metadata", None) or {}
        rd_error = (
            rd_metadata.get("error")
            if isinstance(rd_metadata, dict)
            else None
        )
        if (
            rd_error is not None
        ):
            transient_kinds = {
                "timeout", "transport", "rate_limit", "server_error",
                "circuit_open",
            }
            failure_kind = rd_metadata.get("retrieval_failure_kind")
            if failure_kind not in transient_kinds | {
                "client_error", "unknown",
            }:
                failure_kind = "unknown"
            diag["required_data_failure"] = {
                "failure_kind": failure_kind,
                "retryable": (
                    rd_metadata.get("retrieval_retryable") is True
                    and failure_kind in transient_kinds
                ),
            }
            # Fail before mapping, ForUsBots, ticket extraction, body building
            # or response generation.  An explicit required-data error is not
            # evidence that no fields are required, regardless of the exact
            # provider/local sentinel used to describe it.
            return InquiryOutcome(
                inquiry=ext.inquiry,
                topic=ext.topic,
                route="generate_response",
                record_keeper=ext.record_keeper,
                plan_type=ext.plan_type,
                scrape_status="skipped",
                diagnostics=diag,
            )
        flat_fields = _flatten_required_fields(getattr(rd, "required_fields", {}))
        modules: List[Dict[str, Any]] = []
        extraction_candidates: List[Dict[str, Any]] = []
        if flat_fields:
            modules, extraction_candidates = await self._map_fields(flat_fields, diag)
        diag["mapped_modules"] = modules

        # This diagnostic block becomes part of the durable per-inquiry
        # checkpoint.  Validate it before the first irreversible ForUsBots
        # effect so a Firestore-incompatible shape can never create an
        # upstream job that the worker is then unable to checkpoint.
        validate_durable_document({"diagnostics": diag})

        # El dedupe in-memory del cliente no cruza instancias ni sobrevive a
        # un lease perdido. El worker persiste este intent después de saber
        # que habrá scrape y justo antes del primer POST. Sin contrato
        # upstream idempotente, un intent previo bloquea cualquier resubmit.
        if modules and self._forusbots_prepare_operation is None \
                and self._forusbots_intent_guard is not None:
            await self._forusbots_intent_guard()

        # 2) ForusBots scrapes AND ticket-field extraction, in PARALLEL — the
        # extraction only needs the ticket text, not the scrape result.
        async def _noop_scrape(
        ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]:
            return {}, {}, {}, "skipped"

        async def _noop_extract(
        ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
            return {}, []

        scrape_coro = (
            self._scrape_all(req.participant_id, req.plan_id, modules, diag)
            if modules else _noop_scrape()
        )
        extract_coro = (
            self._extract_ticket_fields(extraction_candidates, req, diag)
            if extraction_candidates else _noop_extract()
        )
        (ppt_modules, plan_modules, scrape_meta, scrape_status), (extracted, not_found) = \
            await asyncio.gather(scrape_coro, extract_coro)
        if not modules and flat_fields:
            diag["scrape_skip_reason"] = "no_mappable_fields"

        # Fields the ticket DID answer are no longer collection gaps.
        if extracted:
            extracted_slugs = set(extracted)
            fm = diag.get("field_mapping") or {}
            fm["unmapped"] = [
                u for u in (fm.get("unmapped") or [])
                if forusbots_catalog._normalize_slug(
                    u.get("field") if isinstance(u, dict) else u
                ) not in extracted_slugs
            ]
            if diag.get("unmapped_fields"):
                diag["unmapped_fields"] = fm["unmapped"]

        # 3) collected_data DETERMINÍSTICO (Task 5, HT-03): los hechos vienen
        # del scrape normalizado + extracción evidence-gated; el LLM no puede
        # aportar ni alterar valores.
        collected_data = gr_payload_builder.build_collected_data(
            ppt_modules, plan_modules, extracted,
            company_name=req.company_name, company_status=req.company_status,
        )

        # 4) el body-builder queda como agente de REDACCIÓN: enriquece la
        # inquiry y afina el topic. Nada más de su output se usa.
        draft = await self._build_gr_body(
            ppt_modules, plan_modules, scrape_meta, scrape_status,
            req, ext, total_inquiries, diag, ticket_extracted=extracted,
        )

        # 5) campos server-owned SIEMPRE fijados desde fuentes confiables
        # (invariante 4 / HT-22): el LLM no controla límites ni conteos.
        response_inquiry = redact_retrieval_context(
            draft.inquiry or ext.inquiry,
            sensitive_literals=self._participant_sensitive_literals(req),
        )
        gr = await self.deps.rag_engine.generate_response(
            inquiry=response_inquiry,
            record_keeper=ext.record_keeper,
            # ``draft`` es salida LLM no confiable. El plan_type canónico se
            # fijó al extraer la inquiry desde el request autorizado y no se
            # puede sobrescribir en la fase de redacción.
            plan_type=ext.plan_type,
            topic=(draft.topic or ext.topic),
            collected_data=collected_data,
            max_response_tokens=req.max_response_tokens,
            total_inquiries_in_ticket=total_inquiries,
        )
        return InquiryOutcome(
            inquiry=ext.inquiry, topic=ext.topic, route="generate_response",
            record_keeper=ext.record_keeper, plan_type=ext.plan_type,
            scrape_status=scrape_status, generate_result=gr, diagnostics=diag,
        )

    @staticmethod
    def _participant_sensitive_literals(req: Any) -> Tuple[str, ...]:
        """Known request values that must not enter semantic-search text."""
        ticket = getattr(req, "ticket", None)
        return tuple(filter(None, (
            getattr(req, "participant_id", None),
            getattr(req, "plan_id", None),
            getattr(req, "company_name", None),
            getattr(ticket, "username", None),
            getattr(ticket, "user_email", None),
        )))

    def _needs_more_info(self, ext: ExtractedInquiry, classification: Any) -> InquiryOutcome:
        return InquiryOutcome(
            inquiry=ext.inquiry, topic=ext.topic, route="needs_more_info",
            record_keeper=ext.record_keeper, plan_type=ext.plan_type,
            needs_more_info_message=getattr(classification, "user_message", None) or _DEFAULT_GREETING,
            diagnostics=self._classifier_diag(classification),
        )

    # ------------------------------------------------------------------
    # GR sub-steps
    # ------------------------------------------------------------------

    async def _map_fields(
        self, flat_fields: List[Dict[str, Any]], diag: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Hybrid mapping: deterministic slug table first, LLM only for the
        leftovers, catalog validation ALWAYS before anything reaches ForusBots.

        Returns ``(modules, extraction_candidates)`` — the candidates are the
        non-scrapeable fields handed to the ticket-extraction layer."""
        year = datetime.now(timezone.utc).year

        det_entries: List[Tuple[str, str]] = []
        det_by_slug: Dict[str, List[Dict[str, str]]] = {}
        unresolved: List[Dict[str, Any]] = []
        request_provided: List[str] = []
        for item in flat_fields:
            if forusbots_catalog.is_request_provided(item):
                # Already carried by the handle-ticket request (caseData):
                # neither scrape nor extraction nor unmapped.
                request_provided.append(str(item.get("field")))
                continue
            entries = forusbots_catalog.map_slug(item, current_year=year)
            if entries is None:
                unresolved.append(item)
            else:
                det_entries.extend(entries)
                det_by_slug[str(item.get("field"))] = [
                    {"module": module, "field": field_name}
                    for module, field_name in entries
                ]

        llm_modules: List[Dict[str, Any]] = []
        llm_unmapped: List[Any] = []
        llm_failed = False
        if unresolved:  # LLM ONLY for the hard cases
            system, user = prompts.build_forusbots_field_map_prompt(
                unresolved, current_year=year
            )
            try:
                resp = await self.deps.llm_router.call(
                    "forusbots_field_map", system, user, max_tokens=_FIELD_MAP_MAX_TOKENS
                )
                mapping = parse_json_object(resp.content) or {}
            except Exception:
                logger.error("forusbots_field_map failed")
                mapping = {}
            # Schema estricto (HT-12): output fuera de contrato = mapper failure
            field_map: Optional[FieldMapOut] = None
            if mapping:
                try:
                    field_map = FieldMapOut.model_validate(mapping)
                except ValidationError:
                    logger.warning("forusbots_field_map: output inválido descartado")
            if field_map is not None:
                llm_modules = [
                    {"key": m.key, "fields": list(m.fields)}
                    for m in field_map.modules
                ]
                llm_unmapped = list(field_map.unmapped)
            else:
                llm_failed = True
                llm_unmapped = [
                    {"field": it.get("field"), "reason": "mapper_parse_failure"}
                    for it in unresolved
                ]

        merged = forusbots_catalog.merge_module_lists(
            forusbots_catalog.build_modules(det_entries), llm_modules
        )
        validated = forusbots_catalog.validate_modules(merged)

        # Ticket-extraction candidates: the non-scrapeable fields — the ones
        # the LLM mapper put in _unmapped (or all unresolved when the mapper
        # failed). They get one more chance: the participant may have stated
        # the value in the ticket itself.
        if llm_failed:
            candidates = list(unresolved)
        else:
            unmapped_slugs = {
                forusbots_catalog._normalize_slug(
                    u.get("field") if isinstance(u, dict) else u
                )
                for u in llm_unmapped
            }
            candidates = [
                it for it in unresolved
                if forusbots_catalog._normalize_slug(it.get("field")) in unmapped_slugs
            ]

        diag["field_mapping"] = {
            "deterministic_mapped": det_by_slug,
            "llm_mapped": llm_modules,
            "llm_called": bool(unresolved),
            "llm_failed": llm_failed,
            "rejected": validated.rejected,
            "warnings": validated.warnings,
            "unmapped": llm_unmapped,
            "request_provided": request_provided,
        }
        if llm_unmapped:
            diag["unmapped_fields"] = llm_unmapped  # legacy diagnostics key
        return validated.modules, candidates

    async def _scrape_all(
        self,
        participant_id: str,
        plan_id: str,
        modules: List[Dict[str, Any]],
        diag: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]:
        """Run the participant scrape and (when plan modules were mapped) the
        plan scrape in parallel. Returns (ppt_flat, plan_flat, meta, status).

        Degraded-proceed: a failure in either scrape never raises; the combined
        status is "ok" only when everything succeeded cleanly, the participant
        scrape dominates, and a plan-side failure downgrades to "partial"."""
        p_modules, plan_modules = forusbots_catalog.split_modules_by_target(modules)

        async def _run_operation(
            operation: str, entity_id: str, operation_modules: List[Dict[str, Any]],
        ) -> Any:
            if self._forusbots_prepare_operation is not None:
                decision = await self._forusbots_prepare_operation(operation)
                action = getattr(decision, "action", None)
                external_job_id = getattr(decision, "external_job_id", None)
                if action == "reconcile":
                    raise _ForusBotsOperationNeedsReconciliation(
                        "ForUsBots operation requires manual reconciliation"
                    )
                if action == "resume":
                    if not isinstance(external_job_id, str) or not external_job_id:
                        raise _ForusBotsOperationNeedsReconciliation(
                            "ForUsBots resume receipt is incomplete"
                        )
                    try:
                        return await self.deps.forusbots.resume_job(
                            external_job_id, operation=operation,
                        )
                    except ForusBotsCircuitOpen:
                        # The effect is already confirmed by a durable receipt.
                        # A local open circuit only prevented observing it; do
                        # not degrade as though no upstream job existed.
                        raise ForusBotsPollFailed(external_job_id) from None
                if action != "submit":
                    raise _ForusBotsOperationNeedsReconciliation(
                        "ForUsBots operation decision is invalid"
                    )

            kwargs: Dict[str, Any] = {
                "dedupe_scope": self._forusbots_dedupe_scope,
            }
            if self._forusbots_submitted_observer is not None:
                kwargs["on_submitted"] = self._forusbots_submitted_observer
            if operation == "participant":
                return await self.deps.forusbots.scrape_participant(
                    entity_id, operation_modules, **kwargs,
                )
            return await self.deps.forusbots.scrape_plan(
                entity_id, operation_modules, **kwargs,
            )

        async def _one(
            coro_label: str,
            coro: Awaitable[Any],
        ) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
            try:
                scrape = await coro
                diag[f"forusbots_{coro_label}_job_id"] = scrape.job_id
                elapsed_seconds = _safe_nonnegative_number(
                    scrape.elapsed_seconds
                )
                if elapsed_seconds is not None:
                    diag[f"forusbots_{coro_label}_elapsed_s"] = elapsed_seconds
                flat, meta = forusbots_catalog.normalize_scrape_result(scrape.result)
                if meta.get("module_error_count") or meta.get("error_count"):
                    status = "partial"
                elif not flat:
                    # Job succeeded but the normalizer found no usable module data
                    # (unknown response shape or a genuinely empty scrape). Surface as
                    # degraded so an empty payload is never silently treated as "ok".
                    meta.setdefault("empty_result", True)
                    status = "partial"
                else:
                    status = "ok"
                return flat, meta, status
            except ForusBotsTimeout as e:
                logger.warning(
                    "ForusBots %s timeout code=%s", coro_label, e.code,
                )
                diag[f"forusbots_{coro_label}_job_id"] = e.job_id
                diag["manual_reconciliation_required"] = True
                return {}, {
                    "error_code": e.code,
                    "job_id": e.job_id,
                    "manual_reconciliation_required": True,
                }, "timeout"
            except (ForusBotsJobFailed, ForusBotsError) as e:
                error_code = e.code
                job_id = getattr(e, "job_id", None)
                logger.warning(
                    "ForusBots %s failed code=%s", coro_label, error_code,
                )
                if job_id:
                    diag[f"forusbots_{coro_label}_job_id"] = job_id
                meta = {"error_code": error_code}
                if getattr(e, "needs_reconciliation", False):
                    diag["manual_reconciliation_required"] = True
                    meta["manual_reconciliation_required"] = True
                if job_id:
                    meta["job_id"] = job_id
                return {}, meta, "failed"

        tasks = []
        if p_modules:
            tasks.append(_one(
                "participant",
                _run_operation("participant", participant_id, p_modules),
            ))
        if plan_modules:
            tasks.append(_one(
                "plan", _run_operation("plan", plan_id, plan_modules),
            ))
        results = await asyncio.gather(*tasks) if tasks else []

        idx = 0
        ppt_flat: Dict[str, Any] = {}
        ppt_status = "skipped"
        ppt_meta: Dict[str, Any] = {}
        if p_modules:
            ppt_flat, ppt_meta, ppt_status = results[idx]
            idx += 1
        plan_flat: Dict[str, Any] = {}
        plan_status = "skipped"
        plan_meta: Dict[str, Any] = {}
        if plan_modules:
            plan_flat, plan_meta, plan_status = results[idx]

        meta: Dict[str, Any] = {}
        if ppt_meta:
            meta["participant"] = ppt_meta
        if plan_meta:
            meta["plan"] = plan_meta
        diag["scrape_meta"] = meta

        # Combined status: participant dominates; a plan-side problem (or any
        # partial) downgrades an otherwise-ok run to "partial".
        if p_modules:
            status = ppt_status
            if status == "ok" and plan_modules and plan_status != "ok":
                status = "partial"
            elif status == "ok" and plan_status == "partial":
                status = "partial"
        elif plan_modules:
            status = plan_status
        else:
            status = "skipped"
        # legacy single-job diagnostics keys (kept for dashboards)
        if "forusbots_participant_job_id" in diag:
            diag.setdefault("forusbots_job_id", diag["forusbots_participant_job_id"])
        if "forusbots_participant_elapsed_s" in diag:
            diag.setdefault(
                "forusbots_elapsed_s",
                diag["forusbots_participant_elapsed_s"],
            )
        return ppt_flat, plan_flat, meta, status

    async def _extract_ticket_fields(
        self,
        candidates: List[Dict[str, Any]],
        req: Any,
        diag: Dict[str, Any],
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """LLM layer that extracts non-scrapeable field values the participant
        already stated in the ticket text. Returns ``(extracted, not_found)``
        where ``extracted`` is keyed by normalized slug →
        ``{"field", "value", "evidence"}``.

        Hard anti-hallucination gate (dos capas, Task 5):
        1. literal — la cita de ``evidence`` debe existir en el ticket;
        2. semántica — para tipos currency/number/date el valor debe poder
           derivarse de la evidencia (``evidence="$5"`` con ``value=999999999``
           se demote). Lo no verificable queda como not_found."""
        fields_payload = [
            {"field": it.get("field"), "description": it.get("description"),
             "why_needed": it.get("why_needed"), "required": it.get("required")}
            for it in candidates
        ]
        ticket_data = {
            "emailSubject": req.ticket.email_subject,
            "emailBody": req.ticket.email_body,
        }
        system, user = prompts.build_ticket_field_extract_prompt(fields_payload, ticket_data)
        try:
            resp = await self.deps.llm_router.call(
                "ticket_field_extract", system, user, max_tokens=_TICKET_EXTRACT_MAX_TOKENS
            )
            parsed = parse_json_object(resp.content) or {}
        except Exception:
            logger.error("ticket_field_extract failed")
            parsed = {}

        extraction: Optional[TicketFieldExtractOut] = None
        if isinstance(parsed, dict) and parsed:
            try:
                extraction = TicketFieldExtractOut.model_validate(parsed)
            except ValidationError:
                logger.warning("ticket_field_extract: output inválido descartado")

        data_types = {
            forusbots_catalog._normalize_slug(str(it.get("field"))): it.get("data_type")
            for it in candidates
        }
        # Allowlist CERRADA de slugs: el extractor sólo puede devolver los
        # campos que se le PIDIERON. Una clave inventada por el LLM (prompt
        # injection) se rechaza — si no está en candidates no tiene data_type
        # y saltaría la validación semántica, permitiendo fabricar hechos
        # (P1/P2 del review final).
        allowed_slugs = set(data_types)
        ticket_text = " ".join(
            str(t or "") for t in (req.ticket.email_subject, req.ticket.email_body)
        ).lower()
        extracted: Dict[str, Dict[str, Any]] = {}
        demoted: List[str] = []
        rejected_keys: List[str] = []
        if extraction is not None:
            for field_name, entry in extraction.extracted.items():
                slug = forusbots_catalog._normalize_slug(field_name)
                if slug not in allowed_slugs:
                    rejected_keys.append(field_name)
                    continue
                evidence = entry.evidence.strip()
                if not evidence or evidence.lower() not in ticket_text:
                    demoted.append(field_name)
                    continue
                if not _evidence_supports_value(entry.value, evidence,
                                                data_types.get(slug)):
                    demoted.append(field_name)
                    continue
                extracted[slug] = {
                    "field": field_name,
                    "value": entry.value,
                    "evidence": evidence,
                }
        if rejected_keys:
            logger.warning(
                "ticket_field_extract: %d non-candidate keys rejected",
                len(rejected_keys),
            )

        not_found = ([str(f) for f in extraction.not_found] if extraction else []) + demoted
        if extraction is None:
            not_found = [str(it.get("field")) for it in candidates]

        fm = diag.setdefault("field_mapping", {})
        fm["ticket_extracted"] = {v["field"]: v["value"] for v in extracted.values()}
        fm["ticket_not_found"] = not_found
        if demoted:
            fm["ticket_evidence_demoted"] = demoted
        if rejected_keys:
            fm["ticket_non_candidate_rejected"] = rejected_keys
        return extracted, not_found

    async def _build_gr_body(
        self,
        ppt_modules: Dict[str, Any],
        plan_modules: Dict[str, Any],
        scrape_meta: Dict[str, Any],
        scrape_status: str,
        req: Any,
        ext: ExtractedInquiry,
        total_inquiries: int,
        diag: Dict[str, Any],
        ticket_extracted: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> GRBodyDraftOut:
        """Agente de REDACCIÓN (Task 5): sólo se consumen ``inquiry``/``topic``/
        ``plan_type`` de su output; collected_data, límites, conteos e IDs los
        fija el código (gr_payload_builder + request). Un fallo aquí degrada la
        redacción, nunca los hechos."""
        entry: Dict[str, Any] = {
            "pptDataModules": ppt_modules,
            "caseData": self._build_case_data(req),
        }
        if plan_modules:
            entry["planDataModules"] = plan_modules
        if ticket_extracted:
            entry["ticketExtractedFields"] = {
                v["field"]: {"value": v["value"], "evidence": v["evidence"]}
                for v in ticket_extracted.values()
            }
        data_collection = _build_data_collection(
            scrape_status, scrape_meta, diag.get("field_mapping") or {}
        )
        if data_collection:
            entry["dataCollection"] = data_collection
        agent_input = [entry]
        system, user = prompts.build_gr_body_build_prompt(agent_input)
        try:
            resp = await self.deps.llm_router.call(
                "gr_body_build", system, user, max_tokens=_GR_BODY_MAX_TOKENS
            )
            body = parse_json_object(resp.content)
        except Exception:
            logger.error("gr_body_build failed")
            body = None
        if isinstance(body, dict):
            try:
                return GRBodyDraftOut.model_validate(body)
            except ValidationError:
                diag["gr_body_build_invalid_output"] = True
        diag["gr_body_build_failed"] = True
        return GRBodyDraftOut(inquiry=ext.inquiry, topic=ext.topic,
                              plan_type=ext.plan_type)

    # ------------------------------------------------------------------
    # Input shaping
    # ------------------------------------------------------------------

    def _build_ticket_data(self, req: Any) -> Dict[str, Any]:
        # Fuente de verdad única: subject + body (Task 1 del plan). El hilo
        # histórico y el tag ya no existen en runtime; los prompts reciben las
        # claves con valores neutrales porque su protocolo ya define ese caso
        # ("empty {} → use emailBody").
        t = req.ticket
        return {
            "userId": None,
            "userName": t.username,
            "userEmail": t.user_email,
            "ticketId": t.ticket_id,
            "emailSubject": t.email_subject,
            "emailBody": t.email_body,
            "tag": None,
            "firstContact": t.first_contact,
            "ticket_messages": {},
        }

    def _build_case_data(self, req: Any) -> Dict[str, Any]:
        return {
            "userData": {
                "pptId": req.participant_id,
                "planId": req.plan_id,
                "companyName": req.company_name,
                "companyStatus": req.company_status,
                "companyStatusDetail": req.company_status_detail,
            },
            "ticketData": self._build_ticket_data(req),
            "forusbots": {"recordKeeper": req.record_keeper},
        }

    @staticmethod
    def _classifier_diag(classification: Any) -> Dict[str, Any]:
        classifier: Dict[str, Any] = {
            "route": getattr(classification, "route", None),
            "confidence": getattr(classification, "confidence", None),
            "reasoning": getattr(classification, "reasoning", None),
        }
        metadata = getattr(classification, "metadata", None) or {}
        if metadata.get("classifier_parse_ok") is False:
            classifier["parse_ok"] = False
        coverage_signals = metadata.get("coverage_signals")
        if isinstance(coverage_signals, dict):
            classifier["coverage_signals"] = dict(coverage_signals)
        return {"classifier": classifier}


# ============================================================================
# Helpers
# ============================================================================

def _flatten_required_fields(required_fields: Any) -> List[Dict[str, Any]]:
    """Flatten the engine's ``required_fields`` (Dict[str, List[item]]) into the
    flat ``[{field, category, description, why_needed, data_type, required}]``
    the field-mapper expects. ``category`` (participant_data | plan_data) is
    preserved so the mapper can route participant-vs-plan-side targets.
    Tolerates dict / pydantic / dataclass items."""
    flat: List[Dict[str, Any]] = []
    if not isinstance(required_fields, dict):
        return flat
    for category, items in required_fields.items():
        for it in (items or []):
            if isinstance(it, dict):
                d = it
            elif hasattr(it, "model_dump"):
                d = it.model_dump()
            elif hasattr(it, "__dict__"):
                d = dict(vars(it))
            else:
                continue
            flat.append({
                "field": d.get("field"),
                "category": category,
                "description": d.get("description"),
                "why_needed": d.get("why_needed"),
                "data_type": d.get("data_type"),
                "required": d.get("required"),
            })
    return flat


def _build_data_collection(
    scrape_status: str,
    scrape_meta: Dict[str, Any],
    field_mapping: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Compact ``dataCollection`` block for the body-builder agent: what was
    attempted but NOT collected (and why), so the final answer can ask instead
    of assuming. Returns ``None`` on a clean run so the documented happy-path
    input contract (pptDataModules + caseData) is unchanged."""
    out: Dict[str, Any] = {}
    if scrape_status not in ("ok", "skipped"):
        out["scrapeStatus"] = scrape_status
    for side in ("participant", "plan"):
        side_meta = scrape_meta.get(side) or {}
        for src, dst in (
            ("module_error_count", "moduleErrorCount"),
            ("unknown_field_count", "unknownFieldCount"),
            ("extractor_warning_count", "extractorWarningCount"),
            ("warning_count", "warningCount"),
            ("error_count", "errorCount"),
        ):
            val = side_meta.get(src)
            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                bucket = out.setdefault(dst, {})
                bucket[side] = val
        error_code = side_meta.get("error_code")
        if error_code in _SAFE_FORUSBOTS_ERROR_CODES:
            bucket = out.setdefault("errorCodes", {})
            bucket[side] = error_code
    if field_mapping.get("unmapped"):
        out["unmappedFields"] = field_mapping["unmapped"]
    if field_mapping.get("rejected"):
        out["rejectedMappings"] = field_mapping["rejected"]
    return out or None
