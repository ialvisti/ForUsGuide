"""
Unit tests for the Stage-3 ticket-handler agent wiring:
  * the 4 prompt builders load the packaged canonical specs and inject the input,
  * the defensive JSON parsers handle fenced / object / array / garbage,
  * the new LLM task routes are registered in the routing table.
"""

from __future__ import annotations

from types import SimpleNamespace

from data_pipeline.json_parsing import parse_json_array, parse_json_object
from data_pipeline.llm_router import build_routes_from_settings
from data_pipeline.prompts import (
    SYSTEM_PROMPT_REQUIRED_DATA,
    build_extract_inquiries_prompt,
    build_forusbots_field_map_prompt,
    build_gr_body_build_prompt,
    build_kb_question_synthesis_prompt,
    build_ticket_field_extract_prompt,
)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

class TestPromptBuilders:

    def test_extract_inquiries_prompt(self):
        agent_input = {
            "userData": {"pptId": "158948", "planId": "580", "companyName": "StarWars Inc."},
            "ticketData": {"emailSubject": "401k", "emailBody": "quiero retirar"},
            "forusbots": {"recordKeeper": "LT Trust"},
        }
        system, user = build_extract_inquiries_prompt(agent_input)
        assert "related_inquiries" in system          # canonical spec loaded
        assert "158948" in user and "LT Trust" in user  # input injected
        assert "JSON array" in user
        # F3 (eval 2026-06-22): security/account-access blocker is always its own inquiry
        assert "account_access" in system
        assert "security or account-access blocker" in system.lower()

    def test_kb_question_synthesis_prompt(self):
        system, user = build_kb_question_synthesis_prompt(
            {"ticketData": {"emailSubject": "401k", "emailBody": "I wanna cashout"}}
        )
        # critical rules preserved in the canonical spec
        assert "Participant Advisory - Form Submission" in system
        assert "insufficient_inquiry" in system
        assert "I wanna cashout" in user

    def test_forusbots_field_map_prompt_includes_reconciled_rules(self):
        system, user = build_forusbots_field_map_prompt(
            [{"field": "vested_balance", "required": True}], current_year=2026
        )
        # reconciliation from Module Builder V2 landed in the packaged spec
        assert "Rule 10: Predicate Decomposition" in system
        assert "Employer Match Vested Balance" in system
        # runtime addendum: plan catalog + prohibitions + year mechanism
        assert "Rule 11" in system and "Rule 14" in system
        assert "plan_design" in system
        assert "vested_balance" in user
        # runtime year injection (never guess the year)
        assert "CURRENT YEAR: 2026" in user
        assert "years:2026" in user

    def test_forusbots_field_map_prompt_defaults_to_utc_year(self):
        from datetime import datetime, timezone
        _, user = build_forusbots_field_map_prompt([{"field": "payroll"}])
        assert f"CURRENT YEAR: {datetime.now(timezone.utc).year}" in user

    def test_gr_body_build_prompt(self):
        agent_input = [{
            "pptDataModules": {"census": {"First Name": "Justin"}},
            "caseData": {"userData": {"pptId": "158948"}},
        }]
        system, user = build_gr_body_build_prompt(agent_input)
        assert "incoming_rollover" in system          # rollover-direction logic present
        # new optional input sections documented
        assert "planDataModules" in system
        assert "dataCollection" in system
        assert "data_collection_notes" in system
        assert "ticketExtractedFields" in system
        # real flat payroll shape (no n8n-era static/years repackaging)
        assert "payroll.static" not in system
        assert "Justin" in user

    def test_ticket_field_extract_prompt(self):
        system, user = build_ticket_field_extract_prompt(
            [{"field": "hardship_reason", "description": "why funds are needed",
              "why_needed": "IRS criteria", "required": True}],
            {"emailSubject": "hardship", "emailBody": "I need $5,000 for medical bills"},
        )
        # anti-hallucination contract
        assert "evidence" in system
        assert "not_found" in system
        assert "When in doubt" in system
        assert "Never use outside knowledge" in system
        assert "hardship_reason" in user
        assert "medical bills" in user

    def test_required_data_prompt_emits_both_tiers(self):
        # nice-to-have flows end-to-end: the RD prompt must instruct extraction
        # of BOTH tiers with the right required flags
        assert "Nice to Have" in SYSTEM_PROMPT_REQUIRED_DATA
        assert "required: false" in SYSTEM_PROMPT_REQUIRED_DATA
        assert '"required": false' in SYSTEM_PROMPT_REQUIRED_DATA


# ---------------------------------------------------------------------------
# Defensive JSON parsing
# ---------------------------------------------------------------------------

class TestJsonParsing:

    def test_object_plain(self):
        assert parse_json_object('{"question": "x"}') == {"question": "x"}

    def test_object_fenced(self):
        assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_with_prose_prefix(self):
        assert parse_json_object('Here you go: {"a": 1} thanks') == {"a": 1}

    def test_object_rejects_array_and_garbage(self):
        assert parse_json_object('[1, 2]') is None
        assert parse_json_object('not json') is None
        assert parse_json_object('') is None
        assert parse_json_object(None) is None

    def test_array_plain(self):
        assert parse_json_array('[{"inquiry": "x"}]') == [{"inquiry": "x"}]

    def test_array_fenced(self):
        assert parse_json_array('```\n[1, 2, 3]\n```') == [1, 2, 3]

    def test_array_wraps_bare_object(self):
        # an agent that should emit a one-element array but emits the bare object
        assert parse_json_array('{"inquiry": "x"}') == [{"inquiry": "x"}]

    def test_array_garbage(self):
        assert parse_json_array('nope') is None
        assert parse_json_array(None) is None


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------

class TestRoutingTableHasTicketAgents:

    def _settings(self):
        return SimpleNamespace(
            LLM_ROUTE_DECOMPOSE="gpt-5.5",
            LLM_ROUTE_REQUIRED_DATA="gpt-5.5",
            LLM_ROUTE_GR_OUTCOME="gpt-5.5",
            LLM_ROUTE_GR_RESPONSE="gpt-5.5",
            LLM_ROUTE_KNOWLEDGE="gpt-5.5",
            LLM_ROUTE_CLASSIFY="gemini-2.5-flash",
            LLM_ROUTE_EXTRACT_INQUIRIES="gpt-5.5",
            LLM_ROUTE_KB_QUESTION_SYNTHESIS="gpt-5.5",
            LLM_ROUTE_FORUSBOTS_FIELD_MAP="gpt-5.5",
            LLM_ROUTE_GR_BODY_BUILD="gpt-5.5",
            LLM_ROUTE_TICKET_FIELD_EXTRACT="gpt-5.5",
        )

    def test_new_routes_registered(self):
        routes = build_routes_from_settings(self._settings())
        for task in (
            "extract_inquiries", "kb_question_synthesis",
            "forusbots_field_map", "gr_body_build", "ticket_field_extract",
        ):
            assert task in routes, f"missing route: {task}"
            assert routes[task].primary is not None
