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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from data_pipeline import forusbots_catalog, prompts
from data_pipeline.forusbots_client import (
    ForusBotsError,
    ForusBotsJobFailed,
    ForusBotsTimeout,
)
from data_pipeline.json_parsing import parse_json_array, parse_json_object

logger = logging.getLogger(__name__)

# Per-agent completion budgets (the router scales these for GPT-5 reasoning).
_EXTRACT_MAX_TOKENS = 1500
_KB_SYNTH_MAX_TOKENS = 800
_FIELD_MAP_MAX_TOKENS = 2000
_GR_BODY_MAX_TOKENS = 6000
_TICKET_EXTRACT_MAX_TOKENS = 1500

_DEFAULT_PLAN_TYPE = "401(k)"
_DEFAULT_GREETING = "Could you share a bit more detail about what you'd like help with?"
_FORM_SUBMISSION_SUBJECT = "Participant Advisory - Form Submission"


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

    # ------------------------------------------------------------------
    # Step 1 — extraction
    # ------------------------------------------------------------------

    async def extract_inquiries(self, req: Any) -> List[ExtractedInquiry]:
        agent_input = self._build_case_data(req)
        system, user = prompts.build_extract_inquiries_prompt(agent_input)
        try:
            resp = await self.deps.llm_router.call(
                "extract_inquiries", system, user, max_tokens=_EXTRACT_MAX_TOKENS
            )
        except Exception:
            logger.exception("extract_inquiries LLM call failed")
            return []

        items = parse_json_array(resp.content)
        if not items:
            return []

        extracted: List[ExtractedInquiry] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            inquiry = (it.get("inquiry") or "").strip()
            if not inquiry:
                continue
            rk = it.get("record_keeper")
            rk = rk if (rk not in (None, "", "N/A")) else req.record_keeper
            extracted.append(ExtractedInquiry(
                inquiry=inquiry,
                record_keeper=rk,
                plan_type=(it.get("plan_type") or _DEFAULT_PLAN_TYPE),
                topic=(it.get("topic") or "general").strip() or "general",
                related_inquiries=it.get("related_inquiries"),
            ))
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
        """Convenience full run: extract → handle each (capped). Returns []
        when no actionable inquiry was found (caller emits needs_more_info)."""
        extracted = await self.extract_inquiries(req)
        if not extracted:
            return []
        total = len(extracted)
        outcomes: List[InquiryOutcome] = []
        for ext in extracted[: 1 + self._max_related]:
            try:
                outcome = await asyncio.wait_for(
                    self.handle_inquiry(ext, req, total_inquiries=total),
                    self._inquiry_budget_s,
                )
            except asyncio.TimeoutError:
                logger.warning("inquiry budget exceeded for topic=%s", ext.topic)
                outcome = InquiryOutcome(
                    inquiry=ext.inquiry, topic=ext.topic, route="needs_more_info",
                    record_keeper=ext.record_keeper, plan_type=ext.plan_type,
                    needs_more_info_message=_DEFAULT_GREETING,
                    diagnostics={"error": "inquiry_budget_exceeded"},
                )
            outcomes.append(outcome)
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
        try:
            resp = await self.deps.llm_router.call(
                "kb_question_synthesis", system, user, max_tokens=_KB_SYNTH_MAX_TOKENS
            )
            parsed = parse_json_object(resp.content)
        except Exception:
            logger.exception("kb_question_synthesis failed")
            parsed = None

        question = (parsed or {}).get("question")
        if not parsed or parsed.get("insufficient_inquiry") or not question:
            return InquiryOutcome(
                inquiry=ext.inquiry, topic=ext.topic, route="needs_more_info",
                record_keeper=ext.record_keeper, plan_type=ext.plan_type,
                needs_more_info_message=getattr(classification, "user_message", None) or _DEFAULT_GREETING,
                diagnostics={**diag, "kb_insufficient": True},
            )

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
        rd = await self.deps.rag_engine.get_required_data(
            inquiry=ext.inquiry, record_keeper=ext.record_keeper,
            plan_type=ext.plan_type, topic=ext.topic,
            related_inquiries=ext.related_inquiries,
        )
        flat_fields = _flatten_required_fields(getattr(rd, "required_fields", {}))
        modules: List[Dict[str, Any]] = []
        extraction_candidates: List[Dict[str, Any]] = []
        if flat_fields:
            modules, extraction_candidates = await self._map_fields(flat_fields, diag)
        diag["mapped_modules"] = modules

        # 2) ForusBots scrapes AND ticket-field extraction, in PARALLEL — the
        # extraction only needs the ticket text, not the scrape result.
        async def _noop_scrape():
            return {}, {}, {}, "skipped"

        async def _noop_extract():
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

        # 3) build /generate-response body via the body-builder agent
        body = await self._build_gr_body(
            ppt_modules, plan_modules, scrape_meta, scrape_status,
            req, ext, total_inquiries, diag, ticket_extracted=extracted,
        )

        collected_data = body.get("collected_data") if isinstance(body, dict) else None
        collected_data = collected_data if isinstance(collected_data, dict) else {}

        gr = await self.deps.rag_engine.generate_response(
            inquiry=(body.get("inquiry") or ext.inquiry),
            record_keeper=body.get("record_keeper", ext.record_keeper),
            plan_type=(body.get("plan_type") or ext.plan_type),
            topic=(body.get("topic") or ext.topic),
            collected_data=collected_data,
            max_response_tokens=int(body.get("max_response_tokens") or req.max_response_tokens),
            total_inquiries_in_ticket=int(body.get("total_inquiries_in_ticket") or total_inquiries),
        )
        return InquiryOutcome(
            inquiry=ext.inquiry, topic=ext.topic, route="generate_response",
            record_keeper=ext.record_keeper, plan_type=ext.plan_type,
            scrape_status=scrape_status, generate_result=gr, diagnostics=diag,
        )

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
        det_by_slug: Dict[str, List[List[str]]] = {}
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
                det_by_slug[str(item.get("field"))] = [list(e) for e in entries]

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
                logger.exception("forusbots_field_map failed")
                mapping = {}
            raw_modules = mapping.get("modules")
            llm_modules = raw_modules if isinstance(raw_modules, list) else []
            llm_unmapped = mapping.get("_unmapped") or []
            if not mapping:
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

        async def _one(coro_label: str, coro) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
            try:
                scrape = await coro
                diag[f"forusbots_{coro_label}_job_id"] = scrape.job_id
                diag[f"forusbots_{coro_label}_elapsed_s"] = scrape.elapsed_seconds
                flat, meta = forusbots_catalog.normalize_scrape_result(scrape.result)
                if meta.get("module_errors") or meta.get("errors"):
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
                logger.warning("ForusBots %s timeout: %s", coro_label, e)
                return {}, {"error": str(e)}, "timeout"
            except (ForusBotsJobFailed, ForusBotsError) as e:
                logger.warning("ForusBots %s failed: %s", coro_label, e)
                return {}, {"error": str(e)}, "failed"

        tasks = []
        if p_modules:
            tasks.append(_one("participant",
                              self.deps.forusbots.scrape_participant(participant_id, p_modules)))
        if plan_modules:
            tasks.append(_one("plan",
                              self.deps.forusbots.scrape_plan(plan_id, plan_modules)))
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
            diag.setdefault("forusbots_elapsed_s", diag["forusbots_participant_elapsed_s"])
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

        Hard anti-hallucination gate: an extraction whose ``evidence`` quote is
        not actually present in the ticket text is demoted to not_found."""
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
            logger.exception("ticket_field_extract failed")
            parsed = {}

        ticket_text = " ".join(
            str(t or "") for t in (req.ticket.email_subject, req.ticket.email_body)
        ).lower()
        extracted: Dict[str, Dict[str, Any]] = {}
        demoted: List[str] = []
        raw_extracted = parsed.get("extracted")
        if isinstance(raw_extracted, dict):
            for field_name, entry in raw_extracted.items():
                if not isinstance(entry, dict):
                    continue
                evidence = str(entry.get("evidence") or "").strip()
                if not evidence or evidence.lower() not in ticket_text:
                    demoted.append(field_name)
                    continue
                extracted[forusbots_catalog._normalize_slug(field_name)] = {
                    "field": field_name,
                    "value": entry.get("value"),
                    "evidence": evidence,
                }

        not_found = [str(f) for f in (parsed.get("not_found") or [])] + demoted
        if not parsed:
            not_found = [str(it.get("field")) for it in candidates]

        fm = diag.setdefault("field_mapping", {})
        fm["ticket_extracted"] = {v["field"]: v["value"] for v in extracted.values()}
        fm["ticket_not_found"] = not_found
        if demoted:
            fm["ticket_evidence_demoted"] = demoted
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
    ) -> Dict[str, Any]:
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
            logger.exception("gr_body_build failed")
            body = None
        if not isinstance(body, dict):
            # degraded fallback: minimal body so generate_response still runs
            diag["gr_body_build_failed"] = True
            body = {
                "inquiry": ext.inquiry, "record_keeper": ext.record_keeper,
                "plan_type": ext.plan_type, "topic": ext.topic,
                "collected_data": {}, "total_inquiries_in_ticket": total_inquiries,
            }
        # Defensive merge: every ticket-extracted value must survive into
        # collected_data even if the body-builder dropped it.
        if ticket_extracted:
            cd = body.setdefault("collected_data", {})
            if isinstance(cd, dict):
                pd = cd.setdefault("participant_data", {})
                if isinstance(pd, dict):
                    for v in ticket_extracted.values():
                        pd.setdefault(v["field"], v["value"])
        return body

    # ------------------------------------------------------------------
    # Input shaping
    # ------------------------------------------------------------------

    def _build_ticket_data(self, req: Any) -> Dict[str, Any]:
        t = req.ticket
        ticket_messages = t.ticket_messages or {}
        tag = t.tag
        # Form-submission rule (defensive): the system-generated subject has no
        # relation to the question — force the agents to rely on the body only.
        if (t.email_subject or "").strip() == _FORM_SUBMISSION_SUBJECT:
            ticket_messages = {}
            tag = None
        return {
            "userId": None,
            "userName": t.username,
            "userEmail": t.user_email,
            "ticketId": t.ticket_id,
            "emailSubject": t.email_subject,
            "emailBody": t.email_body,
            "tag": tag,
            "firstContact": t.first_contact,
            "ticket_messages": ticket_messages,
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
        return {
            "classifier": {
                "route": getattr(classification, "route", None),
                "confidence": getattr(classification, "confidence", None),
                "reasoning": getattr(classification, "reasoning", None),
            }
        }


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
            ("module_errors", "moduleErrors"),
            ("unknown_fields", "unknownFields"),
            ("warnings", "warnings"),
            ("errors", "errors"),
            ("error", "errors"),
        ):
            val = side_meta.get(src)
            if val:
                bucket = out.setdefault(dst, {})
                bucket[side] = val
    if field_mapping.get("unmapped"):
        out["unmappedFields"] = field_mapping["unmapped"]
    if field_mapping.get("rejected"):
        out["rejectedMappings"] = field_mapping["rejected"]
    return out or None
