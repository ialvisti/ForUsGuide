#!/bin/bash
# Script para iniciar la API del sistema RAG

# Colores para output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}  KB RAG System API Startup${NC}"
echo -e "${BLUE}================================${NC}\n"

# Verificar que estamos en el directorio correcto
if [ ! -f ".env" ]; then
    echo -e "${RED}❌ Error: .env file not found${NC}"
    echo -e "${YELLOW}Run this script from kb-rag-system directory${NC}"
    exit 1
fi

# Activar virtual environment
if [ -d "venv" ]; then
    echo -e "${GREEN}🔌 Activating virtual environment...${NC}"
    source venv/bin/activate
else
    echo -e "${RED}❌ Error: venv not found${NC}"
    echo -e "${YELLOW}Create one with: python3 -m venv venv${NC}"
    exit 1
fi

# Mostrar configuración de modelo
echo -e "\n${BLUE}📋 Configuration:${NC}"
echo -e "  Model: ${GREEN}$(grep OPENAI_MODEL .env | cut -d '=' -f2)${NC}"
echo -e "  Reasoning: ${GREEN}$(grep OPENAI_REASONING_EFFORT .env | cut -d '=' -f2)${NC}"
echo -e "  Port: ${GREEN}$(grep API_PORT .env | cut -d '=' -f2)${NC}\n"

# Iniciar servidor
echo -e "${GREEN}🚀 Starting API server...${NC}\n"

cd api && python main.py
