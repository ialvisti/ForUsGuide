# 01 — Contratos de integración verificados

Estado revisado el 2026-07-27 contra el código local, la documentación viva de
ForUsBots 2.5 y la documentación operativa existente de n8n.

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

## ForUsBots — contrato 2.5 verificado

Fuentes revisadas:

- documentación viva: `http://35.224.156.104:10000/docs/`;
- OpenAPI vivo: `http://35.224.156.104:10000/docs/openapi.yaml`;
- implementación local: `/Users/ivanalvis/Desktop/ForUsBots/`.

Contrato observado:

- health: `GET /forusbot/health`;
- auth: `x-auth-token` (también admite bearer según el registro de tokens);
- submit: `POST /forusbot/scrape-participant` y
  `POST /forusbot/scrape-plan`;
- respuesta: `202 {"jobId": ...}`;
- polling: `GET /forusbot/jobs/{jobId}` hasta
  `succeeded|failed|canceled`;
- el job store es de proceso y no hay idempotency key ni lookup por
  correlation ID.

El cliente acepta únicamente HTTPS canónico o el origen HTTP legacy exacto
`http://35.224.156.104:10000`; cualquier otro HTTP, redirect, userinfo, path,
query o fragment se rechaza. Ante un submit ambiguo no reintenta a ciegas:
conserva el estado para reconciliación/manual y evita duplicar trabajo RPA.

La falta de idempotencia upstream es una limitación operativa conocida, no un
contrato pendiente ni un bloqueo de merge.

## Entrega final — sin cambio

La publicación sigue siendo responsabilidad de n8n/DevRev. El productor
durable entrega estados y `next_action`; sólo
`succeeded + send_participant_reply + participant_reply_safe` es publicable.
Estados técnicos, parciales o ambiguos derivan a legacy/humano y nunca se
presentan como respuesta publicable.

No se afirma exactly-once sobre un sistema externo que no lo documenta, pero
eso no exige modificar el workflow actual para integrar o hacer merge de esta
rama.

## Pendientes reales

No quedan owners externos necesarios para el merge. Las únicas aprobaciones
posteriores son operativas y explícitas: crear/aplicar infraestructura,
activar staging, promover tráfico y retirar el rollback anchor. La revisión
semántica de respuestas sigue siendo un gate de promoción, no de merge.
