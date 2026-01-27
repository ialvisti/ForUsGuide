# KB RAG System - Interfaz de Usuario

Interfaz web minimalista y moderna para interactuar con los endpoints del KB RAG System.

## 🎨 Características

- ✅ **Diseño Minimalista** - Interfaz limpia y fácil de usar
- ✅ **Responsive** - Funciona en desktop, tablet y móvil
- ✅ **Dark Mode Ready** - Colores modernos con gradiente
- ✅ **Health Check** - Verifica el estado del sistema en tiempo real
- ✅ **Dos Endpoints** - Acceso completo a `/required-data` y `/generate-response`
- ✅ **Validación JSON** - Validación de datos en tiempo real
- ✅ **Copy to Clipboard** - Copia respuestas con un click
- ✅ **Loading States** - Indicadores visuales durante requests
- ✅ **Error Handling** - Manejo de errores claro y amigable

## 🚀 Uso Rápido

### Opción 1: Acceder a través de la API (Recomendado)

Si tu API está corriendo, la UI está disponible automáticamente en:

**Local:**
```
http://localhost:8000/ui
```

**Producción (Render):**
```
https://forusguide.onrender.com/ui
```

La API Key se configurará automáticamente según tu entorno.

### Opción 2: Abrir directamente el archivo HTML

Simplemente abre el archivo `index.html` en tu navegador:

```bash
# macOS
open index.html

# Linux
xdg-open index.html

# Windows
start index.html
```

### Opción 3: Servidor HTTP local

Para evitar problemas de CORS, usa un servidor HTTP:

**Python:**
```bash
# Python 3
python3 -m http.server 3000

# Luego abre: http://localhost:3000
```

**Node.js:**
```bash
# Instalar http-server globalmente
npm install -g http-server

# Iniciar servidor
http-server -p 3000

# Luego abre: http://localhost:3000
```

**PHP:**
```bash
php -S localhost:3000
```

## 📋 Configuración Inicial

1. **Iniciar la API del sistema:**
   ```bash
   cd ..
   source venv/bin/activate
   bash scripts/start_api.sh
   ```

2. **Abrir la UI:**
   ```bash
   open index.html
   # O iniciar servidor HTTP
   ```

3. **Configurar en la interfaz:**
   - **API URL:** `http://localhost:8000` (por defecto)
   - **API Key:** Tu API key del archivo `.env`
   - **Record Keeper:** Seleccionar el recordkeeper
   - **Plan Type:** Seleccionar el tipo de plan

4. **Verificar estado:**
   - Click en "Verificar Estado" para comprobar la conexión

## 🎯 Uso de los Endpoints

### 1. Required Data Endpoint

**¿Qué hace?**
Determina qué datos del participante y plan se necesitan recolectar.

**Cómo usar:**
1. Ingresa la consulta en "Inquiry"
2. Especifica el "Topic" (rollover, distribution, loan, etc.)
3. Click en "Enviar Consulta"
4. La respuesta mostrará los campos requeridos

**Ejemplo de Inquiry:**
```
I want to rollover my 401k to Fidelity
```

**Respuesta esperada:**
```json
{
  "article_reference": {
    "article_id": "...",
    "title": "...",
    "confidence": 0.85
  },
  "required_fields": {
    "participant_data": [...],
    "plan_data": [...]
  },
  "confidence": 0.85,
  "metadata": {...}
}
```

---

### 2. Generate Response Endpoint

**¿Qué hace?**
Genera una respuesta contextualizada con steps, warnings y guardrails.

**Cómo usar:**
1. Ingresa la consulta en "Inquiry"
2. Especifica el "Topic"
3. Ingresa los datos recolectados en formato JSON
4. Ajusta "Max Response Tokens" si necesario
5. Especifica "Total Inquiries in Ticket"
6. Click en "Generar Respuesta"

**Ejemplo de Collected Data:**
```json
{
  "participant_data": {
    "current_balance": "$1,993.84",
    "employment_status": "Terminated",
    "receiving_institution": "Fidelity"
  },
  "plan_data": {
    "rollover_method": "Direct rollover available",
    "processing_time": "7-10 business days"
  }
}
```

**Respuesta esperada:**
```json
{
  "decision": "can_proceed",
  "confidence": 0.75,
  "response": {
    "sections": [...]
  },
  "guardrails": {
    "must_not_say": [...],
    "must_verify": [...]
  },
  "metadata": {...}
}
```

## 🎨 Personalización

### Cambiar Colores

Edita las variables CSS en el `<style>` del archivo `index.html`:

```css
:root {
    --primary: #2563eb;        /* Color principal */
    --primary-dark: #1d4ed8;   /* Color principal oscuro */
    --success: #10b981;        /* Color de éxito */
    --error: #ef4444;          /* Color de error */
    /* ... más variables ... */
}
```

### Cambiar Gradiente de Fondo

```css
body {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    /* O prueba otros gradientes:
    background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
    background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
    */
}
```

### Cambiar Tipografía

```css
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', ...;
    /* O usa Google Fonts:
    font-family: 'Inter', sans-serif;
    */
}
```

## 🔧 Solución de Problemas

### Error: CORS Policy

**Problema:** El navegador bloquea requests por política CORS.

**Solución:**
1. Usa un servidor HTTP local (ver arriba)
2. O configura CORS en la API:
   ```python
   # En api/main.py
   allow_origins=["*"]  # Permite todos los orígenes
   ```

### Error: Failed to fetch

**Problema:** No se puede conectar a la API.

**Solución:**
1. Verifica que la API esté corriendo: `curl http://localhost:8000/health`
2. Verifica la URL en la configuración de la UI
3. Revisa los logs de la API

### Error: Invalid API Key

**Problema:** La API Key es incorrecta.

**Solución:**
1. Copia la API Key del archivo `.env`
2. Pégala en el campo "API Key" de la UI
3. Asegúrate de no tener espacios extras

### Error: Invalid JSON

**Problema:** El formato del "Collected Data" no es JSON válido.

**Solución:**
1. Usa un validador JSON online
2. Asegúrate de usar comillas dobles `"`
3. No uses trailing commas

## 📱 Responsive Design

La UI está optimizada para diferentes tamaños de pantalla:

- **Desktop (>768px):** Layout de 2 columnas
- **Tablet/Mobile (<768px):** Layout de 1 columna apilada

## ⌨️ Atajos de Teclado

- **Enter en textarea:** Nueva línea
- **Ctrl/Cmd + Enter:** Submit (próximamente)

## 🔐 Seguridad

- La API Key se envía en el header `X-API-Key`
- El campo de API Key usa `type="password"` para ocultar el valor
- **Recomendación:** No compartas screenshots con tu API Key visible

## 📊 Indicadores de Estado

### Health Check

- ✅ **Verde:** Sistema operacional
- ❌ **Rojo:** Sistema degradado o sin conexión

### Response Status

- ✅ **Success:** Request exitoso (status 200)
- ❌ **Error:** Request falló (status 4xx/5xx)

## 🎯 Ejemplos de Uso Completo

### Ejemplo 1: Consulta de Rollover

**Required Data:**
```
Inquiry: "I want to rollover my 401k to Fidelity"
Topic: "rollover"
```

**Generate Response:**
```
Inquiry: "How do I complete a direct rollover to Fidelity?"
Topic: "rollover"
Collected Data: {
  "participant_data": {
    "current_balance": "$5,000.00",
    "employment_status": "Terminated",
    "receiving_institution": "Fidelity"
  },
  "plan_data": {
    "rollover_method": "Direct rollover available"
  }
}
```

### Ejemplo 2: Consulta de Distribution

**Required Data:**
```
Inquiry: "How do I withdraw money from my 401k?"
Topic: "distribution"
```

**Generate Response:**
```
Inquiry: "What are the steps to request a hardship withdrawal?"
Topic: "distribution"
Collected Data: {
  "participant_data": {
    "age": 45,
    "employment_status": "Active",
    "current_balance": "$25,000.00"
  },
  "plan_data": {
    "hardship_withdrawal_allowed": true
  }
}
```

## 🚀 Próximas Mejoras

- [ ] Historial de consultas
- [ ] Favoritos/Templates
- [ ] Export a PDF
- [ ] Dark mode toggle
- [ ] Multi-language support
- [ ] Syntax highlighting para JSON
- [ ] Auto-complete para topics comunes

## 📝 Notas

- La UI es completamente standalone (HTML + CSS + JS vanilla)
- No requiere dependencias externas ni build process
- Compatible con todos los navegadores modernos
- Optimizada para performance y UX

## 🐛 Reportar Problemas

Si encuentras algún bug o tienes sugerencias, por favor:
1. Revisa la sección de "Solución de Problemas"
2. Verifica los logs de la API
3. Documenta los pasos para reproducir el error

---

**Desarrollado para:** KB RAG System v1.0  
**Última actualización:** 2026-01-18
