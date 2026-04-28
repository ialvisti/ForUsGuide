#!/bin/bash
# Script para actualizar la configuración de rutas LLM a GPT-5.5

set -e

echo "=========================================="
echo "🚀 Actualización a GPT-5.5"
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
echo "1) GPT-5.5 (Reasoning) - Razonamiento avanzado [RECOMENDADO]"
echo "2) GPT-5.4 (anterior) - Rollback / menor costo"
echo "3) GPT-4o - Modelo legacy confiable"
echo "4) Mantener configuración actual"
echo ""
read -p "Selecciona una opción (1-4): " option

case $option in
    1)
        MODEL="gpt-5.5"
        echo "✅ Seleccionado: GPT-5.5 con reasoning_effort=medium"
        ;;
    2)
        MODEL="gpt-5.4"
        echo "✅ Seleccionado: GPT-5.4 con reasoning_effort=medium"
        ;;
    3)
        MODEL="gpt-4o"
        echo "✅ Seleccionado: GPT-4o"
        ;;
    4)
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

update_env_var() {
    local key="$1"
    local value="$2"

    if grep -q "^${key}=" .env; then
        sed -i.tmp "s/^${key}=.*/${key}=${value}/" .env
        rm -f .env.tmp
    else
        echo "${key}=${value}" >> .env
    fi
}

ROUTE_VARS=(
    LLM_ROUTE_DECOMPOSE
    LLM_ROUTE_REQUIRED_DATA
    LLM_ROUTE_GR_OUTCOME
    LLM_ROUTE_GR_RESPONSE
    LLM_ROUTE_KNOWLEDGE
)

for route_var in "${ROUTE_VARS[@]}"; do
    update_env_var "$route_var" "$MODEL"
done

echo "✅ Archivo .env actualizado"
echo ""
echo "📋 Nueva configuración:"
for route_var in "${ROUTE_VARS[@]}"; do
    echo "   $route_var=$MODEL"
done
echo ""
echo "ℹ️  OPENAI_MODEL queda como variable legacy; el runtime usa LLM_ROUTE_*."
echo "ℹ️  Para modelos GPT-5, el router aplica reasoning_effort=medium."
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
echo "📚 Para más información, consulta: QUICK_START.md"
