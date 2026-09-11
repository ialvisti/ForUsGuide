"""Frozen-scope simulation contracts; live semantic results are a separate gate."""
import json
from pathlib import Path

import pytest

from data_pipeline.rag_engine import RAGEngine
from scripts.replay_pa_integral import case_collected_data

FIXTURE = json.loads((Path(__file__).parents[1] / 'rag-testing/pa_integral_cases.json').read_text())
CASES = FIXTURE['cases']


def test_frozen_scope_and_human_provenance_are_complete():
    assert FIXTURE['count'] == len(CASES) == 34
    assert len({case['review_id'] for case in CASES}) == 34
    assert FIXTURE['excluded_user_data_changed_ticket'] not in {case['ticket_id'] for case in CASES}
    for case in CASES:
        assert case['rating'] in {1, 2, 3, 4}
        assert case['review_state'] == 'reviewed'
        assert case['associated_runs']
        assert case['human_reply_available'] is True
        assert case['human_reply_count'] > 0
        assert case['fixture_kind'] == 'simulation'
        assert case['historical_cutoff']
        assert case['human_learning'] in case['acceptance_checks']


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['ticket_id'])
def test_replay_preserves_case_conditions_and_independent_human_expectations(case):
    inputs = case['test_input']
    # The expected PA answer is never injected into the model input.
    assert case['human_learning'] not in json.dumps(inputs)
    if inputs['route'] == 'knowledge_question':
        if case['scenario'] == 'identity':
            response, metadata = RAGEngine._apply_identity_knowledge_policy(
                {'answer': 'Submit a withdrawal.', 'key_points': []}, inputs['identity_context'])
            assert metadata['human_review_required'] is True
            assert 'Submit' not in response['answer']
        return
    collected = case_collected_data(case)
    assert collected['internal_response_context']['requested_questions'] == case['requested_questions']
    engine = RAGEngine.__new__(RAGEngine)
    profile = engine._build_retrieval_profile(case['inquiry'], inputs['topic'], inputs['record_keeper'], inputs['plan_type'], collected)
    if case['scenario'] in {'custody', 'hold', 'successor', 'terminated_plan'}:
        draft = {'outcome': 'can_proceed', 'response_to_participant': {'steps': ['Submit to the old provider.']}}
        response, info = engine._apply_termination_response_policy(draft, profile, collected)
        assert response['outcome'] != 'can_proceed'
        assert response['response_to_participant']['steps'] == []
        assert info.get('custody_review_required') or info.get('plan_review_required')
        if case['scenario'] == 'successor':
            assert 'Fidelity' in response['response_to_participant']['opening']
        elif case['scenario'] == 'terminated_plan':
            assert 'ADP' not in json.dumps(response)
    if case['scenario'] == 'identifier':
        assert profile['primary_action'] == 'plan_identifier'
    if case['ticket_id'] == 'TKT-910949':
        sources = collected['internal_preflight_context']['sources']
        assert sources['employer_match_balance']['value'] > 0
        assert sources['employer_match_vested_balance']['value'] == 0
        assert sources['vested_balance']['status'] == 'unknown'
    if case['ticket_id'] == 'TKT-911758':
        assert profile['signals']['employment_state'] == 'terminated'
        assert engine.IN_SERVICE_ARTICLE_ID in profile['excluded_articles']


@pytest.mark.asyncio
async def test_replays_use_production_request_budget_and_record_it(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from data_pipeline.rag_engine import GenerateResponseResult
    from scripts.replay_pa_integral import run_replays
    sample = next(case for case in CASES if case['scenario'] == 'informational')
    engine = SimpleNamespace(generate_response=AsyncMock(return_value=GenerateResponseResult(
        decision='uncertain', confidence=0.5, response={}, source_articles=[], used_chunks=[], coverage_gaps=[], metadata={})))
    await run_replays(engine, {'count':1, 'cases':[sample], 'scope_sha256':FIXTURE['scope_sha256']}, tmp_path, {'fixture_kind':'simulation'})
    assert engine.generate_response.await_args.kwargs['max_response_tokens'] == 5500
    saved = json.loads((tmp_path / (sample['ticket_id']+'.json')).read_text())
    assert saved['request_parameters']['max_response_tokens'] == 5500


@pytest.mark.parametrize('confirmation,expected', [
    ('I have reviewed the Hardship Distribution Guidelines PDF.', True),
    ('I have not reviewed the Hardship Distribution Guidelines PDF.', None),
])
def test_simulated_conversation_confirmation_reaches_the_procedural_gate(confirmation, expected):
    from copy import deepcopy
    case = deepcopy(next(case for case in CASES if case['scenario'] == 'hardship'))
    key = 'confirmation_of_review_of_hardship_distribution_guidelines_pdf'
    case['inquiry'] += ' ' + confirmation
    case['test_input']['ticket_extracted'] = {key: {'value': True, 'evidence': confirmation}}
    collected = case_collected_data(case)
    assert collected['participant_data'].get(key) is expected
    draft = {'outcome': 'can_proceed', 'response_to_participant': {'steps': [
        {'step_number': 1, 'action': 'Complete the approved hardship request form.'},
    ]}}
    fixed, info = RAGEngine._apply_termination_response_policy(draft, {'primary_action': 'hardship_withdrawal'}, collected)
    if expected is True:
        assert fixed == draft
        assert not info.get('hardship_guidelines_review_required')
    else:
        assert fixed['outcome'] == 'blocked_missing_data'
        assert fixed['escalation']['needed'] is True
