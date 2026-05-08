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
        return uploader


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
