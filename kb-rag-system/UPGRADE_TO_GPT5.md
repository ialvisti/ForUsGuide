# 🚀 Guía de Actualización a GPT-5.2

Esta guía te ayudará a actualizar tu sistema RAG para usar **GPT-5.2 Thinking**, el modelo más avanzado de OpenAI.

## 📋 Resumen de Cambios

Tu código ha sido actualizado para soportar:
- ✅ GPT-5.2 (todos las variantes)
- ✅ Nuevos parámetros de razonamiento (`reasoning.effort`)
- ✅ Compatibilidad retroactiva con GPT-4.x
- ✅ Detección automática del modelo

## 🔧 Paso 1: Actualizar tu archivo `.env`

Edita tu archivo `.env` en la raíz del proyecto `kb-rag-system/.env` y actualiza estas líneas:

### Opción A: GPT-5.2 (Thinking) - RECOMENDADO
```bash
# Modelo más potente con razonamiento profundo
OPENAI_MODEL=gpt-5.2
OPENAI_REASONING_EFFORT=medium
OPENAI_TEMPERATURE=0.1
```

### Opción B: GPT-5.2 Chat (Instant) - Más rápido
```bash
# Respuestas más rápidas, menos razonamiento
OPENAI_MODEL=gpt-5.2-chat-latest
OPENAI_REASONING_EFFORT=low
OPENAI_TEMPERATURE=0.1
```

### Opción C: GPT-5.2 Pro - Máxima capacidad
```bash
# Para casos extremadamente complejos
OPENAI_MODEL=gpt-5.2-pro
OPENAI_REASONING_EFFORT=high
OPENAI_TEMPERATURE=0.1
```

### Opción D: Mantener GPT-4o (si prefieres)
```bash
# Modelo anterior, funciona perfectamente
OPENAI_MODEL=gpt-4o
OPENAI_REASONING_EFFORT=medium  # Se ignora en GPT-4
OPENAI_TEMPERATURE=0.1
```

## ⚙️ Valores de `OPENAI_REASONING_EFFORT`

Solo aplica para modelos GPT-5.2:

| Valor | Descripción | Velocidad | Calidad | Costo |
|-------|-------------|-----------|---------|-------|
| `none` | Sin razonamiento extra | 🚀 Muy rápido | ⭐⭐⭐ | 💰 |
| `low` | Razonamiento ligero | 🚀 Rápido | ⭐⭐⭐⭐ | 💰💰 |
| `medium` | Balance óptimo | ⚡ Normal | ⭐⭐⭐⭐⭐ | 💰💰💰 |
| `high` | Razonamiento profundo | 🐢 Lento | ⭐⭐⭐⭐⭐ | 💰💰💰💰 |
| `xhigh` | Máximo razonamiento | 🐌 Muy lento | ⭐⭐⭐⭐⭐ | 💰💰💰💰💰 |

**Para tu sistema RAG, recomiendo `medium`** - buen balance entre calidad y velocidad.

## 🚀 Paso 2: Reiniciar la API

Después de actualizar el `.env`:

```bash
# Navegar al directorio
cd /Users/ivanalvis/Desktop/FUA\ Knowledge\ Base\ Articles/kb-rag-system

# Si la API está corriendo, detenerla (Ctrl+C)

# Reiniciar la API
./scripts/start_api.sh
```

O si estás corriendo manualmente:

```bash
# Activar el entorno virtual
source venv/bin/activate

# Iniciar la API
python -m api.main
```

## ✅ Paso 3: Verificar que funciona

### Verificar logs al iniciar

Al iniciar la API, deberías ver:

```
✅ Configuration validated
✅ RAG Engine initialized
  - Reasoning effort: medium    # <-- Si usas GPT-5.2
🚀 API Ready on http://0.0.0.0:8000
```

### Probar con un request

```bash
# Health check
curl http://localhost:8000/health

# Probar required-data
curl -X POST http://localhost:8000/api/v1/required-data \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "inquiry": "¿Cómo puedo hacer un rollover?",
    "record_keeper": "LT Trust",
    "plan_type": "401(k)",
    "topic": "rollover"
  }'
```

## 📊 Comparación de Modelos

### GPT-4o-mini (actual)
- Velocidad: 🚀 Muy rápido
- Calidad: ⭐⭐⭐
- Costo: 💰 $0.15/M tokens
- Mejor para: Alto volumen, bajo costo

### GPT-4o
- Velocidad: 🚀 Rápido
- Calidad: ⭐⭐⭐⭐
- Costo: 💰💰 $2.50/M tokens
- Mejor para: Balance calidad/costo

### GPT-5.2 (Thinking)
- Velocidad: ⚡ Normal-Lento (depende de effort)
- Calidad: ⭐⭐⭐⭐⭐
- Costo: 💰💰💰💰 ~$10-15/M tokens (estimado)
- Mejor para: Máxima calidad, análisis complejo

## 🎯 Recomendación por Escenario

### Desarrollo/Testing
```bash
OPENAI_MODEL=gpt-4o-mini
```
- Rápido y barato para iterar

### Producción - Balance
```bash
OPENAI_MODEL=gpt-5.2
OPENAI_REASONING_EFFORT=low
```
- Buena calidad sin exceso de latencia

### Producción - Máxima Calidad
```bash
OPENAI_MODEL=gpt-5.2
OPENAI_REASONING_EFFORT=medium
```
- Mejor calidad para usuarios finales

### Casos Críticos
```bash
OPENAI_MODEL=gpt-5.2-pro
OPENAI_REASONING_EFFORT=high
```
- Solo para inquiries muy complejas

## 🔍 Monitoreo

Después de actualizar, monitorea:

1. **Latencia de respuesta**
   - GPT-5.2 puede ser 2-10x más lento que GPT-4o-mini
   - Verifica que sea aceptable para tus usuarios

2. **Costos**
   - Revisa tu uso en https://platform.openai.com/usage
   - GPT-5.2 costará significativamente más

3. **Calidad de respuestas**
   - Compara respuestas del mismo query con ambos modelos
   - Verifica si la mejora justifica el costo

## ⚠️ Notas Importantes

1. **Compatibilidad API**: GPT-5.2 debe estar disponible en tu cuenta de OpenAI
2. **Rate Limits**: GPT-5.2 puede tener límites más estrictos
3. **Código actualizado**: Los cambios ya están en tu código, solo falta actualizar `.env`
4. **Rollback fácil**: Si hay problemas, cambia `OPENAI_MODEL=gpt-4o-mini` y reinicia

## 🆘 Solución de Problemas

### Error: "Model not found"
- GPT-5.2 puede no estar disponible para tu cuenta aún
- Usa `gpt-4o` mientras tanto
- Contacta a OpenAI para acceso

### Respuestas muy lentas
- Reduce `OPENAI_REASONING_EFFORT` a `low`
- O usa `gpt-5.2-chat-latest` en lugar de `gpt-5.2`

### Costos muy altos
- Usa `gpt-4o` que tiene excelente calidad
- Reserva GPT-5.2 para casos específicos

## 📞 Soporte

Si tienes problemas:
1. Verifica los logs de la API
2. Prueba con `gpt-4o` primero para confirmar que funciona
3. Verifica tu acceso a GPT-5.2 en platform.openai.com

---

**¿Listo para actualizar?** Solo edita tu `.env` y reinicia la API! 🚀
