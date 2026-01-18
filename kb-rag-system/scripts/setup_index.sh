#!/bin/bash

##############################################################################
# Script para crear el índice de Pinecone
#
# Este script crea un índice con embeddings integrados usando llama-text-embed-v2
#
# Uso:
#   bash scripts/setup_index.sh
##############################################################################

set -e  # Exit on error

echo "=========================================="
echo "PINECONE INDEX SETUP"
echo "=========================================="
echo ""

# Cargar variables de entorno
if [ -f "../.env" ]; then
    export $(cat ../.env | grep -v '^#' | xargs)
    echo "✅ Variables de entorno cargadas desde .env"
elif [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
    echo "✅ Variables de entorno cargadas desde .env"
else
    echo "⚠️  Archivo .env no encontrado, usando valores por defecto"
fi

# Verificar que PINECONE_API_KEY está definida
if [ -z "$PINECONE_API_KEY" ]; then
    echo "❌ ERROR: PINECONE_API_KEY no está definida"
    echo "   Por favor configura tu API key en el archivo .env"
    exit 1
fi

# Exportar API key para el CLI
export PINECONE_API_KEY

# Configuración del índice
INDEX_NAME="${INDEX_NAME:-kb-articles-production}"
METRIC="${METRIC:-cosine}"
CLOUD="${CLOUD:-aws}"
REGION="${REGION:-us-east-1}"
MODEL="${MODEL:-llama-text-embed-v2}"
FIELD_MAP="${FIELD_MAP:-text=content}"

echo ""
echo "Configuración del Índice:"
echo "  Nombre: $INDEX_NAME"
echo "  Métrica: $METRIC"
echo "  Cloud: $CLOUD"
echo "  Región: $REGION"
echo "  Modelo: $MODEL"
echo "  Field mapping: $FIELD_MAP"
echo ""

# Verificar si el índice ya existe
echo "🔍 Verificando si el índice ya existe..."
if pc index list | grep -q "$INDEX_NAME"; then
    echo "⚠️  El índice '$INDEX_NAME' ya existe"
    echo ""
    echo "Opciones:"
    echo "  1) Mantener índice existente (salir)"
    echo "  2) Eliminar y recrear"
    echo ""
    read -p "Selecciona una opción (1-2): " option
    
    case $option in
        1)
            echo "✅ Manteniendo índice existente"
            echo ""
            pc index describe --name "$INDEX_NAME"
            exit 0
            ;;
        2)
            echo "🗑️  Eliminando índice existente..."
            pc index delete --name "$INDEX_NAME"
            echo "✅ Índice eliminado"
            sleep 2
            ;;
        *)
            echo "❌ Opción inválida"
            exit 1
            ;;
    esac
fi

# Crear índice
echo ""
echo "🏗️  Creando índice en Pinecone..."
echo ""

pc index create \
    --name "$INDEX_NAME" \
    --metric "$METRIC" \
    --cloud "$CLOUD" \
    --region "$REGION" \
    --model "$MODEL" \
    --field-map "$FIELD_MAP"

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Índice creado exitosamente"
else
    echo ""
    echo "❌ Error al crear índice"
    exit 1
fi

# Esperar a que el índice esté ready
echo ""
echo "⏳ Esperando a que el índice esté listo..."
sleep 5

# Verificar índice
echo ""
echo "🔍 Verificando índice..."
echo ""
pc index describe --name "$INDEX_NAME"

echo ""
echo "=========================================="
echo "✅ SETUP COMPLETADO"
echo "=========================================="
echo ""
echo "Próximos pasos:"
echo "  1. Procesar artículos: python scripts/process_single_article.py <path>"
echo "  2. Verificar carga: python scripts/verify_article.py <article_id>"
echo ""
