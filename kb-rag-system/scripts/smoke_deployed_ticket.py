"""
Smoke del servicio DESPLEGADO (Tarea 12/13). Verifica una revisión ya
desplegada (disabled) sin efectos: /livez, /readyz, rechazo de auth, v2
disabled. NO envía datos de participante ni encola tasks. Se ejecuta desde el
pipeline con la SA E2E; en producción sólo contra la revisión tag/disabled.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional
from urllib.parse import urlsplit

import httpx


def _cloud_run_base(value: str, label: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} contiene un puerto inválido") from exc
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or not host.endswith(".run.app")
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{label} debe ser una URL base HTTPS exacta de Cloud Run"
        )
    return f"https://{host}"


def _identity_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token

    token = fetch_id_token(Request(), audience)  # type: ignore[no-untyped-call]
    if not isinstance(token, str) or not token:
        raise RuntimeError("no se pudo obtener ID token para el smoke")
    return token


def _get(
    url: str,
    *,
    authorization_token: str,
    timeout: float = 10.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> tuple[Optional[int], bytes]:
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.get(
                url,
                headers={"Authorization": f"Bearer {authorization_token}"},
            )
        return response.status_code, response.content[:512]
    except httpx.HTTPError:
        return None, b""


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Smoke de la revisión desplegada")
    p.add_argument("--base-url", required=True)
    p.add_argument(
        "--audience",
        help="audiencia Cloud Run exacta; por defecto coincide con --base-url",
    )
    args = p.parse_args(argv)
    try:
        base = _cloud_run_base(args.base_url, "base URL")
        audience = _cloud_run_base(args.audience or args.base_url, "audience")
        if audience != base:
            raise ValueError("audience debe coincidir exactamente con base URL")
    except ValueError as exc:
        p.error(str(exc))
    token = _identity_token(audience)

    failures = []
    status, _ = _get(f"{base}/livez", authorization_token=token)
    if status != 200:
        failures.append(f"/livez devolvió {status}")

    status, _ = _get(f"{base}/readyz", authorization_token=token)
    if status != 200:
        failures.append(f"/readyz devolvió {status}")

    # v2 sin credenciales debe rechazar (401/403), nunca 200/500
    status, _ = _get(
        f"{base}/api/v2/ticket-jobs/does-not-exist",
        authorization_token=token,
    )
    if status not in (401, 403):
        failures.append(f"/api/v2 sin auth devolvió {status} (esperado 401/403)")

    if failures:
        for f in failures:
            print(f"SMOKE FAIL: {f}", file=sys.stderr)
        return 1
    print("smoke-deployed: ok")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
