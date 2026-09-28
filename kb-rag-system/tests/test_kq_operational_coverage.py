"""Procedural KQ coverage: reserve operational chunks from articles already retrieved.

Network and the answer model are mocked. Routing, selection, and the retrieval
sanitizer run for real. The generated answer is not treated as proof.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from data_pipeline.llm_router import LLMResponse
from data_pipeline.pinecone_uploader import PineconeRetrievalError
from data_pipeline.prompts import build_knowledge_question_prompt
from data_pipeline.rag_engine import RAGEngine

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kq_operational_coverage.json").read_text()
)
STAGE1 = FIXTURE["stage1_used"]
LT_HITS = FIXTURE["lt_operational_hits"]
GENERAL_HITS = FIXTURE["general_operational_hits"]
LT_ARTICLE = "lt_request_401k_termination_withdrawal_or_rollover"
GENERAL_ARTICLE = "401k_savings_after_leaving_your_job_rollovers_cash_outs_roth_pre_tax"
SPLIT_ARTICLE = "can_i_split_my_401_k_rollover_between_multiple_providers"
MISSED_ARTICLE = "missed_60_day_rollover_window"
REQUIRED_IDS = {
    "lt_request_401k_termination_withdrawal_or_rollover_chunk_22",
    "lt_request_401k_termination_withdrawal_or_rollover_chunk_26",
    "lt_request_401k_termination_withdrawal_or_rollover_chunk_23",
}
INCOMING_ID = "lt_request_401k_termination_withdrawal_or_rollover_chunk_24"
LOAN_ID = "lt_request_401k_termination_withdrawal_or_rollover_chunk_23"
ORIGINAL = (
    "I have a 401k with ForUsAll from my previous employer and I'm looking to "
    "roll the account over to my new 401k provider, Go-retire (Charles Schwab "
    "Trust Bank). I have received the attached form from them but I'm not sure "
    "how this works exactly."
)


def _engine():
    with patch("data_pipeline.rag_engine.PineconeUploader"):
        return RAGEngine(llm_router=Mock())


def _answer():
    return LLMResponse(
        content=json.dumps({"answer": "synthetic", "key_points": [], "coverage_gaps": []}),
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        provider_used="mock",
        model_used="mock",
    )


def _install(engine, query_chunks):
    engine._decompose_question = AsyncMock(return_value=["401(k) rollover forms procedure"])
    engine._call_llm = AsyncMock(return_value=_answer())
    engine.pinecone.query_chunks = query_chunks


def _stage1_then(extra):
    calls = []

    def query_chunks(query_text, top_k=15, filter_dict=None, rerank=None):
        calls.append({"q": query_text, "k": top_k, "f": filter_dict})
        if not filter_dict:
            return [dict(chunk) for chunk in STAGE1]
        return list(extra(filter_dict, top_k))[:top_k]

    return calls, query_chunks


def _live_hits(filter_dict, top_k):
    article = filter_dict["article_id"]["$eq"]
    pool = {
        LT_ARTICLE: LT_HITS,
        GENERAL_ARTICLE: GENERAL_HITS,
    }.get(article, [])
    return [dict(chunk) for chunk in pool[:top_k]]


def _capture_cap(engine):
    seen = {}
    original = engine._build_context_with_diversity

    def wrapped(*args, **kwargs):
        seen["cap"] = kwargs["max_per_article"]
        return original(*args, **kwargs)

    engine._build_context_with_diversity = wrapped
    return seen


@pytest.mark.asyncio
async def test_live_top15_keeps_pre_submission_portal_steps_and_loan():
    calls, query_chunks = _stage1_then(_live_hits)
    engine = _engine()
    _install(engine, query_chunks)
    caps = _capture_cap(engine)
    result = await engine.ask_knowledge_question(ORIGINAL)
    ids = {chunk["chunk_id"] for chunk in result.used_chunks}
    text = " ".join(chunk["content"] for chunk in result.used_chunks).lower()
    assert REQUIRED_IDS <= ids
    assert "7 business days" in text
    assert "crypto" in text
    assert "outstanding 401(k) loan" in text
    assert "form" in text
    assert INCOMING_ID not in ids
    filtered = [call for call in calls if call["f"]]
    articles = {call["f"]["article_id"]["$eq"] for call in filtered}
    assert len(filtered) <= 2
    assert len(filtered) == 2
    assert articles == {GENERAL_ARTICLE, LT_ARTICLE}
    assert SPLIT_ARTICLE not in articles
    assert MISSED_ARTICLE not in articles
    assert all(call["k"] == 15 for call in filtered)
    assert all(call["k"] == engine.KQ_OPERATIONAL_TOP_K_PER_ARTICLE for call in filtered)
    assert caps["cap"] == engine.KQ_OPERATIONAL_MAX_CHUNKS_PER_ARTICLE == 15
    assert all(
        call["f"]["chunk_type"]["$in"] == ["business_rules", "eligibility", "steps"]
        for call in filtered
    )
    assert result.metadata["context_tokens"] <= 6000
    assert result.metadata["context_tokens"] > 4000
    per_article = {}
    for chunk in result.used_chunks:
        per_article[chunk["article_id"]] = per_article.get(chunk["article_id"], 0) + 1
    assert max(per_article.values()) <= 15


def test_fixture_provenance_is_the_sanitized_query_without_identity():
    note = FIXTURE["provenance"]
    assert note["query"] == "401(k) forms plan sponsor"
    assert note["top_k"] == 15
    assert "existing 401(k)" in note["note"]
    assert "form" in note["note"]
    blob = json.dumps(note).lower()
    assert "schwab" not in blob
    assert "go-retire" not in blob
    assert "@" not in blob


def test_ranked_top8_omits_the_loan_chunk():
    assert LOAN_ID not in {chunk["id"] for chunk in LT_HITS[:8]}
    assert LOAN_ID in {chunk["id"] for chunk in LT_HITS[:15]}
    assert len(LT_HITS) == 15
    assert len(GENERAL_HITS) == 10


def test_cap6_reservation_drops_the_loan_chunk_and_cap15_keeps_it():
    outgoing = [
        chunk for chunk in LT_HITS
        if chunk["metadata"].get("chunk_category") != "incoming_rollover_from_prior_employer"
    ]
    narrow = _engine()
    narrow.KQ_OPERATIONAL_MAX_CHUNKS_PER_ARTICLE = 6
    dropped = narrow._order_operational_reservation([dict(chunk) for chunk in outgoing], [LT_ARTICLE])
    assert LOAN_ID not in {chunk["id"] for chunk in dropped}
    assert len(dropped) == 6
    wide = _engine()
    kept = wide._order_operational_reservation([dict(chunk) for chunk in outgoing], [LT_ARTICLE])
    assert REQUIRED_IDS <= {chunk["id"] for chunk in kept}
    assert len(kept) > 6
    assert len(kept) <= 15


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    ORIGINAL,
    "How do I roll over my 401k account to a new provider? I am not sure how this works.",
    "I need the steps to roll the account over to another 401k.",
    "What form do I use to roll over this account to a new provider?",
])
async def test_procedural_rollover_paraphrases_request_operational_chunks(question):
    calls, query_chunks = _stage1_then(lambda _f, _k: [])
    engine = _engine()
    _install(engine, query_chunks)
    await engine.ask_knowledge_question(question)
    assert any(call["f"] for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "What is the difference between a direct and an indirect rollover?",
    "How are direct and indirect rollovers taxed?",
    "I want to roll my Fidelity 401k into my ForUsAll plan. I am not sure how this works.",
    "I might roll into another account. I am not sure how this works.",
    "Can I split my rollover between two providers? What are the steps?",
    "I missed the 60 day rollover deadline. What is the process?",
    "What is the force-out process for a small 401k balance?",
    "What is the process for a required minimum distribution at age 73?",
])
async def test_incoming_and_baseline_procedures_do_not_augment(question):
    calls, query_chunks = _stage1_then(lambda _f, _k: [dict(chunk) for chunk in LT_HITS])
    engine = _engine()
    _install(engine, query_chunks)
    await engine.ask_knowledge_question(question)
    assert not any(call["f"] for call in calls)


@pytest.mark.asyncio
async def test_other_provider_stage1_never_introduces_lt_article():
    other = {
        "id": "other_provider_rollover_chunk_1",
        "score": 0.7,
        "metadata": {
            "article_id": "other_provider_rollover_guide",
            "article_title": "Other provider rollover guide",
            "chunk_type": "faqs",
            "content": "Public frequently asked questions about requesting a rollover.",
        },
    }
    calls = []

    def query_chunks(query_text, top_k=15, filter_dict=None, rerank=None):
        calls.append(filter_dict)
        if not filter_dict:
            return [other]
        return []

    engine = _engine()
    _install(engine, query_chunks)
    await engine.ask_knowledge_question(
        "How do I roll over my 401k account to a new provider? I am not sure how this works."
    )
    filtered = [item for item in calls if item]
    assert filtered
    assert {item["article_id"]["$eq"] for item in filtered} == {"other_provider_rollover_guide"}
    assert LT_ARTICLE not in json.dumps(filtered)


@pytest.mark.asyncio
async def test_receiving_schwab_never_reaches_pinecone_or_becomes_source_recordkeeper():
    calls, query_chunks = _stage1_then(lambda _f, _k: [])
    engine = _engine()
    _install(engine, query_chunks)
    await engine.ask_knowledge_question(ORIGINAL)
    blob = json.dumps(calls).lower()
    assert "schwab" not in blob
    assert "go-retire" not in blob
    assert "lt trust" not in blob
    assert "record_keeper" not in blob


@pytest.mark.asyncio
async def test_wrong_article_or_type_is_ignored_and_incoming_category_is_omitted():
    incoming = next(chunk for chunk in LT_HITS if chunk["id"] == INCOMING_ID)
    promoted = json.loads(json.dumps(incoming))
    promoted["score"] = 0.99
    wrong_article = {
        "id": "wrong_article_chunk",
        "score": 0.98,
        "metadata": {
            "article_id": "unrelated_article",
            "article_title": "Unrelated",
            "chunk_type": "steps",
            "content": "Unrelated portal steps that must not be reserved.",
        },
    }
    wrong_type = {
        "id": "wrong_type_chunk",
        "score": 0.97,
        "metadata": {
            "article_id": LT_ARTICLE,
            "article_title": "LT",
            "chunk_type": "definitions",
            "content": "A definition returned by a bad filter must not be reserved.",
        },
    }

    def extra(filter_dict, _top_k):
        if filter_dict["article_id"]["$eq"] == LT_ARTICLE:
            return [promoted, wrong_article, wrong_type, *LT_HITS]
        return []

    calls, query_chunks = _stage1_then(extra)
    engine = _engine()
    _install(engine, query_chunks)
    result = await engine.ask_knowledge_question(ORIGINAL)
    ids = {chunk["chunk_id"] for chunk in result.used_chunks}
    assert REQUIRED_IDS <= ids
    assert INCOMING_ID not in ids
    assert "wrong_article_chunk" not in ids
    assert "wrong_type_chunk" not in ids


@pytest.mark.asyncio
async def test_filtered_retrieval_failure_keeps_taxonomy_without_fake_coverage():
    def query_chunks(query_text, top_k=15, filter_dict=None, rerank=None):
        if filter_dict:
            raise PineconeRetrievalError(
                index_name="synthetic",
                namespace="synthetic",
                top_k=top_k,
                filter_dict=filter_dict,
                rerank=None,
                cause=TimeoutError("upstream"),
            )
        return [dict(chunk) for chunk in STAGE1]

    engine = _engine()
    _install(engine, query_chunks)
    result = await engine.ask_knowledge_question(ORIGINAL)
    assert result.metadata["retrieval_failure_kind"] == "timeout"
    assert result.metadata["retrieval_retryable"] is True
    assert result.used_chunks == []
    assert "7 business days" not in result.answer


@pytest.mark.asyncio
async def test_empty_operational_companion_leaves_baseline_budget():
    calls, query_chunks = _stage1_then(lambda _f, _k: [])
    engine = _engine()
    _install(engine, query_chunks)
    caps = _capture_cap(engine)
    result = await engine.ask_knowledge_question(ORIGINAL)
    ids = {chunk["chunk_id"] for chunk in result.used_chunks}
    assert REQUIRED_IDS.isdisjoint(ids)
    assert result.metadata["context_tokens"] <= 4000
    assert caps["cap"] == engine.KQ_MAX_CHUNKS_PER_ARTICLE == 6
    user_prompt = engine._call_llm.await_args.kwargs["user_prompt"]
    assert "does not verify" not in user_prompt


@pytest.mark.asyncio
async def test_stage2_does_not_inflate_an_existing_chunk_score():
    """A non-operational stage-2 duplicate is ignored and never rescored.

    The FAQ chunk is not an operational type, so it can never be reserved, and
    the per-article cap may legitimately keep it out of the selected context.
    The property under test is the merge itself: the canonical copy keeps its
    stage-1 score whether or not selection later admits it.
    """
    duplicate = json.loads(json.dumps(STAGE1[3]))
    duplicate["score"] = 0.99

    def extra(filter_dict, _top_k):
        if filter_dict["article_id"]["$eq"] == LT_ARTICLE:
            return [duplicate, *LT_HITS]
        return []

    _calls, query_chunks = _stage1_then(extra)
    engine = _engine()
    _install(engine, query_chunks)
    merged, reserved = await engine._augment_kq_operational_chunks(
        [dict(chunk) for chunk in STAGE1], ORIGINAL,
    )
    canonical = next(chunk for chunk in merged if chunk["id"] == STAGE1[3]["id"])
    assert canonical["score"] == pytest.approx(STAGE1[3]["score"], abs=0.0001)
    assert STAGE1[3]["id"] not in {chunk["id"] for chunk in reserved}


def test_default_diversity_context_stays_byte_equivalent():
    engine = _engine()
    chunks = []
    for index, article_id in enumerate(("alpha_article", "beta_article")):
        chunks.append({
            "id": f"{article_id}_chunk_{index}",
            "score": 0.9 - index / 10,
            "metadata": {
                "article_id": article_id,
                "article_title": article_id,
                "chunk_type": "definitions",
                "content": f"Public definition {index} for {article_id}.",
            },
        })
    context, selected, tokens = engine._build_context_with_diversity(
        chunks,
        budget=4000,
        prioritize_types=["business_rules", "eligibility", "steps"],
        max_per_article=6,
    )
    assert tokens == 16
    assert [chunk["id"] for chunk in selected] == [
        "alpha_article_chunk_0",
        "beta_article_chunk_1",
    ]
    assert context == (
        "--- Section 1 (definitions | Source: alpha_article) ---\n"
        "Public definition 0 for alpha_article.\n\n"
        "--- Section 2 (definitions | Source: beta_article) ---\n"
        "Public definition 1 for beta_article.\n"
    )


def test_unchanged_knowledge_prompt_stays_identical_and_note_is_conditional():
    plain, noted = (
        build_knowledge_question_prompt("CTX", "Q"),
        build_knowledge_question_prompt("CTX", "Q", context_note=""),
    )
    assert plain == noted
    assert plain[1] == (
        "KNOWLEDGE BASE CONTEXT:\nCTX\n\nQUESTION:\nQ\n\n"
        "Answer the question using ONLY the knowledge base context above. "
        "Provide a thorough, educational response.\n\n"
        "Return ONLY the JSON object, no additional text."
    )
    _system, user = build_knowledge_question_prompt(
        "CTX",
        "Q",
        context_note=(
            "These retrieved procedures are conditional knowledge. "
            "An article title does not verify the current recordkeeper."
        ),
    )
    assert "conditional knowledge" in user
    assert "7 business days" not in user
    assert "CTX" in user and "QUESTION:\nQ" in user


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "How do 401k rollovers work?",
    "How does the 401(k) rollover process work?",
    "What are the steps in a 401k rollover?",
])
async def test_generic_process_education_stays_on_the_baseline_path(question):
    """A bare 401(k) token must not make education look like a live request."""
    calls, query_chunks = _stage1_then(lambda _f, _k: [dict(chunk) for chunk in LT_HITS])
    engine = _engine()
    _install(engine, query_chunks)
    result = await engine.ask_knowledge_question(question)
    assert not any(call["f"] for call in calls)
    assert result.metadata["context_tokens"] <= 4000
    user_prompt = engine._call_llm.await_args.kwargs["user_prompt"]
    assert "does not verify" not in user_prompt
    assert "moving an existing account" not in user_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "How do I roll my 401k over from Fidelity into ForUsAll?",
    "How do I roll my 401k over to ForUsAll?",
    "I need the steps to roll my old 401k over into this plan.",
    "What form do I use to roll my account over to my current plan?",
])
async def test_incoming_destination_variants_do_not_augment(question):
    """Naming this plan as the destination is incoming, whatever the source."""
    calls, query_chunks = _stage1_then(lambda _f, _k: [dict(chunk) for chunk in LT_HITS])
    engine = _engine()
    _install(engine, query_chunks)
    await engine.ask_knowledge_question(question)
    assert not any(call["f"] for call in calls)


@pytest.mark.asyncio
async def test_duplicate_operational_chunk_reserves_the_canonical_merged_copy():
    """Stage 1 already holds an operational chunk, scored below the LT FAQ.

    0.49 sits under the FAQ best of 0.5457, so the article still qualifies as a
    candidate. The stage-2 copy of that same id scores 0.99. The reserved
    object must stay the canonical merged copy. The operational cap is not
    loosened and the score is not boosted.
    """
    base_operational = json.loads(json.dumps(LT_HITS[0]))
    base_operational["score"] = 0.49
    stage2_score = 0.99
    stage1 = [dict(chunk) for chunk in STAGE1] + [base_operational]
    calls = []

    def query_chunks(query_text, top_k=15, filter_dict=None, rerank=None):
        calls.append({"q": query_text, "k": top_k, "f": filter_dict})
        if not filter_dict:
            return [dict(chunk) for chunk in stage1]
        if filter_dict["article_id"]["$eq"] == LT_ARTICLE:
            hits = [dict(chunk) for chunk in LT_HITS]
            for hit in hits:
                if hit["id"] == base_operational["id"]:
                    hit["score"] = stage2_score
            return hits[:top_k]
        return []

    engine = _engine()
    _install(engine, query_chunks)
    result = await engine.ask_knowledge_question(ORIGINAL)
    articles = {call["f"]["article_id"]["$eq"] for call in calls if call["f"]}
    assert LT_ARTICLE in articles
    kept = next(
        chunk for chunk in result.used_chunks
        if chunk["chunk_id"] == base_operational["id"]
    )
    assert kept["score"] == pytest.approx(0.49, abs=0.0001)
    assert kept["score"] < stage2_score


@pytest.mark.asyncio
async def test_augmented_article_uses_operational_cap_not_baseline_six():
    """Fourteen operational hits leave one cap slot; the FAQ may use it.

    The baseline cap of 6 still applies when nothing is reserved. An augmented
    article may exceed 6 and must stay within the operational cap of 15.
    """
    calls, query_chunks = _stage1_then(_live_hits)
    engine = _engine()
    _install(engine, query_chunks)
    result = await engine.ask_knowledge_question(ORIGINAL)
    lt_ids = [
        chunk["chunk_id"] for chunk in result.used_chunks
        if chunk["article_id"] == LT_ARTICLE
    ]
    operational = [chunk_id for chunk_id in lt_ids if chunk_id != STAGE1[3]["id"]]
    assert len(operational) > engine.KQ_MAX_CHUNKS_PER_ARTICLE
    assert len(lt_ids) <= engine.KQ_OPERATIONAL_MAX_CHUNKS_PER_ARTICLE
    assert engine.KQ_MAX_CHUNKS_PER_ARTICLE == 6
    filtered = [call for call in calls if call["f"]]
    assert filtered
    assert all(call["k"] == 15 for call in filtered)


@pytest.mark.asyncio
async def test_identity_fallback_still_replaces_the_answer():
    calls, query_chunks = _stage1_then(lambda _f, _k: [])
    engine = _engine()
    _install(engine, query_chunks)
    result = await engine.ask_knowledge_question(
        ORIGINAL,
        identity_context={
            "identity_resolution_status": "not_found",
            "response_source_reason": "account_not_found",
            "identity_verified": False,
        },
    )
    assert result.answer.startswith("Our team needs to verify the account")
    assert result.metadata["human_review_required"] is True
    assert "synthetic" not in result.answer
