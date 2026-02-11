# Plan: Migrar Artículos de Estructura Vieja (con `summary`) a Estructura Nueva (sin `summary`)

**Fecha**: 2026-02-10
**Ejecutor**: Agente AI (Opus 4.6)
**Tipo**: Transformación de datos — migración de esquema JSON

---

## Contexto

### Estructura vieja (v1 — con summary)

Los artículos tienen **3 keys top-level**:

```
root
├── metadata      ← identidad, clasificación, trazabilidad
├── summary       ← capa compacta (REDUNDANTE — se eliminará)
└── details       ← capa completa operable
```

### Estructura nueva (v2 — sin summary)

Los artículos tienen **2 keys top-level**:

```
root
├── metadata      ← identidad + topic + subtopics (movidos desde summary)
└── details       ← capa completa operable + critical_flags (movido desde summary)
```

### Por qué se elimina summary

1. **Duplicación**: El 95% del contenido de `summary` es una versión simplificada de lo que ya está en `details`. Esto causa vectores duplicados en Pinecone, ruido en búsquedas RAG, y costos innecesarios de tokens/embeddings.
2. **Los campos útiles se preservan**: Los únicos campos de `summary` con datos únicos (`topic`, `subtopics`, `critical_flags`) se mueven a `metadata` y `details` respectivamente.
3. **El pipeline de chunking ya no lee summary**: El código en `chunking.py` y `article_processor.py` fue actualizado para leer `topic`/`subtopics` de `metadata` y `critical_flags` de `details`.

---

## Inventario de campos en `summary` y su destino

| Campo en `summary` | ¿Datos únicos? | Destino | Acción |
|---|---|---|---|
| `purpose` | No — es un resumen del artículo que ya se describe en `metadata.description` | Ninguno | **ELIMINAR** |
| `topic` | Sí — etiqueta temática | `metadata.topic` | **MOVER a metadata** |
| `subtopics` | Sí — lista de subtemas | `metadata.subtopics` | **MOVER a metadata** |
| `required_data_summary` | No — es un resumen de `details.required_data.must_have` | Ninguno | **ELIMINAR** |
| `key_business_rules` | No — es un subconjunto de `details.business_rules[].rules` | Ninguno | **ELIMINAR** |
| `key_steps_summary` | No — es un resumen de `details.steps[]` | Ninguno | **ELIMINAR** |
| `high_impact_faq_pairs` | No — es un subconjunto de `details.faq_pairs[]` | Ninguno | **ELIMINAR** |
| `plan_specific_guardrails` | No — está cubierto por `details.guardrails.must_not[]` | Ninguno | **ELIMINAR** |
| `critical_flags` | **SÍ** — datos de routing/validación que NO existen en otro lugar | `details.critical_flags` | **MOVER a details** |

---

## Instrucciones paso a paso

### Pre-requisito: Identificar artículos a migrar

El agente debe identificar si un artículo JSON tiene la estructura vieja verificando:

```
¿El JSON tiene la key "summary" a nivel top-level?
  → SÍ: necesita migración
  → NO: ya está en estructura nueva, no hacer nada
```

### Paso 1: Leer el artículo completo

Cargar el archivo JSON completo y parsearlo. Verificar que tiene exactamente 3 keys top-level: `metadata`, `summary`, `details`.

Si no tiene estas 3 keys, el artículo tiene una estructura desconocida — **no proceder** y reportar el error.

### Paso 2: Extraer `topic` y `subtopics` del summary

Leer los valores de:
- `summary.topic` → puede ser `string`, `null`, o `""` (string vacío)
- `summary.subtopics` → puede ser `string[]` o `[]`

**Reglas de limpieza:**
- Si `summary.topic` es `""` (string vacío), convertir a `null`
- Si `summary.subtopics` es `[]`, mantener como `[]`
- No modificar los valores, solo copiarlos tal cual (excepto la regla de string vacío)

### Paso 3: Agregar `topic` y `subtopics` a `metadata`

Insertar las dos keys nuevas en el objeto `metadata`:

```json
"metadata": {
    "article_id": "...",
    "title": "...",
    "description": "...",
    ... (todas las keys existentes se preservan sin cambios) ...
    "source_system": "...",
    "topic": "<valor extraído de summary.topic>",
    "subtopics": ["<valores extraídos de summary.subtopics>"]
}
```

**Ubicación recomendada**: al final del objeto `metadata`, después de `source_system`, para mantener consistencia con los artículos ya migrados.

**IMPORTANTE**: NO modificar ninguna otra key de `metadata`. Solo AGREGAR `topic` y `subtopics`.

### Paso 4: Extraer `critical_flags` del summary

Leer el objeto completo:
- `summary.critical_flags` → siempre debe ser un objeto con 3 keys:
  - `portal_required` (boolean)
  - `mfa_relevant` (boolean)
  - `record_keeper_must_be` (string | null)

**Si `summary.critical_flags` no existe o está vacío**: Crear un objeto con valores por defecto:
```json
"critical_flags": {
    "portal_required": false,
    "mfa_relevant": false,
    "record_keeper_must_be": null
}
```

### Paso 5: Agregar `critical_flags` a `details`

Insertar `critical_flags` como la **primera key** dentro de `details`:

```json
"details": {
    "critical_flags": {
        "portal_required": true,
        "mfa_relevant": false,
        "record_keeper_must_be": "LT Trust"
    },
    "business_rules": [...],
    ... (todo lo demás de details se preserva sin cambios) ...
}
```

**Ubicación**: como primera key de `details`, antes de `business_rules`, para que sea fácil de localizar visualmente.

**IMPORTANTE**: NO modificar ninguna otra key de `details`. Solo AGREGAR `critical_flags`.

### Paso 6: Eliminar la sección `summary` completa

Eliminar la key `"summary"` y todo su contenido del JSON.

El resultado debe tener exactamente **2 keys top-level**:

```json
{
    "metadata": { ... },
    "details": { ... }
}
```

### Paso 7: Validar el JSON resultante

Ejecutar las siguientes validaciones:

#### 7.1 — Validación estructural

| # | Check | Esperado |
|---|---|---|
| 1 | Top-level keys | Exactamente `["metadata", "details"]` |
| 2 | `"summary"` no existe | `"summary" not in json_data` |
| 3 | `metadata.topic` existe | `"topic" in metadata` |
| 4 | `metadata.subtopics` existe y es array | `isinstance(metadata["subtopics"], list)` |
| 5 | `details.critical_flags` existe | `"critical_flags" in details` |
| 6 | `critical_flags` tiene las 3 subkeys | `portal_required`, `mfa_relevant`, `record_keeper_must_be` |
| 7 | JSON es válido | Se puede parsear sin errores |

#### 7.2 — Validación de integridad

| # | Check | Esperado |
|---|---|---|
| 8 | `metadata.article_id` sin cambios | Igual que el original |
| 9 | `metadata.title` sin cambios | Igual que el original |
| 10 | `details.business_rules` sin cambios | Igual que el original |
| 11 | `details.steps` sin cambios | Igual que el original |
| 12 | `details.faq_pairs` sin cambios | Igual que el original |
| 13 | `details.guardrails` sin cambios | Igual que el original |
| 14 | Ninguna key de details fue eliminada | Todas las keys originales de details persisten |

### Paso 8: Guardar el archivo

Sobrescribir el archivo JSON original con el JSON migrado, preservando:
- Encoding: UTF-8
- Indentación: consistente (4 espacios o 2 espacios, según el archivo original)
- Sin trailing newlines extra

### Paso 9: Verificar con dry-run (si aplica)

Si el pipeline de procesamiento está disponible, ejecutar:

```bash
cd "/Users/ivanalvis/Desktop/FUA Knowledge Base Articles"
source kb-rag-system/venv/bin/activate

python kb-rag-system/scripts/process_single_article.py \
  "<ruta al artículo>" \
  --dry-run --show-chunks
```

**Verificar:**
- No hay errores de ejecución
- `topic` aparece en la metadata de cada chunk
- El chunk de `critical_flags` existe y tiene los valores correctos
- No hay chunks tipo `steps_summary` o `high_impact` (esos ya no se generan)

---

## Ejemplo completo de transformación

### ANTES (estructura vieja con summary):

```json
{
    "metadata": {
        "article_id": "example_article_id",
        "title": "Example Article Title",
        "description": "Description of the article.",
        "audience": "Internal AI Support Agent",
        "record_keeper": "LT Trust",
        "plan_type": "401(k)",
        "scope": "recordkeeper-specific",
        "tags": ["Distribution", "Participant Advisory"],
        "language": "en-US",
        "last_updated": null,
        "schema_version": "kb_article_v2",
        "transformed_at": "2026-01-15",
        "source_last_updated": null,
        "source_system": "DevRev"
    },
    "summary": {
        "purpose": "Help an agent do something...",
        "topic": "some_topic",
        "subtopics": ["subtopic_a", "subtopic_b"],
        "required_data_summary": ["(participant_profile) Some data point"],
        "key_business_rules": ["Rule 1", "Rule 2"],
        "key_steps_summary": ["Step 1 summary", "Step 2 summary"],
        "high_impact_faq_pairs": [
            {"question": "Q1?", "answer": "A1."}
        ],
        "plan_specific_guardrails": ["Don't do X."],
        "critical_flags": {
            "portal_required": true,
            "mfa_relevant": false,
            "record_keeper_must_be": "LT Trust"
        }
    },
    "details": {
        "business_rules": [...],
        "fees": [...],
        "steps": [...],
        ...
    }
}
```

### DESPUÉS (estructura nueva sin summary):

```json
{
    "metadata": {
        "article_id": "example_article_id",
        "title": "Example Article Title",
        "description": "Description of the article.",
        "audience": "Internal AI Support Agent",
        "record_keeper": "LT Trust",
        "plan_type": "401(k)",
        "scope": "recordkeeper-specific",
        "tags": ["Distribution", "Participant Advisory"],
        "language": "en-US",
        "last_updated": null,
        "schema_version": "kb_article_v2",
        "transformed_at": "2026-01-15",
        "source_last_updated": null,
        "source_system": "DevRev",
        "topic": "some_topic",
        "subtopics": ["subtopic_a", "subtopic_b"]
    },
    "details": {
        "critical_flags": {
            "portal_required": true,
            "mfa_relevant": false,
            "record_keeper_must_be": "LT Trust"
        },
        "business_rules": [...],
        "fees": [...],
        "steps": [...],
        ...
    }
}
```

### Qué cambió:

1. `summary.topic` → copiado a `metadata.topic`
2. `summary.subtopics` → copiado a `metadata.subtopics`
3. `summary.critical_flags` → copiado a `details.critical_flags` (primera key de details)
4. `summary` → **eliminado por completo** (purpose, required_data_summary, key_business_rules, key_steps_summary, high_impact_faq_pairs, plan_specific_guardrails — todo eliminado)
5. Todo lo demás en `metadata` y `details` → **sin cambios**

---

## Script de referencia (Python)

Si el agente necesita una implementación de referencia, aquí está el patrón:

```python
import json

def migrate_article(file_path: str) -> None:
    """Migra un artículo de estructura vieja (con summary) a nueva (sin summary)."""
    
    # 1. Leer
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 2. Verificar que necesita migración
    if 'summary' not in data:
        print(f"SKIP: {file_path} — ya migrado (no tiene summary)")
        return
    
    if set(data.keys()) != {'metadata', 'summary', 'details'}:
        print(f"ERROR: {file_path} — estructura inesperada: {list(data.keys())}")
        return
    
    summary = data['summary']
    
    # 3. Mover topic a metadata
    topic = summary.get('topic')
    if topic == '':
        topic = None
    data['metadata']['topic'] = topic
    
    # 4. Mover subtopics a metadata
    data['metadata']['subtopics'] = summary.get('subtopics', [])
    
    # 5. Mover critical_flags a details
    critical_flags = summary.get('critical_flags', {
        'portal_required': False,
        'mfa_relevant': False,
        'record_keeper_must_be': None
    })
    
    # Insertar critical_flags como primera key de details
    new_details = {'critical_flags': critical_flags}
    new_details.update(data['details'])
    data['details'] = new_details
    
    # 6. Eliminar summary
    del data['summary']
    
    # 7. Validar
    assert set(data.keys()) == {'metadata', 'details'}, f"Keys incorrectas: {list(data.keys())}"
    assert 'topic' in data['metadata'], "Falta topic en metadata"
    assert 'subtopics' in data['metadata'], "Falta subtopics en metadata"
    assert isinstance(data['metadata']['subtopics'], list), "subtopics no es array"
    assert 'critical_flags' in data['details'], "Falta critical_flags en details"
    cf = data['details']['critical_flags']
    assert 'portal_required' in cf, "Falta portal_required"
    assert 'mfa_relevant' in cf, "Falta mfa_relevant"
    assert 'record_keeper_must_be' in cf, "Falta record_keeper_must_be"
    
    # 8. Guardar
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    
    print(f"OK: {file_path} — migrado exitosamente")
    print(f"   topic: {data['metadata']['topic']}")
    print(f"   subtopics: {data['metadata']['subtopics']}")
    print(f"   critical_flags: {data['details']['critical_flags']}")
```

---

## Edge cases y decisiones

### 1. `summary.topic` es string vacío `""`

**Decisión**: Convertir a `null`. Un topic vacío no tiene valor semántico. El pipeline acepta `null` para topic.

### 2. `summary.subtopics` es `[]`

**Decisión**: Mantener como `[]`. Es un array válido que indica "no hay subtopics definidos".

### 3. `summary.critical_flags` no existe

**Decisión**: Crear con valores por defecto (`portal_required: false`, `mfa_relevant: false`, `record_keeper_must_be: null`). Esto es conservador y seguro.

### 4. El artículo ya tiene `metadata.topic` (posible duplicado)

**Decisión**: Si `metadata.topic` ya existe y `summary.topic` también existe, usar el valor de `summary.topic` (es la fuente canónica bajo la estructura vieja). Sobrescribir.

### 5. El artículo ya tiene `details.critical_flags`

**Decisión**: Si `details.critical_flags` ya existe y `summary.critical_flags` también existe, usar el valor de `summary.critical_flags` (es la fuente canónica). Sobrescribir.

### 6. El artículo no tiene sección `summary`

**Decisión**: Ya está migrado. No hacer nada. Reportar "SKIP" y continuar.

### 7. El artículo tiene keys top-level inesperadas (ni `metadata`+`summary`+`details` ni `metadata`+`details`)

**Decisión**: No proceder. Reportar error y requerir revisión manual.

---

## Artículos ya migrados (referencia)

Los siguientes artículos ya fueron migrados exitosamente el 2026-02-10 y pueden usarse como referencia de la estructura nueva:

| Archivo | article_id | topic | critical_flags |
|---|---|---|---|
| `hardship_withdrawal_overview.json` | `forusall_401k_hardship_withdrawal_complete_guide` | `hardship_withdrawal` | `{portal: false, mfa: false, rk: ForUsAll}` |
| `LT: Completing Your Rollover Online – Best Practices.json` | `lt_completing_your_rollover_online_best_practices` | `rollover_online_flow` | `{portal: true, mfa: false, rk: LT Trust}` |
| `LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json` | `lt_request_401k_termination_withdrawal_or_rollover` | `termination_distribution_request` | `{portal: true, mfa: true, rk: LT Trust}` |

---

## Checklist de ejecución por artículo

Para cada artículo a migrar, el agente debe verificar:

- [ ] Artículo tiene key `"summary"` (necesita migración)
- [ ] `summary.topic` extraído y copiado a `metadata.topic`
- [ ] `summary.subtopics` extraído y copiado a `metadata.subtopics`
- [ ] `summary.critical_flags` extraído y copiado a `details.critical_flags`
- [ ] `summary` eliminado completamente del JSON
- [ ] JSON resultante tiene exactamente 2 keys top-level: `metadata`, `details`
- [ ] JSON es válido (parseable sin errores)
- [ ] Ninguna key de `metadata` original fue eliminada o modificada
- [ ] Ninguna key de `details` original fue eliminada o modificada
- [ ] (Opcional) Dry-run del pipeline ejecutado sin errores

---

## Después de migrar: Re-procesar en Pinecone

Si el artículo ya estaba en Pinecone con la estructura vieja, debe re-procesarse:

```bash
cd "/Users/ivanalvis/Desktop/FUA Knowledge Base Articles"
source kb-rag-system/venv/bin/activate

# 1. Verificar article_id en el metadata del JSON
# 2. Eliminar artículo viejo de Pinecone
python kb-rag-system/scripts/delete_article.py "<article_id>"

# 3. Subir artículo actualizado
python kb-rag-system/scripts/process_single_article.py "<ruta al JSON>"

# 4. Verificar
python kb-rag-system/scripts/verify_article.py "<article_id>"
```

**Nota**: Si el artículo es nuevo y nunca fue subido a Pinecone, solo ejecutar los pasos 3 y 4.
