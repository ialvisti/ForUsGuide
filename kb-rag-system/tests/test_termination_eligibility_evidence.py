"""Sanitized eligibility regressions for vested evidence.

A bare participant number is not vested evidence. A structured savings_rate
Account Balance source is the total vested balance under the 2026-09-28 user rule.
"""

import copy

import pytest

from data_pipeline.forusbots_catalog import normalize_scrape_result
from data_pipeline.gr_payload_builder import build_collected_data
from data_pipeline.rag_engine import RAGEngine


def collected(**participant):
    return {
        "participant_data": {
            "employment_status": "Terminated", "termination_date": "2026-07-01",
            **participant,
        },
        "plan_data": {"blackout_period": False},
    }


@pytest.mark.parametrize("field", ["account_balance", "balance"])
def test_total_balance_cannot_rescue_personalized_eligibility(field):
    engine = RAGEngine.__new__(RAGEngine)
    response = {
        "outcome": "blocked_missing_data", "data_gaps": [],
        "questions_to_ask": ["Please provide your full name."],
    }
    fixed, info = engine._apply_informational_outcome_policy(
        response,
        {"inquiry_intent": "informational_options", "primary_action": "termination_rollover"},
        collected(**{field: 20000}),
    )
    assert fixed["outcome"] == "blocked_missing_data"
    assert info["core_eligibility_status"]["vested_balance"] is None
    assert "vested balance" in info["core_eligibility_status"]["core_eligibility_missing"]


@pytest.mark.parametrize("value", [None, "", "--", False, True, float("nan"), float("inf"),
                                   "unavailable as of 2026-08-01", "123abc", "1,23"])
def test_unverified_or_invalid_vested_balance_is_not_eligibility(value):
    engine = RAGEngine.__new__(RAGEngine)
    status = engine._termination_distribution_core_eligibility_status(
        collected(total_vested_balance=value, account_balance=20000)
    )
    assert status["supported"] is False
    assert status["vested_balance"] is None
    assert "vested balance" in status["core_eligibility_missing"]


@pytest.mark.parametrize("field", ["total_vested_balance", "vested_balance"])
def test_explicit_vested_field_remains_supported(field):
    engine = RAGEngine.__new__(RAGEngine)
    status = engine._termination_distribution_core_eligibility_status(collected(**{field: "$12,000"}))
    assert status["supported"] is True
    assert status["vested_balance"] == 12000


@pytest.mark.parametrize("status", ["unknown", "error", "empty"])
def test_structured_vested_unavailability_overrides_legacy_value(status):
    engine = RAGEngine.__new__(RAGEngine)
    data = collected(total_vested_balance=20000)
    data["internal_preflight_context"] = {"sources": {"vested_balance": {"status": status, "value": None}}}
    actual = engine._termination_distribution_core_eligibility_status(data)
    assert actual["supported"] is False
    assert actual["vested_balance"] is None


def test_structured_known_vested_evidence_reaches_eligibility():
    engine = RAGEngine.__new__(RAGEngine)
    data = collected(account_balance=20000)
    data["internal_preflight_context"] = {"sources": {"vested_balance": {
        "status": "known", "value": 15000, "source": "verified_statement", "as_of": "2026-08-01",
    }}}
    actual = engine._termination_distribution_core_eligibility_status(data)
    assert actual["supported"] is True
    assert actual["vested_balance"] == 15000


@pytest.mark.parametrize("value", [0, -10])
def test_confirmed_nonpositive_vested_is_distinct_from_unknown(value):
    engine = RAGEngine.__new__(RAGEngine)
    actual = engine._termination_distribution_core_eligibility_status(collected(total_vested_balance=value))
    assert actual["supported"] is False
    assert actual["vested_balance"] == value
    assert "vested balance" not in actual["core_eligibility_missing"]
    assert actual["blocking_conditions"]


@pytest.mark.parametrize("question", [
    {"question": "What is your current Total Vested Balance?", "why": "Only vested funds can be rolled over by wire."},
    "What vested balance is available for this rollover?",
    "Confirm employment status before selecting the distribution type.",
])
def test_core_eligibility_question_is_not_downgraded_by_execution_words(question):
    engine = RAGEngine.__new__(RAGEngine)
    actual = engine._classify_missing_data([question])
    assert len(actual["core_eligibility_missing"]) == 1
    assert actual["execution_details_missing"] == []


def test_identity_lookup_reason_does_not_invent_a_missing_eligibility_fact():
    engine = RAGEngine.__new__(RAGEngine)
    actual = engine._classify_missing_data([{
        "question": "What is your full name?",
        "why": "To look up the employment status and vested balance already on file.",
    }])
    assert actual["identity_lookup_missing"] == ["What is your full name?"]
    assert actual["core_eligibility_missing"] == []


def _chain(account, *, diagnostics=None, ticket=None):
    savings = {} if account == "absent" else {"Account Balance": account}
    payload = {"data": {
        "census": {"Eligibility Status": "Terminated", "Termination Date": "2026-07-01"},
        "savings_rate": savings,
    }}
    if diagnostics is not None:
        payload["extractionDiagnostics"] = diagnostics
    original = copy.deepcopy(payload)
    flat, meta = normalize_scrape_result(payload)
    assert payload == original
    return build_collected_data(flat, None, ticket, participant_meta=meta)


def test_normalized_savings_account_balance_reaches_core_eligibility_as_vested():
    collected = _chain("$12,500.00", diagnostics={"schemaVersion": 1, "modules": {"savings_rate": {
        "dataState": "ok", "observedAt": "2026-09-12T15:04:00Z",
        "fields": {"Account Balance": {"dataState": "ok", "sourceAsOf": "2026-08-15"}},
    }}})
    sources = collected["internal_preflight_context"]["sources"]
    assert sources["vested_balance"] == sources["account_balance"]
    assert sources["vested_balance"] is not sources["account_balance"]
    assert sources["vested_balance"]["value"] == 12500.0
    assert sources["vested_balance"]["observed_at"] == "2026-09-12T15:04:00Z"
    status = RAGEngine.__new__(RAGEngine)._termination_distribution_core_eligibility_status(collected)
    assert status["vested_balance"] == 12500.0
    assert "vested balance" not in status["core_eligibility_missing"]
    assert status["supported"] is False


def test_extraction_error_does_not_let_a_bare_participant_number_satisfy_vested_eligibility():
    collected = _chain("$12,500.00", diagnostics={"schemaVersion": 1, "modules": {"savings_rate": {
        "dataState": "ok", "observedAt": "2026-09-12T15:04:00Z",
        "fields": {"Account Balance": {"dataState": "parse_error"}},
    }}}, ticket={"total_vested_balance": {"value": 12500, "evidence": "synthetic ticket claim"}})
    assert collected["internal_preflight_context"]["sources"]["vested_balance"]["status"] == "error"
    status = RAGEngine.__new__(RAGEngine)._termination_distribution_core_eligibility_status(collected)
    assert status["vested_balance"] is None
    assert "vested balance" in status["core_eligibility_missing"]
