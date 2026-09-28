"""Total vested balance is savings_rate.Account Balance, with its extraction state.

The previous contract required articles to deny that equivalence. That
expectation changed because of the 2026-09-28 user rule, not because the
old assertion was an implementation failure.
"""

import json
from pathlib import Path

import pytest

from data_pipeline.chunking import KBChunker
from data_pipeline.forusbots_catalog import map_slug


PA = Path(__file__).resolve().parents[2] / "PA"
ARTICLES = [
    PA / "Distributions" / "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json",
    PA / "Distributions" / "LT: Can I Split My 401(k) Rollover Between Multiple Providers?.json",
    PA / "Distributions" / "Can I Take Money From My 401(k) While Employed? Your Options Explained.json",
    PA / "Loans" / "401(k) Loan Basics and Support Guide.json",
    PA / "Loans" / "LT: 401(k) Loan Complete Guide — Submission, Repayment & Support.json",
]


@pytest.mark.parametrize("path", ARTICLES, ids=lambda p: p.stem)
def test_required_vested_balance_uses_savings_account_balance_state(path):
    article = json.loads(path.read_text())
    required = article["details"]["required_data"]
    points = required["must_have"] + required["nice_to_have"]
    vested = next((x for x in points if x["data_point"] == "Total Vested Balance"), None)
    assert vested is not None
    meaning = vested["meaning"].lower()
    note = vested["source_note"].lower()
    assert "savings_rate.account balance" in meaning
    assert "total vested" in meaning
    assert "not a sum" in meaning
    assert "employer-match vested portion" in meaning
    assert "savings_rate.account balance" in note
    assert "absent or in error" in note and "unknown" in note
    assert "do not sum" in note
    assert "do not substitute employer match vested balance" in note
    assert "total vested balance" in json.dumps(KBChunker().chunk_article(article)).lower()
    fallback = next(x for x in required["if_missing"] if x["missing_data_point"] == "Total Vested Balance")
    assert fallback["ask_participant"] is None
    agent = fallback["agent_note"].lower()
    assert "unknown" in agent and "internal" in agent
    assert "savings_rate.account balance" in agent
    assert "do not sum" in agent
    assert "do not substitute employer match vested balance" in agent
    text = json.dumps(article).lower()
    # The superseded denials must stay gone. Phrasings that now state the
    # equivalence correctly are no longer forbidden: the two assertions that
    # banned them contradicted the 2026-09-28 rule this contract encodes.
    assert "not total vested" not in text
    assert "does not supply total vested balance" not in text
    assert "not the savings rate account balance" not in text


def test_vested_copy_rejects_false_source_semantics():
    """Output-state negative: the copy is the Account Balance row itself.

    It is never a sum of the component rows, never the employer-only figure,
    and an absent Account Balance is never backfilled from the components.
    """
    from data_pipeline.gr_payload_builder import _build_preflight_context

    participant = {
        "account_balance": 12500.0,
        "account_balance_as_of": "2026-08-15",
        "employee_deferral_balance": 9000.0,
        "roth_deferral_balance": 1000.0,
        "employer_match_balance": 4000.0,
        "employer_match_vested_balance": 0.0,
        "rollover_balance": 0.0,
    }
    sources = _build_preflight_context(participant, None)["sources"]
    vested = sources["vested_balance"]
    assert vested == sources["account_balance"]
    assert vested is not sources["account_balance"]
    assert vested["value"] == 12500.0
    assert vested["value"] != 14000.0, "vested must not be a sum of the source rows"
    assert vested["value"] != sources["employer_match_vested_balance"]["value"]
    assert sources["after_tax_balance"]["status"] == "unknown"

    without_total = {key: value for key, value in participant.items() if key != "account_balance"}
    absent = _build_preflight_context(without_total, None)["sources"]
    assert absent["vested_balance"]["status"] == "unknown"
    assert absent["vested_balance"]["value"] is None
    assert absent["employee_deferral_balance"]["value"] == 9000.0


def test_total_vested_request_collects_sources_without_inventing_an_extractor():
    mapped = map_slug({"field": "total_vested_balance"}, current_year=2026)
    assert ("savings_rate", "Account Balance") in mapped
    assert ("savings_rate", "Employer Match Vested Balance") in mapped
    assert all(field != "Total Vested Balance" for _, field in mapped)
