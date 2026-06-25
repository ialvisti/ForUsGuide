"""
Regression tests for three RAG behavior fixes (loan/withdrawal ticket audit):

1. Age derivation from birth_date so 59½ / early-withdrawal-penalty statements
   are DEFINITE instead of hedged ("if you're under 59½").
2. A can_proceed answer is a complete first-contact resolution and never carries
   blocking questions for non-blocking execution details.
3. Sub-query decomposition never embeds company / plan / participant names (the
   KB is procedural — those have zero retrieval signal).
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pytest

from data_pipeline.rag_engine import RAGEngine
from data_pipeline import prompts


# ─────────────────────────────────────────────────────────────────────
# 1. Age derivation
# ─────────────────────────────────────────────────────────────────────
class TestAgeDerivation:
    def test_compute_age_iso_and_us_formats_agree(self):
        iso = RAGEngine._compute_age_from_birth_date("1960-04-15")
        us = RAGEngine._compute_age_from_birth_date("04/15/1960")
        assert iso is not None and iso >= 60
        assert iso == us

    def test_compute_age_handles_missing_and_garbage(self):
        assert RAGEngine._compute_age_from_birth_date(None) is None
        assert RAGEngine._compute_age_from_birth_date("") is None
        assert RAGEngine._compute_age_from_birth_date("not a date") is None
        # Future date is rejected.
        assert RAGEngine._compute_age_from_birth_date("2999-01-01") is None

    def test_is_age_59_5_clear_cases(self):
        assert RAGEngine._is_age_59_5_or_older("1950-01-01") is True
        assert RAGEngine._is_age_59_5_or_older("2000-01-01") is False
        assert RAGEngine._is_age_59_5_or_older(None) is None
        assert RAGEngine._is_age_59_5_or_older("garbage") is None

    def test_enrich_injects_age_without_mutating_input(self):
        eng = object.__new__(RAGEngine)
        cd = {"participant_data": {"first_name": "A", "birth_date": "1950-01-01"}}
        out = eng._enrich_collected_data_with_age(cd)
        assert out["participant_data"]["age"] >= 70
        assert out["participant_data"]["is_age_59_5_or_older"] is True
        # Caller's dict is untouched (shallow copy semantics).
        assert "age" not in cd["participant_data"]

    def test_enrich_is_noop_without_birth_date(self):
        eng = object.__new__(RAGEngine)
        cd = {"participant_data": {"first_name": "A"}}
        out = eng._enrich_collected_data_with_age(cd)
        assert "age" not in out["participant_data"]
        assert "is_age_59_5_or_older" not in out["participant_data"]

    def test_enrich_is_noop_on_none_or_empty(self):
        eng = object.__new__(RAGEngine)
        assert eng._enrich_collected_data_with_age(None) is None
        assert eng._enrich_collected_data_with_age({}) == {}


# ─────────────────────────────────────────────────────────────────────
# 2. can_proceed never carries blocking questions
# ─────────────────────────────────────────────────────────────────────
class TestNoQuestionsOnCanProceed:
    def test_guard_strips_questions_for_can_proceed(self):
        parsed = {
            "outcome": "can_proceed",
            "questions_to_ask": [
                {"question": "What loan amount?", "why": "config"},
                {"question": "Which delivery method?", "why": "config"},
            ],
        }
        dropped = RAGEngine._suppress_nonblocking_questions_for_can_proceed(parsed)
        assert parsed["questions_to_ask"] == []
        assert len(dropped) == 2

    def test_guard_preserves_questions_for_blocked_missing_data(self):
        parsed = {
            "outcome": "blocked_missing_data",
            "questions_to_ask": [{"question": "What is your termination date?", "why": "x"}],
        }
        dropped = RAGEngine._suppress_nonblocking_questions_for_can_proceed(parsed)
        assert parsed["questions_to_ask"] == [
            {"question": "What is your termination date?", "why": "x"}
        ]
        assert dropped == []

    def test_guard_preserves_questions_for_ambiguous(self):
        parsed = {"outcome": "ambiguous_plan_rules", "questions_to_ask": [{"question": "q", "why": "w"}]}
        RAGEngine._suppress_nonblocking_questions_for_can_proceed(parsed)
        assert parsed["questions_to_ask"] == [{"question": "q", "why": "w"}]

    def test_can_proceed_schema_declares_empty_questions(self):
        assert '"questions_to_ask": []' in prompts.OUTCOME_SCHEMAS["can_proceed"]

    def test_can_proceed_content_rules_forbid_questions(self):
        assert "questions_to_ask MUST be empty" in prompts.OUTCOME_CONTENT_RULES["can_proceed"]

    def test_gr_response_system_prompt_for_can_proceed_forbids_questions_and_has_age_block(self):
        system_prompt, _ = prompts.build_gr_response_prompt(
            context="Context with fees and delivery methods.",
            inquiry="Can I take a loan?",
            collected_data={"participant_data": {"first_name": "A"}},
            record_keeper="LT Trust",
            plan_type="401(k)",
            topic="loan",
            outcome="can_proceed",
            outcome_reason="eligible",
        )
        assert "questions_to_ask MUST be empty" in system_prompt
        assert "AGE / 59" in system_prompt

    def test_single_phase_gr_prompt_forbids_questions_for_can_proceed(self):
        assert "questions_to_ask MUST be empty" in prompts.SYSTEM_PROMPT_GENERATE_RESPONSE
        assert "AGE / 59" in prompts.SYSTEM_PROMPT_GENERATE_RESPONSE


# ─────────────────────────────────────────────────────────────────────
# 3. Decomposition never embeds company / proper names
# ─────────────────────────────────────────────────────────────────────
class TestDecomposeNoCompanyNames:
    def test_system_prompt_forbids_company_names(self):
        sp = prompts.SYSTEM_PROMPT_DECOMPOSE_QUESTION
        assert "NEVER include company" in sp
        assert "zero retrieval signal" in sp

    def test_builder_uses_the_hardened_system_prompt(self):
        sp_anchored, _ = prompts.build_decompose_question_prompt(
            "q", record_keeper="LT Trust", topic="loan"
        )
        sp_plain, _ = prompts.build_decompose_question_prompt("q")
        assert sp_anchored == sp_plain == prompts.SYSTEM_PROMPT_DECOMPOSE_QUESTION


# ─────────────────────────────────────────────────────────────────────
# 4. Birth Date is must_have in the audited 59½ articles
# ─────────────────────────────────────────────────────────────────────
_PA_DIR = Path(__file__).resolve().parents[2] / "PA"

# (article-title needle, expected blocking_intent for the Birth Date entry)
_AUDITED_59_HALF = [
    ("Can I Take Money From My 401(k) While Employed", "personalization_only"),
    ("ForUsAll 401(k) Hardship Withdrawal", "personalization_only"),
    ("401(k) Loan Basics and Support Guide", "personalization_only"),
    ("The 401(k) Force-Out Process", "personalization_only"),
    ("Missed 60-Day Indirect Rollover", "personalization_only"),
    ("401(k) Options After Leaving Your Job", "personalization_only"),
    ("LT: How to Request a 401(k) Termination Cash Withdrawal", "personalization_only"),
    ("401(k) Required Minimum Distributions", "eligibility_confirmation"),
]


@pytest.mark.skipif(not _PA_DIR.exists(), reason="PA article directory not present")
class TestBirthDateMustHave:
    @pytest.mark.parametrize("needle,expected_intent", _AUDITED_59_HALF)
    def test_birth_date_present_in_must_have(self, needle, expected_intent):
        matches = [
            f
            for f in glob.glob(str(_PA_DIR / "**" / "*.json"), recursive=True)
            if needle in os.path.basename(f)
        ]
        assert matches, f"no article matched {needle!r}"
        for f in matches:
            d = json.load(open(f, encoding="utf-8"))
            must_have = (d.get("details", {}).get("required_data", {}) or {}).get("must_have", [])
            entry = next(
                (x for x in must_have if isinstance(x, dict) and x.get("data_point") == "Birth Date"),
                None,
            )
            assert entry is not None, f"{os.path.basename(f)} missing Birth Date in must_have"
            assert entry.get("blocking_intent") == expected_intent

    def test_eaca_intentionally_excluded(self):
        """EACA refunds are not subject to the 10% penalty, so Birth Date must
        NOT have been force-added there."""
        matches = [
            f
            for f in glob.glob(str(_PA_DIR / "**" / "*.json"), recursive=True)
            if "EACA Refunds" in os.path.basename(f)
        ]
        for f in matches:
            d = json.load(open(f, encoding="utf-8"))
            must_have = (d.get("details", {}).get("required_data", {}) or {}).get("must_have", [])
            dps = {x.get("data_point") for x in must_have if isinstance(x, dict)}
            assert "Birth Date" not in dps
