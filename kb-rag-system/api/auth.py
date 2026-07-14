"""
Identidad de clientes de la API + identidad workload de v2 (plan Tarea 4).

Dos credenciales INDEPENDIENTES protegen v2 activo:

1. ``X-API-Key`` → resuelve QUÉ principal/tenant es el caller
   (``API_CLIENT_KEYS``/``API_CLIENT_TENANTS``; la ``API_KEY`` legacy mapea al
   principal ``"default"``). Identifica cliente/tenant, pero NO autoriza sola.
2. ``X-ForUs-Workload-Authorization: Bearer <ID token>`` → identidad workload
   Google-signed de la SA ``n8n-ticket-invoker-{env}``, verificada DENTRO de
   la app (firma/JWKS, issuer, audiencia exacta, email verificado, exp).
   Cloud Run elimina la firma de ``X-Serverless-Authorization`` antes de
   entregarlo al contenedor, así que ese header se RECHAZA siempre.

Nunca se persiste ni loggea una key o token raw; el principal/tenant derivado
es lo que viaja a jobs, logs y autorización de objetos.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, status

from .config import settings

logger = logging.getLogger(__name__)

LEGACY_PRINCIPAL = "default"
LEGACY_TENANT = "default"

WORKLOAD_AUTH_HEADER = "X-ForUs-Workload-Authorization"
FORBIDDEN_SERVERLESS_HEADER = "X-Serverless-Authorization"

_ALLOWED_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_ALLOWED_ALGS = ("RS256", "ES256")


@dataclass(frozen=True)
class AuthenticatedClient:
    """Resultado autenticado del mapping de clientes: el tenant es
    server-owned (deriva de la credencial, jamás del body/ticket)."""

    principal_id: str
    tenant_id: str


def resolve_principal(api_key: Optional[str]) -> Optional[str]:
    """Devuelve el nombre del principal para una API key válida, o None."""
    if not api_key:
        return None
    client_keys = settings.API_CLIENT_KEYS or {}
    for principal, key in client_keys.items():
        # str(): el dict viene de env JSON; un valor no-string no debe tirar
        # TypeError en el path de auth.
        if key and hmac.compare_digest(api_key, str(key)):
            return str(principal)
    if settings.API_KEY and hmac.compare_digest(api_key, settings.API_KEY):
        return LEGACY_PRINCIPAL
    return None


def resolve_client(api_key: Optional[str]) -> Optional[AuthenticatedClient]:
    principal = resolve_principal(api_key)
    if principal is None:
        return None
    tenants = settings.API_CLIENT_TENANTS or {}
    tenant = str(tenants.get(principal) or LEGACY_TENANT)
    return AuthenticatedClient(principal_id=principal, tenant_id=tenant)


async def authenticate_principal(request: Request) -> str:
    """Dependency: autentica y deja principal/tenant en ``request.state``."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing. Include 'X-API-Key' header.",
        )
    client = resolve_client(api_key)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
    request.state.principal_id = client.principal_id
    request.state.tenant_id = client.tenant_id
    return client.principal_id


# ---------------------------------------------------------------------------
# Identidad workload (WIF) de v2 — Tarea 4 Paso 2a
# ---------------------------------------------------------------------------

def _decode_unverified_header(token: str) -> Dict[str, Any]:
    """Header JOSE sin verificar, SIN red: filtra basura y ``alg=none`` antes
    de tocar los certificados de Google."""
    from google.auth import jwt as gjwt

    return gjwt.decode_header(token)


def _verify_google_id_token(token: str, audience: str) -> Dict[str, Any]:
    """Verificación real contra los certificados públicos de Google
    (``google-auth`` cachea certs según sus headers HTTP). Seam para tests."""
    from google.auth.transport import requests as garequests
    from google.oauth2 import id_token as gid

    return gid.verify_oauth2_token(token, garequests.Request(), audience=audience)


def workload_identity_enforced() -> bool:
    """La verificación corre cuando está configurada (audiencia + SA). En
    producción activa, ``validate_settings`` exige ambas: no hay bypass."""
    return bool(settings.TICKET_WIF_AUDIENCE and settings.TICKET_WIF_EXPECTED_EMAIL)


def verify_workload_identity_token(request: Request) -> None:
    """Valida la identidad workload de un request v2. Fail-closed:

    - ``X-Serverless-Authorization`` presente → 401 (firma ya despojada);
    - header ausente / sin Bearer → 401;
    - token no parseable, ``alg`` no asimétrico, firma inválida, issuer o
      audiencia incorrectos, email no verificado o SA inesperada → 403;
    - verificador indisponible (no se pueden obtener certs) → 503.

    Nunca loggea el token ni sus claims."""
    if request.headers.get(FORBIDDEN_SERVERLESS_HEADER) is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "FORBIDDEN_AUTH_HEADER",
                    "message": "X-Serverless-Authorization no es válido: Cloud "
                               "Run elimina su firma; usar "
                               "X-ForUs-Workload-Authorization"},
        )
    if not workload_identity_enforced():
        return

    raw = request.headers.get(WORKLOAD_AUTH_HEADER, "")
    if not raw.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WORKLOAD_IDENTITY_REQUIRED",
                    "message": f"falta {WORKLOAD_AUTH_HEADER}: Bearer <ID token>"},
        )
    token = raw.removeprefix("Bearer ").strip()

    # Pre-parseo local (sin red): rechaza basura y algoritmos no firmados
    # antes de pedir certificados.
    try:
        header = _decode_unverified_header(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "WORKLOAD_IDENTITY_INVALID"},
        )
    if header.get("alg") not in _ALLOWED_ALGS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "WORKLOAD_IDENTITY_UNSIGNED",
                    "message": "el ID token debe estar firmado (RS256/ES256)"},
        )

    try:
        claims = _verify_google_id_token(token, settings.TICKET_WIF_AUDIENCE)
    except HTTPException:
        raise
    except (ValueError, KeyError):
        # firma/audiencia/exp inválidos según google-auth
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "WORKLOAD_IDENTITY_INVALID"},
        )
    except Exception:
        # certs inaccesibles u otro fallo del verificador: fail-closed 503
        logger.exception("verificador de identidad workload no disponible")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "WORKLOAD_IDENTITY_VERIFIER_UNAVAILABLE"},
        )

    issuer = claims.get("iss")
    if issuer not in _ALLOWED_ISSUERS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "WORKLOAD_IDENTITY_INVALID"})
    audience = claims.get("aud")
    if audience != settings.TICKET_WIF_AUDIENCE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "WORKLOAD_IDENTITY_INVALID"})
    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "WORKLOAD_IDENTITY_INVALID"})
    if claims.get("email") != settings.TICKET_WIF_EXPECTED_EMAIL:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "WORKLOAD_IDENTITY_WRONG_CALLER"})
