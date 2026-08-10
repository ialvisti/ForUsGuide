"""Lightweight Google ID-token verification helpers.

Kept separate from :mod:`api.auth` so internal service apps do not initialize
the RAG application's global settings merely to verify an OIDC caller.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, cast

WORKLOAD_AUTH_HEADER = "X-ForUs-Workload-Authorization"
FORBIDDEN_SERVERLESS_HEADER = "X-Serverless-Authorization"

_google_auth_request: Any = None
_google_auth_request_init_lock = threading.Lock()
_google_auth_verify_lock = threading.Lock()


def decode_unverified_header(token: str) -> dict[str, Any]:
    """Decode only the JOSE header so unsigned algorithms can fail locally."""
    from google.auth import jwt as google_jwt

    decoder = cast(Callable[..., object], google_jwt.decode_header)
    decoded = decoder(token)
    if not isinstance(decoded, dict):
        raise ValueError("invalid JOSE header")
    return {str(key): value for key, value in decoded.items()}


def verify_google_id_token(token: str, audience: str) -> dict[str, Any]:
    """Verify signature, expiry, and audience against Google's certificates."""
    global _google_auth_request

    if _google_auth_request is None:
        with _google_auth_request_init_lock:
            if _google_auth_request is None:
                import requests
                from cachecontrol import CacheControl
                from google.auth.transport import requests as google_requests

                _google_auth_request = google_requests.Request(
                    session=CacheControl(requests.Session())
                )

    from google.oauth2 import id_token

    verifier = cast(Callable[..., object], id_token.verify_oauth2_token)
    with _google_auth_verify_lock:
        verified = verifier(token, _google_auth_request, audience=audience)
    if not isinstance(verified, dict):
        raise ValueError("invalid Google ID token claims")
    return {str(key): value for key, value in verified.items()}


__all__ = [
    "FORBIDDEN_SERVERLESS_HEADER",
    "WORKLOAD_AUTH_HEADER",
    "decode_unverified_header",
    "verify_google_id_token",
]
