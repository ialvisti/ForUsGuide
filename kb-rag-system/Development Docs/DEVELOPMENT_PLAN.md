# Plan de Desarrollo Completo - KB RAG System

## 📋 Índice

1. [Visión General](#visión-general)
2. [Contexto del Proyecto](#contexto-del-proyecto)
3. [Fases del Desarrollo](#fases-del-desarrollo)
4. [Estado Actual](#estado-actual)
5. [Próximos Pasos](#próximos-pasos)
6. [Arquitectura Final](#arquitectura-final)
7. [Decisiones Técnicas Clave](#decisiones-técnicas-clave)

---

## Visión General

### ¿Qué Estamos Construyendo?

Un **sistema RAG (Retrieval-Augmented Generation)** operacional para responder consultas sobre artículos de Knowledge Base de 401(k) Participant Advisory. NO es un RAG tradicional de Q&A, sino parte de un **sistema multi-agente** que incluye:

- DevRev (CRM)
- n8n (Orquestador)
- KB RAG System (este proyecto)
- ForUsBots (RPA/scraping)
- DevRev AI (generador final)

### Objetivo del Sistema

**Dos funcionalidades críticas:**

1. **GET Required Data** (`/api/v1/required-data`): Identificar qué datos del participante necesitamos para responder una consulta
2. **Generate Response** (`/api/v1/generate-response`): Generar respuesta contextualizada una vez que tenemos los datos

### Stack Tecnológico

```
Backend: Python 3.12+ / FastAPI
Vector DB: Pinecone (serverless, AWS us-east-1)
LLM: OpenAI GPT-4o-mini
Embeddings: llama-text-embed-v2 (integrado en Pinecone)
Deploy: Render (web service)
```

---

## Contexto del Proyecto

### Estructura de Datos

**Artículos JSON** con estructura consistente:
- ~280 artículos total
- Estructura: `metadata`, `summary`, `details`
- Ubicación: `Participant Advisory/` (Distributions, Loans, etc.)

**Ejemplo de metadata:**
```json
{
  "article_id": "lt_request_401k_termination_withdrawal_or_rollover",
  "title": "LT: How to Request a 401(k) Termination...",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "scope": "recordkeeper-specific"
}
```

### Flujo Multi-Agente

```
Ticket (DevRev) 
  → n8n detecta inquiries
  → KB API /required-data (por inquiry)
  → n8n mergea campos
  → AI Mapper traduce a campos ForUsBots
  → ForUsBots scrapea portal
  → KB API /generate-response (por inquiry, con datos)
  → n8n empaqueta responses
  → DevRev AI genera respuesta final + acción
```

**Importante:**
- KB API NO detecta inquiries (n8n lo hace)
- KB API NO scrapea datos (ForUsBots lo hace)
- KB API NO decide acciones CRM (DevRev AI lo hace)

### Token Budget

- DevRev AI tiene límite de **~4000 tokens total** por ticket
- Si hay 2 inquiries → ~1500 tokens max por response
- Si hay 3 inquiries → ~1200 tokens max por response
- n8n envía `max_response_tokens` en cada request

---

## Fases del Desarrollo

### Fase 1: Setup & Foundation ✅ COMPLETADA

**Duración:** 30-40 minutos  
**Objetivo:** Ambiente de desarrollo listo  
**Ver:** `PHASE_1.md` para detalles completos

**Logros:**
- ✅ Python 3.13.0 verificado
- ✅ Virtual environment creado
- ✅ Dependencias instaladas (Pinecone, OpenAI, FastAPI, etc.)
- ✅ Estructura de proyecto creada
- ✅ `.env` configurado con API keys
- ✅ Archivos de configuración

---

### Fase 2: Análisis y Diseño ✅ COMPLETADA

**Duración:** 1-1.5 horas  
**Objetivo:** Definir estrategia de chunking y arquitectura  
**Ver:** `PHASE_2.md` para detalles completos

**Logros:**
- ✅ Estructura JSON analizada a fondo
- ✅ Estrategia de chunking multi-tier diseñada
- ✅ Decisiones arquitectónicas tomadas:
  - Filtrado por metadata ANTES de búsqueda
  - Response separado por topic
  - n8n clarifica topic antes de llamar KB API
  - Token budget dinámico
  - Confidence thresholds definidos
- ✅ Formato de endpoints `/required-data` y `/generate-response` definido

---

### Fase 3: Implementación de Chunking ✅ COMPLETADA

**Duración:** 2-2.5 horas  
**Objetivo:** Sistema de chunking funcional  
**Ver:** `PHASE_3.md` para detalles completos

**Logros:**
- ✅ `article_processor.py` - Carga y valida artículos JSON
- ✅ `chunking.py` - Genera chunks semánticos con metadata
- ✅ Estrategia multi-tier implementada (Critical, High, Medium, Low)
- ✅ 33 chunks generados del artículo de prueba
- ✅ Scripts de testing (`test_chunking.py`, `show_chunk_examples.py`)

**Resultado:**
- 9 chunks CRITICAL
- 10 chunks HIGH
- 5 chunks MEDIUM
- 9 chunks LOW

---

### Fase 4: Pinecone & Pipeline 🔄 EN PROGRESO

**Duración estimada:** 1.5-2 horas  
**Objetivo:** Índice en Pinecone + pipeline de carga  
**Ver:** `PHASE_4.md` para detalles completos

**Estado Actual:**
- ✅ Índice creado en Pinecone (`kb-articles-production`)
- ✅ Scripts creados:
  - `setup_index.sh` - Crear índice
  - `pinecone_uploader.py` - Módulo de carga
  - `process_single_article.py` - Procesar 1 artículo
  - `verify_article.py` - Verificar artículo
- ❌ **PROBLEMA ENCONTRADO:** Error al subir chunks
  - Error: "Vector dimension 0 does not match the dimension of the index 1024"
  - Causa: Formato incorrecto en upsert para índice con embeddings integrados
  - **Solución:** Ver PHASE_4.md sección "Problema Actual y Solución"

**Próximo paso:** Corregir `pinecone_uploader.py` para usar formato correcto con embeddings integrados

---

### Fase 5: RAG Engine ⏳ PENDIENTE

**Duración estimada:** 1.5-2 horas  
**Objetivo:** Lógica de búsqueda y generación de respuestas  
**Ver:** `PHASE_5.md` para plan detallado

**Componentes a implementar:**
- Búsqueda semántica en Pinecone
- Reranking con bge-reranker-v2-m3
- Construcción de context respetando token budget
- Integración con OpenAI GPT-4o-mini
- Prompt engineering para ambos endpoints
- Manejo de confidence scores

---

### Fase 6: FastAPI Endpoints ⏳ PENDIENTE

**Duración estimada:** 1.5-2 horas  
**Objetivo:** API REST production-ready

**Componentes:**
- FastAPI app con endpoints
- Autenticación con API keys
- Validación de requests (Pydantic)
- Error handling robusto
- Logging estructurado
- Health checks
- Documentación Swagger

---

### Fase 7: Production Hardening ⏳ PENDIENTE

**Duración estimada:** 1-1.5 horas  
**Objetivo:** Sistema listo para producción

**Componentes:**
- Testing (unit + integration)
- Monitoring y métricas
- Rate limiting
- Retry logic
- Dockerfile
- Deploy a Render

---

## Estado Actual

### ✅ Completado

```
kb-rag-system/
├── .env                           # ✅ Configurado con API keys
├── requirements.txt               # ✅ Todas las dependencias
├── venv/                          # ✅ Virtual environment
├── README.md                      # ✅ Documentación general
├── ARCHITECTURE.md                # ✅ Arquitectura completa (ES)
├── ARCHITECTURE_EN.md             # ✅ Arquitectura completa (EN)
├── PIPELINE_GUIDE.md              # ✅ Guía de procesamiento
├── DEVELOPMENT_PLAN.md            # ✅ Este archivo
├── PHASE_1.md                     # ✅ Fase 1 detallada
├── PHASE_2.md                     # ✅ Fase 2 detallada
├── PHASE_3.md                     # ✅ Fase 3 detallada
├── PHASE_4.md                     # ✅ Fase 4 detallada (con solución)
├── data_pipeline/
│   ├── __init__.py                # ✅
│   ├── article_processor.py       # ✅ Funcional
│   ├── chunking.py                # ✅ Funcional (33 chunks)
│   └── pinecone_uploader.py       # ⚠️  Necesita corrección (ver PHASE_4.md)
├── api/
│   └── __init__.py                # ✅
├── scripts/
│   ├── __init__.py                # ✅
│   ├── setup_index.sh             # ⚠️  Necesita corrección (ver PHASE_4.md)
│   ├── process_single_article.py  # ⚠️  Funcional (depende de uploader)
│   ├── verify_article.py          # ✅ Funcional
│   ├── test_chunking.py           # ✅ Funcional
│   └── show_chunk_examples.py     # ✅ Funcional
└── tests/
    └── __init__.py                # ✅
```

### ⚠️ Problema Actual (Fase 4)

**Error al subir chunks a Pinecone:**
```
Vector dimension 0 does not match the dimension of the index 1024
```

**Causa:**
Los índices de Pinecone con embeddings integrados (usando `--model` y `--field-map`) requieren un formato especial de upsert que NO incluye vectores explícitos.

**Solución detallada:** Ver `PHASE_4.md` sección "Problema Actual y Solución"

---

## Próximos Pasos

### Inmediatos (Continuar Fase 4)

1. **Corregir `pinecone_uploader.py`**
   - Cambiar método `_upload_batch` para usar formato correcto
   - Ver código exacto en `PHASE_4.md`

2. **Probar upload de artículo**
   ```bash
   python scripts/process_single_article.py "../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
   ```

3. **Verificar artículo en Pinecone**
   ```bash
   python scripts/verify_article.py "lt_request_401k_termination_withdrawal_or_rollover"
   ```

4. **Crear scripts adicionales del pipeline**
   - `load_all_articles.py` - Procesar todos los artículos
   - `delete_article_chunks.py` - Eliminar artículo
   - `update_article.py` - Actualizar artículo existente

### Después de Fase 4

5. **Implementar RAG Engine (Fase 5)**
   - Ver `PHASE_5.md` para plan completo
   - Búsqueda + reranking + LLM
   - Prompt engineering para ambos modos

6. **Crear API Endpoints (Fase 6)**
   - FastAPI con `/required-data` y `/generate-response`
   - Autenticación, validación, error handling

7. **Production Hardening (Fase 7)**
   - Testing, monitoring, deploy

---

## Arquitectura Final

### Componentes del Sistema

```
┌─────────────────────────────────────────┐
│         FastAPI Application             │
│                                         │
│  POST /api/v1/required-data             │
│  POST /api/v1/generate-response         │
│  GET  /api/v1/health                    │
│                                         │
│  ┌───────────────────────────────┐     │
│  │      RAG Engine               │     │
│  │                               │     │
│  │  1. Filter by metadata        │     │
│  │  2. Search Pinecone           │     │
│  │  3. Rerank results            │     │
│  │  4. Build context (budget)    │     │
│  │  5. Call OpenAI               │     │
│  │  6. Structure response        │     │
│  └───────────────────────────────┘     │
└──────────────┬──────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│    Pinecone Vector Database              │
│                                          │
│  Index: kb-articles-production           │
│  Namespace: kb_articles                  │
│  Model: llama-text-embed-v2              │
│  Dimension: 1024                         │
│  Metric: cosine                          │
│                                          │
│  ~280 articles × ~33 chunks = ~9,240     │
└──────────────────────────────────────────┘
```

### Flujo de Datos por Endpoint

#### Endpoint 1: `/required-data`

```
Request (n8n) → FastAPI
  ↓
Validate request (Pydantic)
  ↓
RAG Engine:
  1. Filter: record_keeper + plan_type + chunk_type="required_data"
  2. Search: Top 5-10 chunks
  3. Rerank: Top 3-5
  4. Build context
  5. LLM: "Extract required fields from context"
  ↓
Parse response
  ↓
Return JSON: {required_fields: {participant_data, plan_data}}
```

#### Endpoint 2: `/generate-response`

```
Request (n8n + collected_data) → FastAPI
  ↓
Validate request
  ↓
Determine token budget (based on total_inquiries)
  ↓
RAG Engine:
  1. Filter: record_keeper + plan_type + topic
  2. Search: Top 20-30 chunks
  3. Retrieve by tier until budget filled:
     - CRITICAL: always
     - HIGH: if space
     - MEDIUM/LOW: if space left
  4. Rerank retrieved chunks
  5. Build optimized context
  6. LLM: "Generate response with guardrails"
  ↓
Parse and structure response
  ↓
Return JSON: {
  decision, confidence, response, 
  guardrails, metadata
}
```

---

## Decisiones Técnicas Clave

### 1. Chunking Strategy

**Multi-tier basado en importancia:**
- CRITICAL (9): Siempre se recupera
- HIGH (10): Se recupera si hay budget
- MEDIUM (5): Opcional
- LOW (9): Solo si sobra espacio

**Ventaja:** Respeta límites de tokens sin sacrificar información crítica

### 2. Metadata Filtering

**MANDATORY filters (antes de búsqueda):**
- `record_keeper` (LT Trust, Vanguard, etc.)
- `plan_type` (401(k), 403(b), etc.)

**Ventaja:** Evita contaminación entre recordkeepers

### 3. Multi-Article Strategy

**Opción A: Filtrar por metadata ANTES de buscar**
- Solo busca en artículos del recordkeeper correcto
- Evita confusión entre diferentes providers

**Priorización:**
1. Exact match (RK + plan + topic + subtopic)
2. Specific match (RK + plan + topic)
3. General match (plan + topic, scope="general")
4. Fallback (topic only, con disclaimer)

### 4. Response Format

**Por topic/section (no unificado):**
```json
{
  "response": {
    "sections": [
      {
        "topic": "rollover_process",
        "answer_components": [...],
        "steps": [...],
        "warnings": [...]
      }
    ]
  }
}
```

**Ventaja:** n8n puede procesar cada sección independientemente

### 5. Token Budget Management

**Dinámico según número de inquiries:**
```python
1 inquiry  → 3000 tokens max
2 inquiries → 1500 tokens max
3 inquiries → 1200 tokens max
4 inquiries → 900 tokens max
```

n8n envía `max_response_tokens` en cada request.

### 6. Confidence Thresholds

```
>= 0.85 → "can_proceed" (alta confianza)
0.60-0.84 → "uncertain" (con disclaimers)
< 0.60 → "out_of_scope" (recomendar escalamiento)
```

### 7. Embeddings Integrados

**Pinecone genera embeddings automáticamente:**
- Modelo: `llama-text-embed-v2`
- Field mapping: `text=content`
- NO enviamos vectores en upsert
- Pinecone lee `content` de metadata y genera embeddings

---

## Recursos y Referencias

### Documentación Creada

1. `ARCHITECTURE.md` / `ARCHITECTURE_EN.md` - Arquitectura completa
2. `PIPELINE_GUIDE.md` - Guía de procesamiento de artículos
3. `PHASE_1.md` - Fase 1 detallada
4. `PHASE_2.md` - Fase 2 detallada
5. `PHASE_3.md` - Fase 3 detallada
6. `PHASE_4.md` - Fase 4 detallada + solución al problema actual
7. `PHASE_5.md` - Plan completo para Fase 5 (RAG Engine)

### Comandos Útiles

```bash
# Activar venv
cd kb-rag-system
source venv/bin/activate

# Procesar un artículo
python scripts/process_single_article.py "<path-to-json>"

# Verificar artículo
python scripts/verify_article.py "<article_id>"

# Ver chunks generados (dry-run)
python scripts/process_single_article.py "<path>" --dry-run --show-chunks

# Recrear índice
bash scripts/setup_index.sh
```

### Variables de Entorno (`.env`)

```bash
PINECONE_API_KEY=<tu-key>
OPENAI_API_KEY=<tu-key>
INDEX_NAME=kb-articles-production
NAMESPACE=kb_articles
BATCH_SIZE=96
MAX_RETRIES=3
```

---

## Notas Importantes

### Para Continuar en Otro Chat

1. **Lee primero:** `PHASE_4.md` - Contiene el problema actual y su solución exacta
2. **Estado:** Índice creado, chunks generan correctamente, falta corregir upload
3. **Próximo paso:** Aplicar la corrección en `pinecone_uploader.py` (código exacto en PHASE_4.md)
4. **Después:** Completar scripts del pipeline y pasar a Fase 5

### Contexto Crítico

- **NO es un RAG tradicional** - Es operacional, parte de multi-agente
- **Dos endpoints distintos** - required-data y generate-response
- **Token budget dinámico** - Varía según número de inquiries
- **Embeddings integrados** - Pinecone los genera, no los enviamos
- **Filtrado por metadata** - ANTES de búsqueda semántica

### Artículos de Prueba

```
Principal (usado para testing):
../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json

Otros disponibles:
../Participant Advisory/Distributions/LT: Completing Your Rollover Online – Best Practices.json
../Participant Advisory/Distributions/Distribution Requests for Small Account Balances.json
```

---

**Última actualización:** 2026-01-18  
**Fase actual:** 4 (Pinecone & Pipeline) - 80% completada  
**Problema actual:** Upload a Pinecone (solución en PHASE_4.md)  
**Próxima fase:** 5 (RAG Engine)
