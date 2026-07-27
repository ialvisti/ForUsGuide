# 00 — Preflight: baseline segura de handle-ticket (2026-07-13)

Capturado en modo read-only el 2026-07-13 por `ivan.alvis@forusall.com` (sesión gcloud interactiva activa).
Proyecto GCP: `rag-kb-system` (`900340137010`), región `us-central1`.

## Regla de contención permanente

> **Nunca hagas rollback a `kb-rag-system-00047-vkd`.** Esa revisión ejecuta la misma imagen
> vulnerable con `TICKET_HANDLER_MODE=full` y no es un destino de rollback válido.
> El rollback anchor provisional es `kb-rag-system-00048-bkc` (disabled) hasta que la Tarea 16
> establezca la baseline endurecida `dark_100`.

## Git

| Campo | Valor |
|---|---|
| Rama fuente | `handle-ticket-hardening` @ `3d48415577f24c6f22e020cde6983b568b476a71` |
| Posición vs `main` | 14 commits por delante, 0 por detrás (`git rev-list --left-right --count`: `0 14`) |
| `git diff --check main...handle-ticket-hardening` | sin salida (limpio) |
| Worktree de ejecución | `/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization`, rama `handle-ticket-production-finalization`, creado limpio en `3d48415` |
| Árbol original | sucio con cambios propiedad del usuario (`.gitignore`, JSON de PA, planes eliminados, `response.json`, `docs/`); **no tocar** — prohibido `git add -A`/reset/checkout/clean/stash allí |

## Cloud Run — servicio de producción `kb-rag-system`

| Campo | Valor |
|---|---|
| Revisión activa | `kb-rag-system-00048-bkc` |
| Tráfico | 100% → `kb-rag-system-00048-bkc`, sin tag (única entrada de tráfico) |
| Imagen | `us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:4aa9853bf85f08513cd3f4859e40ec8693b3c3105a101cce8cf7d9b6bc7969ff` (tag histórico `:66f8350`) |
| Modo | `TICKET_HANDLER_MODE=disabled` |
| Runtime SA | `kb-rag-runner@rag-kb-system.iam.gserviceaccount.com` |
| Invoker IAM | **sin `allUsers`** — único binding `roles/run.invoker`: `kb-rag-client@rag-kb-system.iam.gserviceaccount.com` (etag `BwZO9B4rTuA=`) |
| Revisión prohibida | `kb-rag-system-00047-vkd` (misma imagen, `TICKET_HANDLER_MODE=full`) |

Consumidores conocidos: el invoker de producción es la SA `kb-rag-client`,
usada por el caller de n8n según la documentación operativa. La decisión del
owner del 2026-07-27 conserva este binding y contrato sin migración AWS/WIF.

## Env vars de la revisión activa (nombres + valores seguros; secretos sólo como referencia)

```
ENVIRONMENT=production
LOG_LEVEL=INFO
GCP_PROJECT=rag-kb-system
INDEX_NAME=kb-articles-production
NAMESPACE=kb_articles
OPENAI_MODEL=gpt-5.4
OPENAI_REASONING_EFFORT=medium
API_KEY=<secret:api-key:latest>
OPENAI_API_KEY=<secret:openai-api-key:latest>
PINECONE_API_KEY=<secret:pinecone-api-key:latest>
ENABLE_EXECUTION_LOGGING=true
GCS_BUCKET=rag-kb-system-kb-articles
USE_VERTEX_AI=true
GCP_LOCATION=us-central1
LLM_ROUTE_DECOMPOSE=gpt-5.5
LLM_ROUTE_REQUIRED_DATA=gpt-5.5
LLM_ROUTE_GR_OUTCOME=gpt-5.5
LLM_ROUTE_GR_RESPONSE=gpt-5.5
LLM_ROUTE_KNOWLEDGE=gpt-5.5
LLM_ROUTE_CLASSIFY=gpt-5.5
FORUSBOTS_AUTH_TOKEN=<secret:FORUSBOTS_AUTH_TOKEN:latest>
FORUSBOTS_BASE_URL=http://35.224.156.104:10000
TICKET_HANDLER_MODE=disabled
```

Hallazgos:

- **Todas las secret refs usan `latest`** — viola el requisito de versiones numéricas inmutables;
  se corrige vía manifest G6A (producción) / G3 (staging), no ahora.
- **`FORUSBOTS_BASE_URL` es el origen HTTP legacy revisado**. La documentación
  viva y el código ForUsBots 2.5 confirman ese deployment. El candidato permite
  sólo ese origen HTTP exacto y rechaza cualquier otro.

## IAM a nivel proyecto (bindings relevantes)

| SA | Roles |
|---|---|
| `kb-rag-runner@` | `roles/aiplatform.user`, `roles/datastore.user` (**project-wide** — bloquea aislamiento `ticket-staging`; migración G1C), `roles/logging.logWriter`, `roles/monitoring.metricWriter`, `roles/secretmanager.secretAccessor`, `roles/storage.objectViewer` |
| `kb-rag-client@` | `roles/aiplatform.user` (+ `run.invoker` sobre el servicio, ver arriba) |
| `900340137010-compute@` (Compute default) | `roles/artifactregistry.writer`, `roles/logging.logWriter`, **`roles/run.admin`**, **`roles/storage.admin`** |
| `900340137010@cloudbuild` | `roles/cloudbuild.builds.builder`, `roles/iam.serviceAccountUser`, **`roles/run.admin`** |

Los `run.admin` de Compute/Cloud Build SA confirman que el pipeline actual puede desplegar
directo a producción; se neutraliza en Tarea 12 con G1B.

La inspección read-only repetida el 2026-07-21 confirmó que
`kb-rag-runner@` conserva `roles/secretmanager.secretAccessor` sin condición a
nivel proyecto y además un member directo sobre `FORUSBOTS_AUTH_TOKEN`. Por
tanto, quitar el env/cliente ForusBots del producer es necesario pero no basta
para una frontera IAM live. La revisión candidata debe usar una SA productiva
separada y demostrar effective-IAM DENIED sobre ese secreto; la SA legacy y sus
grants se preservan mientras `00048-bkc` sea el rollback anchor.

## Endpoints OpenAPI (rutas actuales por rol)

No-ticket (deben preservarse intactas en `APP_ROLE=producer`):
`/`, `/ui`, `/ui/chunks`, `/ui/knowledge`, `/ui/router`, `/health`, `/livez`, `/readyz`,
`/api/v1/chunks`, `/api/v1/generate-response`, `/api/v1/index-stats`,
`/api/v1/knowledge-question`, `/api/v1/required-data`, `/api/v1/route-inquiry`.

Ticket:
`/api/v1/handle-ticket`, `/api/v1/tickets/{ticket_job_id}`,
`/api/v2/handle-ticket`, `/api/v2/ticket-jobs/{ticket_job_id}`,
`/internal/tasks/ticket-job` (router del worker, `include_in_schema=False`).

## Dependencias core no-ticket

- GCS bucket `rag-kb-system-kb-articles` (objectViewer).
- Vertex AI (`USE_VERTEX_AI=true`, `roles/aiplatform.user`).
- Pinecone índice `kb-articles-production`, namespace `kb_articles` (sólo lectura en este plan).
- OpenAI (rutas LLM `gpt-5.4`/`gpt-5.5`).
- Firestore `(default)` Native en `us-central1`.

## Estado de infraestructura de tickets (confirmado ausente)

- `cloudtasks.googleapis.com` y APIs de análisis de contenedores: desactivadas.
- Sin colas de Cloud Tasks, SA invocadora de tasks, políticas TTL, índices compuestos,
  métricas de logs de tickets ni alertas de tickets.
- Trigger de Cloud Build `deploy-kb-rag-system` observa `^main$`, sin aprobación, despliega
  directo a producción; su bucket de artefactos declarado no existe.

## Toolchain local (2026-07-13)

| Herramienta | Estado |
|---|---|
| git | 2.54.0 (`/opt/homebrew/bin/git`) |
| gcloud | Google Cloud SDK 569.0.0 (beta 2026.05.15, bq 2.1.31, core 2026.05.15) |
| python3 | 3.14.5 (`/opt/homebrew/bin/python3`) |
| python3.12 | **no disponible** |
| docker | **no disponible** |
| terraform / tofu | **no disponibles** |
| firebase | **no disponible** |
| gh | **no disponible** |
| syft | **no disponible** |

Consecuencia registrada: los pasos que exigen Python 3.12, Docker, Terraform o Syft se
ejecutarán en Cloud Build/Cloud Shell con imágenes-herramienta fijadas por versión+digest
(las referencias se registran en `ci/tool-images.env` y en los YAML de build). No se instala
software en el host sin aprobación. `gh` no es obligatorio (PR por web).

## Baseline de pytest (bootstrap local NO autoritativo — Python 3.14.5)

```
463 passed, 2 skipped, 4 warnings in 8.45s
SKIPPED [1] tests/test_blocking_intent.py:393: Article has no must_have items
SKIPPED [1] tests/test_forusbots_client.py:376: set FORUSBOTS_LIVE=1 to run
```

Clasificación de skips (identificados por `pytest -rs`):

- `test_blocking_intent.py:393` — artículo sin `must_have`; **no crítico** sólo si el owner lo
  acepta por escrito (pendiente, Tarea 1/aprobaciones).
- `test_forusbots_client.py:376` — dependencia live de ForusBots; gateada tras G4 por diseño.

El gate autoritativo de suite es Cloud Build con Python 3.12 + locks (Tarea 3 en adelante).

## Sesión gcloud

`gcloud auth list` → cuenta activa `ivan.alvis@forusall.com`; `gcloud auth print-access-token`
funcionó el 2026-07-13 (la sesión fue renovada tras el incidente `invalid_rapt` del 2026-07-11).
Esta sesión autoriza **sólo inspección read-only**; ninguna mutación queda autorizada por ella.

### UPDATE 2026-07-14 — la sesión gcloud EXPIRÓ a mitad de ejecución

Durante la Tarea 3, `gcloud auth print-access-token` empezó a fallar con
`Reauthentication failed. cannot prompt during non-interactive execution`
(la política org de reauth). Conforme al STOP del plan (Tarea 0 Paso 4), NO se
usó otra cuenta ni una key. Consecuencia: todos los pasos GCP read/write
quedan bloqueados hasta que el usuario ejecute `gcloud auth login
--update-adc` en una terminal interactiva. La inspección read-only de las
Tareas 0–1 (tráfico, IAM, env, imagen) se completó ANTES de la expiración y
sigue siendo válida.

### UPDATE 2026-07-21 — reautenticación restaurada y verificación remota pre-delta cerrada

El usuario renovó la sesión interactiva. Con su autorización explícita se
consultó el build histórico, se resolvieron/descargaron los locks y se ejecutó
el build de verificación del source pre-delta
`5fe68b12-1381-4bb3-9b4f-594ca401fda0`, que terminó **SUCCESS**. Los cambios
posteriores de controller, runtime, secretos e IaC no quedan cubiertos por ese
build. La autorización cubrió verificación y coste de Cloud Build, no los
gates de rollout: no hubo `apply`, deploy, publicación de imágenes ni cambios
de IAM, secretos, tráfico o n8n.

Toolchain global confirmado ausente en el host (sin instalar):
`terraform`/`tofu`, `docker`, `python3.12`, `syft`, `gh`. Para verificar sin
mutar el host se descargó Terraform 1.9.8 a `/tmp`, se comprobó su artefacto y
se ejecutaron localmente `fmt`, `init -backend=false -lockfile=readonly`,
`validate` y `test` sobre roots/módulo. Los locks 3.12, builds/smokes de imagen
y todo `plan/apply` real siguen requiriendo builders fijados por digest; el
árbol actual no se reenvió a Cloud Build porque aún no existe una identidad
verifier segura pre-G1B. `gh` no es obligatorio (PR por API/web).
