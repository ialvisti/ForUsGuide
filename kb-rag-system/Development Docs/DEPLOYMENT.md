# Guía de Deployment - KB RAG System

**Última actualización:** 2026-01-18

---

## Opciones de Deployment

### 1. Render (Recomendado para inicio rápido)

Render es ideal para comenzar rápidamente con un web service managed.

#### Pasos:

1. **Crear cuenta en Render:** https://render.com

2. **Conectar repositorio Git:**
   - Push tu código a GitHub/GitLab
   - En Render: New → Web Service
   - Conecta tu repositorio

3. **Configurar el servicio:**
   ```
   Name: kb-rag-system
   Environment: Python
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn api.main:app --host 0.0.0.0 --port $PORT --workers 2
   ```

4. **Agregar variables de entorno:**
   ```
   PINECONE_API_KEY=<tu-key>
   OPENAI_API_KEY=<tu-key>
   API_KEY=<tu-key-segura>
   INDEX_NAME=kb-articles-production
   NAMESPACE=kb_articles
   ENVIRONMENT=production
   LOG_LEVEL=INFO
   ```

5. **Deploy:**
   - Render automáticamente hace deploy cuando haces push

**Ventajas:**
- ✅ Deploy automático con Git
- ✅ SSL/HTTPS gratis
- ✅ Escalado fácil
- ✅ Logs integrados

**Costos:**
- Plan gratuito: disponible (con limitaciones)
- Plan Starter: $7/mes

---

### 2. Docker (Local o VPS)

Deployment con Docker en tu propio servidor o VPS.

#### Build y Run Local:

```bash
# 1. Build image
docker build -t kb-rag-system .

# 2. Run container
docker run -d \
  -p 8000:8000 \
  --name kb-rag-api \
  --env-file .env \
  --restart unless-stopped \
  kb-rag-system

# 3. Verificar logs
docker logs -f kb-rag-api

# 4. Verificar health
curl http://localhost:8000/health
```

#### Deploy en VPS (DigitalOcean, AWS EC2, etc.):

```bash
# 1. Conectar a VPS via SSH
ssh user@your-server-ip

# 2. Instalar Docker
curl -fsSL https://get.docker.com | sh

# 3. Clonar repositorio
git clone <your-repo-url>
cd kb-rag-system

# 4. Crear .env file
nano .env
# (agregar variables de entorno)

# 5. Build y run
docker build -t kb-rag-system .
docker run -d -p 8000:8000 --env-file .env --restart unless-stopped kb-rag-system

# 6. Configurar nginx como reverse proxy (opcional)
sudo apt install nginx
sudo nano /etc/nginx/sites-available/kb-rag-api
```

**Nginx config:**
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

---

### 3. Serverless (AWS Lambda + API Gateway)

Para cargas de trabajo intermitentes.

**Nota:** Requiere adaptación del código para Lambda handler.

#### Configuración:

1. **Usar Mangum para adaptar FastAPI:**
   ```python
   # lambda_handler.py
   from mangum import Mangum
   from api.main import app
   
   handler = Mangum(app)
   ```

2. **Deploy con Serverless Framework o SAM**

3. **Configurar API Gateway con Lambda**

**Ventajas:**
- ✅ Pay-per-use
- ✅ Auto-scaling
- ✅ No gestión de servidores

**Desventajas:**
- ❌ Cold starts (~3-5s)
- ❌ Más complejo de configurar

---

### 4. Kubernetes (Para escala enterprise)

Para alta disponibilidad y escala.

**Deployment manifest ejemplo:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kb-rag-system
spec:
  replicas: 3
  selector:
    matchLabels:
      app: kb-rag-system
  template:
    metadata:
      labels:
        app: kb-rag-system
    spec:
      containers:
      - name: api
        image: kb-rag-system:latest
        ports:
        - containerPort: 8000
        env:
        - name: PINECONE_API_KEY
          valueFrom:
            secretKeyRef:
              name: kb-secrets
              key: pinecone-api-key
```

---

## Configuración de Producción

### Variables de Entorno Requeridas

```bash
# API Keys (CRÍTICAS)
PINECONE_API_KEY=<tu-pinecone-key>
OPENAI_API_KEY=<tu-openai-key>
API_KEY=<genera-una-key-segura>

# Pinecone Configuration
INDEX_NAME=kb-articles-production
NAMESPACE=kb_articles

# API Configuration
ENVIRONMENT=production
API_HOST=0.0.0.0
API_PORT=8000
LOG_LEVEL=INFO

# OpenAI Configuration (opcional)
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.1
```

### Generar API Key Segura

```bash
# En Python
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# En bash con openssl
openssl rand -base64 32
```

---

## Monitoreo y Logs

### Logs con Docker

```bash
# Ver logs en tiempo real
docker logs -f kb-rag-api

# Ver últimas 100 líneas
docker logs --tail 100 kb-rag-api

# Buscar errores
docker logs kb-rag-api 2>&1 | grep ERROR
```

### Logs en Render

- Dashboard → Tu servicio → Logs tab
- Logs en tiempo real
- Búsqueda integrada

### Métricas Importantes

Monitor these metrics:
- **Latencia de requests:** < 5s ideal
- **Rate de errores:** < 1% ideal
- **Uso de memoria:** < 512MB por worker
- **CPU usage:** < 80%

---

## Escalado

### Escalado Vertical (más recursos)

```bash
# Docker con más memoria
docker run -d -p 8000:8000 --memory="2g" --cpus="2" kb-rag-system
```

### Escalado Horizontal (más instancias)

```bash
# Uvicorn con múltiples workers
uvicorn api.main:app --workers 4 --host 0.0.0.0 --port 8000

# O en Dockerfile
CMD ["uvicorn", "api.main:app", "--workers", "4", "--host", "0.0.0.0"]
```

**Regla general:** `workers = (2 * CPU cores) + 1`

---

## Security Checklist

- [ ] API Key fuerte y secreta
- [ ] HTTPS habilitado (SSL/TLS)
- [ ] Rate limiting configurado
- [ ] CORS configurado correctamente
- [ ] Logs no exponen información sensible
- [ ] Variables de entorno no commiteadas a Git
- [ ] Firewall configurado (solo puertos necesarios)
- [ ] Actualizaciones de seguridad aplicadas

---

## Troubleshooting

### Error: "PINECONE_API_KEY no está configurada"

```bash
# Verificar que .env está presente
ls -la .env

# Verificar contenido (sin exponer keys)
grep PINECONE_API_KEY .env | cut -d'=' -f1
```

### Error: "Connection refused" al llamar API

```bash
# Verificar que el contenedor está corriendo
docker ps | grep kb-rag-api

# Verificar logs
docker logs kb-rag-api

# Verificar puerto
netstat -tulpn | grep 8000
```

### High Memory Usage

```bash
# Reducir número de workers
CMD ["uvicorn", "api.main:app", "--workers", "1"]

# Agregar límite de memoria
docker run --memory="512m" ...
```

### Slow Responses

- Verificar latencia a Pinecone
- Verificar latencia a OpenAI
- Considerar caching de búsquedas frecuentes
- Optimizar token budget

---

## Backup y Disaster Recovery

### Backup de Pinecone Index

```bash
# Pinecone maneja backups automáticamente
# No requiere acción manual

# Para export/import manual:
# 1. Export todos los chunks
python scripts/export_all_chunks.py

# 2. Guardar en S3/Cloud Storage
```

### Rollback de Deploy

**En Render:**
- Dashboard → Deploys → Rollback to previous

**En Docker:**
```bash
# Mantener imagen anterior
docker tag kb-rag-system:latest kb-rag-system:backup
docker build -t kb-rag-system:latest .

# Si hay problemas, volver a anterior
docker stop kb-rag-api
docker rm kb-rag-api
docker run -d -p 8000:8000 kb-rag-system:backup
```

---

## Performance Tuning

### Uvicorn Workers

```bash
# Configuración óptima depende de CPU y memoria
# CPU < 2 cores: 2 workers
# CPU 2-4 cores: 4 workers
# CPU 4+ cores: (2 * cores) + 1
```

### Request Timeout

```python
# En main.py agregar timeout
@app.middleware("http")
async def add_timeout(request, call_next):
    async with asyncio.timeout(30):  # 30 seconds timeout
        return await call_next(request)
```

### Connection Pooling

- Pinecone SDK maneja connection pooling automáticamente
- OpenAI SDK también

---

## Costos Estimados

### Infraestructura

- **Render Starter:** $7/mes
- **DigitalOcean Droplet:** $6/mes (basic)
- **AWS EC2 t3.small:** ~$15/mes

### APIs

- **Pinecone Serverless:** $0.025 per 1M reads (~$0.50/mes para uso moderado)
- **OpenAI GPT-4o-mini:** ~$0.15 per 1M input tokens (~$5/mes para 100 tickets/día)

**Total estimado:** $15-30/mes para uso inicial

---

## Siguientes Pasos

1. **Elegir plataforma de deployment**
2. **Configurar variables de entorno**
3. **Hacer deploy inicial**
4. **Configurar monitoreo**
5. **Probar endpoints en producción**
6. **Integrar con n8n**
7. **Monitor y optimizar**

---

**Para ayuda:** Ver `DEVELOPMENT_PLAN.md` y `ARCHITECTURE.md`
