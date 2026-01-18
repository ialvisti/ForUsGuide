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


async def add_request_id(request: Request, call_next):
    """
    Agrega un request ID único para tracking.
    """
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    # Agregar a response headers
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    
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
