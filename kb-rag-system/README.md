# KB RAG System

Sistema RAG (Retrieval-Augmented Generation) para búsqueda y consulta de artículos de knowledge base.

## 🏗️ Arquitectura

```
Cliente → FastAPI Endpoint → RAG Engine → Pinecone (búsqueda) + OpenAI (generación)
```

## 📋 Requisitos

- Python 3.12+
- Pinecone API key
- OpenAI API key

## 🚀 Setup

1. Crear virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
```

2. Instalar dependencias:
```bash
pip install -r requirements.txt
```

3. Configurar variables de entorno:
```bash
cp .env.example .env
# Editar .env con tus API keys
```

4. Crear índice en Pinecone:
```bash
bash scripts/setup_index.sh
```

5. Cargar artículos:
```bash
python scripts/load_all_articles.py
```

## 🔧 Uso

### Iniciar API:
```bash
uvicorn api.main:app --reload
```

### Hacer una consulta:
```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "question": "What fees apply to 401k withdrawals?",
    "filters": {
      "record_keeper": "LT Trust"
    }
  }'
```

## 📁 Estructura del Proyecto

```
kb-rag-system/
├── data_pipeline/       # Procesamiento de artículos
├── api/                 # FastAPI application
├── tests/               # Testing
├── scripts/             # Utility scripts
└── requirements.txt     # Dependencias
```

## 🧪 Testing

```bash
pytest tests/
```

## 📝 Documentación API

Una vez iniciado el servidor, visita:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
