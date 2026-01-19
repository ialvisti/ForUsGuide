#!/bin/bash

# Script para iniciar la UI del KB RAG System
# Usa Python 3 para servir el archivo HTML en el puerto 3000

echo "=========================================="
echo "  KB RAG System - UI Server"
echo "=========================================="
echo ""

# Check if we're in the right directory
if [ ! -f "index.html" ]; then
    echo "❌ Error: index.html not found"
    echo "Please run this script from the ui/ directory"
    exit 1
fi

# Get the port (default 3000)
PORT=${1:-3000}

echo "🚀 Starting UI server on port $PORT..."
echo ""
echo "📱 Open in browser: http://localhost:$PORT"
echo ""
echo "⚠️  IMPORTANTE:"
echo "   1. Asegúrate de que la API esté corriendo en http://localhost:8000"
echo "   2. Configura tu API Key en la interfaz"
echo ""
echo "💡 Tips:"
echo "   - Usa Ctrl+C para detener el servidor"
echo "   - La API debe estar activa: bash ../scripts/start_api.sh"
echo ""
echo "=========================================="
echo ""

# Start Python HTTP server
python3 -m http.server $PORT

# If Python 3 is not available, try Python 2
if [ $? -ne 0 ]; then
    echo "⚠️  Python 3 not found, trying Python 2..."
    python -m SimpleHTTPServer $PORT
fi
