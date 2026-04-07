# Dockerfile for KB RAG System API
# 
# Build: docker build -t kb-rag-system .
# Run: docker run -p 8000:8000 --env-file .env kb-rag-system

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY data_pipeline/ ./data_pipeline/
COPY api/ ./api/
COPY scripts/ ./scripts/
COPY ui/ ./ui/

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Cloud Run sets PORT; default to 8000 for local Docker use
ENV PORT=8000
EXPOSE 8000

# Health check (uses fixed port for local Docker; Cloud Run ignores HEALTHCHECK)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run application — uses ${PORT} so Cloud Run can set it dynamically
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 2"]
