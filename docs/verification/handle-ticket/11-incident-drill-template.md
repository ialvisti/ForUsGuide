# 11 — Plantilla de simulacro de observabilidad, incidente y rollback

Se crea una copia por simulacro. Todo timestamp es UTC. La evidencia sólo
puede contener `job_hash`, `trace_id`, estados, códigos y conteos; nunca texto
del ticket, identificadores de participante/plan, payloads, tokens, URLs
firmadas ni errores upstream sin sanitizar.

## Metadatos y baseline

| Campo | Valor |
|---|---|
| Inicio UTC / fin UTC | |
| Drill lead / incident commander | |
| Entorno | staging \| production |
| `release_phase` inicial | |
| Producer revision + digest | |
| Worker revision + digest | |
| Queue / reconciler lógico | |
| State serial + rollback plan hash | |
| Baseline segura preacordada | legacy en n8n + producer disabled/dark |
| Enlace al runbook | `kb-rag-system/Development Docs/HANDLE_TICKET_RUNBOOK.md` |

Antes de inyectar un fallo:

- [ ] El dashboard `[env] Ticket handler operations` carga datos del entorno
      correcto y no mezcla servicios ni colas.
- [ ] Dos canales on-call aprobados recibieron una notificación de prueba.
- [ ] Owner, canal lógico y hora del ack están en `approvals.md`; no se
      registraron direcciones privadas ni tokens.
- [ ] Existe rollback plan contra el state serial actual, con hash aprobado.
- [ ] n8n puede volver a legacy sin desactivar polling de jobs ya aceptados.

## Matriz de alertas ejecutables

Registrar para cada alerta: hora de inyección, primera serie anómala, apertura
del incidente, recepción por cada canal, ack y recuperación.

| Policy Terraform | Señal/umbral | Primera respuesta segura | Resultado/tiempos |
|---|---|---|---|
| `ticket_poll_not_found` | 404 > 0 durante 5m | comprobar receipt/control y ruta; no recrear el job | |
| `ticket_poll_gone` | 410 > 0 durante 5m | tratar como terminal; revisar TTL/watch, no reintentar misma key | |
| `ticket_terminal_incorrect_ratio` | terminales incorrectos >10% durante 15m | contener cohort en n8n y separar códigos técnicos | |
| `ticket_accepted_terminal_ratio` | terminales/aceptados <99% durante 15m | buscar jobs activos o terminales sin evento; contener admisión sin reejecutar efectos | |
| `ticket_terminal_failed` | primer terminal `failed` | inspeccionar código/fase sanitizados; no consultar payloads | |
| `ticket_terminal_partial` | primer terminal `partial` | confirmar degradación publicable y contener la cohorte afectada | |
| `ticket_terminal_internal_error` | primer terminal `INTERNAL_ERROR` | abrir incidente técnico y correlacionar sólo por hashes/traces sanitizados | |
| `ticket_queue_backlog` | depth >50 o p99 dispatch delay >120s durante 10m | contener admisión; revisar worker antes de cambiar capacidad | |
| `worker_5xx_ratio` | 5xx/requests >1% durante 5m | n8n a legacy; preservar store y polling | |
| `producer_auth_failure_ratio` | 401/403 >5% durante 5m | revisar token IAM de `kb-rag-client` y `X-API-Key`; nunca relajar auth | |
| `ticket_lease_fencing` | reconciler fencea un lease vencido | verificar heartbeat, epoch y generación; no revivir worker viejo | |
| `ticket_oldest_active_job` | job activo >2400s (deadline absoluto) | comprobar heartbeat/fencing; no repetir efectos externos | |
| `ticket_reconciler_health` | sin heartbeat 10m o errores >0 | revisar Job/Scheduler/locks; CLI sólo break-glass auditado | |
| `ticket_forusbots_reconciliation` | circuit abierto, fallo o conciliación manual | consultar estado upstream; nunca repetir POST ambiguo | |
| `ticket_pinecone_circuit` | circuit breaker abierto | mantener fail-fast y usar fallback/legacy | |
| `ticket_task_delivery_deadline` | intentos no-OK 5m o deadline terminalizado | inspeccionar retry horizon; requeue sólo con nueva generación | |
| `ticket_billable_time_budget` | segundos facturables/hora sobre guardrail | contener cohort/capacidad y revisar presupuesto de Billing | |
| `ticket_llm_cost_budget` | costo LLM estimado >5 USD/h staging o >50 USD/h production | contener cohort y revisar tokens/modelo/presupuesto | |

## Semántica de Cloud Tasks: no inventar una DLQ

Cloud Tasks no ofrece una DLQ nativa. La evidencia equivalente del incidente
es la combinación de:

1. `queue/task_attempt_count` no-OK sostenido;
2. `queue/depth` y `queue/task_attempt_delays`;
3. `deadline_terminalized` del reconciliador;
4. estado durable, generation y receipt del job, identificados sólo por hash.

Una task agotada o tombstoned no se recrea con el mismo nombre. Se usa la CLI
auditada de requeue, que incrementa generación y rechaza jobs terminales o con
lease activo. Registrar hash, generación anterior/nueva y operador; no pegar
documentos Firestore en esta evidencia.

## Escenarios mínimos del drill

### Worker defectuoso

- [ ] Provocar 5xx sostenido sólo en staging.
- [ ] Confirmar que `worker_5xx_ratio` usa numerador 5xx y denominador total.
- [ ] Confirmar que `ticket_accepted_terminal_ratio` compara eventos canónicos
      y alcanza razón 1:1 después de la ventana terminal controlada.
- [ ] Confirmar que ambos canales reciben y reconocen el incidente.
- [ ] Ejecutar contención n8n→legacy; comprobar polling de aceptados.

### Saturación de cola y deadline

- [ ] Superar depth o dispatch delay sin cambiar capacidad durante la prueba.
- [ ] Observar `ticket_queue_backlog` y el panel de Cloud Tasks.
- [ ] Simular intentos no-OK y confirmar `ticket_task_delivery_deadline`.
- [ ] Probar pause/resume sólo si ejecutar el worker es inseguro; registrar
      quién autorizó y cuándo se drenó la cola.

### Lease/reconciler

- [ ] Perder un heartbeat y observar fencing con epoch nuevo.
- [ ] Confirmar `ticket_oldest_active_job` al superar el deadline absoluto de 2400s.
- [ ] Confirmar que el worker viejo no publica ni checkpointea.
- [ ] Detener una ejecución programada y observar ausencia del reconciler.
- [ ] Restaurar Scheduler/Job declarativamente y comprobar reanudación.

### Dependencias

- [ ] ForusBots timeout/fallo ambiguo: abrir circuit, conciliar y no reenviar.
- [ ] Pinecone degradado: circuit abierto y fallback acotado.
- [ ] Forzar costo LLM controlado y confirmar `ticket_llm_cost_budget` sin
      registrar prompts, respuestas ni IDs.
- [ ] Fallo de identidad: 401/403 fail-closed, sin desactivar segundo factor.

## Entrega real a dos canales

Una policy creada sin entrega comprobada no supera el gate. Rellenar dos filas
por policy (una por canal) o adjuntar un reporte automatizado equivalente.

| Policy | Canal lógico | Owner | Envío UTC | Recepción UTC | Ack UTC | Runbook enlazado |
|---|---|---|---|---|---|---|
| | canal primario | | | | | |
| | canal secundario | | | | | |

## Rollback separado por componente

1. [ ] n8n → legacy como contención inmediata.
2. [ ] Producer → baseline segura mediante plan binario aprobado; conservar
       worker/polling para jobs ya aceptados.
3. [ ] Worker → digest anterior sólo si la ejecución es insegura. No confundir
       este rollback con el del producer ni cambiar ambos sin evidencia.
4. [ ] Pausar cola únicamente si cada nueva ejecución puede causar daño.
5. [ ] Preservar Firestore, receipts, payloads y contadores; nunca borrar.
6. [ ] Requeue por CLI/generación después de clasificar efectos ambiguos.
7. [ ] Reanudar y drenar con 5xx, depth, delay, fencing y deadlines visibles.
8. [ ] Confirmar estado final de todos los hashes aceptados antes del corte.

Confirmaciones obligatorias:

- [ ] No se volvió a la revisión vulnerable histórica.
- [ ] Cero replies tardíos después del corte a legacy.
- [ ] Cero efectos duplicados; ambiguos quedaron en conciliación manual.
- [ ] Hash del rollback plan y aprobación se registraron en `approvals.md`.

## Evidencia sanitizada adjunta

- Dashboard export/capturas (sin PII):
- Alert incidents y tiempos por canal:
- Reporte JUnit/JSON del drill:
- Decisiones y timestamps:
- Jobs afectados (`job_hash`, `trace_id`, estado final):
- Queue depth/delay e intentos no-OK:
- Fencing/reconciler/deadline counters:
- Resultado de rollback y drain:
