# Fase 5: RAG Engine - Plan de Implementación

**Estado:** ⏳ PENDIENTE  
**Duración estimada:** 1.5-2 horas  
**Prerequisitos:** Fase 4 completada (chunks en Pinecone)

---

## Objetivo

Implementar el motor RAG que:
1. Busca chunks relevantes en Pinecone
2. Reordena por relevancia
3. Construye context respetando token budget
4. Genera respuestas con OpenAI GPT-4o-mini

---

## Componentes a Crear

### 1. `data_pipeline/rag_engine.py` (~400-500 líneas)

Motor principal del RAG.

#### Clases Principales

```python
class RAGEngine:
    """Motor RAG para búsqueda y generación."""
    
    def __init__(self):
        self.pinecone_index = ...
        self.openai_client = ...
        
    async def get_required_data(
        self, 
        inquiry: str,
        record_keeper: str,
        plan_type: str,
        topic: str
    ) -> RequiredDataResponse:
        """Endpoint 1: Determinar qué datos se necesitan."""
        pass
        
    async def generate_response(
        self,
        inquiry: str,
        record_keeper: str,
        plan_type: str,
        topic: str,
        collected_data: dict,
        max_tokens: int
    ) -> GenerateResponse:
        """Endpoint 2: Generar respuesta contextualizada."""
        pass
        
    def _search_pinecone(
        self,
        query: str,
        filters: dict,
        top_k: int
    ) -> List[Chunk]:
        """Búsqueda semántica en Pinecone."""
        pass
        
    def _rerank_chunks(
        self,
        query: str,
        chunks: List[Chunk]
    ) -> List[Chunk]:
        """Reordenar chunks por relevancia."""
        pass
        
    def _build_context(
        self,
        chunks: List[Chunk],
        max_tokens: int,
        mode: str  # "required_data" or "response"
    ) -> str:
        """Construir context respetando budget."""
        pass
        
    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int
    ) -> str:
        """Llamar OpenAI GPT-4o-mini."""
        pass
```

---

## Flujo Detallado por Endpoint

### Endpoint 1: Get Required Data

```
Input:
  - inquiry: "Participant wants rollover to Fidelity"
  - record_keeper: "LT Trust"
  - plan_type: "401(k)"
  - topic: "rollover"

Proceso:

1. Filter Setup:
   filter = {
       "record_keeper": {"$eq": "LT Trust"},
       "plan_type": {"$eq": "401(k)"},
       "chunk_type": {"$in": ["required_data", "eligibility", "business_rules"]}
   }

2. Search Pinecone:
   - Query: inquiry (Pinecone lo embedirá)
   - Top K: 5-10 chunks
   - Include metadata: True

3. Rerank (opcional para este endpoint):
   - Usar bge-reranker-v2-m3 si disponible
   - O confiar en Pinecone score

4. Build Context (~500 tokens):
   - CRITICAL chunks primero
   - Incluir required_data completo
   - Incluir eligibility rules

5. Call LLM:
   System: "You are a 401(k) KB assistant. Extract required data fields."
   User: "Based on this context, what data do we need?"
   Max tokens: 500

6. Parse Response:
   - Estructurar en participant_data y plan_data
   - Agregar field metadata (type, required, description)

Output:
{
  "article_reference": {
    "article_id": "...",
    "confidence": 0.95
  },
  "required_fields": {
    "participant_data": [...],
    "plan_data": [...]
  }
}
```

---

### Endpoint 2: Generate Response

```
Input:
  - inquiry: "How to rollover remaining balance?"
  - record_keeper: "LT Trust"
  - plan_type: "401(k)"
  - topic: "rollover"
  - collected_data: {...}
  - max_tokens: 1500

Proceso:

1. Filter Setup:
   filter = {
       "record_keeper": {"$eq": "LT Trust"},
       "plan_type": {"$eq": "401(k)"},
       "topic": {"$eq": "rollover"}
   }

2. Search Pinecone:
   - Query: inquiry + collected_data summary
   - Top K: 20-30 chunks
   - Include metadata: True

3. Retrieve by Tier:
   a. CRITICAL chunks (siempre incluir):
      - decision_guide
      - response_frames
      - guardrails
      - business_rules críticas
   
   b. HIGH chunks (si hay budget):
      - steps
      - fees_details
      - common_issues
   
   c. MEDIUM/LOW (si sobra espacio):
      - examples
      - faqs

4. Rerank:
   - Reordenar todos los chunks HIGH/MEDIUM/LOW
   - CRITICAL chunks mantienen prioridad

5. Build Context (respetando max_tokens):
   - Token budget: 70% del max_tokens
   - Ejemplo: max_tokens=1500 → context ~1000 tokens
   - Priorizar según tier

6. Call LLM:
   System: """
   You are a 401(k) KB assistant.
   Respond based ONLY on context.
   Follow guardrails strictly.
   """
   
   User: """
   Context: {context}
   
   Collected Data: {collected_data}
   
   Inquiry: {inquiry}
   
   Generate response with:
   - Answer components
   - Steps (if applicable)
   - Warnings
   - Guardrails check
   """
   
   Max tokens: 30% de max_tokens (para respuesta LLM)

7. Parse Response:
   - Estructurar en sections
   - Calcular confidence basado en Pinecone scores
   - Agregar metadata

Output:
{
  "decision": "can_proceed",
  "confidence": 0.95,
  "response": {
    "sections": [...]
  },
  "guardrails": {...},
  "metadata": {
    "token_count": 487,
    "chunks_used": 8
  }
}
```

---

## Prompt Engineering

### System Prompt (Required Data)

```python
SYSTEM_PROMPT_REQUIRED_DATA = """You are a specialized assistant for 401(k) participant advisory.

Your task: Analyze the provided knowledge base context and determine what specific data fields are needed to properly respond to the participant's inquiry.

Rules:
1. Extract ONLY fields explicitly mentioned or implied in the context
2. Categorize fields into participant_data and plan_data
3. For each field, specify:
   - field name (clear, descriptive)
   - description (what it is)
   - why_needed (why we need it)
   - data_type (text, currency, date, boolean, number)
   - required (true/false)
4. If insufficient context, indicate uncertainty but provide best guess

Respond in JSON format."""
```

### System Prompt (Generate Response)

```python
SYSTEM_PROMPT_GENERATE_RESPONSE = """You are a specialized 401(k) participant advisory assistant.

Your task: Generate a contextual, accurate response based ONLY on the provided knowledge base context and collected participant data.

Critical Rules:
1. Base ALL information on the provided context
2. NEVER invent or assume information not in context
3. Follow ALL guardrails strictly (what NOT to say)
4. If information is incomplete, acknowledge it
5. Structure response clearly with sections
6. Include warnings for critical items (taxes, fees, deadlines)
7. Be specific about recordkeeper procedures

Response Structure:
- sections: Array of topic sections
  - topic: Topic identifier
  - answer_components: Key points
  - steps: Actionable steps (if applicable)
  - warnings: Important warnings
  
If confidence is low (<70%), indicate "uncertain" decision."""
```

### User Prompt Template (Generate Response)

```python
USER_PROMPT_TEMPLATE = """
CONTEXT FROM KNOWLEDGE BASE:
{context}

COLLECTED PARTICIPANT DATA:
{collected_data}

PARTICIPANT INQUIRY:
{inquiry}

Generate a comprehensive response following the guidelines.
"""
```

---

## Reranking Strategy

### Opción A: Sentence Transformers (Local)

```python
from sentence_transformers import CrossEncoder

class Reranker:
    def __init__(self):
        self.model = CrossEncoder('BAAI/bge-reranker-v2-m3')
    
    def rerank(self, query: str, chunks: List[Dict]) -> List[Dict]:
        """Reordena chunks por relevancia."""
        pairs = [[query, chunk['content']] for chunk in chunks]
        scores = self.model.predict(pairs)
        
        for chunk, score in zip(chunks, scores):
            chunk['rerank_score'] = float(score)
        
        return sorted(chunks, key=lambda x: x['rerank_score'], reverse=True)
```

**Ventaja:** Gratis, privado  
**Desventaja:** Requiere instalación modelo (~500MB)

---

### Opción B: Cohere Rerank API

```python
import cohere

class CohereReranker:
    def __init__(self, api_key: str):
        self.client = cohere.Client(api_key)
    
    def rerank(self, query: str, chunks: List[Dict]) -> List[Dict]:
        """Reordena chunks usando Cohere API."""
        documents = [chunk['content'] for chunk in chunks]
        
        results = self.client.rerank(
            model="rerank-english-v3.0",
            query=query,
            documents=documents,
            top_n=10
        )
        
        reranked = []
        for result in results.results:
            chunk = chunks[result.index]
            chunk['rerank_score'] = result.relevance_score
            reranked.append(chunk)
        
        return reranked
```

**Ventaja:** Mejor performance  
**Desventaja:** Costo por request (~$1 per 1000 searches)

---

### Recomendación Inicial

Empezar **sin reranking** y confiar en Pinecone cosine similarity. Agregar reranking después si es necesario.

---

## Token Management

### Estrategia de Budget

```python
def calculate_context_budget(max_response_tokens: int) -> int:
    """
    Calcula cuántos tokens podemos usar para context.
    
    Reserva 30% para la respuesta del LLM.
    """
    context_budget = int(max_response_tokens * 0.7)
    return context_budget

# Ejemplo:
# max_response_tokens = 1500
# context_budget = 1050 tokens
# response_budget = 450 tokens
```

### Counting Tokens

```python
import tiktoken

def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Cuenta tokens usando tiktoken."""
    encoding = tiktoken.encoding_for_model(model)
    return len(encoding.encode(text))

def truncate_to_budget(chunks: List[str], budget: int) -> List[str]:
    """Trunca lista de chunks para respetar budget."""
    selected = []
    used_tokens = 0
    
    for chunk in chunks:
        chunk_tokens = count_tokens(chunk)
        if used_tokens + chunk_tokens <= budget:
            selected.append(chunk)
            used_tokens += chunk_tokens
        else:
            break
    
    return selected
```

---

## Confidence Calculation

```python
def calculate_confidence(
    pinecone_scores: List[float],
    rerank_scores: Optional[List[float]] = None,
    chunk_tiers: List[str] = []
) -> float:
    """
    Calcula confidence score basado en múltiples factores.
    
    Factores:
    - Pinecone similarity (0-1)
    - Rerank score (si disponible)
    - Presencia de chunks CRITICAL
    """
    # Base: promedio de top 3 Pinecone scores
    base_confidence = sum(pinecone_scores[:3]) / 3
    
    # Boost si hay reranking
    if rerank_scores:
        rerank_confidence = sum(rerank_scores[:3]) / 3
        base_confidence = (base_confidence + rerank_confidence) / 2
    
    # Boost si hay chunks CRITICAL
    critical_count = chunk_tiers.count('critical')
    if critical_count >= 2:
        base_confidence = min(1.0, base_confidence * 1.1)
    
    return round(base_confidence, 2)
```

---

## Error Handling

```python
class RAGException(Exception):
    """Base exception para RAG engine."""
    pass

class NoResultsFound(RAGException):
    """No se encontraron chunks relevantes."""
    pass

class InsufficientContext(RAGException):
    """Context insuficiente para responder."""
    pass

class LLMError(RAGException):
    """Error al llamar LLM."""
    pass

# Uso en RAG engine:
try:
    results = search_pinecone(...)
    if not results:
        raise NoResultsFound(f"No results for topic: {topic}")
    
    context = build_context(...)
    if len(context) < MIN_CONTEXT_TOKENS:
        raise InsufficientContext("Not enough context")
    
    response = await call_llm(...)
    
except NoResultsFound:
    return {
        "decision": "out_of_scope",
        "confidence": 0.0,
        "message": "No relevant articles found"
    }
```

---

## Testing

### Unit Tests

```python
# tests/test_rag_engine.py

async def test_get_required_data():
    """Test endpoint 1."""
    rag = RAGEngine()
    
    result = await rag.get_required_data(
        inquiry="Participant wants rollover",
        record_keeper="LT Trust",
        plan_type="401(k)",
        topic="rollover"
    )
    
    assert result.article_reference.confidence > 0.7
    assert len(result.required_fields.participant_data) > 0

async def test_generate_response():
    """Test endpoint 2."""
    rag = RAGEngine()
    
    result = await rag.generate_response(
        inquiry="How to rollover?",
        record_keeper="LT Trust",
        plan_type="401(k)",
        topic="rollover",
        collected_data={"balance": "$10,000"},
        max_tokens=1500
    )
    
    assert result.decision in ["can_proceed", "uncertain", "out_of_scope"]
    assert result.metadata.token_count <= 1500
```

---

## Dependencias Adicionales

Agregar a `requirements.txt`:

```txt
# Para reranking (opcional)
sentence-transformers>=2.2.0
# O
cohere>=4.0.0

# Para token counting
tiktoken>=0.5.0
```

---

## Archivos a Crear

```
data_pipeline/
├── rag_engine.py          # Motor principal (~400-500 líneas)
├── prompts.py             # System y user prompts
├── token_manager.py       # Token counting y truncation
└── confidence.py          # Cálculo de confidence

tests/
├── test_rag_engine.py     # Unit tests
└── test_integration.py    # Integration tests
```

---

## Próximo Paso (Fase 6)

Una vez completado el RAG Engine, proceder a crear los endpoints FastAPI.

Ver: `PHASE_6.md` (por crear)

---

**Prerequisito:** Fase 4 completada  
**Duración:** 1.5-2 horas  
**Output:** RAG Engine funcional y testeado
