# 🚀 Quick Start - KB RAG System

Guía rápida para poner en marcha el sistema completo en menos de 5 minutos.

---

## ⚡ Inicio Ultra Rápido (Si ya está configurado)

```bash
# Terminal 1: Iniciar API
cd kb-rag-system
source venv/bin/activate
bash scripts/start_api.sh

# Terminal 2: Iniciar UI
cd kb-rag-system/ui
bash start_ui.sh
```

Abre en tu navegador: **http://localhost:3000** 🎉

---

## 📋 Primera Vez - Setup Completo

### Paso 1: Verificar Requisitos

```bash
# Verificar Python 3.12+
python3 --version

# Si no tienes Python 3.12, instálalo:
# macOS: brew install python@3.12
# Linux: apt install python3.12
```

### Paso 2: Configurar el Proyecto

```bash
# Navegar al directorio
cd "kb-rag-system"

# Crear virtual environment
python3 -m venv venv

# Activar virtual environment
source venv/bin/activate  # En Windows: venv\Scripts\activate

# Instalar dependencias
pip install -r requirements.txt
```

### Paso 3: Configurar Variables de Entorno

```bash
# Crear archivo .env
cat > .env << 'EOF'
# Pinecone Configuration
PINECONE_API_KEY=your_pinecone_api_key_here
PINECONE_INDEX_NAME=kb-articles-production
PINECONE_NAMESPACE=kb_articles

# OpenAI Configuration
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.1

# API Configuration
API_KEY=your_secure_api_key_here
API_HOST=0.0.0.0
API_PORT=8000

# Logging
LOG_LEVEL=INFO
EOF

# IMPORTANTE: Edita el archivo .env con tus API keys reales
nano .env  # O usa tu editor favorito
```

### Paso 4: Crear Índice en Pinecone (Solo primera vez)

```bash
bash scripts/setup_index.sh
```

### Paso 5: Procesar Artículos (Opcional - ya hay 33 chunks de prueba)

```bash
# Procesar un artículo específico
python scripts/process_single_article.py "../Participant Advisory/Distributions/ARTICLE.json"

# O procesar todos los artículos (toma ~15 minutos)
python scripts/load_all_articles.py
```

### Paso 6: Iniciar el Sistema

```bash
# Terminal 1: API
bash scripts/start_api.sh

# Espera a ver: "🚀 API Ready on http://0.0.0.0:8000"
```

Abre una **nueva terminal**:

```bash
# Terminal 2: UI
cd kb-rag-system/ui
bash start_ui.sh

# Espera a ver: "📱 Open in browser: http://localhost:3000"
```

### Paso 7: Probar el Sistema

Abre tu navegador en: **http://localhost:3000**

1. **Verificar estado:** Click en "Verificar Estado"
2. **Configurar:** Ingresa tu API Key del archivo `.env`
3. **Probar Required Data:**
   - Inquiry: "I want to rollover my 401k to Fidelity"
   - Topic: "rollover"
   - Click "Enviar Consulta"
4. **Probar Generate Response:**
   - Inquiry: "How do I complete a rollover?"
   - Topic: "rollover"
   - Los datos ya están pre-cargados
   - Click "Generar Respuesta"

---

## 🎯 Checklist de Verificación

Antes de considerar que el sistema está funcionando correctamente:

- [ ] Python 3.12+ instalado
- [ ] Virtual environment activado
- [ ] Dependencias instaladas (`pip list` muestra fastapi, pinecone, openai)
- [ ] Archivo `.env` creado y editado con API keys reales
- [ ] Índice de Pinecone creado
- [ ] API corriendo en http://localhost:8000
- [ ] Health check responde: `curl http://localhost:8000/health`
- [ ] UI corriendo en http://localhost:3000
- [ ] UI muestra "Sistema Operacional" en verde
- [ ] Endpoint "Required Data" funciona
- [ ] Endpoint "Generate Response" funciona

---

## 🐛 Problemas Comunes

### 1. Error: "ModuleNotFoundError: No module named 'fastapi'"

**Solución:**
```bash
# Verifica que el virtual environment esté activado
which python  # Debe mostrar una ruta con 'venv'

# Si no está activado:
source venv/bin/activate

# Reinstala dependencias
pip install -r requirements.txt
```

### 2. Error: "PINECONE_API_KEY not found"

**Solución:**
```bash
# Verifica que .env existe y tiene las variables
cat .env

# Edita el archivo y agrega tus API keys reales
nano .env
```

### 3. Error: "Failed to connect to Pinecone"

**Solución:**
```bash
# Verifica tu API key de Pinecone
echo $PINECONE_API_KEY

# Verifica que el índice existe
# Ve a: https://app.pinecone.io/

# Si no existe, créalo:
bash scripts/setup_index.sh
```

### 4. UI muestra "Failed to fetch"

**Solución:**
```bash
# Verifica que la API esté corriendo
curl http://localhost:8000/health

# Si no responde, verifica logs
tail -f api_server.log

# Verifica la URL en la UI (debe ser http://localhost:8000)
```

### 5. Error: "Invalid API Key"

**Solución:**
```bash
# Copia el API_KEY del archivo .env
grep API_KEY .env

# Pégalo exactamente en el campo "API Key" de la UI
# (sin espacios extras ni comillas)
```

---

## 📊 Comandos Útiles

### Ver logs de la API

```bash
tail -f api_server.log
```

### Verificar estado del sistema

```bash
curl http://localhost:8000/health
```

### Ver estadísticas de Pinecone

```bash
python -c "
from data_pipeline.pinecone_uploader import PineconeUploader
uploader = PineconeUploader()
stats = uploader.get_index_stats()
print(f'Total vectors: {stats.get(\"total_vectors\", 0)}')
"
```

### Detener el sistema

```bash
# En cada terminal, presiona: Ctrl+C
```

### Reiniciar el sistema

```bash
# En Terminal 1:
bash scripts/start_api.sh

# En Terminal 2:
cd ui
bash start_ui.sh
```

---

## 🧪 Probar con cURL (Sin UI)

### Health Check

```bash
curl http://localhost:8000/health
```

### Required Data

```bash
curl -X POST http://localhost:8000/api/v1/required-data \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $(grep '^API_KEY=' .env | cut -d'=' -f2)" \
  -d '{
    "inquiry": "I want to rollover my 401k",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    "topic": "rollover"
  }' | python -m json.tool
```

### Generate Response

```bash
curl -X POST http://localhost:8000/api/v1/generate-response \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $(grep '^API_KEY=' .env | cut -d'=' -f2)" \
  -d '{
    "inquiry": "How do I complete a rollover?",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    "topic": "rollover",
    "collected_data": {
      "participant_data": {
        "current_balance": "$1,993.84",
        "employment_status": "Terminated"
      },
      "plan_data": {
        "rollover_method": "Direct rollover available"
      }
    },
    "max_response_tokens": 1500,
    "total_inquiries_in_ticket": 2
  }' | python -m json.tool
```

---

## 📚 Siguiente Paso

Una vez que el sistema esté funcionando:

1. **Explora la UI:** Prueba diferentes inquiries y parámetros
2. **Lee los ejemplos:** Revisa `ui/examples.json` para casos de uso
3. **Lee la documentación:** Ve a `Development Docs/PROJECT_COMPLETE.md`
4. **Procesa más artículos:** Carga los 279 artículos restantes
5. **Integra con n8n:** Sigue la guía de integración

---

## 🎉 ¡Listo!

Si llegaste hasta aquí y todo funciona, ¡felicitaciones! 🎊

Tu sistema KB RAG está completamente operacional y listo para:
- Responder consultas sobre 401(k)
- Determinar datos requeridos
- Generar respuestas contextualizadas
- Integrarse con n8n y DevRev

---

**¿Necesitas ayuda?**

- Ver logs: `tail -f api_server.log`
- Documentación completa: `README.md`
- Documentación UI: `ui/README.md`
- Troubleshooting: `Development Docs/DEPLOYMENT.md`

---

**Sistema listo y funcionando** ✅  
**Tiempo total de setup:** ~5 minutos  
**¡Ahora a construir cosas increíbles!** 🚀
