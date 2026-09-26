"""Policy regression simulations; no current facts replace historic snapshots."""

import json
import re

import pytest

from data_pipeline.rag_engine import RAGEngine


def parsed(**overrides):
    return {
        "outcome": "can_proceed", "outcome_reason": "Eligible based on collected facts.",
        "response_to_participant": {
            "opening": "Here are the options.", "key_points": [], "steps": [], "warnings": [],
        },
        "questions_to_ask": [], "data_gaps": [], "coverage_gaps": [],
        "escalation": {"needed": False, "reason": None}, "guardrails_applied": [],
        **overrides,
    }


def profile(rollover=True):
    return {"primary_action": "termination_rollover" if rollover else "termination_distribution",
            "inquiry_intent": "transactional_submission",
            "signals": {"pure_rollover": rollover, "cash_component": not rollover,
                        "employment_state": "terminated", "termination_date_present": True,
                        "termination_distribution": True, "procedure_requested": True}}


def facts(loan="zero", crypto=0, balance=10000):
    return {"internal_preflight_context": {
        "schema_version": 1,
        "loans": {"status": "known", "outstanding_status": loan, "source": "loans", "as_of": "2026-08-01"},
        "sources": {"account_balance": {"status": "known", "value": balance, "source": "savings_rate", "as_of": "2026-08-01"}},
        "crypto": {"enrollment": {"value": True, "status": "known"},
                   "holdings": {"value": crypto, "status": "known" if crypto is not None else "unknown"}},
    }}


@pytest.mark.parametrize("rollover", [True, False])
def test_positive_loan_and_unknown_crypto_are_used_for_both_distribution_paths(rollover):
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(rollover), facts("positive", None))
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert "outstanding loan is recorded" in text
    assert "positions have not been verified" in text
    assert "liquidate" not in text
    assert info["structured_preflight_applied"] is True


def test_confirmed_zero_drops_generic_loan_crypto_conditionals():
    response = parsed()
    response["response_to_participant"]["warnings"] = [
        "If you have an outstanding loan, ask Support.", "If Crypto Enrollment is active, contact Support.",
    ]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), facts())
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert "outstanding loan" not in text
    assert "crypto" not in text
    assert "final payroll" in text


@pytest.mark.parametrize("rollover", [True, False])
def test_unknown_crypto_is_owned_by_internal_review(rollover):
    fixed, _ = RAGEngine._apply_termination_response_policy(
        parsed(), profile(rollover), facts("unknown", None),
    )
    warnings = " ".join(fixed["response_to_participant"]["warnings"])
    assert "positions have not been verified" in warnings
    assert "Our team needs to verify" in warnings
    assert "Ask Support" not in warnings
    assert "loan status has not been verified" in warnings


def test_recorded_loan_treatment_is_owned_by_internal_review():
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile(), facts("positive"))
    warnings = " ".join(fixed["response_to_participant"]["warnings"])
    assert "outstanding loan is recorded" in warnings
    assert "Our team needs to verify its payoff or offset handling" in warnings
    assert "Contact Support" not in warnings


def test_loan_offset_warning_survives_pure_rollover_cash_tax_filter():
    response = parsed()
    response["response_to_participant"]["warnings"] = [
        "An unpaid loan offset may be a taxable distribution and may incur an early withdrawal penalty.",
        "The direct rollover has 20% cash withholding.",
    ]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), facts("positive"))
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert "loan offset may be a taxable distribution" in text
    assert "20%" not in text


@pytest.mark.parametrize("delivery", [
    "Delivery: check or wire are available; ACH is not used for direct rollovers.",
    "Delivery: checks or wires, not ACH, are available for a direct rollover.",
])
def test_negative_ach_clause_does_not_delete_the_delivery_answer(delivery):
    response = parsed(outcome="blocked_missing_data")
    response["response_to_participant"]["key_points"] = [delivery]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), facts())
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert "delivery:" in text and "check" in text and "wire" in text
    assert not re.search(r"\bach\b", text)


@pytest.mark.parametrize("delivery", [
    "Delivery: use ACH or a wire for your rollover.",
    "Delivery: ACH is not used for direct rollovers. Choose ACH anyway.",
    "Delivery: ACH is not only available but preferred.",
])
def test_positive_or_ambiguous_ach_still_cannot_be_rollover_delivery(delivery):
    response = parsed(outcome="blocked_missing_data")
    response["response_to_participant"]["key_points"] = [delivery]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), facts())
    assert not re.search(r"\bach\b", json.dumps(fixed["response_to_participant"]).lower())


def test_requested_answers_keep_order_when_required_rollover_facts_are_merged():
    response = parsed(outcome="blocked_missing_data")
    points = [
        "Process: eligibility must be verified before using the distribution request.",
        "Sources: non-Roth after-tax has not been verified; Support will check the statement.",
        "Delivery: the receiving provider accepts check or wire.",
        "Fees: $75 per request; $35 for each wire.",
    ]
    response["response_to_participant"]["key_points"] = points
    context = facts("positive", None)
    context["internal_response_context"] = {"requested_questions": [
        "What is the process?", "What are my sources?", "How are funds delivered?", "What are the fees?",
    ]}
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), context)
    actual = fixed["response_to_participant"]["key_points"]
    assert actual[0] == points[0]
    assert actual[1] == points[1]
    assert "check or wire" in actual[2]
    assert "$75" in actual[3] and "$35" in actual[3]


def test_numbered_question_sections_survive_canonical_delivery_and_fees():
    response = parsed(outcome="blocked_missing_data")
    response["response_to_participant"]["key_points"] = [
        "1. Process: the receiving provider's paperwork does not replace our request.",
        "2. Sources: Support must verify non-Roth after-tax using the statement.",
        "3. Delivery: check or wire can be accepted by the receiving provider.",
        "4. Fees: $75 request fee plus $35 for each wire.",
    ]
    context = facts()
    context["internal_response_context"] = {"requested_questions": [
        "What paperwork is needed?", "What are my sources?", "What delivery methods are available?", "What fees apply?",
    ]}
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), context)
    points = fixed["response_to_participant"]["key_points"]
    assert points[0] == response["response_to_participant"]["key_points"][0]
    for index, label in enumerate(['Process', 'Sources', 'Delivery', 'Fees']):
        assert points[index].startswith(f'{index + 1}. {label}:')


@pytest.mark.parametrize('reference', [
    RAGEngine._TERMINATION_FORM_URL,
    f"[secure form]({RAGEngine._TERMINATION_FORM_URL})",
    '844-401-2253',
])
def test_form_support_deduplication_preserves_numbered_procedure_answer(reference):
    response = parsed(outcome='can_proceed')
    response['response_to_participant']['key_points'] = [
        f'1. Process: use the participant portal first; for access problems use {reference}.',
        '2. Sources: Support must verify non-Roth after-tax from the statement.',
        '3. Delivery: the receiving provider accepts check or wire.',
        '4. Fees: fees apply to this request.',
    ]
    context = facts()
    context['internal_response_context'] = {'requested_questions': [
        'What paperwork is needed?', 'What are my sources?',
        'What delivery methods are available?', 'What fees apply?',
    ]}
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), context)
    participant = fixed['response_to_participant']
    for index, label in enumerate(['Process', 'Sources', 'Delivery', 'Fees']):
        assert participant['key_points'][index].startswith(f'{index + 1}. {label}:')
    assert 'participant portal first' in participant['key_points'][0]
    assert '(the secure' not in participant['key_points'][0]
    rendered = json.dumps(participant)
    assert rendered.count(RAGEngine._TERMINATION_FORM_URL) == 1
    assert rendered.count('844-401-2253') == 1


@pytest.mark.parametrize('delivery', [
    '3. Delivery: check or wire are supported. ACH is not a rollover delivery method.',
    '3. Delivery: choose ACH or wire for this rollover.',
    None,
])
def test_removed_delivery_section_is_restored_at_its_question_position(delivery):
    """A filtered/omitted answer must not shift source or fee question indexes."""
    response = parsed(outcome='blocked_missing_data')
    original = [
        "1. Process: the receiving provider's paperwork does not replace our request.",
        '2. Sources: the receiving provider must accept each verified source; non-Roth after-tax needs verification.',
    ]
    response['response_to_participant']['key_points'] = original + (
        [delivery] if delivery is not None else []
    ) + ['4. Fees: a request fee applies.']
    context = facts()
    context['internal_response_context'] = {'requested_questions': [
        'What paperwork is needed?', 'What are my sources?',
        'Can it be delivered by check or wire?', 'What fees apply?',
    ]}
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), context)
    points = fixed['response_to_participant']['key_points']
    assert points[:2] == original
    assert points[2].startswith('3. Delivery:')
    assert 'check or wire' in points[2]
    assert points[3].startswith('4. Fees:')
    assert '$75' in points[3] and '$35' in points[3]
    assert not re.search(r'\bach\b', json.dumps(fixed['response_to_participant']), re.I)


@pytest.mark.parametrize('inquiry,selected', [
    ('I want a direct rollover by check to a Roth IRA.', 'check'),
    ('Please send the funds by check to my IRA.', 'check'),
    ('Can I use check or wire for a direct rollover?', None),
    ('I do not want a rollover by check. What are the options?', None),
])
def test_delivery_focus_comes_from_the_inquiry_not_account_fields(inquiry, selected):
    engine = RAGEngine.__new__(RAGEngine)
    context = facts()
    context['participant_data'] = {'employment_status': 'Terminated', 'termination_date': '2026-07-01'}
    actual_profile = engine._build_retrieval_profile(inquiry, 'rollover', 'LT Trust', '401(k)', context)
    assert actual_profile['signals'].get('selected_delivery') == selected
    if selected:
        fixed, _ = engine._apply_termination_response_policy(parsed(outcome='blocked_missing_data'), actual_profile, context)
        text = json.dumps(fixed['response_to_participant']).lower()
        assert 'check' in text and '$75' in text
        assert 'wire' not in text and '$35' not in text


@pytest.mark.parametrize("rollover", [True, False])
def test_missing_balance_retains_applicable_preflight_without_confirming_eligibility(rollover):
    response = parsed(outcome="blocked_missing_data", data_gaps=["Total vested balance is unknown."])
    fixed, info = RAGEngine._apply_termination_response_policy(response, profile(rollover), facts("positive", None))
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert "outstanding loan is recorded" in text
    assert "positions have not been verified" in text
    assert "final payroll" in text
    assert fixed["outcome"] == "blocked_missing_data"
    assert info["structured_preflight_applied"] is True


def test_separation_conflict_does_not_gain_submission_preflight():
    response = parsed(outcome="blocked_missing_data")
    query = profile()
    query["signals"].update(employment_state="active", separation_conflicts_active=True)
    fixed, info = RAGEngine._apply_termination_response_policy(response, query, facts("positive", None))
    assert not info.get("structured_preflight_applied")
    assert "final payroll" not in json.dumps(fixed["response_to_participant"]).lower()


def test_unknown_loan_is_not_absence_or_ineligibility():
    context = facts("unknown", None)
    context["internal_preflight_context"]["loans"]["status"] = "error"
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    assert fixed["outcome"] == "can_proceed"
    assert "loan status has not been verified" in json.dumps(fixed).lower()


def test_zero_balance_requires_internal_custody_investigation_without_distribution_macro():
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), facts(balance=0))
    assert fixed["outcome"] == "blocked_missing_data"
    assert fixed["escalation"]["needed"] is True
    assert fixed["response_to_participant"]["steps"] == []
    assert fixed["questions_to_ask"] == []
    assert "custod" in fixed["escalation"]["reason"].lower()
    assert "penchecks" not in json.dumps(fixed).lower()
    assert info["custody_review_required"] is True


def test_ineligible_loan_cannot_end_with_steps_to_apply_for_smaller_loan():
    response = parsed(outcome="blocked_not_eligible")
    response["response_to_participant"]["steps"] = [{"step_number": 1, "action": "Apply for a smaller loan in the portal."}]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, {"primary_action": "loan_request", "signals": {}}, {})
    assert fixed["outcome"] == "blocked_not_eligible"
    assert fixed["response_to_participant"]["steps"] == []


def test_informational_questions_keep_order_and_do_not_gain_submission_macro():
    response = parsed()
    points = ["You may leave eligible funds in the plan.", "Your fees require statement verification.", "A partial rollover depends on plan rules."]
    response["response_to_participant"]["key_points"] = points.copy()
    query_profile = profile()
    query_profile["inquiry_intent"] = "informational_options"
    query_profile["signals"]["procedure_requested"] = False
    fixed, _ = RAGEngine._apply_termination_response_policy(response, query_profile, facts())
    assert fixed["response_to_participant"]["key_points"] == points
    assert fixed["response_to_participant"]["steps"] == []
    assert "rightsignature" not in json.dumps(fixed).lower()


def test_plan_identifier_question_is_not_replaced_by_rollover_context():
    engine = RAGEngine.__new__(RAGEngine)
    actual_profile = engine._build_retrieval_profile(
        "What is my plan ID for the rollover form?", "rollover", "LT Trust", "401(k)",
        {"participant_data": {"participant_status": "Terminated", "termination_date": "2026-07-01"}},
    )
    assert actual_profile["primary_action"] == "plan_identifier"
    response = parsed()
    response["response_to_participant"]["key_points"] = ["Your verified recordkeeper plan ID is [PLAN_CODE]."]
    context = facts()
    context['internal_plan_disclosure_context'] = _plan_disclosure(rk_plan_id='SYNTHETIC-PLAN-CODE')
    fixed, _ = RAGEngine._apply_termination_response_policy(response, actual_profile, context)
    assert fixed['response_to_participant']['opening'] == (
        'The recordkeeper plan ID for your plan is SYNTHETIC-PLAN-CODE, '
        'from the plan record as of 2026-09-01.'
    )
    assert fixed['response_to_participant']['steps'] == []


def test_plan_identifier_answer_does_not_grow_into_a_distribution_tutorial():
    context = facts()
    context['internal_plan_disclosure_context'] = _plan_disclosure(rk_plan_id='SYNTHETIC-PLAN-CODE')
    draft = parsed()
    draft['response_to_participant'] = {'opening': 'You are terminated and may roll over eligible funds.',
        'key_points': ['$75 distribution fee and $35 wire fee.'], 'steps': [{'action': 'Submit a rollover.'}], 'warnings': ['Verify bank details.']}
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'plan_identifier'}, context)
    assert fixed['response_to_participant']['opening'] == (
        'The recordkeeper plan ID for your plan is SYNTHETIC-PLAN-CODE, '
        'from the plan record as of 2026-09-01.'
    )
    assert fixed['response_to_participant']['steps'] == []
    assert '$75' not in json.dumps(fixed['response_to_participant'])


def test_missing_recordkeeper_identifier_cannot_be_replaced_with_portal_route_id():
    context = facts()
    context['plan_data'] = {'plan_id': 'ROUTE-ONLY'}
    draft = parsed()
    draft['response_to_participant']['opening'] = 'Use ROUTE-ONLY as your plan ID.'
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'plan_identifier'}, context)
    assert 'ROUTE-ONLY' not in json.dumps(fixed['response_to_participant'])
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True


def test_identifier_question_does_not_swallow_other_requested_questions():
    context = {'internal_response_context': {'requested_questions': ['What is my plan ID?', 'What fees apply?']}}
    actual = RAGEngine.__new__(RAGEngine)._build_retrieval_profile('What is my plan ID and what fees apply?', 'rollover', 'LT Trust', '401(k)', context)
    assert actual['primary_action'] != 'plan_identifier'


def test_incoming_rollover_is_unchanged():
    response = parsed()
    fixed, info = RAGEngine._apply_termination_response_policy(response, {"primary_action": "incoming_rollover", "signals": {}}, facts(balance=0))
    assert fixed == response
    assert info["applied"] is False


@pytest.mark.parametrize("balance", [0, 10000])
def test_plan_hold_blocks_distribution_execution_even_if_participant_eligible(balance):
    context = facts(balance=balance)
    context["internal_plan_context"] = {
        "current": {"status": "pending_termination", "active": True, "status_as_of": "2026-03-26"},
        "operational_facts": [{"kind": "distribution_hold", "value": True, "source": "plan_history.notes", "recorded_at": "2026-03-26", "effective_on": "2026-03-26"}],
    }
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    assert fixed["outcome"] == "blocked_not_eligible"
    assert fixed["response_to_participant"]["steps"] == []
    assert "hold" in fixed["response_to_participant"]["opening"].lower()
    assert info["plan_review_required"] is True


def test_completed_deconversion_does_not_send_participant_to_old_recordkeeper():
    context = facts(balance=0)
    context["internal_plan_context"] = {
        "current": {"status": "terminated", "active": False},
        "operational_facts": [
            {"kind": "custody_transition_status", "value": "completed", "source": "plan_history.notes", "recorded_at": "2025-10-09", "effective_on": "2025-10-02"},
            {"kind": "successor_recordkeeper", "value": "Fidelity", "source": "plan_history.notes", "recorded_at": "2025-10-09", "effective_on": "2025-10-02"},
        ],
    }
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    assert "Fidelity" in fixed["response_to_participant"]["opening"]
    assert "rightsignature" not in json.dumps(fixed).lower()
    assert fixed["response_to_participant"]["steps"] == []
    assert info["plan_review_required"] is True


def test_later_hold_release_is_not_overridden_by_an_older_hold_note():
    context = facts()
    context["internal_plan_context"] = {"operational_facts": [
        {"kind": "distribution_hold", "value": False, "recorded_at": "2026-04-01", "effective_on": "2026-04-01"},
        {"kind": "distribution_hold", "value": True, "recorded_at": "2026-03-26", "effective_on": "2026-03-26"},
    ]}
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    assert fixed["outcome"] == "can_proceed"
    assert info.get("plan_review_required") is not True


def test_preflight_warnings_survive_existing_warning_cap():
    response = parsed()
    response["response_to_participant"]["warnings"] = [f"Existing caution {i}." for i in range(4)]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), facts("positive", None))
    warnings = " ".join(fixed["response_to_participant"]["warnings"]).lower()
    assert "outstanding loan is recorded" in warnings
    assert "positions have not been verified" in warnings
    assert len(fixed["response_to_participant"]["warnings"]) <= 4


@pytest.mark.parametrize("mixed_warning", [
    "After a loan offset, receive your rollover by ACH.",
    "After a loan offset, take cash from the remainder.",
    "After a loan offset, select overnight delivery in the portal.",
])
def test_loan_offset_tax_exception_cannot_admit_unrelated_cash_or_delivery(mixed_warning):
    response = parsed()
    response["response_to_participant"]["warnings"] = [mixed_warning]
    fixed, _ = RAGEngine._apply_termination_response_policy(response, profile(), facts("positive", 0))
    assert mixed_warning not in fixed["response_to_participant"]["warnings"]


def test_informational_scope_does_not_bypass_brokerage_clarification():
    query_profile = profile()
    query_profile["inquiry_intent"] = "informational_options"
    query_profile["signals"].update(procedure_requested=False, brokerage_destination_ambiguous=True)
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), query_profile, facts())
    assert fixed["outcome"] == "blocked_missing_data"
    assert len(fixed["questions_to_ask"]) == 1
    assert info["brokerage_clarification"] is True


def test_successor_from_older_transition_is_not_reused_for_new_completed_event():
    context = facts()
    context["internal_plan_context"] = {"operational_facts": [
        {"kind": "successor_recordkeeper", "value": "Fidelity", "source": "plan_history.notes", "recorded_at": "2024-01-01", "effective_on": "2024-01-01"},
        {"kind": "custody_transition_status", "value": "completed", "source": "plan_history.notes", "recorded_at": "2026-08-01", "effective_on": "2026-08-01"},
    ]}
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    assert "Fidelity" not in fixed["response_to_participant"]["opening"]
    assert info["plan_review_required"] is True


@pytest.mark.parametrize(("successor_ref", "should_name_successor"), [("note-a", True), ("note-b", False)])
def test_successor_and_completion_require_same_record_when_refs_are_available(successor_ref, should_name_successor):
    context = facts()
    context["internal_plan_context"] = {"operational_facts": [
        {"kind": "successor_recordkeeper", "value": "Fidelity", "source": "plan_history.notes", "recorded_at": "2026-08-01", "effective_on": "2026-08-01", "record_ref": successor_ref},
        {"kind": "custody_transition_status", "value": "completed", "source": "plan_history.notes", "recorded_at": "2026-08-01", "effective_on": "2026-08-01", "record_ref": "note-a"},
    ]}
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    assert ("Fidelity" in fixed["response_to_participant"]["opening"]) is should_name_successor


def test_hold_and_zero_are_reported_without_claiming_hold_caused_balance():
    context = facts(balance=0)
    context["internal_preflight_context"]["sources"]["account_balance"]["as_of"] = "2023-04-01"
    context["internal_plan_context"] = {"operational_facts": [
        {"kind": "distribution_hold", "value": True, "source": "plan_history.notes", "recorded_at": "2026-03-26", "effective_on": "2026-03-26"},
    ]}
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    opening = fixed["response_to_participant"]["opening"].lower()
    assert "zero balance" in opening
    assert "hold" in opening
    assert "because" not in opening and "caused" not in opening
    assert fixed["response_to_participant"]["steps"] == []

@pytest.mark.parametrize('inquiry', [
    'Where did the funds in my account go?',
    'Why does my former employer account show no money?',
    'My account shows zero. Why can I not move my money?',
])
def test_zero_custody_inquiries_reach_the_guard_without_requesting_a_distribution(inquiry):
    engine = RAGEngine.__new__(RAGEngine)
    context = facts(balance=0)
    query_profile = engine._build_retrieval_profile(inquiry, 'account_balance', 'LT Trust', '401(k)', context)
    fixed, info = engine._apply_termination_response_policy(parsed(), query_profile, context)
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['response_to_participant']['steps'] == []
    assert info['custody_review_required'] is True


def test_positive_balance_custody_question_does_not_gain_distribution_steps():
    engine = RAGEngine.__new__(RAGEngine)
    context = facts(balance=10000)
    query_profile = engine._build_retrieval_profile('Where are the funds in my account held?', 'account_balance', 'LT Trust', '401(k)', context)
    response = parsed()
    response['response_to_participant']['steps'] = []
    fixed, _ = engine._apply_termination_response_policy(response, query_profile, context)
    assert fixed == response


@pytest.mark.parametrize('topic', ['hardship', 'hardship_withdrawal'])
def test_active_hardship_routes_to_its_own_complete_procedure(topic):
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile('I need a hardship withdrawal for medical bills.', topic, 'LT Trust', '401(k)',
        {'participant_data': {'employment_status': 'Active'}})
    assert result['mode'] == 'exact_procedure'
    assert result['primary_article_id'] == engine.HARDSHIP_ARTICLE_ID
    assert result['primary_action'] == 'hardship_withdrawal'
    assert engine.EXACT_TERMINATION_ROLLOVER_ARTICLE_ID in result['excluded_articles']


def test_separation_conflict_does_not_gain_hardship_procedure():
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile('I quit last week and need a hardship withdrawal.', 'hardship_withdrawal', 'LT Trust', '401(k)',
        {'participant_data': {'employment_status': 'Active'}})
    assert result['primary_article_id'] != engine.HARDSHIP_ARTICLE_ID


def test_retaining_funds_question_keeps_options_article_available():
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile('Can I keep my funds invested here, what fees apply if I stay, and can I roll over only part to a Traditional IRA?',
        'rollover', 'LT Trust', '401(k)', {'participant_data': {'employment_status': 'Terminated', 'termination_date': '2026-07-01'}})
    assert result['mode'] == 'exact_procedure'
    assert result['inquiry_intent'] == 'informational_options'
    assert engine.GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID not in result['excluded_articles']


@pytest.mark.parametrize('confirmation', [None, False, 'true', 'not reviewed', [], {}])
def test_hardship_does_not_send_request_form_before_guidelines_review_is_confirmed(confirmation):
    draft = parsed()
    draft['response_to_participant']['opening'] = 'Submit the hardship request now.'
    draft['response_to_participant']['steps'] = [{'step_number':1, 'action':'Complete the form',
        'detail':'https://secure.rightsignature.com/templates/synthetic/template-signer-link/synthetic'}]
    fixed, info = RAGEngine._apply_termination_response_policy(draft, {'primary_action':'hardship_withdrawal'}, {
        'participant_data':{'confirmation_of_review_of_hardship_distribution_guidelines_pdf':confirmation}})
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True
    assert 'secure.rightsignature.com' not in json.dumps(fixed['response_to_participant'])
    assert 'guidelines' in json.dumps(fixed['response_to_participant']).lower()
    assert info['guidelines_confirmation_required'] is True


def test_hardship_with_explicit_guidelines_confirmation_preserves_form():
    draft = parsed()
    draft['response_to_participant']['steps'] = [{'action':'Complete the verified hardship request form.'}]
    fixed, info = RAGEngine._apply_termination_response_policy(draft, {'primary_action':'hardship_withdrawal'}, {
        'participant_data':{'confirmation_of_review_of_hardship_distribution_guidelines_pdf':True}})
    assert fixed == draft
    assert info.get('guidelines_confirmation_required') is not True


def test_reported_plan_successor_guides_verification_without_claiming_individual_assets_moved():
    context = facts(balance=0)
    context['internal_plan_context'] = {'current': {'status':'terminated','active':False,'status_as_of':'2024-10-24'},
        'operational_facts':[{'kind':'servicing_successor_reported','value':'ADP','recorded_at':'2026-09-10','effective_on':None,'source':'plan_history.notes'}]}
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    text = fixed['response_to_participant']['opening']
    assert 'ADP' in text
    assert '2024-10-24' not in text
    assert 'your funds were transferred' not in text.lower()
    assert fixed['escalation']['needed'] is True
    assert info['plan_review_required'] is True


def test_retention_question_reserves_its_options_source_after_live_broad_retrieval_missed_it():
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile('Can I keep my funds invested here, what fees apply if I stay, and can I roll over only part to a Traditional IRA?',
        'rollover', 'LT Trust', '401(k)', {'participant_data': {'employment_status':'Active','termination_date':None}})
    assert result['primary_article_id'] == engine.GENERAL_POST_TERMINATION_OPTIONS_ARTICLE_ID
    assert result['primary_action'] == 'retention_options'
    draft = parsed()
    draft['response_to_participant']['steps'] = [{'action':'Submit a distribution'}]
    fixed, _ = engine._apply_termination_response_policy(draft, result, facts())
    assert fixed['response_to_participant']['steps'] == []


def test_partial_retention_does_not_treat_generic_partial_distribution_as_verified_remainder_permission():
    questions = ['Can I keep funds invested?', 'What ongoing fees apply?', 'Can I roll over only part and leave the rest?']
    context = facts()
    context['internal_response_context'] = {'requested_questions': questions}
    draft = parsed(outcome='blocked_not_eligible')
    draft['response_to_participant']['opening'] = 'You cannot take a post-employment distribution.'
    draft['response_to_participant']['key_points'] = ['1. Keeping funds depends on plan rules.', '2. Ongoing fees require verification.', '3. The plan supports voluntary partial distributions.']
    draft['question_coverage'] = [{'question_index': 2, 'status': 'answered', 'answer_reference': 'The plan supports voluntary partial distributions.'}]
    fixed, info = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'retention_options'}, context)
    assert 'leave the rest invested' in fixed['response_to_participant']['key_points'][2]
    assert 'needs to verify' in fixed['response_to_participant']['key_points'][2]
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True
    assert fixed['response_to_participant']['steps'] == []
    metadata = RAGEngine._validate_question_coverage(fixed, context)
    assert metadata['question_coverage'][2]['status'] == 'needs_verification'
    assert info['partial_retention_verification_required'] is True


def test_retention_without_partial_question_preserves_supported_answer():
    context = facts()
    context['internal_response_context'] = {'requested_questions': ['Can I keep my money invested?']}
    draft = parsed()
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'retention_options'}, context)
    assert fixed == draft


def test_authoritative_plan_identifier_rewrite_updates_coverage_reference():
    context = facts()
    context['internal_plan_disclosure_context'] = _plan_disclosure(rk_plan_id='SYNTHETIC-PLAN-CODE')
    context['internal_response_context'] = {'requested_questions': ['What is my plan ID for the rollover form?']}
    draft = parsed()
    draft['question_coverage'] = [{'question_index': 0, 'status': 'answered', 'answer_reference': 'Use SYNTHETIC-PLAN-CODE as the plan ID for your rollover form.'}]
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'plan_identifier'}, context)
    metadata = RAGEngine._validate_question_coverage(fixed, context)
    assert metadata['question_coverage'][0]['status'] == 'answered'
    assert metadata['human_review_required'] is False
    assert metadata['question_coverage'][0]['answer_reference'] == fixed['response_to_participant']['opening']


def test_missing_plan_identifier_keeps_coverage_unresolved():
    context = facts()
    context['plan_data'] = {'plan_id': 'ROUTE-ONLY'}
    context['internal_response_context'] = {'requested_questions': ['What is my plan ID?']}
    draft = parsed()
    draft['question_coverage'] = [{'question_index': 0, 'status': 'answered', 'answer_reference': 'Use ROUTE-ONLY.'}]
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'plan_identifier'}, context)
    assert RAGEngine._validate_question_coverage(fixed, context)['human_review_required'] is True


_PLAN_ID_REFUSAL = (
    'Our team needs to verify the recordkeeper plan ID for your plan before providing it for the form.'
)
_LEAKED_PLAN_CODE = 'LT99887'
_PORTAL_ONLY_ID = '9081726354'


def _plan_identifier_leak_context(disclosure):
    """Raw plan_data still carries a code; only a verified disclosure may answer."""
    context = facts()
    context['plan_data'] = {'rk_plan_id': _LEAKED_PLAN_CODE, 'plan_id': _PORTAL_ONLY_ID}
    if disclosure is not None:
        context['internal_plan_disclosure_context'] = disclosure
    return context


def _defective_plan_disclosure(case):
    if case == 'absent_disclosure':
        return None
    disclosure = _plan_disclosure(rk_plan_id=_LEAKED_PLAN_CODE)
    fact = disclosure['facts']['rk_plan_id']
    if case == 'unverified_identity':
        disclosure['identity_verified'] = False
    elif case == 'unmatched_identity':
        disclosure['identity_resolution_status'] = 'unmatched'
    elif case == 'missing_selected_plan':
        disclosure.pop('plan_id')
    elif case == 'invalid_source':
        fact['source'] = 'plan_data.rk_plan_id'
    elif case == 'missing_dates':
        fact['as_of'] = None
        fact['observed_at'] = None
    elif case == 'malformed_dates':
        fact['as_of'] = '2025-02-29'
        fact['observed_at'] = 'not-a-timestamp'
    elif case == 'sentinel':
        fact['value'] = 'unknown'
    else:
        raise AssertionError(case)
    return disclosure


@pytest.mark.parametrize('case', [
    'absent_disclosure',
    'unverified_identity',
    'unmatched_identity',
    'missing_selected_plan',
    'invalid_source',
    'missing_dates',
    'malformed_dates',
    'sentinel',
])
def test_plan_identifier_refuses_unverified_plan_data_code(case):
    """The literal plan code in plan_data is not a verified disclosure."""
    context = _plan_identifier_leak_context(_defective_plan_disclosure(case))
    fixed, _ = RAGEngine._apply_termination_response_policy(
        parsed(), {'primary_action': 'plan_identifier'}, context)
    rendered = json.dumps(fixed['response_to_participant'])
    assert _LEAKED_PLAN_CODE not in rendered
    assert _PORTAL_ONLY_ID not in rendered
    assert fixed['response_to_participant']['opening'] == _PLAN_ID_REFUSAL
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['outcome_reason'] == (
        'The recordkeeper plan ID is missing; a portal route ID is not a substitute.'
    )
    assert fixed['data_gaps'] == ['Verified recordkeeper plan ID']
    assert fixed['escalation'] == {
        'needed': True,
        'reason': 'Verify the recordkeeper identifier against the correct plan.',
    }


def test_verified_selected_plan_identifier_uses_its_own_date():
    """A verified rk_plan_id is answered with that code and its own as-of date."""
    disclosure = _plan_disclosure(
        legal_plan_name='Synthetic Fixture 401(k) Plan',
        rk_plan_id='LT99887',
        record_keeper='LT Trust',
    )
    disclosure['facts']['legal_plan_name']['as_of'] = '2024-03-15'
    disclosure['facts']['legal_plan_name']['observed_at'] = '2024-03-15T00:00:00Z'
    disclosure['facts']['record_keeper']['as_of'] = '2025-06-01'
    disclosure['facts']['record_keeper']['observed_at'] = '2025-06-01T00:00:00Z'
    context = facts()
    context['plan_data'] = {'rk_plan_id': 'UNVERIFIED-PLAN-CODE', 'plan_id': _PORTAL_ONLY_ID}
    context['internal_plan_disclosure_context'] = disclosure
    context['internal_response_context'] = {
        'requested_questions': ['What is my plan ID for the rollover form?'],
    }
    fixed, info = RAGEngine._apply_termination_response_policy(
        parsed(), {'primary_action': 'plan_identifier'}, context)
    opening = fixed['response_to_participant']['opening']
    assert opening == (
        'The recordkeeper plan ID for your plan is LT99887, '
        'from the plan record as of 2026-09-01.'
    )
    rendered = json.dumps(fixed['response_to_participant'])
    assert '2024-03-15' not in rendered
    assert '2025-06-01' not in rendered
    assert 'UNVERIFIED-PLAN-CODE' not in rendered
    assert _PORTAL_ONLY_ID not in rendered
    assert fixed['outcome'] == 'can_proceed'
    assert fixed['escalation']['needed'] is False
    assert info['identifier_answer_only'] is True
    assert fixed['question_coverage'][0]['status'] == 'answered'
    assert fixed['question_coverage'][0]['answer_reference'] == opening


# The fixtures above are hand-built in the shape of a disclosure context. The
# cases below assemble it through the production builder instead, so the branch
# is pinned to what the real scrape/identity/selected-plan path hands it.
_SCRAPED_PLAN_CODE = 'RK-0000123'
_SCRAPED_PLAN_MODULES = {
    'basic_info': {'official_plan_name': 'Synthetic Fixture 401(k) Plan'},
    'plan_design': {'rk_plan_id': _SCRAPED_PLAN_CODE, 'record_keeper_id': 'LT Trust'},
}
_SCRAPED_PLAN_META = {'extraction_diagnostics': {'modules': {
    'basic_info': {'dataState': 'ok', 'observedAt': '2026-09-11T18:00:00Z',
                   'fields': {'official_plan_name': {'dataState': 'ok', 'sourceAsOf': '2026-09-01'}}},
    'plan_design': {'dataState': 'ok', 'observedAt': '2026-09-11T18:00:00Z',
                    'fields': {'rk_plan_id': {'dataState': 'ok'},
                               'record_keeper_id': {'dataState': 'ok'}}},
}}}
_MATCHED_IDENTITY = {'identity_resolution_status': 'matched', 'identity_verified': True}


def _scraped_collected_data(**overrides):
    """collected_data exactly as the production builder assembles it."""
    from data_pipeline.gr_payload_builder import build_collected_data

    kwargs = dict(plan_meta=_SCRAPED_PLAN_META, identity_context=_MATCHED_IDENTITY,
                  selected_plan_id='222')
    kwargs.update(overrides)
    return build_collected_data(None, _SCRAPED_PLAN_MODULES, {}, **kwargs)


def test_builder_backed_plan_identifier_answers_with_the_scraped_code_and_its_own_date():
    """The plan scrape dates rk_plan_id by module observation only.

    ``plan_design.rk_plan_id`` carries no ``sourceAsOf``, so the builder leaves
    ``as_of`` empty and the answer falls back to the observed timestamp. That
    full timestamp, not the calendar date the hand-built fixtures supply, is
    what a participant reads on this path.
    """
    collected = _scraped_collected_data()
    fact = collected['internal_plan_disclosure_context']['facts']['rk_plan_id']
    assert (fact['as_of'], fact['observed_at']) == (None, '2026-09-11T18:00:00Z')
    fixed, info = RAGEngine._apply_termination_response_policy(
        parsed(), {'primary_action': 'plan_identifier'}, collected)
    assert fixed['response_to_participant']['opening'] == (
        'The recordkeeper plan ID for your plan is RK-0000123, '
        'from the plan record as of 2026-09-11T18:00:00Z.'
    )
    assert fixed['outcome'] == 'can_proceed'
    assert fixed['escalation']['needed'] is False
    assert info['identifier_answer_only'] is True


def test_builder_backed_unmatched_identity_keeps_the_scraped_code_unsaid():
    """A plan scrape that succeeded still fills plan_data for an unmatched identity."""
    collected = _scraped_collected_data(
        identity_context={'identity_resolution_status': 'ambiguous',
                          'identity_verified': True})
    assert collected['plan_data']['rk_plan_id'] == _SCRAPED_PLAN_CODE
    assert 'internal_plan_disclosure_context' not in collected
    fixed, _ = RAGEngine._apply_termination_response_policy(
        parsed(), {'primary_action': 'plan_identifier'}, collected)
    assert _SCRAPED_PLAN_CODE not in json.dumps(fixed['response_to_participant'])
    assert fixed['response_to_participant']['opening'] == _PLAN_ID_REFUSAL
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True


@pytest.mark.parametrize('override', [
    {'identity_context': None},
    {'identity_context': {'identity_resolution_status': 'matched',
                          'identity_verified': False}},
    {'selected_plan_id': None},
    {'selected_plan_id': 'PLAN-222'},
    {'plan_meta': None},
    {'plan_meta': {'extraction_diagnostics': {'modules': {'plan_design': {
        'dataState': 'parse_error', 'observedAt': '2026-09-11T18:00:00Z'}}}}},
])
def test_builder_backed_plan_identifier_refuses_without_the_upstream_binding(override):
    """Selected-plan binding lives upstream; unbound plan facts stay unsaid."""
    collected = _scraped_collected_data(**override)
    assert not collected.get('internal_plan_disclosure_context')
    fixed, _ = RAGEngine._apply_termination_response_policy(
        parsed(), {'primary_action': 'plan_identifier'}, collected)
    assert _SCRAPED_PLAN_CODE not in json.dumps(fixed['response_to_participant'])
    assert fixed['response_to_participant']['opening'] == _PLAN_ID_REFUSAL
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['data_gaps'] == ['Verified recordkeeper plan ID']


def test_employed_options_question_after_statement_is_informational():
    engine = RAGEngine.__new__(RAGEngine)
    context = {'participant_data': {'participant_status': 'Active'}}
    profile = engine._build_retrieval_profile('I am still employed. What options do I have to access funds?', 'distribution', 'LT Trust', '401(k)', context)
    assert profile['inquiry_intent'] == 'informational_options'
    draft = parsed()
    draft['outcome'] = 'ambiguous_plan_rules'
    draft['response_to_participant']['key_points'] = ['Our team can review whether your plan permits a loan or hardship withdrawal.']
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, profile, context)
    assert fixed['response_to_participant']['steps'] == []
    assert 'loan or hardship' in str(fixed['response_to_participant']['key_points'])
    assert fixed['outcome'] == 'ambiguous_plan_rules'


def test_explicit_submission_is_not_erased_by_an_options_question():
    engine = RAGEngine.__new__(RAGEngine)
    assert engine._infer_inquiry_intent('What options do I have? Submit my request now.') == 'transactional_submission'


def test_while_employed_options_omit_unasked_transaction_fees_and_invite_choice():
    engine = RAGEngine.__new__(RAGEngine)
    context = {'participant_data': {'participant_status': 'Active'}}
    profile = engine._build_retrieval_profile('I am still employed. What options do I have?', 'distribution', 'LT Trust', '401(k)', context)
    draft = parsed()
    draft['outcome'] = 'ambiguous_plan_rules'
    draft['response_to_participant']['key_points'] = ['A loan or hardship withdrawal may be worth reviewing.', 'Hardship processing fee is $75 and wire fee is $35.']
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, profile, context)
    points = str(fixed['response_to_participant']['key_points'])
    assert '$75' not in points
    assert 'which option' in points.lower()
    assert 'plan' in points.lower()


def test_options_retain_explicitly_requested_fees():
    engine = RAGEngine.__new__(RAGEngine)
    context = {'participant_data': {'participant_status': 'Active'}}
    profile = engine._build_retrieval_profile('I am still employed. What options do I have and what fees apply?', 'distribution', 'LT Trust', '401(k)', context)
    draft = parsed()
    draft['response_to_participant']['key_points'] = ['Hardship processing fee is $75.']
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, profile, context)
    assert '$75' in str(fixed['response_to_participant']['key_points'])


def test_retaining_funds_answer_invites_preference_without_initiating_transaction():
    context = facts()
    context['internal_response_context'] = {'requested_questions': ['Can I keep funds invested?', 'What ongoing fees apply?', 'Can I roll over only part?']}
    draft = parsed()
    draft['response_to_participant']['key_points'] = ['1. Keep funds.', '2. Verify ongoing fees.', '3. Verify partial rule.']
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'retention_options'}, context)
    assert 'which option' in str(fixed['response_to_participant']['key_points']).lower()
    assert fixed['response_to_participant']['steps'] == []


# ─────────────────────────────────────────────────────────────────────
# TKT-911797: a factual request for the participant's OWN account number
# that explains a rollover motive. A1 keeps it factual, A3 keeps own
# account / plan code / receiving account distinct, A5 forbids invented IDs.
# ─────────────────────────────────────────────────────────────────────

# An authored PARAPHRASE of the TKT-911797 ask, not the participant's words and
# not a live model output. The verbatim source is SOURCE_BODY.
PARAPHRASED_911797_INQUIRY = 'I need my Roth account number so I can roll it into my new employer 401k.'


def account_identifier_context(**overrides):
    context = facts()
    context['participant_data'] = {'participant_status': 'Terminated', 'termination_date': '2026-07-01'}
    context['internal_response_context'] = {'requested_questions': [PARAPHRASED_911797_INQUIRY]}
    context.update(overrides)
    return context


def test_own_account_number_request_is_not_absorbed_by_the_rollover_procedure():
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        PARAPHRASED_911797_INQUIRY, 'rollover', 'LT Trust', '401(k)', account_identifier_context())
    assert profile['primary_action'] == 'participant_account_identifier'
    assert profile['mode'] == 'broad_search'
    assert profile['primary_article_id'] is None
    assert profile['inquiry_intent'] == 'informational_options'


def test_own_account_number_answer_drops_the_terminated_rollover_macro():
    context = account_identifier_context()
    draft = parsed()
    draft['response_to_participant'] = {
        'opening': 'You are terminated and may roll over eligible funds.',
        'key_points': ['The distribution request fee is $75 and each wire costs $35.'],
        'steps': [{'step_number': 1, 'action': 'Submit the request through RightSignature.'}],
        'warnings': ['Confirm the receiving provider accepts Roth sources.'],
    }
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        PARAPHRASED_911797_INQUIRY, 'rollover', 'LT Trust', '401(k)', context)
    fixed, info = RAGEngine._apply_termination_response_policy(draft, profile, context)
    rendered = json.dumps(fixed['response_to_participant'])
    assert fixed['response_to_participant']['steps'] == []
    assert '$75' not in rendered and '$35' not in rendered
    assert 'rightsignature' not in rendered.lower()
    assert info['identifier_answer_only'] is True


def test_plan_code_is_never_served_as_the_participant_account_number():
    context = account_identifier_context()
    context['plan_data'] = {'rk_plan_id': 'SYNTHETIC-PLAN-CODE'}
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        PARAPHRASED_911797_INQUIRY, 'rollover', 'LT Trust', '401(k)', context)
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile, context)
    rendered = json.dumps(fixed['response_to_participant'])
    assert 'SYNTHETIC-PLAN-CODE' not in rendered
    assert not re.search(r'\d{4,}', rendered)
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True
    assert 'account number' in json.dumps(fixed['data_gaps']).lower()


def test_own_account_answer_keeps_plan_code_and_receiving_account_distinct():
    context = account_identifier_context()
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        PARAPHRASED_911797_INQUIRY, 'rollover', 'LT Trust', '401(k)', context)
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile, context)
    rendered = json.dumps(fixed['response_to_participant']).lower()
    # The participant's own account is named as theirs and is explicitly
    # separated from the two identifiers production conflated it with.
    assert 'your account number' in rendered
    assert 'not the recordkeeper plan code' in rendered
    assert 'not the receiving account' in rendered


def test_own_account_identifier_coverage_stays_unresolved_for_review():
    context = account_identifier_context()
    draft = parsed()
    draft['question_coverage'] = [{'question_index': 0, 'status': 'answered',
                                   'answer_reference': 'Use SYNTHETIC-PLAN-CODE as your account number.'}]
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        PARAPHRASED_911797_INQUIRY, 'rollover', 'LT Trust', '401(k)', context)
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, profile, context)
    metadata = RAGEngine._validate_question_coverage(fixed, context)
    assert metadata['question_coverage'][0]['status'] == 'needs_verification'
    assert metadata['human_review_required'] is True


@pytest.mark.parametrize('inquiry', [
    'What account number should I give my new provider to send the rollover to?',
    "I need my new employer's account number for the rollover.",
    'What is the receiving account number for the destination IRA?',
])
def test_receiving_account_questions_are_not_own_account_identifier_requests(inquiry):
    engine = RAGEngine.__new__(RAGEngine)
    context = {'participant_data': {'participant_status': 'Terminated', 'termination_date': '2026-07-01'},
               'internal_response_context': {'requested_questions': [inquiry]}}
    profile = engine._build_retrieval_profile(inquiry, 'rollover', 'LT Trust', '401(k)', context)
    assert profile['primary_action'] != 'participant_account_identifier'


def test_plan_identifier_question_keeps_its_own_route():
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        'What is my plan ID for the rollover form?', 'rollover', 'LT Trust', '401(k)',
        {'participant_data': {'participant_status': 'Terminated'}})
    assert profile['primary_action'] == 'plan_identifier'


def test_account_identifier_does_not_swallow_a_real_rollover_request():
    engine = RAGEngine.__new__(RAGEngine)
    context = {'internal_response_context': {'requested_questions': [
        'I need my Roth account number.', 'How do I start the rollover?']}}
    profile = engine._build_retrieval_profile(
        'I need my Roth account number. How do I start the rollover?',
        'rollover', 'LT Trust', '401(k)', context)
    assert profile['primary_action'] != 'participant_account_identifier'


def test_generate_response_prompts_keep_the_three_identifiers_apart():
    from data_pipeline import prompts

    outcome_system, _ = prompts.build_gr_outcome_prompt(
        context='KB', inquiry=PARAPHRASED_911797_INQUIRY, collected_data={},
        record_keeper='LT Trust', plan_type='401(k)', topic='rollover')
    response_system, _ = prompts.build_gr_response_prompt(
        context='KB', inquiry=PARAPHRASED_911797_INQUIRY, collected_data={},
        record_keeper='LT Trust', plan_type='401(k)', topic='rollover',
        outcome='blocked_missing_data', outcome_reason='identifier unverified')
    single_system, _ = prompts.build_generate_response_prompt(
        context='KB', inquiry=PARAPHRASED_911797_INQUIRY, collected_data={},
        record_keeper='LT Trust', plan_type='401(k)', topic='rollover', max_tokens=5500)
    for system_prompt in (outcome_system, response_system, single_system):
        assert 'identifier request is a factual lookup' in system_prompt
        assert 'never present one as another and never invent one' in system_prompt


@pytest.mark.parametrize('inquiry,expected', [
    # A1/A6: a stated motive stays factual; an explicit transaction request
    # alongside the identifier keeps both intents on the procedure route.
    (PARAPHRASED_911797_INQUIRY, 'participant_account_identifier'),
    ('I would like my Roth account number for the rollover paperwork.', 'participant_account_identifier'),
    ('I want to know my account number.', 'participant_account_identifier'),
    ('I left my job and want to roll over my account. Also, what is my account number?', 'termination_rollover'),
    ('Please submit my rollover; what is my account number?', 'termination_rollover'),
])
def test_identifier_motive_and_transaction_requests_route_separately(inquiry, expected):
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(
        inquiry, 'rollover', 'LT Trust', '401(k)',
        {'participant_data': {'participant_status': 'Terminated', 'termination_date': '2026-07-01'}})
    assert profile['primary_action'] == expected


# ─────────────────────────────────────────────────────────────────────
# TKT-911797 orchestration shape. The failing job extracted TWO inquiries
# from one message. The extractor restates each inquiry in the third person
# and keeps the participant's verbatim wording in requested_questions, so
# both shapes are exercised here. These strings apply that documented shape
# to the sanitized source text; they are not a live extractor run.
# ─────────────────────────────────────────────────────────────────────

SOURCE_ROLLOVER_QUOTE = 'Hello, I am trying to roll my Roth over to my new 401k.'
SOURCE_IDENTIFIER_QUOTE = 'How do I go about getting my account number?'


def routed(inquiry, questions, **collected):
    engine = RAGEngine.__new__(RAGEngine)
    context = {'participant_data': {'participant_status': 'Terminated', 'termination_date': '2026-07-01'},
               'internal_response_context': {'requested_questions': questions}}
    context.update(collected)
    profile = engine._build_retrieval_profile(inquiry, 'rollover', 'LT Trust', '401(k)', context)
    return profile, context


def macro_draft():
    draft = parsed()
    draft['response_to_participant'] = {
        'opening': 'You are terminated and may roll over your balance.',
        'key_points': ['1. Process: submit the termination distribution request.',
                       'For a wire, use the receiving provider bank account number and subaccount number.'],
        'steps': [{'step_number': 1, 'action': 'Complete the termination distribution request.'}],
        'warnings': ['Confirm the receiving provider accepts Roth sources.'],
    }
    return draft


def test_extracted_identifier_inquiry_routes_to_the_bounded_identifier_answer():
    profile, context = routed(
        'Participant is requesting their Roth account number in order to transfer it to their new 401(k).',
        [SOURCE_IDENTIFIER_QUOTE])
    assert profile['primary_action'] == 'participant_account_identifier'
    assert profile['signals']['account_identifier_requested'] is True
    fixed, info = RAGEngine._apply_termination_response_policy(macro_draft(), profile, context)
    assert fixed['response_to_participant']['steps'] == []
    assert info['participant_account_identifier'] is True
    assert fixed['outcome'] == 'blocked_missing_data'


def test_extracted_rollover_inquiry_keeps_its_procedure_route():
    profile, _ = routed(
        "Participant is trying to roll their Roth balance over to their new employer's 401(k) plan.",
        [SOURCE_ROLLOVER_QUOTE])
    assert profile['primary_action'] == 'termination_rollover'
    assert profile['signals']['account_identifier_requested'] is False


def test_unbound_process_wording_does_not_resurrect_the_distribution_macro():
    # The DevRev restatement says "seeking guidance on the process". The only
    # process the participant asked about is how to get their account number,
    # so this stays the bounded identifier answer (A1).
    profile, context = routed(
        'Participant is requesting their Roth account number and is seeking guidance on the process.',
        [SOURCE_IDENTIFIER_QUOTE])
    assert profile['signals']['account_identifier_requested'] is True
    assert profile['primary_action'] == 'participant_account_identifier'
    fixed, _ = RAGEngine._apply_termination_response_policy(macro_draft(), profile, context)
    rendered = json.dumps(fixed['response_to_participant']).lower()
    assert fixed['response_to_participant']['steps'] == []
    assert 'not the recordkeeper plan code' in rendered
    assert 'not the receiving account' in rendered


def test_process_wording_bound_to_the_transaction_keeps_the_procedure_answer():
    # A6: once the process wording is about the rollover itself, both intents
    # are live — the procedure answer stays and carries the distinction.
    profile, context = routed(
        'Participant is requesting their Roth account number and is seeking guidance on the rollover process.',
        [SOURCE_IDENTIFIER_QUOTE])
    assert profile['primary_action'] == 'termination_rollover'
    assert profile['signals']['account_identifier_requested'] is True
    fixed, info = RAGEngine._apply_account_identifier_distinctions(macro_draft(), profile, context)
    rendered = json.dumps(fixed['response_to_participant']).lower()
    assert 'not the recordkeeper plan code' in rendered
    assert 'not the receiving account' in rendered
    assert 'needs to verify your account number' in rendered
    assert info['applied'] is True


def test_mixed_rollover_and_identifier_keeps_both_intents():
    profile, context = routed(
        'Participant is requesting their Roth account number and wants to roll it over to their new 401(k).',
        [SOURCE_ROLLOVER_QUOTE, SOURCE_IDENTIFIER_QUOTE])
    assert profile['primary_action'] == 'termination_rollover'
    assert profile['signals']['account_identifier_requested'] is True
    draft = macro_draft()
    draft['question_coverage'] = [
        {'question_index': 0, 'status': 'answered', 'answer_reference': '1. Process: submit the termination distribution request.'},
        {'question_index': 1, 'status': 'answered', 'answer_reference': '1. Process: submit the termination distribution request.'},
    ]
    fixed, _ = RAGEngine._apply_account_identifier_distinctions(draft, profile, context)
    # The rollover answer is preserved; only the identifier stays unresolved.
    assert fixed['response_to_participant']['steps'] == draft['response_to_participant']['steps']
    metadata = RAGEngine._validate_question_coverage(fixed, context)
    assert metadata['question_coverage'][0]['status'] == 'answered'
    assert metadata['question_coverage'][1]['status'] == 'needs_verification'
    assert metadata['human_review_required'] is True
    assert 'Verified participant account number' in fixed['data_gaps']
    assert fixed['escalation']['needed'] is True


def test_plan_code_echoed_as_the_account_number_is_detected_and_corrected():
    # Detecting the echo is not correcting it. A draft that states "your
    # account number is <plan code>" and then denies it in the next line is
    # worse than one that never answered: the human reviewer is handed a
    # contradiction, and the wrong value is the concrete one.
    profile, context = routed(
        'Participant is requesting their Roth account number and wants to roll it over to their new 401(k).',
        [SOURCE_IDENTIFIER_QUOTE], plan_data={'rk_plan_id': 'SYNTHETIC-PLAN-CODE'})
    draft = macro_draft()
    draft['response_to_participant']['key_points'].append(
        'Your account number is SYNTHETIC-PLAN-CODE.')
    fixed, info = RAGEngine._apply_account_identifier_distinctions(draft, profile, context)
    assert info['plan_code_echoed'] is True
    assert info['identifier_claims_corrected'] == 1
    response = fixed['response_to_participant']
    rendered = json.dumps(response)
    assert 'not the recordkeeper plan code' in rendered.lower()
    # The claim itself is gone, and with no verified plan disclosure there is
    # no legitimate reason for the plan code to reach the participant at all.
    assert 'your account number is synthetic-plan-code' not in rendered.lower()
    assert 'SYNTHETIC-PLAN-CODE' not in rendered
    # Only the wrong claim is removed: the independent rollover answer stays.
    baseline = macro_draft()['response_to_participant']
    assert response['opening'] == baseline['opening']
    assert response['steps'] == baseline['steps']
    assert response['warnings'] == baseline['warnings']
    for point in baseline['key_points']:
        assert point in response['key_points']
    # The pending verification, the gap, the escalation and the coverage
    # handoff all survive the correction.
    assert RAGEngine._ACCOUNT_IDENTIFIER_PENDING in response['key_points']
    assert 'Verified participant account number' in fixed['data_gaps']
    assert fixed['escalation']['needed'] is True
    metadata = RAGEngine._validate_question_coverage(fixed, context)
    assert metadata['human_review_required'] is True
    assert metadata['question_coverage'][0]['status'] == 'needs_verification'


def test_identifier_claim_is_corrected_in_every_response_section():
    """The echo is not confined to key_points in the real schema."""
    profile, context = routed(
        'Participant is requesting their Roth account number and wants to roll it over to their new 401(k).',
        [SOURCE_IDENTIFIER_QUOTE], plan_data={'rk_plan_id': 'SYNTH01'},
        internal_plan_disclosure_context=_plan_disclosure(
            legal_plan_name='Synthetic Fixture 401(k) Plan', rk_plan_id='SYNTH01'))
    draft = macro_draft()
    response = draft['response_to_participant']
    response['opening'] = ('You are terminated and may roll over your balance. '
                           'Your account number is SYNTH01.')
    response['warnings'].append(
        'Give SYNTH01 as your account number to the receiving provider.')
    response['steps'].append(
        {'step_number': 2, 'action': 'Enter your account number SYNTH01 on the rollover form.',
         'detail': 'Your account number is SYNTH01.'})
    response['steps'].append(
        {'step_number': 3, 'action': 'Send the completed form to the receiving provider.',
         'detail': 'Your account number is SYNTH01. Keep a copy for your records.'})
    fixed, info = RAGEngine._apply_account_identifier_distinctions(draft, profile, context)
    assert info['plan_code_echoed'] is True
    assert info['identifier_claims_corrected'] == 4
    fixed_response = fixed['response_to_participant']
    rendered = json.dumps(fixed_response)
    assert 'your account number is synth01' not in rendered.lower()
    assert 'Give SYNTH01 as your account number' not in rendered
    assert 'Enter your account number SYNTH01' not in rendered
    # The independent procedure is corrected, not erased.
    assert fixed_response['opening'] == 'You are terminated and may roll over your balance.'
    assert fixed_response['warnings'] == ['Confirm the receiving provider accepts Roth sources.']
    assert fixed_response['steps'] == [
        {'step_number': 1, 'action': 'Complete the termination distribution request.'},
        {'step_number': 2, 'action': 'Send the completed form to the receiving provider.',
         'detail': 'Keep a copy for your records.'},
    ]
    assert ('For a wire, use the receiving provider bank account number and subaccount number.'
            in fixed_response['key_points'])
    # A2 is untouched: the verified plan identification is legitimate and the
    # plan code stays visible there, attributed to the plan and not to the
    # participant.
    assert any('Synthetic Fixture 401(k) Plan' in point and 'SYNTH01' in point
               for point in fixed_response['key_points'])
    assert RAGEngine._ACCOUNT_IDENTIFIER_PENDING in fixed_response['key_points']
    metadata = RAGEngine._validate_question_coverage(fixed, context)
    assert metadata['human_review_required'] is True


def test_an_opening_that_is_only_the_wrong_claim_becomes_the_pending_verification():
    profile, context = routed(
        'Participant is requesting their Roth account number and wants to roll it over to their new 401(k).',
        [SOURCE_IDENTIFIER_QUOTE], plan_data={'rk_plan_id': 'SYNTHETIC-PLAN-CODE'})
    draft = macro_draft()
    draft['response_to_participant']['opening'] = 'Your account number is SYNTHETIC-PLAN-CODE.'
    fixed, info = RAGEngine._apply_account_identifier_distinctions(draft, profile, context)
    assert info['plan_code_echoed'] is True
    # An empty opening is not a valid response; the honest statement is the
    # pending verification, never a blank or an invented identifier.
    assert fixed['response_to_participant']['opening'] == RAGEngine._ACCOUNT_IDENTIFIER_PENDING
    assert 'SYNTHETIC-PLAN-CODE' not in json.dumps(fixed['response_to_participant'])


def test_unechoed_plan_code_leaves_the_draft_sections_untouched():
    profile, context = routed(
        'Participant is requesting their Roth account number and wants to roll it over to their new 401(k).',
        [SOURCE_IDENTIFIER_QUOTE], plan_data={'rk_plan_id': 'SYNTHETIC-PLAN-CODE'})
    draft = macro_draft()
    fixed, info = RAGEngine._apply_account_identifier_distinctions(draft, profile, context)
    assert info['plan_code_echoed'] is False
    assert info['identifier_claims_corrected'] == 0
    baseline = macro_draft()['response_to_participant']
    fixed_response = fixed['response_to_participant']
    assert fixed_response['opening'] == baseline['opening']
    assert fixed_response['steps'] == baseline['steps']
    assert fixed_response['warnings'] == baseline['warnings']


def test_bounded_identifier_answer_never_discloses_plan_side_values():
    # A4/A5: no plan-side value reaches the participant from this answer, so
    # an unverified identity or a cross-plan record cannot leak through it.
    profile, context = routed(
        'Participant is requesting their Roth account number.', [SOURCE_IDENTIFIER_QUOTE],
        plan_data={'rk_plan_id': 'SYNTHETIC-PLAN-CODE', 'plan_id': '4321',
                   'legal_plan_name': 'Synthetic Plan Name'},
        participant_data={'account_number': 'ECHOED-FROM-UNVERIFIED-INPUT'})
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile, context)
    rendered = json.dumps(fixed['response_to_participant'])
    for leaked in ('SYNTHETIC-PLAN-CODE', '4321', 'Synthetic Plan Name', 'ECHOED-FROM-UNVERIFIED-INPUT'):
        assert leaked not in rendered
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True


# ─────────────────────────────────────────────────────────────────────
# TKT-911797 — the ACTUAL source text, not the shortened paraphrase.
#
#   subject: "Roth Account Number"
#   body:    "Hello, I am trying to roll my Roth over to my new 401k.
#             How do I go about getting my account number?"
#
# The rollover is the MOTIVE for one factual question. A1 therefore holds
# for the whole ticket, not only for the identifier half of it. These
# boundaries are the ones the shortened paraphrase never exercised.
# ─────────────────────────────────────────────────────────────────────

SOURCE_BODY = ('Hello, I am trying to roll my Roth over to my new 401k. '
               'How do I go about getting my account number?')
SOURCE_DESCRIPTION = ('Participant is requesting his Roth account number to transfer it '
                      'to his new 401k and is seeking guidance on the process.')


def test_motive_only_rollover_is_not_a_transaction_request():
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_own_account_identifier(SOURCE_BODY) is True
    # "I am trying to X. How do I get Y?" states a situation and asks for Y.
    assert sem.requests_transaction_action(SOURCE_BODY) is False
    assert sem.requests_transaction_action(SOURCE_DESCRIPTION) is False
    # A purpose clause in the shortened paraphrase behaves identically.
    assert sem.requests_transaction_action(PARAPHRASED_911797_INQUIRY) is False


@pytest.mark.parametrize('text', [
    'I left my job and want to roll over my account. Also, what is my account number?',
    'Please submit my rollover; what is my account number?',
    'What is my account number, and how do I start the rollover?',
    'I need my account number to roll this over, and please send me the distribution forms.',
    'Can I roll over my balance? Also I need my account number.',
    'What is the process to roll over my 401k? My account number would help too.',
])
def test_a6_explicit_transaction_requests_are_still_transaction_requests(text):
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_transaction_action(text) is True


@pytest.mark.parametrize('text', [
    # modifier BEFORE "account number"
    "I need my new employer's account number for the rollover.",
    'What is my receiving account number at the destination IRA?',
    # modifier AFTER "account number" (second grammatical position)
    'What is the account number for my new employer 401k plan?',
    'Can you confirm the account number for my outside IRA?',
])
def test_receiving_account_modifiers_are_excluded_in_both_positions(text):
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_own_account_identifier(text) is False


@pytest.mark.parametrize('text', [
    'How do I go about getting my account number?',
    'What is the account number for my Roth balance?',
    "Please send the participant's account number.",
])
def test_own_account_requests_survive_the_modifier_guard(text):
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_own_account_identifier(text) is True


@pytest.mark.parametrize('text', [
    # No comma: the purpose adjunct must stop at the coordinated request, not
    # swallow the rest of the sentence.
    'I need my account number to roll this over and please send me the distribution forms.',
    'Send me my account number to roll it over and I would like to start the withdrawal.',
    # "can you" is as much a request as "can I".
    'Can you start my rollover? Also, what is my account number?',
    'Could you please process my distribution and tell me my account number?',
    # A bare or "kindly" imperative addressed to support.
    'Kindly process my rollover and send me my account number.',
    'Process my rollover and send me my account number.',
])
def test_a6_transaction_requests_survive_without_a_clause_comma(text):
    """A real request must not be erased by the motive-stripping pass.

    Each of these carries BOTH a motive and a genuine transaction request. If
    the request is not seen, the orchestrator folds the inquiry away and the
    participant's actual ask is answered by an identifier-only reply.
    """
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_transaction_action(text) is True


@pytest.mark.parametrize('text', [
    'Participant is requesting their account number to roll it over and wants to start '
    'the distribution.',
    'Participant is requesting their account number to transfer it and needs to begin '
    'the withdrawal.',
    'Participant is asking for their account number to roll it over and is requesting '
    'that the rollover be processed.',
])
def test_a6_transaction_requests_survive_a_third_person_coordinator(text):
    """The extractor paraphrases in the third person, so the coordinator that
    opens a second real request reads "and wants to ...", not "and I need ...".

    Recognising only first-person/imperative coordinators let the purpose
    adjunct run to the end of the sentence and delete the transaction request
    with it, so the inquiry routed to an identifier-only answer and the
    distribution the participant explicitly asked for went unanswered.
    """
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_transaction_action(text) is True


def test_a_third_person_coordinator_that_is_not_a_request_stays_in_the_adjunct():
    """Only a REQUEST verb closes the adjunct; "and is seeking guidance" does
    not, so the motive-only description keeps its non-transactional reading."""
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_transaction_action(SOURCE_DESCRIPTION) is False


@pytest.mark.parametrize('text', [
    'Participant wants to know if their employer match is vested before a rollover.',
    'Participant is asking whether the rollover is taxable.',
    'Participant wants to confirm how long a transfer takes.',
    'Participant is unsure whether their distribution has been processed.',
    'Participant is requesting clarification on the rollover paperwork.',
])
def test_a_reported_question_is_not_a_background_statement(text):
    """A third-person paraphrase of a question has no question mark and often
    no wh-word, but it still asks for something.

    Treating it as background let the orchestrator fold it away as the
    survivor's motive, discarding its verbatim ``requested_questions``, and let
    ``rag_engine`` drop it from the routing decision so a bounded
    identifier-only answer was returned instead.
    """
    from data_pipeline import inquiry_semantics as sem

    assert sem.is_background_statement(text) is False


@pytest.mark.parametrize('text', [
    'Participant is trying to roll their Roth over to a new 401(k).',
    'Participant is rolling their balance into an outside IRA.',
])
def test_a_genuine_motive_statement_is_still_background(text):
    """The TKT-911797 motive must keep folding: it asks for nothing."""
    from data_pipeline import inquiry_semantics as sem

    assert sem.is_background_statement(text) is True


@pytest.mark.parametrize('text', [
    # Destination modifier ends the sentence: no trailing whitespace to match.
    'What is the account number for my IRA?',
    'Can you confirm the account number of my IRA?',
    'Please confirm the account number for my new 401k',
    # Destination clause AFTER a first-position possessive: the leftmost
    # alternative matches first, so the trailing clause must still be read.
    'Can you give me my account number for my new rollover IRA?',
    'I need my account number at my new provider.',
])
def test_receiving_account_modifiers_are_excluded_at_sentence_end(text):
    from data_pipeline import inquiry_semantics as sem

    assert sem.requests_own_account_identifier(text) is False


def test_verified_plan_point_attributes_each_fact_to_its_own_date():
    """Borrowing one fact's observation date for another misstates provenance."""
    context = _plan_disclosure(legal_plan_name='Synthetic Fixture 401(k) Plan',
                              rk_plan_id='SYNTH01')
    context['facts']['rk_plan_id']['as_of'] = '2026-07-15'
    context['facts']['rk_plan_id']['observed_at'] = '2026-07-15T00:00:00Z'
    point = RAGEngine._verified_plan_identity_point(
        {'internal_plan_disclosure_context': context})

    assert 'the plan name as of 2026-09-01' in point
    assert 'the recordkeeper plan code as of 2026-07-15' in point
    # The shared-date wording must not claim a single date for both.
    assert 'from the plan record as of 2026-09-01.' not in point


def test_verified_plan_point_names_the_recordkeeper_itself():
    """A2 asks for the recordkeeper, not only the plan code it issued."""
    point = RAGEngine._verified_plan_identity_point({
        'internal_plan_disclosure_context': _plan_disclosure(
            legal_plan_name='Synthetic Fixture 401(k) Plan',
            rk_plan_id='SYNTH01', record_keeper='LT Trust')})

    assert 'Synthetic Fixture 401(k) Plan' in point
    assert 'SYNTH01' in point
    assert 'LT Trust' in point
    assert 'from the plan record as of 2026-09-01' in point


def test_actual_source_routes_to_the_bounded_identifier_answer():
    # The extractor restates in third person and quotes the participant
    # verbatim in requested_questions. The rollover sentence is a background
    # statement, so it must not pull the inquiry back onto the macro.
    profile, context = routed(
        'Participant is requesting their Roth account number so they can roll their '
        'balance over to their new employer 401(k).',
        [SOURCE_BODY])
    assert profile['primary_action'] == 'participant_account_identifier'
    fixed, info = RAGEngine._apply_termination_response_policy(macro_draft(), profile, context)
    rendered = json.dumps(fixed['response_to_participant'])
    assert fixed['response_to_participant']['steps'] == []
    assert 'receiving provider bank account number' not in rendered
    assert fixed['outcome'] == 'blocked_missing_data'
    assert info['participant_account_identifier'] is True


def test_two_llm_output_inquiries_are_canonicalised_before_routing():
    # The failing job emitted TWO inquiries from this one message. The motive
    # half must be folded back into the identifier request before routing.
    from data_pipeline.ticket_orchestrator import ExtractedInquiry, TicketOrchestrator

    extracted = [
        ExtractedInquiry(
            inquiry="Participant is trying to roll their Roth balance over to their new employer's 401(k).",
            record_keeper='LT Trust', plan_type='401(k)', topic='rollover',
            related_inquiries=['Participant is requesting their Roth account number.'],
            requested_questions=['Hello, I am trying to roll my Roth over to my new 401k.'],
        ),
        ExtractedInquiry(
            inquiry='Participant is requesting their Roth account number.',
            record_keeper='LT Trust', plan_type='401(k)', topic='balance_inquiry',
            related_inquiries=["Participant is trying to roll their Roth balance over to their new employer's 401(k)."],
            requested_questions=['How do I go about getting my account number?'],
        ),
    ]
    req = _source_request()
    folded = TicketOrchestrator._fold_motive_only_inquiries(extracted, req)
    assert len(folded) == 1
    assert 'account number' in folded[0].inquiry.lower()
    # Nothing is lost: the stated motive stays as context on the survivor.
    assert any('roll' in item.lower() for item in folded[0].related_inquiries or [])
    assert folded[0].requested_questions == ['How do I go about getting my account number?']


def test_a6_two_real_inquiries_are_not_folded():
    from data_pipeline.ticket_orchestrator import ExtractedInquiry, TicketOrchestrator

    extracted = [
        ExtractedInquiry(
            inquiry='Participant left their employer and wants to roll over their balance.',
            record_keeper='LT Trust', plan_type='401(k)', topic='termination_distribution_request'),
        ExtractedInquiry(
            inquiry='Participant is requesting their account number.',
            record_keeper='LT Trust', plan_type='401(k)', topic='balance_inquiry'),
    ]
    req = _source_request(body='I left my job and want to roll over my account. '
                               'Also, what is my account number?')
    assert TicketOrchestrator._fold_motive_only_inquiries(extracted, req) == extracted


def _source_request(body=SOURCE_BODY, subject='Roth Account Number'):
    from api.models import HandleTicketRequest

    return HandleTicketRequest(
        participant_id='158948', plan_id='580', company_name='StarWars Inc.',
        company_status='Terminated', company_status_detail=None, record_keeper='LT Trust',
        ticket={'username': 'Participant', 'user_email': 'p@example.com',
                'email_subject': subject, 'email_body': body},
    )


def _plan_disclosure(**facts):
    entries = {
        'legal_plan_name': ('basic_info', 'official_plan_name'),
        'rk_plan_id': ('plan_design', 'rk_plan_id'),
        'record_keeper': ('plan_design', 'record_keeper_id'),
    }
    return {'plan_id': '580', 'identity_verified': True, 'identity_resolution_status': 'matched',
            'facts': {key: {'value': value, 'status': 'known',
                            'source': f'plan.{entries[key][0]}.{entries[key][1]}',
                            'observed_at': '2026-09-04T00:00:00Z', 'as_of': '2026-09-01'}
                      for key, value in facts.items()}}


def test_a2_verified_plan_is_named_with_provenance_and_is_not_the_account_number():
    profile, context = routed(
        'Participant is requesting their Roth account number.', [SOURCE_BODY],
        internal_plan_disclosure_context=_plan_disclosure(
            legal_plan_name='Synthetic Fixture 401(k) Plan', rk_plan_id='SYNTH01'))
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile, context)
    rendered = json.dumps(fixed['response_to_participant'])
    assert 'Synthetic Fixture 401(k) Plan' in rendered
    assert 'SYNTH01' in rendered
    assert 'not your account number' in rendered.lower()
    # A2 names the plan; A5 still refuses to invent the participant identifier.
    assert fixed['outcome'] == 'blocked_missing_data'
    assert 'Verified participant account number' in fixed['data_gaps']


def test_a2_fails_closed_when_the_plan_fact_has_no_verified_source():
    broken = _plan_disclosure(rk_plan_id='SYNTH01')
    broken['facts']['rk_plan_id']['observed_at'] = None
    broken['facts']['rk_plan_id']['as_of'] = None
    profile, context = routed(
        'Participant is requesting their Roth account number.', [SOURCE_BODY],
        internal_plan_disclosure_context=broken)
    fixed, _ = RAGEngine._apply_termination_response_policy(parsed(), profile, context)
    rendered = json.dumps(fixed['response_to_participant'])
    assert 'SYNTH01' not in rendered
    assert fixed['outcome'] == 'blocked_missing_data'


def test_own_account_identifier_request_collects_plan_design_and_plan_name():
    from data_pipeline.ticket_orchestrator import ExtractedInquiry, TicketOrchestrator

    ext = ExtractedInquiry(
        inquiry='Participant is requesting their Roth account number.',
        record_keeper='LT Trust', plan_type='401(k)', topic='balance_inquiry')
    modules = TicketOrchestrator._case_modules(ext, _source_request())
    by_key = {m['key']: m['fields'] for m in modules}
    assert 'rk_plan_id' in by_key['plan_design']
    assert 'record_keeper_id' in by_key['plan_design']
    assert 'Legal Plan Name' in by_key['basic_info']


# ---------------------------------------------------------------------------
# Hardship closure matrix (TKT-912262 / TKT-910381 / TKT-909652).
#
# The four audited axes already behave correctly in the source; no backend
# defect was reproducible. What these add is acceptance-operator strength.
#
# Verified by mutation, not by assumption. Two INDEPENDENT guards keep an
# Active + declared-separation participant out of the hardship form flow:
#   A  rag_engine.py:2517-2518 - an explicit separation claim promotes
#      `termination_distribution`, so `exact_termination_distribution` wins
#      the primary_action elif-chain ahead of the hardship arm.
#   B  rag_engine.py:2746 - `exact_hardship_request` separately refuses an
#      explicit separation claim.
# Removing B ALONE killed nothing in the 138-test file: B is a live but
# entirely uncovered redundancy, so it could be deleted with a green suite.
# Removing A+B failed the pre-existing article-id guard AND the two new
# cross-layer tests below. The genuinely new coverage here is the
# `guidelines_confirmation_required` binding: no existing test asserted that
# the profile built from the conflict input cannot drive the draft into the
# Guidelines/request-form arm of `_apply_termination_response_policy`.
#
# Inputs are the authentic Active-employment + declared-separation shape used
# by the hardship closure pack. Every LLM/runtime value is DERIVED CONTROL,
# not a captured run: these are isolated component tests, not ticket proof.
# ---------------------------------------------------------------------------

_HARDSHIP_ACTIVE_CONTEXT = {"participant_data": {"employment_status": "Active"}}


def test_declared_separation_withholds_the_hardship_primary_action_not_only_the_article():
    """Active + declared separation must not reach the hardship form branch.

    The Guidelines / request-form arm of `_apply_termination_response_policy`
    dispatches on `primary_action`, so `primary_action` — not only
    `primary_article_id` — has to be withheld. Fails under mutant A+B.
    """
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile(
        'I quit last week and need a hardship withdrawal.',
        'hardship_withdrawal', 'LT Trust', '401(k)', _HARDSHIP_ACTIVE_CONTEXT)
    assert result['primary_action'] != 'hardship_withdrawal'
    assert result['signals']['explicit_separation_claim'] is True
    assert result['separation_status_conflict'] is True
    assert engine.HARDSHIP_ARTICLE_ID in result['excluded_articles']


def test_declared_separation_profile_never_produces_a_guidelines_prerequisite():
    """Cross-layer: the profile actually built from the conflict input must
    not drive the draft into a Guidelines-confirmation blocker, which would
    re-offer an active-only option the retrieval layer just excluded. This is
    the assertion no pre-existing test made. Fails under mutant A+B."""
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile(
        'I quit last week and need a hardship withdrawal.',
        'hardship_withdrawal', 'LT Trust', '401(k)', _HARDSHIP_ACTIVE_CONTEXT)
    draft = parsed()
    draft['response_to_participant']['steps'] = [{'step_number': 1, 'action': 'Complete the hardship form'}]
    fixed, info = RAGEngine._apply_termination_response_policy(
        draft, result, _HARDSHIP_ACTIVE_CONTEXT)
    assert info.get('guidelines_confirmation_required') is not True
    assert 'hardship distribution guidelines' not in json.dumps(fixed).lower()


def test_active_hardship_without_a_separation_claim_still_reaches_the_guidelines_gate():
    """Negative control for the two tests above: the guard must be specific to
    a declared separation and must not disable the ordinary hardship gate."""
    engine = RAGEngine.__new__(RAGEngine)
    result = engine._build_retrieval_profile(
        'I need a hardship withdrawal for medical bills.',
        'hardship_withdrawal', 'LT Trust', '401(k)', _HARDSHIP_ACTIVE_CONTEXT)
    assert result['primary_action'] == 'hardship_withdrawal'
    draft = parsed()
    draft['response_to_participant']['steps'] = [{'step_number': 1, 'action': 'Complete the hardship form'}]
    fixed, info = RAGEngine._apply_termination_response_policy(
        draft, result, _HARDSHIP_ACTIVE_CONTEXT)
    assert info['guidelines_confirmation_required'] is True
    assert fixed['outcome'] == 'blocked_missing_data'
    assert fixed['escalation']['needed'] is True


@pytest.mark.parametrize('confirmation', ['True', 1, 'yes', 'TRUE'])
def test_guidelines_confirmation_is_identity_true_only(confirmation):
    """Truthy-but-not-True confirmations must stay unconfirmed. Guards the
    `is True` comparison against a future `bool(...)`/truthy relaxation, which
    would silently release the request form on a string the extractor emitted.
    """
    draft = parsed()
    draft['response_to_participant']['steps'] = [{'step_number': 1, 'action': 'Complete the form'}]
    fixed, info = RAGEngine._apply_termination_response_policy(
        draft, {'primary_action': 'hardship_withdrawal'},
        {'participant_data': {
            'confirmation_of_review_of_hardship_distribution_guidelines_pdf': confirmation}})
    assert info['guidelines_confirmation_required'] is True
    assert fixed['outcome'] == 'blocked_missing_data'
    assert 'Confirmed review of the Hardship Distribution Guidelines' in fixed['data_gaps']


# ---------------------------------------------------------------------------
# Frozen-observation regressions (backend five-gap batch).
# Ticket fixtures are paraphrased simulations; no current participant facts.
# ---------------------------------------------------------------------------


def contested_zero_facts():
    """TKT-912043 shape: zero Account Balance, recorded loan, crypto enrolled,
    holdings unknown, plus undated source rows that are NOT current custody proof."""
    context = facts("positive", None, balance=0)
    context["internal_preflight_context"]["crypto"]["holdings"] = {"value": None, "status": "unknown"}
    context["internal_preflight_context"]["sources"].update({
        "employee_deferral_balance": {"status": "known", "value": 14000, "source": "savings_rate"},
        "roth_deferral_balance": {"status": "known", "value": 1000, "source": "savings_rate"},
        "employer_match_balance": {"status": "known", "value": 5000, "source": "savings_rate"},
        "rollover_balance": {"status": "known", "value": 0, "source": "savings_rate"},
    })
    return context


def test_zero_total_with_positive_loan_does_not_drop_loan_crypto_preflight():
    fixed, info = RAGEngine._apply_termination_response_policy(
        parsed(), profile(rollover=False), contested_zero_facts(),
    )
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert info["custody_review_required"] is True
    assert info["zero_total_conflict"] is True
    assert "outstanding loan is recorded" in text
    assert "positions have not been verified" in text
    # Enrollment is never served as proof of holdings.
    assert "if crypto enrollment is active" not in text
    # A contested total is not a confirmed empty account.
    assert "confirmed zero" not in json.dumps(fixed).lower()
    assert fixed["outcome"] == "blocked_missing_data"
    assert fixed["response_to_participant"]["steps"] == []
    assert fixed["questions_to_ask"] == []


def test_zero_total_does_not_treat_stale_source_breakdown_as_current_custody_proof():
    fixed, _ = RAGEngine._apply_termination_response_policy(
        parsed(), profile(rollover=False), contested_zero_facts(),
    )
    blob = json.dumps(fixed)
    for amount in ("14000", "14,000", "1000", "5000"):
        assert amount not in blob
    assert "outstanding loan is recorded" in blob.lower()


def test_proven_empty_zero_balance_keeps_the_plain_custody_review():
    """Control: a true zero with no loan and verified zero holdings is unchanged."""
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), facts(balance=0))
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert info["custody_review_required"] is True
    assert not info.get("zero_total_conflict")
    assert "outstanding loan" not in text
    assert "crypto" not in text


def test_zero_total_with_crypto_enrollment_and_unknown_holdings_is_not_proven_empty():
    context = facts("zero", None, balance=0)
    context["internal_preflight_context"]["crypto"]["holdings"] = {"value": None, "status": "unknown"}
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(), context)
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert info["zero_total_conflict"] is True
    assert "positions have not been verified" in text
    assert "outstanding loan is recorded" not in text


def test_missing_preflight_does_not_condition_crypto_on_enrollment():
    """TKT-911756: the no-preflight fallback must not read enrollment as holdings."""
    fixed, info = RAGEngine._apply_termination_response_policy(parsed(), profile(rollover=True), {})
    text = json.dumps(fixed["response_to_participant"]).lower()
    assert not info.get("structured_preflight_applied")
    assert "if crypto enrollment is active" not in text
    assert "crypto enrollment" not in text
    if "crypto" in text:
        assert "verify" in text
    assert "outstanding loan" in text
    assert "final payroll" in text


def test_leave_and_possible_return_is_not_termination_distribution():
    """TKT-911961: the product name 'separation distribution' is not an
    employment claim, and leave with a possible return is not a separation."""
    engine = RAGEngine.__new__(RAGEngine)
    signals = engine._infer_retrieval_signals(
        "I am on leave and may return. Can I take a separation distribution?",
        "identity", None,
    )
    assert signals["termination_distribution"] is False
    assert signals.get("explicit_separation_claim") is not True


@pytest.mark.parametrize("inquiry", [
    "Can I take a separation distribution?",
    "What is the separation distribution paperwork?",
    "Please send the separation from service distribution form.",
])
def test_advisory_separation_token_does_not_fire_on_product_name_only(inquiry):
    from data_pipeline.rag_engine import detect_advisory_concepts

    concepts = detect_advisory_concepts(inquiry, "identity", None)
    assert concepts["separation_signal"] is False
    assert "termination_distribution_request" not in concepts["detected_concepts"]


@pytest.mark.parametrize("inquiry", [
    "I separated from my employer last month. What are my options?",
    "I left my job and want to roll over my 401(k).",
    "Can I access funds from my former employer plan?",
    # Severance vocabulary is a genuine separation claim, not a product name.
    "I received my separation package. What happens to my 401(k)?",
    "I submitted my separation paperwork last week.",
])
def test_genuine_separation_language_still_signals_termination(inquiry):
    from data_pipeline.rag_engine import detect_advisory_concepts

    assert detect_advisory_concepts(inquiry, None, None)["separation_signal"] is True


def test_reported_separation_claim_survives_the_product_name_guard():
    """Control for TKT-910920/TKT-911758: the explicit-claim path is untouched."""
    from data_pipeline.rag_engine import detect_advisory_concepts

    claimed = detect_advisory_concepts("I no longer work there. Why does my account say Active?", None, None)
    assert claimed["explicit_separation_claim"] is True
    product_only = detect_advisory_concepts("Can I take a separation distribution?", None, None)
    assert product_only["explicit_separation_claim"] is False


def test_failed_password_reset_sets_account_access_signal():
    """TKT-910081: the KQ recovery policy and the GR signals must agree."""
    engine = RAGEngine.__new__(RAGEngine)
    signals = engine._infer_retrieval_signals(
        "My password reset did not work. How can I recover access?", "account_access", None,
    )
    assert signals["account_access"] is True
    _, info = RAGEngine._apply_account_recovery_knowledge_policy(
        {"answer": "", "key_points": []}, "My password reset did not work. How can I recover access?",
    )
    assert info["reason"] == "password_reset_failed"


def test_unsolicited_reset_mention_is_not_a_failed_recovery_signal():
    """Control: merely mentioning a reset is not a failed recovery attempt."""
    from data_pipeline.rag_engine import mentions_failed_password_reset

    assert mentions_failed_password_reset("I received a password reset email I did not request.") is False
    assert mentions_failed_password_reset("How do I do a password reset?") is False
    assert mentions_failed_password_reset("My password reset did not work.") is True
    assert mentions_failed_password_reset(
        "I reset my password but I still cannot log in.",
    ) is True
    assert mentions_failed_password_reset(
        "My password reset did not work, but I can now log in.",
    ) is False


# --- TKT-911961 sibling: the "termination distribution" PRODUCT NAME ---------
# Same class as the "separation distribution" guard above. A participant on
# leave with an unknown employment record who asks about the product must not
# be recorded as having been terminated, nor have a distribution authorized,
# purely because the product noun phrase carries a bare "termination" token.


@pytest.mark.parametrize("inquiry", [
    "I am on leave and may return. Can I take a termination distribution?",
    "What is the termination distribution paperwork?",
    "Please send the termination distribution request form.",
    "How long does a termination withdrawal take?",
])
def test_termination_product_name_is_not_an_employment_claim(inquiry):
    from data_pipeline.rag_engine import detect_advisory_concepts

    concepts = detect_advisory_concepts(inquiry, "identity", None)
    assert concepts["separation_signal"] is False
    assert concepts["explicit_separation_claim"] is False
    assert "termination_distribution_request" not in concepts["detected_concepts"]


def test_termination_product_name_does_not_authorize_a_distribution():
    """Leave + unknown employment: asking about the product asserts nothing."""
    engine = RAGEngine.__new__(RAGEngine)
    signals = engine._infer_retrieval_signals(
        "I am on leave and may return. Can I take a termination distribution?",
        "identity", None,
    )
    assert signals["termination_distribution"] is False
    assert signals["employment_state"] == "unknown"
    assert signals.get("explicit_separation_claim") is not True


@pytest.mark.parametrize("inquiry", [
    # Genuine employment claims keep firing even when the product is named.
    "I was terminated last month. Can I take a termination distribution?",
    "I no longer work there. How do I request a termination distribution?",
    "After my termination, can I roll over my 401(k)?",
    "My termination date was in June and I want to withdraw.",
    "I was terminated and need to access funds.",
])
def test_genuine_termination_language_still_signals_separation(inquiry):
    from data_pipeline.rag_engine import detect_advisory_concepts

    assert detect_advisory_concepts(inquiry, None, None)["separation_signal"] is True


def test_actual_terminated_record_still_routes_as_termination_distribution():
    """Control: the system-of-record status, not the product noun, decides."""
    engine = RAGEngine.__new__(RAGEngine)
    signals = engine._infer_retrieval_signals(
        "Can I take a termination distribution?", "identity",
        {"participant_data": {"employment_status": "Terminated",
                              "termination_date": "2026-06-30"}},
    )
    assert signals["employment_state"] == "terminated"
    assert signals["termination_distribution"] is True
