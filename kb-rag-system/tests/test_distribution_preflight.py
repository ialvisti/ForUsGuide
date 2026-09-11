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
    context['plan_data'] = {'rk_plan_id': 'SYNTHETIC-PLAN-CODE'}
    fixed, _ = RAGEngine._apply_termination_response_policy(response, actual_profile, context)
    assert 'SYNTHETIC-PLAN-CODE' in fixed['response_to_participant']['opening']
    assert fixed['response_to_participant']['steps'] == []


def test_plan_identifier_answer_does_not_grow_into_a_distribution_tutorial():
    context = facts()
    context['plan_data'] = {'rk_plan_id': 'SYNTHETIC-PLAN-CODE'}
    draft = parsed()
    draft['response_to_participant'] = {'opening': 'You are terminated and may roll over eligible funds.',
        'key_points': ['$75 distribution fee and $35 wire fee.'], 'steps': [{'action': 'Submit a rollover.'}], 'warnings': ['Verify bank details.']}
    fixed, _ = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'plan_identifier'}, context)
    assert fixed['response_to_participant']['opening'] == 'The recordkeeper plan ID for your plan is SYNTHETIC-PLAN-CODE.'
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
    context['plan_data'] = {'rk_plan_id': 'SYNTHETIC-PLAN-CODE'}
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
