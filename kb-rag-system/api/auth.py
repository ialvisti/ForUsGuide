"""Identidad de clientes de la API.

El contrato desplegado conserva la integración histórica de n8n: Cloud Run
valida el Google ID token de ``kb-rag-client`` en ``Authorization`` y la
aplicación valida el ``X-API-Key`` existente. No requiere cuentas, roles ni
credenciales de AWS, ni un segundo bearer header.

Las utilidades de validación workload que permanecen en este módulo son
opt-in y no forman parte de la configuración desplegada. Nunca se persiste ni
loggea una key o token raw; sólo el principal/tenant derivado.
"""

from __future__ import annotations

import hmac
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, cast

from fastapi import HTTPException, Request, status

from .config import settings

logger = logging.getLogger(__name__)

LEGACY_PRINCIPAL = "default"
LEGACY_TENANT = "default"

WORKLOAD_AUTH_HEADER = "X-ForUs-Workload-Authorization"
FORBIDDEN_SERVERLESS_HEADER = "X-Serverless-Authorization"

_ALLOWED_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_ALLOWED_ALGS = ("RS256", "ES256")

_google_auth_request: Any = None
_google_auth_request_init_lock = threading.Lock()
_google_auth_verify_lock = threading.Lock()


@dataclass(frozen=True)
class AuthenticatedClient:
    """Resultado autenticado del mapping de clientes: el tenant es
    server-owned (deriva de la credencial, jamás del body/ticket)."""

    principal_id: str
    tenant_id: str


def resolve_principal(
    api_key: Optional[str], *, allow_legacy: bool = True
) -> Optional[str]:
    """Devuelve el nombre del principal para una API key válida, o None."""
    if not api_key:
        return None
    client_keys = settings.API_CLIENT_KEYS or {}
    for principal, configured in client_keys.items():
        keys = (configured,) if isinstance(configured, str) else tuple(configured)
        for key in keys:
            if key and hmac.compare_digest(api_key, key):
                return str(principal)
    if (allow_legacy and settings.API_KEY
            and hmac.compare_digest(api_key, settings.API_KEY)):
        return LEGACY_PRINCIPAL
    return None


def resolve_client(
    api_key: Optional[str], *, allow_legacy: bool = True,
    require_tenant: bool = False,
) -> Optional[AuthenticatedClient]:
    """Resolve a credential to a server-owned principal/tenant.

    v1 may temporarily opt into the legacy ``API_KEY`` mapping during its
    migration window. v2 must call this with ``allow_legacy=False`` and
    ``require_tenant=True`` so a missing mapping never becomes ``default``.
    """
    principal = resolve_principal(api_key, allow_legacy=allow_legacy)
    if principal is None:
        return None
    tenants = settings.API_CLIENT_TENANTS or {}
    mapped_tenant = tenants.get(principal)
    if require_tenant and not mapped_tenant:
        return None
    tenant = str(mapped_tenant or LEGACY_TENANT)
    return AuthenticatedClient(principal_id=principal, tenant_id=tenant)


async def authenticate_principal(
    request: Request, *, allow_legacy: bool = True,
    require_tenant: bool = False,
) -> str:
    """Dependency: autentica y deja principal/tenant en ``request.state``."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing. Include 'X-API-Key' header.",
        )
    client = resolve_client(
        api_key,
        allow_legacy=allow_legacy,
        require_tenant=require_tenant,
    )
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

    decoder = cast(Callable[..., object], gjwt.decode_header)
    decoded = decoder(token)
    if not isinstance(decoded, dict):
        raise ValueError("invalid JOSE header")
    return {str(key): value for key, value in decoded.items()}


def _verify_google_id_token(token: str, audience: str) -> Dict[str, Any]:
    """Verificación real contra los certificados públicos de Google
    mediante un transporte HTTP singleton con caché. Seam para tests."""
    global _google_auth_request

    # google-auth fetches the certificate endpoint on every verification; its
    # Request adapter does not cache by itself.  CacheControl honors Google's
    # Cache-Control/ETag headers, while the process lock keeps the shared
    # requests.Session safe under concurrent sync FastAPI dependencies.
    if _google_auth_request is None:
        with _google_auth_request_init_lock:
            if _google_auth_request is None:
                import requests
                from cachecontrol import CacheControl
                from google.auth.transport import requests as garequests

                _google_auth_request = garequests.Request(
                    session=CacheControl(requests.Session()),
                )

    from google.oauth2 import id_token as gid

    verifier = cast(Callable[..., object], gid.verify_oauth2_token)
    with _google_auth_verify_lock:
        verified = verifier(
            token, _google_auth_request, audience=audience,
        )
    if not isinstance(verified, dict):
        raise ValueError("invalid Google ID token claims")
    return {str(key): value for key, value in verified.items()}


def workload_identity_enforced() -> bool:
    """Return whether audience plus an exact caller set are configured.

    Deployed active environments are required to use the list.  The singular
    field remains a local migration fallback only; Terraform never injects it.
    """
    return bool(settings.TICKET_WIF_AUDIENCE and _allowed_workload_emails())


def _allowed_workload_emails() -> tuple[str, ...]:
    configured = tuple(settings.TICKET_WIF_ALLOWED_EMAILS or ())
    if configured:
        return configured
    legacy = settings.TICKET_WIF_EXPECTED_EMAIL
    return (legacy,) if legacy else ()


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
        ) from None
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
        ) from None
    except Exception as exc:
        # certs inaccesibles u otro fallo del verificador: fail-closed 503
        logger.error("verificador de identidad workload no disponible")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "WORKLOAD_IDENTITY_VERIFIER_UNAVAILABLE"},
        ) from exc

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
    if claims.get("email") not in _allowed_workload_emails():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "WORKLOAD_IDENTITY_WRONG_CALLER"})
