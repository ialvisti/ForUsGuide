"""Contracts for the nine 3★/4★ PA remediation tickets.

The cases all concern outgoing termination distributions, account access, or
their delivery details. Incoming rollovers are intentionally outside this
contract and must not be changed as part of this batch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from data_pipeline import prompts
from data_pipeline.forusbots_catalog import map_slug
from data_pipeline.rag_engine import RAGEngine


ARTICLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "PA"
    / "Distributions"
    / "LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
)
ACCESS_ARTICLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "PA"
    / "Participant Dashboard"
    / "LT: How to Set Up Your ForUsAll 401(k) Account.json"
)
FORM_URL = (
    "https://secure.rightsignature.com/templates/"
    "105723c3-eaf1-4a44-aaed-09ad5c253ec8/template-signer-link/"
    "a9d83fbb137e5315fbd641bb522b9723"  # pragma: allowlist secret
)


def _article() -> dict:
    return json.loads(ARTICLE_PATH.read_text(encoding="utf-8"))


def _business_rules(article: dict) -> dict[str, list[str]]:
    return {
        entry["category"]: entry["rules"]
        for entry in article["details"]["business_rules"]
    }


class TestKnowledgeContract:
    def test_obsolete_threshold_known_issue_and_rare_multi_plan_advice_are_gone(self):
        text = json.dumps(_article(), ensure_ascii=False).lower()
        assert "fee-out" not in text
        assert "small balance threshold" not in text
        assert "$75 or less" not in text
        assert "known issue" not in text
        assert "more than one 401(k)" not in text
        assert "multiple 401(k)" not in text

    def test_rollover_cash_and_delivery_methods_are_strictly_scoped(self):
        rules = _business_rules(_article())
        rollover = " ".join(rules["rollover_payment_method"])
        tax = " ".join(rules["tax_withholding"])
        wire = " ".join(rules["wire_instructions"])
        delivery = " ".join(rules["delivery"])

        assert "check or wire" in rollover.lower()
        assert "ACH is not a rollover delivery method" in rollover
        assert "cash or cash portion" in tax.lower()
        assert "does not apply to a direct rollover" in tax.lower()
        assert "$35" in wire and "each wire transaction" in wire.lower()
        assert "separate wire" in wire.lower() and "source" in wire.lower()
        assert "RightSignature" in delivery
        assert "$50" in delivery and "cannot be selected" in delivery

    def test_portal_then_form_and_support_details_are_complete(self):
        article = _article()
        rules = _business_rules(article)
        submission = " ".join(rules["submission_method"])
        contact = article["details"]["references"]["contact"]

        assert "participant portal" in submission
        assert FORM_URL in submission
        assert "secondary" in submission.lower() or "fallback" in submission.lower()
        assert contact == {
            "email": "help@forusall.com",
            "phone": "844-401-2253",
            "support_hours": "Mon-Fri, 07:00-17:00 PST",
        }

    def test_cashout_followup_and_rollover_preflight_cover_reviewed_cases(self):
        rules = _business_rules(_article())
        cashout = " ".join(rules["termination_date_followup"])
        preflight = " ".join(rules["rollover_pre_submission_checks"])

        assert "termination date" in cashout.lower()
        assert "do not ask for payment details" in cashout.lower()
        assert "portal" in cashout.lower() and FORM_URL in cashout
        for required in (
            "receiving provider", "Loans & Distributions", "1099-R",
            "outstanding loan", "final payroll", "crypto",
        ):
            assert required.lower() in preflight.lower()

    def test_account_recovery_article_covers_forgotten_email_and_password(self):
        article = json.loads(ACCESS_ARTICLE_PATH.read_text(encoding="utf-8"))
        rules = _business_rules(article)
        recovery = " ".join(rules["login_recovery"])

        for required in (
            "Forgot your password?",
            "email on file",
            "844-401-2253",
            "Monday-Friday",
            "7:00 AM-5:00 PM",
        ):
            assert required.lower() in recovery.lower()


class TestDeterministicIntentSignals:
    def setup_method(self):
        self.engine = object.__new__(RAGEngine)
        self.terminated = {
            "participant_data": {
                "employment_status": "Terminated",
                "termination_date": "2026-01-15",
                "account_balance": 12000,
            },
            "plan_data": {"record_keeper": "LT Trust"},
        }

    def test_pure_rollover_excludes_cash_only_topics(self):
        signals = self.engine._infer_retrieval_signals(
            "I left my employer and want a full direct rollover to my Fidelity IRA.",
            "rollover", self.terminated,
        )
        assert signals["pure_rollover"] is True
        assert signals["cash_component"] is False

    def test_brokerage_destination_requires_retirement_vs_taxable_clarification(self):
        ambiguous = self.engine._infer_retrieval_signals(
            "I left my employer and want to move my 401(k) to a brokerage account.",
            "rollover", self.terminated,
        )
        explicit_ira = self.engine._infer_retrieval_signals(
            "I left my employer and want to move it to a rollover IRA at Fidelity.",
            "rollover", self.terminated,
        )
        assert ambiguous["brokerage_destination_ambiguous"] is True
        assert explicit_ira["brokerage_destination_ambiguous"] is False

    def test_account_access_is_a_companion_to_distribution_not_a_lost_intent(self):
        profile = self.engine._build_retrieval_profile(
            inquiry=(
                "I left my employer, no longer have the email or password for my "
                "account, and need to request my distribution."
            ),
            topic="termination_distribution_request",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self.terminated,
        )
        assert profile["signals"]["account_access"] is True
        assert profile["companion_article_ids"] == [
            RAGEngine.ACCOUNT_SETUP_ARTICLE_ID,
            RAGEngine.MFA_ARTICLE_ID,
        ]

    def test_crypto_enrollment_can_be_collected_for_preflight(self):
        assert map_slug({"field": "crypto_enrollment"}, current_year=2026) == [
            ("census", "Crypto Enrollment")
        ]


class TestDeterministicResponsePolicy:
    def test_incoming_rollover_is_outside_this_policy(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "Incoming rollover guidance.",
                "key_points": ["Keep this unchanged."],
                "steps": [],
                "warnings": [],
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(
            parsed,
            {
                "primary_action": "incoming_rollover",
                "signals": {"pure_rollover": True},
            },
        )

        assert fixed is parsed
        assert fixed["response_to_participant"]["key_points"] == [
            "Keep this unchanged."
        ]
        assert info["applied"] is False

    def test_full_rollover_removes_cash_only_and_obsolete_content(self):
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "Eligible",
            "response_to_participant": {
                "opening": "You can proceed.",
                "key_points": [
                    "ACH is available.",
                    "20% federal withholding applies.",
                    "Balances of $75 or less use fee-out.",
                    "This is a known issue.",
                    "If you have multiple 401(k) plans, pick one.",
                    "Confirm the receiving IRA accepts the rollover.",
                ],
                "steps": [
                    {"step_number": i, "action": f"step {i}", "detail": None}
                    for i in range(1, 10)
                ],
                "warnings": ["Cash distributions are taxable income."],
            },
            "questions_to_ask": [],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [],
            "data_gaps": [],
            "coverage_gaps": [],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
                "account_access": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(
            parsed, profile
        )
        rendered = json.dumps(fixed, ensure_ascii=False).lower()
        assert info["applied"] is True
        assert re.search(r"\bach\b", rendered) is None
        for forbidden in (
            "20%", "withholding", "fee-out", "$75 or less",
            "known issue", "multiple 401(k)", "cash distributions are taxable",
        ):
            assert forbidden not in rendered
        assert len(fixed["response_to_participant"]["steps"]) <= 6
        assert FORM_URL in json.dumps(fixed)
        assert "$35" in json.dumps(fixed)

    def test_ambiguous_brokerage_forces_one_blocking_clarification(self):
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "Eligible",
            "response_to_participant": {
                "opening": "You can roll it over.", "key_points": [],
                "steps": [], "warnings": [],
            },
            "questions_to_ask": [],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [], "data_gaps": [], "coverage_gaps": [],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "overnight_request": False,
                "brokerage_destination_ambiguous": True,
                "account_access": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        assert fixed["outcome"] == "blocked_missing_data"
        assert len(fixed["questions_to_ask"]) == 1
        question = fixed["questions_to_ask"][0]["question"].lower()
        assert "retirement account" in question
        assert "taxable brokerage" in question

    def test_required_rollover_preflight_and_support_survive_response_caps(self):
        """Regression for TKT-905935/TKT-909733.

        The model may already fill all six key-point and step slots.  The
        reviewed outgoing-rollover facts still have to be present after the
        deterministic response cap is applied.
        """
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "Eligible",
            "response_to_participant": {
                "opening": "You can proceed.",
                "key_points": [f"Optional model point {i}" for i in range(1, 7)],
                "steps": [
                    {
                        "step_number": i,
                        "action": f"Optional model step {i}",
                        "detail": None,
                    }
                    for i in range(1, 7)
                ],
                "warnings": [],
            },
            "questions_to_ask": [],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [],
            "data_gaps": [],
            "coverage_gaps": [],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
                "account_access": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        response = fixed["response_to_participant"]
        rendered = json.dumps(response, ensure_ascii=False).lower()

        assert len(response["key_points"]) <= 6
        assert len(response["steps"]) <= 6
        for required in (
            "receiving provider",
            "$35",
            "each separate wire",
            "loans & distributions",
            "1099-r",
            "outstanding loan",
            "final payroll",
            "crypto",
            "844-401-2253",
            FORM_URL.lower(),
        ):
            assert required in rendered

    def test_prompts_declare_the_same_intent_scope(self):
        prompt = prompts.SYSTEM_PROMPT_GENERATE_RESPONSE
        assert "INTENT-SCOPED TERMINATION DISTRIBUTION" in prompt
        assert "pure direct rollover" in prompt.lower()
        assert "brokerage account" in prompt.lower()
        assert "six participant-facing steps" in prompt.lower()
