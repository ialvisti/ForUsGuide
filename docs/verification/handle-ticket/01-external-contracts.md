# 01 — Inventario de contratos externos (Tarea 1)

Estado al 2026-07-13. Este documento registra, por cada uno de los cuatro contratos que
bloquean la activación, lo que el repositorio ya sabe (con citas), lo que **debe** entregar
el equipo propietario, y el estado del bloqueo. Ninguna fila contiene valores de secretos.

**Estado global: los cuatro contratos están PENDIENTES.** Conforme al punto de control STOP
de la Tarea 1, el trabajo continúa en modo local/infraestructura desactivada; el E2E activo
de staging y el despliegue progresivo quedan bloqueados hasta cerrar estos contratos.

Los conectores de mensajería de esta sesión (Slack/Salesforce/Atlassian/Linear/DevRev) no
están autenticados, por lo que las solicitudes a los equipos deben enviarlas los humanos;
cada sección incluye la lista exacta de lo que hay que pedir.

---

## 1. Fuente canónica participante-plan-tenant — **PENDIENTE (bloqueante)**

**Lo que el repo sabe:**

- El seam existe pero siempre es `None`: `api/main.py:333` (`app.state.participant_plan_validator = None`);
  el check en `api/main.py:1224-1238` sólo corre `if validator is not None` ⇒ **fail-open hoy**.
- La firma runtime actual es un callable posicional de 2 args sin tenant
  (`await validator(participant_id, plan_id)`, `main.py:1227`); el contrato objetivo de la
  Tarea 4 es tenant-aware (`authorize(*, tenant_id, participant_id, plan_id)`).
- `validate_settings()` no exige el validador en modos activos (nada fail-closed en config).
- `TicketJobRecord.tenant_id` existe pero nada lo puebla desde una fuente autorizada.
- El runbook lo lista como "pendiente operativo (fuera del repo)"
  (`Development Docs/HANDLE_TICKET_RUNBOOK.md:106-112`).

**Owner:** sin nombre en el repo — "el equipo propietario" del directorio de participantes.
Aprobador del gate: fila "participant-plan" en G4/G6A/G9.

**Solicitud abierta (qué pedir):** endpoint o librería y su propietario; método de auth y
audiencia; schema exacto request/response; campos de tenant y record keeper devueltos;
timeout/SLA y semántica de errores; un par sintético autorizado y un mismatch sintético.
Criterio: responder "¿P pertenece a L en T?" sin confiar en texto del ticket ni valores de n8n.

**Fixtures destino (no creados; no se inventan):**
`tests/fixtures/participant_plan/authorized_pair.json`, `tests/fixtures/participant_plan/rejected_pair.json`.

---

## 2. Contrato HTTPS + idempotencia de ForusBots — **PENDIENTE (bloqueante para GR/full)**

**Lo que el repo sabe (cliente `data_pipeline/forusbots_client.py`):**

- Transporte actual: `http://35.224.156.104:10000` (HTTP plano a IP pública). El hostname
  antiguo de Render (`forusbots-6jyh.onrender.com`) está explícitamente descartado.
- Auth: header `x-auth-token`. Async-only: `POST /forusbot/scrape-participant|scrape-plan`
  → `202 {jobId}` → poll `GET /forusbot/jobs/{id}` (`succeeded|failed|canceled`); no hay
  endpoint separado de result; `/forusbot/health` sólo se ejercita en el test live opt-in.
- **El upstream no deduplica y no acepta idempotency key ni lookup por correlation ID**: un
  5xx tras el POST es irresoluble y el cliente lanza `ForusBotsAmbiguousSubmit`
  (`needs_reconciliation=True`). El dedupe del cliente es sólo in-process.
- Capacidad asumida `maxConcurrency=3` (cliente usa `max_inflight=2`); sin confirmación del owner.
- IDs de prueba usados hoy (158948, 342393, 580) son literales ad-hoc, **no** IDs sintéticos
  sancionados por el equipo.

**Owner:** equipo ForusBots (sin individuos nombrados en el repo); owner de cert/DNS por identificar.

**Solicitud abierta:** URL base HTTPS verificada + owner de certificado/DNS; contratos de
`/health`/submit/status/result; capacidad global de concurrencia/tasa; IDs sintéticos;
procedimiento de rotación del token (el actual debe tratarse como expuesto por viajar en HTTP);
key de idempotencia aceptada por submit **o** búsqueda por correlation ID estable; dedupe
documentado, retención de keys ≥ horizonte de replay y reconciliación tras timeout/reset.

**STOP de GR (vigente):** sin idempotencia/reconciliación observable en ForusBots y en el
canal de entrega final, `full` no puede activarse ni afirmarse "cero efectos duplicados".

**Fixture destino (no creado):** `tests/fixtures/forusbots/live_contract.sanitized.json`.

---

## 3. Export real del workflow n8n + identidad AWS→GCP — **PENDIENTE (bloqueante)**

**Lo que el repo sabe:**

- **No existe export real/sanitizado del workflow de n8n en el repo**; los dos fixtures
  (`n8n_handle_ticket_request.json`, `n8n_handle_ticket_polling.json`) declaran
  `provenance: RECONSTRUIDO`, cubren sólo v1 y omiten el comportamiento real del consumidor
  de `next_action` (bloqueo #10 del plan).
- n8n corre en **AWS EC2** (`Development Docs/INFRASTRUCTURE_DIAGRAM_EXPLAINED.md:96-97`).
- **Identidad actual: OAuth humano** — `ivan.alvis@forusall.com` con
  `roles/iam.serviceAccountTokenCreator` genera ID tokens de `kb-rag-client@` vía IAM
  Credentials API (`GCP_SERVICES_GUIDE.md:89-98,649-696`). El plan prohíbe credenciales
  humanas en el flujo; deben cerrarse con la migración WIF (Tarea 10 Paso 5 / G3).
- WIF se intentó antes y se desactivó (`sts.googleapis.com` disabled,
  `GCP_SERVICES_GUIDE.md:583`); habrá que reactivarla vía IaC platform (G1B).
- El pipeline legacy documentado llama `/api/v1/required-data` + `/api/v1/generate-response`
  por inquiry; el consumo de handle-ticket v2 es el objetivo del workflow nuevo.

**Owner:** owner de n8n (instancia `n8n.forusall.com` / `n8nhooks.forusall.com`); sin
individuo nombrado. La cuenta AWS/ARN del execution role debe venir de ese owner.

**Solicitud abierta:** export de respaldo del workflow real (previo a cualquier edición);
export sanitizado preservando nombres/expresiones/casing/null/timeouts/retries/ramas; cuenta
AWS + ARN exacto del execution role de n8n + mecanismo de credenciales temporales; y
confirmación de si el runtime n8n puede hacer WIF/impersonation (si no puede: STOP y diseñar
broker service-to-service aprobado — no usar credenciales humanas).

**Contrato objetivo registrado:** AWS WIF → pool/provider GCP con condition por cuenta+ARN →
`n8n-ticket-invoker-{env}@rag-kb-system.iam.gserviceaccount.com` → ID token con audiencia del
producer e `includeEmail=true`, enviado en `X-ForUs-Workload-Authorization: Bearer <token>`
(nunca `X-Serverless-Authorization`); `X-API-Key` identifica cliente/tenant pero no autoriza
solo. Nota de preflight: el servicio hoy es **privado** (invoker únicamente `kb-rag-client@`),
así que n8n además debe mandar el mismo token en `Authorization: Bearer` mientras Cloud Run
IAM esté delante.

**Fixture destino (no creado):** `tests/fixtures/n8n/handle_ticket_workflow.sanitized.json`.

---

## 4. Contrato idempotente de entrega final al participante — **PENDIENTE (bloqueante para publicar)**

**Lo que el repo sabe:**

- La entrega es vía **DevRev** (CRM), orquestada por n8n: el DevRev AI Agent redacta la
  respuesta y llama a `https://n8nhooks.forusall.com/webhook/final-handling`; n8n escribe
  `participant_reply`/`internal_notes` en el ticket DevRev y fija el stage
  (`INFRASTRUCTURE_DIAGRAM_EXPLAINED.md:296-329`). No hay Zendesk/Front/email.
- **No existe ledger de delivery ni dedupe** en kb-rag-system ni (documentado) en n8n; el
  plan exige que n8n reclame transaccionalmente el delivery y persista/reconcilie el
  delivery ID (Tarea 9 Paso 1.8).
- Se desconoce si la API de DevRev acepta una key estable derivada del evento y si permite
  reconciliar un timeout ambiguo; sin eso no puede garantizarse cero duplicados.

**Owner:** owners de n8n/delivery + equipo DevRev; producto/operaciones para los gates G8/G9.

**Solicitud abierta:** sistema/nodo exacto que publica el reply para handle-ticket; si acepta
key estable y devuelve/reconcilia el mismo delivery ID; horizonte máximo de redelivery de la
fuente; retención de dedupe del receptor; estados y semántica del timeout ambiguo.
Con esas cifras se fija `TICKET_IDEMPOTENCY_RETENTION_DAYS = max(90d, horizonte fuente,
dedupe downstream, retención rollback)`; hasta entonces rige el default **90d** como mínimo.

**Fixture destino (no creado):** `tests/fixtures/participant_delivery/live_contract.sanitized.json`.

---

## 5. Umbrales diferenciales — **defaults del plan vigentes; aprobación product/ops pendiente**

Hasta que producto/operaciones los cambien explícitamente, rigen los valores seguros del plan:

| Métrica | Umbral |
|---|---|
| IDs, hechos, módulos y límites de tokens determinísticos | coincidencia exacta 100% |
| Tasa de publicación insegura | 0% |
| Tasa de inquiries faltantes | 0% |
| Aceptabilidad semántica vs casos legacy revisados | ≥95% |
| Tasa de respuestas duplicadas al participante | 0% |
| Tasa de 404 de polling sin explicación | 0% |

El arnés real (`rag-testing/ticket_differential.py` + `ticket_differential_thresholds.json`)
se crea en la Tarea 9; el `run_ticket_differential()` actual de `test_endpoints_stress.py`
no llama al sistema legacy ni calcula diferencia (bloqueo #11 confirmado).

---

## Registro de bloqueos

| # | Contrato | Owner responsable | Estado | Fecha solicitud | Fecha respuesta |
|---|---|---|---|---|---|
| 1 | Fuente canónica participant-plan-tenant | equipo directorio participantes (por nombrar) | **BLOQUEADO** — solicitud redactada, pendiente de envío por el requester | 2026-07-13 (redactada) | — |
| 2 | ForusBots HTTPS + idempotencia/reconciliación | equipo ForusBots + owner cert/DNS | **BLOQUEADO** — ídem | 2026-07-13 (redactada) | — |
| 3 | Export n8n real + AWS ARN/WIF | owner n8n | **BLOQUEADO** — ídem | 2026-07-13 (redactada) | — |
| 4 | Entrega final idempotente (DevRev vía n8n) | owners n8n/delivery + DevRev | **BLOQUEADO** — ídem | 2026-07-13 (redactada) | — |
| 5 | Umbrales diferenciales | producto + operaciones | defaults del plan activos; ratificación pendiente | 2026-07-13 | — |

**Consecuencia operativa (STOP de Tarea 1):** pueden ejecutarse las Tareas 2–12 (trabajo
local, mocks, IaC declarativa, CI) y los pasos de infraestructura desactivada gateados por
G1A/G1B/G1C/G2. No puede iniciarse: E2E activo en staging (G4), migración WIF en n8n (G3),
ni ningún escalón de producción (G5+). Los fixtures live de las Tareas 4/8/9 usan el
contrato mock hasta que lleguen los contratos reales.
