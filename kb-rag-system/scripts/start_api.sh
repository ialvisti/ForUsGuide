#!/bin/bash

##############################################################################
# Script para iniciar la API FastAPI
#
# Uso:
#   bash scripts/start_api.sh                # Development mode (con reload)
#   bash scripts/start_api.sh --production   # Production mode
##############################################################################

set -e

echo "=========================================="
echo "KB RAG System API - Starting"
echo "=========================================="
echo ""

# Verificar que estamos en el directorio correcto
if [ ! -f "api/main.py" ]; then
    echo "❌ Error: Debe ejecutar este script desde el directorio kb-rag-system/"
    exit 1
fi

# Activar virtual environment si existe
if [ -d "venv" ]; then
    echo "🔧 Activando virtual environment..."
    source venv/bin/activate
fi

# Verificar .env
if [ ! -f ".env" ]; then
    echo "⚠️  Warning: Archivo .env no encontrado"
    echo "   Crea el archivo .env con las siguientes variables:"
    echo "   - PINECONE_API_KEY"
    echo "   - OPENAI_API_KEY"
    echo "   - API_KEY"
    echo ""
    read -p "¿Continuar sin .env? (y/n): " continue
    if [ "$continue" != "y" ]; then
        exit 1
    fi
fi

# Determinar modo (development o production)
MODE="development"
if [ "$1" = "--production" ] || [ "$1" = "-p" ]; then
    MODE="production"
fi

echo "📊 Modo: $MODE"
echo ""

# Iniciar servidor
if [ "$MODE" = "production" ]; then
    echo "🚀 Iniciando servidor (production mode)..."
    # Unica configuracion soportada: WEB_CONCURRENCY (igual que Dockerfile).
    uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers "${WEB_CONCURRENCY:-1}" \
        --log-level info \
        --no-access-log
else
    echo "🔧 Iniciando servidor (development mode con auto-reload)..."
    uvicorn api.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --reload \
        --log-level debug
fi
