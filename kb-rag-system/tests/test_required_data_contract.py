"""Required-data contracts from actual approved chunk structure; no live calls."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from data_pipeline.chunking import KBChunker
from data_pipeline.llm_router import LLMEmptyResponseError, LLMResponse
from data_pipeline.rag_engine import RAGEngine


def article_chunks():
    article_path = next((Path(__file__).parents[2] / "PA").rglob("*Hardship*.json"))
    article = json.loads(article_path.read_text())
    chunker = KBChunker.__new__(KBChunker)
    fields = article["details"]["required_data"]
    return [
        {
            "id": tier,
            "score": 0.8,
            "metadata": {
                "article_id": article["metadata"]["article_id"],
                "article_title": "Hardship",
                "topic": "hardship",
                "chunk_type": f"required_data_{tier}",
                "content": getattr(chunker, f"_format_required_data_{tier}")(fields[tier]),
            },
        }
        for tier in ("must_have", "nice_to_have")
    ]


def make_engine(chunks, response="not valid json"):
    router = Mock()
    router.call = AsyncMock(return_value=LLMResponse(response, None, "openai", "gpt-5.5"))
    with patch("data_pipeline.rag_engine.PineconeUploader"):
        engine = RAGEngine(llm_router=router)
    engine._decompose_question = AsyncMock(return_value=["hardship"])
    engine._search_for_required_data = AsyncMock(return_value=(chunks, {}))
    engine._augment_context_with_nice_to_have = AsyncMock(
        side_effect=lambda **kw: (kw["context"], kw["selected_chunks"], kw["tokens_used"])
    )
    return engine


@pytest.mark.asyncio
async def test_actual_hardship_sections_produce_fields_without_llm_extraction():
    engine = make_engine(article_chunks())
    result = await engine.get_required_data("Emergency hardship guidance", "LT Trust", "401(k)", "hardship")
    assert result.metadata.get("error") is None
    fields = {field["field"]: field for field in result.required_fields["participant_data"]}
    assert fields["birth_date"]["required"] is True
    assert fields["birth_date"]["data_type"] == "date"
    assert "hardship_reason" not in fields
    assert result.metadata["extraction_method"] == "validated_chunk_contract"
    sources = result.metadata["conversation_fields"]
    assert any(field["field"] == "hardship_reason" and field["source"] == "message_text" for field in sources)
    assert any(field["source"] == "agent_input" for field in sources)
    assert engine.router.call.await_count == 0


def test_parser_keeps_must_tier_and_deduplicates_by_entity():
    chunks = article_chunks()
    chunks[1]["metadata"]["content"] += (
        "\n### Birth Date\n**Description:** Date of birth.\n"
        "**Why needed:** Context.\n**Source:** participant_profile\n"
    )
    fields, diagnostics = RAGEngine._required_data_from_chunks(chunks)
    birth_dates = [f for f in fields["participant_data"] if f["field"] == "birth_date"]
    assert len(birth_dates) == 1
    assert birth_dates[0]["required"] is True
    assert diagnostics["contract_status"] == "valid"


@pytest.mark.parametrize("body", [
    "### Birth Date\n**Description:** Date.\n**Source:** participant_profile\n",
    "### Birth Date\n**Description:** Date.\n**Why needed:** Context.\n**Source:** secret_store\n",
    "",
])
def test_incomplete_or_unknown_source_contract_cannot_succeed(body):
    chunks = article_chunks()[:1]
    chunks[0]["metadata"]["content"] = "# Required Data — Must Have (Portal/Profile Data)\n" + body
    fields, diagnostics = RAGEngine._required_data_from_chunks(chunks)
    assert fields is None
    assert diagnostics["contract_status"] != "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "failure"), [
    ("not valid json", "invalid_json"),
    ("[]", "schema_rejected"),
    (json.dumps({"participant_data": [], "plan_data": [], "coverage_gaps": []}), "empty_extraction"),
    (json.dumps({"participant_data": [], "plan_data": [], "coverage_gaps": ["missing"]}), "empty_extraction"),
])
async def test_unrecoverable_extraction_preserves_failure_reason_and_closed_contract(response, failure):
    chunks = article_chunks()[:1]
    chunks[0]["metadata"]["content"] = "Legacy incomplete required fields"
    engine = make_engine(chunks, response)
    result = await engine.get_required_data("Emergency hardship guidance", "LT Trust", "401(k)", "hardship")
    assert result.metadata["error"] == "required_data_failed"
    assert result.metadata["required_data_failure_kind"] == failure
    assert result.required_fields == {"participant_data": [], "plan_data": []}
    assert len(result.used_chunks) == 1


@pytest.mark.asyncio
async def test_empty_provider_preserves_safe_finish_reason_without_raw_diagnostics():
    chunks = article_chunks()[:1]
    chunks[0]["metadata"]["content"] = "Legacy incomplete required fields"
    engine = make_engine(chunks)
    engine.router.call.side_effect = LLMEmptyResponseError("length", {"completion_tokens": 800})
    result = await engine.get_required_data("Emergency hardship guidance", "LT Trust", "401(k)", "hardship")
    assert result.metadata["required_data_failure_kind"] == "empty_extraction"
    assert result.metadata["extraction_attempts"][-1]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_higher_scoring_rules_cannot_evict_the_required_field_contract():
    # Reproduces live retrieval ordering: one must-have below six rule chunks.
    must = article_chunks()[0]
    must['score'] = 0.31
    rules = [{
        'id': f'rule-{i}', 'score': 0.48 - i * 0.01,
        'metadata': dict(must['metadata'], chunk_type='business_rules',
                         content='The request is limited to the verified expense.'),
    } for i in range(6)]
    engine = make_engine(rules + [must])
    result = await engine.get_required_data('Medical hardship', 'LT Trust', '401(k)', 'hardship_withdrawal')
    assert result.metadata.get('error') is None
    assert any(field['field'] == 'birth_date' for field in result.required_fields['participant_data'])
    assert engine.router.call.await_count == 0


@pytest.mark.asyncio
async def test_nice_fields_use_top_level_chunk_ids_and_are_added_only_once():
    must, nice = article_chunks()
    engine = make_engine([must])
    engine._cached_query = AsyncMock(return_value=[nice, nice])
    context, selected, tokens = await RAGEngine._augment_context_with_nice_to_have(
        engine, inquiry='Medical hardship', primary_article_id=None,
        context='Must fields', selected_chunks=[must], tokens_used=10,
    )
    assert [c['id'] for c in selected] == [must['id'], nice['id']]
    assert tokens > 10
    assert 'Hardship reason' in context
