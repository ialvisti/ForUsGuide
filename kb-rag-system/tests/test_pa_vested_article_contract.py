"""The affected PA articles must not relabel an account total as vested funds."""

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
def test_required_vested_balance_has_no_false_account_total_source(path):
    article = json.loads(path.read_text())
    required = article["details"]["required_data"]
    points = required["must_have"] + required["nice_to_have"]
    vested = next((x for x in points if x["data_point"] == "Total Vested Balance"), None)
    assert vested is not None
    assert "not" in vested["meaning"].lower() and "account balance" in vested["meaning"].lower()
    assert "not" in vested["source_note"].lower() and "extract" in vested["source_note"].lower()
    assert "total vested balance" in json.dumps(KBChunker().chunk_article(article)).lower()
    fallback = next(x for x in required["if_missing"] if x["missing_data_point"] == "Total Vested Balance")
    assert fallback["ask_participant"] is None
    assert "unknown" in fallback["agent_note"].lower()
    assert "internal" in fallback["agent_note"].lower()
    text = json.dumps(article).lower()
    assert "account balance field returned by forusbots represents the total vested balance" not in text
    assert "account balance (total vested balance)" not in text


def test_total_vested_request_collects_sources_without_inventing_an_extractor():
    mapped = map_slug({"field": "total_vested_balance"}, current_year=2026)
    assert ("savings_rate", "Account Balance") in mapped
    assert ("savings_rate", "Employer Match Vested Balance") in mapped
    assert all(field != "Total Vested Balance" for _, field in mapped)
