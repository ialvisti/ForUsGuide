"""
Configuración de la API.

Maneja variables de entorno y settings de la aplicación.
Pydantic BaseSettings reads env vars automatically — no os.getenv needed.
"""

from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings de la aplicación."""
    
    # API Configuration
    API_VERSION: str = "1.0.0"
    API_TITLE: str = "KB RAG System API"
    API_DESCRIPTION: str = "API para sistema RAG de Knowledge Base de Participant Advisory"
    
    # Server
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    ENVIRONMENT: str = "development"
    
    # Security
    API_KEY: str = ""
    ALLOWED_ORIGINS: List[str] = ["*"]
    
    # OpenAI
    OPENAI_API_KEY: str = ""
    # NOTE: OPENAI_MODEL / OPENAI_TEMPERATURE / OPENAI_REASONING_EFFORT remain
    # readable for backward compatibility, but runtime model selection now
    # flows through the LLM_ROUTE_* vars + LLMRouter.
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TEMPERATURE: float = 0.1
    OPENAI_REASONING_EFFORT: str = "medium"

    # Gemini / Vertex AI
    GEMINI_API_KEY: str = ""
    USE_VERTEX_AI: bool = False
    GCP_LOCATION: str = "us-central1"

    # LLM Routing (model name per task; provider is inferred from prefix).
    LLM_ROUTE_DECOMPOSE: str = "gpt-5.4"
    LLM_ROUTE_REQUIRED_DATA: str = "gpt-5.4"
    LLM_ROUTE_GR_OUTCOME: str = "gpt-5.4"
    LLM_ROUTE_GR_RESPONSE: str = "gpt-5.4"
    LLM_ROUTE_KNOWLEDGE: str = "gpt-5.4"

    # Pinecone
    PINECONE_API_KEY: str = ""
    INDEX_NAME: str = "kb-articles-production"
    NAMESPACE: str = "kb_articles"
    
    # Logging
    LOG_LEVEL: str = "INFO"
    
    # Rate Limiting (requests per minute)
    RATE_LIMIT_REQUIRED_DATA: int = 60
    RATE_LIMIT_GENERATE_RESPONSE: int = 30
    
    # GCP
    GCP_PROJECT: str = ""
    GCS_BUCKET: str = ""
    ENABLE_EXECUTION_LOGGING: bool = False
    
    model_config = {
        "env_file": ".env",
        "case_sensitive": True,
        "extra": "ignore",
    }

    @property
    def cors_origins(self) -> List[str]:
        """Return CORS origins based on environment."""
        if self.ENVIRONMENT == "production":
            return [o for o in self.ALLOWED_ORIGINS if o != "*"] or [
                "https://forusguide.onrender.com"
            ]
        return self.ALLOWED_ORIGINS


# Singleton instance
settings = Settings()


def validate_settings():
    """Valida que todas las settings críticas estén configuradas."""
    errors = []

    if not settings.API_KEY:
        errors.append("API_KEY no está configurada")

    if not settings.PINECONE_API_KEY:
        errors.append("PINECONE_API_KEY no está configurada")

    has_openai = bool(settings.OPENAI_API_KEY)
    has_gemini = bool(settings.GEMINI_API_KEY) or (
        settings.USE_VERTEX_AI and bool(settings.GCP_PROJECT)
    )

    if not has_openai and not has_gemini:
        errors.append(
            "Debe configurarse al menos un proveedor LLM: "
            "OPENAI_API_KEY, o GEMINI_API_KEY, o USE_VERTEX_AI=true + GCP_PROJECT"
        )

    # Each route's model must have its provider's credentials available.
    route_models = {
        "LLM_ROUTE_DECOMPOSE": settings.LLM_ROUTE_DECOMPOSE,
        "LLM_ROUTE_REQUIRED_DATA": settings.LLM_ROUTE_REQUIRED_DATA,
        "LLM_ROUTE_GR_OUTCOME": settings.LLM_ROUTE_GR_OUTCOME,
        "LLM_ROUTE_GR_RESPONSE": settings.LLM_ROUTE_GR_RESPONSE,
        "LLM_ROUTE_KNOWLEDGE": settings.LLM_ROUTE_KNOWLEDGE,
    }
    for var_name, model_name in route_models.items():
        model_lower = (model_name or "").strip().lower()
        if not model_lower:
            errors.append(f"{var_name} no puede estar vacío")
            continue
        if model_lower.startswith("gpt-") and not has_openai:
            errors.append(
                f"{var_name}={model_name} requiere OPENAI_API_KEY configurada"
            )
        elif model_lower.startswith("gemini-") and not has_gemini:
            errors.append(
                f"{var_name}={model_name} requiere GEMINI_API_KEY o "
                f"USE_VERTEX_AI=true + GCP_PROJECT"
            )
        elif not (model_lower.startswith("gpt-") or model_lower.startswith("gemini-")):
            errors.append(
                f"{var_name}={model_name} tiene un prefijo desconocido "
                f"(se esperaba 'gpt-*' o 'gemini-*')"
            )

    if errors:
        raise ValueError(f"Configuración inválida: {', '.join(errors)}")

    return True
