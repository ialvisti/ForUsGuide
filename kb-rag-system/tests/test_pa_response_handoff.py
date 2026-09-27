"""A generated draft is not proof that every question is safe to publish."""

import json as _json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.ticket_worker import _entry_from_outcome, aggregate_states
from data_pipeline.rag_engine import RAGEngine
from data_pipeline.ticket_orchestrator import InquiryOutcome

_EXISTING_PLAN_NEXT_ACTION = (
    "Verify the dated plan lifecycle and participant disposition record before approving execution guidance."
)
_SUPPORT_PLAN_NEXT_ACTION = (
    "Route this case to Support to verify current plan servicing and the individual account record before providing withdrawal instructions."
)


def _terminated_plan_collected(*, hold=False):
    facts = []
    if hold:
        facts.append({
            "kind": "distribution_hold", "value": True, "source": "plan_history.notes",
            "recorded_at": "2026-03-26", "effective_on": "2026-03-26",
        })
    return {
        "participant_data": {"employment_status": "Active"},
        "internal_plan_context": {
            "current": {"status": "Terminated", "active": False, "status_as_of": "2024-10-24"},
            "operational_facts": facts,
        },
        "internal_preflight_context": {
            "schema_version": 1,
            "loans": {"status": "unknown", "outstanding_status": "unknown"},
            "sources": {
                "account_balance": {"status": "known", "value": 0},
                "vested_balance": {"status": "unknown", "value": None},
            },
            "crypto": {
                "enrollment": {"status": "unknown", "value": None},
                "holdings": {"status": "unknown", "value": None},
            },
        },
    }


async def _generate_plan_response(collected):
    from data_pipeline.llm_router import LLMResponse

    chunk = {"id": "distribution_policy", "score": 0.9, "metadata": {
        "article_id": "distribution_policy", "article_title": "Distribution policy",
        "chunk_type": "business_rules", "content": "Plan servicing may need review.",
    }}
    parsed = {
        "outcome": "can_proceed", "outcome_reason": "Eligible based on collected facts.",
        "response_to_participant": {
            "opening": "Here are the options.", "key_points": [], "steps": [{"action": "Submit the withdrawal"}], "warnings": [],
        },
        "questions_to_ask": [{"question": "Which provider?", "why": "routing"}],
        "escalation": {"needed": False, "reason": None},
        "guardrails_applied": [], "data_gaps": [], "coverage_gaps": [],
    }
    with patch("data_pipeline.rag_engine.PineconeUploader"):
        engine = RAGEngine(llm_router=Mock())
    engine._decompose_question = AsyncMock(return_value=["withdrawal request"])
    engine._search_for_exact_response_procedure = AsyncMock(return_value=([chunk], {}))
    engine._search_for_response_parallel_cascade = AsyncMock(return_value=([chunk], {}))
    engine._add_response_article_bundles = AsyncMock(return_value=([chunk], {"articles_added": []}))
    engine._build_context_with_diversity_and_tiers = Mock(return_value=("Plan rules.", [chunk], 3, {}))
    engine._call_llm = AsyncMock(return_value=LLMResponse(
        content=_json.dumps(parsed), usage={}, provider_used="openai", model_used="test",
    ))
    return await engine.generate_response(
        "Participant requests a withdrawal. The employer plan is shown as terminated.",
        "LT Trust",
        "401(k)",
        "termination_distribution_request",
        collected,
        5500,
    )


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


async def test_plain_terminated_plan_handoff_routes_to_support():
    collected = _terminated_plan_collected()
    result = await _generate_plan_response(collected)
    opening = result.response["response_to_participant"]["opening"]
    handoff = result.metadata["handoff"]
    preflight = handoff["preflight"]
    assert "plan is terminated" in opening.lower()
    assert "support" in opening.lower()
    assert result.response["response_to_participant"]["steps"] == []
    assert result.response["questions_to_ask"] == []
    assert result.metadata["human_review_required"] is True
    assert result.metadata["termination_response_policy"]["support_review_required"] is True
    assert result.metadata["termination_response_policy"]["plan_review_required"] is True
    assert handoff["reason"] == "plan_servicing_verification"
    assert handoff["next_action"] == _SUPPORT_PLAN_NEXT_ACTION
    assert preflight["loans"]["outstanding_status"] == "unknown"
    assert preflight["crypto"]["holdings"]["status"] == "unknown"
    assert preflight["sources"]["vested_balance"]["status"] == "unknown"
    assert collected["participant_data"]["employment_status"] == "Active"


async def test_non_support_plan_review_keeps_existing_handoff_next_action():
    result = await _generate_plan_response(_terminated_plan_collected(hold=True))
    handoff = result.metadata["handoff"]
    opening = result.response["response_to_participant"]["opening"].lower()
    assert "hold" in opening
    assert "plan is terminated" not in opening
    assert result.metadata["human_review_required"] is True
    assert result.metadata["termination_response_policy"].get("support_review_required") is not True
    assert result.metadata["termination_response_policy"]["plan_review_required"] is True
    assert handoff["reason"] == "plan_servicing_verification"
    assert handoff["next_action"] == _EXISTING_PLAN_NEXT_ACTION
