"""
Middleware para la API.

Incluye autenticación, logging, y manejo de errores.
"""

import time
import uuid
import logging
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
        logger.warning(f"Invalid API key attempted from {request.client.host}")
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

    Dos capas: (1) Content-Length declarado; (2) contador sobre el stream
    para requests chunked sin Content-Length.
    """
    max_bytes = settings.MAX_REQUEST_BODY_BYTES
    if request.method in ("POST", "PUT", "PATCH") and max_bytes > 0:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"error": "http_error",
                         "detail": {"code": "REQUEST_BODY_TOO_LARGE",
                                    "max_bytes": max_bytes},
                         "message": "request body too large"},
            )
        if not declared:
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
    if inbound:
        request.state.correlation_id = inbound[:128]

    # Agregar a response headers
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if inbound:
        response.headers["X-Correlation-ID"] = inbound[:128]

    return response


async def log_requests(request: Request, call_next):
    """
    Log de todas las requests.
    """
    start_time = time.time()
    request_id = getattr(request.state, "request_id", "unknown")
    
    # Log request
    logger.info(
        f"Request started | "
        f"ID: {request_id} | "
        f"Method: {request.method} | "
        f"Path: {request.url.path} | "
        f"Client: {request.client.host if request.client else 'unknown'}"
    )
    
    # Process request
    try:
        response = await call_next(request)
        
        # Log response
        duration = time.time() - start_time
        logger.info(
            f"Request completed | "
            f"ID: {request_id} | "
            f"Status: {response.status_code} | "
            f"Duration: {duration:.3f}s"
        )
        
        return response
    
    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            f"Request failed | "
            f"ID: {request_id} | "
            f"Error: {str(e)} | "
            f"Duration: {duration:.3f}s"
        )
        raise


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
        # Log unexpected errors
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception(f"Unexpected error | ID: {request_id} | Error: {str(e)}")
        
        # Return generic error response
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "internal_server_error",
                "message": "An unexpected error occurred",
                "request_id": request_id
            }
        )
