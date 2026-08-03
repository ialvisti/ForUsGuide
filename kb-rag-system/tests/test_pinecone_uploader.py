"""
Unit tests for Pinecone uploader retrieval behavior.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api import metrics as ticket_metrics
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
    chunks = uploader.query_chunks("retirement plan guidance", top_k=1)
    assert index.search.call_count == 3
    assert chunks and chunks[0]["id"] == "c1"


def test_query_does_not_retry_4xx():
    index = Mock()
    index.search.side_effect = _HTTPErr(400)
    uploader = _uploader_with_index(index)
    with pytest.raises(pinecone_uploader.PineconeRetrievalError) as exc_info:
        uploader.query_chunks("retirement plan guidance", top_k=1)
    assert index.search.call_count == 1, "un 4xx no debe reintentarse"
    assert exc_info.value.retryable is False
    assert exc_info.value.failure_kind == "client_error"


@pytest.mark.parametrize(
    ("failure", "expected_calls"),
    [
        (_HTTPErr(400), 1),
        (_HTTPErr(408), 1),
        (ValueError("invalid record"), 1),
        (_HTTPErr(503), 3),
        (TimeoutError("transport timeout"), 3),
    ],
)
def test_upload_retries_only_closed_transient_taxonomy(
    failure, expected_calls,
):
    index = Mock()
    index.upsert_records.side_effect = failure
    uploader = _uploader_with_index(index)
    uploader.max_retries = 3
    uploader.retry_delay = 0

    result = uploader._upload_batch([
        {"id": "c1", "content": "retirement", "metadata": {}},
    ])

    assert result is False
    assert index.upsert_records.call_count == expected_calls


@pytest.mark.parametrize(
    ("status_code", "failure_kind"),
    [(429, "rate_limit"), (503, "server_error")],
)
def test_transient_retrieval_error_exposes_closed_retry_taxonomy(
        status_code, failure_kind):
    index = Mock()
    index.search.side_effect = _HTTPErr(status_code)
    uploader = _uploader_with_index(index)

    with pytest.raises(pinecone_uploader.PineconeRetrievalError) as exc_info:
        uploader.query_chunks("retirement plan guidance", top_k=1)

    assert exc_info.value.retryable is True
    assert exc_info.value.failure_kind == failure_kind


@pytest.mark.parametrize(
    ("failure", "failure_kind", "retryable", "expected_calls"),
    [
        (TimeoutError("synthetic timeout"), "timeout", True, 3),
        (ConnectionError("synthetic connection"), "transport", True, 3),
        (ValueError("synthetic invalid request"), "unknown", False, 1),
    ],
)
def test_statusless_failures_use_concrete_types_not_name_heuristics(
        failure, failure_kind, retryable, expected_calls):
    index = Mock()
    index.search.side_effect = failure
    uploader = _uploader_with_index(index)

    with pytest.raises(pinecone_uploader.PineconeRetrievalError) as exc_info:
        uploader.query_chunks("retirement plan guidance", top_k=1)

    assert exc_info.value.failure_kind == failure_kind
    assert exc_info.value.retryable is retryable
    assert index.search.call_count == expected_calls


def test_sdk_connection_failure_is_retryable_transport_error():
    """The pinned SDK wraps network failures in its own non-OSError type."""
    from pinecone import PineconeConnectionError

    index = Mock()
    index.search.side_effect = PineconeConnectionError(
        "synthetic connection failure"
    )
    uploader = _uploader_with_index(index)

    with pytest.raises(pinecone_uploader.PineconeRetrievalError) as exc_info:
        uploader.query_chunks("retirement plan guidance", top_k=1)

    assert exc_info.value.failure_kind == "transport"
    assert exc_info.value.retryable is True
    assert index.search.call_count == 3


@pytest.mark.parametrize(
    ("status_code", "expected_requests"),
    [(408, 1), (503, 3)],
)
def test_query_has_one_retry_authority_over_real_sdk_http_transport(
    status_code, expected_requests
):
    """The pinned SDK must not multiply the uploader's three attempts.

    Pinecone 9.1.0 performs three retries of its own by default (four HTTP
    requests per ``Index.search`` call).  The uploader owns the reviewed
    policy, so one logical query must produce at most three HTTP requests.
    """
    from pinecone import RetryConfig
    from pinecone.index import Index

    class AlwaysUnavailable(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self):  # noqa: N802 - stdlib handler API
            type(self).requests += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = b'{"message":"synthetic unavailable"}'
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    assert RetryConfig().max_retries == 3, (
        "the pinned SDK retry default changed; re-audit the single authority"
    )
    assert 408 in RetryConfig().retryable_status_codes
    server = ThreadingHTTPServer(("127.0.0.1", 0), AlwaysUnavailable)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    sdk_index = Index(
        host=f"http://127.0.0.1:{server.server_port}",
        api_key="synthetic-api-key",  # pragma: allowlist secret
        timeout=1.0,
    )
    index = SimpleNamespace(
        host=sdk_index.host,
        search=sdk_index.search,
        describe_index_stats=lambda: SimpleNamespace(total_vector_count=0),
    )
    pinecone_client = Mock()
    pinecone_client.Index.return_value = index

    try:
        with patch(
            "data_pipeline.pinecone_uploader.Pinecone",
            return_value=pinecone_client,
        ), patch("time.sleep", return_value=None):
            uploader = PineconeUploader(
                api_key="synthetic-api-key",  # pragma: allowlist secret
                index_name="synthetic-index",
            )
            with pytest.raises(pinecone_uploader.PineconeRetrievalError):
                uploader.query_chunks("retirement plan guidance", top_k=1)

        assert AlwaysUnavailable.requests == expected_requests
    finally:
        close = getattr(locals().get("uploader"), "close", None)
        if callable(close):
            close()
        sdk_index.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_retrieval_error_does_not_retain_filter_or_rerank_values():
    private_sentinel = "participant-private-sentinel"
    index = Mock()
    index.search.side_effect = _HTTPErr(400)
    uploader = _uploader_with_index(index)

    with pytest.raises(pinecone_uploader.PineconeRetrievalError) as exc_info:
        uploader.query_chunks(
            "retirement plan guidance",
            top_k=1,
            filter_dict={"participant": private_sentinel},
            rerank={"model": private_sentinel},
        )

    serialized = repr(vars(exc_info.value)) + repr(exc_info.value)
    assert private_sentinel not in serialized
    assert "filter_dict" not in vars(exc_info.value)
    assert "rerank" not in vars(exc_info.value)


def test_close_releases_all_pinecone_clients_once():
    uploader = PineconeUploader.__new__(PineconeUploader)
    uploader._query_search = Mock()
    uploader.index = Mock()
    uploader.pc = Mock()

    uploader.close()
    uploader.close()

    uploader._query_search.close.assert_called_once_with()
    uploader.index.close.assert_called_once_with()
    uploader.pc.close.assert_called_once_with()


def test_close_continues_after_failure_without_logging_exception_details(caplog):
    private_sentinel = "participant-private-close-sentinel"
    uploader = PineconeUploader.__new__(PineconeUploader)
    uploader._query_search = Mock()
    uploader._query_search.close.side_effect = RuntimeError(private_sentinel)
    uploader.index = Mock()
    uploader.pc = Mock()

    with caplog.at_level(logging.ERROR, logger=pinecone_uploader.__name__):
        uploader.close()

    uploader.index.close.assert_called_once_with()
    uploader.pc.close.assert_called_once_with()
    assert private_sentinel not in caplog.text
    assert "RuntimeError" in caplog.text


def test_circuit_opens_after_consecutive_failures():
    index = Mock()
    index.search.side_effect = _HTTPErr(503)
    uploader = _uploader_with_index(index)
    # threshold=3, max_attempts=3 → una llamada agota los 3 intentos y abre
    with pytest.raises(pinecone_uploader.PineconeRetrievalError):
        uploader.query_chunks("retirement plan guidance", top_k=1)
    calls_after_first = index.search.call_count
    # circuito abierto: la siguiente llamada falla rápido sin tocar el índice
    with pytest.raises(pinecone_uploader.PineconeCircuitOpen):
        uploader.query_chunks("retirement plan guidance", top_k=1)
    assert index.search.call_count == calls_after_first, (
        "con el circuito abierto no debe llamarse a Pinecone"
    )


def test_inflight_query_does_not_retry_after_another_query_opens_circuit():
    entered = threading.Event()
    release = threading.Event()

    class CoordinatedIndex:
        def __init__(self):
            self.calls_by_query = {}
            self.lock = threading.Lock()

        def search(self, **kwargs):
            query = kwargs.get("query") or kwargs
            query_text = query["inputs"]["text"]
            with self.lock:
                query_calls = self.calls_by_query.get(query_text, 0) + 1
                self.calls_by_query[query_text] = query_calls
            if query_text == "retirement plan" and query_calls == 1:
                entered.set()
                assert release.wait(timeout=2)
            raise _HTTPErr(503)

    index = CoordinatedIndex()
    uploader = _uploader_with_index(index)
    uploader._query_breaker = pinecone_uploader._CircuitBreaker(
        threshold=1, cooldown_s=30.0
    )

    with patch("data_pipeline.pinecone_uploader.time.sleep"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            inflight = pool.submit(
                uploader.query_chunks, "retirement plan overview", 1
            )
            assert entered.wait(timeout=2)
            try:
                with pytest.raises(pinecone_uploader.PineconeRetrievalError):
                    uploader.query_chunks("401(k) rollover", top_k=1)
            finally:
                release.set()
            with pytest.raises(pinecone_uploader.PineconeRetrievalError):
                inflight.result(timeout=2)

    assert index.calls_by_query["retirement plan"] == 1


def test_half_open_allows_exactly_one_concurrent_probe(caplog):
    entered = threading.Event()
    release = threading.Event()

    class BlockingIndex:
        calls = 0

        def search(self, **_kwargs):
            self.calls += 1
            entered.set()
            assert release.wait(timeout=2)
            return {"result": {"hits": []}}

    index = BlockingIndex()
    uploader = _uploader_with_index(index)
    uploader._query_breaker.record_failure()
    uploader._query_breaker.record_failure()
    uploader._query_breaker.record_failure()
    uploader._query_breaker._opened_at = 0.0

    def scoped_query():
        # A manually-created ThreadPoolExecutor does not copy ContextVars.
        # Production uses asyncio.to_thread (which does); scope explicitly in
        # this lower-level concurrency test.
        with ticket_metrics.ticket_execution_scope():
            return uploader.query_chunks("retirement plan guidance", 1)

    with caplog.at_level(logging.INFO, logger="ticket_metrics"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            probe = pool.submit(scoped_query)
            assert entered.wait(timeout=2)
            rejected = pool.submit(scoped_query)
            with pytest.raises(pinecone_uploader.PineconeCircuitOpen):
                rejected.result(timeout=2)
            release.set()
            probe.result(timeout=2)

    assert index.calls == 1
    assert '"state":"half_open"' in caplog.text
    assert '"state":"closed"' in caplog.text


def test_retry_and_open_transitions_emit_closed_schema_metrics(caplog):
    index = Mock()
    index.search.side_effect = _HTTPErr(429)
    uploader = _uploader_with_index(index)

    with ticket_metrics.ticket_execution_scope():
        with caplog.at_level(logging.INFO, logger="ticket_metrics"):
            with pytest.raises(pinecone_uploader.PineconeRetrievalError):
                uploader.query_chunks("retirement plan guidance", top_k=1)

    assert caplog.text.count('"metric":"ticket_pinecone_retry_count"') == 2
    assert '"reason":"rate_limit"' in caplog.text
    assert '"metric":"ticket_pinecone_circuit_count"' in caplog.text
    assert '"state":"open"' in caplog.text


def test_core_query_does_not_emit_ticket_metrics(caplog):
    index = Mock()
    index.search.side_effect = _HTTPErr(429)
    uploader = _uploader_with_index(index)

    with caplog.at_level(logging.INFO, logger="ticket_metrics"):
        with pytest.raises(pinecone_uploader.PineconeRetrievalError):
            uploader.query_chunks("retirement plan guidance", top_k=1)

    assert "ticket_pinecone_retry_count" not in caplog.text
    assert "ticket_pinecone_circuit_count" not in caplog.text


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
    # Raw SDK exceptions can contain request bodies/URLs; the public typed
    # boundary retains only the closed cause type, never the exception chain.
    assert exc_info.value.__cause__ is None
    assert exc_info.value.cause_type == "RuntimeError"


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
            "inputs": {"text": "401(k) rollover"},
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
