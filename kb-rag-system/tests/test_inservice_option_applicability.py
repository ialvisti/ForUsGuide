"""RED tests for in-service option applicability (proposed for
kb-rag-system/tests/test_inservice_option_applicability.py).

Driven by the observed d909 live response to the pinned 910526 request
(request_body_sha256 f71296a7...). They exercise the deterministic
informational_options branch of `_apply_termination_response_policy`
(rag_engine.py:3975-3992) with the ACTUAL captured draft as input.

No ticket ID, no expected-answer string, and no age/balance literal is
hardcoded as a gate: each case derives applicability from the typed
preflight fact and the derived age flag supplied in the fixture.
"""
from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest


# --- The observed draft, verbatim from the live d909 capture. -------------
OBSERVED_KEY_POINTS = [
    "1. Options while employed: The main possible options are a hardship "
    "withdrawal, a 401(k) loan, a withdrawal of rollover-source money, or an "
    "age-based in-service distribution; each option depends on plan rules and "
    "your account details.",
    "Hardship withdrawals require an immediate and serious financial need that "
    "fits an IRS-approved reason, and the amount must be limited to what is "
    "necessary to meet that need.",
    "A 401(k) loan may be available only if the plan allows loans, you have "
    "sufficient vested balance, and you are not already at the plan's "
    "active-loan limit.",
    "A rollover-source withdrawal applies only to money previously rolled into "
    "this plan from another retirement account; your current rollover source "
    "balance on file is zero.",
    "Tell us which option you would like to explore, and our team will verify "
    "whether your plan permits it and what requirements apply.",
]

ROLLOVER_SOURCE = re.compile(r"rollover[-\s]source", re.I)
AGE_IN_SERVICE = re.compile(r"in[-\s]service distribution", re.I)
ZERO_DISCLOSURE = re.compile(r"balance on file is zero|rollover source balance", re.I)


@pytest.fixture
def engine():
    from data_pipeline.rag_engine import RAGEngine

    router = Mock()
    router.call = AsyncMock()
    with patch("data_pipeline.rag_engine.PineconeUploader"):
        return RAGEngine(llm_router=router)


def _collected(*, rollover: Dict[str, Any], age_59_5: bool | None) -> Dict[str, Any]:
    participant: Dict[str, Any] = {"employment_status": "Active", "termination_date": None}
    if age_59_5 is not None:
        participant["is_age_59_5_or_older"] = age_59_5
        participant["age"] = 46 if age_59_5 is False else 61
    return {
        "participant_data": participant,
        "plan_data": {"plan_status": "actively_managed", "plan_active": True},
        "internal_preflight_context": {
            "schema_version": 1,
            "sources": {
                "rollover_balance": rollover,
                "account_balance": {"value": 20000.0, "status": "known"},
            },
        },
        "internal_response_context": {
            "requested_questions": [
                "I am still employed. What options do I have to access funds?"
            ]
        },
    }


def _profile(engine, collected) -> Dict[str, Any]:
    return engine._build_retrieval_profile(
        inquiry="I am still employed. What options do I have to access funds?",
        topic="in_service_withdrawal_options",
        record_keeper="LT Trust",
        plan_type="401(k)",
        collected_data=collected,
    )


def _draft() -> Dict[str, Any]:
    return {
        "outcome": "can_proceed",
        "outcome_reason": "General information about accessing funds while employed.",
        "response_to_participant": {
            "opening": "You are currently listed as an active employee.",
            "key_points": copy.deepcopy(OBSERVED_KEY_POINTS),
            "steps": [],
            "warnings": [],
        },
        "questions_to_ask": [],
        "escalation": {"needed": False, "reason": None},
        "guardrails_applied": [],
        "data_gaps": [],
        "coverage_gaps": [],
    }


def _apply(engine, collected):
    parsed, info = engine._apply_termination_response_policy(
        parsed=_draft(), retrieval_profile=_profile(engine, collected),
        collected_data=collected,
    )
    return parsed["response_to_participant"]["key_points"], info


KNOWN_ZERO = {"value": 0.0, "status": "known", "source": "participant.savings_rate.Rollover Balance"}
KNOWN_POSITIVE = {"value": 12500.0, "status": "known", "source": "participant.savings_rate.Rollover Balance"}
UNKNOWN = {"value": None, "status": "unknown", "source": None}


def test_known_zero_rollover_source_is_not_offered_as_an_option(engine):
    """RED: a known-zero rollover source must not survive in the option list."""
    points, _ = _apply(engine, _collected(rollover=KNOWN_ZERO, age_59_5=False))
    assert not any(ROLLOVER_SOURCE.search(p) for p in points), points


def test_known_zero_rollover_source_does_not_disclose_the_zero_figure(engine):
    """RED: removing the option must not leave an account-specific zero behind."""
    points, _ = _apply(engine, _collected(rollover=KNOWN_ZERO, age_59_5=False))
    assert not any(ZERO_DISCLOSURE.search(p) for p in points), points


def test_under_59_5_age_based_in_service_is_not_offered_as_an_option(engine):
    """RED: the derived age flag already says this is unavailable; the option
    list must agree with the opening instead of contradicting it."""
    points, _ = _apply(engine, _collected(rollover=KNOWN_ZERO, age_59_5=False))
    assert not any(AGE_IN_SERVICE.search(p) for p in points), points


OPTION_PATTERNS = {
    "hardship": re.compile(r"hardship", re.I),
    "loan": re.compile(r"\bloan", re.I),
    "rollover_source": ROLLOVER_SOURCE,
    "age_based_in_service": AGE_IN_SERVICE,
}


def _options_named(points):
    joined = " ".join(points)
    return {name for name, rx in OPTION_PATTERNS.items() if rx.search(joined)}


def test_active_under_59_5_known_zero_leaves_exactly_loan_and_hardship(engine):
    """RED: exactly the two applicable options survive, and the deterministic
    invite to choose one is preserved."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = engine._apply_termination_response_policy(
        parsed=_draft(), retrieval_profile=_profile(engine, collected),
        collected_data=collected,
    )
    points = parsed["response_to_participant"]["key_points"]
    assert _options_named(points) == {"hardship", "loan"}, points
    assert any("main possible options" in p.casefold() for p in points), points
    assert any("which option" in p.casefold() for p in points), points
    assert parsed["response_to_participant"]["steps"] == []
    joined = " ".join(points)
    assert not re.search(r"rightsignature|\bform\b", joined, re.I)


def test_termination_informational_path_does_not_strip_source_options(engine):
    """GREEN-GUARD: retired/termination informational answers are not the
    in-service options rewrite. Known-zero must not silently delete education
    on that path."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    collected["participant_data"]["employment_status"] = "Terminated"
    profile = {
        "primary_action": "termination_rollover",
        "inquiry_intent": "informational_options",
        "signals": {
            "termination_distribution": True,
            "procedure_requested": False,
            "incoming_rollover": False,
        },
    }
    parsed, _ = engine._apply_termination_response_policy(
        parsed=_draft(), retrieval_profile=profile, collected_data=collected,
    )
    points = parsed["response_to_participant"]["key_points"]
    assert any(ROLLOVER_SOURCE.search(p) for p in points), points
    assert parsed["response_to_participant"]["steps"] == []


def test_known_positive_rollover_source_remains_available(engine):
    """GREEN-GUARD: a non-zero known source must keep its option."""
    points, _ = _apply(engine, _collected(rollover=KNOWN_POSITIVE, age_59_5=False))
    assert any(ROLLOVER_SOURCE.search(p) for p in points), points


def test_unknown_rollover_source_is_neither_zeroed_nor_turned_into_a_question(engine):
    """GREEN-GUARD: unknown stays an option and never becomes a participant
    questionnaire item."""
    collected = _collected(rollover=UNKNOWN, age_59_5=False)
    parsed, _ = engine._apply_termination_response_policy(
        parsed=_draft(), retrieval_profile=_profile(engine, collected),
        collected_data=collected,
    )
    points = parsed["response_to_participant"]["key_points"]
    assert any(ROLLOVER_SOURCE.search(p) for p in points), points
    assert parsed["questions_to_ask"] == []


def test_59_5_or_older_keeps_the_age_based_in_service_option(engine):
    """GREEN-GUARD: general education about the option survives when it applies."""
    points, _ = _apply(engine, _collected(rollover=KNOWN_POSITIVE, age_59_5=True))
    assert any(AGE_IN_SERVICE.search(p) for p in points), points


def test_missing_age_flag_keeps_the_general_option_list_intact(engine):
    """GREEN-GUARD: absent derived age must not silently exclude an option."""
    points, _ = _apply(engine, _collected(rollover=UNKNOWN, age_59_5=None))
    assert any(AGE_IN_SERVICE.search(p) for p in points), points


def test_rewritten_option_list_keeps_coverage_answer_reference_in_response(engine):
    """RC-5: rewriting key_points[0] must keep answer_reference a substring of
    the final participant response, or coverage flips to needs_verification."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    draft = _draft()
    draft["question_coverage"] = [{
        "question_index": 0,
        "status": "answered",
        "answer_reference": OBSERVED_KEY_POINTS[0],
    }]
    parsed, _ = engine._apply_termination_response_policy(
        parsed=draft, retrieval_profile=_profile(engine, collected),
        collected_data=collected,
    )
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("human_review_required") is False, validated
    coverage = validated["question_coverage"]
    assert coverage[0]["status"] == "answered"
    response_text = json.dumps(
        parsed.get("response_to_participant") or {}, ensure_ascii=False,
    )
    assert coverage[0]["answer_reference"].casefold() in response_text.casefold()


def _apply_custom(engine, collected, key_points, coverage=None, questions=None):
    draft = _draft()
    draft["response_to_participant"]["key_points"] = copy.deepcopy(key_points)
    if coverage is not None:
        draft["question_coverage"] = copy.deepcopy(coverage)
    if questions is not None:
        collected = copy.deepcopy(collected)
        collected.setdefault("internal_response_context", {})
        collected["internal_response_context"]["requested_questions"] = questions
    parsed, info = engine._apply_termination_response_policy(
        parsed=draft, retrieval_profile=_profile(engine, collected),
        collected_data=collected,
    )
    return parsed, info


def _string_points(parsed):
    return [
        item for item in parsed["response_to_participant"]["key_points"]
        if isinstance(item, str)
    ]


def test_rv1_mixed_clause_does_not_leave_unqualified_zero_balance(engine):
    """RV-1: stripping the source qualifier must not leave a false account $0."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "A 401(k) loan may be available, and your rollover source balance is $0.",
        "Hardship withdrawals require an immediate and serious financial need.",
    ])
    joined = " ".join(_string_points(parsed))
    assert re.search(r"\bloan\b", joined, re.I)
    assert not re.search(r"your balance is\s*\$0", joined, re.I), joined
    assert "$0" not in joined
    assert not ROLLOVER_SOURCE.search(joined)


def test_rv1_second_sentence_zero_is_not_dequalified(engine):
    """RV-1 paraphrase: leftover 0.00 must not become an unqualified balance."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "A 401(k) loan may be available. Your rollover-source balance is currently 0.00.",
    ])
    joined = " ".join(_string_points(parsed))
    assert re.search(r"\bloan\b", joined, re.I)
    assert "0.00" not in joined
    assert "your balance is currently" not in joined.casefold()
    assert not ROLLOVER_SOURCE.search(joined)


def test_rv2_paraphrase_list_is_not_corrupted(engine):
    """RV-2: list paraphrases must stay grammatical, not 'distributionss'."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "While you are still working, the routes that may exist are hardship "
        "withdrawals, plan loans, rollover source distributions and in-service "
        "distributions once you reach 59½.",
    ])
    joined = " ".join(_string_points(parsed))
    assert "distributionss" not in joined.casefold()
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)
    assert not ROLLOVER_SOURCE.search(joined)
    assert not re.search(r"\bare\s*,", joined, re.I)


def test_rv2_leading_inapplicable_keeps_conjunction_grammar(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "Your choices are an in-service distribution, a rollover-source "
        "withdrawal, a hardship withdrawal or a loan.",
    ])
    joined = " ".join(_string_points(parsed))
    assert not re.search(r"choices are,", joined, re.I), joined
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)
    assert not ROLLOVER_SOURCE.search(joined)
    assert not AGE_IN_SERVICE.search(joined)


def test_rv2_semicolon_list_does_not_leave_empty_slots(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "Options: hardship withdrawal; 401(k) loan; rollover-source "
        "withdrawal; in-service distribution.",
    ])
    joined = " ".join(_string_points(parsed))
    assert ";;" not in joined
    assert not joined.strip().endswith(";.")
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)
    assert not ROLLOVER_SOURCE.search(joined)


def test_rv3_deleted_second_question_is_not_reanchored(engine):
    """RV-3: dropping the rollover-money answer must need review, not a substitute."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(
        engine,
        collected,
        OBSERVED_KEY_POINTS,
        coverage=[
            {
                "question_index": 0,
                "status": "answered",
                "answer_reference": OBSERVED_KEY_POINTS[0],
            },
            {
                "question_index": 1,
                "status": "answered",
                "answer_reference": OBSERVED_KEY_POINTS[3],
            },
        ],
    )
    coverage_collected = copy.deepcopy(collected)
    coverage_collected["internal_response_context"]["requested_questions"] = [
        "I am still employed. What options do I have to access funds?",
        "Do I have any rollover money in the plan?",
    ]
    validated = engine._validate_question_coverage(parsed, coverage_collected)
    assert validated["human_review_required"] is True, validated
    q0, q1 = validated["question_coverage"]
    assert q0["status"] == "answered"
    assert q1["status"] == "needs_verification"
    assert "main possible options" not in q1["answer_reference"].casefold()
    assert q0["answer_reference"].casefold() in json.dumps(
        parsed.get("response_to_participant") or {}, ensure_ascii=False,
    ).casefold()


def test_rv4_unsolicited_accurate_denial_is_omitted(engine):
    """BD2: an age denial the inquiry did not name is omitted."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "Because you are under age 59½, an age-based in-service distribution "
        "is not available to you.",
        "A 401(k) loan may be available if the plan allows loans.",
    ])
    joined = " ".join(_string_points(parsed))
    assert not AGE_IN_SERVICE.search(joined)
    assert not re.search(r"not available", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)


def test_rv4_unsolicited_future_eligibility_note_is_omitted(engine):
    """BD2: an unsolicited future-eligibility note still names the option."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "Once you reach 59½ you may become eligible for an in-service "
        "distribution if the plan allows it.",
        "Hardship withdrawals require an immediate and serious financial need.",
    ])
    joined = " ".join(_string_points(parsed))
    assert not AGE_IN_SERVICE.search(joined)
    assert not re.search(r"once you reach", joined, re.I)
    assert re.search(r"hardship", joined, re.I)


def test_rv5_dict_point_is_rewritten_or_guardrail_stays_honest(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(engine, collected, [
        {
            "text": "Options include a hardship withdrawal, a 401(k) loan, a "
            "rollover-source withdrawal, and an in-service distribution."
        },
        OBSERVED_KEY_POINTS[0],
        "Hardship withdrawals require an immediate and serious financial need.",
    ])
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    guardrails = " ".join(parsed.get("guardrails_applied") or []).casefold()
    still_offers_inapplicable = bool(
        ROLLOVER_SOURCE.search(body) or AGE_IN_SERVICE.search(body)
    )
    if still_offers_inapplicable:
        assert "did not present inapplicable rollover source" not in guardrails
        assert "did not present inapplicable age based in service" not in guardrails
        assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    else:
        assert re.search(r"hardship", body, re.I)
        assert re.search(r"\bloan", body, re.I)
        assert any(isinstance(item, dict) for item in parsed["response_to_participant"]["key_points"])


def test_rv6_observed_enumeration_keeps_a_conjunction(engine):
    points, _ = _apply(engine, _collected(rollover=KNOWN_ZERO, age_59_5=False))
    enumeration = next(p for p in points if "main possible options" in p.casefold())
    assert re.search(
        r"hardship withdrawal(?:,)?\s+(?:and|or)\s+a?\s*401\(k\) loan",
        enumeration,
        re.I,
    )
    assert not re.search(r"withdrawal,\s+a 401\(k\) loan;", enumeration, re.I)


def test_rewrite_is_deterministic_across_hash_seeds(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    first = None
    for seed in (0, 1, 7, 42):
        import random
        random.seed(seed)
        points, _ = _apply(engine, collected)
        if first is None:
            first = points
        else:
            assert points == first


def test_procedure_requested_path_does_not_rewrite_options(engine):
    """R9: a procedure request leaves the option text untouched."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    profile = _profile(engine, collected)
    profile.setdefault("signals", {})["procedure_requested"] = True
    parsed, _ = engine._apply_termination_response_policy(
        parsed=_draft(), retrieval_profile=profile, collected_data=collected,
    )
    assert parsed["response_to_participant"]["key_points"] == OBSERVED_KEY_POINTS


NESTED_OPTIONS = {
    "heading": "Your options",
    "items": [
        "a hardship withdrawal",
        "a 401(k) loan",
        "a rollover-source withdrawal",
        "an in-service distribution",
    ],
}
HARDSHIP_ANCHOR = (
    "A hardship withdrawal requires documentation and a qualifying need."
)
INPLACE_BALANCE_CLAUSE = (
    "A 401(k) loan may be available if the plan allows loans, and a "
    "rollover-source withdrawal applies only to money previously rolled "
    "into this plan."
)
UNRECOGNIZED_OPTION_INTRO = (
    "Among the products a participant might use: a hardship withdrawal, "
    "a 401(k) loan, a rollover-source withdrawal, and an in-service "
    "distribution."
)


def test_rv5_nested_heading_items_does_not_claim_false_guardrail(engine):
    """RV-5: option names under heading/items must not earn withheld-option guardrails."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [NESTED_OPTIONS, HARDSHIP_ANCHOR],
        coverage=[{
            "question_index": 0,
            "status": "answered",
            "answer_reference": HARDSHIP_ANCHOR,
        }],
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    guardrails = " ".join(parsed.get("guardrails_applied") or []).casefold()
    still_offers_inapplicable = bool(
        ROLLOVER_SOURCE.search(body) or AGE_IN_SERVICE.search(body)
    )
    if still_offers_inapplicable:
        assert "did not present inapplicable rollover source" not in guardrails
        assert "did not present inapplicable age based in service" not in guardrails
        assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
        assert "age_based_in_service" not in (info.get("inapplicable_options_removed") or [])
        assert info.get("inapplicable_options_unresolved")
    else:
        assert re.search(r"hardship", body, re.I)
        assert re.search(r"\bloan", body, re.I)
    validated = engine._validate_question_coverage(parsed, collected)
    if info.get("inapplicable_options_unresolved"):
        assert validated.get("human_review_required") is True, validated
    assert parsed["questions_to_ask"] == []


def test_rv5_nested_list_item_is_visible_to_honesty_scan(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(engine, collected, [{"items": NESTED_OPTIONS["items"]}])
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    guardrails = " ".join(parsed.get("guardrails_applied") or []).casefold()
    still_offers_inapplicable = bool(
        ROLLOVER_SOURCE.search(body) or AGE_IN_SERVICE.search(body)
    )
    if still_offers_inapplicable:
        assert "did not present inapplicable rollover source" not in guardrails
        assert info.get("inapplicable_options_unresolved")
    validated = engine._validate_question_coverage(parsed, collected)
    if info.get("inapplicable_options_unresolved"):
        assert validated.get("human_review_required") is True, validated


def test_h2_unsolicited_non_offer_is_omitted_without_zero_figure(engine):
    """BD2: an unsolicited known-zero denial is omitted, and no zero figure remains."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    non_offer = (
        "A rollover-source withdrawal does not apply to you; your rollover "
        "source balance on file is $0.00."
    )
    parsed, _ = _apply_custom(engine, collected, [
        "The main possible options are a hardship withdrawal or a 401(k) loan.",
        non_offer,
        HARDSHIP_ANCHOR,
    ])
    points = _string_points(parsed)
    joined = " ".join(points)
    assert not ROLLOVER_SOURCE.search(joined), points
    assert not re.search(r"does not apply", joined, re.I), points
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)
    assert not re.search(r"\$\s*0(?:\.0+)?\b|\b0\.0+\b|\bbalance on file is zero\b", joined, re.I), points
    assert not re.search(r"your (?:current )?balance is", joined, re.I), points
    assert "20000" not in joined


def test_rv3_inplace_clause_removal_does_not_answer_balance_question(engine):
    """In-place clause drop must not treat leftover loan text as the balance answer."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(
        engine,
        collected,
        [OBSERVED_KEY_POINTS[0], INPLACE_BALANCE_CLAUSE, HARDSHIP_ANCHOR],
        coverage=[
            {
                "question_index": 0,
                "status": "answered",
                "answer_reference": OBSERVED_KEY_POINTS[0],
            },
            {
                "question_index": 1,
                "status": "answered",
                "answer_reference": INPLACE_BALANCE_CLAUSE,
            },
        ],
        questions=[
            "I am still employed. What options do I have to access funds?",
            "Do I have any rollover money in the plan?",
        ],
    )
    coverage_collected = copy.deepcopy(collected)
    coverage_collected["internal_response_context"]["requested_questions"] = [
        "I am still employed. What options do I have to access funds?",
        "Do I have any rollover money in the plan?",
    ]
    validated = engine._validate_question_coverage(parsed, coverage_collected)
    q0, q1 = validated["question_coverage"]
    assert q0["status"] == "answered"
    assert q1["status"] == "needs_verification"
    assert validated["human_review_required"] is True
    assert "main possible options" not in q1["answer_reference"].casefold()
    remaining = " ".join(_string_points(parsed)).casefold()
    if "loan may be available" in remaining:
        assert q1["answer_reference"].casefold() != (
            "a 401(k) loan may be available if the plan allows loans."
        )
    assert q0["answer_reference"].casefold() in json.dumps(
        parsed.get("response_to_participant") or {}, ensure_ascii=False,
    ).casefold()


def test_unresolved_inapplicable_options_require_human_review(engine):
    """Fail-open leftovers must use the review metadata path, not diagnostics-only."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [UNRECOGNIZED_OPTION_INTRO, HARDSHIP_ANCHOR],
        coverage=[{
            "question_index": 0,
            "status": "answered",
            "answer_reference": HARDSHIP_ANCHOR,
        }],
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body)
    assert info.get("inapplicable_options_unresolved")
    assert "did not present inapplicable rollover source" not in " ".join(
        parsed.get("guardrails_applied") or []
    ).casefold()
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("human_review_required") is True, validated
    assert parsed["questions_to_ask"] == []


CLEAN_HARDSHIP_COVERAGE = [{
    "question_index": 0,
    "status": "answered",
    "answer_reference": HARDSHIP_ANCHOR,
}]
NESTED_OFFER_PLUS_AGE_NOTE = {
    "heading": "What you can consider",
    "items": [
        "a hardship withdrawal",
        "a 401(k) loan",
        "a withdrawal of rollover-source money",
    ],
    "note": "An age-based in-service distribution does not apply to you yet.",
}
NESTED_OFFER_PLUS_FUTURE_NOTE = {
    "heading": "What you can consider",
    "items": [
        "a hardship withdrawal",
        "a 401(k) loan",
        "a withdrawal of rollover-source money",
    ],
    "footnote": (
        "You may become eligible for other routes when you reach age 59 1/2."
    ),
}


def _guardrail_text(parsed) -> str:
    return " ".join(parsed.get("guardrails_applied") or []).casefold()


def test_nested_offer_is_not_hidden_by_unrelated_non_offer_note(engine):
    """Minimal pair: nested rollover offer + age non-offer must not claim withheld."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [NESTED_OFFER_PLUS_AGE_NOTE, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body), body
    assert "did not present inapplicable rollover source" not in _guardrail_text(parsed)
    assert "rollover_source" in (info.get("inapplicable_options_unresolved") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated
    assert parsed["questions_to_ask"] == []


def test_nested_offer_is_not_hidden_by_unrelated_future_eligibility_note(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [NESTED_OFFER_PLUS_FUTURE_NOTE, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body), body
    assert "did not present inapplicable rollover source" not in _guardrail_text(parsed)
    assert "rollover_source" in (info.get("inapplicable_options_unresolved") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated


def test_same_leaf_rollover_offer_is_not_hidden_by_age_non_offer(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [
            "You may take a withdrawal of rollover-source money, but an "
            "age-based in-service distribution does not apply to you yet.",
            HARDSHIP_ANCHOR,
        ],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body), body
    assert "did not present inapplicable rollover source" not in _guardrail_text(parsed)
    assert "rollover_source" in (info.get("inapplicable_options_unresolved") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated


def test_same_leaf_age_offer_is_not_hidden_by_rollover_non_offer(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [
            "An age-based in-service distribution may be available, but a "
            "rollover-source withdrawal does not apply to you.",
            HARDSHIP_ANCHOR,
        ],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert AGE_IN_SERVICE.search(body), body
    assert "did not present inapplicable age based in service" not in _guardrail_text(parsed)
    assert "age_based_in_service" in (info.get("inapplicable_options_unresolved") or [])
    assert "age_based_in_service" not in (info.get("inapplicable_options_removed") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated


@pytest.mark.parametrize(
    "non_offer",
    [
        "A rollover-source withdrawal does not apply to you because your "
        "rollover source balance on file is $0.00.",
        "A rollover-source withdrawal is not an option, since your rollover "
        "source balance is $0.00.",
        "A rollover-source withdrawal does not apply to you - your rollover "
        "source balance is $0.00.",
        "A rollover-source withdrawal does not apply given your rollover "
        "source balance on file is $0.00.",
    ],
)
def test_known_zero_non_offer_does_not_keep_source_figure(engine, non_offer):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [
            "The main possible options are a hardship withdrawal or a 401(k) loan.",
            non_offer,
            HARDSHIP_ANCHOR,
        ],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    joined = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert not ROLLOVER_SOURCE.search(joined), joined
    assert not re.search(r"does not apply|is not an option", joined, re.I), joined
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)
    assert not re.search(r"\$\s*0(?:\.0+)?\b|\b0\.0+\b", joined), joined
    assert "your current balance is" not in joined.casefold()
    assert "20000" not in joined
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is False, validated


def test_known_zero_strip_preserves_unrelated_fee_figure(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "A rollover-source withdrawal does not apply to you because your "
        "rollover source balance on file is $0.00. A hardship withdrawal may "
        "include a $50 administrative amount.",
        HARDSHIP_ANCHOR,
    ])
    joined = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert "$50" in joined
    assert not re.search(r"\$\s*0(?:\.0+)?\b|\b0\.0+\b", joined), joined
    assert not re.search(r"does not apply", joined, re.I)
    assert not ROLLOVER_SOURCE.search(joined)


# --- Honesty blockers: one inapplicable subject; truncation must fail open. --
ONE_INAPPLICABLE = dict(rollover=KNOWN_ZERO, age_59_5=True)
MIXED_DEPTH_OFFER = {
    "heading": "What you can consider",
    "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {
        "text": "You may take a withdrawal of rollover-source money."
    }}}}}}}}},
}


def _assert_no_false_withheld_rollover(parsed, info):
    assert "did not present inapplicable rollover source" not in _guardrail_text(parsed)
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    assert "rollover_source" in (info.get("inapplicable_options_unresolved") or [])
    assert parsed.get("inapplicable_options_unresolved")
    assert parsed["questions_to_ask"] == []


@pytest.mark.parametrize(
    "sentence",
    [
        "A 401(k) loan is not available under your plan, but you may "
        "take a withdrawal of rollover-source money.",
        "A 401(k) loan is not available under your plan, however you may "
        "take a withdrawal of rollover-source money.",
        "A hardship withdrawal does not apply to you while a withdrawal of "
        "rollover-source money is still open to you.",
    ],
)
def test_loan_or_hardship_denial_does_not_exempt_same_sentence_rollover_offer(
    engine, sentence,
):
    """Exactly one inapplicable option: a denial about loan/hardship must not
    hide a same-unit rollover offer, and must not claim it was withheld."""
    collected = _collected(**ONE_INAPPLICABLE)
    parsed, info = _apply_custom(
        engine, collected, [sentence, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body), body
    assert re.search(r"not available|does not apply", body, re.I), body
    _assert_no_false_withheld_rollover(parsed, info)
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated


def test_inverse_rollover_non_offer_with_loan_offer_fails_open_to_review(engine):
    """Inverse mix: non-offer of the inapplicable option plus an applicable
    offer in the same sentence is mixed-subject, not a silent withheld claim."""
    collected = _collected(**ONE_INAPPLICABLE)
    sentence = (
        "A rollover-source withdrawal is not available under your plan, but "
        "you may take a 401(k) loan."
    )
    parsed, info = _apply_custom(
        engine, collected, [sentence, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body), body
    assert re.search(r"not available", body, re.I), body
    assert re.search(r"\bloan", body, re.I), body
    _assert_no_false_withheld_rollover(parsed, info)
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated


def test_shallow_leaf_cannot_hide_truncated_depth_offer(engine):
    """A heading at depth 1 plus an offer past leaf-walk max depth must not
    imply absence or ship a withheld-option guardrail."""
    collected = _collected(**ONE_INAPPLICABLE)
    parsed, info = _apply_custom(
        engine, collected, [MIXED_DEPTH_OFFER, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert ROLLOVER_SOURCE.search(body), body
    _assert_no_false_withheld_rollover(parsed, info)
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated
    leaves, truncated = engine._inservice_text_leaves(MIXED_DEPTH_OFFER)
    assert truncated is True
    assert any("What you can consider" in leaf for leaf in leaves)


# --- Prose: offer-first mixed clauses must not be treated as enumerations. --
OFFER_FIRST_MIXED = (
    "You may take a withdrawal of rollover-source money, but a 401(k) loan "
    "is not available under your plan."
)
GARBLED_MAY_TAKE_BUT = (
    "You may take but a 401(k) loan is not available under your plan."
)
GENUINE_MAY_TAKE_ENUMERATION = (
    "You may take a hardship withdrawal, a 401(k) loan, or a withdrawal of "
    "rollover-source money."
)


def test_offer_first_mixed_clause_is_not_rewritten_into_may_take_but(engine):
    """Contrastive offer-first prose is not a two-item option list. Ambiguous
    rewrite must keep the original sentence and fail open to review."""
    collected = _collected(**ONE_INAPPLICABLE)
    parsed, info = _apply_custom(
        engine, collected, [OFFER_FIRST_MIXED, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    body = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert GARBLED_MAY_TAKE_BUT not in body, body
    assert "may take but" not in body.casefold(), body
    assert OFFER_FIRST_MIXED in parsed["response_to_participant"]["key_points"], body
    assert ROLLOVER_SOURCE.search(body), body
    _assert_no_false_withheld_rollover(parsed, info)
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is True, validated


def test_genuine_may_take_enumeration_still_drops_inapplicable_options(engine):
    """A real option list after 'may take' may still drop the inapplicable item."""
    collected = _collected(**ONE_INAPPLICABLE)
    parsed, info = _apply_custom(
        engine, collected, [GENUINE_MAY_TAKE_ENUMERATION, HARDSHIP_ANCHOR],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    points = parsed["response_to_participant"]["key_points"]
    body = json.dumps(points, ensure_ascii=False)
    assert GARBLED_MAY_TAKE_BUT not in body, body
    assert "may take but" not in body.casefold(), body
    assert not ROLLOVER_SOURCE.search(body), body
    assert re.search(r"hardship", body, re.I), body
    assert re.search(r"\bloan", body, re.I), body
    enumeration = next(
        item for item in points
        if isinstance(item, str) and "may take" in item.casefold()
    )
    assert re.search(
        r"hardship withdrawal(?:,)?\s+(?:and|or)\s+a?\s*401\(k\) loan",
        enumeration,
        re.I,
    ), enumeration
    assert "rollover_source" in (info.get("inapplicable_options_removed") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("incomplete_question_count") == 0, validated
    assert validated.get("human_review_required") is False, validated


# --- BD2: omit unsolicited inapplicable options; denial wording is not a license. ---
C590_LIVE_KEY_POINTS = [
    "1. Options: At age 46, a standard age-based in-service distribution is "
    "generally not available; the options worth reviewing are a hardship "
    "withdrawal, if you have an IRS-approved hardship and the plan allows it, "
    "and a 401(k) loan, if the plan and participant-level loan checks allow "
    "it. A rollover-source withdrawal is not applicable based on current "
    "source information.",
    "Hardship withdrawals must be limited to the amount necessary to meet the "
    "financial need and must fit one of the IRS safe-harbor reasons: "
    "unreimbursed medical expenses, next-12-month post-secondary education "
    "expenses, purchase of a primary residence, preventing eviction or "
    "foreclosure on a primary residence, qualifying funeral or burial "
    "expenses, or qualifying casualty-damage repairs to a primary residence.",
    "For a 401(k) loan, the plan must allow loans, the maximum number of loans "
    "must be greater than zero, you must be under the plan’s active-loan "
    "limit, and the vested balance requirement must be met; if allowed, there "
    "is no credit check and repayment is through payroll deductions.",
    "Eligible hardship sources and any available amount are plan-specific, "
    "including whether employer match sources can be used.",
]
UNSOLICITED_DENIAL_PHRASINGS = [
    "A rollover-source withdrawal does not apply to you.",
    "A rollover-source withdrawal is not an option.",
    "A rollover-source withdrawal is not available.",
    "A rollover-source withdrawal is not applicable.",
    "A rollover-source withdrawal isn't applicable.",
    "A rollover-source withdrawal does not apply given current source information.",
]
APPLICABLE_OPTION_LIST = (
    "The main possible options are a hardship withdrawal or a 401(k) loan."
)


def _participant_visible(parsed) -> str:
    response = parsed["response_to_participant"]
    return " ".join([
        response.get("opening") or "",
        json.dumps(response.get("key_points") or [], ensure_ascii=False),
        json.dumps(response.get("warnings") or [], ensure_ascii=False),
    ])


def test_r1_c590_unsolicited_rollover_denial_is_omitted(engine):
    """R1: the captured c590 denial is unsolicited, so rollover-source leaves
    opening, key points, and warnings. Hardship and loan, both explanations,
    the active-status opening, the numeric marker, and the invite stay."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(engine, collected, C590_LIVE_KEY_POINTS)
    response = parsed["response_to_participant"]
    points = _string_points(parsed)
    joined = " ".join(points)
    visible = _participant_visible(parsed)
    assert not ROLLOVER_SOURCE.search(visible), visible
    assert not AGE_IN_SERVICE.search(visible), visible
    assert "active employee" in (response.get("opening") or "").casefold()
    assert points[0].startswith("1. "), points[0]
    assert "safe-harbor" in points[1], points[1]
    assert "maximum number of loans" in points[2], points[2]
    assert _options_named(points) == {"hardship", "loan"}, points
    assert re.search(r"hardship", joined, re.I), points
    assert re.search(r"\bloan", joined, re.I), points
    assert any("which option" in point.casefold() for point in points), points
    assert info.get("inapplicable_options_removed") == [
        "age_based_in_service",
        "rollover_source",
    ], info
    assert info.get("inapplicable_options_unresolved") in (None, []), info
    assert parsed.get("inapplicable_options_unresolved") in (None, []), parsed


def test_r4_unsolicited_denial_phrasings_share_one_participant_outcome(engine):
    """R4: six denial wordings of an unsolicited rollover-source option produce
    the same participant-visible text, and none of them names the option."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    outcomes = []
    for phrase in UNSOLICITED_DENIAL_PHRASINGS:
        parsed, _info = _apply_custom(engine, collected, [
            APPLICABLE_OPTION_LIST,
            phrase,
        ])
        response = parsed["response_to_participant"]
        visible = {
            "opening": response.get("opening"),
            "key_points": response.get("key_points"),
            "warnings": response.get("warnings"),
        }
        outcomes.append(visible)
        assert not ROLLOVER_SOURCE.search(json.dumps(visible, ensure_ascii=False)), phrase
    assert outcomes[1:] == [outcomes[0]] * (len(outcomes) - 1), outcomes


def test_r7_first_sentence_refusal_still_evaluates_later_sentences(engine):
    """R7: a first sentence that cannot be clause-split must not block a later
    unsolicited inapplicable sentence."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    first = (
        "A hardship withdrawal and a rollover-source withdrawal can both be "
        "discussed with the plan."
    )
    later = (
        "A rollover-source withdrawal is not applicable based on current "
        "source information."
    )
    parsed, _info = _apply_custom(engine, collected, [f"{first} {later}"])
    joined = " ".join(_string_points(parsed))
    assert later not in joined, joined
    assert "not applicable" not in joined.casefold(), joined
    assert re.search(r"hardship", joined, re.I), joined
    assert first in joined, joined


def test_r10_removed_names_are_absent_from_every_participant_field(engine):
    """R10: a name in inapplicable_options_removed is absent from opening,
    key points, and warnings together."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    draft = _draft()
    draft["response_to_participant"]["opening"] = (
        "An age-based in-service distribution is not available to you."
    )
    draft["response_to_participant"]["warnings"] = [
        "A rollover-source withdrawal is not applicable based on current "
        "source information."
    ]
    draft["response_to_participant"]["key_points"] = [APPLICABLE_OPTION_LIST]
    parsed, info = engine._apply_termination_response_policy(
        parsed=draft,
        retrieval_profile=_profile(engine, collected),
        collected_data=collected,
    )
    visible = _participant_visible(parsed)
    removed = info.get("inapplicable_options_removed") or []
    assert removed == ["age_based_in_service", "rollover_source"], removed
    assert not AGE_IN_SERVICE.search(visible), visible
    assert not ROLLOVER_SOURCE.search(visible), visible


def test_r2_known_positive_rollover_source_is_not_suppressed(engine):
    """R2: a known-positive rollover source stays in the option list."""
    collected = _collected(rollover=KNOWN_POSITIVE, age_59_5=False)
    parsed, info = _apply_custom(engine, collected, [
        "The main possible options are a hardship withdrawal, a 401(k) loan, "
        "or a rollover-source withdrawal.",
    ])
    joined = " ".join(_string_points(parsed))
    assert ROLLOVER_SOURCE.search(joined), joined
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])


def test_r3_unknown_rollover_source_is_not_suppressed_or_zeroed(engine):
    """R3: unknown source stays an option and is not turned into a question or a zero."""
    collected = _collected(rollover=UNKNOWN, age_59_5=False)
    parsed, info = _apply_custom(engine, collected, [
        "The main possible options are a hardship withdrawal, a 401(k) loan, "
        "or a rollover-source withdrawal.",
    ])
    joined = " ".join(_string_points(parsed))
    assert ROLLOVER_SOURCE.search(joined), joined
    assert parsed["questions_to_ask"] == []
    assert not re.search(r"\$\s*0(?:\.0+)?\b|\bbalance on file is zero\b|\bzero\b", joined, re.I), joined
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])


def test_r5_explicit_rollover_source_request_keeps_truthful_denial(engine):
    """R5: naming rollover-source keeps the denial, including the c590 wording."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [
            "A rollover-source withdrawal is not applicable based on current "
            "source information.",
            "A 401(k) loan may be available if the plan allows loans.",
        ],
        questions=[
            "Is a rollover-source withdrawal available while I am employed?"
        ],
    )
    joined = " ".join(_string_points(parsed))
    assert ROLLOVER_SOURCE.search(joined), joined
    assert re.search(r"not applicable", joined, re.I), joined
    assert re.search(r"\bloan", joined, re.I), joined
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])
    assert "rollover_source" not in (info.get("inapplicable_options_removed") or [])


def test_r5_explicit_age_based_request_keeps_truthful_denial(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [
            "Because you are under age 59½, an age-based in-service distribution "
            "is not available to you.",
            "A 401(k) loan may be available if the plan allows loans.",
        ],
        questions=["Is an age-based in-service distribution available to me?"],
    )
    joined = " ".join(_string_points(parsed))
    assert AGE_IN_SERVICE.search(joined), joined
    assert re.search(r"not available", joined, re.I), joined
    assert re.search(r"\bloan", joined, re.I), joined
    assert "age_based_in_service" not in (info.get("inapplicable_options_unresolved") or [])
    assert "age_based_in_service" not in (info.get("inapplicable_options_removed") or [])


def test_r5_explicit_request_keeps_future_eligibility_note(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    note = (
        "Once you reach 59½ you may become eligible for an in-service "
        "distribution if the plan allows it."
    )
    parsed, _info = _apply_custom(
        engine,
        collected,
        [note, "Hardship withdrawals require an immediate and serious financial need."],
        questions=["When would an in-service distribution become available?"],
    )
    joined = " ".join(_string_points(parsed))
    assert note in joined, joined
    assert re.search(r"hardship", joined, re.I)


def test_r5_generic_rollover_word_does_not_keep_rollover_source_denial(engine):
    """A generic rollover word is not an explicit rollover-source request."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _info = _apply_custom(
        engine,
        collected,
        [
            "A rollover-source withdrawal is not applicable based on current "
            "source information.",
            APPLICABLE_OPTION_LIST,
        ],
        questions=["What rollover choices do I have while I am employed?"],
    )
    joined = " ".join(_string_points(parsed))
    assert not ROLLOVER_SOURCE.search(joined), joined
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)


def test_r5_assistant_prose_does_not_count_as_an_explicit_request(engine):
    """The draft may name the option; only the inquiry keeps the denial."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _info = _apply_custom(engine, collected, [
        "A rollover-source withdrawal does not apply to you.",
        APPLICABLE_OPTION_LIST,
    ])
    joined = " ".join(_string_points(parsed))
    assert not ROLLOVER_SOURCE.search(joined), joined


@pytest.mark.parametrize(
    "non_offer",
    [
        "A rollover-source withdrawal does not apply to you because your "
        "rollover source balance on file is $0.00.",
        "A rollover-source withdrawal is not an option, since your rollover "
        "source balance is $0.00.",
        "A rollover-source withdrawal is not applicable because your rollover "
        "source balance on file is $0.00.",
        "A rollover-source withdrawal isn't applicable; your rollover source "
        "balance on file is $0.00.",
    ],
)
def test_r5_explicit_request_keeps_denial_without_zero_figure(engine, non_offer):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(
        engine,
        collected,
        [APPLICABLE_OPTION_LIST, non_offer, HARDSHIP_ANCHOR],
        questions=["Can I take a rollover-source withdrawal?"],
        coverage=CLEAN_HARDSHIP_COVERAGE,
    )
    joined = json.dumps(parsed["response_to_participant"]["key_points"], ensure_ascii=False)
    assert re.search(r"does not apply|is not an option|not applicable|isn't applicable", joined, re.I), joined
    assert ROLLOVER_SOURCE.search(joined)
    assert re.search(r"hardship", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)
    assert not re.search(r"\$\s*0(?:\.0+)?\b|\b0\.0+\b", joined), joined
    assert "your current balance is" not in joined.casefold()
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])
    validated = engine._validate_question_coverage(parsed, collected)
    assert validated.get("human_review_required") is False, validated


def test_r6_c590_removal_keeps_a_conjunction_and_clean_prose(engine):
    """R6: dropping the unsolicited options leaves a grammatical list."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _info = _apply_custom(engine, collected, C590_LIVE_KEY_POINTS)
    joined = " ".join(_string_points(parsed))
    assert re.search(
        r"hardship withdrawal, if you have an IRS-approved hardship and the "
        r"plan allows it, and a 401\(k\) loan",
        joined,
        re.I,
    ), joined
    assert not type(engine)._INSERVICE_CORRUPT_PROSE.search(joined), joined
    assert ";;" not in joined
    assert not re.search(r",\s+and\s*[.]", joined), joined


def test_r8_known_zero_omission_does_not_disclose_a_zero_figure(engine):
    """R8: omitting the unsolicited option does not leave an account zero."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _info = _apply_custom(engine, collected, [
        APPLICABLE_OPTION_LIST,
        "A rollover-source withdrawal is not applicable because your rollover "
        "source balance on file is zero.",
        "Your rollover-source balance on file is $0.",
    ])
    joined = " ".join(_string_points(parsed))
    assert not ROLLOVER_SOURCE.search(joined), joined
    assert "$0" not in joined
    assert "balance on file is zero" not in joined.casefold()
    assert not re.search(r"\b0\.0+\b", joined)


def test_r11_age_based_bookkeeping_follows_the_rollover_omission_rule(engine):
    """R11: under 59½, age-based stays out of the offered set. Removal is
    claimed only when the name is gone from participant text."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_custom(engine, collected, C590_LIVE_KEY_POINTS)
    visible = _participant_visible(parsed)
    assert "age_based_in_service" not in (info.get("inapplicable_options_unresolved") or [])
    assert not info.get("inapplicable_options_unresolved")
    removed = info.get("inapplicable_options_removed") or []
    if "age_based_in_service" in removed:
        assert not AGE_IN_SERVICE.search(visible), visible
    else:
        assert AGE_IN_SERVICE.search(visible), visible


def _apply_points(engine, collected, key_points, *, profile=None, questions=None, draft_over=None):
    draft = _draft()
    draft["response_to_participant"]["key_points"] = copy.deepcopy(key_points)
    if questions is not None:
        collected = copy.deepcopy(collected)
        collected.setdefault("internal_response_context", {})
        collected["internal_response_context"]["requested_questions"] = questions
    if draft_over:
        for key, value in draft_over.items():
            if key == "response_to_participant":
                draft["response_to_participant"].update(value)
            else:
                draft[key] = copy.deepcopy(value)
    return engine._apply_termination_response_policy(
        parsed=draft,
        retrieval_profile=profile or _profile(engine, collected),
        collected_data=collected,
    )


def test_b1_untracked_tax_penalty_and_fee_clauses_survive(engine):
    """B1: dropping an inapplicable clause must keep later tax, penalty, and fee prose."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    tax = (
        "A hardship withdrawal may be available, and an age-based in-service "
        "distribution is not available, and taxes and penalties may apply to "
        "any distribution."
    )
    fee = (
        "A hardship withdrawal may be available, and an age-based in-service "
        "distribution is not available, and your plan may charge a processing fee."
    )
    parsed, _info = _apply_points(engine, collected, [tax])
    joined = " ".join(_string_points(parsed))
    assert (
        "A hardship withdrawal may be available, and taxes and penalties may "
        "apply to any distribution."
        in joined
    ), joined
    assert not AGE_IN_SERVICE.search(joined), joined
    # A fee key point is removed before this rewriter when the request is not
    # about fees. The evidenced loss is inside the rewriter itself.
    rewritten_fee = type(engine)._rewrite_inapplicable_inservice_option_text(
        fee, {"rollover_source", "age_based_in_service"}
    )
    assert (
        "A hardship withdrawal may be available, and your plan may charge a "
        "processing fee."
        == rewritten_fee
    ), rewritten_fee
    assert not AGE_IN_SERVICE.search(rewritten_fee), rewritten_fee


def test_root_three_clause_omission_keeps_hardship_and_loan(engine):
    """Root counterexample: omit the unsolicited rollover denial and keep both offers."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    sentence = (
        "A rollover-source withdrawal does not apply, and a hardship "
        "withdrawal may be available, and a 401(k) loan may be available."
    )
    parsed, info = _apply_points(engine, collected, [sentence])
    joined = " ".join(_string_points(parsed))
    assert not ROLLOVER_SOURCE.search(joined), joined
    assert (
        "A hardship withdrawal may be available, and a 401(k) loan may be available."
        in joined
    ), joined
    assert "rollover_source" in (info.get("inapplicable_options_removed") or []), info
    assert "rollover_source" not in (info.get("inapplicable_options_unresolved") or [])


def test_b2_pure_multi_option_inapplicable_offer_is_omitted(engine):
    """B2: a sentence that only offers two inapplicable options is omitted."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    offer = (
        "You may take a rollover-source withdrawal or an age-based in-service "
        "distribution at any time."
    )
    parsed, info = _apply_points(engine, collected, [offer, APPLICABLE_OPTION_LIST])
    points = _string_points(parsed)
    joined = " ".join(points)
    assert offer not in joined, joined
    assert not ROLLOVER_SOURCE.search(joined), joined
    assert not AGE_IN_SERVICE.search(joined), joined
    assert re.search(r"hardship", joined, re.I), joined
    assert re.search(r"\bloan", joined, re.I), joined
    removed = info.get("inapplicable_options_removed") or []
    assert "rollover_source" in removed, info
    assert "age_based_in_service" in removed, info
    assert info.get("inapplicable_options_unresolved") in (None, []), info


def test_r12_internal_notes_survive_participant_omission(engine):
    """R12: omission changes participant text and leaves internal notes intact."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    reason = (
        "General information about accessing funds while employed. "
        "A rollover-source withdrawal is not applicable."
    )
    gaps = ["Plan loan limit is an internal gap."]
    escalation = {"needed": False, "reason": None}
    parsed, info = _apply_points(
        engine,
        collected,
        C590_LIVE_KEY_POINTS,
        draft_over={
            "outcome_reason": reason,
            "data_gaps": gaps,
            "escalation": escalation,
            "guardrails_applied": ["Source check stayed an internal control."],
        },
    )
    visible = _participant_visible(parsed)
    assert not ROLLOVER_SOURCE.search(visible), visible
    assert parsed["outcome_reason"] == reason
    assert parsed["data_gaps"] == gaps
    assert parsed["escalation"] == escalation
    guardrails = parsed.get("guardrails_applied") or []
    assert "Source check stayed an internal control." in guardrails
    assert (
        "Did not present inapplicable age based in service as an available "
        "in-service option."
    ) in guardrails
    assert (
        "Did not present inapplicable rollover source as an available "
        "in-service option."
    ) in guardrails
    assert info.get("inapplicable_options_unresolved") in (None, [])


def test_r13_numeric_marker_stays_with_the_surviving_sentence(engine):
    """R13: the captured numeric marker stays on the surviving offer sentence."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _info = _apply_points(engine, collected, C590_LIVE_KEY_POINTS)
    first = _string_points(parsed)[0]
    assert first.startswith("1. The options worth reviewing are "), first
    assert "not available" not in first.casefold(), first
    assert not ROLLOVER_SOURCE.search(first), first
    assert not AGE_IN_SERVICE.search(first), first


def test_r14_semicolon_keeps_the_surviving_offer(engine):
    """R14: a semicolon-fused denial drops, and the valid offer stays."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    sentence = (
        "An age-based in-service distribution is not available; a 401(k) loan "
        "may be available if the plan allows loans."
    )
    parsed, info = _apply_points(engine, collected, [sentence])
    joined = " ".join(_string_points(parsed))
    assert "A 401(k) loan may be available if the plan allows loans." in joined, joined
    assert not AGE_IN_SERVICE.search(joined), joined
    assert "age_based_in_service" in (info.get("inapplicable_options_removed") or [])
    assert info.get("inapplicable_options_unresolved") in (None, []), info


def test_r15_ambiguous_escalation_still_requires_human_review(engine):
    """R15: clearing unresolved options does not clear native ambiguous review."""
    from data_pipeline.response_handoff import response_requires_review

    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, info = _apply_points(
        engine,
        collected,
        C590_LIVE_KEY_POINTS,
        draft_over={
            "outcome": "ambiguous_plan_rules",
            "escalation": {"needed": True, "reason": "plan rules unresolved"},
            "question_coverage": [{
                "question_index": 0,
                "status": "answered",
                "answer_reference": C590_LIVE_KEY_POINTS[1],
            }],
        },
    )
    assert parsed["outcome"] == "ambiguous_plan_rules"
    assert parsed["escalation"] == {"needed": True, "reason": "plan rules unresolved"}
    assert info.get("inapplicable_options_unresolved") in (None, []), info
    metadata = engine._validate_question_coverage(parsed, collected)
    assert metadata.get("incomplete_question_count") == 0, metadata
    assert metadata.get("human_review_required") is False, metadata
    assert response_requires_review(parsed, metadata) is True


def _questions_through_evidence(
    questions, *, participant_body, advisor_body=None, inquiry=None, sensitive=()
):
    """Build requested_questions the way the orchestrator does, without editing it."""
    from data_pipeline.retrieval_privacy import redact_retrieval_context
    from data_pipeline.ticket_orchestrator import TicketOrchestrator

    if advisor_body is None:
        ticket = SimpleNamespace(
            email_subject="Access while employed",
            email_body=participant_body,
            conversation_snapshot=None,
        )
    else:
        ticket = SimpleNamespace(
            conversation_snapshot=SimpleNamespace(
                subject="Access while employed",
                initial_message=SimpleNamespace(
                    author_role="participant", body=participant_body
                ),
                messages=[SimpleNamespace(author_role="advisor", body=advisor_body)],
            )
        )
    evidenced = TicketOrchestrator._evidenced_questions(
        questions, SimpleNamespace(ticket=ticket)
    )
    chosen = evidenced or [inquiry if inquiry is not None else participant_body]
    return [
        redact_retrieval_context(question, sensitive_literals=sensitive)
        for question in chosen
    ]


def test_b4_evidenced_questions_and_redaction_drive_explicit_requests(engine):
    """B4: participant evidence, generic rollover wording, and assistant-only text."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    denial = (
        "A rollover-source withdrawal is not applicable based on current "
        "source information."
    )
    named = "Can I take a rollover-source withdrawal? Reply to pat.sample@example.com"
    named_questions = _questions_through_evidence(
        [named], participant_body=named, sensitive=("pat.sample@example.com",)
    )
    assert any("rollover-source" in question for question in named_questions)
    assert all("pat.sample@example.com" not in question for question in named_questions)
    named_parsed, named_info = _apply_points(
        engine, collected, [denial, APPLICABLE_OPTION_LIST], questions=named_questions
    )
    named_joined = " ".join(_string_points(named_parsed))
    assert ROLLOVER_SOURCE.search(named_joined), named_joined
    assert re.search(r"not applicable", named_joined, re.I), named_joined
    assert "pat.sample@example.com" not in named_joined
    assert "rollover_source" not in (named_info.get("inapplicable_options_removed") or [])

    generic = "What rollover choices do I have while I am employed?"
    generic_questions = _questions_through_evidence([generic], participant_body=generic)
    assert generic_questions == [generic]
    generic_parsed, _generic_info = _apply_points(
        engine,
        collected,
        [denial, APPLICABLE_OPTION_LIST],
        questions=generic_questions,
    )
    generic_joined = " ".join(_string_points(generic_parsed))
    assert not ROLLOVER_SOURCE.search(generic_joined), generic_joined
    assert re.search(r"hardship", generic_joined, re.I)

    assistant = "Can I take a rollover-source withdrawal?"
    participant = "What options do I have while I am employed?"
    assistant_questions = _questions_through_evidence(
        [assistant],
        participant_body=participant,
        advisor_body=assistant,
        inquiry=participant,
    )
    assert assistant_questions == [participant]
    assistant_parsed, _assistant_info = _apply_points(
        engine,
        collected,
        [denial, APPLICABLE_OPTION_LIST],
        questions=assistant_questions,
    )
    assistant_joined = " ".join(_string_points(assistant_parsed))
    assert not ROLLOVER_SOURCE.search(assistant_joined), assistant_joined


def test_d3_empty_opening_preserves_the_substantive_answer(engine):
    """D3: an omitted opening may be empty while the substantive answer stays."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    substantive = "The main possible options are a hardship withdrawal or a 401(k) loan."
    parsed, info = _apply_points(
        engine,
        collected,
        [substantive],
        draft_over={
            "response_to_participant": {
                "opening": "An age-based in-service distribution is not available to you.",
                "warnings": [
                    "A rollover-source withdrawal is not applicable based on "
                    "current source information."
                ],
            }
        },
    )
    response = parsed["response_to_participant"]
    points = _string_points(parsed)
    assert response.get("opening") == ""
    assert substantive in points, points
    assert re.search(r"hardship", " ".join(points), re.I)
    assert re.search(r"\bloan", " ".join(points), re.I)
    assert "eligible" not in (response.get("opening") or "").casefold()
    assert not ROLLOVER_SOURCE.search(_participant_visible(parsed))
    assert not AGE_IN_SERVICE.search(_participant_visible(parsed))
    assert info.get("inapplicable_options_unresolved") in (None, []), info


INVITE = (
    "Tell us which option you would like to explore, and our team will verify "
    "whether your plan permits it and what requirements apply."
)
# Each source names an inapplicable option and substantive non-option prose,
# and names no applicable option. Survival is structural: the prose is not an
# option clause.
F1_POLICY_CASES = [
    pytest.param(
        "key_points",
        "An age-based in-service distribution is not available, and taxes and "
        "penalties may apply to any distribution.",
        "Taxes and penalties may apply to any distribution.",
        "age_based_in_service",
        "An age-based in-service distribution is not available",
        id="key_points-denial-then-tax-comma-and",
    ),
    pytest.param(
        "key_points",
        "Taxes and penalties may apply to any distribution, and an age-based "
        "in-service distribution is not available.",
        "Taxes and penalties may apply to any distribution.",
        "age_based_in_service",
        "an age-based in-service distribution is not available",
        id="key_points-tax-then-denial-comma-and",
    ),
    pytest.param(
        "warnings",
        "An age-based in-service distribution is not available, or a 10% "
        "early-withdrawal penalty may apply.",
        "A 10% early-withdrawal penalty may apply.",
        "age_based_in_service",
        "An age-based in-service distribution is not available",
        id="warnings-denial-then-penalty-comma-or",
    ),
    pytest.param(
        "warnings",
        "A 10% early-withdrawal penalty may apply, or an age-based in-service "
        "distribution is not available.",
        "A 10% early-withdrawal penalty may apply.",
        "age_based_in_service",
        "an age-based in-service distribution is not available",
        id="warnings-penalty-then-denial-comma-or",
    ),
    pytest.param(
        "opening",
        "An age-based in-service distribution is not available; taxes and "
        "penalties may apply to any distribution.",
        "Taxes and penalties may apply to any distribution.",
        "age_based_in_service",
        "An age-based in-service distribution is not available",
        id="opening-denial-then-tax-semicolon",
    ),
    pytest.param(
        "opening",
        "Taxes and penalties may apply to any distribution; an age-based "
        "in-service distribution is not available.",
        "Taxes and penalties may apply to any distribution.",
        "age_based_in_service",
        "an age-based in-service distribution is not available",
        id="opening-tax-then-denial-semicolon",
    ),
    pytest.param(
        "key_points",
        "An age-based in-service distribution is not available. Taxes and "
        "penalties may apply to any distribution.",
        "Taxes and penalties may apply to any distribution.",
        "age_based_in_service",
        "An age-based in-service distribution is not available.",
        id="key_points-denial-then-tax-sentence",
    ),
    pytest.param(
        "warnings",
        "Taxes and penalties may apply to any distribution. An age-based "
        "in-service distribution is not available.",
        "Taxes and penalties may apply to any distribution.",
        "age_based_in_service",
        "An age-based in-service distribution is not available.",
        id="warnings-tax-then-denial-sentence",
    ),
    pytest.param(
        "opening",
        "You are an active employee. An age-based in-service distribution is "
        "not available to you.",
        "You are an active employee.",
        "age_based_in_service",
        "An age-based in-service distribution is not available to you.",
        id="opening-active-status-then-denial",
    ),
    pytest.param(
        "opening",
        "An age-based in-service distribution is not available to you. You are "
        "an active employee.",
        "You are an active employee.",
        "age_based_in_service",
        "An age-based in-service distribution is not available to you.",
        id="opening-denial-then-active-status",
    ),
    pytest.param(
        "key_points",
        "A rollover-source withdrawal does not apply, and taxes and penalties "
        "may apply to any distribution.",
        "Taxes and penalties may apply to any distribution.",
        "rollover_source",
        "A rollover-source withdrawal does not apply",
        id="key_points-rollover-denial-then-tax-comma-and",
    ),
]


@pytest.mark.parametrize(
    ("field", "source", "expected", "target", "omitted"),
    F1_POLICY_CASES,
)
def test_f1_non_option_prose_survives_without_an_applicable_option(
    engine, field, source, expected, target, omitted,
):
    """F1: a string with no applicable option still keeps non-option prose."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    if field == "key_points":
        parsed, info = _apply_points(
            engine,
            collected,
            [source],
            draft_over={
                "response_to_participant": {"opening": "", "warnings": []},
            },
        )
    elif field == "opening":
        parsed, info = _apply_points(
            engine,
            collected,
            [APPLICABLE_OPTION_LIST],
            draft_over={
                "response_to_participant": {"opening": source, "warnings": []},
            },
        )
    else:
        parsed, info = _apply_points(
            engine,
            collected,
            [APPLICABLE_OPTION_LIST],
            draft_over={
                "response_to_participant": {"opening": "", "warnings": [source]},
            },
        )
    response = parsed["response_to_participant"]
    if field == "key_points":
        assert response["key_points"] == [expected, INVITE], response["key_points"]
        assert response["opening"] == ""
        assert response["warnings"] == []
    elif field == "opening":
        assert response["opening"] == expected, response["opening"]
        assert response["key_points"] == [APPLICABLE_OPTION_LIST, INVITE]
        assert response["warnings"] == []
    else:
        assert response["warnings"] == [expected], response["warnings"]
        assert response["opening"] == ""
        assert response["key_points"] == [APPLICABLE_OPTION_LIST, INVITE]
    visible = _participant_visible(parsed)
    mark = AGE_IN_SERVICE if target == "age_based_in_service" else ROLLOVER_SOURCE
    assert not mark.search(visible), visible
    assert omitted not in visible, visible
    assert expected in visible
    assert info.get("inapplicable_options_removed") == [
        "age_based_in_service",
        "rollover_source",
    ], info
    assert info.get("inapplicable_options_unresolved") in (None, []), info
    assert any(
        f"inapplicable {target.replace('_', ' ')}" in note.casefold()
        for note in (parsed.get("guardrails_applied") or [])
    )


F1_FEE_CASES = [
    pytest.param(
        "An age-based in-service distribution is not available, and your plan "
        "may charge a processing fee.",
        "Your plan may charge a processing fee.",
        id="fee-denial-then-fee-comma-and",
    ),
    pytest.param(
        "Your plan may charge a processing fee, or an age-based in-service "
        "distribution is not available.",
        "Your plan may charge a processing fee.",
        id="fee-fee-then-denial-comma-or",
    ),
    pytest.param(
        "An age-based in-service distribution is not available; your plan may "
        "charge a processing fee.",
        "Your plan may charge a processing fee.",
        id="fee-denial-then-fee-semicolon",
    ),
    pytest.param(
        "Your plan may charge a processing fee. An age-based in-service "
        "distribution is not available.",
        "Your plan may charge a processing fee.",
        id="fee-fee-then-denial-sentence",
    ),
]


@pytest.mark.parametrize(("source", "expected"), F1_FEE_CASES)
def test_f1_fee_prose_survives_at_the_helper_boundary(engine, source, expected):
    """Fee prose is locked on the rewriter. The informational fee gate is separate."""
    rewritten = type(engine)._rewrite_inapplicable_inservice_option_text(
        source, {"rollover_source", "age_based_in_service"}
    )
    assert rewritten == expected, rewritten
    assert not AGE_IN_SERVICE.search(rewritten), rewritten
    assert "not available" not in rewritten.casefold()


# A reduced semicolon unit is a segment, not a sentence. The F1 reduction made
# this rejoin reachable: base kept the whole string and the prior patch deleted
# it, so neither shape could expose the punctuation.
F1_SEMICOLON_REJOIN_CASES = [
    pytest.param(
        "an age-based in-service distribution is not available, and fees may "
        "apply; taxes may apply.",
        "Fees may apply; taxes may apply.",
        id="semicolon-rejoin-clause-unit-first",
    ),
    pytest.param(
        "a rollover-source withdrawal does not apply, and fees may apply; "
        "taxes may apply.",
        "Fees may apply; taxes may apply.",
        id="semicolon-rejoin-rollover-clause-unit-first",
    ),
    pytest.param(
        "taxes may apply; an age-based in-service distribution is not "
        "available, and fees may apply; charges may apply.",
        "Taxes may apply; fees may apply; charges may apply.",
        id="semicolon-rejoin-clause-unit-middle",
    ),
    pytest.param(
        "taxes may apply to any distribution; an age-based in-service "
        "distribution is not available, and fees may apply.",
        "Taxes may apply to any distribution; fees may apply.",
        id="semicolon-rejoin-clause-unit-last",
    ),
]


@pytest.mark.parametrize(("source", "expected"), F1_SEMICOLON_REJOIN_CASES)
def test_f1_semicolon_rejoin_keeps_segment_shape(engine, source, expected):
    """A reduced semicolon unit must not carry a sentence period or capital."""
    rewritten = type(engine)._rewrite_inapplicable_inservice_option_text(
        source, {"rollover_source", "age_based_in_service"}
    )
    assert ".;" not in rewritten, rewritten
    assert rewritten == expected, rewritten
    assert not AGE_IN_SERVICE.search(rewritten), rewritten
    assert not ROLLOVER_SOURCE.search(rewritten), rewritten


def test_f1_semicolon_rejoin_artefact_is_absent_from_participant_text(engine):
    """The rejoin artefact must not reach participant-visible text."""
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    source = (
        "an age-based in-service distribution is not available, and fees may "
        "apply; taxes may apply."
    )
    parsed, info = _apply_points(
        engine,
        collected,
        [APPLICABLE_OPTION_LIST],
        draft_over={"response_to_participant": {"opening": source, "warnings": []}},
    )
    response = parsed["response_to_participant"]
    assert response["opening"] == "Fees may apply; taxes may apply.", response["opening"]
    visible = _participant_visible(parsed)
    assert ".;" not in visible, visible
    assert not AGE_IN_SERVICE.search(response["opening"]), response["opening"]
    assert info.get("inapplicable_options_unresolved") in (None, []), info
