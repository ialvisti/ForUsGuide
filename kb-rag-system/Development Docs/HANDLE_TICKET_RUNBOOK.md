# Handle-Ticket Runbook

> Operación del flujo durable `POST /api/v{1,2}/handle-ticket` (Firestore +
> Cloud Tasks). Última actualización: 2026-07-14 (plan de finalización).
> Contexto de diseño: `HANDLE_TICKET_AUDIT_AND_REMEDIATION_PLAN.md` +
> `docs/plans/2026-07-13-handle-ticket-production-completion.md`.

## 1. Topología

Roles de proceso EXCLUYENTES (`APP_ROLE`), misma imagen inmutable:

- **Producer** (`APP_ROLE=producer`, servicio `kb-rag-system`): API completa
  existente + `POST /api/v{1,2}/handle-ticket` y polls. Autentica principal/
  tenant (X-API-Key → `API_CLIENT_KEYS`/`API_CLIENT_TENANTS`) y, en v2,
  verifica la identidad workload (`X-ForUs-Workload-Authorization`, ID token
  WIF). Autoriza participant-plan-tenant fail-closed, reserva idempotencia +
  cuotas en UNA transacción Firestore ANTES de cualquier LLM, encola Cloud
  Task `ticket-{job_id}-g{generation}` y responde. NUNCA sirve `/internal/*`.
- **Worker** (`APP_ROLE=worker`, servicio privado `kb-rag-ticket-worker`,
  ingress internal): sólo `POST /internal/tasks/ticket-job` (OIDC de la task
  signer) + probes. Claim con `lease_epoch`/owner/expiry (lease 90 s,
  heartbeat 30 s); checkpoints por inquiry condicionados al epoch; reanuda
  desde el `execution_plan` persistido sin repetir efectos; agregación
  exhaustiva. Una generación stale/job desconocido/terminal → 204 sin efecto.
- **Reconciler** (`APP_ROLE=reconciler`, Run Job + Scheduler cada 1 min):
  `python -m data_pipeline.ticket_reconciler --once --batch-size=25`. Repara
  outbox pending y leases vencidos (recovery lock separado del lease de
  ejecución), terminaliza deadlines/payloads ausentes. No sirve HTTP.
- **Poll**: `GET /api/v1/tickets/{id}` / `GET /api/v2/ticket-jobs/{id}` —
  404 = inexistente; 410 = el control/tombstone vive pero el payload expiró
  (no reintentar con la misma key); 403 = de otro principal.
- **Colecciones Firestore** (base NOMBRADA: `(default)` prod, `ticket-staging`
  staging — la base es el límite de aislamiento, no un prefijo):
  - `ticket_jobs` — control/tombstone SIN PII; terminal retiene
    `TICKET_IDEMPOTENCY_RETENTION_DAYS` (≥90 d), no terminal sin TTL;
  - `ticket_job_payloads` — request/plan/checkpoints/resultado con PII;
    `expires_at` nativo a 24 h (fail-safe de privacidad);
  - `ticket_idempotency_receipts` — hash → job_id, TTL = retención;
  - `ticket_active_counters` — cuota por principal, sin TTL mientras >0;
  - `ticket_rate_windows` — ventana de tasa durable, TTL 48 h.
  TTL/índices declarados en `firestore.indexes.json` + `infra/terraform`.

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
1. Ver `lease_owner`/`lease_expires_at`/`lease_epoch` en el doc de
   `ticket_jobs`.
2. El lease expira a los 90 s; el reconciliador automático (cada 1 min)
   fencea al worker viejo (incrementa `lease_epoch`), transiciona
   `running→queued` y re-encola con generación nueva. No requiere acción.
3. Si hace falta forzarlo antes: usar la **CLI auditada** (NO recrear el
   mismo nombre de task): `APP_ROLE=reconciler python -m
   scripts.requeue_ticket_job --job-id JOB --operator you@forusall.com`.
   Rechaza jobs terminales y leases activos; incrementa la generación
   transaccionalmente y registra sólo `job_hash`/generación/operador.

### Reconciliación ForusBots (`FORUSBOTS_NEEDS_RECONCILIATION`)
Un submit con 5xx ambiguo NO se reintenta (el job RPA pudo crearse). Buscar
en ForusBots por ventana temporal el job del participante; si existe y
terminó, procesar manualmente; si no, reintentar el ticket por legacy. No
existe contrato de idempotencia upstream — pendiente con el equipo ForusBots.

### Cancelación
No hay endpoint público de cancel (v2 lo reserva). Operativo: marcar el doc
`state=cancelled` en Firestore; el claim del worker rechaza jobs terminales.

### Rollback de rollout (producer vs worker)
La contención SIEMPRE empieza en **n8n → legacy** (inmediata, sin gate). El
cambio de modo del producer se hace SÓLO por Terraform (production-plan al
último `release_phase` seguro → apply del binary plan preaprobado; ver
Tarea 17 Paso 5). Distinción:

- **Rollback del producer**: volver el `release_phase`/modo a `dark_100`
  (disabled) por Terraform. Es el rollback anchor endurecido.
- **Rollback del worker**: pausar la cola SÓLO si la ejecución es insegura;
  los jobs en vuelo drenan o se cancelan.

NUNCA volver a `kb-rag-system-00047-vkd` (imagen vulnerable + full). NUNCA
borrar Firestore. Preservar el polling de todos los jobs aceptados. El
rollback config no depende de un plan stale: se regenera contra el state
serial actual y se hashea/preaprueba antes de exponer el cohort.

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
