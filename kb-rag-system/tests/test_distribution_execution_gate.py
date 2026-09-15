"""Sanitized model-output reproductions of premature submission guidance."""

import copy
import json
import re

import pytest

from data_pipeline.rag_engine import RAGEngine


def sample(outcome="blocked_missing_data", rollover=True):
    form = RAGEngine._TERMINATION_FORM_URL
    result = {
        "outcome": outcome,
        "outcome_reason": "Current vested balance needs internal verification.",
        "response_to_participant": {
            "opening": "You can start the distribution in the portal.",
            "key_points": [
                f"1. Process: Submit through the portal or use [the form]({form}).",
                "2. Sources: Our team needs to verify vested and non-Roth after-tax amounts.",
                "3. Delivery: Check or wire are available.",
                "4. Fees: The request fee is $75; each separate wire costs $35.",
            ],
            "steps": [
                {"step_number": 1, "action": "Review the account source and vesting information."},
                {"step_number": 2, "action": "Review and submit the request."},
                {"step_number": 3, "action": "After verification, use the electronic form.", "detail": form},
            ],
            "warnings": ["An outstanding loan offset may have tax consequences."],
        },
        "questions_to_ask": [],
        "data_gaps": ["Current Total Vested Balance"],
        "escalation": {"needed": True, "reason": "Our team must verify vested balance."},
    }
    profile = {
        "primary_action": "termination_rollover" if rollover else "termination_distribution",
        "inquiry_intent": "transactional_submission",
        "signals": {"pure_rollover": rollover, "termination_distribution": True,
                    "employment_state": "terminated", "termination_date_present": True,
                    "procedure_requested": True},
    }
    data = {
        "internal_response_context": {"requested_questions": [
            "What is the process and paperwork?", "What are my sources?",
            "Can delivery be by check or wire?", "What are the fees?",
        ]},
        "internal_preflight_context": {
            "loans": {"status": "known", "outstanding_status": "positive"},
            "crypto": {"holdings": {"status": "unknown"}},
            "sources": {"vested_balance": {"status": "unknown"}},
        },
    }
    return result, profile, data


@pytest.mark.parametrize("outcome", ["blocked_missing_data", "blocked_not_eligible", "ambiguous_plan_rules"])
@pytest.mark.parametrize("rollover", [True, False])
def test_blocked_distribution_has_no_submission_steps_or_live_form(outcome, rollover):
    response, profile, data = sample(outcome, rollover)
    original = copy.deepcopy(response)
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile, data)
    participant = fixed["response_to_participant"]
    assert participant["steps"] == []
    assert "rightsignature" not in json.dumps(participant).lower()
    assert "You can start" not in participant["opening"]
    assert fixed["outcome"] == outcome
    assert fixed["data_gaps"] == original["data_gaps"]
    assert fixed["escalation"] == original["escalation"]
    assert response == original, "Do not mutate the original model response"


def test_blocked_process_answer_keeps_question_order_and_supported_facts():
    response, profile, data = sample()
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile, data)
    participant = fixed["response_to_participant"]
    points = participant["key_points"]
    assert [re.match(r"^(\d+)\.", p).group(1) for p in points[:4]] == ["1", "2", "3", "4"]
    assert "verify" in points[0].lower()
    assert "Submit through" not in points[0]
    assert "non-Roth after-tax" in points[1]
    assert "check or wire" in points[2]
    assert "$75" in points[3] and "$35" in points[3]
    assert any("loan" in item.lower() for item in participant["warnings"])
    assert fixed["questions_to_ask"] == []


def test_eligible_distribution_keeps_portal_and_fallback_form():
    response, profile, data = sample("can_proceed")
    response["data_gaps"] = []
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile, data)
    participant = fixed["response_to_participant"]
    assert "Loans & Distributions" in participant["steps"][0]["action"]
    assert participant["steps"][-1]["detail"] == RAGEngine._TERMINATION_FORM_URL


def test_specific_verification_reason_and_tax_document_are_retained():
    response, profile, data = sample()
    response["response_to_participant"]["opening"] = "Our team needs to verify your total vested balance before confirming eligibility."
    response["response_to_participant"]["warnings"].append("Form 1099-R reports a completed distribution.")
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile, data)
    assert fixed["response_to_participant"]["opening"] == response["response_to_participant"]["opening"]
    assert "Form 1099-R reports a completed distribution." in fixed["response_to_participant"]["warnings"]


@pytest.mark.parametrize("action", ["incoming_rollover", "account_access", "loan_request"])
def test_distribution_gate_does_not_change_other_actions(action):
    response, profile, data = sample()
    profile["primary_action"] = action
    fixed, info = RAGEngine._apply_termination_response_policy(response, profile, data)
    assert fixed == response
    assert info["applied"] is False
