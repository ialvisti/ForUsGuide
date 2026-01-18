# Fase 4: Pinecone & Pipeline de Procesamiento

**Estado:** 🔄 EN PROGRESO (80% completada)  
**Duración estimada:** 1.5-2 horas  
**Fecha inicio:** 2026-01-18

---

## Objetivo

Crear índice en Pinecone y pipeline completo para procesar artículos JSON y subirlos como chunks vectorizados.

---

## ✅ Completado

### 1. Índice Creado en Pinecone

```bash
bash scripts/setup_index.sh
```

**Configuración:**
```
Name: kb-articles-production
Dimension: 1024
Metric: cosine
Cloud: aws
Region: us-east-1
Model: llama-text-embed-v2
Field Map: text=content
State: Ready ✅
```

### 2. Scripts Creados

- ✅ `scripts/setup_index.sh` - Crear índice
- ✅ `data_pipeline/pinecone_uploader.py` - Módulo de carga
- ✅ `scripts/process_single_article.py` - Procesar 1 artículo
- ✅ `scripts/verify_article.py` - Verificar artículo

---

## ⚠️ PROBLEMA ACTUAL

### Error al Subir Chunks

```
Vector dimension 0 does not match the dimension of the index 1024
```

### Causa

Los índices de Pinecone con **embeddings integrados** requieren formato especial de upsert que NO incluye vectores explícitos.

**Lo que estábamos haciendo (INCORRECTO):**
```python
record = {
    "id": chunk["id"],
    "values": [],  # ❌ INCORRECTO para embeddings integrados
    "metadata": {...}
}
```

**Lo que necesitamos hacer (CORRECTO):**
```python
record = {
    "id": chunk["id"],
    "data": {
        "content": chunk["content"]  # ✅ Pinecone genera embeddings de esto
    },
    "metadata": {
        # Metadata sin el content
    }
}
```

---

## 🔧 SOLUCIÓN COMPLETA

### Paso 1: Corregir `pinecone_uploader.py`

**Archivo:** `data_pipeline/pinecone_uploader.py`

**Buscar el método `_upload_batch` (línea ~140):**

```python
def _upload_batch(self, batch: List[Dict[str, Any]]) -> bool:
    """Sube un batch de chunks con retry logic."""
    # Preparar records para Pinecone
    records = []
    for chunk in batch:
        record = {
            "id": chunk["id"],
            "values": [],  # ❌ ESTA LÍNEA ES EL PROBLEMA
            "metadata": {
                **chunk["metadata"],
                "content": chunk["content"]
            }
        }
        records.append(record)
    
    # ... resto del código
```

**REEMPLAZAR CON:**

```python
def _upload_batch(self, batch: List[Dict[str, Any]]) -> bool:
    """
    Sube un batch de chunks con retry logic.
    
    IMPORTANTE: Para índices con embeddings integrados (model + field_map),
    Pinecone espera formato con 'data' en vez de 'values'.
    """
    # Preparar records para Pinecone con embeddings integrados
    records = []
    for chunk in batch:
        # Para embeddings integrados: usar 'data' con el contenido
        # Pinecone generará el embedding del campo 'content'
        record = {
            "id": chunk["id"],
            "data": {
                "content": chunk["content"]  # Campo que Pinecone embedirá
            },
            "metadata": chunk["metadata"]  # Metadata SIN el content duplicado
        }
        records.append(record)
    
    # Intentar upload con retries
    for attempt in range(self.max_retries):
        try:
            # Upsert usando inference API (para embeddings integrados)
            self.index.upsert(
                vectors=records,
                namespace=self.namespace
            )
            return True
            
        except Exception as e:
            logger.warning(f"Intento {attempt + 1}/{self.max_retries} falló: {e}")
            
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay * (attempt + 1))
            else:
                logger.error(f"Batch falló después de {self.max_retries} intentos")
                return False
```

**CAMBIOS CLAVE:**
1. ❌ Eliminar `"values": []`
2. ✅ Agregar `"data": {"content": chunk["content"]}`
3. ✅ Metadata NO debe incluir content duplicado

---

### Paso 2: Probar la Corrección

```bash
cd kb-rag-system
source venv/bin/activate

# Procesar artículo de prueba
python scripts/process_single_article.py \
  "../Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"
```

**Output esperado:**
```
✅ Artículo cargado: LT: How to Request...
🔨 Generando chunks...
✅ 33 chunks generados
📤 Subiendo chunks a Pinecone...
Uploading: 100%|██████████| 1/1 [00:02<00:00]
✅ Todos los chunks se subieron exitosamente
✅ PROCESAMIENTO COMPLETADO
```

---

### Paso 3: Verificar en Pinecone

```bash
python scripts/verify_article.py \
  "lt_request_401k_termination_withdrawal_or_rollover"
```

**Output esperado:**
```
✅ Artículo encontrado en Pinecone
  Total chunks: 33
  CRITICAL: 9 chunks
  HIGH: 10 chunks
  MEDIUM: 5 chunks
  LOW: 9 chunks
✅ Todos los chunks tienen el mismo article_id
✅ Todos los chunks críticos presentes
```

---

## Próximos Scripts a Crear

Una vez funcionando el upload, crear:

### 1. `scripts/load_all_articles.py`

Procesar todos los artículos del directorio.

```bash
python scripts/load_all_articles.py \
  --directory "../Participant Advisory"
```

### 2. `scripts/delete_article_chunks.py`

Eliminar chunks de un artículo.

```bash
python scripts/delete_article_chunks.py "article_id"
```

### 3. `scripts/update_article.py`

Actualizar un artículo existente (delete + process).

```bash
python scripts/update_article.py "../Path/To/ARTICLE.json"
```

---

## Referencia: Formato Pinecone con Embeddings Integrados

### Índice con Embeddings Integrados

```bash
pc index create \
  --name "my-index" \
  --metric "cosine" \
  --cloud "aws" \
  --region "us-east-1" \
  --model "llama-text-embed-v2" \      # ← Modelo de embeddings
  --field-map "text=content"            # ← Campo a embed
```

### Upsert con Embeddings Integrados

```python
# Formato CORRECTO
index.upsert(
    vectors=[
        {
            "id": "chunk_1",
            "data": {
                "content": "This text will be embedded"  # ← Pinecone embedirá esto
            },
            "metadata": {
                "article_id": "...",
                "chunk_type": "..."
            }
        }
    ],
    namespace="kb_articles"
)
```

### Query con Embeddings Integrados

```python
# Query también usa el campo de texto
results = index.query(
    data={"content": "search query text"},  # ← Pinecone embedirá esto
    top_k=10,
    include_metadata=True,
    namespace="kb_articles",
    filter={"article_id": {"$eq": "..."}}
)
```

---

## Troubleshooting

### Si el error persiste:

1. **Verificar versión de Pinecone SDK:**
```bash
pip show pinecone
# Debe ser >= 5.0.0
```

2. **Verificar configuración del índice:**
```bash
pc index describe --name kb-articles-production
# Verificar que tenga Model y Field Map
```

3. **Recrear índice si es necesario:**
```bash
pc index delete --name kb-articles-production
bash scripts/setup_index.sh
```

---

## Estado Actual

- ✅ Índice creado y configurado
- ✅ Scripts base creados
- ✅ Chunking funciona (33 chunks generados)
- ⚠️  Upload necesita corrección (código provisto arriba)
- ⏳ Scripts adicionales pendientes

---

## Próximo Paso

1. **Aplicar corrección** en `pinecone_uploader.py`
2. **Probar upload** con artículo de ejemplo
3. **Verificar** que chunks estén en Pinecone
4. **Crear scripts adicionales** del pipeline
5. **Pasar a Fase 5:** Implementación del RAG Engine

---

**Progreso:** 80% completado  
**Bloqueador actual:** Formato de upsert (solución provista)  
**Siguiente fase:** PHASE_5.md (RAG Engine)
