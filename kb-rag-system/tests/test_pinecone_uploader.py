"""
Unit tests for Pinecone uploader retrieval behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import data_pipeline.pinecone_uploader as pinecone_uploader

PineconeUploader = pinecone_uploader.PineconeUploader


def _uploader_with_index(index: Mock) -> PineconeUploader:
    with patch("data_pipeline.pinecone_uploader.Pinecone"):
        uploader = PineconeUploader.__new__(PineconeUploader)
        uploader.api_key = "test"
        uploader.index_name = "test-index"
        uploader.namespace = "test-namespace"
        uploader.index = index
        # resiliencia de query (Tarea 8): el helper bypasea __init__, así que
        # se cablean los atributos que query_chunks usa. Backoff a 0 para no
        # dormir en los tests.
        uploader._query_breaker = pinecone_uploader._CircuitBreaker(
            threshold=3, cooldown_s=30.0)
        uploader._query_max_attempts = 3
        uploader._query_backoff_base_s = 0.0
        uploader._query_backoff_cap_s = 0.0
        return uploader


class _HTTPErr(RuntimeError):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


def test_query_retries_only_on_429_and_5xx():
    """Guía PINECONE.md: sólo 429/5xx son transitorios; un 4xx NO se reintenta."""
    index = Mock()
    # 500 dos veces, luego éxito → 3 llamadas
    index.search.side_effect = [
        _HTTPErr(503), _HTTPErr(503),
        {"result": {"hits": [{"_id": "c1", "_score": 0.9, "fields": {}}]}},
    ]
    uploader = _uploader_with_index(index)
    chunks = uploader.query_chunks("neutral text", top_k=1)
    assert index.search.call_count == 3
    assert chunks and chunks[0]["id"] == "c1"


def test_query_does_not_retry_4xx():
    index = Mock()
    index.search.side_effect = _HTTPErr(400)
    uploader = _uploader_with_index(index)
    with pytest.raises(pinecone_uploader.PineconeRetrievalError):
        uploader.query_chunks("neutral text", top_k=1)
    assert index.search.call_count == 1, "un 4xx no debe reintentarse"


def test_circuit_opens_after_consecutive_failures():
    index = Mock()
    index.search.side_effect = _HTTPErr(503)
    uploader = _uploader_with_index(index)
    # threshold=3, max_attempts=3 → una llamada agota los 3 intentos y abre
    with pytest.raises(pinecone_uploader.PineconeRetrievalError):
        uploader.query_chunks("neutral text", top_k=1)
    calls_after_first = index.search.call_count
    # circuito abierto: la siguiente llamada falla rápido sin tocar el índice
    with pytest.raises(pinecone_uploader.PineconeCircuitOpen):
        uploader.query_chunks("neutral text", top_k=1)
    assert index.search.call_count == calls_after_first, (
        "con el circuito abierto no debe llamarse a Pinecone"
    )


def test_verify_readonly_uses_stats_and_neutral_query():
    index = Mock()
    index.describe_index_stats.return_value = SimpleNamespace(total_vector_count=42)
    index.search.return_value = {"result": {"hits": []}}
    uploader = _uploader_with_index(index)
    out = uploader.verify_readonly()
    assert out["total_vectors"] == 42
    assert out["namespace"] == "test-namespace"
    index.describe_index_stats.assert_called_once()
    # verificación read-only: jamás upsert/delete
    index.upsert.assert_not_called()
    index.delete.assert_not_called()


def test_query_sanitizes_participant_and_financial_values_before_pinecone():
    index = Mock()
    index.search.return_value = {"result": {"hits": []}}
    uploader = _uploader_with_index(index)

    uploader.query_chunks(
        "Participant Jane Doe, participant ID 158948, has balance $40,000 "
        "and email jane.doe@example.com",
        top_k=1,
    )

    kwargs = index.search.call_args.kwargs
    outbound = (kwargs.get("inputs") or kwargs["query"]["inputs"])["text"]
    assert "Jane Doe" not in outbound
    assert "158948" not in outbound
    assert "$40,000" not in outbound
    assert "jane.doe@example.com" not in outbound
    assert "balance" in outbound.lower()


def test_query_chunks_raises_typed_error_with_safe_context_on_pinecone_exception():
    index = Mock()
    index.search.side_effect = RuntimeError("pinecone boom")
    uploader = _uploader_with_index(index)
    retrieval_error = getattr(pinecone_uploader, "PineconeRetrievalError", None)

    assert retrieval_error is not None
    with pytest.raises(retrieval_error) as exc_info:
        uploader.query_chunks("How do I rollover my 401k?", top_k=5)

    message = str(exc_info.value)
    assert "test-index" in message
    assert "test-namespace" in message
    assert "top_k=5" in message
    assert "How do I rollover" not in message
    assert exc_info.value.index_name == "test-index"
    assert exc_info.value.namespace == "test-namespace"
    assert exc_info.value.top_k == 5
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_query_chunks_parses_integrated_embedding_search_hits():
    index = Mock()
    index.search.return_value = SimpleNamespace(
        to_dict=lambda: {
            "result": {
                "hits": [
                    {
                        "_id": "chunk-1",
                        "_score": 0.75,
                        "fields": {"article_id": "article-1", "content": "body"},
                    }
                ]
            }
        }
    )
    uploader = _uploader_with_index(index)

    chunks = uploader.query_chunks("How do I rollover my 401k?", top_k=5)

    assert chunks == [
        {
            "id": "chunk-1",
            "score": 0.75,
            "metadata": {"article_id": "article-1", "content": "body"},
        }
    ]


def test_query_chunks_supports_pinecone_sdk_9_search_signature():
    calls = []

    class SDK9Index:
        def search(
            self,
            *,
            namespace,
            top_k,
            inputs,
            filter=None,
            fields=None,
            rerank=None,
        ):
            calls.append(
                {
                    "namespace": namespace,
                    "top_k": top_k,
                    "inputs": inputs,
                    "filter": filter,
                    "fields": fields,
                    "rerank": rerank,
                }
            )
            return SimpleNamespace(
                to_dict=lambda: {
                    "result": {
                        "hits": [
                            {
                                "id_": "chunk-9",
                                "score_": 0.82,
                                "fields": {"article_id": "article-9"},
                            }
                        ]
                    }
                }
            )

    uploader = _uploader_with_index(SDK9Index())

    chunks = uploader.query_chunks(
        "How do I rollover my 401k?",
        top_k=7,
        filter_dict={"plan_type": {"$eq": "401(k)"}},
        rerank={"model": "bge-reranker-v2-m3"},
    )

    assert calls == [
        {
            "namespace": "test-namespace",
            "top_k": 7,
            "inputs": {"text": "How do I rollover my 401k?"},
            "filter": {"plan_type": {"$eq": "401(k)"}},
            "fields": ["*"],
            "rerank": {"model": "bge-reranker-v2-m3"},
        }
    ]
    assert chunks[0] == {
        "id": "chunk-9",
        "score": 0.82,
        "metadata": {"article_id": "article-9"},
    }
