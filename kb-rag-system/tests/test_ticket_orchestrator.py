"""
Unit tests for the TicketOrchestrator (Stage 4).

All collaborators are mocked: the LLM router replays canned JSON per task_type,
the RAG engine / inquiry router / ForusBots client are AsyncMocks. No network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.models import HandleTicketRequest
from data_pipeline.forusbots_client import ForusBotsJobFailed, ForusBotsTimeout
from data_pipeline.ticket_orchestrator import (
    ExtractedInquiry,
    OrchestratorDeps,
    TicketOrchestrator,
    _detect_account_access_signal,
    _flatten_required_fields,
)


# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------

def _req(email_subject="401k", email_body="quiero retirar mi 401k", **over):
    base = dict(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing", company_status_detail=None,
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": email_subject, "email_body": email_body},
        record_keeper="LT Trust",
    )
    base.update(over)
    return HandleTicketRequest(**base)


class LLMStub:
    """Replays a canned response string per task_type; records calls + prompts."""

    def __init__(self, by_task):
        self.by_task = by_task
        self.calls = []
        self.user_prompts = {}

    async def call(self, task_type, system, user, max_tokens=0, force_fallback=False):
        self.calls.append(task_type)
        self.user_prompts[task_type] = user
        return SimpleNamespace(content=self.by_task[task_type])


def _settings(max_related=3):
    return SimpleNamespace(TICKET_MAX_RELATED=max_related, TICKET_INQUIRY_BUDGET_S=300.0)


def _classification(route, **kw):
    return SimpleNamespace(
        route=route, confidence=kw.get("confidence", 0.9),
        reasoning=kw.get("reasoning", "because"),
        user_message=kw.get("user_message"),
    )


def _deps(*, llm, classify_route="knowledge_question", classification=None):
    rag = SimpleNamespace(
        get_required_data=AsyncMock(),
        generate_response=AsyncMock(),
        ask_knowledge_question=AsyncMock(),
    )
    router = SimpleNamespace(
        classify=AsyncMock(return_value=classification or _classification(classify_route))
    )
    forusbots = SimpleNamespace(scrape_participant=AsyncMock())
    return OrchestratorDeps(rag_engine=rag, inquiry_router=router,
                            llm_router=llm, forusbots=forusbots), rag, router, forusbots


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class TestExtraction:

    async def test_extract_splits_financial_and_account_access(self):
        # F3 (eval 2026-06-22): a financial inquiry + a security/access blocker must
        # come back as two inquiries with cross-linked related_inquiries.
        arr = (
            '[{"inquiry": "Participant left their employer and wants to cash out their 401k",'
            ' "topic": "termination_distribution_request",'
            ' "related_inquiries": ["Participant received an unsolicited password reset email they did not request"]},'
            ' {"inquiry": "Participant received an unsolicited password reset email they did not request",'
            ' "topic": "account_access",'
            ' "related_inquiries": ["Participant left their employer and wants to cash out their 401k"]}]'
        )
        llm = LLMStub({"extract_inquiries": arr})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        out = await orch.extract_inquiries(_req())
        assert len(out) == 2
        assert {o.topic for o in out} == {"termination_distribution_request", "account_access"}
        acct = next(o for o in out if o.topic == "account_access")
        assert acct.related_inquiries  # cross-link populated

    async def test_extract_falls_back_record_keeper_and_defaults(self):
        llm = LLMStub({"extract_inquiries": '[{"inquiry": "wants to cash out 401k", "topic": "termination_distribution_request"}]'})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        out = await orch.extract_inquiries(_req())
        assert len(out) == 1
        assert out[0].record_keeper == "LT Trust"      # fell back to request value
        assert out[0].plan_type == "401(k)"            # default
        assert out[0].topic == "termination_distribution_request"

    async def test_extract_empty_array(self):
        llm = LLMStub({"extract_inquiries": "[]"})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        assert await orch.extract_inquiries(_req()) == []

    async def test_extract_unparseable_is_empty(self):
        llm = LLMStub({"extract_inquiries": "sorry, I cannot"})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        assert await orch.extract_inquiries(_req()) == []


# ---------------------------------------------------------------------------
# F3 — deterministic account_access split guard
# ---------------------------------------------------------------------------

class TestAccountAccessGuard:
    # LLM returns only the financial inquiry (the failure mode from C4/C5).
    _FIN = (
        '[{"inquiry": "Participant left their employer and wants to cash out their 401k",'
        ' "topic": "termination_distribution_request"}]'
    )

    async def test_injects_when_llm_omits_split(self):
        # F3: ticket carries a security signal but the LLM emitted only the
        # financial inquiry → the guard must add a separate account_access inquiry.
        llm = LLMStub({"extract_inquiries": self._FIN})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(
            email_subject="cash out + reset email",
            email_body="They received a password reset email but did not request it.",
        )
        out = await orch.extract_inquiries(req)
        assert len(out) == 2
        assert {o.topic for o in out} == {"termination_distribution_request", "account_access"}
        # injected at index 1 so it survives the dispatch cap (extracted[: 1 + _max_related])
        assert out[1].topic == "account_access"
        acct, fin = out[1], out[0]
        assert acct.record_keeper == "LT Trust"   # inherited from the request
        assert acct.plan_type == "401(k)"
        # bidirectional cross-link
        assert acct.related_inquiries == [fin.inquiry]
        assert acct.inquiry in (fin.related_inquiries or [])

    async def test_does_not_duplicate_when_llm_already_split(self):
        arr = (
            '[{"inquiry": "wants to cash out 401k", "topic": "termination_distribution_request"},'
            ' {"inquiry": "got a reset email I did not request", "topic": "account_access"}]'
        )
        llm = LLMStub({"extract_inquiries": arr})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(email_body="received a password reset email but did not request it")
        out = await orch.extract_inquiries(req)
        assert len(out) == 2
        assert sum(o.topic == "account_access" for o in out) == 1

    async def test_no_false_positive_on_plain_financial(self):
        # "email" appears benignly with no compound access phrase → no injection.
        llm = LLMStub({"extract_inquiries": self._FIN})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(
            email_subject="cash out",
            email_body="Participant wants to cash out their 401k. Please email the paperwork to them.",
        )
        out = await orch.extract_inquiries(req)
        assert len(out) == 1
        assert out[0].topic == "termination_distribution_request"

    async def test_none_email_body_is_safe(self):
        llm = LLMStub({"extract_inquiries": self._FIN})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        out = await orch.extract_inquiries(_req(email_subject="cash out", email_body=None))
        assert len(out) == 1   # no signal, no crash on None body

    async def test_multiple_signals_collapse_to_one_inquiry(self):
        # C5-shaped: locked out AND stale email → a single composite account_access.
        llm = LLMStub({"extract_inquiries": self._FIN})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(
            email_subject="rollover help",
            email_body=(
                "Former employee wants a rollover to Schwab. They are locked out "
                "and their email on file is no longer valid."
            ),
        )
        out = await orch.extract_inquiries(req)
        assert len(out) == 2
        acct = next(o for o in out if o.topic == "account_access")
        assert "cannot log in" in acct.inquiry
        assert "no longer valid" in acct.inquiry

    # --- DEFECT A (f3-07): "log into" / credential-invalid must be detected ---

    async def test_injects_on_cant_log_into(self):
        # f3-07 shape: rollover + "can't log into ... credentials are invalid".
        # The word-bounded match did NOT find "can't log in" inside "can't log
        # into", so the guard previously missed the split entirely.
        fin = ('[{"inquiry": "Participant wants to roll over an old 401(k) into '
               'their current plan", "topic": "rollover"}]')
        llm = LLMStub({"extract_inquiries": fin})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(
            email_subject="Two things",
            email_body=("First, I'd like to roll over my old 401(k) into my current "
                        "plan. Second, I can't log into my account — it says my "
                        "credentials are invalid."),
        )
        out = await orch.extract_inquiries(req)
        assert len(out) == 2
        assert {o.topic for o in out} == {"rollover", "account_access"}
        assert out[1].topic == "account_access"           # injected at index 1
        assert out[1].related_inquiries == [out[0].inquiry]  # cross-linked

    def test_detects_login_and_credential_variants(self):
        # Direct unit coverage of the phrases the word boundary used to drop.
        for phrase in (
            "i can't log into my account",
            "i cannot log in to the portal",
            "i am unable to sign in",
            "it says my credentials are invalid",
            "error: invalid credentials",
        ):
            assert _detect_account_access_signal(phrase) is not None, phrase

    async def test_no_false_positive_on_benign_login_mention(self):
        # "log into" WITHOUT a negated verb is a normal request, not a blocker.
        llm = LLMStub({"extract_inquiries": self._FIN})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(
            email_subject="beneficiary",
            email_body="I want to log into my account to update my beneficiary designation.",
        )
        out = await orch.extract_inquiries(req)
        assert len(out) == 1
        assert all(o.topic != "account_access" for o in out)

    # --- DEFECT B: "no longer works" employment phrase must not false-fire ---

    async def test_no_false_positive_on_no_longer_works_employment(self):
        # Plain financial ticket: "no longer works there" is EMPLOYMENT status,
        # not an email problem; the benign "email" mention must not split.
        llm = LLMStub({"extract_inquiries": self._FIN})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(
            email_subject="cash out",
            email_body=("The user no longer works there and wants to cash out their "
                        "401(k). Please email them the check."),
        )
        out = await orch.extract_inquiries(req)
        assert len(out) == 1
        assert all(o.topic != "account_access" for o in out)

    def test_still_detects_genuine_email_no_longer_works(self):
        # The real "my email no longer works" signal must be preserved.
        sig = _detect_account_access_signal(
            "i can't get my statements because my work email no longer works."
        )
        assert sig is not None
        assert "email on file is no longer valid" in sig

    def test_detects_email_no_longer_valid_with_intervening_words(self):
        # f3-02 pattern: "email" and "no longer valid" are NOT adjacent — the
        # (email present + validity phrase) form must still fire (guards against
        # an over-strict adjacency rewrite).
        sig = _detect_account_access_signal(
            "the email address used for this account is no longer valid"
        )
        assert sig is not None
        assert "email on file is no longer valid" in sig


# ---------------------------------------------------------------------------
# Knowledge-question branch
# ---------------------------------------------------------------------------

class TestKnowledgeBranch:

    async def test_kq_sufficient_calls_engine(self):
        llm = LLMStub({"kb_question_synthesis": '{"question": "What are the cash-out options?"}'})
        deps, rag, _r, _f = _deps(llm=llm, classify_route="knowledge_question")
        rag.ask_knowledge_question.return_value = SimpleNamespace(answer="A", key_points=[])
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "termination_distribution_request")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert out.route == "knowledge_question"
        assert out.knowledge_result.answer == "A"
        assert out.diagnostics["synthesized_question"] == "What are the cash-out options?"
        rag.ask_knowledge_question.assert_awaited_once()

    async def test_kq_insufficient_returns_needs_more_info(self):
        llm = LLMStub({"kb_question_synthesis": '{"question": null, "insufficient_inquiry": true}'})
        deps, rag, _r, _f = _deps(llm=llm, classify_route="knowledge_question")
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("401k", "LT Trust", "401(k)", "general")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert out.route == "needs_more_info"
        assert out.needs_more_info_message
        assert out.diagnostics.get("kb_insufficient") is True
        rag.ask_knowledge_question.assert_not_awaited()

    async def test_kq_synthesis_is_inquiry_scoped(self):
        # Cross-contamination fix: the KB-question synthesis must focus on THIS
        # inquiry's text, not the whole ticket, so a co-occurring second topic
        # cannot hijack the synthesized question (eval f3-07/f3-03).
        llm = LLMStub({"kb_question_synthesis":
                       '{"question": "How does a participant change their contribution rate?"}'})
        deps, rag, _r, _f = _deps(llm=llm, classify_route="knowledge_question")
        rag.ask_knowledge_question.return_value = SimpleNamespace(answer="A", key_points=[])
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry(
            "Participant wants to increase their contribution rate from 5% to 10%",
            "LT Trust", "401(k)", "contribution_change",
        )
        req = _req(
            email_subject="Help with my 401k",
            email_body=("I want to increase my contribution rate. Also I cannot "
                        "complete MFA and can't get into the portal."),
        )
        await orch.handle_inquiry(
            ext, req, total_inquiries=2,
            classification=_classification("knowledge_question"),
        )
        synth = llm.user_prompts["kb_question_synthesis"]
        assert "contribution rate" in synth                    # focused inquiry present
        assert "MFA" not in synth and "portal" not in synth    # other topic excluded


# ---------------------------------------------------------------------------
# Generate-response branch
# ---------------------------------------------------------------------------

def _scrape_ok(job_id="job-1", modules=None):
    """Real-payload-shaped result (confirmed): [{state, data: {<module>: {...}}}]."""
    data = {"participantId": "158948"}
    data.update(modules or {"savings_rate": {"Account Balance": 123}})
    return SimpleNamespace(job_id=job_id, elapsed_seconds=12.0,
                           result=[{"state": "succeeded", "data": data,
                                    "warnings": [], "errors": []}])


class TestGenerateBranch:

    def _gr_llm(self, extra=None):
        by_task = {
            "forusbots_field_map": '{"modules": [{"key": "savings_rate", "fields": ["Account Balance"]}]}',
            "gr_body_build": '{"inquiry": "enriched OUT rollover", "topic": "rollover", '
                             '"collected_data": {"participant_data": {"account_balance": 123}}}',
        }
        by_task.update(extra or {})
        return LLMStub(by_task)

    async def test_gr_happy_path_deterministic_no_llm_mapper(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "account_balance", "description": "bal",
                                  "why_needed": "elig", "data_type": "currency", "required": True}]
        })
        forusbots.scrape_participant.return_value = _scrape_ok()
        rag.generate_response.return_value = SimpleNamespace(decision="can_proceed", confidence=0.8)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=2)

        assert out.route == "generate_response"
        assert out.scrape_status == "ok"
        assert out.generate_result.decision == "can_proceed"
        # canonical slug resolved deterministically — LLM mapper NOT called
        assert "forusbots_field_map" not in llm.calls
        assert out.diagnostics["field_mapping"]["llm_called"] is False
        assert out.diagnostics["mapped_modules"] == [{"key": "savings_rate", "fields": ["Account Balance"]}]
        assert out.diagnostics["forusbots_job_id"] == "job-1"
        # the wrapped real-payload result was NORMALIZED before gr_body_build:
        # the agent input contains the flat module, not the {state,data} wrapper
        gr_user = llm.user_prompts["gr_body_build"]
        assert '"Account Balance": 123' in gr_user
        assert '"state"' not in gr_user
        kw = rag.generate_response.await_args.kwargs
        assert kw["collected_data"] == {"participant_data": {"account_balance": 123}}
        assert kw["inquiry"] == "enriched OUT rollover"
        assert kw["total_inquiries_in_ticket"] == 2
        forusbots.scrape_participant.assert_awaited_once()
        # the scrape request used the deterministic modules
        assert forusbots.scrape_participant.await_args.args[1] == \
            [{"key": "savings_rate", "fields": ["Account Balance"]}]

    async def test_gr_hybrid_partial_goes_to_llm(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [
                {"field": "account_balance", "required": True},
                {"field": "hardship_reason", "description": "why funds are needed",
                 "required": True},
            ]})
        forusbots.scrape_participant.return_value = _scrape_ok()
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.5)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("hardship", "LT Trust", "401(k)", "hardship_withdrawal")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        # LLM was called ONLY with the unresolved field + the runtime year
        assert "forusbots_field_map" in llm.calls
        fm_user = llm.user_prompts["forusbots_field_map"]
        assert "hardship_reason" in fm_user
        assert "account_balance" not in fm_user
        assert "CURRENT YEAR:" in fm_user
        assert out.diagnostics["field_mapping"]["llm_called"] is True

    async def test_gr_llm_output_validated_against_catalog(self):
        llm = self._gr_llm(extra={
            "forusbots_field_map": '{"modules": ['
                '{"key": "communications", "fields": ["Message History"]}, '
                '{"key": "census", "fields": ["termination date", "SSN"]}]}'
        })
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "weird_unknown_thing", "required": True}]})
        forusbots.scrape_participant.return_value = _scrape_ok(
            modules={"census": {"Termination Date": "2019-10-01"}})
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.5)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        # communications rejected, case fixed up, SSN rejected
        assert out.diagnostics["mapped_modules"] == \
            [{"key": "census", "fields": ["Termination Date"]}]
        reasons = {r["reason"] for r in out.diagnostics["field_mapping"]["rejected"]}
        assert "no_structured_extractor" in reasons
        assert "full_ssn_not_permitted" in reasons

    async def test_gr_all_deterministic_would_keyerror_if_llm_called(self):
        # LLMStub has NO forusbots_field_map entry: calling it would KeyError
        llm = LLMStub({
            "gr_body_build": '{"inquiry": "x", "topic": "rollover", "collected_data": {}}',
        })
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "termination_date", "required": True},
                                 {"field": "mfa_status", "required": True}],
            "plan_data": [{"field": "force_out_limit", "required": True}]})
        forusbots.scrape_participant.return_value = _scrape_ok(
            modules={"census": {"Termination Date": ""}})
        rag.generate_response.return_value = SimpleNamespace(decision="can_proceed", confidence=0.9)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)
        assert out.diagnostics["mapped_modules"] == [
            {"key": "census", "fields": ["Termination Date"]},
            {"key": "plan_details", "fields": ["Force-out Limit"]},
            {"key": "mfa", "fields": ["MFA Status"]},
        ]

    async def test_gr_unmapped_flows_to_body_builder(self):
        import json as _json
        llm = self._gr_llm(extra={
            "forusbots_field_map": '{"modules": [], "_unmapped": '
                '[{"field": "hardship_reason", "reason": "No extractor available"}]}'
        })
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [
                {"field": "account_balance", "required": True},
                {"field": "hardship_reason", "required": True},
            ]})
        forusbots.scrape_participant.return_value = _scrape_ok()
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.5)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("hardship", "LT Trust", "401(k)", "hardship_withdrawal")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        gr_user = llm.user_prompts["gr_body_build"]
        assert "dataCollection" in gr_user
        assert "hardship_reason" in gr_user
        assert out.diagnostics["unmapped_fields"][0]["field"] == "hardship_reason"

    async def test_gr_plan_fields_trigger_dual_scrape(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "account_balance", "required": True}],
            "plan_data": [{"field": "default_savings_rate", "required": True},
                          {"field": "ein", "required": True}]})
        forusbots.scrape_participant.return_value = _scrape_ok()
        forusbots.scrape_plan = AsyncMock(return_value=SimpleNamespace(
            job_id="plan-job-1", elapsed_seconds=8.0,
            result=[{"state": "succeeded",
                     "data": {"planId": "580",
                              "plan_design": {"default_savings_rate": 6},
                              "basic_info": {"ein": "12-3456789"},
                              "notes": ["backfill force out limit"]},
                     "warnings": [], "errors": []}]))
        rag.generate_response.return_value = SimpleNamespace(decision="can_proceed", confidence=0.8)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("match question", "LT Trust", "401(k)", "employer_match")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert out.scrape_status == "ok"
        # both scrapes ran with their split module lists
        forusbots.scrape_participant.assert_awaited_once()
        forusbots.scrape_plan.assert_awaited_once()
        plan_args = forusbots.scrape_plan.await_args.args
        assert plan_args[0] == "580"                       # plan_id from request
        plan_keys = {m["key"] for m in plan_args[1]}
        assert plan_keys == {"basic_info", "plan_design"}
        # plan data + notes reached the body builder as planDataModules
        gr_user = llm.user_prompts["gr_body_build"]
        assert "planDataModules" in gr_user
        assert "default_savings_rate" in gr_user
        assert "plan_notes" in gr_user
        assert out.diagnostics["forusbots_plan_job_id"] == "plan-job-1"

    async def test_gr_plan_failure_downgrades_to_partial(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "account_balance", "required": True}],
            "plan_data": [{"field": "ein", "required": True}]})
        forusbots.scrape_participant.return_value = _scrape_ok()
        forusbots.scrape_plan = AsyncMock(
            side_effect=ForusBotsJobFailed("plan-x", "failed", "plan not found"))
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.5)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("ein question", "LT Trust", "401(k)", "plan_information")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert out.scrape_status == "partial"     # participant ok, plan failed
        rag.generate_response.assert_awaited_once()
        gr_user = llm.user_prompts["gr_body_build"]
        assert "dataCollection" in gr_user        # plan failure reported

    async def test_gr_scrape_failure_degrades(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "account_balance", "required": True}]})
        forusbots.scrape_participant.side_effect = ForusBotsJobFailed("job-x", "failed", "not found")
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.4)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert out.scrape_status == "failed"
        assert out.generate_result is not None        # degraded-proceed: still generated
        rag.generate_response.assert_awaited_once()

    async def test_gr_timeout_degrades(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "account_balance", "required": True}]})
        forusbots.scrape_participant.side_effect = ForusBotsTimeout("job-x", 200.0)
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.4)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)
        assert out.scrape_status == "timeout"
        assert out.generate_result is not None

    async def test_gr_no_required_fields_skips_scrape(self):
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={})
        rag.generate_response.return_value = SimpleNamespace(decision="can_proceed", confidence=0.9)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("how long", "LT Trust", "401(k)", "distribution")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)
        assert out.scrape_status == "skipped"
        forusbots.scrape_participant.assert_not_awaited()

    async def test_gr_ticket_extraction_fills_unmapped_field(self):
        # hardship_reason is unmapped by the LLM mapper BUT the participant
        # stated it in the ticket → the extraction layer recovers it.
        llm = self._gr_llm(extra={
            "forusbots_field_map": '{"modules": [], "_unmapped": '
                '[{"field": "hardship_reason", "reason": "No extractor available"}]}',
            "ticket_field_extract": '{"extracted": {"hardship_reason": '
                '{"value": "medical bills", "evidence": "medical bills my insurance"}}, '
                '"not_found": []}',
        })
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [
                {"field": "account_balance", "required": True},
                {"field": "hardship_reason", "description": "why funds are needed",
                 "required": True},
            ]})
        forusbots.scrape_participant.return_value = _scrape_ok()
        rag.generate_response.return_value = SimpleNamespace(decision="can_proceed", confidence=0.8)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("hardship", "LT Trust", "401(k)", "hardship_withdrawal")
        req = _req(email_body="I need to withdraw to cover medical bills my insurance won't pay")

        out = await orch.handle_inquiry(ext, req, total_inquiries=1)

        assert "ticket_field_extract" in llm.calls
        # extraction user prompt got the candidate + the ticket text
        tfe_user = llm.user_prompts["ticket_field_extract"]
        assert "hardship_reason" in tfe_user and "medical bills" in tfe_user
        # extracted value reached the body-builder input
        assert "ticketExtractedFields" in llm.user_prompts["gr_body_build"]
        # defensive merge: value present in collected_data even though the
        # stubbed body-builder output omitted it
        kw = rag.generate_response.await_args.kwargs
        assert kw["collected_data"]["participant_data"]["hardship_reason"] == "medical bills"
        # the answered field is no longer a collection gap
        assert out.diagnostics["field_mapping"]["unmapped"] == []
        assert out.diagnostics["field_mapping"]["ticket_extracted"] == \
            {"hardship_reason": "medical bills"}

    async def test_gr_ticket_extraction_evidence_gate_demotes_hallucination(self):
        # the extractor claims a value whose evidence is NOT in the ticket →
        # hard gate demotes it to not_found
        llm = self._gr_llm(extra={
            "forusbots_field_map": '{"modules": [], "_unmapped": '
                '[{"field": "loan_amount_needed", "reason": "ticket field"}]}',
            "ticket_field_extract": '{"extracted": {"loan_amount_needed": '
                '{"value": 50000, "evidence": "I want fifty thousand dollars"}}, '
                '"not_found": []}',
        })
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "loan_amount_needed", "required": True}]})
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.5)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("loan", "LT Trust", "401(k)", "loan_request")
        req = _req(email_body="how do I request a loan?")   # no amount stated

        out = await orch.handle_inquiry(ext, req, total_inquiries=1)

        fm = out.diagnostics["field_mapping"]
        assert fm["ticket_extracted"] == {}
        assert "loan_amount_needed" in fm["ticket_not_found"]
        assert fm["ticket_evidence_demoted"] == ["loan_amount_needed"]
        # still a gap → flows to dataCollection
        assert "dataCollection" in llm.user_prompts["gr_body_build"]

    async def test_gr_request_provided_fields_skip_everything(self):
        # plan_id is carried by the request: no LLM mapper, no extraction, no unmapped
        llm = self._gr_llm()
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "account_balance", "required": True}],
            "plan_data": [{"field": "plan_id", "required": True}]})
        forusbots.scrape_participant.return_value = _scrape_ok()
        rag.generate_response.return_value = SimpleNamespace(decision="can_proceed", confidence=0.8)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert "forusbots_field_map" not in llm.calls
        assert "ticket_field_extract" not in llm.calls
        assert out.diagnostics["field_mapping"]["request_provided"] == ["plan_id"]
        assert out.diagnostics["field_mapping"]["unmapped"] == []

    async def test_gr_extraction_parse_failure_degrades_to_not_found(self):
        llm = self._gr_llm(extra={
            "forusbots_field_map": '{"modules": [], "_unmapped": '
                '[{"field": "hardship_reason", "reason": "ticket field"}]}',
            "ticket_field_extract": "I cannot produce JSON today",
        })
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "hardship_reason", "required": True}]})
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.4)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("hardship", "LT Trust", "401(k)", "hardship_withdrawal")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        fm = out.diagnostics["field_mapping"]
        assert fm["ticket_extracted"] == {}
        assert fm["ticket_not_found"] == ["hardship_reason"]
        rag.generate_response.assert_awaited_once()   # degraded-proceed

    async def test_gr_mapper_parse_failure_records_reason(self):
        llm = self._gr_llm(extra={"forusbots_field_map": "sorry, I cannot do JSON"})
        deps, rag, _r, forusbots = _deps(llm=llm, classify_route="generate_response")
        rag.get_required_data.return_value = SimpleNamespace(required_fields={
            "participant_data": [{"field": "totally_unknown_field", "required": True}]})
        rag.generate_response.return_value = SimpleNamespace(decision="uncertain", confidence=0.3)
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("cash out", "LT Trust", "401(k)", "rollover")

        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)

        assert out.scrape_status == "skipped"
        assert out.diagnostics["scrape_skip_reason"] == "no_mappable_fields"
        assert out.diagnostics["field_mapping"]["llm_failed"] is True
        assert out.diagnostics["unmapped_fields"][0]["reason"] == "mapper_parse_failure"
        forusbots.scrape_participant.assert_not_awaited()
        rag.generate_response.assert_awaited_once()   # degraded-proceed


# ---------------------------------------------------------------------------
# needs_more_info + run_ticket
# ---------------------------------------------------------------------------

class TestNeedsMoreInfoAndRun:

    async def test_needs_more_info_uses_classifier_message(self):
        llm = LLMStub({})
        deps, *_ = _deps(llm=llm, classification=_classification("needs_more_info", user_message="¿Puedes dar más detalle?"))
        orch = TicketOrchestrator(deps, _settings())
        ext = ExtractedInquiry("hola", "LT Trust", "401(k)", "general")
        out = await orch.handle_inquiry(ext, _req(), total_inquiries=1)
        assert out.route == "needs_more_info"
        assert out.needs_more_info_message == "¿Puedes dar más detalle?"

    async def test_run_ticket_empty_extraction(self):
        llm = LLMStub({"extract_inquiries": "[]"})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        assert await orch.run_ticket(_req()) == []

    async def test_run_ticket_caps_related(self):
        five = "[" + ",".join(
            f'{{"inquiry": "q{i}", "topic": "general"}}' for i in range(5)) + "]"
        llm = LLMStub({"extract_inquiries": five, "kb_question_synthesis": '{"question": "Q?"}'})
        deps, rag, _r, _f = _deps(llm=llm, classify_route="knowledge_question")
        rag.ask_knowledge_question.return_value = SimpleNamespace(answer="A")
        orch = TicketOrchestrator(deps, _settings(max_related=3))
        outcomes = await orch.run_ticket(_req())
        assert len(outcomes) == 4          # primary + 3 related (capped)


# ---------------------------------------------------------------------------
# Form-submission guard + flatten helper
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_form_submission_strips_messages_and_tag(self):
        llm = LLMStub({})
        deps, *_ = _deps(llm=llm)
        orch = TicketOrchestrator(deps, _settings())
        req = _req(email_subject="Participant Advisory - Form Submission",
                   email_body="how long to receive funds?")
        # inject forward-compat fields that must be ignored under the form rule
        req.ticket.ticket_messages = {"message_1": "noise"}
        req.ticket.tag = "NOT FOUND"
        td = orch._build_ticket_data(req)
        assert td["ticket_messages"] == {}
        assert td["tag"] is None
        assert td["emailBody"] == "how long to receive funds?"

    def test_flatten_required_fields_dict_and_object(self):
        class RF:
            def __init__(self):
                self.field = "vested_balance"; self.description = "d"
                self.why_needed = "w"; self.data_type = "currency"; self.required = True
        rf = {
            "participant_data": [{"field": "account_balance", "description": "b",
                                  "why_needed": "n", "data_type": "currency", "required": True}],
            "plan_data": [RF()],
        }
        flat = _flatten_required_fields(rf)
        assert len(flat) == 2
        names = {f["field"] for f in flat}
        assert names == {"account_balance", "vested_balance"}
