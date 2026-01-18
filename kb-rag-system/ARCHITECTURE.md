# Arquitectura del Sistema RAG - Knowledge Base API

## 📋 Tabla de Contenidos

1. [Introducción](#introducción)
2. [¿Qué es un Sistema RAG?](#qué-es-un-sistema-rag)
3. [Arquitectura General](#arquitectura-general)
4. [Chunking: El Corazón del Sistema](#chunking-el-corazón-del-sistema)
5. [Metadata y Filtrado](#metadata-y-filtrado)
6. [Endpoints de la API](#endpoints-de-la-api)
7. [Flujo de Datos Completo](#flujo-de-datos-completo)
8. [Integración con el Sistema Multi-Agente](#integración-con-el-sistema-multi-agente)
9. [Consideraciones de Producción](#consideraciones-de-producción)

---

## Introducción

El **KB RAG System** es un sistema de Retrieval-Augmented Generation diseñado específicamente para responder consultas sobre artículos de Knowledge Base de 401(k) Participant Advisory. No es un RAG tradicional de Q&A, sino un **RAG operacional** que forma parte de un sistema multi-agente complejo.

### Objetivo Principal

Proporcionar dos funcionalidades críticas:
1. **Identificar qué datos se necesitan** del participante para responder una consulta
2. **Generar respuestas contextualizadas** una vez que se tienen los datos necesarios

### Casos de Uso

- Responder tickets de soporte de participantes de planes 401(k)
- Automatizar la recolección de información necesaria
- Proveer respuestas consistentes y compliance-ready
- Soportar múltiples recordkeepers (LT Trust, Vanguard, etc.)
- Manejar múltiples inquiries en un solo ticket

---

## ¿Qué es un Sistema RAG?

### RAG = Retrieval-Augmented Generation

Un sistema RAG combina dos componentes:

1. **Retrieval (Recuperación):** Busca información relevante en una base de datos vectorial
2. **Generation (Generación):** Usa un LLM para generar respuestas basadas en la información recuperada

### ¿Por qué RAG y no solo un LLM?

| Sin RAG (Solo LLM) | Con RAG |
|-------------------|---------|
| ❌ Información desactualizada (entrenamiento hasta fecha X) | ✅ Información siempre actualizada (KB en tiempo real) |
| ❌ Alucinaciones (inventa información) | ✅ Respuestas basadas en fuentes verificadas |
| ❌ No puede acceder a información específica de la empresa | ✅ Acceso a KB propietaria |
| ❌ Inconsistente entre respuestas | ✅ Consistente (misma fuente → misma respuesta) |
| ❌ No tiene contexto de compliance | ✅ Incluye guardrails y políticas |

### Analogía

**Sin RAG:** Es como preguntarle a alguien sobre un libro que leyó hace meses (memoria limitada, puede confundir detalles)

**Con RAG:** Es como darle el libro abierto en las páginas relevantes y pedirle que responda basándose en esas páginas específicas (información precisa y verificable)

---

## Arquitectura General

### Componentes del Sistema

```
┌─────────────────────────────────────────────────────────────┐
│                    DevRev (CRM)                              │
│                  Tickets de Participantes                    │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                   n8n (Orquestador)                          │
│  • Detecta inquiries en ticket                              │
│  • Determina topics                                          │
│  • Llama KB API (2 veces por inquiry)                       │
│  • Mergea respuestas                                         │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│               KB RAG System (ESTE PROYECTO)                  │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  POST /api/v1/required-data                        │    │
│  │  • Input: inquiry + topic                          │    │
│  │  • Output: campos necesarios (lenguaje natural)    │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  POST /api/v1/generate-response                    │    │
│  │  • Input: inquiry + topic + collected_data         │    │
│  │  • Output: respuesta + guardrails + warnings       │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │           RAG Engine (Lógica Core)                 │    │
│  │  1. Filtra por metadata (record_keeper, plan)     │    │
│  │  2. Busca chunks en Pinecone (semántica)           │    │
│  │  3. Rerank con bge-reranker-v2-m3                  │    │
│  │  4. Construye context (respeta token budget)       │    │
│  │  5. Llama OpenAI GPT-4o-mini                       │    │
│  │  6. Parsea y estructura respuesta                  │    │
│  └────────────────────────────────────────────────────┘    │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              Pinecone Vector Database                        │
│                                                              │
│  • ~280 artículos × ~30 chunks = ~8,400 vectores            │
│  • Embeddings: llama-text-embed-v2 (integrados)             │
│  • Metadata enriquecida para filtrado                        │
│  • Namespace: kb_articles                                    │
└──────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                  ForUsBots (RPA)                             │
│  • Recibe lista de campos necesarios                         │
│  • Scrapea portal del participante                           │
│  • Devuelve datos a n8n                                      │
└──────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│               DevRev AI (Generador Final)                    │
│  • Recibe respuestas de KB API (mergeadas)                  │
│  • Genera respuesta final al participante                    │
│  • Decide acción en ticket (cerrar, escalar, etc.)          │
└──────────────────────────────────────────────────────────────┘
```

---

## Chunking: El Corazón del Sistema

### ¿Qué es Chunking?

**Chunking** es el proceso de dividir un documento grande en fragmentos (chunks) más pequeños y semánticamente coherentes.

### ¿Por Qué es Necesario?

#### Problema sin Chunking

Imagina que tienes un artículo de 5,000 palabras sobre "Cómo Solicitar una Distribución 401(k)":

- **Búsqueda imprecisa:** Si buscas "¿Cuánto cuesta?", el sistema devuelve TODO el artículo
- **Desperdicio de tokens:** El LLM recibe información irrelevante (pasos, FAQs, etc.) cuando solo necesita la sección de fees
- **Menor calidad:** El LLM se "distrae" con información no relevante
- **Ineficiente:** Pagas por procesar miles de tokens innecesarios

#### Solución con Chunking

El mismo artículo dividido en ~33 chunks específicos:

- **Chunk 1:** Required data (campos necesarios)
- **Chunk 2:** Eligibility rules (reglas de elegibilidad)
- **Chunk 3:** Fees details (detalles de costos)
- **Chunk 4:** Steps 1-3 (primeros pasos)
- **Chunk 5:** Steps 4-6 (pasos intermedios)
- ... y así sucesivamente

**Resultado:**
- ✅ Búsqueda precisa: "¿Cuánto cuesta?" → Solo devuelve Chunk 3 (fees)
- ✅ Eficiencia: LLM recibe solo 200 palabras en vez de 5,000
- ✅ Mayor calidad: Respuesta enfocada y precisa
- ✅ Menor costo: 95% menos tokens procesados

### Estrategia de Chunking Implementada

Nuestro sistema usa una estrategia **multi-tier basada en uso**:

#### Principio de Diseño

No todos los chunks son iguales. Algunos son **críticos** y siempre se necesitan, otros son **opcionales** y solo se incluyen si hay espacio.

#### Tiers de Prioridad

```
┌──────────────────────────────────────────────────────────┐
│  TIER CRITICAL (9 chunks)                                │
│  Siempre se recuperan, sin importar el token budget     │
│  ------------------------------------------------        │
│  • required_data (para /required-data)                  │
│  • decision_guide (para determinar outcome)             │
│  • response_frames (templates de respuesta)             │
│  • guardrails (qué NO decir)                            │
│  • business_rules críticas (fees, eligibility, taxes)   │
└──────────────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│  TIER HIGH (10 chunks)                                   │
│  Se recuperan si hay token budget disponible            │
│  ------------------------------------------------        │
│  • steps (procedimientos detallados)                    │
│  • fees_details (desglose de costos)                    │
│  • common_issues (troubleshooting)                      │
│  • examples (casos de uso específicos)                  │
└──────────────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│  TIER MEDIUM (5 chunks)                                  │
│  Información útil pero no esencial                      │
│  ------------------------------------------------        │
│  • high_impact_faqs (preguntas frecuentes top)          │
│  • examples (escenarios adicionales)                    │
└──────────────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────┐
│  TIER LOW (9 chunks)                                     │
│  Información de relleno, solo si sobra mucho espacio    │
│  ------------------------------------------------        │
│  • regular_faqs (preguntas frecuentes)                  │
│  • definitions (glosario de términos)                   │
│  • additional_notes (notas complementarias)             │
│  • references (links y contactos)                       │
└──────────────────────────────────────────────────────────┘
```

### Tipos de Chunks por Endpoint

#### Para `/required-data` (Modo A)

**Objetivo:** Identificar qué datos necesitamos del participante

**Chunks recuperados:**
- `required_data` - Lista completa de campos (must_have, nice_to_have)
- `eligibility` - Reglas de elegibilidad para validar si procede
- `critical_flags` - Flags especiales (portal_required, etc.)

**Ejemplo de contenido de chunk:**

```markdown
# Required Data for This Process

## Must Have (Required):

### Confirmation participant has left employer
**Description:** The participant confirms they are separated from service
**Why needed:** Determines this is a termination distribution
**Data type:** message_text
**Example:** "I have left my employer and want to withdraw my 401(k)"

### Requested transaction type  
**Description:** Cash withdrawal, full rollover, or partial rollover + cash
**Why needed:** Determines portal options and delivery requirements
**Data type:** message_text
**Examples:** "Lump Sum Cash", "Full Rollover", "Partial Rollover + Cash"

### Email address for confirmations
**Description:** Valid email for confirmation updates
**Why needed:** Required for portal submission
**Data type:** agent_input

[... más campos ...]
```

#### Para `/generate-response` (Modo B)

**Objetivo:** Generar respuesta contextualizada con los datos recolectados

**Chunks recuperados (por tier, según budget):**

**Tier Critical:**
- `decision_guide` - Determina si puede proceder, está bloqueado, etc.
- `response_frames` - Templates de respuesta por outcome
- `guardrails` - Qué NO debe decir el agente
- `business_rules` - Reglas de fees, elegibilidad, taxes

**Tier High:**
- `steps` - Pasos detallados del procedimiento
- `fees_details` - Desglose completo de costos
- `common_issues` - Resolución de problemas comunes

**Tier Medium/Low:**
- `examples` - Casos de uso específicos
- `faqs` - Preguntas frecuentes
- `definitions` - Glosario

**Ejemplo de contenido de chunk:**

```markdown
# Response Frames by Outcome

## Outcome: can_proceed

### Message Components:
- You can request a termination distribution in the ForUsAll portal
- A $75 distribution fee applies to all requests
- An additional $35 wire fee applies if you choose wire transfer

### Next Steps:
- Log in to https://account.forusall.com/login
- Navigate to Loans & Distributions
- Select Separation of Service as reason

### Warnings:
- 20% federal withholding applies to cash distributions
- Wire fees are non-refundable

### Do NOT Say:
- Exact delivery date guarantees
- That wire fees can be refunded
- That unvested funds can be distributed
```

### Agrupación Semántica

Los chunks no se dividen arbitrariamente por tamaño, sino **semánticamente**:

#### ❌ Mal: División por Tamaño

```
Chunk 1: Primeros 500 caracteres del artículo
Chunk 2: Siguientes 500 caracteres
Chunk 3: Siguientes 500 caracteres
```

**Problema:** Un chunk puede empezar a mitad de una regla de negocio o paso, perdiendo contexto.

#### ✅ Bien: División Semántica

```
Chunk 1: Business Rules - Fees (completo)
Chunk 2: Business Rules - Eligibility (completo)
Chunk 3: Business Rules - Tax Withholding (completo)
Chunk 4: Steps 1-3 (procedimiento inicial completo)
Chunk 5: Steps 4-6 (procedimiento intermedio completo)
```

**Ventaja:** Cada chunk es una **unidad de significado completa**.

---

## Metadata y Filtrado

### ¿Por Qué Metadata?

La metadata permite **filtrar chunks antes de buscar semánticamente**, haciendo el sistema más preciso y eficiente.

### Metadata Incluida en Cada Chunk

```json
{
  "id": "lt_request_401k_withdrawal_chunk_5",
  "content": "# Business Rules: Fees...",
  "metadata": {
    // Metadata del Artículo
    "article_id": "lt_request_401k_termination_withdrawal_or_rollover",
    "article_title": "LT: How to Request a 401(k) Termination...",
    "record_keeper": "LT Trust",           // ← FILTRO CRÍTICO
    "plan_type": "401(k)",                 // ← FILTRO CRÍTICO
    "scope": "recordkeeper-specific",
    "tags": ["Distribution", "Withdrawal", "Taxes"],
    "topic": "distribution",               // ← Para routing
    "subtopics": ["termination_distribution", "rollover", "cash_withdrawal"],
    
    // Metadata del Chunk
    "chunk_type": "business_rules",        // ← Para endpoint routing
    "chunk_category": "fees",              // ← Subcategoría específica
    "chunk_index": 5,                      // ← Orden dentro del artículo
    "chunk_tier": "critical",              // ← Para priorización
    
    // Para Búsqueda Avanzada
    "specific_topics": ["fees", "costs", "charges"],
    "content_hash": "a3f2d8c1"            // ← Para deduplicación
  }
}
```

### Estrategia de Filtrado

#### Filtros MANDATORY (siempre se aplican)

```python
# Antes de hacer búsqueda semántica, filtrar:
filter = {
    "record_keeper": {"$eq": "LT Trust"},  # Solo artículos de LT Trust
    "plan_type": {"$eq": "401(k)"}         # Solo planes 401(k)
}
```

**¿Por qué?** Evita que artículos de otros recordkeepers (Vanguard, Fidelity) contaminen los resultados.

#### Filtros SOFT (preferir pero no requerir)

```python
# Preferir chunks que matcheen el topic
preferred_filter = {
    "topic": {"$eq": "distribution"},
    "subtopics": {"$in": ["rollover", "cash_withdrawal"]}
}
```

**¿Por qué?** Si no hay match exacto, puede buscar en topics relacionados.

#### Priorización de Resultados

Cuando hay múltiples chunks que matchean:

```
Priority 1: record_keeper + plan_type + topic + subtopic (Exact match)
Priority 2: record_keeper + plan_type + topic (Specific match)
Priority 3: plan_type + topic, scope="general" (General match)
Priority 4: topic only (Fallback con disclaimer)
```

**Ejemplo:**

Query: "What fees apply to LT Trust 401k withdrawals?"

```
Búsqueda con filtros:
  record_keeper = "LT Trust"
  plan_type = "401(k)"
  topic = "distribution"
  subtopics contains "withdrawal"

Resultados ordenados por:
1. Chunk de LT Trust, 401(k), distribution, fees → 100% match
2. Chunk de LT Trust, 401(k), distribution, general → 90% match
3. Chunk general, 401(k), distribution, fees → 70% match
```

---

## Endpoints de la API

### Endpoint 1: `/api/v1/required-data`

**Propósito:** Identificar qué datos necesitamos del participante para responder su consulta.

#### Request

```json
POST /api/v1/required-data
Content-Type: application/json
X-API-Key: <tu-api-key>

{
  "inquiry": "Participant wants to rollover remaining 401k balance to Fidelity",
  "topic": "rollover",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "related_inquiries": [
    "How to close ForUsAll account"
  ]
}
```

#### Response

```json
{
  "article_reference": {
    "article_id": "lt_rollover_to_ira",
    "title": "LT: How to Complete a Rollover",
    "confidence": 0.95
  },
  
  "required_fields": {
    "participant_data": [
      {
        "field": "Current account balance",
        "description": "Total current balance in the ForUsAll 401(k)",
        "why_needed": "To determine if there are funds available to rollover",
        "data_type": "currency",
        "required": true
      },
      {
        "field": "Vested balance",
        "description": "Amount that is vested (eligible for distribution)",
        "why_needed": "Only vested amounts can be rolled over",
        "data_type": "currency",
        "required": true
      },
      {
        "field": "Employment status",
        "description": "Current status (terminated, active, etc.)",
        "why_needed": "Must be terminated to request distribution",
        "data_type": "string",
        "required": true
      }
    ],
    
    "plan_data": [
      {
        "field": "Plan status",
        "description": "Whether plan is active, terminated, or in blackout",
        "why_needed": "Distributions cannot be processed during blackout",
        "data_type": "string",
        "required": true
      },
      {
        "field": "Distribution fees",
        "description": "Fees that apply to distributions",
        "why_needed": "To inform participant of costs",
        "data_type": "object",
        "required": false
      }
    ]
  },
  
  "metadata": {
    "total_fields": 5,
    "critical_fields": 3,
    "estimated_complexity": "medium"
  }
}
```

#### Flujo Interno

```
1. Recibe request con inquiry + topic + record_keeper
2. Filtra chunks por metadata:
   - record_keeper = "LT Trust"
   - plan_type = "401(k)"  
   - chunk_type = "required_data" | "eligibility" | "critical_flags"
3. Búsqueda semántica en Pinecone (top 5-10 chunks)
4. Rerank chunks
5. Construye context con chunks relevantes
6. LLM genera respuesta estructurada en JSON
7. Parsea y devuelve required_fields
```

---

### Endpoint 2: `/api/v1/generate-response`

**Propósito:** Generar respuesta contextualizada una vez que tenemos los datos del participante.

#### Request

```json
POST /api/v1/generate-response
Content-Type: application/json
X-API-Key: <tu-api-key>

{
  "inquiry": "Participant wants to rollover $1,993.84 to Fidelity 401k",
  "topic": "rollover",
  "record_keeper": "LT Trust",
  "plan_type": "401(k)",
  "related_inquiries": ["How to close account"],
  
  "collected_data": {
    "participant_data": {
      "current_balance": "$1,993.84",
      "vested_balance": "$1,993.84",
      "employment_status": "terminated"
    },
    "plan_data": {
      "plan_status": "active",
      "distribution_fees": {
        "base_fee": "$75",
        "wire_fee": "$35"
      }
    }
  },
  
  "context": {
    "max_response_tokens": 1500,
    "total_inquiries_in_ticket": 2
  }
}
```

#### Response

```json
{
  "inquiry_id": "auto-generated-uuid",
  
  "primary_source": {
    "article_id": "lt_rollover_process",
    "title": "LT: How to Complete a Rollover",
    "record_keeper": "LT Trust",
    "specificity": "recordkeeper-specific"
  },
  
  "decision": "can_proceed",
  "confidence": 0.97,
  
  "response": {
    "sections": [
      {
        "topic": "rollover_process",
        "answer_components": [
          "You can rollover the remaining $1,993.84 to your Fidelity 401(k)",
          "Log in to the ForUsAll portal and go to Loans & Distributions",
          "Select Rollover and provide your Fidelity account details",
          "A $75 distribution fee applies ($35 additional if you choose wire)"
        ],
        "steps": [
          "Log in to https://account.forusall.com/login",
          "Navigate to Loans & Distributions",
          "Select 'Rollover' as distribution type",
          "Enter Fidelity account information",
          "Review and submit request"
        ],
        "warnings": [
          "Distribution fee ($75) is non-refundable",
          "Wire fee ($35) is non-refundable if wire is chosen",
          "Verify Fidelity account details to avoid rejection"
        ]
      }
    ]
  },
  
  "guardrails": {
    "must_not_say": [
      "Exact delivery date guarantees",
      "That wire fees can be refunded",
      "That unvested amounts can be rolled over"
    ],
    "must_verify": [
      "Receiving institution details are correct"
    ]
  },
  
  "metadata": {
    "confidence": 0.97,
    "sources_used": ["business_rules.fees", "steps.1-5"],
    "token_count": 487,
    "processing_time_ms": 1250
  }
}
```

#### Flujo Interno

```
1. Recibe request con inquiry + topic + collected_data
2. Determina token budget (1500 tokens para 2 inquiries)
3. Filtra chunks por metadata:
   - record_keeper = "LT Trust"
   - plan_type = "401(k)"
   - topic = "distribution"
4. Búsqueda semántica en Pinecone
5. Recupera chunks por tier (hasta llenar budget):
   - Tier CRITICAL: siempre
   - Tier HIGH: si cabe
   - Tier MEDIUM/LOW: solo si sobra espacio
6. Rerank chunks recuperados
7. Construye context optimizado
8. LLM genera respuesta usando prompt específico
9. Parsea y estructura respuesta
10. Devuelve JSON con response + guardrails + metadata
```

---

## Flujo de Datos Completo

### Caso de Uso: Ticket con 2 Inquiries

**Ticket Original:**
> "Quiero hacer rollover de mi 401k a Fidelity. También quiero cerrar mi cuenta después."

#### Fase 1: Análisis (n8n)

```
AI Analyzer detecta:
  - Inquiry 1: "Rollover to Fidelity" → topic: "rollover"
  - Inquiry 2: "Close account" → topic: "account_closure"
```

#### Fase 2: Recolección de Datos (Secuencial)

```
┌─ Inquiry 1: Rollover ─┐
│                        │
│ KB API /required-data  │ → Devuelve: ["current_balance", "vested_balance", 
│                        │              "employment_status", "plan_status"]
└────────────────────────┘
           │
           ▼
┌─ Inquiry 2: Account Closure ─┐
│                               │
│ KB API /required-data         │ → Devuelve: ["pending_distributions",
│                               │              "final_balance"]
└───────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ n8n MERGEA required fields  │ → Lista consolidada (sin duplicados)
│ (deduplicación)             │
└─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ AI Mapper traduce a campos  │ → ["participant_data.balance",
│ de ForUsBots                │    "participant_data.vesting",
│                             │    "plan_data.status"]
└─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ ForUsBots scrapea portal    │ → Obtiene datos reales
└─────────────────────────────┘
```

#### Fase 3: Generación de Respuestas (Secuencial)

```
┌─ Inquiry 1: Rollover ─────────┐
│                                │
│ KB API /generate-response      │ → Response sobre proceso de rollover
│ + collected_data               │   + fees + timelines + warnings
│                                │
│ Token budget: 1500 tokens      │
└────────────────────────────────┘
           │
           ▼
┌─ Inquiry 2: Account Closure ──┐
│                                │
│ KB API /generate-response      │ → Response sobre cierre de cuenta
│ + collected_data               │   + qué pasa después + timelines
│                                │
│ Token budget: 1500 tokens      │
└────────────────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ n8n EMPAQUETA responses        │ → Bundle consolidado
│ (kb_bundle_v1)                 │   (shared context + inquiries)
└────────────────────────────────┘
           │
           ▼
┌────────────────────────────────┐
│ DevRev AI procesa bundle       │ → Genera respuesta unificada
│ (context window: 4000 tokens)  │   + decide acción en ticket
└────────────────────────────────┘
```

#### Token Budget Management

```
Ticket con 2 inquiries:
  - Response 1: 1500 tokens max
  - Response 2: 1500 tokens max
  - Overhead merge: ~200 tokens
  ─────────────────────────────
  Total: ~3200 tokens (< 4000 limit DevRev AI)
```

---

## Integración con el Sistema Multi-Agente

### Actores del Sistema

```
DevRev (CRM) 
  ↓ dispara trigger
n8n (Orquestador)
  ↓ consulta x2
KB API (ESTE SISTEMA)
  ↓ indica campos necesarios
AI Mapper
  ↓ traduce a endpoints
ForUsBots (RPA)
  ↓ devuelve datos
n8n (mergea)
  ↓ consulta x2 con datos
KB API (respuestas)
  ↓ empaqueta
n8n (bundle)
  ↓ envía bundle
DevRev AI (decisión final)
```

### Responsabilidades de Cada Actor

#### DevRev (CRM)
- Recibe tickets de participantes
- Dispara workflow de n8n
- Recibe respuesta final y acción

#### n8n (Orquestador)
- Detecta inquiries en ticket (con IA)
- Determina topics por inquiry
- Llama KB API (2 veces por inquiry)
- Mergea required_fields (deduplicación)
- Llama AI Mapper
- Llama ForUsBots
- Mergea responses en bundle
- Envía bundle a DevRev AI

#### KB API (Este Sistema)
- **NO** detecta inquiries (n8n lo hace)
- **NO** scrapea datos (ForUsBots lo hace)
- **NO** decide acciones en CRM (DevRev AI lo hace)
- **SÍ** devuelve qué datos necesita (lenguaje natural)
- **SÍ** genera respuestas contextualizadas
- **SÍ** incluye guardrails y warnings
- **SÍ** respeta token budgets

#### AI Mapper
- Traduce campos en lenguaje natural a campos de ForUsBots
- Determina qué endpoints llamar (participant_data, plan_data)
- Construye payloads para ForUsBots

#### ForUsBots (RPA)
- Scrapea portal del participante
- Devuelve datos estructurados
- No interpreta ni decide, solo extrae

#### DevRev AI
- Recibe bundle de KB API
- Genera respuesta final al participante
- Decide acción (cerrar ticket, escalar, crear issue)
- Tiene context window de ~4000 tokens

---

## Consideraciones de Producción

### Performance

- **Latencia target:** < 2 segundos por request
- **Throughput:** ~10 requests/segundo
- **Caching:** Considerar cache de chunks frecuentes

### Escalabilidad

- **Artículos:** Diseñado para ~280, escalable a miles
- **Chunks por artículo:** ~30-35
- **Total vectores:** ~8,400 (escalable a millones con Pinecone)

### Monitoring

- Confidence scores por respuesta
- Token usage por request
- Latencias de Pinecone y OpenAI
- Error rates por endpoint

### Costos Estimados (Mensual)

```
Pinecone (Starter): ~$70/mes
OpenAI API (GPT-4o-mini): ~$30-50/mes (uso moderado)
Render (Deployment): ~$7-25/mes
─────────────────────────────
Total: ~$110-150/mes
```

### Mantenimiento

- **Actualización de artículos:** Pipeline automático (ver PIPELINE_GUIDE.md)
- **Nuevos artículos:** Mismo pipeline
- **Cambios en estructura JSON:** Requiere ajuste en chunking.py

---

## Próximos Pasos

1. Crear índice en Pinecone
2. Procesar y subir artículos existentes
3. Implementar RAG engine con búsqueda y reranking
4. Crear endpoints FastAPI
5. Testing con tickets reales
6. Deploy a producción

---

**Documentación Completa:** Ver también `PIPELINE_GUIDE.md` para instrucciones de procesamiento de artículos nuevos.
