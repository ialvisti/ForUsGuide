# KB RAG System

Sistema RAG (Retrieval-Augmented Generation) para búsqueda y consulta de artículos de knowledge base de Participant Advisory sobre 401(k).

## 🚀 Quick Start

### 1. Iniciar el Sistema Completo

```bash
# Iniciar API (incluye UI integrada)
cd kb-rag-system
source venv/bin/activate
bash scripts/start_api.sh
```

Luego abre en tu navegador:
- **UI:** http://localhost:8000/ui
- **API Docs:** http://localhost:8000/docs
- **Health Check:** http://localhost:8000/health

### Producción (Render)
- **UI:** https://forusguide.onrender.com/ui
- **API Docs:** https://forusguide.onrender.com/docs

---

## 🏗️ Arquitectura

```
┌─────────────┐
│  Web UI     │ ← Interfaz visual minimalista
└──────┬──────┘
       │
       ↓
┌─────────────────────────────────────┐
│      FastAPI Endpoints              │
│  • POST /api/v1/required-data       │
│  • POST /api/v1/generate-response   │
│  • GET  /health                     │
└──────┬──────────────────────────────┘
       │
       ↓
┌─────────────────────────────────────┐
│         RAG Engine                  │
│  • Token management                 │
│  • Context selection                │
│  • Response generation              │
└──────┬──────────────┬───────────────┘
       │              │
       ↓              ↓
┌──────────┐    ┌────────────┐
│ Pinecone │    │   OpenAI   │
│ Vectors  │    │ GPT-4o-mini│
└──────────┘    └────────────┘
```

---

## 🎨 Interfaz de Usuario

La UI web minimalista permite interactuar fácilmente con ambos endpoints:

- ✅ **Diseño moderno y responsive**
- ✅ **Health check en tiempo real**
- ✅ **Formularios intuitivos para ambos endpoints**
- ✅ **Validación de JSON**
- ✅ **Copiar respuestas con un click**
- ✅ **Ejemplos pre-cargados**

Ver documentación completa en: [`ui/README.md`](ui/README.md)

---

## 📋 Requisitos

- Python 3.12+
- Pinecone API key
- OpenAI API key

---

## 🚀 Setup Detallado

### 1. Crear virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
```

### 2. Instalar dependencias:
```bash
pip install -r requirements.txt
```

### 3. Configurar variables de entorno:
```bash
cp .env.example .env
# Editar .env con tus API keys
```

Variables requeridas:
```env
PINECONE_API_KEY=your_pinecone_key
OPENAI_API_KEY=your_openai_key
API_KEY=your_api_key_for_auth
```

### 4. Crear índice en Pinecone:
```bash
bash scripts/setup_index.sh
```

### 5. Procesar artículos:
```bash
# Procesar un artículo
python scripts/process_single_article.py "../Participant Advisory/Distributions/ARTICLE.json"

# O procesar todos
python scripts/load_all_articles.py
```

---

## 🔧 Uso

### Opción 1: Usar la Web UI (Recomendado)

```bash
# Terminal 1: API
bash scripts/start_api.sh

# Terminal 2: UI
cd ui
bash start_ui.sh
```

Abre http://localhost:3000 y usa la interfaz visual.

### Opción 2: Usar cURL

**Health Check:**
```bash
curl http://localhost:8000/health
```

**Required Data:**
```bash
curl -X POST http://localhost:8000/api/v1/required-data \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "inquiry": "I want to rollover my 401k",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    "topic": "rollover"
  }'
```

**Generate Response:**
```bash
curl -X POST http://localhost:8000/api/v1/generate-response \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
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
  }'
```

---

## 📁 Estructura del Proyecto

```
kb-rag-system/
├── api/                    # FastAPI application
│   ├── main.py            # Endpoints principales
│   ├── models.py          # Pydantic models
│   ├── config.py          # Configuración
│   └── middleware.py      # Auth y logging
│
├── data_pipeline/          # Procesamiento de datos
│   ├── article_processor.py
│   ├── chunking.py        # Chunking multi-tier
│   ├── pinecone_uploader.py
│   ├── rag_engine.py      # Motor RAG
│   ├── prompts.py         # System prompts
│   └── token_manager.py   # Token budget
│
├── ui/                     # Interfaz web
│   ├── index.html         # UI standalone
│   ├── start_ui.sh        # Script de inicio
│   ├── examples.json      # Ejemplos de uso
│   └── README.md          # Documentación UI
│
├── scripts/                # Utility scripts
│   ├── start_api.sh       # Iniciar API
│   ├── process_single_article.py
│   ├── verify_article.py
│   └── test_api.py
│
├── tests/                  # Testing
│   ├── test_rag_engine.py
│   └── test_api.py
│
├── Development Docs/       # Documentación completa
│   ├── PROJECT_COMPLETE.md
│   ├── ARCHITECTURE.md
│   └── ...
│
├── requirements.txt        # Dependencias Python
├── Dockerfile             # Container production
└── README.md              # Este archivo
```

---

## 🧪 Testing

```bash
# Todos los tests
pytest tests/

# Tests específicos
pytest tests/test_rag_engine.py -v
pytest tests/test_api.py -v

# Coverage
pytest --cov=data_pipeline --cov=api tests/
```

---

## 📝 Documentación

### Documentación de la API

Una vez iniciado el servidor, visita:
- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

### Documentación del Proyecto

- **Start Here:** [`START_HERE.md`](START_HERE.md) - Punto de entrada
- **Project Complete:** [`Development Docs/PROJECT_COMPLETE.md`](Development%20Docs/PROJECT_COMPLETE.md) - Resumen completo
- **Architecture:** [`ARCHITECTURE.md`](ARCHITECTURE.md) - Arquitectura del sistema
- **Pipeline Guide:** [`PIPELINE_GUIDE.md`](PIPELINE_GUIDE.md) - Cómo procesar artículos
- **Deployment:** [`Development Docs/DEPLOYMENT.md`](Development%20Docs/DEPLOYMENT.md) - Guía de deployment
- **UI Documentation:** [`ui/README.md`](ui/README.md) - Documentación de la interfaz

---

## 🎯 Endpoints Disponibles

### 1. `/api/v1/required-data`

Determina qué datos se necesitan recolectar del participante y plan.

**Input:**
- `inquiry`: Consulta del participante
- `record_keeper`: Record keeper (ej: "LT Trust")
- `plan_type`: Tipo de plan (ej: "401(k)")
- `topic`: Tema (ej: "rollover")

**Output:**
- Lista de campos requeridos organizados por categoría
- Confidence score
- Metadata del procesamiento

### 2. `/api/v1/generate-response`

Genera respuesta contextualizada con los datos recolectados.

**Input:**
- Mismos campos que required-data +
- `collected_data`: Datos recolectados
- `max_response_tokens`: Límite de tokens (opcional)
- `total_inquiries_in_ticket`: Número de inquiries (opcional)

**Output:**
- Respuesta estructurada con steps, warnings, outcomes
- Guardrails aplicados
- Decision y confidence score
- Metadata

---

## 🚢 Deployment

### Docker

```bash
# Build
docker build -t kb-rag-system .

# Run
docker run -d -p 8000:8000 \
  --env-file .env \
  --name kb-rag-api \
  kb-rag-system
```

### Render / Railway / Fly.io

Ver guía completa en [`Development Docs/DEPLOYMENT.md`](Development%20Docs/DEPLOYMENT.md)

---

## 📊 Métricas del Sistema

- **Latencia:** 2-5 segundos por request
- **Accuracy:** 88% en tests
- **Costo:** ~$0.0016 USD por ticket (2 inquiries)
- **Escalabilidad:** ~600 tickets por $1 USD

---

## 🔐 Seguridad

- ✅ Autenticación con API Key (`X-API-Key` header)
- ✅ Validación de requests con Pydantic
- ✅ Error handling robusto
- ✅ Logging seguro
- ✅ CORS configurado

---

## 🐛 Troubleshooting

### API no inicia

```bash
# Verificar dependencias
pip install -r requirements.txt

# Verificar .env
cat .env

# Ver logs
tail -f api_server.log
```

### UI no se conecta

```bash
# Verificar que API esté corriendo
curl http://localhost:8000/health

# Verificar CORS en api/main.py
# allow_origins debe incluir "http://localhost:3000"
```

### Pinecone no conecta

```bash
# Verificar API key
echo $PINECONE_API_KEY

# Verificar índice existe
python scripts/verify_article.py
```

---

## 🎓 Aprende Más

- [Documentación de Pinecone](https://docs.pinecone.io/)
- [Documentación de OpenAI](https://platform.openai.com/docs)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)

---

## ✅ Estado del Proyecto

**Versión:** 1.0  
**Estado:** ✅ Production-Ready  
**Última actualización:** 2026-01-18  
**Test Coverage:** 88%  

### Funcionalidades Completadas

- [x] Pipeline de procesamiento de artículos
- [x] Chunking multi-tier inteligente
- [x] Vector database con Pinecone
- [x] RAG engine con token management
- [x] API REST con 2 endpoints
- [x] Autenticación y seguridad
- [x] Testing automatizado
- [x] **Interfaz web minimalista** ✨ NEW!
- [x] Docker containerization
- [x] Documentación exhaustiva

---

**Desarrollado para:** Participant Advisory 401(k) Knowledge Base  
**Tecnologías:** Python 3.12, FastAPI, Pinecone, OpenAI GPT-4o-mini, HTML/CSS/JS
