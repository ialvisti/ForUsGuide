# Handle-Ticket Runbook

> Operación del flujo durable `POST /api/v{1,2}/handle-ticket` (Firestore +
> Cloud Tasks). Última actualización: 2026-07-11 (remediación Task 11).
> Contexto de diseño: `HANDLE_TICKET_AUDIT_AND_REMEDIATION_PLAN.md` (raíz).

## 1. Topología

- **Productor**: `POST /api/v1/handle-ticket` (adapter híbrido) y
  `POST /api/v2/handle-ticket` (uniforme 202+poll). Autentica principal
  (X-API-Key → `API_CLIENT_KEYS`/`API_KEY`), valida, reserva idempotencia en
  transacción Firestore ANTES de cualquier LLM, encola Cloud Task con nombre
  determinístico `ticket-{job_id}` y responde.
- **Worker**: `POST /internal/tasks/ticket-job` (OIDC de la SA de Cloud
  Tasks; `TICKET_WORKER_SERVICE_ACCOUNT`). Claim transaccional con lease de
  15 min; checkpoints por inquiry; agregación exhaustiva a
  `succeeded|partial|failed|timeout|cancelled` + `next_action`.
- **Poll**: `GET /api/v1/tickets/{id}` / `GET /api/v2/ticket-jobs/{id}` —
  404 = inexistente/expirado; 403 = de otro principal.
- **Colecciones Firestore**: `ticket_jobs` (job + request_payload + resultados
  minimizados) y `ticket_idempotency` (hash → job_id). TTL por `expires_at`
  (`TICKET_JOB_RETENTION_S`, default 24 h) — configurar la TTL policy de
  Firestore sobre `expires_at` en ambas colecciones (IaC).

## 2. Estados y acciones de n8n

Contrato congelado en `tests/fixtures/n8n_handle_ticket_*.json`. Regla de
oro: **sólo `state=succeeded` con `next_action=send_participant_reply` y
`metadata.fallback != true` se publica**. Todo lo demás → legacy/humano.

## 3. Procedimientos

### Replay / retry de un ticket
n8n reintenta el POST con la MISMA `Idempotency-Key`: el servidor replaya el
job existente (`idempotency_replayed=true`) y re-asegura el enqueue si el
crash ocurrió entre record y task. Key igual + payload distinto → `409
IDEMPOTENCY_PAYLOAD_MISMATCH` (bug del productor de payloads; no reintentar).

### Job atascado en `running`
1. Ver `claimed_by`/`claimed_at` en el doc de `ticket_jobs`.
2. El lease expira a los 15 min; el retry de Cloud Tasks (el worker responde
   503 `JOB_CLAIMED_ELSEWHERE` mientras el lease vive) lo re-reclama.
3. Si la task ya agotó reintentos: re-crear la task manualmente
   (`ticket-{job_id}` ya no existirá si la cola la purgó) o marcar el job
   `failed` vía consola para que n8n use legacy.

### Reconciliación ForusBots (`FORUSBOTS_NEEDS_RECONCILIATION`)
Un submit con 5xx ambiguo NO se reintenta (el job RPA pudo crearse). Buscar
en ForusBots por ventana temporal el job del participante; si existe y
terminó, procesar manualmente; si no, reintentar el ticket por legacy. No
existe contrato de idempotencia upstream — pendiente con el equipo ForusBots.

### Cancelación
No hay endpoint público de cancel (v2 lo reserva). Operativo: marcar el doc
`state=cancelled` en Firestore; el claim del worker rechaza jobs terminales.

### Rollback de rollout
`TICKET_HANDLER_MODE` es server-side y el body sólo puede restringirlo:
bajar a `shadow`/`disabled` y redeploy (o env-var update). Los jobs ya
aceptados siguen visibles y se drenan solos; NUNCA borrar las colecciones
durante un rollback. n8n vuelve a legacy por `next_action`.

### Recuperación tras deploy/restart
Nada que hacer: jobs y resultados viven en Firestore; los 202 emitidos
siguen polleables desde cualquier instancia/revisión.

## 4. Métricas y alertas

Contadores emitidos como `ticket_metric <nombre>=<valor>` (log-based
metrics en Cloud Logging):

```
ticket_jobs_accepted / ticket_jobs_replayed / ticket_jobs_conflicted
ticket_jobs_terminal{state=succeeded|partial|failed|timeout|cancelled}
ticket_poll_not_found / ticket_poll_forbidden
ticket_rate_limited / ticket_outstanding_capped
```

Alertar (umbrales iniciales, ajustar con datos):
- `ticket_jobs_terminal{state=partial|failed|timeout}` > 10 % de accepted / 15 min
- `ticket_poll_not_found` > 0 sostenido (jobs perdidos: NO debe ocurrir ya)
- `ticket_rate_limited`/`ticket_outstanding_capped` sostenidos (capacidad)
- auth failures 401/403 spike (log_requests)
- gasto LLM/Pinecone/ForusBots: presupuestos de billing GCP + cuotas provider
- Cloud Tasks: queue depth / oldest task age (métricas nativas de la cola)

## 5. Probes

- `/livez` — proceso vivo, sin I/O.
- `/readyz` — clientes y config críticos inicializados; 503 si no puede
  aceptar trabajo.
- `/health` — incluye dependencia Pinecone real (stats) y modos de rollout;
  un error de stats YA NO se reporta como conectado (HT-24).

## 6. Datos y retención (PII)

- `ticket_jobs.request_payload` y los resultados contienen texto del ticket:
  retención = TTL `expires_at`. Acceso: sólo la SA runtime y operadores con
  rol Firestore explícito. Borrado ad-hoc: eliminar el doc (y su entrada en
  `ticket_idempotency` si aplica).
- Nunca se persisten: API keys, Idempotency-Key raw (sólo hash scoped),
  bodies de error upstream.
- Logs: IDs de participante/plan pseudonimizados en labels ForusBots; texto
  de inquiries/subqueries/LLM raw redactado (sólo conteos/longitudes).

## 7. Pendientes operativos (fuera del repo)

Ver `GCP_SERVICES_GUIDE.md` §Ticket Handler Containment: HTTPS/rotación de
token ForusBots, workflow n8n (consumir `next_action` + guard de shadow,
poll deadline ≥ 540 s, aceptar 202 en toda ruta), fuente canónica
participant-plan para `participant_plan_validator`, captura sanitizada del
despliegue real, TTL policies + cola Cloud Tasks en IaC.
