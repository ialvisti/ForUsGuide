# Despliegue de UI Integrada

La UI ahora está completamente integrada con la API de FastAPI y se sirve automáticamente cuando la API está corriendo.

## ✅ Cambios Realizados

### 1. Modificaciones en `api/main.py`
- ✅ Agregado soporte para servir archivos estáticos (`StaticFiles`)
- ✅ Agregado endpoint `/ui` que sirve `index.html`
- ✅ Actualizado endpoint raíz `/` para incluir enlace a la UI
- ✅ Montado directorio `ui/` como archivos estáticos en `/ui/static`

### 2. Modificaciones en `ui/index.html`
- ✅ Auto-detección de API URL según el entorno
- ✅ Cuando se sirve desde producción, usa automáticamente `window.location.origin`
- ✅ Cuando se sirve desde local, permite configuración manual

### 3. Documentación Actualizada
- ✅ README principal actualizado con nueva URL de UI
- ✅ README de UI actualizado con instrucciones de acceso

## 🌐 URLs de Acceso

### Local
```
http://localhost:8000/ui
```

### Producción (Render)
```
https://forusguide.onrender.com/ui
```

## 🚀 Cómo Desplegar en Render

### Opción 1: Auto-deploy desde GitHub (Recomendado)

1. **Commit y push de los cambios:**
   ```bash
   cd kb-rag-system
   git add .
   git commit -m "feat: Integrate UI with FastAPI for seamless deployment"
   git push origin main
   ```

2. **Render detectará automáticamente los cambios y desplegará**
   - Ve a tu dashboard de Render
   - Espera a que termine el build (2-5 minutos)
   - La UI estará disponible automáticamente en `/ui`

### Opción 2: Deploy Manual

Si no tienes auto-deploy configurado:

1. **Commit los cambios localmente:**
   ```bash
   git add .
   git commit -m "feat: Integrate UI with FastAPI"
   ```

2. **Push al repositorio remoto:**
   ```bash
   git push origin main
   ```

3. **En el dashboard de Render:**
   - Ve a tu servicio "forusguide"
   - Click en "Manual Deploy" → "Deploy latest commit"
   - Espera a que termine el build

## 🧪 Verificación

### 1. Verificar API está corriendo
```bash
curl https://forusguide.onrender.com/health
```

**Respuesta esperada:**
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "pinecone_connected": true,
  "openai_configured": true,
  "total_vectors": 1234
}
```

### 2. Verificar endpoint raíz
```bash
curl https://forusguide.onrender.com/
```

**Respuesta esperada:**
```json
{
  "name": "KB RAG System API",
  "version": "1.0.0",
  "status": "online",
  "docs": "/docs",
  "ui": "/ui"
}
```

### 3. Acceder a la UI
Abre en tu navegador:
```
https://forusguide.onrender.com/ui
```

**Deberías ver:**
- ✅ Interfaz moderna con fondo degradado
- ✅ Badge de "Connected" en verde (si la API está healthy)
- ✅ URL de API pre-configurada con `https://forusguide.onrender.com`
- ✅ Dos botones para endpoints: "Required Data" y "Generate Response"

## 📋 Checklist Post-Deploy

- [ ] API responde en `/health`
- [ ] Endpoint raíz `/` muestra `"ui": "/ui"`
- [ ] UI carga correctamente en `/ui`
- [ ] UI muestra badge "Connected" en verde
- [ ] API URL se auto-detecta correctamente
- [ ] Se pueden enviar requests desde la UI
- [ ] Responses se muestran correctamente

## 🔧 Troubleshooting

### UI no carga (404 Not Found)

**Posibles causas:**
1. La carpeta `ui/` no se incluyó en el deploy
2. El archivo `index.html` no existe

**Solución:**
```bash
# Verificar que ui/ esté en el repo
ls -la kb-rag-system/ui/

# Debe contener:
# - index.html
# - README.md
# - start_ui.sh (opcional)
```

### UI carga pero muestra "Disconnected"

**Causa:** La API no está respondiendo en `/health`

**Solución:**
1. Verificar que la API esté corriendo
2. Verificar logs en Render dashboard
3. Verificar que las API keys estén configuradas

### CORS errors en la consola

**Causa:** CORS middleware no está configurado correctamente

**Solución:**
Verificar en `api/config.py` que `ALLOWED_ORIGINS` incluya:
```python
ALLOWED_ORIGINS = ["*"]  # O tu dominio específico
```

### La URL de API no se auto-detecta

**Causa:** JavaScript no se está ejecutando correctamente

**Solución:**
1. Verificar que el archivo `index.html` tenga los cambios más recientes
2. Abrir la consola del navegador para ver errores
3. Configurar manualmente la URL si es necesario

## 🎯 Ventajas de la Integración

1. **Deploy Unificado:** Un solo servicio en Render sirve tanto API como UI
2. **Sin CORS:** La UI y API comparten el mismo origen
3. **Auto-configuración:** La UI detecta automáticamente la URL de la API
4. **Simplicidad:** No hay que gestionar dos servicios separados
5. **Costo:** Solo pagas por un servicio en lugar de dos

## 📚 Recursos Adicionales

- [FastAPI Static Files](https://fastapi.tiangolo.com/tutorial/static-files/)
- [Render Deploy Guide](https://render.com/docs/deploy-fastapi)
- [UI Documentation](./ui/README.md)

---

**Última actualización:** 2026-01-27
