"""
Unit tests for the ForusBots async client.

The HTTP layer is fully mocked: a fake client replays a scripted list of
``httpx.Response`` objects (or raises scripted exceptions) per call. ``asyncio.sleep``
is neutralised so the poll loop runs instantly, and ``time.monotonic`` is faked only
in the timeout test so the deadline can be crossed deterministically.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import httpx
import pytest

import data_pipeline.forusbots_client as fb
from data_pipeline.forusbots_client import (
    ForusBotsClient,
    ForusBotsCircuitOpen,
    ForusBotsError,
    ForusBotsJobFailed,
    ForusBotsTimeout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resp(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body)


class FakeHTTPClient:
    """Replays a scripted sequence of responses / exceptions for ``request``."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []  # list of (method, url, json)

    async def request(self, method, url, headers=None, json=None):
        self.calls.append((method, url, json))
        if not self._script:
            raise AssertionError(f"unexpected extra HTTP call: {method} {url}")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass

    def count(self, method: str) -> int:
        return sum(1 for m, _u, _j in self.calls if m == method)


def _client(script, **kwargs) -> tuple[ForusBotsClient, FakeHTTPClient]:
    fake = FakeHTTPClient(script)
    defaults = dict(
        base_url="https://forusbots.example.com",
        auth_token="t0ken",
        poll_interval_s=0.0,
        poll_max_interval_s=0.0,
        max_wait_s=60.0,
        client=fake,
    )
    defaults.update(kwargs)
    return ForusBotsClient(**defaults), fake


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make the poll loop instant."""
    monkeypatch.setattr(fb.asyncio, "sleep", AsyncMock())


_SUBMIT_OK = _resp(202, {"jobId": "j1", "queuePosition": 1, "estimate": {}, "capacitySnapshot": {}})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestScrapeHappyPath:

    async def test_submit_poll_succeeds(self):
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "running", "stage": "login"}),
            _resp(200, {"state": "succeeded", "result": {"census": {"First Name": "A"}},
                        "elapsedSeconds": 12, "stage": "done"}),
        ])

        result = await client.scrape_participant("158948", [{"key": "census", "fields": ["First Name"]}])

        assert result.state == "succeeded"
        assert result.job_id == "j1"
        assert result.result == {"census": {"First Name": "A"}}
        assert result.elapsed_seconds == 12
        assert result.stages == ["login", "done"]
        # one POST to submit, two GET polls
        assert fake.count("POST") == 1
        assert fake.count("GET") == 2
        assert fake.calls[0][1].endswith("/forusbot/scrape-participant")
        assert fake.calls[1][1].endswith("/forusbot/jobs/j1")

    async def test_scrape_plan_uses_plan_endpoint(self):
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {"basic_info": {}}}),
        ])

        result = await client.scrape_plan("580", [{"key": "basic_info", "fields": []}])

        assert result.state == "succeeded"
        assert fake.calls[0][1].endswith("/forusbot/scrape-plan")
        assert fake.calls[0][2]["planId"] == "580"

    async def test_elapsed_seconds_computed_locally_when_absent(self):
        # The public job response never includes elapsedSeconds (admin-only):
        # the client must fall back to locally measured wall time.
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {}}),
        ])
        result = await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert result.elapsed_seconds is not None
        assert isinstance(result.elapsed_seconds, float)

    async def test_elapsed_seconds_from_body_when_present(self):
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {}, "elapsedSeconds": 42}),
        ])
        result = await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert result.elapsed_seconds == 42


# ---------------------------------------------------------------------------
# Regression: real ForusBots job-status shape (data at body top level, NO
# "result" wrapper). Before the fix the client read body.get("result") -> None,
# so every scrape was silently dropped (normalize -> shape:"empty"). See the
# 2026-06-22 eval report (bug F0).
# ---------------------------------------------------------------------------

class TestRealJobStatusShape:

    # The actual payload ForusBots returns from GET /forusbot/jobs/{id}
    _REAL_BODY = {
        "state": "succeeded",
        "data": {
            "participantId": "342393",
            "census": {"Termination Date": "2025-03-14", "First Name": "Daantron",
                       "Last Name": "Ammons", "Eligibility Status": "Terminated"},
            "savings_rate": {"Account Balance": 1234.56, "Record Keeper": "LT Trust"},
            "mfa": {"MFA Status": "Not Enrolled"},
        },
        "warnings": [],
        "errors": [],
    }

    async def test_data_at_top_level_is_not_dropped(self):
        client, _ = _client([_SUBMIT_OK, _resp(200, self._REAL_BODY)])
        result = await client.scrape_participant(
            "342393", [{"key": "census", "fields": ["First Name"]}])
        assert result.state == "succeeded"
        # The fix passes the whole body through; the {data: ...} envelope survives.
        assert result.result is not None
        assert "census" in result.result.get("data", {})

    async def test_normalizer_consumes_real_shape(self):
        # End-to-end: what the client returns must normalize to real modules,
        # not to {"shape": "empty"} (the pre-fix failure mode).
        from data_pipeline import forusbots_catalog
        client, _ = _client([_SUBMIT_OK, _resp(200, self._REAL_BODY)])
        result = await client.scrape_participant(
            "342393", [{"key": "census", "fields": ["First Name"]}])
        flat, meta = forusbots_catalog.normalize_scrape_result(result.result)
        assert meta.get("shape") != "empty"
        assert set(flat) >= {"census", "savings_rate", "mfa"}
        assert flat["census"]["Eligibility Status"] == "Terminated"


# ---------------------------------------------------------------------------
# Terminal failure states
# ---------------------------------------------------------------------------

class TestTerminalFailures:

    async def test_failed_state_raises(self):
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "failed", "error": "participant not found"}),
        ])
        with pytest.raises(ForusBotsJobFailed) as ei:
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert ei.value.state == "failed"
        assert ei.value.code == "FORUSBOTS_JOB_FAILED"
        assert "participant not found" not in str(ei.value)

    async def test_canceled_state_raises(self):
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "canceled", "error": None}),
        ])
        with pytest.raises(ForusBotsJobFailed):
            await client.scrape_participant("x", [{"key": "census", "fields": []}])

    async def test_failed_state_discards_raw_upstream_error_body(self):
        raw_error = (
            "participant jane@example.com SSN 123 45 6789 "
            "account 1234 5678 9012"
        )
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "failed", "error": raw_error}),
        ])

        with pytest.raises(ForusBotsJobFailed) as exc_info:
            await client.scrape_participant(
                "x", [{"key": "census", "fields": []}]
            )

        error = exc_info.value
        assert error.code == "FORUSBOTS_JOB_FAILED"
        assert raw_error not in str(error)
        assert raw_error not in repr(error)
        assert getattr(error, "error", None) != raw_error


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TestTimeout:

    async def test_never_terminal_times_out(self):
        # max_wait_s=0 → the poll deadline is already past on the first check,
        # so the loop raises without ever issuing a poll GET. (Avoids patching
        # time.monotonic, which the asyncio event loop also consumes.)
        client, fake = _client([_SUBMIT_OK], max_wait_s=0.0)
        with pytest.raises(ForusBotsTimeout) as ei:
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert ei.value.job_id == "j1"
        assert fake.count("GET") == 0


# ---------------------------------------------------------------------------
# De-duplication + result cache
# ---------------------------------------------------------------------------

class TestDedupe:

    async def test_logs_do_not_emit_raw_or_dictionary_hashable_entity_id(
            self, caplog):
        """Los IDs numéricos tienen poca entropía: un SHA truncado sin clave
        sigue siendo identificable por fuerza bruta y no es anonimización."""
        import hashlib

        entity_id = "158948"
        weak_hash = hashlib.sha256(entity_id.encode()).hexdigest()[:10]
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {}}),
        ])

        with caplog.at_level("INFO"):
            await client.scrape_participant(
                entity_id, [{"key": "census", "fields": []}]
            )

        assert entity_id not in caplog.text
        assert weak_hash not in caplog.text

    async def test_concurrent_identical_scrapes_share_one_job(self):
        import asyncio
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {"census": {}}}),
        ])
        modules = [{"key": "census", "fields": ["First Name"]}]

        r1, r2 = await asyncio.gather(
            client.scrape_participant(
                "158948", modules, dedupe_scope="ticket-job-a",
            ),
            client.scrape_participant(
                "158948", modules, dedupe_scope="ticket-job-a",
            ),
        )

        assert r1.job_id == r2.job_id == "j1"
        # exactly one submit despite two callers
        assert fake.count("POST") == 1

    async def test_result_cache_reuses_without_new_submit(self):
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {"census": {}}}),
        ])
        modules = [{"key": "census", "fields": ["First Name"]}]

        await client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-a",
        )
        # second call hits the TTL result cache; no further HTTP calls scripted
        result2 = await client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-a",
        )

        assert result2.result == {"census": {}}
        assert fake.count("POST") == 1
        assert fake.count("GET") == 1

    async def test_identical_scrapes_in_different_scopes_never_share_payload(self):
        """La caché singleton no puede cruzar el límite de dos ticket jobs."""
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {"owner": "job-a"}}),
            _resp(202, {"jobId": "j2", "queuePosition": 1,
                        "estimate": {}, "capacitySnapshot": {}}),
            _resp(200, {"state": "succeeded", "result": {"owner": "job-b"}}),
        ])
        modules = [{"key": "census", "fields": ["First Name"]}]

        first = await client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-a",
        )
        second = await client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-b",
        )

        assert first.job_id == "j1"
        assert second.job_id == "j2"
        assert second.result == {"owner": "job-b"}
        assert fake.count("POST") == 2

    async def test_unscoped_calls_fail_closed_without_shared_cache(self):
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {"owner": "first"}}),
            _resp(202, {"jobId": "j2", "queuePosition": 1,
                        "estimate": {}, "capacitySnapshot": {}}),
            _resp(200, {"state": "succeeded", "result": {"owner": "second"}}),
        ])
        modules = [{"key": "census", "fields": []}]

        await client.scrape_participant("158948", modules)
        second = await client.scrape_participant("158948", modules)

        assert second.job_id == "j2"
        assert fake.count("POST") == 2


# ---------------------------------------------------------------------------
# Concurrency cap (semaphore)
# ---------------------------------------------------------------------------

class TestSemaphore:

    async def test_max_inflight_serializes_distinct_scrapes(self):
        import asyncio

        gate = asyncio.Event()        # holds job A's poll open
        entered = asyncio.Event()     # signals A acquired the slot and polled

        class GatedClient:
            def __init__(self):
                self.calls = []

            async def request(self, method, url, headers=None, json=None):
                self.calls.append((method, url))
                if method == "POST":
                    return _resp(202, {"jobId": f"job-{json['participantId']}", "estimate": {}})
                # GET poll
                if "job-A" in url:
                    entered.set()
                    await gate.wait()
                return _resp(200, {"state": "succeeded", "result": {}})

            async def aclose(self):
                pass

            def post_count(self):
                return sum(1 for m, _ in self.calls if m == "POST")

        fake = GatedClient()
        client = ForusBotsClient(
            base_url="https://x", auth_token="t",
            poll_interval_s=0.0, max_wait_s=60.0, max_inflight=1, client=fake,
        )
        mods = [{"key": "census", "fields": []}]
        t1 = asyncio.ensure_future(client.scrape_participant("A", mods))
        t2 = asyncio.ensure_future(client.scrape_participant("B", mods))

        await entered.wait()                       # A holds the only slot, blocked on its poll
        assert fake.post_count() == 1              # B cannot have submitted yet
        gate.set()                                 # release A; B proceeds afterwards
        await asyncio.gather(t1, t2)
        assert fake.post_count() == 2


# ---------------------------------------------------------------------------
# HTTP retry / error handling
# ---------------------------------------------------------------------------

class TestHTTPErrors:

    async def test_transport_error_discards_raw_url_exception_and_chain(self):
        sentinel = "jane@example.com/participant-158948"
        client, _ = _client([httpx.ReadTimeout(sentinel)])

        with pytest.raises(ForusBotsError) as captured:
            await client.scrape_participant(
                "158948", [{"key": "census", "fields": []}]
            )

        assert sentinel not in str(captured.value)
        assert sentinel not in repr(captured.value)
        assert captured.value.__cause__ is None

    async def test_4xx_raises_and_is_not_retried(self):
        client, fake = _client([_resp(401, {"ok": False, "error": "unauthorized"})])
        with pytest.raises(ForusBotsError):
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert fake.count("POST") == 1  # no retry on a client error

    async def test_submit_5xx_is_ambiguous_never_retried(self):
        """HT-16 (Task 8): un 5xx tras el POST de submit es ambiguo — el job
        upstream pudo haberse creado. La política anterior (retry) duplicaba
        jobs RPA; ahora se levanta ForusBotsAmbiguousSubmit sin reintento."""
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([
            _resp(503, {"ok": False}),
            _SUBMIT_OK,   # un retry consumiría esto y duplicaría el job
        ])
        with pytest.raises(ForusBotsAmbiguousSubmit):
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert fake.count("POST") == 1

    async def test_submit_429_is_ambiguous_without_upstream_contract(self):
        """Hasta recibir el contrato externo no se puede asumir que un 429
        de un proxy ocurrió antes de que el origin aceptara el POST."""
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([
            _resp(429, {"ok": False}),
            _SUBMIT_OK,
        ])
        with pytest.raises(ForusBotsAmbiguousSubmit):
            await client.scrape_participant(
                "x", [{"key": "census", "fields": []}]
            )
        assert fake.count("POST") == 1

    async def test_submit_408_is_ambiguous_without_upstream_contract(self):
        """Un timeout HTTP no prueba que el origin no haya creado el job."""
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([
            _resp(408, {"error": "request timeout"}),
            _SUBMIT_OK,
        ])

        with pytest.raises(ForusBotsAmbiguousSubmit) as captured:
            await client.scrape_participant(
                "x", [{"key": "census", "fields": []}]
            )

        assert captured.value.needs_reconciliation is True
        assert fake.count("POST") == 1

    async def test_submit_not_retried_on_read_timeout(self):
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([httpx.ReadTimeout("ambiguous timeout")])
        with pytest.raises(ForusBotsAmbiguousSubmit) as exc:
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert exc.value.needs_reconciliation is True
        assert fake.count("POST") == 1  # POST may have created a job -> never resubmit

    async def test_poll_retried_on_transport_error(self):
        client, fake = _client([
            _SUBMIT_OK,
            httpx.ReadError("transient"),
            _resp(200, {"state": "succeeded", "result": {}}),
        ])
        result = await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert result.state == "succeeded"
        assert fake.count("GET") == 2  # one failed, one ok

    async def test_confirmed_submit_preserves_job_id_when_poll_exhausts(self):
        from data_pipeline.forusbots_client import ForusBotsPollFailed

        client, fake = _client([
            _SUBMIT_OK,
            _resp(503, {"error": "one"}),
            _resp(503, {"error": "two"}),
            _resp(503, {"error": "three"}),
        ])

        with pytest.raises(ForusBotsPollFailed) as exc:
            await client.scrape_participant(
                "x", [{"key": "census", "fields": []}]
            )

        assert exc.value.job_id == "j1"
        assert exc.value.needs_reconciliation is True
        assert "j1" not in str(exc.value)
        assert exc.value.__cause__ is None
        assert fake.count("POST") == 1
        assert fake.count("GET") == 3

    async def test_submit_accepted_without_job_id_is_ambiguous(self):
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([_resp(202, {"accepted": True})])

        with pytest.raises(ForusBotsAmbiguousSubmit) as exc:
            await client.scrape_participant(
                "x", [{"key": "census", "fields": []}]
            )

        assert exc.value.needs_reconciliation is True
        assert fake.count("POST") == 1

    @pytest.mark.parametrize("invalid_job_id", [123, {"id": "j1"}, ["j1"], "   "])
    async def test_submit_rejects_non_string_or_blank_job_id(self, invalid_job_id):
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([_resp(202, {"jobId": invalid_job_id})])

        with pytest.raises(ForusBotsAmbiguousSubmit):
            await client.scrape_participant(
                "x", [{"key": "census", "fields": []}]
            )

        assert fake.count("POST") == 1
        assert fake.count("GET") == 0

    async def test_poll_percent_encodes_opaque_job_id_as_one_path_segment(self):
        client, fake = _client([
            _resp(202, {"jobId": "job/a?b#c"}),
            _resp(200, {"state": "succeeded", "result": {}}),
        ])

        result = await client.scrape_participant(
            "x", [{"key": "census", "fields": []}]
        )

        assert result.job_id == "job/a?b#c"
        assert fake.calls[1][1].endswith("/forusbot/jobs/job%2Fa%3Fb%23c")


class TestCircuitBreaker:

    async def test_open_circuit_stops_requests_already_waiting_for_semaphore(self):
        """Backlog admitted while closed must re-check before each submit."""
        import asyncio

        first_submit_started = asyncio.Event()
        release_first_submit = asyncio.Event()

        class GatedFailureHTTP:
            def __init__(self):
                self.calls = []

            async def request(self, method, url, headers=None, json=None):
                self.calls.append((method, url, json))
                if method == "POST" and len(self.calls) == 1:
                    first_submit_started.set()
                    await release_first_submit.wait()
                return _resp(401, {"error": "dependency unavailable"})

            async def aclose(self):
                pass

        fake = GatedFailureHTTP()
        client = ForusBotsClient(
            base_url="https://forusbots.example.com",
            auth_token="t0ken",
            poll_interval_s=0.0,
            poll_max_interval_s=0.0,
            max_wait_s=60.0,
            max_inflight=1,
            circuit_failure_threshold=1,
            client=fake,
        )
        modules = [{"key": "census", "fields": []}]
        requests = [
            asyncio.create_task(client.scrape_participant(
                f"participant-{index}", modules,
            ))
            for index in range(3)
        ]

        await first_submit_started.wait()
        await asyncio.sleep(0)
        release_first_submit.set()
        results = await asyncio.gather(*requests, return_exceptions=True)

        assert sum(
            method == "POST" for method, _url, _json in fake.calls
        ) == 1
        assert isinstance(results[0], ForusBotsError)
        assert all(isinstance(result, ForusBotsCircuitOpen)
                   for result in results[1:])

    async def test_terminal_business_failures_never_open_dependency_circuit(self):
        client, fake = _client(
            [
                _SUBMIT_OK,
                _resp(200, {"state": "failed", "error": "not scrapeable"}),
                _SUBMIT_OK,
                _resp(200, {"state": "failed", "error": "not scrapeable"}),
                _SUBMIT_OK,
                _resp(200, {"state": "succeeded", "result": {"ok": True}}),
            ],
            circuit_failure_threshold=2,
        )
        modules = [{"key": "census", "fields": []}]

        for _ in range(2):
            with pytest.raises(ForusBotsJobFailed):
                await client.scrape_participant("158948", modules)
        result = await client.scrape_participant("158948", modules)

        assert result.state == "succeeded"
        assert fake.count("POST") == 3
        assert client._circuit_state == "closed"
        assert client._circuit_failures == 0

    async def test_opens_fail_fast_then_half_open_probe_closes(
        self, monkeypatch, caplog
    ):
        now = [100.0]
        monkeypatch.setattr(fb.time, "monotonic", lambda: now[0])
        client, fake = _client(
            [
                _resp(401, {"error": "one"}),
                _resp(401, {"error": "two"}),
                _SUBMIT_OK,
                _resp(200, {"state": "succeeded", "result": {}}),
            ],
            circuit_failure_threshold=2,
            circuit_reset_s=30.0,
        )
        modules = [{"key": "census", "fields": []}]

        with caplog.at_level("INFO", logger="ticket_metrics"):
            for _ in range(2):
                with pytest.raises(ForusBotsError):
                    await client.scrape_participant("158948", modules)
            with pytest.raises(ForusBotsCircuitOpen):
                await client.scrape_participant("158948", modules)
            assert fake.count("POST") == 2

            now[0] += 31.0
            result = await client.scrape_participant("158948", modules)

        assert result.state == "succeeded"
        assert fake.count("POST") == 3
        assert '"state":"open"' in caplog.text
        assert '"state":"half_open"' in caplog.text
        assert '"state":"closed"' in caplog.text

    async def test_forusbots_outcome_metrics_never_include_job_or_entity_id(
        self, caplog
    ):
        client, _ = _client(
            [_SUBMIT_OK, _resp(200, {"state": "succeeded", "result": {}})]
        )

        with caplog.at_level("INFO"):
            await client.scrape_participant(
                "participant-158948", [{"key": "census", "fields": []}]
            )

        assert "participant-158948" not in caplog.text
        assert "job=j1" not in caplog.text
        assert '"code":"submit_success"' in caplog.text
        assert '"code":"poll_success"' in caplog.text


# ---------------------------------------------------------------------------
# Live sandbox (opt-in, skipped by default)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("FORUSBOTS_LIVE"), reason="set FORUSBOTS_LIVE=1 to run")
class TestLive:

    async def test_health_reachable(self):
        client = ForusBotsClient(
            base_url=os.getenv("FORUSBOTS_BASE_URL", "http://35.224.156.104:10000"),
            auth_token=os.getenv("FORUSBOTS_AUTH_TOKEN", ""),
        )
        try:
            resp = await client._http_request(
                "GET", f"{client._base}/forusbot/health", idempotent=True
            )
            assert resp.status_code == 200
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Task 2 regressions — HT-16 (POST 5xx ambiguo) y HT-17 (cancelación compartida)
# ---------------------------------------------------------------------------

class TestAmbiguousSubmit:

    async def test_post_5xx_is_not_blindly_resubmitted(self):
        """HT-16: un 5xx tras el POST de submit es AMBIGUO (el job pudo haberse
        creado upstream). No debe reintentarse el POST; debe marcarse como
        needs_reconciliation con un error tipado."""
        from data_pipeline.forusbots_client import ForusBotsAmbiguousSubmit

        client, fake = _client([
            _resp(500, {"error": "boom"}),
            # si el cliente reintentara, consumiría estos y duplicaría el job:
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "data": {}}),
        ])
        with pytest.raises(ForusBotsAmbiguousSubmit) as exc:
            await client.scrape_participant("158948", [{"key": "census", "fields": []}])
        assert fake.count("POST") == 1, (
            f"{fake.count('POST')} POSTs para un submit no-idempotente tras 5xx"
        )
        assert getattr(exc.value, "needs_reconciliation", False) is True

    async def test_get_5xx_still_retries(self):
        """El poll (GET) sí es idempotente: 5xx transitorio se reintenta."""
        client, fake = _client([
            _SUBMIT_OK,
            _resp(500, {"error": "transient"}),
            _resp(200, {"state": "succeeded", "data": {"census": {"Name": "x"}}}),
        ])
        result = await client.scrape_participant("158948", [{"key": "census", "fields": []}])
        assert result.job_id == "j1"
        assert fake.count("POST") == 1


class TestWaiterCancellationIsolation:

    async def test_cancelling_last_waiter_before_semaphore_prevents_late_submit(self):
        """A timed-out worker must not leave a detached task that submits later.

        The semaphore represents the shared ForusBots concurrency budget.  If
        the only waiter is cancelled while queued for that budget, there is no
        ambiguous network boundary to reconcile and the queued scrape must be
        cancelled before it can issue a POST.
        """
        import asyncio
        import contextlib

        async def _spin(n=10):
            loop = asyncio.get_running_loop()
            for _ in range(n):
                fut = loop.create_future()
                loop.call_soon(fut.set_result, None)
                await fut

        class RecordingHTTP:
            def __init__(self):
                self.calls = []

            async def request(self, method, url, headers=None, json=None):
                self.calls.append((method, url, json))
                if method == "POST":
                    return _resp(202, {"jobId": "late-job", "estimate": {}})
                return _resp(200, {"state": "succeeded", "data": {}})

            async def aclose(self):
                pass

        fake = RecordingHTTP()
        client = ForusBotsClient(
            base_url="https://forusbots.example.com",
            auth_token="t0ken",
            poll_interval_s=0.0,
            poll_max_interval_s=0.0,
            max_wait_s=60.0,
            max_inflight=1,
            client=fake,
        )
        await client._semaphore.acquire()
        waiter = asyncio.create_task(client.scrape_participant(
            "158948",
            [{"key": "census", "fields": []}],
            dedupe_scope="ticket-job-cancelled",
        ))
        await _spin()
        assert not fake.calls

        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        client._semaphore.release()
        await _spin()

        assert not fake.calls, "a detached scrape submitted after its last waiter ended"
        assert not client._inflight
        await client.aclose()

    async def test_presend_failure_after_last_waiter_cancels_never_retries_submit(self):
        """A proved-presend failure must not revive work whose owner is gone.

        The first transport attempt is conservatively inside the submit
        boundary while it is pending.  Once it reports ``ConnectError`` the
        client knows that attempt was side-effect free; if the sole waiter was
        cancelled meanwhile, retrying would create a brand-new late effect.
        """
        import asyncio
        import contextlib

        async def _spin(n=20):
            loop = asyncio.get_running_loop()
            for _ in range(n):
                fut = loop.create_future()
                loop.call_soon(fut.set_result, None)
                await fut

        class PresendFailureHTTP:
            def __init__(self):
                self.calls = []
                self.first_attempt_started = asyncio.Event()
                self.release_first_attempt = asyncio.Event()

            async def request(self, method, url, headers=None, json=None):
                self.calls.append((method, url, json))
                if len(self.calls) == 1:
                    self.first_attempt_started.set()
                    await self.release_first_attempt.wait()
                    raise httpx.ConnectError("proved pre-send failure")
                if method == "POST":
                    return _resp(202, {"jobId": "late-job", "estimate": {}})
                return _resp(200, {"state": "succeeded", "data": {}})

            async def aclose(self):
                pass

        fake = PresendFailureHTTP()
        client = ForusBotsClient(
            base_url="https://forusbots.example.com",
            auth_token="t0ken",
            poll_interval_s=0.0,
            poll_max_interval_s=0.0,
            max_wait_s=60.0,
            http_retries=2,
            client=fake,
        )
        waiter = asyncio.create_task(client.scrape_participant(
            "158948",
            [{"key": "census", "fields": []}],
            dedupe_scope="ticket-job-cancelled-during-connect",
        ))
        await fake.first_attempt_started.wait()

        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        fake.release_first_attempt.set()
        await _spin()

        assert sum(1 for method, _url, _json in fake.calls if method == "POST") == 1
        assert not client._inflight
        await client.aclose()

    async def test_cancelling_one_waiter_preserves_shared_scrape(self):
        """HT-17: cancelar el waiter originador no debe romper el dedupe ni
        provocar un submit duplicado para el mismo trabajo en curso."""
        import asyncio
        import contextlib

        # asyncio.sleep está mockeado por _no_sleep; forzar iteraciones reales
        # del loop con futures programados via call_soon.
        async def _spin(n=10):
            loop = asyncio.get_running_loop()
            for _ in range(n):
                fut = loop.create_future()
                loop.call_soon(fut.set_result, None)
                await fut

        gate = asyncio.Event()
        posted = asyncio.Event()
        submits = 0

        class GatedHTTP:
            def __init__(self):
                self.calls = []

            async def request(self, method, url, headers=None, json=None):
                nonlocal submits
                self.calls.append((method, url, json))
                if method == "POST":
                    submits += 1
                    posted.set()
                    return _resp(202, {"jobId": f"j{submits}", "queuePosition": 1,
                                       "estimate": {}, "capacitySnapshot": {}})
                # el poll espera hasta que el test libere el gate
                await gate.wait()
                return _resp(200, {"state": "succeeded",
                                   "data": {"census": {"Name": "x"}}})

            async def aclose(self):
                pass

        fake = GatedHTTP()
        client = ForusBotsClient(
            base_url="https://forusbots.example.com", auth_token="t0ken",
            poll_interval_s=0.0, poll_max_interval_s=0.0, max_wait_s=60.0,
            client=fake,
        )
        modules = [{"key": "census", "fields": []}]

        waiter_a = asyncio.create_task(client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-a",
        ))
        await asyncio.wait_for(posted.wait(), timeout=2)   # A ya hizo submit
        waiter_b = asyncio.create_task(client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-a",
        ))
        await _spin()                                       # B se une al trabajo

        waiter_a.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter_a                                  # cancelación procesada

        # C llega mientras el scrape sigue en vuelo: debe unirse, no re-submitir
        waiter_c = asyncio.create_task(client.scrape_participant(
            "158948", modules, dedupe_scope="ticket-job-a",
        ))
        await _spin()
        gate.set()

        result_b = await asyncio.wait_for(waiter_b, timeout=2)
        result_c = await asyncio.wait_for(waiter_c, timeout=2)
        assert result_b.job_id == "j1"
        assert result_c.job_id == "j1", (
            "una request idéntica tras la cancelación del originador re-submitió "
            f"({submits} submits): el dedupe se rompió"
        )
        assert submits == 1


# ---------------------------------------------------------------------------
# TLS / transporte (plan de finalización, Tarea 8 Paso 3)
# ---------------------------------------------------------------------------

class TestTransportSecurity:

    def test_constructor_rejects_non_tls_base_url(self):
        """El token viaja en x-auth-token: un base_url no-HTTPS debe fallar
        antes de emitir la request."""
        with pytest.raises(ForusBotsError, match="HTTPS"):
            ForusBotsClient(
                base_url="http://35.224.156.104:10000", auth_token="tok",
            )

    @pytest.mark.parametrize("base_url", [
        "https://user:raw-secret@forusbots.example.com",
        "https://forusbots.example.com?token=raw-secret",
        "https://forusbots.example.com#raw-secret",
        "https://forusbots.example.com/unreviewed-prefix",
    ])
    def test_constructor_rejects_noncanonical_https_origin_without_echoing_it(
        self, base_url,
    ):
        with pytest.raises(ForusBotsError, match="origen HTTPS") as captured:
            ForusBotsClient(base_url=base_url, auth_token="tok")

        assert "raw-secret" not in str(captured.value)

    def test_invalid_port_secret_is_absent_from_exception_chain_and_traceback(self):
        import traceback

        sentinel = "raw-secret"
        base_url = f"https://forusbots.example.com:{sentinel}"

        with pytest.raises(ForusBotsError) as captured:
            ForusBotsClient(base_url=base_url, auth_token="tok")

        rendered = "".join(traceback.format_exception(captured.value))
        assert captured.value.__cause__ is None
        assert sentinel not in rendered

    async def test_client_never_follows_redirects(self):
        """follow_redirects=False explícito: un 3xx a otro host filtraría el
        token."""
        client = ForusBotsClient(base_url="https://forusbots.example.com",
                                 auth_token="tok")
        try:
            assert client._client.follow_redirects is False
        finally:
            await client.aclose()

    async def test_authenticated_redirect_is_rejected(self):
        """Un 3xx en una request autenticada se rechaza (no se sigue)."""
        class RedirectClient:
            async def request(self, method, url, headers=None, json=None):
                return httpx.Response(302, headers={"location": "https://evil.example"})
            async def aclose(self):
                pass

        client = ForusBotsClient(base_url="https://forusbots.example.com",
                                 auth_token="tok", client=RedirectClient())
        try:
            with pytest.raises(ForusBotsError, match="redirect"):
                await client._http_request(
                    "GET", "https://forusbots.example.com/forusbot/health",
                    idempotent=True)
        finally:
            await client.aclose()
