# Plan: Eliminar Sección "Summary" de los Artículos KB

**Fecha**: 2026-02-10
**Estado**: Pendiente de ejecución
**Ejecutor**: Agente AI (Opus 4.6)

---

## Objetivo

Eliminar completamente la sección `"summary"` de todos los artículos JSON y del pipeline de procesamiento (chunking, validación, vectorización). Solo se deben mover `"topic"` y `"subtopics"` de `summary` a `metadata`. Ninguna otra información del summary se mueve a `details`.

**Razón**: El summary es una copia simplificada de lo que ya existe en `details`, lo que causa duplicación de vectores en Pinecone, ruido en búsquedas RAG y costos innecesarios.

---

## HALLAZGO CRÍTICO: `critical_flags`

**ANTES de ejecutar cualquier cambio, el agente DEBE resolver esto:**

El campo `summary.critical_flags` contiene datos que **NO existen en ningún otro lugar** del artículo:

```json
"critical_flags": {
    "portal_required": true,
    "mfa_relevant": false,
    "record_keeper_must_be": "LT Trust"
}
```

Este campo es utilizado por el chunker para crear un chunk tipo `critical_flags` (tier: critical). Es información de validación/routing, no un resumen.

**Decisión tomada**: Mover `critical_flags` a `details.critical_flags` en cada JSON, ya que es data única y no duplicada. Esto es la única excepción a la regla de "no mover nada del summary a details", porque en este caso `critical_flags` NO es un resumen de algo en details — es data original que solo vive en summary.

---

## Archivos a Modificar (en orden de ejecución)

### Archivo 1: `kb-rag-system/data_pipeline/article_processor.py`

**Cambios necesarios:**

#### Cambio 1.1 — Línea 22: Eliminar `"summary"` de secciones requeridas

```python
# ANTES (línea 22):
self.required_sections = ["metadata", "summary", "details"]

# DESPUÉS:
self.required_sections = ["metadata", "details"]
```

#### Cambio 1.2 — Líneas 96-109: Leer `topic` y `subtopics` desde `metadata` en vez de `summary`

```python
# ANTES (líneas 96-109):
def get_article_info(self, article: Dict[str, Any]) -> Dict[str, Any]:
    metadata = article.get("metadata", {})
    summary = article.get("summary", {})

    return {
        "article_id": metadata.get("article_id"),
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "record_keeper": metadata.get("record_keeper"),
        "plan_type": metadata.get("plan_type"),
        "scope": metadata.get("scope"),
        "tags": metadata.get("tags", []),
        "topic": summary.get("topic"),
        "subtopics": summary.get("subtopics", [])
    }

# DESPUÉS:
def get_article_info(self, article: Dict[str, Any]) -> Dict[str, Any]:
    metadata = article.get("metadata", {})

    return {
        "article_id": metadata.get("article_id"),
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "record_keeper": metadata.get("record_keeper"),
        "plan_type": metadata.get("plan_type"),
        "scope": metadata.get("scope"),
        "tags": metadata.get("tags", []),
        "topic": metadata.get("topic"),
        "subtopics": metadata.get("subtopics", [])
    }
```

---

### Archivo 2: `kb-rag-system/data_pipeline/chunking.py`

Este archivo tiene **8 lugares** donde se usa `summary`. Todos deben ser modificados.

#### Cambio 2.1 — Líneas 64-77: `_extract_base_metadata()` — Leer topic/subtopics de metadata

```python
# ANTES (líneas 62-77):
def _extract_base_metadata(self, article: Dict[str, Any]) -> Dict[str, Any]:
    metadata = article.get("metadata", {})
    summary = article.get("summary", {})

    return {
        "article_id": metadata.get("article_id"),
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "record_keeper": metadata.get("record_keeper"),
        "plan_type": metadata.get("plan_type"),
        "scope": metadata.get("scope"),
        "tags": metadata.get("tags", []),
        "topic": summary.get("topic"),
        "subtopics": summary.get("subtopics", [])
    }

# DESPUÉS:
def _extract_base_metadata(self, article: Dict[str, Any]) -> Dict[str, Any]:
    metadata = article.get("metadata", {})

    return {
        "article_id": metadata.get("article_id"),
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "record_keeper": metadata.get("record_keeper"),
        "plan_type": metadata.get("plan_type"),
        "scope": metadata.get("scope"),
        "tags": metadata.get("tags", []),
        "topic": metadata.get("topic"),
        "subtopics": metadata.get("subtopics", [])
    }
```

#### Cambio 2.2 — Líneas 148-155: `_create_required_data_chunks()` — Eliminar uso de summary

```python
# ANTES (líneas 148-155):
chunks = []
details = article.get("details", {})
summary = article.get("summary", {})

# Chunk 1: Required Data Complete
required_data = details.get("required_data", {})
if required_data:
    content = self._format_required_data(required_data, summary)

# DESPUÉS:
chunks = []
details = article.get("details", {})

# Chunk 1: Required Data Complete
required_data = details.get("required_data", {})
if required_data:
    content = self._format_required_data(required_data)
```

#### Cambio 2.3 — Líneas 182-193: Leer `critical_flags` de `details` en vez de `summary`

```python
# ANTES (líneas 182-193):
# Chunk 3: Critical Flags
critical_flags = summary.get("critical_flags", {})

# DESPUÉS:
# Chunk 3: Critical Flags
critical_flags = details.get("critical_flags", {})
```

#### Cambio 2.4 — Líneas 197-211: `_format_required_data()` — Eliminar parámetro summary

```python
# ANTES (líneas 197-211):
def _format_required_data(
    self,
    required_data: Dict[str, Any],
    summary: Dict[str, Any]
) -> str:
    """Formatea required_data para el chunk."""
    lines = ["# Required Data for This Process\n"]

    # Agregar resumen de data requerida
    required_summary = summary.get("required_data_summary", [])
    if required_summary:
        lines.append("## Summary:")
        for item in required_summary:
            lines.append(f"- {item}")
        lines.append("")

    # Must have fields
    ...

# DESPUÉS:
def _format_required_data(
    self,
    required_data: Dict[str, Any]
) -> str:
    """Formatea required_data para el chunk."""
    lines = ["# Required Data for This Process\n"]

    # Must have fields
    ...
```

Nota: Se elimina completamente el bloque de `required_data_summary` (líneas 205-211). Es un resumen redundante; la data completa ya está en `must_have` y `nice_to_have`.

#### Cambio 2.5 — Líneas 271-304: `_create_tier1_chunks()` — Eliminar summary de guardrails

```python
# ANTES (líneas 271-304):
def _create_tier1_chunks(self, article, base_metadata):
    chunks = []
    details = article.get("details", {})
    summary = article.get("summary", {})
    ...
    # Chunk: Guardrails Complete
    guardrails = details.get("guardrails", {})
    if guardrails:
        content = self._format_guardrails(guardrails, summary)
    ...

# DESPUÉS:
def _create_tier1_chunks(self, article, base_metadata):
    chunks = []
    details = article.get("details", {})
    ...
    # Chunk: Guardrails Complete
    guardrails = details.get("guardrails", {})
    if guardrails:
        content = self._format_guardrails(guardrails)
    ...
```

#### Cambio 2.6 — Líneas 432-463: `_format_guardrails()` — Eliminar parámetro summary

```python
# ANTES (líneas 432-463):
def _format_guardrails(
    self,
    guardrails: Dict[str, Any],
    summary: Dict[str, Any]
) -> str:
    """Formatea guardrails."""
    lines = ["# Guardrails and Safety Rules\n"]

    # Plan specific guardrails from summary
    plan_guardrails = summary.get("plan_specific_guardrails", [])
    if plan_guardrails:
        lines.append("## Plan-Specific Guardrails:")
        for item in plan_guardrails:
            lines.append(f"- {item}")
        lines.append("")

    # Must not
    must_not = guardrails.get("must_not", [])
    ...

# DESPUÉS:
def _format_guardrails(
    self,
    guardrails: Dict[str, Any]
) -> str:
    """Formatea guardrails."""
    lines = ["# Guardrails and Safety Rules\n"]

    # Must not
    must_not = guardrails.get("must_not", [])
    ...
```

Nota: Se elimina `plan_specific_guardrails` porque su contenido es redundante con `details.guardrails.must_not`. Verificado en los 3 JSONs: las reglas de `plan_specific_guardrails` están cubiertas en `must_not`.

#### Cambio 2.7 — Líneas 495-529: `_create_tier2_chunks()` — Eliminar summary, eliminar key_steps_summary chunk

```python
# ANTES (líneas 495-529):
def _create_tier2_chunks(self, article, base_metadata):
    chunks = []
    details = article.get("details", {})
    summary = article.get("summary", {})
    ...
    # Chunk: Key Steps Summary
    key_steps_summary = summary.get("key_steps_summary", [])
    if key_steps_summary:
        content = "# Key Steps Summary\n\n" + ...
        chunks.append(...)

    ...
    # Chunk: Fees Details
    fees = details.get("fees", [])
    if fees:
        content = self._format_fees(fees, summary.get("key_business_rules", []))

# DESPUÉS:
def _create_tier2_chunks(self, article, base_metadata):
    chunks = []
    details = article.get("details", {})
    ...
    # ELIMINADO: Key Steps Summary chunk (era resumen redundante de steps)

    ...
    # Chunk: Fees Details
    fees = details.get("fees", [])
    if fees:
        content = self._format_fees(fees)
```

Nota: Se elimina el chunk `key_steps_summary` completamente porque es un resumen de los steps detallados que ya tienen su propio chunk. También se elimina `key_business_rules` del formato de fees.

#### Cambio 2.8 — Línea 643: `_format_fees()` — Eliminar parámetro business_rules

```python
# ANTES (líneas 643-666):
def _format_fees(self, fees: List[Dict[str, Any]], business_rules: List[str]) -> str:
    """Formatea fees."""
    lines = ["# Fees and Charges\n"]

    # Fee rules from business rules
    fee_rules = [rule for rule in business_rules if "fee" in rule.lower()]
    if fee_rules:
        lines.append("## Fee Rules:")
        for rule in fee_rules:
            lines.append(f"- {rule}")
        lines.append("")

    # Detailed fees
    lines.append("## Fee Details:")
    ...

# DESPUÉS:
def _format_fees(self, fees: List[Dict[str, Any]]) -> str:
    """Formatea fees."""
    lines = ["# Fees and Charges\n"]

    # Detailed fees
    lines.append("## Fee Details:")
    ...
```

Nota: Se elimina el bloque "Fee Rules" que venía de `summary.key_business_rules`. La info de fees ya está completa en `details.fees` con service, fee y notes.

#### Cambio 2.9 — Líneas 678-696: `_create_tier3_chunks()` — Eliminar high_impact_faq_pairs

```python
# ANTES (líneas 678-696):
def _create_tier3_chunks(self, article, base_metadata):
    chunks = []
    details = article.get("details", {})
    summary = article.get("summary", {})

    # Chunks: FAQs
    faq_pairs = details.get("faq_pairs", [])
    high_impact_faqs = summary.get("high_impact_faq_pairs", [])

    # High impact FAQs primero
    if high_impact_faqs:
        content = self._format_faqs(high_impact_faqs)
        chunks.append(self._create_chunk(
            content=content,
            ...
            chunk_type="faqs",
            chunk_category="high_impact",
            tier="medium",
            ...
        ))

    # Regular FAQs
    if faq_pairs:
        ...

# DESPUÉS:
def _create_tier3_chunks(self, article, base_metadata):
    chunks = []
    details = article.get("details", {})

    # Chunks: FAQs
    faq_pairs = details.get("faq_pairs", [])

    # ELIMINADO: high_impact_faq_pairs (eran un subconjunto de faq_pairs)

    # FAQs
    if faq_pairs:
        ...
```

Nota: Se elimina `high_impact_faq_pairs` porque son un subconjunto de `details.faq_pairs`. Verificado: las FAQs de high_impact están incluidas en faq_pairs en los 3 artículos.

---

### Archivos 3, 4, 5: Los 3 archivos JSON de artículos

Estos son los 3 artículos existentes:

```
Participant Advisory/Distributions/
├── hardship_withdrawal_overview.json
├── LT: Completing Your Rollover Online – Best Practices.json
└── LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json
```

**Para CADA archivo JSON, hacer exactamente estos 3 cambios:**

#### Cambio JSON.1 — Agregar `topic` y `subtopics` a `metadata`

Copiar los valores de `summary.topic` y `summary.subtopics` a la sección `metadata`.

Ejemplo para `hardship_withdrawal_overview.json`:

```json
"metadata": {
    "article_id": "forusall_401k_hardship_withdrawal_complete_guide",
    "title": "...",
    "topic": "hardship_withdrawal",
    "subtopics": [
        "irs_approved_reasons",
        "fees",
        "tax_and_penalties",
        "rightsignature_request_flow",
        "estimated_processing_times",
        "documentation_retention",
        "plan_specific_limits"
    ],
    ...resto de metadata existente...
}
```

Valores por artículo:

| Artículo | topic | subtopics |
|----------|-------|-----------|
| hardship_withdrawal_overview.json | `"hardship_withdrawal"` | `["irs_approved_reasons", "fees", "tax_and_penalties", "rightsignature_request_flow", "estimated_processing_times", "documentation_retention", "plan_specific_limits"]` |
| LT: Completing Your Rollover... | `"rollover_online_flow"` | `["portal_navigation", "eligibility_separation_of_service_vs_default", "required_wire_vs_check_details", "fees_distribution_wire_overnight_check", "processing_times_and_review_timeline", "common_data_entry_errors_character_limits_fbo"]` |
| LT: How to Request a 401(k)... | `"termination_distribution_request"` | `["eligibility_prerequisites", "fees", "tax_withholding", "delivery_methods_check_wire_overnight", "mfa_and_portal_access", "processing_and_delivery_timelines"]` |

#### Cambio JSON.2 — Mover `critical_flags` a `details`

Copiar `summary.critical_flags` a `details.critical_flags`.

Ejemplo para `hardship_withdrawal_overview.json`:

```json
"details": {
    "critical_flags": {
        "portal_required": false,
        "mfa_relevant": false,
        "record_keeper_must_be": "ForUsAll"
    },
    "business_rules": [...],
    ...resto de details existente...
}
```

Valores por artículo:

| Artículo | critical_flags |
|----------|---------------|
| hardship_withdrawal_overview.json | `{"portal_required": false, "mfa_relevant": false, "record_keeper_must_be": "ForUsAll"}` |
| LT: Completing Your Rollover... | `{"portal_required": true, "mfa_relevant": false, "record_keeper_must_be": "LT Trust"}` |
| LT: How to Request a 401(k)... | `{"portal_required": true, "mfa_relevant": true, "record_keeper_must_be": "LT Trust"}` |

#### Cambio JSON.3 — Eliminar sección `summary` completa

Eliminar toda la key `"summary": {...}` del JSON. El archivo resultante debe tener solo 2 top-level keys:

```json
{
    "metadata": {...},
    "details": {...}
}
```

---

### NO requieren modificación

Los siguientes archivos fueron verificados y **NO contienen referencias a `summary`**:

- `kb-rag-system/data_pipeline/rag_engine.py` — Solo usa chunks genéricos
- `kb-rag-system/data_pipeline/pinecone_uploader.py` — Solo sube/consulta chunks
- `kb-rag-system/data_pipeline/token_manager.py` — Solo maneja tokens
- `kb-rag-system/data_pipeline/prompts.py` — Solo maneja prompts
- `kb-rag-system/api/main.py` — Solo endpoints
- `kb-rag-system/api/models.py` — Solo modelos Pydantic
- `kb-rag-system/api/config.py` — Solo configuración
- `kb-rag-system/scripts/process_single_article.py` — No referencia summary
- `kb-rag-system/scripts/verify_article.py` — No referencia summary
- `kb-rag-system/scripts/update_article.py` — No referencia summary
- `kb-rag-system/scripts/delete_article.py` — No referencia summary
- `kb-rag-system/scripts/list_chunks.py` — No referencia summary

---

## Después de Modificar: Re-procesar Artículos en Pinecone

Una vez que los cambios en el código y los JSONs estén completos, se deben re-procesar los 3 artículos para actualizar los vectores en Pinecone.

### Paso 1: Verificar que el código funcione con dry-run

Para CADA artículo, ejecutar (desde el directorio raíz del proyecto):

```bash
cd "/Users/ivanalvis/Desktop/FUA Knowledge Base Articles"
source kb-rag-system/venv/bin/activate

python kb-rag-system/scripts/process_single_article.py \
  "Participant Advisory/Distributions/hardship_withdrawal_overview.json" \
  --dry-run --show-chunks

python kb-rag-system/scripts/process_single_article.py \
  "Participant Advisory/Distributions/LT: Completing Your Rollover Online – Best Practices.json" \
  --dry-run --show-chunks

python kb-rag-system/scripts/process_single_article.py \
  "Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json" \
  --dry-run --show-chunks
```

**Verificar:**
- No hay errores de ejecución
- Ningún chunk contiene data del summary (required_data_summary, key_steps_summary, key_business_rules, high_impact_faq_pairs, plan_specific_guardrails)
- El chunk de critical_flags sigue existiendo y lee de details
- topic y subtopics aparecen en la metadata de cada chunk
- La cantidad de chunks es MENOR que antes (se eliminaron chunks redundantes)

**Conteo esperado de chunks (aproximado):**

| Artículo | Antes | Después (esperado) |
|----------|-------|--------------------|
| hardship_withdrawal_overview | 25 chunks | ~22 chunks (-key_steps_summary, -high_impact_faqs, -fee_rules_section) |
| Los otros 2 artículos | Similar reducción | Similar reducción |

### Paso 2: Eliminar artículos antiguos de Pinecone

Usar el script de delete para cada artículo:

```bash
python kb-rag-system/scripts/delete_article.py "forusall_401k_hardship_withdrawal_complete_guide"
python kb-rag-system/scripts/delete_article.py "lt_completing_your_rollover_online_best_practices"
python kb-rag-system/scripts/delete_article.py "lt_request_401k_termination_withdrawal_or_rollover"
```

**NOTA**: Verificar los article_ids exactos en el metadata de cada JSON antes de ejecutar.

### Paso 3: Subir artículos actualizados

```bash
python kb-rag-system/scripts/process_single_article.py \
  "Participant Advisory/Distributions/hardship_withdrawal_overview.json"

python kb-rag-system/scripts/process_single_article.py \
  "Participant Advisory/Distributions/LT: Completing Your Rollover Online – Best Practices.json"

python kb-rag-system/scripts/process_single_article.py \
  "Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
```

### Paso 4: Verificar los 3 artículos

```bash
python kb-rag-system/scripts/verify_article.py "forusall_401k_hardship_withdrawal_complete_guide"
python kb-rag-system/scripts/verify_article.py "lt_completing_your_rollover_online_best_practices"
python kb-rag-system/scripts/verify_article.py "lt_request_401k_termination_withdrawal_or_rollover"
```

**Verificar:**
- Cada artículo tiene chunks
- topic aparece en la metadata de los chunks
- No hay chunks con data de summary (key_steps_summary, high_impact_faqs, etc.)
- critical_flags chunk sigue existiendo

---

## Resumen de Cambios por Archivo

| # | Archivo | Tipo de Cambio | Complejidad |
|---|---------|---------------|-------------|
| 1 | `data_pipeline/article_processor.py` | Hacer summary opcional, leer topic/subtopics de metadata | Baja |
| 2 | `data_pipeline/chunking.py` | 9 cambios: eliminar todas las refs a summary | Media |
| 3 | `hardship_withdrawal_overview.json` | Mover topic+subtopics a metadata, mover critical_flags a details, eliminar summary | Baja |
| 4 | `LT: Completing Your Rollover...json` | Mover topic+subtopics a metadata, mover critical_flags to details, eliminar summary | Baja |
| 5 | `LT: How to Request a 401(k)...json` | Mover topic+subtopics a metadata, mover critical_flags to details, eliminar summary | Baja |

**Total**: 5 archivos a modificar. Ningún archivo nuevo a crear.

---

## Checklist de Validación Final

Después de completar todos los cambios, verificar:

- [ ] `article_processor.py` ya no requiere `"summary"` como sección obligatoria
- [ ] `article_processor.py` lee `topic` y `subtopics` de `metadata`
- [ ] `chunking.py` no contiene ninguna referencia a `summary` (buscar con grep)
- [ ] `chunking.py` lee `critical_flags` de `details`
- [ ] `_format_required_data()` ya no recibe parámetro `summary`
- [ ] `_format_guardrails()` ya no recibe parámetro `summary`
- [ ] `_format_fees()` ya no recibe parámetro `business_rules`
- [ ] No existe chunk tipo `steps_summary` (eliminado)
- [ ] No existe chunk de `high_impact_faq_pairs` (eliminado)
- [ ] Los 3 JSONs no tienen sección `"summary"`
- [ ] Los 3 JSONs tienen `"topic"` y `"subtopics"` en `metadata`
- [ ] Los 3 JSONs tienen `"critical_flags"` en `details`
- [ ] Los 3 artículos se procesaron con `--dry-run` sin errores
- [ ] Los 3 artículos antiguos se eliminaron de Pinecone
- [ ] Los 3 artículos actualizados se subieron a Pinecone
- [ ] Los 3 artículos se verificaron exitosamente
- [ ] `grep -r "summary" kb-rag-system/data_pipeline/` no retorna resultados relevantes
