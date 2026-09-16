"""Sanitized reproduction of duplicate checks in a deployed PA draft.

The source sentence is from the 2026-09-16 canary for TKT-912180; its
amount is a simulation value, not a participant balance. No live calls.
"""

import copy

import pytest

from data_pipeline.rag_engine import RAGEngine


COMBINED_REVIEW = (
    "Loan and crypto review: Your account shows an outstanding $9,000 loan and "
    "crypto enrollment, so ForUsAll must verify loan payoff or treatment and "
    "whether crypto holdings exist before finalizing distribution handling."
)


def apply_review(point, *, warnings=None, loan_state="positive", crypto_state="unknown", extra_points=None):
    original = {
        "outcome": "blocked_missing_data",
        "outcome_reason": "Our team needs to verify the current vested balance.",
        "response_to_participant": {
            "opening": "Our team needs to verify the current vested balance.",
            "key_points": list(extra_points or []) + [point],
            "warnings": list(warnings or []),
            "steps": [],
        },
        "questions_to_ask": [],
        "data_gaps": ["Current Total Vested Balance"],
        "escalation": {"needed": True},
    }
    profile = {
        "primary_action": "termination_rollover",
        "signals": {"pure_rollover": True, "selected_delivery": "check",
                    "employment_state": "terminated", "termination_date_present": True},
    }
    data = {
        "internal_response_context": {"requested_questions": ["What needs verification?"]},
        "internal_preflight_context": {
            "loans": {"status": "known" if loan_state != "unknown" else "unknown",
                      "outstanding_status": loan_state},
            "crypto": {"holdings": {"status": crypto_state,
                                     "value": 100 if crypto_state == "known" else None}},
        },
    }
    before = copy.deepcopy(original)
    fixed, _ = RAGEngine._apply_termination_response_policy(original, profile, data)
    assert original == before
    assert fixed["outcome"] == before["outcome"]
    assert fixed["data_gaps"] == before["data_gaps"]
    assert fixed["escalation"] == before["escalation"]
    return fixed["response_to_participant"]


def test_deployed_loan_check_is_not_repeated_in_warnings():
    response = apply_review(COMBINED_REVIEW)
    assert COMBINED_REVIEW in response["key_points"]
    assert not any("loan" in warning.lower() for warning in response["warnings"])
    # This adds a distinct requirement: verify applicable crypto transfers,
    # not only the existence of holdings. It is not a duplicate.
    assert any("applicable transfer requirements" in w for w in response["warnings"])


def test_preserve_distinct_offset_tax_warning_and_supported_amount():
    warning = "An outstanding loan offset may have tax consequences."
    response = apply_review(COMBINED_REVIEW, warnings=[warning])
    assert warning in response["warnings"]
    assert sum("loan" in w.lower() for w in response["warnings"]) == 1
    assert "$9,000" in " ".join(response["key_points"])


def test_current_llm_paraphrase_keeps_tax_caveat_without_a_second_review():
    point = (
        "Because your records show an outstanding 401(k) loan, ForUsAll also "
        "needs to review loan treatment before execution; an unpaid loan can "
        "have repayment and tax consequences after separation."
    )
    response = apply_review(point)
    assert point in response["key_points"]
    assert not any("loan" in w.lower() for w in response["warnings"])


@pytest.mark.parametrize("point", [
    "An outstanding loan offset may have tax consequences.",
    "Contact Support to check your outstanding loan and crypto holdings.",
    "ForUsAll has already verified loan payoff and whether crypto holdings exist.",
    "ForUsAll does not need to verify loan payoff or whether crypto holdings exist.",
    "ForUsAll must verify your mailing address. You have an outstanding loan and crypto enrollment.",
    "ForUsAll must verify crypto transfer requirements. There is no outstanding loan.",
    "Your outstanding loan balance is $0. ForUsAll must verify loan treatment before distribution.",
    "Your outstanding loan has been paid off. ForUsAll must verify loan treatment before distribution.",
    "ForUsAll must verify loan treatment before distribution if you have an outstanding loan.",
])
def test_incomplete_or_different_statement_does_not_replace_required_checks(point):
    response = apply_review(point)
    assert any("An outstanding loan is recorded" in w for w in response["warnings"])
    assert any("Crypto positions have not been verified" in w for w in response["warnings"])


def test_unknown_loan_is_not_proven_by_an_assertion_of_a_positive_balance():
    response = apply_review(COMBINED_REVIEW, loan_state="unknown")
    assert any("Your loan status has not been verified" in w for w in response["warnings"])


def test_known_crypto_positions_keep_transfer_requirement_check():
    response = apply_review(COMBINED_REVIEW, crypto_state="known")
    assert any("Crypto positions are recorded" in w for w in response["warnings"])


def test_check_removed_by_response_cap_cannot_suppress_warning():
    response = apply_review(COMBINED_REVIEW, extra_points=[f"Information {i}." for i in range(12)])
    assert COMBINED_REVIEW not in response["key_points"]
    assert any("An outstanding loan is recorded" in w for w in response["warnings"])
    assert any("Crypto positions have not been verified" in w for w in response["warnings"])
