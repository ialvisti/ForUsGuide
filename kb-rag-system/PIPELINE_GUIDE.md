# Guía del Pipeline - Procesamiento de Artículos

## 📋 Tabla de Contenidos

1. [Introducción](#introducción)
2. [Pipeline Automático](#pipeline-automático)
3. [Procesar un Artículo Nuevo](#procesar-un-artículo-nuevo)
4. [Procesar Múltiples Artículos](#procesar-múltiples-artículos)
5. [Actualizar un Artículo Existente](#actualizar-un-artículo-existente)
6. [Eliminar un Artículo](#eliminar-un-artículo)
7. [Scripts Disponibles](#scripts-disponibles)
8. [Configuración Avanzada](#configuración-avanzada)
9. [Troubleshooting](#troubleshooting)

---

## Introducción

Este documento explica cómo procesar artículos JSON de la Knowledge Base y subirlos a Pinecone. El sistema está diseñado para ser **simple y automatizado**.

### ¿Qué hace el Pipeline?

```
Artículo JSON → Chunking → Embeddings → Pinecone
```

1. **Lee** el artículo JSON
2. **Genera** ~30-35 chunks con metadata
3. **Crea** embeddings automáticamente (Pinecone lo hace)
4. **Sube** los chunks a Pinecone
5. **Valida** que se subieron correctamente

---

## Pipeline Automático

### Opción 1: Procesar UN artículo nuevo

```bash
cd kb-rag-system

# Activar virtual environment
source venv/bin/activate

# Ejecutar pipeline para un artículo
python scripts/process_single_article.py "../Participant Advisory/Distributions/NEW_ARTICLE.json"
```

**Output esperado:**
```
✅ Artículo cargado: [Título del artículo]
🔨 Generando chunks...
✅ 33 chunks generados
📤 Subiendo a Pinecone...
✅ Chunks subidos exitosamente
✅ Pipeline completado
```

---

### Opción 2: Procesar TODOS los artículos

```bash
cd kb-rag-system
source venv/bin/activate

# Procesar todos los .json en directorio
python scripts/load_all_articles.py
```

**Output esperado:**
```
📂 Buscando artículos en: ../Participant Advisory/
✅ Encontrados 280 artículos

Procesando: [1/280] LT: How to Request...
  ✅ 33 chunks generados
  ✅ Subidos a Pinecone

Procesando: [2/280] Vanguard: Loan Application...
  ✅ 31 chunks generados
  ✅ Subidos a Pinecone

...

✅ COMPLETADO
  Total artículos: 280
  Total chunks: 8,523
  Tiempo: 12m 34s
```

---

### Opción 3: Modo Watch (Automático)

Para entornos de producción, puedes dejar un proceso corriendo que detecte artículos nuevos automáticamente:

```bash
cd kb-rag-system
source venv/bin/activate

# Monitorear directorio y procesar automáticamente
python scripts/watch_articles.py --directory "../Participant Advisory" --interval 60
```

**Qué hace:**
- Monitorea el directorio cada 60 segundos
- Detecta archivos `.json` nuevos o modificados
- Los procesa automáticamente
- Loguea todo en `logs/pipeline.log`

---

## Procesar un Artículo Nuevo

### Paso a Paso

#### 1. Crear el artículo JSON

Coloca tu nuevo artículo en el directorio apropiado:

```
Participant Advisory/
  └── Distributions/
      └── NUEVO_ARTICULO.json
```

**Requisitos del JSON:**
- Debe tener las secciones: `metadata`, `summary`, `details`
- Debe incluir: `article_id`, `title`, `record_keeper`, `plan_type`

#### 2. Validar estructura

```bash
python scripts/validate_article.py "../Participant Advisory/Distributions/NUEVO_ARTICULO.json"
```

**Output si es válido:**
```
✅ Estructura válida
✅ Metadata completa
✅ Secciones requeridas presentes
```

#### 3. Procesar y subir

```bash
python scripts/process_single_article.py "../Participant Advisory/Distributions/NUEVO_ARTICULO.json"
```

#### 4. Verificar en Pinecone

```bash
python scripts/verify_article.py "article_id_del_nuevo_articulo"
```

**Output esperado:**
```
✅ Artículo encontrado en Pinecone
  Chunks: 33
  Record keeper: LT Trust
  Plan type: 401(k)
  
Chunks por tier:
  CRITICAL: 9
  HIGH: 10
  MEDIUM: 5
  LOW: 9
```

---

## Procesar Múltiples Artículos

### Escenario: Tienes 50 artículos nuevos

```bash
# Opción A: Procesar todos de una vez
python scripts/load_all_articles.py --directory "../Participant Advisory/Distributions" --new-only

# Opción B: Procesar por lotes (más seguro)
python scripts/batch_process.py --directory "../Participant Advisory/Distributions" --batch-size 10
```

**Ventajas del procesamiento por lotes:**
- Si falla uno, los demás continúan
- Menos carga en Pinecone
- Mejor logging y tracking
- Más fácil de pausar/reanudar

---

## Actualizar un Artículo Existente

### ¿Cuándo actualizar?

- El contenido del artículo cambió
- Se agregaron nuevas secciones
- Se corrigieron errores
- Se actualizaron fees o reglas

### Proceso de Actualización

#### 1. Modificar el artículo JSON

Edita el archivo JSON con los cambios necesarios.

#### 2. Eliminar chunks antiguos

```bash
python scripts/delete_article_chunks.py "article_id_del_articulo"
```

**Output:**
```
🔍 Buscando chunks de: article_id_del_articulo
✅ Encontrados 33 chunks
🗑️  Eliminando chunks...
✅ 33 chunks eliminados
```

#### 3. Procesar versión actualizada

```bash
python scripts/process_single_article.py "../Path/To/ARTICULO_ACTUALIZADO.json"
```

#### 4. Verificar actualización

```bash
python scripts/verify_article.py "article_id_del_articulo"
```

### Actualización Automática (Un solo comando)

```bash
python scripts/update_article.py "../Path/To/ARTICULO_ACTUALIZADO.json"
```

Esto hace internamente:
1. Lee el `article_id` del JSON
2. Elimina chunks antiguos
3. Procesa y sube nueva versión
4. Verifica que todo esté OK

---

## Eliminar un Artículo

### ¿Cuándo eliminar?

- El artículo ya no es válido
- Se deprecó por uno nuevo
- Contenía información incorrecta
- Plan o recordkeeper se descontinuó

### Proceso de Eliminación

```bash
# Eliminar por article_id
python scripts/delete_article_chunks.py "article_id_a_eliminar"

# O eliminar por archivo (lee el article_id del JSON)
python scripts/delete_article_by_file.py "../Path/To/ARTICULO.json"
```

**Confirmación:**
```
⚠️  ADVERTENCIA: Esto eliminará todos los chunks de:
  Article ID: lt_request_401k_withdrawal
  Title: LT: How to Request a 401(k)...
  Chunks estimados: 33

¿Continuar? (yes/no): yes

🗑️  Eliminando...
✅ 33 chunks eliminados exitosamente
```

---

## Scripts Disponibles

### Tabla Rápida

| Script | Propósito | Uso |
|--------|-----------|-----|
| `process_single_article.py` | Procesar 1 artículo | Artículos nuevos |
| `load_all_articles.py` | Procesar todos | Setup inicial o bulk update |
| `update_article.py` | Actualizar artículo existente | Cambios en contenido |
| `delete_article_chunks.py` | Eliminar chunks de artículo | Deprecación |
| `validate_article.py` | Validar estructura JSON | Pre-procesamiento |
| `verify_article.py` | Verificar en Pinecone | Post-procesamiento |
| `watch_articles.py` | Monitorear y auto-procesar | Producción |
| `batch_process.py` | Procesar por lotes | Bulk seguro |
| `list_articles_in_pinecone.py` | Listar artículos en DB | Inventario |
| `reprocess_failed.py` | Reprocesar fallos | Recovery |

---

### 1. `process_single_article.py`

**Uso:**
```bash
python scripts/process_single_article.py <path-to-json>

# Ejemplos
python scripts/process_single_article.py "../Participant Advisory/Distributions/NEW.json"
python scripts/process_single_article.py --file "../Loans/LOAN_ARTICLE.json" --dry-run
```

**Opciones:**
- `--dry-run` - Solo genera chunks, no sube a Pinecone
- `--verbose` - Output detallado
- `--show-chunks` - Muestra chunks generados

---

### 2. `load_all_articles.py`

**Uso:**
```bash
python scripts/load_all_articles.py [--directory <dir>] [--new-only] [--skip-existing]

# Ejemplos
python scripts/load_all_articles.py
python scripts/load_all_articles.py --directory "../Participant Advisory/Loans"
python scripts/load_all_articles.py --new-only --skip-existing
```

**Opciones:**
- `--directory` - Directorio a escanear (default: ../Participant Advisory)
- `--new-only` - Solo artículos no procesados previamente
- `--skip-existing` - Skip artículos ya en Pinecone
- `--parallel` - Procesar en paralelo (más rápido)

---

### 3. `update_article.py`

**Uso:**
```bash
python scripts/update_article.py <path-to-json>

# Ejemplo
python scripts/update_article.py "../Participant Advisory/Distributions/UPDATED.json"
```

**Proceso:**
1. Extrae `article_id` del JSON
2. Busca y elimina chunks existentes
3. Genera nuevos chunks
4. Sube a Pinecone
5. Verifica

---

### 4. `watch_articles.py`

**Uso:**
```bash
python scripts/watch_articles.py --directory <dir> --interval <seconds>

# Ejemplo
python scripts/watch_articles.py --directory "../Participant Advisory" --interval 60
```

**Qué monitorea:**
- Archivos `.json` nuevos → Procesa automáticamente
- Archivos `.json` modificados → Actualiza automáticamente
- Archivos `.json` eliminados → Elimina chunks de Pinecone

**Log:**
```
[2026-01-18 10:30:15] INFO: Monitoring: ../Participant Advisory
[2026-01-18 10:31:20] INFO: New file detected: NEW_ARTICLE.json
[2026-01-18 10:31:22] INFO: Processing...
[2026-01-18 10:31:45] INFO: ✅ Processed successfully (33 chunks)
[2026-01-18 10:32:30] INFO: Modified file detected: EXISTING.json
[2026-01-18 10:32:32] INFO: Updating...
[2026-01-18 10:32:55] INFO: ✅ Updated successfully (33 chunks)
```

---

### 5. `verify_article.py`

**Uso:**
```bash
python scripts/verify_article.py <article_id>

# Ejemplo
python scripts/verify_article.py "lt_request_401k_termination_withdrawal_or_rollover"
```

**Output:**
```
🔍 Verificando artículo: lt_request_401k...

✅ Artículo encontrado en Pinecone

Información:
  Title: LT: How to Request a 401(k)...
  Record keeper: LT Trust
  Plan type: 401(k)
  Total chunks: 33

Chunks por tier:
  CRITICAL: 9 chunks
    - required_data (data_collection)
    - eligibility (requirements)
    - critical_flags (validation)
    ...
  
  HIGH: 10 chunks
    - steps (steps_1_to_3)
    - fees_details (costs)
    ...

Metadata consistente: ✅
Todos los chunks tienen article_id correcto: ✅
```

---

## Configuración Avanzada

### Variables de Entorno (`.env`)

```bash
# Pinecone
PINECONE_API_KEY=your-api-key
INDEX_NAME=kb-articles-production
NAMESPACE=kb_articles

# Processing
BATCH_SIZE=96                 # Max chunks por batch (Pinecone limit)
MAX_RETRIES=3                 # Reintentos si falla upload
RETRY_DELAY=2                 # Segundos entre reintentos

# Monitoring
LOG_LEVEL=INFO                # DEBUG, INFO, WARNING, ERROR
LOG_FILE=logs/pipeline.log

# Watch mode
WATCH_INTERVAL=60             # Segundos entre scans
WATCH_RECURSIVE=true          # Buscar en subdirectorios
```

---

### Configuración de Chunking

Si necesitas ajustar el chunking (archivo `data_pipeline/chunking.py`):

```python
# Cambiar tamaño de agrupación de steps
def _group_steps(self, steps):
    chunk_size = 3  # ← Cambiar aquí (default: 3 pasos por chunk)
    ...

# Cambiar agrupación de FAQs
def _group_faqs(self, faqs):
    chunk_size = 3  # ← Cambiar aquí (default: 3 FAQs por chunk)
    ...
```

**Consideraciones:**
- Chunks más pequeños → Búsqueda más precisa, pero más vectores
- Chunks más grandes → Menos vectores, pero búsqueda menos precisa
- Balance recomendado: 200-500 palabras por chunk

---

### Configuración de Batch Processing

Archivo `scripts/batch_process.py`:

```python
# Configurar tamaño de lotes
BATCH_SIZE = 10  # Procesar 10 artículos a la vez

# Configurar paralelismo
MAX_WORKERS = 4  # Procesar 4 artículos en paralelo

# Configurar delay entre lotes
BATCH_DELAY = 5  # 5 segundos entre lotes
```

---

## Troubleshooting

### Problema 1: "Article not found"

**Síntoma:**
```
❌ Error: Archivo no encontrado: ../Path/To/ARTICLE.json
```

**Solución:**
- Verifica la ruta del archivo
- Usa rutas absolutas si hay duda:
  ```bash
  python scripts/process_single_article.py "/Users/user/Desktop/FUA Knowledge Base Articles/Participant Advisory/Distributions/ARTICLE.json"
  ```

---

### Problema 2: "Invalid JSON structure"

**Síntoma:**
```
❌ Error: Artículo inválido
  Sección faltante: metadata
```

**Solución:**
1. Validar estructura:
   ```bash
   python scripts/validate_article.py "../Path/To/ARTICLE.json"
   ```
2. Asegurar que el JSON tiene:
   - `metadata` con `article_id`, `title`, `record_keeper`, `plan_type`
   - `summary`
   - `details`

---

### Problema 3: "Pinecone connection failed"

**Síntoma:**
```
❌ Error: Failed to connect to Pinecone
  Status code: 401
```

**Solución:**
1. Verificar API key en `.env`:
   ```bash
   cat .env | grep PINECONE_API_KEY
   ```
2. Verificar que el índice existe:
   ```bash
   python scripts/check_pinecone_connection.py
   ```
3. Si el índice no existe, crearlo:
   ```bash
   bash scripts/setup_index.sh
   ```

---

### Problema 4: "Batch upload failed"

**Síntoma:**
```
❌ Error: Batch upload failed
  Failed chunks: 15/96
```

**Solución:**
1. Revisar tamaño de chunks (no debe exceder 2MB por batch)
2. Reducir batch size en `.env`:
   ```bash
   BATCH_SIZE=50  # Reducir de 96 a 50
   ```
3. Reintentar con:
   ```bash
   python scripts/reprocess_failed.py
   ```

---

### Problema 5: "Duplicate chunks detected"

**Síntoma:**
```
⚠️  Warning: Duplicate chunks detected
  Article: lt_request_401k...
  Duplicates: 33 chunks already exist
```

**Solución:**
1. Si quieres reemplazar, eliminar primero:
   ```bash
   python scripts/delete_article_chunks.py "article_id"
   python scripts/process_single_article.py "../Path/To/ARTICLE.json"
   ```
2. Si quieres mantener existentes, usar `--skip-existing`:
   ```bash
   python scripts/load_all_articles.py --skip-existing
   ```

---

### Problema 6: "Out of memory"

**Síntoma:**
```
❌ Error: MemoryError
  Processing large batch...
```

**Solución:**
1. Procesar en lotes más pequeños:
   ```bash
   python scripts/batch_process.py --batch-size 5
   ```
2. Deshabilitar paralelismo:
   ```bash
   python scripts/load_all_articles.py --no-parallel
   ```

---

## Best Practices

### 1. Siempre Validar Antes de Procesar

```bash
# Validar primero
python scripts/validate_article.py "../Path/To/NEW.json"

# Si es válido, procesar
python scripts/process_single_article.py "../Path/To/NEW.json"
```

### 2. Usar Dry-Run para Testing

```bash
# Ver qué chunks se generarían sin subir a Pinecone
python scripts/process_single_article.py "../Path/To/NEW.json" --dry-run --show-chunks
```

### 3. Backup Antes de Bulk Updates

```bash
# Exportar artículos actuales de Pinecone
python scripts/export_all_chunks.py --output backup_$(date +%Y%m%d).json

# Luego procesar
python scripts/load_all_articles.py
```

### 4. Monitorear Logs en Producción

```bash
# Tail logs en tiempo real
tail -f logs/pipeline.log

# Buscar errores
grep "ERROR" logs/pipeline.log

# Ver resumen
python scripts/analyze_logs.py
```

### 5. Verificar Después de Cambios

```bash
# Después de actualizar un artículo
python scripts/verify_article.py "article_id"

# Después de bulk update
python scripts/verify_all_articles.py
```

---

## Integración con CI/CD

### Ejemplo: GitHub Actions

```yaml
name: Process New KB Articles

on:
  push:
    paths:
      - 'Participant Advisory/**/*.json'

jobs:
  process-articles:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      
      - name: Setup Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.12'
      
      - name: Install dependencies
        run: |
          cd kb-rag-system
          pip install -r requirements.txt
      
      - name: Process changed articles
        env:
          PINECONE_API_KEY: ${{ secrets.PINECONE_API_KEY }}
        run: |
          cd kb-rag-system
          python scripts/process_changed_articles.py
```

---

## Resumen de Comandos Frecuentes

```bash
# Setup inicial (una vez)
bash scripts/setup_index.sh
python scripts/load_all_articles.py

# Artículo nuevo
python scripts/process_single_article.py "../Path/To/NEW.json"

# Actualizar artículo
python scripts/update_article.py "../Path/To/UPDATED.json"

# Eliminar artículo
python scripts/delete_article_chunks.py "article_id"

# Verificar artículo
python scripts/verify_article.py "article_id"

# Modo watch (producción)
python scripts/watch_articles.py --directory "../Participant Advisory" --interval 60
```

---

**Próximos Pasos:** Ver `ARCHITECTURE.md` para entender cómo funciona el sistema completo.
