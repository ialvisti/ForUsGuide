"""
Configuración de la API.

Maneja variables de entorno y settings de la aplicación.
"""

import os
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()


class Settings(BaseSettings):
    """Settings de la aplicación."""
    
    # API Configuration
    API_VERSION: str = "1.0.0"
    API_TITLE: str = "KB RAG System API"
    API_DESCRIPTION: str = "API para sistema RAG de Knowledge Base de Participant Advisory"
    
    # Server
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    
    # Security
    API_KEY: str = os.getenv("API_KEY", "")
    ALLOWED_ORIGINS: list = [
        "http://localhost:3000",
        "http://localhost:8000",
        "https://your-n8n-instance.com"  # Actualizar con URL real
    ]
    
    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_TEMPERATURE: float = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))
    
    # Pinecone
    PINECONE_API_KEY: str = os.getenv("PINECONE_API_KEY", "")
    INDEX_NAME: str = os.getenv("INDEX_NAME", "kb-articles-production")
    NAMESPACE: str = os.getenv("NAMESPACE", "kb_articles")
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Rate Limiting (requests per minute)
    RATE_LIMIT_REQUIRED_DATA: int = 60
    RATE_LIMIT_GENERATE_RESPONSE: int = 30
    
    class Config:
        env_file = ".env"
        case_sensitive = True


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
