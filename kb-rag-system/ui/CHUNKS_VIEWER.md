# 📦 Chunks Viewer - Documentation

## 📋 Overview

El **Chunks Viewer** es una interfaz web completa para visualizar y explorar todos los chunks almacenados en Pinecone. Proporciona una forma bonita, organizada y filtrable de ver el contenido vectorizado de tus artículos de Knowledge Base.

---

## ✨ Features

### 🔍 Filtros Avanzados
- **Article ID**: Filtrar por artículo específico
- **Tier**: Filtrar por prioridad (Critical, High, Medium, Low)
- **Type**: Filtrar por tipo de chunk (business_rules, faqs, steps, etc.)
- **Limit**: Controlar cuántos chunks cargar (10, 25, 50, 100, 500)

### 📊 Visualización Rica
- **Badges de Tier**: Colores distintivos para cada nivel de prioridad
- **Metadata Completa**: Toda la información estructurada del chunk
- **Tags y Topics**: Visualización de categorías y temas específicos
- **Content Expandible**: Los chunks largos se pueden expandir/colapsar
- **Score de Similitud**: Muestra el score de cada chunk

### 🎨 Diseño
- **Responsive**: Funciona perfecto en desktop, tablet y móvil
- **Modern UI**: Diseño limpio con sistema de colores coherente
- **Loading States**: Indicadores visuales durante carga
- **Empty States**: Mensajes claros cuando no hay resultados

---

## 🚀 Cómo Usar

### 1. Iniciar la API

```bash
cd kb-rag-system
source venv/bin/activate
bash scripts/start_api.sh
```

### 2. Abrir la UI

Hay dos formas:

**Opción A: Desde el index principal**
1. Abre `http://localhost:8000/ui`
2. Haz clic en el botón "📦 View Chunks"

**Opción B: Directamente**
1. Abre el archivo `kb-rag-system/ui/chunks.html` en tu navegador
2. O usa: `http://localhost:8000/ui/static/chunks.html`

### 3. Filtrar y Explorar

1. **Ver todos los chunks**: Simplemente haz clic en "Load Chunks"
2. **Filtrar por artículo**: Ingresa el article_id y carga
3. **Filtrar por tier**: Selecciona Critical, High, Medium o Low
4. **Filtrar por tipo**: Escribe el chunk_type (ej: business_rules)
5. **Combinar filtros**: Usa múltiples filtros simultáneamente

---

## 🔌 API Endpoints

El Chunks Viewer usa dos nuevos endpoints:

### 1. POST /api/v1/chunks

Lista chunks con filtros opcionales.

**Request:**
```json
{
  "article_id": "forusall_401k_hardship_withdrawal_complete_guide",
  "tier": "critical",
  "chunk_type": "business_rules",
  "limit": 25
}
```

**Response:**
```json
{
  "chunks": [
    {
      "id": "chunk_id_here",
      "score": 0.1234,
      "metadata": {
        "article_id": "...",
        "article_title": "...",
        "record_keeper": "...",
        "plan_type": "...",
        "topic": "...",
        "chunk_tier": "critical",
        "chunk_type": "business_rules",
        "chunk_category": "...",
        "content": "...",
        "specific_topics": [...],
        "tags": [...]
      }
    }
  ],
  "total": 25,
  "filters_applied": {...}
}
```

### 2. GET /api/v1/index-stats

Obtiene estadísticas del índice.

**Response:**
```json
{
  "total_vectors": 92,
  "namespaces": {
    "kb_articles": {
      "vector_count": 92
    }
  }
}
```

---

## 🎨 Código de Colores

### Badges de Tier

- 🔴 **Critical**: Rojo - Información más importante
- 🟡 **High**: Amarillo - Información importante
- 🔵 **Medium**: Azul - Información moderada
- 🟣 **Low**: Púrpura - Información complementaria

---

## 📖 Ejemplos de Uso

### Ejemplo 1: Ver chunks críticos de un artículo

```
Filtros:
- Article ID: forusall_401k_hardship_withdrawal_complete_guide
- Tier: critical
- Limit: 100

Resultado: 7 chunks críticos del artículo de hardship withdrawal
```

### Ejemplo 2: Ver todos los business rules

```
Filtros:
- Type: business_rules
- Limit: 50

Resultado: Todos los chunks tipo business_rules de todos los artículos
```

### Ejemplo 3: Explorar chunks de ForUsAll

```
Filtros:
- (Ninguno, cargar todos)
- Luego buscar manualmente en la página "ForUsAll"

Resultado: Vista de todos los chunks, puedes filtrar visualmente
```

---

## 🛠️ Arquitectura

```
┌─────────────────────────────────────┐
│         chunks.html (UI)            │
│  • Formulario de filtros            │
│  • Grid de chunks                   │
│  • Badges y metadata                │
└──────────────┬──────────────────────┘
               │
               ↓ Fetch API
┌─────────────────────────────────────┐
│      FastAPI Backend                │
│  • POST /api/v1/chunks              │
│  • GET  /api/v1/index-stats         │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│      PineconeUploader               │
│  • query_chunks()                   │
│  • get_index_stats()                │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│         Pinecone Index              │
│  • kb-articles-production           │
│  • namespace: kb_articles           │
└─────────────────────────────────────┘
```

---

## 🎯 Casos de Uso

### Para Desarrolladores
- Verificar que los chunks se generaron correctamente
- Debuggear problemas de chunking
- Explorar la estructura de metadata
- Validar content de chunks específicos

### Para Analistas
- Entender cómo se organizan los artículos
- Ver qué información está en cada tier
- Revisar cobertura de topics
- Analizar distribución de chunk types

### Para QA
- Verificar integridad de datos después de uploads
- Comparar chunks antes y después de updates
- Validar filtros y búsquedas
- Testear UI responsiveness

---

## 🔧 Troubleshooting

### Los chunks no cargan

**Problema**: La página muestra "Loading..." indefinidamente

**Solución**:
1. Verifica que la API esté corriendo: `curl http://localhost:8000/health`
2. Verifica que Pinecone esté conectado
3. Revisa la consola del navegador para errores

### No se encuentran chunks

**Problema**: La búsqueda no retorna resultados

**Solución**:
1. Verifica que hay vectores en el índice: Ver stats en el header
2. Intenta sin filtros primero (todos los chunks)
3. Verifica que los filtros sean correctos (case-sensitive)

### Errores de CORS

**Problema**: Error de CORS en la consola

**Solución**:
1. Asegúrate de que la API permita el origen correcto
2. Si usas file://, considera usar un servidor HTTP
3. Usa `bash start_ui.sh` para servir la UI correctamente

---

## 📝 Notas Técnicas

### Performance
- Los chunks se cargan bajo demanda
- Usa límites razonables (≤100) para mejor performance
- Pinecone tiene consistencia eventual (~10s después de upload)

### Seguridad
- Los endpoints de chunks NO requieren API key (son públicos)
- Solo para uso interno, no exponer a internet
- La UI solo lee datos, no puede modificar Pinecone

### Compatibilidad
- Funciona en todos los browsers modernos
- No requiere dependencias externas
- HTML/CSS/JS vanilla (sin framework)

---

## 🚀 Futuras Mejoras

Posibles features para agregar:

- ✅ Búsqueda por texto en content
- ✅ Ordenamiento por score, tier, tipo
- ✅ Exportar chunks a JSON/CSV
- ✅ Visualización de relaciones entre chunks
- ✅ Comparación de chunks entre artículos
- ✅ Estadísticas agregadas por tier/type
- ✅ Dark mode

---

## 📚 Referencias

- [Pinecone Python SDK](https://docs.pinecone.io/docs/python-client)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Chunking Strategy](../PIPELINE_GUIDE.md)

---

**Versión**: 1.0  
**Fecha**: 2026-02-10  
**Autor**: Sistema KB RAG
