"""
Middleware para la API.

Incluye autenticación, logging, y manejo de errores.
"""

import time
import uuid
import logging
import re
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from .config import settings

logger = logging.getLogger(__name__)


async def authenticate_request(request: Request):
    """
    Middleware de autenticación con API key.

    La API key debe enviarse en el header: X-API-Key
    """
    # Endpoints públicos (sin autenticación)
    public_endpoints = ["/", "/health", "/docs", "/redoc", "/openapi.json"]

    if request.url.path in public_endpoints:
        return

    # Verificar API key
    api_key = request.headers.get("X-API-Key")

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing. Include 'X-API-Key' header."
        )

    if api_key != settings.API_KEY:
        # Client IP is customer-linked data and is already available in the
        # platform access log with its own retention/access policy.
        logger.warning("Invalid API key attempted")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key"
        )


# Rutas servidas por cada rol de proceso (Tarea 4 Paso 1a). La MISMA app
# registra todas las rutas; este gate decide en runtime cuáles existen para
# el rol activo. Las demás devuelven 404 (no 403: no se revela su existencia).
_PROBE_PATHS = frozenset({"/livez", "/readyz", "/health"})
_WORKER_PATHS = frozenset({"/internal/tasks/ticket-job"}) | _PROBE_PATHS
_RECONCILER_PATHS = _PROBE_PATHS
_TICKET_BODY_LIMIT_PATHS = frozenset({
    "/api/v1/handle-ticket",
    "/api/v2/handle-ticket",
    "/internal/tasks/ticket-job",
})


def _path_allowed_for_role(path: str, role: str) -> bool:
    if role == "worker":
        return path in _WORKER_PATHS
    if role == "reconciler":
        return path in _RECONCILER_PATHS
    # producer: la API completa existente, NUNCA la ruta interna del worker
    return not path.startswith("/internal/")


async def enforce_app_role(request: Request, call_next):
    """Rutas excluyentes por rol: producer nunca sirve /internal/*; worker
    sólo sirve la ruta de Cloud Tasks + probes; reconciler sólo probes."""
    if not _path_allowed_for_role(request.url.path, settings.APP_ROLE):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "http_error", "message": "Not Found"},
        )
    return await call_next(request)


async def limit_body_size(request: Request, call_next):
    """Rechaza bodies gigantes ANTES de materializar JSON (HT-06).

    ``Content-Length`` es sólo una prevalidación; el stream siempre se cuenta
    porque un proxy/cliente puede omitirlo o declarar menos bytes que envía.
    """
    max_bytes = settings.MAX_REQUEST_BODY_BYTES
    if (
        request.method in ("POST", "PUT", "PATCH")
        and request.url.path in _TICKET_BODY_LIMIT_PATHS
        and max_bytes > 0
    ):
        declared = request.headers.get("content-length")
        if declared is not None and re.fullmatch(r"[0-9]+", declared) is None:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "http_error",
                    "detail": {"code": "INVALID_CONTENT_LENGTH"},
                    "message": "invalid content length",
                },
            )
        if declared is not None and int(declared) > max_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"error": "http_error",
                         "detail": {"code": "REQUEST_BODY_TOO_LARGE",
                                    "max_bytes": max_bytes},
                         "message": "request body too large"},
            )
        received = 0
        original_receive = request.receive

        async def counting_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail={"code": "REQUEST_BODY_TOO_LARGE",
                                "max_bytes": max_bytes},
                    )
            return message

        request._receive = counting_receive
    return await call_next(request)


async def add_request_id(request: Request, call_next):
    """
    Agrega un request ID único para tracking y conserva el correlation ID
    del caller (n8n) cuando llega en X-Correlation-ID: ambos IDs viajan en
    la respuesta y en los logs (Task 11).
    """
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    inbound = request.headers.get("X-Correlation-ID")
    correlation_id = (
        inbound
        if inbound is not None
        and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", inbound) is not None
        else None
    )
    if correlation_id is not None:
        request.state.correlation_id = correlation_id

    # Agregar a response headers
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if correlation_id is not None:
        response.headers["X-Correlation-ID"] = correlation_id

    return response


async def log_requests(request: Request, call_next):
    """
    Log de todas las requests.
    """
    start_time = time.time()
    request_id = getattr(request.state, "request_id", "unknown")

    path = _safe_route_path(request.url.path)
    # Request IDs are server-generated opaque UUIDs.  Client IP and raw path
    # segments are deliberately omitted (poll paths contain durable job IDs).
    logger.info(
        "Request started | ID: %s | Method: %s | Path: %s",
        request_id,
        request.method,
        path,
    )

    # Process request
    try:
        response = await call_next(request)

        # Log response
        duration = time.time() - start_time
        logger.info(
            "Request completed | ID: %s | Status: %d | Duration: %.3fs",
            request_id,
            response.status_code,
            duration,
        )

        return response

    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            "Request failed | ID: %s | ErrorType: %s | Duration: %.3fs",
            request_id,
            type(e).__name__,
            duration,
        )
        raise


def _safe_route_path(path: str) -> str:
    """Return a bounded route template, never a raw identifier-bearing path."""
    if re.fullmatch(r"/api/v1/tickets/[^/]+", path):
        return "/api/v1/tickets/{ticket_job_id}"
    if re.fullmatch(r"/api/v2/ticket-jobs/[^/]+", path):
        return "/api/v2/ticket-jobs/{ticket_job_id}"
    if path in {
        "/",
        "/health",
        "/livez",
        "/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/internal/tasks/ticket-job",
        "/api/v1/handle-ticket",
        "/api/v2/ticket-jobs",
    }:
        return path
    return "/{unclassified}"


async def handle_errors(request: Request, call_next):
    """
    Manejo global de errores.
    """
    try:
        return await call_next(request)

    except HTTPException:
        # Re-raise HTTP exceptions (ya manejadas)
        raise

    except Exception as e:
        # Exception messages can contain ticket text, upstream bodies or
        # identifiers. Emit only the stable type and request correlation; the
        # public response is likewise generic.
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "Unexpected error | ID: %s | ErrorType: %s",
            request_id,
            type(e).__name__,
        )

        # Return generic error response
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "internal_server_error",
                "message": "An unexpected error occurred",
                "request_id": request_id
            }
        )
