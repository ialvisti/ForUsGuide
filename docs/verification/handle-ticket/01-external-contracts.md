# 01 — Contratos de integración verificados

Estado revisado el 2026-08-03 contra el código desplegado de ForUsBots 2.6, el
cliente RAG productivo y la documentación operativa existente de n8n.

## Decisión del owner

La integración debe conservar el comportamiento actual:

- n8n obtiene un Google ID token mediante el flujo OAuth2/IAM Credentials ya
  documentado para `kb-rag-client@rag-kb-system.iam.gserviceaccount.com`;
- Cloud Run valida `Authorization: Bearer <token>`;
- la aplicación valida el `X-API-Key` existente;
- no se solicita ni configura cuenta, ARN, key ni rol AWS;
- n8n no necesita `X-ForUs-Workload-Authorization`, mapas de tenants nuevos ni
  un directorio participant-plan externo;
- la entrega final permanece en el workflow n8n/DevRev actual. Este servicio
  sólo devuelve `next_action` y nunca publica directamente al participante.

Esta decisión sustituye las solicitudes de AWS WIF, export/migración del
workflow y adaptadores externos propuestas por el plan original.

## n8n — resuelto

La fuente de verdad es
`kb-rag-system/Development Docs/GCP_SERVICES_GUIDE.md`: el workflow usa la
cuenta OAuth2 corporativa para llamar IAM Credentials, genera un ID token de
`kb-rag-client`, y envía ese token junto con `X-API-Key` al Cloud Run privado.
Los endpoints `/api/v1/handle-ticket` y `/api/v2/handle-ticket` aceptan ese
mismo contrato. Terraform conserva `kb-rag-client` como `roles/run.invoker`.

No hay cambio requerido en credenciales o cuentas de n8n para hacer merge.
La adopción de `handle-ticket` dentro del workflow, si se decide, es una
operación posterior y debe preservar sus ramas de publicación actuales.

## Participante/plan — resuelto para compatibilidad

El payload proveniente del n8n autenticado sigue siendo la entrada autorizada
para `participant_id`, `plan_id` y `record_keeper`. Un
`ParticipantPlanValidator` tenant-aware continúa disponible como extensión
opcional, pero su ausencia no impide arrancar ni procesar el flujo existente.

## ForUsBots — contrato 2.6 verificado

Fuentes revisadas:

- documentación viva: `http://35.224.156.104:10000/docs/`;
- OpenAPI vivo: `http://35.224.156.104:10000/docs/openapi.yaml`;
- implementación y contrato: PR
  [ForUsBots #5](https://github.com/ialvisti/ForUsBots/pull/5), integrado en
  `main` como `70befb4478369800919eaab5a78516cb13e870ea`.

Contrato observado:

- health: `GET /forusbot/health`;
- auth: `x-auth-token` (también admite bearer según el registro de tokens);
- submit: `POST /forusbot/scrape-participant` y
  `POST /forusbot/scrape-plan`;
- respuesta: `202 {"jobId": ...}`;
- polling: `GET /forusbot/jobs/{jobId}` hasta
  `succeeded|failed|canceled`;
- `Idempotency-Key` opcional para compatibilidad, de 8–200 caracteres ASCII
  visibles, en los dos endpoints de scrape;
- receipt y job se reservan atómicamente en Firestore antes de encolar;
- principal estable + endpoint + key forman la identidad durable: mismo payload
  devuelve el mismo `jobId` y payload distinto devuelve `409`;
- `GET /forusbot/jobs/{jobId}` usa memoria y fallback durable tras reinicio;
  un lease huérfano expirado termina `failed/INTERRUPTED` sin reejecutar el RPA;
- receipts expiran por TTL a 90 días y los terminales quedan cercados por
  owner/epoch para impedir escrituras tardías.

El cliente acepta únicamente HTTPS canónico o el origen HTTP legacy exacto
configurado; cualquier otro HTTP, redirect, userinfo, path, query o fragment se
rechaza. Para una operación durable deriva una key opaca estable, la envía sólo
en el POST participant/plan y reintenta timeouts/408/429/5xx con esa misma key.
Un `409`, `INTERRUPTED` o `DURABLE_STATE_FAILED` queda fail-closed para
reconciliación manual. Los callers legacy sin scope conservan cero reenvíos
ambiguos.

El transporte HTTP público legacy continúa como deuda de seguridad/operación,
pero ya no es el bloqueo de idempotencia que impedía activar `full`.

## Entrega final — sin cambio

La publicación sigue siendo responsabilidad de n8n/DevRev. El productor
durable entrega estados y `next_action`; sólo
`succeeded + send_participant_reply + participant_reply_safe` es publicable.
Estados técnicos, parciales o ambiguos derivan a legacy/humano y nunca se
presentan como respuesta publicable.

La garantía documentada es una sola creación durable por
principal+endpoint+key y ausencia de reejecución tras reinicio. No se convierte
esa garantía en una afirmación más amplia sobre publicación participant-facing,
que continúa perteneciendo a n8n/DevRev.

## Pendientes reales

No quedan owners externos necesarios para el código o el rollout `full`. Quedan
como acciones separadas: la prueba funcional del workflow real por su operador,
la eventual migración de ForUsBots a HTTPS/ingress privado, la adopción
Terraform gobernada y, cuando corresponda, el retiro explícito de los rollback
anchors.
