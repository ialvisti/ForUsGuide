#!/bin/bash
# Script para actualizar la configuración a GPT-5.4

set -e

echo "=========================================="
echo "🚀 Actualización a GPT-5.4"
echo "=========================================="
echo ""

# Verificar que estamos en el directorio correcto
if [ ! -f ".env" ]; then
    echo "❌ Error: Archivo .env no encontrado"
    echo "   Por favor ejecuta este script desde el directorio kb-rag-system/"
    exit 1
fi

echo "📋 Selecciona el modelo que quieres usar:"
echo ""
echo "1) GPT-5.4 (Thinking) - Razonamiento profundo [RECOMENDADO]"
echo "2) GPT-5.4 Chat (Instant) - Respuestas rápidas"
echo "3) GPT-5.4 Pro - Máxima capacidad"
echo "4) GPT-4o - Modelo anterior confiable"
echo "5) Mantener configuración actual"
echo ""
read -p "Selecciona una opción (1-5): " option

case $option in
    1)
        MODEL="gpt-5.4"
        EFFORT="medium"
        echo "✅ Seleccionado: GPT-5.4 (Thinking) con effort=medium"
        ;;
    2)
        MODEL="gpt-5.4-chat-latest"
        EFFORT="low"
        echo "✅ Seleccionado: GPT-5.4 Chat (Instant) con effort=low"
        ;;
    3)
        MODEL="gpt-5.4-pro"
        EFFORT="high"
        echo "✅ Seleccionado: GPT-5.4 Pro con effort=high"
        ;;
    4)
        MODEL="gpt-4o"
        EFFORT="medium"
        echo "✅ Seleccionado: GPT-4o"
        ;;
    5)
        echo "ℹ️  Manteniendo configuración actual"
        exit 0
        ;;
    *)
        echo "❌ Opción inválida"
        exit 1
        ;;
esac

echo ""
echo "📝 Actualizando archivo .env..."

# Crear backup del .env
cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
echo "✅ Backup creado: .env.backup.*"

# Actualizar o agregar OPENAI_MODEL
if grep -q "^OPENAI_MODEL=" .env; then
    # Reemplazar línea existente
    sed -i.tmp "s/^OPENAI_MODEL=.*/OPENAI_MODEL=$MODEL/" .env
    rm .env.tmp
else
    # Agregar nueva línea
    echo "OPENAI_MODEL=$MODEL" >> .env
fi

# Actualizar o agregar OPENAI_REASONING_EFFORT
if grep -q "^OPENAI_REASONING_EFFORT=" .env; then
    sed -i.tmp "s/^OPENAI_REASONING_EFFORT=.*/OPENAI_REASONING_EFFORT=$EFFORT/" .env
    rm .env.tmp
else
    echo "OPENAI_REASONING_EFFORT=$EFFORT" >> .env
fi

echo "✅ Archivo .env actualizado"
echo ""
echo "📋 Nueva configuración:"
echo "   OPENAI_MODEL=$MODEL"
echo "   OPENAI_REASONING_EFFORT=$EFFORT"
echo ""

# Preguntar si reiniciar la API
read -p "¿Quieres reiniciar la API ahora? (y/n): " restart

if [ "$restart" = "y" ] || [ "$restart" = "Y" ]; then
    echo ""
    echo "🔄 Reiniciando API..."
    
    # Activar entorno virtual si existe
    if [ -f "venv/bin/activate" ]; then
        source venv/bin/activate
        echo "✅ Entorno virtual activado"
    fi
    
    # Iniciar API
    echo "🚀 Iniciando API..."
    python -m api.main
else
    echo ""
    echo "ℹ️  Para aplicar los cambios, reinicia la API manualmente:"
    echo "   ./scripts/start_api.sh"
    echo ""
fi

echo ""
echo "=========================================="
echo "✨ Actualización completada"
echo "=========================================="
echo ""
echo "📚 Para más información, consulta: UPGRADE_TO_GPT5.md"
