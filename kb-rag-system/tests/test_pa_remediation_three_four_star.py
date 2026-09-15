"""Contracts for the nine 3★/4★ PA remediation tickets.

The cases all concern outgoing termination distributions, account access, or
their delivery details. Incoming rollovers are intentionally outside this
contract and must not be changed as part of this batch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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


class TestAccountRecoveryKnowledgePolicy:
    @pytest.mark.parametrize('question', [
        'My ForUsAll password reset did not work.',
        "My password reset didn't work and I still cannot sign in.",
        'The participant reports that the password reset failed.',
        'Resetting my password did not work.',
        'I reset my password but I still cannot log in.',
    ])
    def test_failed_password_reset_advances_to_supported_recovery(self, question):
        parsed = {'answer': "Start by selecting Forgot your password? and reset it again.",
                  'key_points': ['Try the password reset first.'], 'coverage_gaps': []}
        fixed, info = RAGEngine._apply_account_recovery_knowledge_policy(parsed, question)
        rendered = json.dumps(fixed).lower()
        assert info == {'applied': True, 'reason': 'password_reset_failed'}
        assert '844-401-2253' in fixed['answer']
        assert 'forgot your password?' not in rendered
        assert 'reset it again' not in rendered
        assert 'try the password reset first' not in rendered
        assert 'password or authentication code' in rendered
        assert 'email on file is unknown' not in rendered
        assert 'mfa reset is required' not in rendered

    @pytest.mark.parametrize('question', [
        'How do I reset my password?',
        'My password reset worked, but my loan request failed.',
        'I have not tried resetting my password yet.',
        'I received a password reset email I did not request.',
        'My password reset failed yesterday, but I can now log in.',
        'What if the password reset fails?',
    ])
    def test_unattempted_successful_or_unrelated_failure_is_not_failed_reset(self, question):
        parsed = {'answer': 'Existing relevant answer.', 'key_points': [], 'coverage_gaps': []}
        fixed, info = RAGEngine._apply_account_recovery_knowledge_policy(parsed, question)
        assert fixed is parsed
        assert info == {'applied': False}

    def test_unknown_email_uses_phone_support_not_self_service_reset(self):
        parsed = {
            "answer": (
                "Select 'Forgot your password?' and call Support Monday-Friday, "
                "8am-5pm PST if that does not work."
            ),
            "key_points": ["Try the normal password reset first."],
            "coverage_gaps": [],
        }

        fixed, info = RAGEngine._apply_account_recovery_knowledge_policy(
            parsed,
            "I do not remember which email is on file or my password.",
        )
        rendered = json.dumps(fixed, ensure_ascii=False).lower()

        assert info["applied"] is True
        assert "844-401-2253" in rendered
        assert "monday-friday" in rendered
        assert "7:00 am-5:00 pm pt" in rendered
        assert "forgot your password?" not in rendered
        assert "8am-5pm" not in rendered
        assert "password or authentication code" in rendered

    def test_known_email_forgotten_password_keeps_self_service_guidance(self):
        parsed = {
            "answer": "Select 'Forgot your password?' using the email on file.",
            "key_points": [],
            "coverage_gaps": [],
        }

        fixed, info = RAGEngine._apply_account_recovery_knowledge_policy(
            parsed,
            "I know and can access my email on file, but I forgot my password.",
        )

        assert fixed is parsed
        assert info["applied"] is False

    def test_unknown_email_detection_handles_contractions_and_third_person(self):
        parsed = {
            "answer": "Use self-service password reset.",
            "key_points": [],
            "coverage_gaps": [],
        }

        contracted, contracted_info = (
            RAGEngine._apply_account_recovery_knowledge_policy(
                parsed,
                "I can't remember which email is on file.",
            )
        )
        third_person, third_person_info = (
            RAGEngine._apply_account_recovery_knowledge_policy(
                parsed,
                "She doesn't remember which email she used.",
            )
        )

        assert contracted_info["applied"] is True
        assert third_person_info["applied"] is True
        assert "844-401-2253" in contracted["answer"]
        assert "844-401-2253" in third_person["answer"]

    def test_inaccessible_email_detection_handles_natural_contractions(self):
        parsed = {
            "answer": "Use self-service password reset.",
            "key_points": [],
            "coverage_gaps": [],
        }
        questions = (
            "I can't access my email on file.",
            "I don't have access to the email on file.",
            "She can't access her email anymore.",
        )

        results = [
            RAGEngine._apply_account_recovery_knowledge_policy(parsed, question)
            for question in questions
        ]

        assert all(info["applied"] is True for _, info in results)
        assert all("844-401-2253" in fixed["answer"] for fixed, _ in results)

    def test_unknown_email_contractions_without_which_use_phone_support(self):
        parsed = {
            "answer": "Use self-service password reset.",
            "key_points": [],
            "coverage_gaps": [],
        }
        questions = (
            "I can't remember my email.",
            "I don't remember my email on file.",
            "She doesn't know her email on file.",
        )

        results = [
            RAGEngine._apply_account_recovery_knowledge_policy(parsed, question)
            for question in questions
        ]

        assert all(info["applied"] is True for _, info in results)

    def test_email_password_phrase_does_not_mean_email_is_unknown(self):
        parsed = {
            "answer": "Use self-service password reset.",
            "key_points": [],
            "coverage_gaps": [],
        }

        questions = (
            "I don't know my email password, but I can access the inbox.",
            "I cannot remember my email password, but I can access the inbox.",
            "I cant remember my email password, but I can access the inbox.",
            "I forgot my email password, but I can access the inbox.",
        )

        results = [
            RAGEngine._apply_account_recovery_knowledge_policy(parsed, question)
            for question in questions
        ]

        assert all(fixed is parsed for fixed, _ in results)
        assert all(info["applied"] is False for _, info in results)


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

    def test_unknown_email_access_is_distinct_from_known_email_password_reset(self):
        unknown = self.engine._infer_retrieval_signals(
            "I do not remember which email is on file or my password.",
            "distribution",
            self.terminated,
        )
        known = self.engine._infer_retrieval_signals(
            "I know and can access my email on file but forgot my password.",
            "distribution",
            self.terminated,
        )

        assert unknown["unknown_email_access"] is True
        assert known["unknown_email_access"] is False

    def test_unknown_email_signal_always_enables_account_access_companions(self):
        profile = self.engine._build_retrieval_profile(
            inquiry=(
                "I don't remember my email and need guidance for my "
                "termination distribution."
            ),
            topic="termination_distribution_request",
            record_keeper="LT Trust",
            plan_type="401(k)",
            collected_data=self.terminated,
        )

        assert profile["signals"]["unknown_email_access"] is True
        assert profile["signals"]["account_access"] is True
        assert profile["companion_article_ids"] == [
            RAGEngine.ACCOUNT_SETUP_ARTICLE_ID,
            RAGEngine.MFA_ARTICLE_ID,
        ]

    def test_known_59_5_status_is_carried_into_response_signals(self):
        collected = {
            "participant_data": {
                "employment_status": "Terminated",
                "is_age_59_5_or_older": False,
            },
            "plan_data": {"record_keeper": "LT Trust"},
        }

        signals = self.engine._infer_retrieval_signals(
            "I left my employer and want a cash distribution.",
            "distribution",
            collected,
        )

        assert signals["is_age_59_5_or_older"] is False

    def test_termination_date_presence_is_carried_into_response_signals(self):
        signals = self.engine._infer_retrieval_signals(
            "I left my employer and want a direct rollover.",
            "rollover",
            self.terminated,
        )

        assert signals["termination_date_present"] is True

    def test_fidelity_next_steps_carries_narrow_rollover_guidance_signals(self):
        signals = self.engine._infer_retrieval_signals(
            (
                "I left my employer and am requesting guidance on the next "
                "steps to roll over my account to Fidelity."
            ),
            "rollover",
            self.terminated,
        )

        assert signals["guidance_request"] is True
        assert signals["rollover_provider_named"] is True

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

    def test_incoming_signal_is_outside_policy_even_if_action_was_misrouted(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "Incoming guidance.",
                "key_points": ["Keep this unchanged."],
                "steps": [],
                "warnings": [],
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(
            parsed,
            {
                "primary_action": "termination_rollover",
                "signals": {
                    "incoming_rollover": True,
                    "pure_rollover": True,
                },
            },
        )

        assert fixed is parsed
        assert info["applied"] is False

    def test_active_generic_distribution_is_outside_termination_policy(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "Hardship guidance.",
                "key_points": ["Keep the hardship-specific answer."],
                "steps": [],
                "warnings": [],
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(
            parsed,
            {
                "primary_action": "distribution",
                "signals": {
                    "termination_distribution": False,
                    "cash_component": True,
                    "pure_rollover": False,
                },
            },
        )

        assert fixed is parsed
        assert info["applied"] is False
        assert "10%" not in json.dumps(fixed)

    def test_full_rollover_removes_cash_only_and_obsolete_content(self):
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "Eligible",
            "response_to_participant": {
                "opening": "You can proceed.",
                "key_points": [
                    "ACH is available.",
                    "20% federal withholding applies.",
                    "An additional 10% tax may apply.",
                    "A 10% early distribution penalty may apply.",
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
            "20%", "10%", "withholding", "fee-out", "$75 or less",
            "known issue", "multiple 401(k)", "cash distributions are taxable",
        ):
            assert forbidden not in rendered
        assert len(fixed["response_to_participant"]["steps"]) <= 6
        assert FORM_URL in json.dumps(fixed)
        assert "$35" in json.dumps(fixed)

    def test_pure_rollover_removes_cash_tax_from_opening_and_reason(self):
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "A 10% tax may apply to this rollover.",
            "response_to_participant": {
                "opening": "Your direct rollover may have 20% withholding.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(fixed, ensure_ascii=False).lower()

        assert "10%" not in rendered
        assert "20%" not in rendered
        assert "withholding" not in rendered

    def test_cash_distribution_keeps_the_under_59_5_penalty_condition(self):
        """Regression for TKT-905906 when birth date is unavailable."""
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "Eligible",
            "response_to_participant": {
                "opening": "You can proceed with a cash distribution.",
                "key_points": [
                    "A cash distribution generally has 20% federal withholding."
                ],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [],
            "escalation": {"needed": False, "reason": None},
            "guardrails_applied": [],
            "data_gaps": [],
            "coverage_gaps": [],
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "cash_component": False,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
                "account_access": True,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(
            fixed["response_to_participant"], ensure_ascii=False
        ).lower()

        assert "10%" in rendered
        assert "59½" in rendered or "59.5" in rendered
        assert "if" in rendered

    def test_unknown_email_removes_self_service_reset_from_main_response(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "Start with the Forgot your password? link.",
                "key_points": ["Request a password reset email first."],
                "steps": [{
                    "step_number": 1,
                    "action": "Use the reset link sent to your email.",
                    "detail": None,
                }],
                "warnings": [],
            },
            "questions_to_ask": [],
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "account_access": True,
                "unknown_email_access": True,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(fixed, ensure_ascii=False).lower()

        assert "forgot your password" not in rendered
        assert "password reset" not in rendered
        assert "reset link" not in rendered
        assert "844-401-2253" in rendered

    def test_cash_distribution_uses_known_over_59_5_status(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "You can proceed.",
                "key_points": ["20% federal withholding applies."],
                "steps": [],
                "warnings": [],
            },
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "cash_component": True,
                "is_age_59_5_or_older": True,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(
            fixed["response_to_participant"], ensure_ascii=False
        ).lower()

        assert "10%" in rendered
        assert "does not apply" in rendered
        assert "at least age 59½" in rendered

    def test_cash_distribution_uses_known_under_59_5_status(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "You can proceed.",
                "key_points": ["20% federal withholding applies."],
                "steps": [],
                "warnings": [],
            },
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "cash_component": True,
                "is_age_59_5_or_older": False,
                "overnight_request": False,
                "brokerage_destination_ambiguous": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(
            fixed["response_to_participant"], ensure_ascii=False
        ).lower()

        assert "10%" in rendered
        assert "because you are under age 59½" in rendered

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

    def test_supported_rollover_normalizes_destination_execution_question(self):
        parsed = {
            "outcome": "blocked_missing_data",
            "outcome_reason": "Destination account type is needed.",
            "response_to_participant": {
                "opening": "I need one detail before I can provide the steps.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [{
                "question": (
                    "What type of Fidelity account will receive the rollover: "
                    "a Traditional IRA, Roth IRA, or qualified plan?"
                ),
                "why": "The destination account type was not provided.",
            }],
            "data_gaps": ["Destination account type"],
            "guardrails_applied": [],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "employment_state": "terminated",
                "termination_date_present": True,
                "guidance_request": True,
                "rollover_provider_named": True,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
                "account_access": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(fixed, ensure_ascii=False).lower()

        assert fixed["outcome"] == "can_proceed"
        assert fixed["questions_to_ask"] == []
        assert fixed["data_gaps"] == []
        assert fixed["escalation"] == {"needed": False, "reason": None}
        assert info["rollover_execution_normalized"] is True
        assert "confirm" in rendered and "receiving provider" in rendered
        assert FORM_URL.lower() in rendered
        assert "20%" not in rendered

    @pytest.mark.parametrize(
        "why",
        [
            "This determines the correct rollover path.",
            "This determines the rollover path and tax-character handling.",
        ],
    )
    def test_supported_rollover_accepts_provider_between_type_and_account(self, why):
        parsed = {
            "outcome": "blocked_missing_data",
            "outcome_reason": "One receiving-account detail is needed.",
            "response_to_participant": {
                "opening": "I need one detail before I can provide the steps.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [{
                "question": (
                    "What type of Fidelity account will receive the rollover: "
                    "a Traditional IRA, Roth IRA, or qualified plan?"
                ),
                "why": why,
            }],
            "data_gaps": [],
            "guardrails_applied": [],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "employment_state": "terminated",
                "termination_date_present": True,
                "guidance_request": True,
                "rollover_provider_named": True,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(parsed, profile)

        assert fixed["outcome"] == "can_proceed"
        assert fixed["questions_to_ask"] == []
        assert info["rollover_execution_normalized"] is True

    def test_rollover_without_termination_date_keeps_execution_block(self):
        parsed = {
            "outcome": "blocked_missing_data",
            "response_to_participant": {
                "opening": "I need more information.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [{
                "question": "What is your termination date?",
                "why": "The date is not on file.",
            }],
            "data_gaps": ["Termination date"],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "employment_state": "terminated",
                "termination_date_present": False,
                "guidance_request": True,
                "rollover_provider_named": True,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(parsed, profile)

        assert fixed["outcome"] == "blocked_missing_data"
        assert len(fixed["questions_to_ask"]) == 1
        assert info["rollover_execution_normalized"] is False

    def test_active_separation_conflict_withholds_form_until_date_is_confirmed(self):
        parsed = {
            "outcome": "blocked_missing_data",
            "response_to_participant": {
                "opening": "Your record still shows active employment.",
                "key_points": [],
                "steps": [{
                    "step_number": 1,
                    "action": "Use the cash-out form now.",
                    "detail": FORM_URL,
                }],
                "warnings": [],
            },
            "questions_to_ask": [{
                "question": "What was your last day of employment?",
                "why": "The plan still shows active status.",
            }],
            "data_gaps": ["Termination date"],
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "employment_state": "active",
                "separation_conflicts_active": False,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(fixed, ensure_ascii=False).lower()

        assert fixed["outcome"] == "blocked_missing_data"
        assert len(fixed["questions_to_ask"]) == 1
        assert fixed["response_to_participant"]["steps"] == []
        assert FORM_URL.lower() not in rendered
        assert info["separation_conflict_trimmed"] is True

    def test_active_blocked_termination_never_emits_operational_steps(self):
        parsed = {
            "outcome": "blocked_missing_data",
            "response_to_participant": {
                "opening": "Your record still shows active employment.",
                "key_points": [],
                "steps": [{
                    "step_number": 1,
                    "action": "Start a cash withdrawal in the portal.",
                    "detail": None,
                }],
                "warnings": [],
            },
            "questions_to_ask": [{
                "question": "When did you leave the company?",
                "why": "The plan still shows active status.",
            }],
            "data_gaps": ["Separation information"],
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "employment_state": "active",
                "separation_conflicts_active": False,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(parsed, profile)

        assert fixed["outcome"] == "blocked_missing_data"
        assert fixed["response_to_participant"]["steps"] == []
        assert info["separation_conflict_trimmed"] is True

    def test_pure_rollover_removes_cash_and_tax_paraphrases_everywhere(self):
        parsed = {
            "outcome": "can_proceed",
            "outcome_reason": "You may take cash or roll over.",
            "response_to_participant": {
                "opening": "You can request a cash payment or direct rollover.",
                "key_points": [
                    "A taxable cash withdrawal may incur a 10 percent IRS penalty.",
                    "Federal taxes will be withheld from cash withdrawals.",
                ],
                "steps": [{
                    "step_number": 1,
                    "action": "Choose a cash payout or direct rollover.",
                    "detail": None,
                }],
                "warnings": ["Cash payouts are taxable."],
            },
            "questions_to_ask": [],
            "data_gaps": [],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(fixed, ensure_ascii=False).lower()

        for forbidden in (
            "cash withdrawal",
            "cash payment",
            "cash payout",
            "take cash",
            "10 percent",
            "taxes will be withheld",
            "irs penalty",
        ):
            assert forbidden not in rendered

    def test_destination_normalization_keeps_combined_hard_blockers(self):
        parsed = {
            "outcome": "blocked_missing_data",
            "response_to_participant": {
                "opening": "I need more information.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
            "questions_to_ask": [{
                "question": "What type of account at Fidelity will receive it?",
                "why": "Confirm whether it is an IRA or qualified plan.",
            }],
            "data_gaps": [
                "Destination account type and outstanding loan status"
            ],
        }
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "employment_state": "terminated",
                "termination_date_present": True,
                "guidance_request": True,
                "rollover_provider_named": True,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        fixed, info = RAGEngine._apply_termination_response_policy(parsed, profile)

        assert fixed["outcome"] == "blocked_missing_data"
        assert info["rollover_execution_normalized"] is False

    def test_destination_normalization_rejects_all_combined_gaps(self):
        combined_gaps = (
            "Destination account type and separation date",
            "Destination account type and last day of employment",
            "Destination account type and eligibility status",
            "Destination account type and spousal consent",
        )
        profile = {
            "primary_action": "termination_rollover",
            "signals": {
                "pure_rollover": True,
                "employment_state": "terminated",
                "termination_date_present": True,
                "guidance_request": True,
                "rollover_provider_named": True,
                "brokerage_destination_ambiguous": False,
                "overnight_request": False,
            },
        }

        for gap in combined_gaps:
            parsed = {
                "outcome": "blocked_missing_data",
                "response_to_participant": {
                    "opening": "I need more information.",
                    "key_points": [],
                    "steps": [],
                    "warnings": [],
                },
                "questions_to_ask": [{
                    "question": (
                        "What is the destination account type: Traditional IRA, "
                        "Roth IRA, or qualified plan?"
                    ),
                    "why": "The rollover destination type is needed.",
                }],
                "data_gaps": [gap],
            }

            fixed, info = RAGEngine._apply_termination_response_policy(
                parsed, profile
            )

            assert fixed["outcome"] == "blocked_missing_data", gap
            assert info["rollover_execution_normalized"] is False, gap

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

    def test_overnight_delivery_is_form_only_and_keeps_the_fee(self):
        parsed = {
            "outcome": "can_proceed",
            "response_to_participant": {
                "opening": "You can proceed.",
                "key_points": [],
                "steps": [],
                "warnings": [],
            },
        }
        profile = {
            "primary_action": "termination_distribution",
            "signals": {
                "pure_rollover": False,
                "cash_component": True,
                "overnight_request": True,
                "brokerage_destination_ambiguous": False,
            },
        }

        fixed, _ = RAGEngine._apply_termination_response_policy(parsed, profile)
        rendered = json.dumps(
            fixed["response_to_participant"], ensure_ascii=False
        ).lower()

        assert "overnight check" in rendered
        assert "cannot be selected" in rendered
        assert "$50" in rendered
        assert "rightsignature" in rendered

    def test_prompts_declare_the_same_intent_scope(self):
        prompt = prompts.SYSTEM_PROMPT_GENERATE_RESPONSE
        assert "INTENT-SCOPED TERMINATION DISTRIBUTION" in prompt
        assert "pure direct rollover" in prompt.lower()
        assert "brokerage account" in prompt.lower()
        assert "six participant-facing steps" in prompt.lower()
