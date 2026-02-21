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
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TEMPERATURE: float = 0.1
    OPENAI_REASONING_EFFORT: str = "medium"
    
    # Pinecone
    PINECONE_API_KEY: str = ""
    INDEX_NAME: str = "kb-articles-production"
    NAMESPACE: str = "kb_articles"
    
    # Logging
    LOG_LEVEL: str = "INFO"
    
    # Rate Limiting (requests per minute)
    RATE_LIMIT_REQUIRED_DATA: int = 60
    RATE_LIMIT_GENERATE_RESPONSE: int = 30
    
    model_config = {
        "env_file": ".env",
        "case_sensitive": True,
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
    
    if not settings.OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY no está configurada")
    
    if not settings.PINECONE_API_KEY:
        errors.append("PINECONE_API_KEY no está configurada")
    
    if errors:
        raise ValueError(f"Configuración inválida: {', '.join(errors)}")
    
    return True
