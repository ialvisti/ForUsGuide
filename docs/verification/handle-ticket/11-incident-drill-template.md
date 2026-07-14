# 11 — Plantilla de simulacro de incidente y rollback (Tarea 11 / Tarea 15)

Se llena una copia por simulacro. Ningún campo contiene PII, tokens ni bodies
upstream: sólo `job_hash`, `trace_id`, conteos y timestamps UTC.

## Metadatos

| Campo | Valor |
|---|---|
| Fecha/hora UTC inicio | |
| Operador (drill lead) | |
| Entorno | staging \| production |
| `release_phase` al inicio | |
| Revisión producer / digest | |
| Enlace al runbook | `kb-rag-system/Development Docs/HANDLE_TICKET_RUNBOOK.md` |

## Escenarios ejercitados (Tarea 15 Paso 1)

Marcar cada uno y registrar la señal observada + tiempo a la alerta.

- [ ] Worker defectuoso (5xx sostenido) → alerta `worker_5xx_ratio`
- [ ] Saturación de cola (profundidad/edad) → alerta `ticket_queue_age`
- [ ] Caída del validador participant-plan → 503 fail-closed, sin publicar
- [ ] Timeout de ForusBots → `manual_reconciliation_required`, no reenvío ciego
- [ ] Caída de Pinecone → circuit breaker abierto, fail-fast acotado

## Entrega de alertas (Tarea 11 Paso 3)

Una policy sin notificación comprobada NO supera el gate. Registrar el ack en
`approvals.md` (owner, canal lógico, hora), nunca direcciones privadas/tokens.

| Alerta | Canal lógico | Owner | Hora ack UTC | Enlace al runbook presente |
|---|---|---|---|---|
| | | | | |

## Simulacro de rollback (Tarea 15 Paso 2)

Orden ejecutado y timestamps:

1. [ ] n8n → legacy (contención inmediata, sin gate)
2. [ ] producer a `release_phase` seguro por Terraform (binary plan preaprobado)
3. [ ] pausa de cola SÓLO si la ejecución del worker es insegura
4. [ ] rollback plan generado tras el forward apply contra el state serial
       actual, hasheado/preaprobado ANTES de exponer el cohort (no reusar un
       plan tras drift)
5. [ ] Firestore preservado (NO borrar)
6. [ ] requeue por CLI consciente de generación (si aplica)
7. [ ] reanudar/drenar
8. [ ] polling de todos los jobs aceptados sigue disponible

Confirmaciones:

- [ ] NUNCA se volvió a `kb-rag-system-00047-vkd`
- [ ] cero efectos duplicados / cero replies tardíos tras legacy
- [ ] hash del rollback plan y `APROBADO <GATE> ROLLBACK <HASH>` en approvals

## Evidencia adjunta (sanitizada)

- reporte JUnit/JSON del simulacro:
- capturas de alerta (metadatos, sin PII):
- decisión y timestamps:
- jobs afectados (`job_hash` + estado final):
