# Scripts de Gestión de Artículos

Scripts para gestionar artículos en Pinecone.

## Scripts Disponibles

### 1. `update_article.py` - 🔄 Actualizar Artículo (Recomendado)

**Uso más común.** Actualiza un artículo en Pinecone borrando la versión vieja y subiendo la nueva.

```bash
# Actualizar un artículo (pedirá confirmación)
python scripts/update_article.py "Participant Advisory/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json"

# Actualizar sin pedir confirmación
python scripts/update_article.py <path> --skip-confirmation

# Ver qué haría sin hacer cambios (dry-run)
python scripts/update_article.py <path> --dry-run

# Ver chunks generados
python scripts/update_article.py <path> --show-chunks
```

**Lo que hace:**
1. ✅ Lee el artículo JSON
2. 🔍 Busca la versión vieja en Pinecone
3. 📊 Muestra comparación (chunks viejos vs nuevos)
4. ⚠️ Pide confirmación
5. 🗑️ Borra la versión vieja
6. 📤 Sube la versión nueva
7. ✔️ Verifica que todo esté correcto

---

### 2. `delete_article.py` - 🗑️ Borrar Artículo

Borra un artículo de Pinecone por su article_id.

```bash
# Listar artículos disponibles
python scripts/delete_article.py --list

# Borrar un artículo específico
python scripts/delete_article.py lt_request_401k_termination_withdrawal_or_rollover
```

**Lo que hace:**
1. 🔍 Busca el artículo en Pinecone
2. 📄 Muestra información del artículo
3. ⚠️ Pide confirmación
4. 🗑️ Borra todos los chunks
5. ✔️ Verifica que se borró

**Uso típico:** Cuando quieres borrar un artículo sin reemplazarlo.

---

### 3. `process_single_article.py` - 📤 Procesar Artículo Nuevo

Procesa y sube un artículo **nuevo** a Pinecone (sin borrar nada).

```bash
# Procesar un artículo nuevo
python scripts/process_single_article.py <path-to-json>

# Ver chunks sin subir (dry-run)
python scripts/process_single_article.py <path> --dry-run

# Mostrar chunks generados
python scripts/process_single_article.py <path> --show-chunks
```

**Lo que hace:**
1. ✅ Lee el artículo JSON
2. 🔨 Genera chunks
3. 📤 Sube a Pinecone

**Uso típico:** Cuando tienes un artículo completamente nuevo (no existe en Pinecone).

---

### 4. `verify_article.py` - 🔍 Verificar Artículo

Verifica que un artículo esté correctamente en Pinecone.

```bash
python scripts/verify_article.py <article_id>
```

---

## Workflow Recomendado

### Actualizar un artículo existente
```bash
python scripts/update_article.py "ruta/al/articulo.json"
```

### Procesar un artículo nuevo
```bash
python scripts/process_single_article.py "ruta/al/articulo.json"
```

### Borrar un artículo sin reemplazarlo
```bash
# 1. Ver qué artículos hay
python scripts/delete_article.py --list

# 2. Borrar uno específico
python scripts/delete_article.py <article_id>
```

---

## Tips

### Ver cambios sin aplicarlos
Usa `--dry-run` para ver qué haría el script sin hacer cambios:

```bash
python scripts/update_article.py <path> --dry-run
```

### Ver chunks generados
Usa `--show-chunks` para ver los chunks que se generarían:

```bash
python scripts/update_article.py <path> --show-chunks
```

### Automatizar (sin confirmación)
Usa `--skip-confirmation` para scripts automatizados:

```bash
python scripts/update_article.py <path> --skip-confirmation
```

---

## Estructura de Artículos

Los artículos JSON deben tener esta estructura:

```json
{
  "metadata": {
    "article_id": "unique_article_id",
    "title": "Article Title",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    ...
  },
  "summary": { ... },
  "sections": [ ... ]
}
```

El `article_id` es lo que se usa para identificar artículos en Pinecone.
