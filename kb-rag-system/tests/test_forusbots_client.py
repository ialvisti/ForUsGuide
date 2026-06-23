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
        assert "participant not found" in str(ei.value)

    async def test_canceled_state_raises(self):
        client, _ = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "canceled", "error": None}),
        ])
        with pytest.raises(ForusBotsJobFailed):
            await client.scrape_participant("x", [{"key": "census", "fields": []}])


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

    async def test_concurrent_identical_scrapes_share_one_job(self):
        import asyncio
        client, fake = _client([
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {"census": {}}}),
        ])
        modules = [{"key": "census", "fields": ["First Name"]}]

        r1, r2 = await asyncio.gather(
            client.scrape_participant("158948", modules),
            client.scrape_participant("158948", modules),
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

        await client.scrape_participant("158948", modules)
        # second call hits the TTL result cache; no further HTTP calls scripted
        result2 = await client.scrape_participant("158948", modules)

        assert result2.result == {"census": {}}
        assert fake.count("POST") == 1
        assert fake.count("GET") == 1


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

    async def test_4xx_raises_and_is_not_retried(self):
        client, fake = _client([_resp(401, {"ok": False, "error": "unauthorized"})])
        with pytest.raises(ForusBotsError):
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert fake.count("POST") == 1  # no retry on a client error

    async def test_submit_5xx_is_retried_then_succeeds(self):
        client, fake = _client([
            _resp(503, {"ok": False}),
            _SUBMIT_OK,
            _resp(200, {"state": "succeeded", "result": {}}),
        ])
        result = await client.scrape_participant("x", [{"key": "census", "fields": []}])
        assert result.state == "succeeded"
        assert fake.count("POST") == 2  # 503 then 202

    async def test_submit_not_retried_on_read_timeout(self):
        client, fake = _client([httpx.ReadTimeout("ambiguous timeout")])
        with pytest.raises(ForusBotsError):
            await client.scrape_participant("x", [{"key": "census", "fields": []}])
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


# ---------------------------------------------------------------------------
# Live sandbox (opt-in, skipped by default)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("FORUSBOTS_LIVE"), reason="set FORUSBOTS_LIVE=1 to run")
class TestLive:

    async def test_health_reachable(self):
        client = ForusBotsClient(
            base_url=os.getenv("FORUSBOTS_BASE_URL", "https://forusbots-6jyh.onrender.com"),
            auth_token=os.getenv("FORUSBOTS_AUTH_TOKEN", ""),
        )
        try:
            resp = await client._http_request(
                "GET", f"{client._base}/forusbot/health", idempotent=True
            )
            assert resp.status_code == 200
        finally:
            await client.aclose()
