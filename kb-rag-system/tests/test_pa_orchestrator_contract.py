"""Sanitized simulations of the PA round's missing end-to-end data paths."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.models import HandleTicketRequest
from data_pipeline.ticket_orchestrator import ExtractedInquiry, OrchestratorDeps, TicketOrchestrator


class LLM:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    async def call(self, task, system, user, **kwargs):
        self.calls.append((task, user))
        return SimpleNamespace(content=json.dumps(self.responses[task]))


def request(body="Can I move my old 401(k) to an IRA?", **kwargs):
    return HandleTicketRequest(
        participant_id="111", plan_id="222", company_name="Fixture Company",
        company_status="Ongoing", ticket={"username": "Fixture", "user_email": "fixture@example.test",
                                          "email_subject": "Retirement account", "email_body": body},
        record_keeper="LT Trust", **kwargs,
    )


def setup(*, fields=None, metadata=None, llm=None):
    rag = SimpleNamespace(
        get_required_data=AsyncMock(return_value=SimpleNamespace(required_fields=fields or {}, metadata=metadata or {})),
        generate_response=AsyncMock(return_value=SimpleNamespace(outcome="needs_more_info")),
    )
    llm = llm or LLM(gr_body_build={"inquiry": "A shortened question", "topic": "rollover"})
    orch = TicketOrchestrator(OrchestratorDeps(rag, SimpleNamespace(), llm, SimpleNamespace()), SimpleNamespace())
    orch._scrape_all = AsyncMock(return_value=({}, {}, {}, "skipped"))
    return orch, rag, llm


CLASSIFICATION = SimpleNamespace(route="generate_response", confidence=1, metadata={})


async def test_lifecycle_and_preflight_requested_even_if_kb_only_asks_balance():
    orch, _, _ = setup(fields={"participant_data": [{"field": "account_balance", "required": True}]})
    ext = ExtractedInquiry("Please roll my former employer's 401k into an IRA", "LT Trust", "401(k)", "termination_distribution_request")
    result = await orch._handle_gr(ext, request(), CLASSIFICATION, 1)
    modules = {m["key"]: set(m["fields"]) for m in result.diagnostics["mapped_modules"]}
    assert {"status", "status_as_of", "active"} <= modules.get("basic_info", set())
    assert {"Eligibility Status", "Termination Date", "Crypto Enrollment"} <= modules.get("census", set())
    assert "Loan History" in modules.get("loans", set())
    assert "Employer Match Vested Balance" in modules.get("savings_rate", set())
    assert orch._scrape_all.await_count == 1  # durable operation is submitted once per entity


async def test_nonfinancial_inquiry_does_not_collect_distribution_data():
    orch, _, _ = setup()
    ext = ExtractedInquiry("Why did my contribution election not change?", "LT Trust", "401(k)", "contributions")
    result = await orch._handle_gr(ext, request(), CLASSIFICATION, 1)
    assert result.diagnostics["mapped_modules"] == []
    orch._scrape_all.assert_not_awaited()


async def test_message_fields_go_to_evidence_extractor_without_scraping():
    llm = LLM(gr_body_build={}, ticket_field_extract={"extracted": {
        "hardship_reason": {"value": "medical bills", "evidence": "medical bills"}
    }, "not_found": []})
    orch, rag, _ = setup(llm=llm, metadata={"conversation_fields": [{
        "field": "hardship_reason", "description": "Reason", "why_needed": "Assess category",
        "source": "message_text", "data_type": "text", "required": True,
    }]})
    ext = ExtractedInquiry("I need help with medical bills", "LT Trust", "401(k)", "general")
    await orch._handle_gr(ext, request("I need help with medical bills"), CLASSIFICATION, 1)
    data = rag.generate_response.await_args.kwargs["collected_data"]
    assert data["participant_data"]["hardship_reason"] == "medical bills"
    orch._scrape_all.assert_not_awaited()


async def test_company_detail_and_failed_extraction_reach_consumer():
    orch, rag, _ = setup(fields={"participant_data": [{"field": "account_balance"}]})
    orch._scrape_all.return_value = (
        {"savings_rate": {"Account Balance": 0}, "loans": {"Loan History": []}},
        {"basic_info": {"status": "pending_term", "active": True}},
        {"participant": {"extraction_diagnostics": {"modules": {"loans": {"dataState": "parse_error"}}}}}, "partial",
    )
    ext = ExtractedInquiry("Where is my balance?", "LT Trust", "401(k)", "balance")
    await orch._handle_gr(ext, request(company_status_detail="Pending termination"), CLASSIFICATION, 1)
    data = rag.generate_response.await_args.kwargs["collected_data"]
    assert data["plan_data"]["company_status_detail"] == "Pending termination"
    assert data["internal_preflight_context"]["loans"]["outstanding_status"] == "unknown"


async def test_body_redaction_cannot_drop_original_questions():
    orch, rag, _ = setup()
    inquiry = "Which investments can I choose, what are the fees, and can I do a partial rollover?"
    ext = ExtractedInquiry(inquiry, "LT Trust", "401(k)", "general")
    await orch._handle_gr(ext, request(inquiry), CLASSIFICATION, 1)
    data = rag.generate_response.await_args.kwargs["collected_data"]
    assert data["internal_response_context"]["requested_questions"] == [inquiry]


@pytest.mark.parametrize("failure", ["invalid_json", "schema_rejected", "empty_extraction", "provider_error"])
async def test_required_data_diagnostic_preserves_exact_non_retryable_failure(failure):
    orch, rag, _ = setup(metadata={"error": "required_data_failed", "required_data_failure_kind": failure})
    ext = ExtractedInquiry("Can I take a hardship distribution?", "LT Trust", "401(k)", "hardship")
    result = await orch._handle_gr(ext, request(), CLASSIFICATION, 1)
    assert result.diagnostics["required_data_failure"] == {"failure_kind": failure, "retryable": False}
    rag.generate_response.assert_not_awaited()
    orch._scrape_all.assert_not_awaited()


async def test_extraction_preserves_only_questions_with_literal_ticket_evidence():
    questions = ["What fees apply?", "Can I move only part of the account?"]
    llm = LLM(extract_inquiries=[{"inquiry": "Participant asks about an outgoing rollover", "topic": "rollover",
                                  "requested_questions": questions + ["Please withdraw everything now"]}])
    orch, _, _ = setup(llm=llm)
    result = await orch.extract_inquiries(request(" ".join(questions)))
    assert result[0].requested_questions == questions


def test_question_inventory_survives_durable_execution_plan():
    from api.ticket_worker import _build_execution_plan
    ext = ExtractedInquiry("Questions about fees", "LT Trust", "401(k)", "rollover", requested_questions=["What fees apply?"])
    result = _build_execution_plan([ext], [CLASSIFICATION], [("generate_response", None)], 1, 0)
    assert result["inquiries"][0]["requested_questions"] == ["What fees apply?"]


def test_prompt_preserves_bounded_operational_facts_preflight_and_questions():
    from data_pipeline.prompts import _format_collected_data
    text = _format_collected_data({
        "internal_plan_context": {"extraction_status": "ok", "current": {"status": "pending_termination", "active": True},
                                  "operational_facts": [{"kind": "distribution_hold", "value": True, "source": "plan_history.notes", "recorded_at": "2026-03-26", "effective_on": "2026-03-26", "note": "SECRET"}],
                                  "completeness": {"complete": False, "truncated": True}},
        "internal_preflight_context": {"loans": {"outstanding_status": "unknown", "status": "error", "source": "participant.loans.Loan History"},
                                       "crypto": {"enrollment": {"status": "known", "value": False}, "holdings": {"status": "unknown"}}},
        "internal_response_context": {"requested_questions": ["What fees apply?", "Can I move only part of the account?"]},
    })
    for expected in ["pending_termination", "distribution_hold", "complete: false", "outstanding_status", "unknown", "holdings", "What fees apply?", "Can I move only part"]:
        assert expected in text
    assert "SECRET" not in text


def test_extraction_prompt_requests_literal_questions():
    from data_pipeline.prompts import build_extract_inquiries_prompt
    system, _ = build_extract_inquiries_prompt({})
    assert "requested_questions" in system
    assert "verbatim" in system


async def test_kq_preserves_lookup_failure_reason_to_composer():
    llm = LLM(kb_question_synthesis={"question": "How can a former employee access retirement funds?"})
    orch, rag, _ = setup(llm=llm)
    rag.ask_knowledge_question = AsyncMock()
    req = request(identity_context={"identity_resolution_status": "access_error", "response_source_reason": "account_lookup_failed", "provided_identifiers": ["name", "employer"]})
    ext = ExtractedInquiry("How can I access my former account?", "LT Trust", "401(k)", "account_access")
    await orch._handle_kq(ext, req, CLASSIFICATION)
    assert rag.ask_knowledge_question.await_args.kwargs["identity_context"]["identity_resolution_status"] == "access_error"


async def test_recordkeeper_identifier_is_requested_and_reaches_consumer():
    orch, rag, _ = setup()
    query = 'What is my plan ID for the rollover form?'
    orch._scrape_all.return_value = ({}, {'plan_design': {'rk_plan_id': 'SYNTHETIC-RK', 'record_keeper_id': 'LT Trust'}}, {}, 'ok')
    ext = ExtractedInquiry(query, 'LT Trust', '401(k)', 'general')
    result = await orch._handle_gr(ext, request(query), CLASSIFICATION, 1)
    modules = {m['key']: set(m['fields']) for m in result.diagnostics['mapped_modules']}
    assert {'rk_plan_id', 'record_keeper_id'} <= modules.get('plan_design', set())
    assert rag.generate_response.await_args.kwargs['collected_data']['plan_data']['rk_plan_id'] == 'SYNTHETIC-RK'
    orch._scrape_all.assert_awaited_once()


def test_loan_history_empty_state_survives_prompt_projection():
    from data_pipeline.prompts import _format_internal_preflight
    output = _format_internal_preflight({'loans': {'status': 'empty', 'outstanding_status': 'zero'}})
    assert json.loads(output.split('\n', 1)[1])['loans'] == {'status': 'empty', 'outstanding_status': 'zero'}

@pytest.mark.parametrize('verified', [True, False])
async def test_verified_disclosure_requests_name_and_reaches_reasoning_only_with_provenance(verified):
    orch, rag, _ = setup(fields={'participant_data': [{'field': 'account_balance'}]})
    orch._scrape_all.return_value = (
        {'census': {'First Name': 'Alex'}, 'savings_rate': {'Account Balance': 500}}, {},
        {'participant': {'extraction_diagnostics': {'modules': {
            'census': {'dataState': 'ok', 'observedAt': '2026-09-11T18:00:00Z'},
            'savings_rate': {'dataState': 'ok', 'observedAt': '2026-09-11T18:00:00Z'},
        }}}}, 'ok',
    )
    identity = {'identity_resolution_status': 'matched', 'identity_verified': verified,
                'response_source_reason': 'account_context_required'}
    ext = ExtractedInquiry('What is my account balance?', 'LT Trust', '401(k)', 'balance')
    result = await orch._handle_gr(ext, request(identity_context=identity), CLASSIFICATION, 1)
    modules = {m['key']: m['fields'] for m in result.diagnostics['mapped_modules']}
    assert ('First Name' in modules['census']) is verified
    data = rag.generate_response.await_args.kwargs['collected_data']
    if verified:
        from data_pipeline.prompts import _format_collected_data
        facts = data['internal_disclosure_context']['facts']
        assert facts['first_name']['value'] == 'Alex'
        assert facts['account_balance']['value'] == 500
        rendered = _format_collected_data(data)
        assert 'participant.census.First Name' in rendered
        assert '2026-09-11T18:00:00Z' in rendered
    else:
        assert 'internal_disclosure_context' not in data

async def test_n8n_selected_account_contract_drives_name_request_and_disclosure():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    script = "const fs=require('fs'),vm=require('vm');const s=fs.readFileSync(process.argv[1],'utf8');const selected={Result:'Participant was found',userData:{pptId:'111',planId:'222'}};const i=vm.runInNewContext('(function(){'+s+'\\n})()',{$:name=>name==='Participant Search'?{first:()=>({json:selected})}:{item:{json:{userData:{pptId:'111',planId:'222'}}}}});process.stdout.write(JSON.stringify(i));"
    identity = json.loads(subprocess.check_output(['node', '-e', script, str(root/'PA/n8n/candidates/handle-ticket-identity.js')], text=True))
    req = request(identity_context=identity)
    orch, rag, _ = setup()
    orch._scrape_all.return_value = (
        {'census': {'First Name': 'Alex'}}, {},
        {'participant': {'extraction_diagnostics': {'modules': {'census': {'dataState': 'ok', 'observedAt': '2026-09-11T18:00:00Z'}}}}}, 'ok',
    )
    result = await orch._handle_gr(ExtractedInquiry('What is my account balance?', 'LT Trust', '401(k)', 'balance'), req, CLASSIFICATION, 1)
    assert any(m['key']=='census' and 'First Name' in m['fields'] for m in result.diagnostics['mapped_modules'])
    data = rag.generate_response.await_args.kwargs['collected_data']
    assert data['internal_disclosure_context']['facts']['first_name']['value'] == 'Alex'
