"""Canonical onboarding dates become blackout evidence for the termination gate.

A boolean is not invented from a blank template, an unread module, or participant text.
"""

import json
from datetime import date
from pathlib import Path

from data_pipeline.chunking import KBChunker
from data_pipeline.forusbots_catalog import PARTICIPANT_MODULES, PLAN_MODULES, map_slug, normalize_scrape_result
from data_pipeline.gr_payload_builder import build_collected_data
from data_pipeline.rag_engine import RAGEngine

EVAL = date(2026, 9, 28)


def _collect(onboarding=None, *, status="ok", diagnostics=None, ticket=None, plan_history=None):
    modules = [
        {"key": "census", "status": "ok", "data": {
            "Eligibility Status": "Terminated", "Termination Date": "2026-07-01",
        }},
        {"key": "savings_rate", "status": "ok", "data": {"Account Balance": "6044.85"}},
    ]
    if onboarding is not None:
        modules.append({"key": "onboarding", "status": status, "data": onboarding})
    payload = {"data": {"modules": modules}}
    if plan_history is not None:
        payload["data"]["planHistory"] = plan_history
    if diagnostics is not None:
        payload["extractionDiagnostics"] = diagnostics
    flat, meta = normalize_scrape_result(payload)
    participant = {key: value for key, value in flat.items() if key in PARTICIPANT_MODULES}
    plan = {key: value for key, value in flat.items() if key in PLAN_MODULES}
    return build_collected_data(
        participant, plan, ticket,
        participant_meta=meta, plan_meta=meta, blackout_evaluation_date=EVAL,
    )


def _status(collected):
    return RAGEngine.__new__(RAGEngine)._termination_distribution_core_eligibility_status(collected)


def _evidence(collected):
    return collected["internal_preflight_context"]["blackout"]


def test_catalog_blackout_slug_requests_both_onboarding_dates():
    assert map_slug({"field": "blackout_dates"}, current_year=2026) == [
        ("onboarding", "blackout_begins_date"),
        ("onboarding", "blackout_ends_date"),
    ]


def test_current_onboarding_range_blocks_without_dropping_other_facts():
    collected = _collect({
        "blackout_begins_date": "2026-09-01",
        "blackout_ends_date": "2026-10-01",
    })
    evidence = _evidence(collected)
    assert evidence["status"] == "active"
    assert evidence["evaluation_date"] == "2026-09-28"
    assert evidence["begins"] == "2026-09-01"
    assert evidence["ends"] == "2026-10-01"
    assert evidence["source"] == "plan.onboarding.blackout_begins_date"
    status = _status(collected)
    assert status["supported"] is False
    assert "blackout period is active" in status["blocking_conditions"]
    assert "blackout status" not in status["core_eligibility_missing"]
    assert status["employment_state"] == "terminated"
    assert status["vested_balance"] == 6044.85
    assert "termination date" not in status["core_eligibility_missing"]
    assert "vested balance" not in status["core_eligibility_missing"]


def test_range_edges_are_active():
    collected = _collect({
        "blackout_begins_date": "2026-09-28",
        "blackout_ends_date": "2026-09-28",
    })
    assert _evidence(collected)["status"] == "active"
    assert "blackout period is active" in _status(collected)["blocking_conditions"]


def test_past_and_future_ranges_are_not_active():
    past = _collect({"blackout_begins_date": "2020-01-01", "blackout_ends_date": "2020-01-31"})
    future = _collect({"blackout_begins_date": "2027-01-01", "blackout_ends_date": "2027-02-01"})
    for collected in (past, future):
        assert _evidence(collected)["status"] == "inactive"
        status = _status(collected)
        assert status["supported"] is True
        assert status["core_eligibility_missing"] == []
        assert status["blocking_conditions"] == []


def test_explicit_complete_empty_read_is_no_window():
    collected = _collect(
        {"blackout_begins_date": "", "blackout_ends_date": ""},
        diagnostics={"modules": {"onboarding": {
            "dataState": "ok",
            "completeness": {"complete": True, "truncated": False},
            "fields": {
                "blackout_begins_date": {"dataState": "empty"},
                "blackout_ends_date": {"dataState": "empty"},
            },
        }}},
    )
    evidence = _evidence(collected)
    assert evidence["status"] == "no_window"
    assert evidence["complete"] is True
    status = _status(collected)
    assert status["supported"] is True
    assert "blackout status" not in status["core_eligibility_missing"]
    assert "blackout period is active" not in status["blocking_conditions"]


def test_default_blank_template_stays_unknown():
    collected = _collect({"blackout_begins_date": "", "blackout_ends_date": ""})
    assert _evidence(collected)["status"] == "unknown"
    status = _status(collected)
    assert status["supported"] is False
    assert "blackout status" in status["core_eligibility_missing"]
    assert "blackout period is active" not in status["blocking_conditions"]


def test_missing_module_stays_absent_and_unknown_to_the_gate():
    collected = _collect(None)
    assert _evidence(collected)["status"] == "absent"
    status = _status(collected)
    assert "blackout status" in status["core_eligibility_missing"]
    assert status["vested_balance"] == 6044.85
    assert status["employment_state"] == "terminated"


def test_failed_incomplete_one_sided_malformed_and_inverted_stay_unknown():
    failed = _collect(
        {"blackout_begins_date": "2026-09-01", "blackout_ends_date": "2026-10-01"},
        status="error",
    )
    incomplete = _collect(
        {"blackout_begins_date": "2026-09-01", "blackout_ends_date": "2026-10-01"},
        diagnostics={"modules": {"onboarding": {
            "dataState": "ok",
            "completeness": {"complete": False, "truncated": True, "reason": "pagination"},
            "fields": {
                "blackout_begins_date": {"dataState": "ok"},
                "blackout_ends_date": {"dataState": "ok"},
            },
        }}},
    )
    one_sided = _collect({"blackout_begins_date": "2026-09-01", "blackout_ends_date": ""})
    malformed = _collect({"blackout_begins_date": "not-a-date", "blackout_ends_date": "2026-10-01"})
    inverted = _collect({"blackout_begins_date": "2026-10-01", "blackout_ends_date": "2026-09-01"})
    for collected in (failed, incomplete, one_sided, malformed, inverted):
        evidence = _evidence(collected)
        assert evidence["status"] == "unknown"
        assert evidence["review_required"] is True
        status = _status(collected)
        assert status["supported"] is False
        assert "blackout status" in status["core_eligibility_missing"]
        assert "blackout period is active" not in status["blocking_conditions"]
    assert "blackout_begins_date" not in failed.get("plan_data", {})


def test_plan_history_completeness_does_not_make_a_blank_onboarding_read():
    collected = _collect(
        {"blackout_begins_date": "", "blackout_ends_date": ""},
        plan_history={"extractionStatus": "ok", "completeness": {"complete": True}, "current": {}, "entries": []},
    )
    assert _evidence(collected)["status"] == "unknown"


def test_legacy_boolean_works_only_when_structured_evidence_is_absent():
    legacy = {
        "participant_data": {
            "employment_status": "Terminated", "termination_date": "2026-07-01",
            "total_vested_balance": 6044.85,
        },
        "plan_data": {"blackout_period": False},
    }
    assert _status(legacy)["supported"] is True
    failed = _collect(
        {"blackout_begins_date": "2026-09-01", "blackout_ends_date": "2026-10-01"},
        status="error",
    )
    failed.setdefault("plan_data", {})["blackout_period"] = False
    status = _status(failed)
    assert status["supported"] is False
    assert "blackout status" in status["core_eligibility_missing"]


def test_complete_read_without_field_diagnostics_is_not_a_closed_empty_read():
    """``no_window`` needs both fields declared empty, not just a complete module.

    Module completeness alone says the module was read, not that the extractor
    looked at these two labels. Without that statement the blanks are a default
    template, so the only safe answer is unknown.
    """
    collected = _collect(
        {"blackout_begins_date": "", "blackout_ends_date": ""},
        diagnostics={"modules": {"onboarding": {
            "dataState": "ok",
            "completeness": {"complete": True, "truncated": False},
        }}},
    )
    evidence = _evidence(collected)
    assert evidence["status"] == "unknown"
    assert evidence["review_required"] is True
    status = _status(collected)
    assert status["supported"] is False
    assert "blackout status" in status["core_eligibility_missing"]


def test_empty_field_diagnostic_contradicted_by_a_value_stays_unknown():
    """A declared-empty field carrying a date is a contradiction, not a window."""
    collected = _collect(
        {"blackout_begins_date": "2026-09-01", "blackout_ends_date": "2026-10-01"},
        diagnostics={"modules": {"onboarding": {
            "dataState": "ok",
            "completeness": {"complete": True},
            "fields": {
                "blackout_begins_date": {"dataState": "empty"},
                "blackout_ends_date": {"dataState": "ok"},
            },
        }}},
    )
    evidence = _evidence(collected)
    assert evidence["status"] == "unknown"
    assert evidence["review_required"] is True
    status = _status(collected)
    assert "blackout period is active" not in status["blocking_conditions"]
    assert "blackout status" in status["core_eligibility_missing"]


def test_structured_error_state_overrides_a_legacy_false_boolean():
    """A failed read in the diagnostics, not only in the legacy envelope status.

    The dates would otherwise evaluate to an active window, so the error state
    has to be what decides, and it decides unknown rather than a blocker.
    """
    collected = _collect(
        {"blackout_begins_date": "2026-09-01", "blackout_ends_date": "2026-10-01"},
        diagnostics={"modules": {"onboarding": {
            "dataState": "access_denied",
            "completeness": {"complete": True},
        }}},
    )
    evidence = _evidence(collected)
    assert evidence["status"] == "unknown"
    assert evidence["review_required"] is True
    collected.setdefault("plan_data", {})["blackout_period"] = False
    status = _status(collected)
    assert status["supported"] is False
    assert "blackout status" in status["core_eligibility_missing"]


def test_declared_missing_module_is_unknown_not_absent():
    """Silence leaves the boolean in charge; a declared missing read does not."""
    collected = _collect(None, diagnostics={"modules": {"onboarding": {"dataState": "missing"}}})
    assert _evidence(collected)["status"] == "unknown"
    collected.setdefault("plan_data", {})["blackout_period"] = False
    assert "blackout status" in _status(collected)["core_eligibility_missing"]


def test_participant_text_and_ticket_claim_are_not_blackout_authority():
    collected = _collect(None, ticket={
        "blackout_period": {"value": False, "evidence": "The participant said there is no blackout period."},
        "blackout_begins_date": {"value": "2020-01-01", "evidence": "model text"},
    })
    assert _evidence(collected)["status"] == "absent"
    assert "blackout status" in _status(collected)["core_eligibility_missing"]
    assert collected.get("plan_data", {}).get("blackout_period") is not False


def test_article_chunks_request_both_onboarding_blackout_dates():
    """The published article chunk contract must request both onboarding dates."""
    article_path = (
        Path(__file__).resolve().parents[2]
        / "PA" / "Distributions"
        / "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
    )
    article = json.loads(article_path.read_text())
    chunks = KBChunker().chunk_article(article)
    must = next(chunk for chunk in chunks if chunk["metadata"]["chunk_type"] == "required_data_must_have")
    missing = next(chunk for chunk in chunks if chunk["metadata"]["chunk_type"] == "required_data_if_missing")
    assert "Blackout dates|eligibility_confirmation" in must["metadata"]["must_have_blocking_intents"]
    assert "**Source:** plan_profile" in must["content"]
    assert "onboarding.blackout_begins_date" in must["content"]
    assert "onboarding.blackout_ends_date" in must["content"]
    blackout_missing = missing["content"].split("### Missing: Blackout dates", 1)[1].split("### Missing:", 1)[0]
    assert "**Ask participant:** None" in blackout_missing
    assert "not a reason to ask the participant" in blackout_missing
    assert "not false" in blackout_missing.casefold()
    presented = []
    for chunk in chunks:
        metadata = dict(chunk["metadata"])
        metadata["content"] = chunk["content"]
        presented.append({"id": chunk["id"], "metadata": metadata})
    fields, diagnostics = RAGEngine._required_data_from_chunks(presented)
    assert diagnostics["contract_status"] == "valid"
    plan_fields = {field["field"]: field for field in fields["plan_data"]}
    assert "blackout_dates" in plan_fields
    provenance = next(item for item in diagnostics["field_provenance"] if item["field"] == "blackout_dates")
    assert provenance["source"] == "plan_profile"
    assert map_slug({"field": "blackout_dates"}, current_year=2026) == [
        ("onboarding", "blackout_begins_date"),
        ("onboarding", "blackout_ends_date"),
    ]
