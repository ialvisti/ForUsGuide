"""A generated draft is not proof that every question is safe to publish."""

from types import SimpleNamespace

import pytest

from api.ticket_worker import _entry_from_outcome, aggregate_states
from data_pipeline.rag_engine import RAGEngine
from data_pipeline.ticket_orchestrator import InquiryOutcome


def test_question_coverage_requires_evidence_in_final_response():
    context = {"internal_response_context": {"requested_questions": ["What fees apply?", "Can I move part?", "Where are my funds?"]}}
    response = {"response_to_participant": {"opening": "A partial rollover depends on the plan rules.", "key_points": [], "steps": [], "warnings": []},
                "question_coverage": [{"question_index": 0, "status": "answered", "answer_reference": "The fee is $0"},
                                      {"question_index": 1, "status": "answered", "answer_reference": "A partial rollover depends on the plan rules."}]}
    metadata = RAGEngine._validate_question_coverage(response, context)
    assert [entry["status"] for entry in metadata["question_coverage"]] == ["needs_verification", "answered", "needs_verification"]
    assert metadata["incomplete_question_count"] == 2
    assert metadata["human_review_required"] is True
    assert "question_coverage" not in response  # internal metadata has one home


@pytest.mark.parametrize("coverage", [None, [], {"question_index": 0}, ["wrong"], [{"question_index": True, "status": "answered", "answer_reference": "A fact."}]])
def test_invalid_coverage_does_not_certify_an_answer(coverage):
    response = {"response_to_participant": {"opening": "A fact."}, "question_coverage": coverage}
    context = {"internal_response_context": {"requested_questions": ["Question?"]}}
    result = RAGEngine._validate_question_coverage(response, context)
    assert result["incomplete_question_count"] == 1


def test_legacy_request_without_inventory_has_no_new_coverage_requirement():
    assert RAGEngine._validate_question_coverage({}, {}) == {}


def test_question_inventory_reaches_consumer_with_coverage_indices_intact():
    questions = ["What fees apply?", None, {"redacted_reference": "fixture"}, "How is it sent?"]
    metadata = RAGEngine._validate_question_coverage({}, {
        "internal_response_context": {"requested_questions": questions},
    })
    assert metadata["requested_questions"] == ["What fees apply?", "", "", "How is it sent?"]
    assert [entry["question_index"] for entry in metadata["question_coverage"]] == [0, 1, 2, 3]


def test_question_inventory_is_bounded_for_downstream_consumer():
    metadata = RAGEngine._validate_question_coverage({}, {
        "internal_response_context": {"requested_questions": ["Q" * 1500] * 20},
    })
    assert len(metadata["requested_questions"]) == 12
    assert all(len(question) == 1000 for question in metadata["requested_questions"])


@pytest.mark.parametrize("reason", ["general_knowledge", "account_not_found"])
def test_identity_metadata_preserves_only_identifier_types_not_values(reason):
    _, metadata = RAGEngine._apply_identity_knowledge_policy({}, {
        "identity_resolution_status": "not_found", "response_source_reason": reason,
        "provided_identifiers": ["name", "employer", "name", "synthetic@example.test", {"email": "synthetic@example.test"}, "last_four_ssn"],
    })
    assert metadata["provided_identifiers"] == ["name", "employer", "last_four_ssn"]


def test_internal_handoff_is_successful_processing_without_publishable_reply():
    outcome = InquiryOutcome("Where are the funds?", "balance", "generate_response", scrape_status="ok", generate_result=SimpleNamespace(
        decision="uncertain", confidence=0.8, source_articles=[], used_chunks=[], coverage_gaps=[],
        response={"outcome": "blocked_missing_data", "response_to_participant": {"opening": "Verification is needed."}, "escalation": {"needed": True, "reason": "Verify current custody"}},
        metadata={"human_review_required": True, "handoff": {"reason": "custody_verification", "facts": [{"field": "account_balance", "value": 0}]}}
    ))
    entry = _entry_from_outcome(0, outcome)
    assert entry["execution_status"] == "succeeded"
    assert entry["degraded"] is False
    assert entry["participant_reply_safe"] is False
    assert entry["human_review_required"] is True
    state, action = aggregate_states([entry], 0)
    assert state.value == "succeeded"
    assert action.value == "human_review"


def test_direct_endpoint_preserves_internal_handoff_without_marking_sent():
    from api.direct_ticket_evaluation import direct_success_entry
    entry = direct_success_entry(route="knowledge_question", inquiry="Which plan holds my funds?", topic="balance", response={
        "answer": "Account verification is needed.", "metadata": {"human_review_required": True,
                                                                     "identity_resolution_status": "ambiguous"},
    })
    assert entry["participant_reply_safe"] is False
    assert entry["execution_status"] == "succeeded"
    assert entry["human_review_required"] is True


def test_kq_identity_context_is_typed_and_survives_request():
    from api.models import KnowledgeQuestionRequest
    request = KnowledgeQuestionRequest(question="How can I withdraw my account?", identity_context={
        "identity_resolution_status": "not_found", "response_source_reason": "account_not_found",
        "provided_identifiers": ["name", "employer"],
    })
    assert request.identity_context.identity_resolution_status == "not_found"
    assert request.identity_context.identity_verified is False


@pytest.mark.parametrize("status,reason", [("not_found", "account_not_found"), ("ambiguous", "account_ambiguous"), ("access_error", "account_lookup_failed")])
def test_account_lookup_failures_do_not_become_personalized_kq_steps(status, reason):
    raw = {"answer": "Open Loans & Distributions and submit a withdrawal.", "key_points": ["Assume you are eligible"]}
    fixed, metadata = RAGEngine._apply_identity_knowledge_policy(raw, {
        "identity_resolution_status": status, "response_source_reason": reason,
        "provided_identifiers": ["name", "employer"], "identity_verified": False,
    })
    assert metadata["identity_resolution_status"] == status
    assert metadata["response_source_reason"] == reason
    assert metadata["human_review_required"] is True
    assert "submit" not in fixed["answer"]
    assert "name" not in fixed["answer"]  # do not repeat identifiers already provided
    assert "SSN" not in fixed["answer"]  # no new unsupported checklist


def test_educational_kq_does_not_require_account_identity():
    raw = {"answer": "A direct rollover moves funds between eligible retirement accounts.", "key_points": []}
    fixed, metadata = RAGEngine._apply_identity_knowledge_policy(raw, {
        "identity_resolution_status": "not_found", "response_source_reason": "general_knowledge",
    })
    assert fixed == raw
    assert metadata.get("human_review_required") is not True


def test_general_kq_without_lookup_has_explicit_purpose_for_downstream_formatter():
    raw = {"answer": "A general explanation.", "key_points": []}
    fixed, metadata = RAGEngine._apply_identity_knowledge_policy(raw, None)
    assert fixed == raw
    assert metadata["response_source_reason"] == "general_knowledge"
    assert "identity_resolution_status" not in metadata


def test_worker_kq_uses_identity_handoff_gate():
    outcome = InquiryOutcome("Where is my account?", "account_access", "knowledge_question", knowledge_result=SimpleNamespace(
        answer="Verification is needed.", key_points=[], source_articles=[], used_chunks=[], confidence_note="limited_coverage", coverage_gaps=[],
        metadata={"human_review_required": True, "identity_resolution_status": "ambiguous"},
    ))
    entry = _entry_from_outcome(0, outcome)
    assert entry["participant_reply_safe"] is False
    assert entry["human_review_required"] is True

@pytest.mark.asyncio
async def test_empty_kq_retrieval_preserves_identity_handoff():
    from unittest.mock import AsyncMock, Mock
    engine = RAGEngine.__new__(RAGEngine)
    engine._decompose_question = AsyncMock(return_value=['Where is my account?'])
    engine._cached_query = AsyncMock(return_value=[])
    engine.router = Mock()
    result = await engine.ask_knowledge_question('Where is my account?', identity_context={
        'identity_resolution_status':'ambiguous', 'response_source_reason':'account_ambiguous',
    })
    assert result.metadata['human_review_required'] is True
    assert result.metadata['identity_resolution_status'] == 'ambiguous'


@pytest.mark.parametrize('outcome,escalation,expected', [
    ('blocked_missing_data', False, True),
    ('ambiguous_plan_rules', False, True),
    ('can_proceed', True, True),
    ('blocked_not_eligible', True, True),
    ('blocked_not_eligible', False, False),
    ('can_proceed', False, False),
])
def test_final_business_outcome_controls_handoff_independently_of_retrieval(outcome, escalation, expected):
    from api.direct_ticket_evaluation import direct_success_entry
    response = {'outcome': outcome, 'response_to_participant': {'opening': 'A verified answer.'},
                'escalation': {'needed': escalation, 'reason': 'Review required' if escalation else None}}
    generated = SimpleNamespace(decision='can_proceed', confidence=0.9,
                                response=response, source_articles=[], used_chunks=[], coverage_gaps=[],
                                metadata={'human_review_required': False})
    worker = _entry_from_outcome(0, InquiryOutcome('Question?', 'loan', 'generate_response',
                                                 scrape_status='ok', generate_result=generated))
    direct = direct_success_entry(route='generate_response', inquiry='Question?', topic='loan', response=vars(generated))
    for entry in [worker, direct]:
        assert entry['participant_reply_safe'] is (not expected)
        assert entry.get('human_review_required', False) is expected
        state, action = aggregate_states([entry], 0)
        assert state.value == 'succeeded'
        if expected:
            assert action.value == 'human_review'
