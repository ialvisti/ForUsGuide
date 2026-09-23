"""Non-Roth after-tax balance has no authoritative source; pin the fail-closed contract.

No participant module published by ForUsBots carries a non-Roth after-tax balance.
The `savings_rate` module enumerates Account / Employee Deferral / Roth Deferral /
Rollover / Employer Match / Employer Match Vested / Loan balances and nothing else,
so `after_tax_balance` (like `vested_balance`) is deliberately reported unknown.

These regressions pin the three ways that "unknown" has been observed to erode:
subtraction from the account total, an unmapped upstream label minting an
unprovenanced participant fact, and the PLAN-level `support_aftertax` feature flag
being read as participant money. Fixtures are synthetic and sanitized.
"""
import pytest

from data_pipeline.gr_payload_builder import build_collected_data
from data_pipeline.prompts import _format_collected_data

# Every source balance the scrape does publish, summing to less than the account
# total. The remainder is NOT attributable to after-tax funds.
FULL_COMPLEMENT = {
    "Account Balance": "$10,000.00",
    "Account Balance As Of": "2026-09-01",
    "Employee Deferral Balance": "$4,000.00",
    "Roth Deferral Balance": "$3,000.00",
    "Rollover Balance": "$1,000.00",
    "Employer Match Balance": "$500.00",
    "Employer Match Vested Balance": "$500.00",
}


def sources(ppt_modules, plan_modules=None, **kwargs):
    return build_collected_data(ppt_modules, plan_modules, **kwargs)["internal_preflight_context"]["sources"]


def test_after_tax_is_not_reconstructed_from_the_unexplained_account_remainder():
    entry = sources({"savings_rate": FULL_COMPLEMENT})["after_tax_balance"]
    assert entry["status"] == "unknown"
    assert entry["value"] is None
    assert entry["source"] is None


def test_sibling_balances_summing_to_the_account_total_do_not_prove_zero_after_tax():
    """A fully explained total is still no evidence; absence is not zero."""
    exact = {**FULL_COMPLEMENT, "Employee Deferral Balance": "$5,500.00"}
    assert sources({"savings_rate": exact})["after_tax_balance"]["status"] == "unknown"


@pytest.mark.parametrize("roth", ["$0.00", "$3,000.00"])
def test_roth_balance_never_settles_the_non_roth_after_tax_question(roth):
    entry = sources({"savings_rate": {**FULL_COMPLEMENT, "Roth Deferral Balance": roth}})
    assert entry["after_tax_balance"]["status"] == "unknown"
    assert entry["roth_deferral_balance"]["status"] in {"known", "unknown"}


@pytest.mark.parametrize("label", ["After-Tax Balance", "After Tax Balance", "Vested Balance"])
def test_unmapped_upstream_label_cannot_mint_a_preflight_governed_balance(label):
    """snake_case() on an undeclared label collides with the reserved keys.

    Such a value carries no extraction diagnostics, so it can never be dated or
    attributed. It must not reach participant_data, where the prompt prints every
    key verbatim and would contradict the preflight's own "unknown".
    """
    data = build_collected_data({"savings_rate": {**FULL_COMPLEMENT, label: "$2,500.00"}}, None)
    key = label.lower().replace(" ", "_").replace("-", "_")
    assert key not in data["participant_data"]
    assert data["internal_preflight_context"]["sources"][key]["status"] == "unknown"
    rendered = _format_collected_data(data)
    assert "2,500" not in rendered and "2500" not in rendered


def test_declared_savings_labels_still_map_and_unrelated_new_labels_still_pass_through():
    """The reserved-key guard is narrow: it must not swallow the real contract."""
    data = build_collected_data({"savings_rate": {**FULL_COMPLEMENT, "Maxed out": "Yes"}}, None)
    participant = data["participant_data"]
    assert participant["roth_deferral_balance"] == "$3,000.00"
    assert participant["employer_match_vested_balance"] == "$500.00"
    assert participant["maxed_out"] == "Yes"
    assert data["internal_preflight_context"]["sources"]["roth_deferral_balance"]["value"] == 3000


def test_plan_support_aftertax_flag_is_not_a_participant_after_tax_balance():
    """`support_aftertax` says the PLAN permits the source, not that funds exist."""
    data = build_collected_data({"savings_rate": FULL_COMPLEMENT},
                                {"plan_design": {"support_aftertax": True}})
    assert data["plan_data"]["support_aftertax"] is True
    assert data["internal_preflight_context"]["sources"]["after_tax_balance"]["status"] == "unknown"


def test_after_tax_is_never_a_verified_own_participant_disclosure_fact():
    meta = {"extraction_diagnostics": {"modules": {
        "savings_rate": {"dataState": "ok", "observedAt": "2026-09-11T18:00:00Z"}}}}
    identity = {"identity_resolution_status": "matched", "identity_verified": True}
    data = build_collected_data({"savings_rate": {**FULL_COMPLEMENT, "After-Tax Balance": "$2,500.00"}},
                                {}, {}, participant_meta=meta, identity_context=identity)
    facts = data["internal_disclosure_context"]["facts"]
    assert "after_tax_balance" not in facts
    assert facts["roth_deferral_balance"]["value"] == 3000


def test_participant_claimed_after_tax_amount_is_not_promoted_to_a_preflight_source():
    """Ticket text is untrusted; it cannot substitute for the missing source."""
    data = build_collected_data({"savings_rate": FULL_COMPLEMENT}, None,
                                {"after_tax_balance": {"value": 2500, "evidence": "I have $2,500 after-tax"}})
    assert data["internal_preflight_context"]["sources"]["after_tax_balance"]["status"] == "unknown"
