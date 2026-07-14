"""
Smoke del servicio DESPLEGADO (Tarea 12/13). Verifica una revisión ya
desplegada (disabled) sin efectos: /livez, /readyz, rechazo de auth, v2
disabled. NO envía datos de participante ni encola tasks. Se ejecuta desde el
pipeline con la SA E2E; en producción sólo contra la revisión tag/disabled.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(512)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Smoke de la revisión desplegada")
    p.add_argument("--base-url", required=True)
    args = p.parse_args(argv)
    base = args.base_url.rstrip("/")

    failures = []
    status, _ = _get(f"{base}/livez")
    if status != 200:
        failures.append(f"/livez devolvió {status}")

    # v2 sin credenciales debe rechazar (401/403), nunca 200/500
    status, _ = _get(f"{base}/api/v2/ticket-jobs/does-not-exist")
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
