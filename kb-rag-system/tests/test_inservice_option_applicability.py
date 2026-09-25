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


def test_rv4_accurate_denial_is_preserved(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "Because you are under age 59½, an age-based in-service distribution "
        "is not available to you.",
        "A 401(k) loan may be available if the plan allows loans.",
    ])
    joined = " ".join(_string_points(parsed))
    assert AGE_IN_SERVICE.search(joined)
    assert re.search(r"not available", joined, re.I)
    assert re.search(r"\bloan", joined, re.I)


def test_rv4_future_eligibility_note_is_preserved(engine):
    collected = _collected(rollover=KNOWN_ZERO, age_59_5=False)
    parsed, _ = _apply_custom(engine, collected, [
        "Once you reach 59½ you may become eligible for an in-service "
        "distribution if the plan allows it.",
        "Hardship withdrawals require an immediate and serious financial need.",
    ])
    joined = " ".join(_string_points(parsed))
    assert AGE_IN_SERVICE.search(joined)
    assert re.search(r"once you reach", joined, re.I)
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


def test_h2_non_offer_keeps_unavailability_without_zero_figure(engine):
    """Known-zero non-offer may stay truthful without an account-specific figure."""
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
    assert re.search(r"does not apply", joined, re.I), points
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
    assert re.search(r"does not apply|is not an option", joined, re.I), joined
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
    assert re.search(r"does not apply", joined, re.I)


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
