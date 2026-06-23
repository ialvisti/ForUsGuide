"""
Unit tests for the ForusBots catalog: catalogs, deterministic slug map,
validation gate, target split, and scrape-result normalization.

The normalization fixtures reproduce the REAL payloads confirmed in production
(participant + plan), plus the local-repo normalizer envelope.
"""

from __future__ import annotations

import re

from data_pipeline import prompts
from data_pipeline.forusbots_catalog import (
    FORBIDDEN_PARTICIPANT_MODULES,
    PARTICIPANT_MODULES,
    PLAN_MODULES,
    SLUG_MAP,
    build_modules,
    map_slug,
    merge_module_lists,
    normalize_scrape_result,
    split_modules_by_target,
    validate_modules,
)

YEAR = 2026


# ---------------------------------------------------------------------------
# Catalogs
# ---------------------------------------------------------------------------

class TestCatalogs:

    def test_participant_module_keys(self):
        assert set(PARTICIPANT_MODULES) == {
            "census", "savings_rate", "plan_details", "loans", "payroll", "mfa"
        }

    def test_plan_module_keys(self):
        assert set(PLAN_MODULES) == {
            "basic_info", "plan_design", "onboarding", "communications",
            "extra_settings", "feature_flags"
        }

    def test_forbidden_participant_modules(self):
        assert FORBIDDEN_PARTICIPANT_MODULES == {"communications", "documents"}

    def test_core_fields_present(self):
        assert "Termination Date" in PARTICIPANT_MODULES["census"]
        assert "Employer Match Vested Balance" in PARTICIPANT_MODULES["savings_rate"]
        assert "Vested Balance" not in PARTICIPANT_MODULES["savings_rate"]
        # observed-in-real-payload drift fields are present
        assert "Formula" in PARTICIPANT_MODULES["savings_rate"]
        assert "Employer Contribution Type" in PARTICIPANT_MODULES["plan_details"]
        assert "employer_contribution" in PLAN_MODULES["plan_design"]
        assert "blackout_begins_date" in PLAN_MODULES["onboarding"]

    def test_slug_map_targets_exist_in_catalogs(self):
        """Self-consistency: every SLUG_MAP target is a cataloged field or a
        valid payroll token/sentinel."""
        token_re = re.compile(r"^years:(all|CURRENT_YEAR|\d{4}(,\d{4})*)$")
        for slug, entries in SLUG_MAP.items():
            for module, fld in entries:
                catalog = PARTICIPANT_MODULES.get(module) or PLAN_MODULES.get(module)
                assert catalog is not None, f"{slug}: unknown module {module}"
                if module == "payroll" and token_re.match(fld):
                    continue
                assert fld in catalog, f"{slug}: {module}/{fld} not in catalog"

    def test_canonical_kb_slugs_are_covered(self):
        """Every canonical slug the KB's required-data prompt advertises must
        resolve deterministically."""
        canonical = [
            "first_name", "last_name", "participant_name", "participant_status",
            "birth_date", "hire_date", "rehire_date", "termination_date",
            "primary_email", "home_email", "phone", "address",
            "account_balance", "vested_balance", "employer_match_vested_balance",
            "roth_deferral_balance", "rollover_balance", "record_keeper",
            "plan_enrollment_type", "ytd_employee_contributions",
            "ytd_employer_contributions", "plan_type", "plan_status",
            "force_out_limit", "maximum_number_of_loans", "auto_enrollment_rate",
            "loan_history", "loan_account_balance", "payroll_frequency",
            "last_payroll_date", "payroll_history", "mfa_status",
        ]
        for slug in canonical:
            assert slug in SLUG_MAP, f"canonical slug not in SLUG_MAP: {slug}"
            # and the prompt really advertises it (guards against prompt drift)
            assert f"`{slug}`" in prompts.SYSTEM_PROMPT_REQUIRED_DATA


# ---------------------------------------------------------------------------
# map_slug
# ---------------------------------------------------------------------------

class TestMapSlug:

    def test_simple_and_case_tolerance(self):
        assert map_slug({"field": "termination_date"}, current_year=YEAR) == \
            [("census", "Termination Date")]
        assert map_slug({"field": " Termination Date "}, current_year=YEAR) == \
            [("census", "Termination Date")]

    def test_composites(self):
        assert len(map_slug({"field": "participant_name"}, current_year=YEAR)) == 2
        assert len(map_slug({"field": "address"}, current_year=YEAR)) == 5

    def test_vested_balance_default(self):
        assert map_slug({"field": "vested_balance"}, current_year=YEAR) == \
            [("savings_rate", "Account Balance")]

    def test_vested_balance_employer_match_hook(self):
        out = map_slug({"field": "vested_balance",
                        "description": "vested portion of employer match contributions"},
                       current_year=YEAR)
        assert out == [("savings_rate", "Employer Match Vested Balance")]

    def test_payroll_default_current_year(self):
        assert map_slug({"field": "payroll"}, current_year=2026) == \
            [("payroll", "years:2026")]

    def test_payroll_historical(self):
        out = map_slug({"field": "payroll", "description": "all historical payroll data"},
                       current_year=YEAR)
        assert out == [("payroll", "years:all")]

    def test_payroll_explicit_years(self):
        out = map_slug({"field": "payroll_data",
                        "description": "contributions made during 2024"},
                       current_year=YEAR)
        assert out == [("payroll", "years:2024")]

    def test_latest_payroll_never_years_token(self):
        for slug in ("last_payroll_date", "latest_payroll", "most_recent_payroll"):
            out = map_slug({"field": slug}, current_year=YEAR)
            assert out == [("payroll", "Latest Payroll")]
            assert not any("years:" in f for _, f in out)

    def test_category_plan_data_override(self):
        # participant-side default
        assert map_slug({"field": "enrollment_type"}, current_year=YEAR) == \
            [("savings_rate", "Plan enrollment type")]
        # plan_data category prefers the plan-side target
        assert map_slug({"field": "enrollment_type", "category": "plan_data"},
                        current_year=YEAR) == [("plan_design", "enrollment_type")]

    def test_plan_slugs(self):
        assert map_slug({"field": "blackout_dates"}, current_year=YEAR) == \
            [("onboarding", "blackout_begins_date"), ("onboarding", "blackout_ends_date")]
        assert map_slug({"field": "ein"}, current_year=YEAR) == [("basic_info", "EIN")]
        assert map_slug({"field": "default_savings_rate"}, current_year=YEAR) == \
            [("plan_design", "default_savings_rate")]

    def test_unknown_slug_returns_none(self):
        assert map_slug({"field": "hardship_reason"}, current_year=YEAR) is None
        assert map_slug({"field": "whether_participant_received_check"},
                        current_year=YEAR) is None
        assert map_slug({"field": ""}, current_year=YEAR) is None

    def test_article_alignment_aliases(self):
        """Aliases added to close the article↔catalog alignment gaps."""
        assert map_slug({"field": "participants_name"}, current_year=YEAR) == \
            [("census", "First Name"), ("census", "Last Name")]
        assert map_slug({"field": "participants_email"}, current_year=YEAR) == \
            [("census", "Primary Email")]
        assert map_slug({"field": "employment_separation_date"}, current_year=YEAR) == \
            [("census", "Termination Date")]
        assert len(map_slug({"field": "mailing_address_on_file"}, current_year=YEAR)) == 5
        # age questions resolve from Birth Date
        assert map_slug({"field": "participant_age_relative_to_595"}, current_year=YEAR) == \
            [("census", "Birth Date")]
        # loan-history-derived questions
        assert map_slug({"field": "outstanding_401k_loan_status"}, current_year=YEAR) == \
            [("loans", "Loan History")]
        assert map_slug(
            {"field": "previous_highest_outstanding_loan_balance_during_the_last_12_months"},
            current_year=YEAR) == [("loans", "Loan History")]
        assert map_slug({"field": "plan_termination_status"}, current_year=YEAR) == \
            [("plan_details", "Status")]
        # employer-match eligibility → plan scrape (extra_settings)
        out = map_slug({"field": "employer_match_eligibility_for_hardship_withdrawal"},
                       current_year=YEAR)
        assert {m for m, _f in out} == {"extra_settings"}

    def test_request_provided_slugs(self):
        from data_pipeline.forusbots_catalog import is_request_provided
        assert is_request_provided({"field": "plan_id"})
        assert is_request_provided({"field": "Participant Plan"})
        assert is_request_provided({"field": "company_name"})
        assert not is_request_provided({"field": "termination_date"})
        assert not is_request_provided({"field": ""})


# ---------------------------------------------------------------------------
# build / merge / validate / split
# ---------------------------------------------------------------------------

class TestBuildMergeValidate:

    def test_build_modules_dedupes_and_orders(self):
        mods = build_modules([
            ("payroll", "Latest Payroll"), ("census", "First Name"),
            ("census", "First Name"), ("savings_rate", "Account Balance"),
        ])
        assert [m["key"] for m in mods] == ["census", "savings_rate", "payroll"]
        assert mods[0]["fields"] == ["First Name"]

    def test_merge_module_lists(self):
        merged = merge_module_lists(
            [{"key": "census", "fields": ["First Name"]}],
            [{"key": "census", "fields": ["First Name", "Last Name"]},
             {"key": "mfa", "fields": ["MFA Status"]}],
        )
        assert merged == [
            {"key": "census", "fields": ["First Name", "Last Name"]},
            {"key": "mfa", "fields": ["MFA Status"]},
        ]

    def test_validate_case_fixup(self):
        res = validate_modules([{"key": "Census", "fields": ["termination date"]}])
        assert res.modules == [{"key": "census", "fields": ["Termination Date"]}]
        assert not res.rejected

    def test_validate_rejects_unknown_module_and_documents(self):
        res = validate_modules([
            {"key": "user_profile", "fields": ["x"]},
            {"key": "documents", "fields": ["y"]},
        ])
        assert res.modules == []
        reasons = {r["module"]: r["reason"] for r in res.rejected}
        assert reasons["user_profile"] == "unknown_module"
        assert reasons["documents"] == "no_structured_extractor"

    def test_validate_communications_participant_vs_plan(self):
        # participant-looking fields → rejected
        res = validate_modules([{"key": "communications", "fields": ["Message History"]}])
        assert res.modules == []
        assert res.rejected[0]["reason"] == "no_structured_extractor"
        # plan-catalog fields → accepted as plan module
        res = validate_modules([{"key": "communications", "fields": ["e_statement", "logo"]}])
        assert res.modules == [{"key": "communications", "fields": ["e_statement", "logo"]}]

    def test_validate_rejects_ssn(self):
        res = validate_modules([{"key": "census", "fields": ["SSN", "First Name"]}])
        assert res.modules == [{"key": "census", "fields": ["First Name"]}]
        assert res.rejected[0]["reason"] == "full_ssn_not_permitted"

    def test_validate_unknown_field_warn_and_pass(self):
        res = validate_modules([{"key": "savings_rate", "fields": ["Mystery Field"]}])
        assert res.modules == [{"key": "savings_rate", "fields": ["Mystery Field"]}]
        assert res.warnings[0]["reason"] == "unverified_field"

    def test_validate_payroll_tokens(self):
        res = validate_modules([{"key": "payroll",
                                 "fields": ["Years: 2025 , 2024", "payroll 2023",
                                            "latest payroll", "years:banana"]}])
        assert res.modules == [{"key": "payroll",
                                "fields": ["years:2025,2024", "Payroll 2023", "Latest Payroll"]}]
        assert res.rejected[0]["reason"] == "invalid_payroll_token"

    def test_validate_malformed_entries(self):
        res = validate_modules(["census", {"key": "census", "fields": ["First Name"]}])
        assert res.modules == [{"key": "census", "fields": ["First Name"]}]
        assert res.rejected[0]["reason"] == "malformed_entry"
        assert validate_modules("garbage").rejected[0]["reason"] == "malformed_modules_payload"
        assert validate_modules(None).modules == []

    def test_split_modules_by_target(self):
        participant, plan = split_modules_by_target([
            {"key": "census", "fields": ["First Name"]},
            {"key": "plan_design", "fields": ["employer_contribution"]},
            {"key": "onboarding", "fields": ["blackout_begins_date"]},
            {"key": "mfa", "fields": ["MFA Status"]},
        ])
        assert [m["key"] for m in participant] == ["census", "mfa"]
        assert [m["key"] for m in plan] == ["plan_design", "onboarding"]


# ---------------------------------------------------------------------------
# normalize_scrape_result — real payload fixtures
# ---------------------------------------------------------------------------

# Confirmed real participant payload (abridged but structurally exact).
REAL_PARTICIPANT_PAYLOAD = [{
    "state": "succeeded",
    "data": {
        "participantId": "129043",
        "census": {
            "First Name": "Darth", "Last Name": "Maul",
            "Eligibility Status": "Terminated",
            "Termination Date": "2019-10-01", "Rehire Date": "",
        },
        "savings_rate": {
            "Current Pre-tax Percent": "6%", "Record Keeper": "LT Trust",
            "Formula": "Employer matches 100% up to first 3%...",
            "Timing": "Ongoing",
        },
        "loans": {"Loan History": "There's no Loan History for this Participant"},
        "plan_details": {"Plan Type": "Startup", "Force-out Limit": 7000,
                         "Formula": "Employer matches 100.0%..."},
        "payroll": {
            "Payroll Frequency": "Semi-monthly",
            "Available Years": ["2021", "2020", "2019"],
            "Payroll 2021": {"Total": {"Pre-tax": 180}, "Rows": [
                {"Pay Date": "2021-01-15", "Pre-tax": 180,
                 "Pay Date URL": "/issues/issues_for_slot?slot_id=76500"}]},
            "Latest Payroll": {"Pay Date": "2021-01-15", "Pre-tax": 180,
                               "Pay Date URL": "/issues/..."},
        },
        "mfa": {"MFA Status": "not enrolled"},
    },
    "warnings": [],
    "errors": [],
}]

# Confirmed real plan payload (abridged but structurally exact).
REAL_PLAN_PAYLOAD = [{
    "state": "succeeded",
    "data": {
        "planId": "580",
        "basic_info": {"plan_id": "580", "company_name": "StarWars Inc.",
                       "official_plan_name": "StarWars Inc.", "ein": "EIN",
                       "status": "Ongoing"},
        "plan_design": {"record_keeper_id": "LT Trust",
                        "employer_contribution": "SH Match Traditional",
                        "default_savings_rate": 6, "eligibility_min_age": 18},
        "onboarding": {"first_deferral_date": "2019-08-01",
                       "blackout_begins_date": ""},
        "communications": {"e_statement": "Yes"},
        "extra_settings": {"plan_year_start": "January"},
        "feature_flags": {"payroll_xray": "true",
                          "crypto_portfolio_alert_blacklist": "false"},
        "notes": ["using for test", "backfill force out limit"],
    },
    "warnings": [],
    "errors": [],
}]

# Local-repo normalizer envelope (normScrape shape).
ENVELOPE_PAYLOAD = {
    "ok": True, "code": "SCRAPE_OK", "message": None,
    "data": {
        "participantId": "158948", "url": "https://employer.forusall.com/participants/158948",
        "modulesRequested": [{"key": "census", "fields": ["Termination Date"]}],
        "modules": [
            {"key": "census", "status": "ok", "source": "panel",
             "requestedFields": ["Termination Date"],
             "unknownFields": ["Vested Balance"],
             "data": {"Termination Date": "2026-02-01"},
             "extractorWarnings": ["slow panel"]},
            {"key": "payroll", "status": "error", "source": "panel",
             "error": "panel_not_found"},
        ],
        "full": None,
    },
    "warnings": ["w1"], "errors": [],
}


class TestNormalizeScrapeResult:

    def test_real_participant_payload(self):
        flat, meta = normalize_scrape_result(REAL_PARTICIPANT_PAYLOAD)
        assert meta["shape"] == "flat_data"
        assert flat["census"]["Termination Date"] == "2019-10-01"
        assert flat["savings_rate"]["Formula"].startswith("Employer matches")
        assert flat["payroll"]["Latest Payroll"]["Pay Date"] == "2021-01-15"
        assert "participantId" not in flat   # non-module keys excluded

    def test_real_plan_payload_with_notes(self):
        flat, meta = normalize_scrape_result(REAL_PLAN_PAYLOAD)
        assert meta["shape"] == "flat_data"
        assert flat["plan_design"]["employer_contribution"] == "SH Match Traditional"
        assert flat["basic_info"]["company_name"] == "StarWars Inc."
        assert flat["plan_notes"] == ["using for test", "backfill force out limit"]
        assert "planId" not in flat

    def test_envelope_payload(self):
        flat, meta = normalize_scrape_result(ENVELOPE_PAYLOAD)
        assert meta["shape"] == "envelope"
        assert flat["census"]["Termination Date"] == "2026-02-01"
        assert "payroll" not in flat                      # error module excluded
        assert meta["module_errors"] == {"payroll": "panel_not_found"}
        assert meta["unknown_fields"] == {"census": ["Vested Balance"]}
        assert meta["extractor_warnings"] == {"census": ["slow panel"]}
        assert meta["warnings"] == ["w1"]

    def test_already_flat_passthrough(self):
        flat, meta = normalize_scrape_result({"census": {"First Name": "A"}})
        assert meta["shape"] == "flat"
        assert flat == {"census": {"First Name": "A"}}

    def test_garbage_and_empty(self):
        assert normalize_scrape_result(None) == ({}, {"shape": "empty"})
        assert normalize_scrape_result([]) == ({}, {"shape": "empty"})
        assert normalize_scrape_result("nope") == ({}, {"shape": "empty"})
        assert normalize_scrape_result({"foo": "bar"}) == ({}, {"shape": "empty"})

    def test_envelope_module_with_missing_data_key(self):
        payload = {"data": {"modules": [{"key": "census", "status": "ok"}]}}
        flat, meta = normalize_scrape_result(payload)
        assert flat == {"census": {}}    # data undefined → {}
