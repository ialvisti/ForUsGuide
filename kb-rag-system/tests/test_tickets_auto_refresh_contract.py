"""Static contract for bounded, visibility-aware ticket queue refresh."""

from __future__ import annotations

import re
from pathlib import Path


APP = (
    Path(__file__).resolve().parents[1]
    / "ui"
    / "tickets"
    / "assets"
    / "app.js"
)


def test_queue_auto_refreshes_without_waiting_for_manual_reload():
    source = APP.read_text(encoding="utf-8")
    match = re.search(r"const QUEUE_REFRESH_MS = ([0-9_]+);", source)
    assert match is not None
    interval_ms = int(match.group(1).replace("_", ""))
    assert 10_000 <= interval_ms <= 30_000
    assert "setInterval(refreshVisibleQueue, QUEUE_REFRESH_MS)" in source
    assert 'document.addEventListener("visibilitychange"' in source


def test_auto_refresh_is_bounded_to_an_idle_visible_first_page():
    source = APP.read_text(encoding="utf-8")
    function = source.split("function refreshVisibleQueue()", 1)[1].split(
        "function startQueueAutoRefresh()", 1
    )[0]
    assert 'document.visibilityState !== "visible"' in function
    assert 'state.phase === "ready"' in function
    assert 'state.phase === "error"' in function
    assert 'state.error?.recoverable === true' in function
    assert 'state.error?.status >= 500' in function
    assert 'state.error?.status === 429' in function
    assert 'state.selected !== ""' in function
    assert "state.cursor !== null" in function
    assert "api.cooldownRemainingS() > 0" in function
    assert "load({ refresh: true })" in function


def test_pagehide_stops_polling_and_aborts_requests():
    source = APP.read_text(encoding="utf-8")
    assert "clearInterval(queueRefreshTimer)" in source
    assert "queueRefreshTimer = null" in source
    assert "api.abortAll()" in source


def test_pageshow_restarts_bfcache_polling_once_and_refreshes_immediately():
    source = APP.read_text(encoding="utf-8")
    handler = source.split("function handlePageShow(event)", 1)[1].split(
        "function startCooldown", 1
    )[0]

    assert "if (!event.persisted)" in handler
    assert "startQueueAutoRefresh()" in handler
    assert "refreshVisibleQueue()" in handler
    assert 'globalThis.addEventListener("pageshow", handlePageShow)' in source
    assert source.count(
        'document.addEventListener("visibilitychange", handleVisibilityChange)'
    ) == 1
