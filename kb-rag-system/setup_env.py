#!/usr/bin/env python3
"""
Script para configurar el archivo .env de manera interactiva.
"""
import os

def setup_env():
    print("🔧 Configuración de variables de entorno\n")
    print("Este script te ayudará a crear tu archivo .env\n")
    
    # Solicitar API keys
    pinecone_key = input("📌 Ingresa tu PINECONE_API_KEY: ").strip()
    openai_key = input("🤖 Ingresa tu OPENAI_API_KEY: ").strip()
    
    # Configuración por defecto
    index_name = input("\n📊 Nombre del índice en Pinecone [kb-articles-production]: ").strip() or "kb-articles-production"
    namespace = input("📁 Namespace para los artículos [kb_articles]: ").strip() or "kb_articles"
    api_key = input("🔐 API key para autenticación del endpoint [genera uno aleatorio]: ").strip()
    
    if not api_key:
        import secrets
        api_key = secrets.token_urlsafe(32)
        print(f"   ✅ API key generada: {api_key}")
    
    # Crear contenido del .env
    env_content = f"""# Pinecone Configuration
PINECONE_API_KEY={pinecone_key}

# OpenAI Configuration
OPENAI_API_KEY={openai_key}

# Application Configuration
INDEX_NAME={index_name}
NAMESPACE={namespace}
ENVIRONMENT=development

# API Configuration
API_HOST=0.0.0.0
API_PORT=8000
API_KEY={api_key}
"""
    
    # Escribir archivo
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    with open(env_path, 'w') as f:
        f.write(env_content)
    
    print(f"\n✅ Archivo .env creado exitosamente en: {env_path}")
    print("\n⚠️  IMPORTANTE: No compartas este archivo ni lo subas a git")
    print("   El archivo .gitignore ya está configurado para ignorarlo.\n")

if __name__ == "__main__":
    setup_env()
