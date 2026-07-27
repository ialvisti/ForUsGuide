"""Fail-closed contracts for the deployed, no-effect smoke probe."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_deployed_ticket.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("smoke_deployed_ticket", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_userinfo_url_fixtures_use_reserved_domains() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    urls = re.findall(r'["\'](https://[^"\']+@[^"\']+)["\']', source)
    userinfo_urls = [urlsplit(url) for url in urls]

    assert userinfo_urls
    assert all((url.hostname or "").endswith(".invalid")
               for url in userinfo_urls)


def test_smoke_fails_when_role_aware_readiness_is_not_ready(monkeypatch) -> None:
    module = _load_module()
    called: list[str] = []

    def fake_get(url: str, *, authorization_token: str,
                 timeout: float = 10.0):
        del authorization_token, timeout
        called.append(url)
        if url.endswith("/readyz"):
            return 503, b""
        if url.endswith("/livez"):
            return 200, b""
        return 401, b""

    monkeypatch.setattr(module, "_get", fake_get)
    monkeypatch.setattr(module, "_identity_token", lambda _audience: "token")

    assert module.main(["--base-url", "https://staging.run.app"]) == 1
    assert called[:2] == [
        "https://staging.run.app/livez",
        "https://staging.run.app/readyz",
    ]


@pytest.mark.parametrize("base_url", [
    "http://staging.run.app",
    "file:///etc/passwd",
    "https://user:pass@staging.invalid",
    "https://staging.run.app?redirect=https://evil.invalid",
    "https://staging.run.app/#fragment",
])
def test_smoke_rejects_unsafe_base_url_before_minting_token(
    monkeypatch, base_url: str,
) -> None:
    module = _load_module()
    minted = False

    def fake_token(_audience: str) -> str:
        nonlocal minted
        minted = True
        return "token"

    monkeypatch.setattr(module, "_identity_token", fake_token)

    with pytest.raises(SystemExit):
        module.main(["--base-url", base_url])
    assert minted is False


def test_http_client_never_follows_redirect_with_authorization() -> None:
    module = _load_module()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(
            302, headers={"Location": "https://evil.invalid/steal"},
        )

    status, _ = module._get(
        "https://staging.run.app/livez",
        authorization_token="token",
        transport=httpx.MockTransport(handler),
    )

    assert status == 302
    assert seen == ["https://staging.run.app/livez"]
