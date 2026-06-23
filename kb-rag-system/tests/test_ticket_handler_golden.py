"""
Golden tests — drive the REAL TicketOrchestrator (real packaged prompts, real
parsing/plumbing) with the LLM mocked to return the OUTPUTS documented in the
agent specs. This validates the end-to-end wiring against documented examples;
true behavioral parity (the LLM actually producing those outputs) is a separate
live suite (@pytest.mark.live, not run here).

Cases are harvested from the examples in External agents/*.md.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.models import HandleTicketRequest
from data_pipeline.ticket_orchestrator import OrchestratorDeps, TicketOrchestrator
from unittest.mock import AsyncMock


def _req(subject, body):
    return HandleTicketRequest(
        participant_id="158948", plan_id="580", company_name="StarWars Inc.",
        company_status="Ongoing",
        ticket={"username": "Ivan", "user_email": "i@f.com",
                "email_subject": subject, "email_body": body},
        record_keeper="LT Trust", ticket_handler_mode="full",
    )


class LLMStub:
    def __init__(self, by_task):
        self.by_task = by_task
        self.calls = []

    async def call(self, task_type, system, user, max_tokens=0, force_fallback=False):
        self.calls.append(task_type)
        return SimpleNamespace(content=self.by_task[task_type])


def _settings():
    return SimpleNamespace(TICKET_MAX_RELATED=3, TICKET_INQUIRY_BUDGET_S=300.0)


def _deps(llm, classify_route):
    rag = SimpleNamespace(
        get_required_data=AsyncMock(return_value=SimpleNamespace(required_fields={
            "participant_data": [
                {"field": "account_balance", "description": "total 401k balance", "required": True},
                {"field": "loan_balance", "description": "outstanding loan balance", "required": True},
            ]})),
        generate_response=AsyncMock(return_value=SimpleNamespace(
            decision="can_proceed", confidence=0.8, response={}, source_articles=[],
            used_chunks=[], coverage_gaps=[], metadata={})),
        ask_knowledge_question=AsyncMock(return_value=SimpleNamespace(
            answer="...", key_points=[], source_articles=[], used_chunks=[],
            confidence_note="well_covered", metadata={})),
    )
    router = SimpleNamespace(classify=AsyncMock(return_value=SimpleNamespace(
        route=classify_route, confidence=0.9, reasoning="r", user_message=None)))
    # Real confirmed payload shape: [{state, data: {<module>: {...}}}]
    forusbots = SimpleNamespace(scrape_participant=AsyncMock(return_value=SimpleNamespace(
        job_id="job-1", elapsed_seconds=10.0,
        result=[{"state": "succeeded",
                 "data": {"participantId": "158948",
                          "savings_rate": {"Account Balance": 123},
                          "loans": {"Account Balance": 50.0}},
                 "warnings": [], "errors": []}])))
    return OrchestratorDeps(rag, router, llm, forusbots), rag, forusbots


# ---------------------------------------------------------------------------
# Golden case 1 — Knowledge Question Ex1 (vague cash-out)
# ---------------------------------------------------------------------------

class TestGoldenKnowledgeQuestion:

    async def test_vague_cashout_synthesizes_documented_question(self):
        documented_q = ("What are the distribution options available to a participant who wants to "
                        "cash out their 401(k), including eligibility requirements, taxes, and penalties?")
        llm = LLMStub({
            "extract_inquiries": '[{"inquiry": "participant wants to cash out their 401k", '
                                 '"topic": "termination_distribution_request"}]',
            "kb_question_synthesis": f'{{"question": "{documented_q}"}}',
        })
        deps, rag, _f = _deps(llm, "knowledge_question")
        orch = TicketOrchestrator(deps, _settings())

        outcomes = await orch.run_ticket(_req("401k", "The customer wants to cash out their 401k."))

        assert len(outcomes) == 1
        o = outcomes[0]
        assert o.route == "knowledge_question"
        assert o.diagnostics["synthesized_question"] == documented_q
        rag.ask_knowledge_question.assert_awaited_once_with(question=documented_q)

    async def test_form_submission_insufficient_returns_nmi(self):
        # KQ Ex5: form-submission subject + body "401k" → insufficient
        llm = LLMStub({
            "extract_inquiries": '[{"inquiry": "401k", "topic": "general"}]',
            "kb_question_synthesis": '{"question": null, "insufficient_inquiry": true}',
        })
        deps, rag, _f = _deps(llm, "knowledge_question")
        orch = TicketOrchestrator(deps, _settings())

        outcomes = await orch.run_ticket(
            _req("Participant Advisory - Form Submission", "401k"))

        assert outcomes[0].route == "needs_more_info"
        assert outcomes[0].diagnostics.get("kb_insufficient") is True
        rag.ask_knowledge_question.assert_not_awaited()


# ---------------------------------------------------------------------------
# Golden case 2 — Field mapper Ex1 flows through the GR path
# ---------------------------------------------------------------------------

class TestGoldenGenerateResponse:

    async def test_field_map_documented_modules_drive_scrape(self):
        # Field mapper Ex1: account_balance + loan_balance → savings_rate + loans
        # Account Balance. Both slugs are canonical → resolved DETERMINISTICALLY
        # (the LLM mapper is not called); the request must still match the
        # documented spec output exactly.
        documented_modules = [
            {"key": "savings_rate", "fields": ["Account Balance"]},
            {"key": "loans", "fields": ["Account Balance"]},
        ]
        import json
        llm = LLMStub({
            "extract_inquiries": '[{"inquiry": "wants to cash out terminated 401k", "topic": "rollover"}]',
            "gr_body_build": json.dumps({
                "inquiry": "participant wants to cash out (withdraw) their ForUsAll 401(k) balance",
                "topic": "termination_distribution_request",
                "collected_data": {"participant_data": {"account_balance": 123},
                                   "plan_data": {"max_loans": 1}},
            }),
        })
        deps, rag, forusbots = _deps(llm, "generate_response")
        orch = TicketOrchestrator(deps, _settings())

        outcomes = await orch.run_ticket(_req("401k", "cash out my terminated 401k"))

        o = outcomes[0]
        assert o.route == "generate_response"
        assert o.scrape_status == "ok"
        # canonical slugs → deterministic origin, no LLM mapper call
        assert "forusbots_field_map" not in llm.calls
        assert o.diagnostics["mapped_modules"] == documented_modules
        # scrape requested EXACTLY the documented modules
        forusbots.scrape_participant.assert_awaited_once()
        assert forusbots.scrape_participant.await_args.args[1] == documented_modules
        # generate_response got the body-builder's enriched inquiry + collected_data
        kw = rag.generate_response.await_args.kwargs
        assert kw["collected_data"]["participant_data"]["account_balance"] == 123
        assert "cash out" in kw["inquiry"].lower()
