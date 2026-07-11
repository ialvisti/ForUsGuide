"""
Identidad de clientes de la API (Task 4/6 del plan de remediación).

Cloud Run IAM es la primera barrera (fuera del proceso); esta capa resuelve
QUÉ principal es el caller a partir de ``X-API-Key``:

- ``API_CLIENT_KEYS`` (nombre → key) permite múltiples clientes con
  credenciales propias y rotación por cliente.
- ``API_KEY`` legacy mapea al principal ``"default"`` (compat).

Nunca se persiste ni loggea la key raw; el principal derivado (nombre corto)
es lo que viaja a jobs, logs y autorización de objetos.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import HTTPException, Request, status

from .config import settings

LEGACY_PRINCIPAL = "default"


def resolve_principal(api_key: Optional[str]) -> Optional[str]:
    """Devuelve el nombre del principal para una API key válida, o None."""
    if not api_key:
        return None
    client_keys = settings.API_CLIENT_KEYS or {}
    for principal, key in client_keys.items():
        if key and hmac.compare_digest(api_key, key):
            return principal
    if settings.API_KEY and hmac.compare_digest(api_key, settings.API_KEY):
        return LEGACY_PRINCIPAL
    return None


async def authenticate_principal(request: Request) -> str:
    """Dependency: autentica y devuelve el principal; lo deja en
    ``request.state.principal_id`` para logging/autorización."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing. Include 'X-API-Key' header.",
        )
    principal = resolve_principal(api_key)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
    request.state.principal_id = principal
    return principal
