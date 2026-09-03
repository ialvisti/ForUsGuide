"""Sanitized acceptance replays for the approved 3★/4★ remediation batch."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from data_pipeline.rag_engine import RAGEngine


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "rag-testing" / "pa_three_four_star_cases.json"
ARTICLE_PATH = (
    ROOT.parent
    / "PA"
    / "Distributions"
    / "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
)
APPROVED_IDS = {
    "TKT-906357",
    "TKT-905927",
    "TKT-905906",
    "TKT-909636",
    "TKT-909733",
    "TKT-910966",
    "TKT-904783",
    "TKT-905935",
    "TKT-905909",
}


def _payload() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _article_rules() -> dict[str, str]:
    article = json.loads(ARTICLE_PATH.read_text(encoding="utf-8"))
    return {
        row["category"]: " ".join(row["rules"]).lower()
        for row in article["details"]["business_rules"]
    }


def _polluted_answer() -> dict:
    """Representative over-broad model output, intentionally containing regressions."""
    return {
        "outcome": "can_proceed",
        "outcome_reason": "Eligibility was confirmed.",
        "response_to_participant": {
            "opening": "You can continue with the applicable distribution path.",
            "key_points": [
                "ACH is available for every request.",
                "20% withholding applies to every request.",
                "Balances of $75 or less use fee-out.",
                "This is a known issue.",
                "If you have multiple 401(k) plans, select one.",
                "Cash distributions are taxable income.",
                "Overnight check delivery is available.",
            ],
            "steps": [
                {"step_number": number, "action": f"Generic step {number}", "detail": None}
                for number in range(1, 10)
            ],
            "warnings": [],
        },
        "questions_to_ask": [],
        "escalation": {"needed": False, "reason": None},
        "guardrails_applied": [],
        "data_gaps": [],
        "coverage_gaps": [],
    }


def _rendered(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def test_fixture_is_exactly_the_approved_sanitized_scope():
    payload = _payload()
    cases = payload["cases"]
    assert payload["scope"]["incoming_rollovers_excluded"] is True
    assert payload["scope"]["plan_notes_reference_ticket_excluded"] == "TKT-910971"
    assert {case["ticket_id"] for case in cases} == APPROVED_IDS
    assert {case["rating"] for case in cases} <= {3, 4}
    assert len(cases) == len(APPROVED_IDS)

    serialized = json.dumps(cases, ensure_ascii=False).lower()
    assert "incoming rollover" not in serialized
    assert "tkt-910971" not in serialized
    assert "@" not in serialized
    for pii_key in ("participant_name", "participant_email", "account_number"):
        assert pii_key not in serialized


@pytest.mark.parametrize("case", _payload()["cases"], ids=lambda row: row["ticket_id"])
def test_sanitized_ticket_replay(case: dict):
    engine = object.__new__(RAGEngine)
    collected = {
        "participant_data": {
            "employment_status": "Terminated",
            "termination_date": "2026-01-15",
            "account_balance": 12000,
        },
        "plan_data": {"record_keeper": "LT Trust"},
    }
    signals = engine._infer_retrieval_signals(
        case["sanitized_inquiry"], case["topic"], collected
    )
    for key, expected in case["expected_signals"].items():
        assert signals[key] is expected, f"{case['ticket_id']}: signal {key}"

    fixed, info = RAGEngine._apply_termination_response_policy(
        _polluted_answer(),
        {"primary_action": case["primary_action"], "signals": signals},
    )
    assert info["applied"] is True
    assert fixed["outcome"] == case["expected_outcome"]
    assert len(fixed["response_to_participant"]["steps"]) <= case["max_steps"]
    if "expected_questions" in case:
        assert len(fixed["questions_to_ask"]) == case["expected_questions"]

    last_step_required = case.get("last_step_must_include", [])
    if last_step_required:
        steps = fixed["response_to_participant"]["steps"]
        assert steps, f"{case['ticket_id']}: expected a final fallback step"
        last_step = _rendered(steps[-1])
        for required in last_step_required:
            assert required.lower() in last_step, (
                f"{case['ticket_id']}: final step missing {required}"
            )

    rendered = _rendered(fixed)
    for required in case["must_include"]:
        assert required.lower() in rendered, f"{case['ticket_id']}: missing {required}"
    for forbidden_pattern in case["must_exclude"]:
        assert re.search(forbidden_pattern.lower(), rendered) is None, (
            f"{case['ticket_id']}: retained {forbidden_pattern}"
        )

    category = case.get("knowledge_category")
    if category is not None:
        knowledge = _article_rules()[category]
        for required in case["required_knowledge_phrases"]:
            assert required.lower() in knowledge, (
                f"{case['ticket_id']}: KB category {category} missing {required}"
            )


def test_tkt_909636_historical_state_stays_blocked_until_date_confirmation():
    case = next(
        row for row in _payload()["cases"] if row["ticket_id"] == "TKT-909636"
    )
    engine = object.__new__(RAGEngine)
    collected = {
        "participant_data": {"employment_status": "Active"},
        "plan_data": {"record_keeper": "LT Trust"},
    }
    signals = engine._infer_retrieval_signals(
        case["sanitized_inquiry"], case["topic"], collected
    )
    parsed = {
        "outcome": "blocked_missing_data",
        "outcome_reason": "The record still shows active employment.",
        "response_to_participant": {
            "opening": "We need to confirm the separation date.",
            "key_points": [],
            "steps": [{
                "step_number": 1,
                "action": "Use the cash-out form now.",
                "detail": RAGEngine._TERMINATION_FORM_URL,
            }],
            "warnings": [],
        },
        "questions_to_ask": [{
            "question": "What was your last day of employment?",
            "why": "The record still shows active employment.",
        }],
        "data_gaps": ["Termination date"],
    }

    fixed, _ = RAGEngine._apply_termination_response_policy(
        parsed,
        {"primary_action": case["primary_action"], "signals": signals},
    )

    assert signals["separation_conflicts_active"] is True
    assert fixed["outcome"] == case["historical_expected_outcome"]
    assert len(fixed["questions_to_ask"]) == case["historical_expected_questions"]
    assert (
        len(fixed["response_to_participant"]["steps"])
        <= case["historical_max_steps"]
    )
    rendered = _rendered(fixed)
    for pattern in case["historical_must_exclude"]:
        assert re.search(pattern.lower(), rendered) is None
    questions = _rendered({"questions": fixed["questions_to_ask"]})
    for pattern in case["historical_question_must_match"]:
        assert re.search(pattern.lower(), questions) is not None
