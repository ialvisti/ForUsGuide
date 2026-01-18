# Fase 2: Análisis y Diseño de Arquitectura

**Estado:** ✅ COMPLETADA  
**Duración:** 1-1.5 horas  
**Fecha:** 2026-01-18

---

## Objetivo

Analizar la estructura JSON de los artículos y diseñar la estrategia completa de chunking y arquitectura del sistema RAG.

---

## Contexto del Sistema Multi-Agente

### Flujo Completo

```
1. DevRev (CRM) → Ticket llega
2. n8n detecta temas/inquiries con IA
3. POR CADA INQUIRY:
   ├─ Llamada 1: KB API /required-data → "¿Qué datos necesito?"
   │  └─ n8n → AI Mapper → ForUsBots → Obtiene datos
   │
   └─ Llamada 2: KB API /generate-response + datos → "¿Cómo respondo?"
      └─ DevRev AI (~4000 tokens) → Respuesta final + acción
```

### Responsabilidades de KB API

**SÍ hace:**
- ✅ Devuelve qué datos necesita (lenguaje natural)
- ✅ Genera respuestas contextualizadas
- ✅ Incluye guardrails y warnings
- ✅ Respeta token budgets

**NO hace:**
- ❌ NO detecta inquiries (n8n lo hace)
- ❌ NO scrapea datos (ForUsBots lo hace)
- ❌ NO decide acciones CRM (DevRev AI lo hace)

---

## Análisis de Estructura JSON

### Artículo Ejemplo Analizado

```json
{
  "metadata": {
    "article_id": "lt_request_401k_termination_withdrawal_or_rollover",
    "title": "LT: How to Request a 401(k)...",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    "scope": "recordkeeper-specific",
    "tags": ["Withdrawal Request", "Distribution", "Taxes"]
  },
  "summary": {
    "purpose": "...",
    "topic": "distribution",
    "subtopics": [...],
    "required_data_summary": [...],
    "key_business_rules": [...],
    "key_steps_summary": [...],
    "high_impact_faq_pairs": [...],
    "plan_specific_guardrails": [...],
    "critical_flags": {...}
  },
  "details": {
    "business_rules": [...],
    "fees": [...],
    "steps": [...],
    "common_issues": [...],
    "examples": [...],
    "faq_pairs": [...],
    "definitions": [...],
    "guardrails": {...},
    "required_data": {...},
    "decision_guide": {...},
    "response_frames": {...},
    "references": {...}
  }
}
```

**Características:**
- 647 líneas, ~5000 palabras
- Estructura consistente entre 280 artículos
- Información rica y estructurada

---

## Decisiones Arquitectónicas Clave

### 1. Multi-Article Strategy

**Decisión:** Opción A - Filtrar por metadata ANTES de buscar

**Implementación:**
```python
filter = {
    "record_keeper": {"$eq": "LT Trust"},  # MANDATORY
    "plan_type": {"$eq": "401(k)"}         # MANDATORY
}
```

**Priorización de resultados:**
1. Exact match: RK + plan + topic + subtopic
2. Specific match: RK + plan + topic
3. General match: plan + topic (scope="general")
4. Fallback: topic only (con disclaimer)

---

### 2. Response Format

**Decisión:** Opción A - Response separado por topic/section

```json
{
  "response": {
    "sections": [
      {
        "topic": "rollover_process",
        "primary_source": "lt_rollover_process",
        "answer_components": [...],
        "steps": [...],
        "warnings": [...]
      }
    ]
  }
}
```

**Ventajas:**
- n8n puede procesar cada sección independientemente
- Mejor trazabilidad
- Más fácil testing y debugging

---

### 3. Token Budget Management

**Decisión:** Budget dinámico según número de inquiries

```python
1 inquiry  → 3000 tokens max por response
2 inquiries → 1500 tokens max por response
3 inquiries → 1200 tokens max por response
4 inquiries → 900 tokens max por response
```

n8n envía `max_response_tokens` en cada request.

---

### 4. Confidence Thresholds

```python
confidence >= 0.85 → "can_proceed" (alta confianza)
confidence 0.60-0.84 → "uncertain" (con disclaimers)
confidence < 0.60 → "out_of_scope" (escalar)
```

---

### 5. Disambiguation

**Decisión:** n8n debe clarificar topic ANTES de llamar KB API

KB API asume que el topic ya viene refinado.

---

## Estrategia de Chunking

### Principio de Diseño

**Multi-tier basado en importancia y uso:**

No todos los chunks son iguales. Algunos son críticos y siempre se necesitan, otros son opcionales.

### Tiers de Prioridad

```
TIER CRITICAL (siempre recuperar):
- required_data (para /required-data)
- decision_guide (para determinar outcome)
- response_frames (templates de respuesta)
- guardrails (qué NO decir)
- business_rules críticas (fees, eligibility, taxes)

TIER HIGH (recuperar si hay budget):
- steps (procedimientos detallados)
- fees_details (desglose de costos)
- common_issues (troubleshooting)
- examples (casos específicos)

TIER MEDIUM (útil pero no esencial):
- high_impact_faqs
- examples adicionales

TIER LOW (relleno):
- regular_faqs
- definitions
- additional_notes
- references
```

### Chunking por Endpoint

#### Para `/required-data`:
- `required_data` - Completo, sin dividir
- `eligibility` - Reglas de elegibilidad
- `critical_flags` - Flags especiales

#### Para `/generate-response`:
- Tier CRITICAL: decision_guide, response_frames, guardrails, business_rules
- Tier HIGH: steps, fees_details, common_issues, examples
- Tier MEDIUM: high_impact_faqs, scenarios
- Tier LOW: regular_faqs, definitions, notes, references

---

## Formato de Endpoints

### Endpoint 1: `/api/v1/required-data`

**Request:**
```json
{
  "inquiry": "Participant wants to rollover 401k to Fidelity",
  "topic": "rollover",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "related_inquiries": []
}
```

**Response:**
```json
{
  "article_reference": {
    "article_id": "...",
    "title": "...",
    "confidence": 0.95
  },
  "required_fields": {
    "participant_data": [
      {
        "field": "Current account balance",
        "description": "...",
        "why_needed": "...",
        "data_type": "currency",
        "required": true
      }
    ],
    "plan_data": [...]
  }
}
```

---

### Endpoint 2: `/api/v1/generate-response`

**Request:**
```json
{
  "inquiry": "...",
  "topic": "rollover",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "collected_data": {
    "participant_data": {...},
    "plan_data": {...}
  },
  "context": {
    "max_response_tokens": 1500,
    "total_inquiries_in_ticket": 2
  }
}
```

**Response:**
```json
{
  "decision": "can_proceed",
  "confidence": 0.97,
  "response": {
    "sections": [
      {
        "topic": "rollover_process",
        "answer_components": [...],
        "steps": [...],
        "warnings": [...]
      }
    ]
  },
  "guardrails": {
    "must_not_say": [...],
    "must_verify": [...]
  },
  "metadata": {
    "confidence": 0.97,
    "token_count": 487
  }
}
```

---

## Tickets de Ejemplo Analizados

### Ticket 1: Distribution Update (Complejidad ALTA)
```
"I thought I rolled over completely but still have $1,993.84 remaining. 
Want to rollover to Fidelity and close account."

Inquiries detectadas:
1. Why remaining balance after rollover?
2. How to rollover remaining to Fidelity?
3. How to close account?

Topics: rollover, account_balance, account_closure
```

**Nota:** ForUsBots solo trae estado actual, no histórico.

---

### Ticket 2: Password Reset (Complejidad BAJA)
```
"Error 10111 when resetting password"

Inquiry: How to resolve error 10111?
Topic: password_reset, technical_support
```

**Manejo:** Confidence bajo → out_of_scope → escalar a soporte técnico.

---

### Ticket 3: Loan Request (Complejidad MEDIA)
```
"How do I get a loan from my retirement?"

Inquiry: How to request loan?
Topic: loan_request
```

**Manejo:** Directo, si existe artículo.

---

## Metadata en Chunks

```json
{
  "id": "article_id_chunk_5",
  "content": "...",
  "metadata": {
    // Artículo
    "article_id": "...",
    "article_title": "...",
    "record_keeper": "LT Trust",      // ← Filtro crítico
    "plan_type": "401(k)",             // ← Filtro crítico
    "scope": "recordkeeper-specific",
    "tags": [...],
    "topic": "distribution",           // ← Routing
    "subtopics": [...],                // ← Matching
    
    // Chunk
    "chunk_type": "business_rules",    // ← Endpoint routing
    "chunk_category": "fees",          // ← Subcategoría
    "chunk_tier": "critical",          // ← Priorización
    "specific_topics": [...],          // ← Búsqueda
    "content_hash": "..."              // ← Deduplicación
  }
}
```

---

## Decisiones de Implementación

### 1. Embeddings Integrados

**Decisión:** Usar embeddings integrados de Pinecone

- Modelo: `llama-text-embed-v2`
- Field mapping: `text=content`
- Pinecone genera embeddings automáticamente
- NO enviamos vectores en upsert

---

### 2. Namespace Strategy

**Decisión:** Un namespace para todos los artículos

- Namespace: `kb_articles`
- Filtrado por metadata dentro del namespace

---

### 3. Reranking

**Decisión:** Usar reranking con `bge-reranker-v2-m3`

Flujo:
1. Búsqueda semántica → Top 20-30 chunks
2. Rerank → Top 5-10 chunks
3. Build context respetando budget

---

## Próximo Paso

**Fase 3:** Implementación del Sistema de Chunking

Ver: `PHASE_3.md`

---

**Tiempo total:** ~1 hora de diálogo + análisis  
**Siguiente fase:** PHASE_3.md
