# ForUsGuide — Overview del proyecto (brief para infografía)

> **Para:** Claude Design (u otro asistente de generación visual)
> **De:** Ivan Alvis — ForUsAll Engineering
> **Propósito:** Servir como **única fuente de contexto** para producir una infografía del proyecto ForUsGuide.
> **Idioma de los entregables:** Español. Nombres de producto y términos técnicos se dejan en inglés (Cloud Run, Service Account, lease, etc.).
> **Fecha del snapshot:** 2026-08-05 · commit `806932d` · rama `main`.

**Reglas duras para quien diseñe a partir de este documento:**

1. **No inventes servicios, números ni nombres.** Todo lo que se puede afirmar está aquí. Si un dato no está, omítelo — no lo alucines.
2. Los números marcados ⚠️ son **inconsistentes entre fuentes**: no los presentes como hecho verificado (ver §14).
3. Los README antiguos del repo (`kb-rag-system/README.md`, `ARCHITECTURE.md`, `SKILL.md`) están **desactualizados** en hosting y modelos de LLM. Este documento gana (ver §14).

---

## 1. Identidad del proyecto

**ForUsGuide** es la plataforma de **automatización con IA del soporte a participantes de planes de retiro** 401(k) / 403(b) / 457 de ForUsAll.

No es un chatbot. Es un **RAG operacional**: orientado a decisiones y resultados, insertado como una pieza dentro de una cadena multi-agente que resuelve tickets reales de punta a punta.

La pregunta que responde el sistema no es *"¿qué dice el artículo?"* sino:

> **"¿Qué datos necesito del participante, y con esos datos qué puede o no puede hacer según las reglas reales de su plan?"**

| Atributo | Valor |
|---|---|
| Repositorio | `ialvist/ForUsGuide` — rama única `main` |
| Proyecto GCP | `rag-kb-system` (project number `900340137010`) |
| Región | `us-central1` (Iowa, EE. UU.) — despliegue single-region |
| Organización | `forusall.com` |
| Vida del proyecto | 3 dic 2025 → 5 ago 2026 (~8 meses) |
| Commits | 154 |

---

## 2. El flujo end-to-end: el viaje de un ticket (9 etapas)

```
Participante → DevRev (CRM) → n8n (orquestador) → KB RAG API → ForUsBots (RPA)
            → KB RAG API → kb_bundle_v1 → DevRev AI Agent → n8n → Ticket cerrado
```

| # | Etapa | Actor | Qué pasa | Color sugerido |
|---|---|---|---|---|
| 1 | **Ticket DevRev** | DevRev | El participante escribe su consulta; se crea el ticket y dispara un webhook a n8n | Naranja DevRev |
| 2 | **Descomposición** | n8n (nodo AI) | Divide un ticket multi-tema en *inquiries* individuales y etiqueta cada una con su `topic` | Rosa n8n |
| 3 | **`/required-data`** | KB RAG API | Por cada inquiry, devuelve qué campos del participante/plan hacen falta para responder | Azul GCP |
| 4 | **Scraping** | ForUsBots (RPA) | Recibe la lista de campos deduplicada, entra al portal del recordkeeper y devuelve los valores reales | Cian Render |
| 5 | **Búsqueda vectorial** | Pinecone | Búsqueda semántica **filtrada** por `record_keeper` + `plan_type` + `topic` | Indigo |
| 6 | **Routing LLM** | OpenAI / Gemini | El router híbrido decide el *outcome* y genera la respuesta estructurada | Verde LLM |
| 7 | **`kb_bundle_v1`** | n8n | Consolida las respuestas por inquiry en un único bundle y lo entrega al agente de DevRev | Púrpura |
| 8 | **Redacción final** | DevRev AI Agent | Escribe la respuesta al participante, decide la etapa del ticket y las notas internas; hace **callback** a n8n | Rojo DevRev |
| 9 | **Inyección y cierre** | n8n | Escribe `participant_reply` + `internal_notes` en DevRev y marca `solved` / `escalated` | Verde |

**El insight visual más importante de esta sección:** un LLM nunca puede saber el saldo, el estado de empleo ni la elegibilidad real de una persona. Todo el bucle `required-data → scraping → generate-response` existe precisamente para **anclar la respuesta en el estado real de la cuenta**. Sin ForUsBots, el sistema sólo podría dar información genérica.

**Nota de diseño sobre la etapa 8:** el AI Agent de DevRev **no escribe directo al ticket**. Hace un callback a n8n, y n8n escribe. Esa indirección es intencional: permite que n8n aplique guardrails (nunca exponer datos internos del sistema, elegir la etapa correcta) *antes* de que la respuesta llegue al participante.

---

## 3. Tres planos arquitectónicos (separación deliberada)

El sistema tiene tres planos separados por frontera de seguridad: **distinto servicio, distinta service account, distinta base de datos, distinta autenticación.**

### Plano A — Data plane: RAG API *(productivo)*

Servicio Cloud Run privado `kb-rag-system`. Su invocador es una **service account de n8n**, nunca usuarios humanos.

Endpoints REST:

| Endpoint | Propósito |
|---|---|
| `POST /api/v1/required-data` | Qué datos del participante hacen falta (rate limit 60 req/min) |
| `POST /api/v1/generate-response` | Respuesta contextualizada orientada a resultado (30 req/min) |
| `POST /api/v1/knowledge-question` | Q&A libre sobre la KB, sin contexto de participante (UI interna) |
| `POST /api/v1/route-inquiry` | Clasificador / enrutador de inquiries |
| `GET /api/v1/chunks` · `GET /api/v1/index-stats` | Introspección del índice vectorial |
| `GET /health` · `/livez` · `/readyz` | Probes; `/health` valida conexión a Pinecone y config de LLM |
| `GET /ui`, `/ui/chunks`, `/ui/knowledge`, `/ui/router` | UI interna integrada |

### Plano B — Execution plane: `handle-ticket` durable *(productivo)*

Una **misma imagen inmutable** con tres **roles de proceso mutuamente excluyentes** (variable `APP_ROLE`):

| Rol | Servicio | Responsabilidad |
|---|---|---|
| **producer** | `kb-rag-system` | Autentica principal/tenant, autoriza fail-closed, reserva **idempotencia + cuota en una sola transacción Firestore ANTES de cualquier llamada a LLM**, encola una Cloud Task y responde `202`. Nunca sirve rutas `/internal/*` |
| **worker** | `kb-rag-ticket-worker` (ingress internal) | Claim con `lease_epoch`/owner/expiry — **lease 90 s, heartbeat 30 s**. Checkpoints por inquiry condicionados al epoch; reanuda desde el plan persistido **sin repetir efectos** |
| **reconciler** | Cloud Run Job + Cloud Scheduler **cada 6 min** | Repara outbox pendiente y leases vencidos, terminaliza deadlines. Timeout 300 s, cero retries: el siguiente tick es la única recuperación. **SLA de recuperación ≤ 10 min** |

Endpoints de polling: `GET /api/v1/tickets/{job_id}` y `GET /api/v2/ticket-jobs/{job_id}`.
Semántica de estados: `404` = no existe · `410` = el control vive pero el payload expiró (no reintentar con la misma key) · `403` = pertenece a otro principal.

**Regla de oro del contrato con n8n (fail-safe de publicación):**

> Sólo `state=succeeded` + `next_action=send_participant_reply` + `metadata.fallback ≠ true` se publica al participante.
> Todo lo demás — `partial`, `failed`, `timeout`, error técnico — va a la ruta legacy / humana.

Colecciones Firestore del plano B: `ticket_jobs` (control/tombstone **sin PII**, retención ≥ 90 d) · `ticket_job_payloads` (request/plan/checkpoints **con PII**, TTL nativo 24 h como fail-safe de privacidad) · `ticket_idempotency_receipts` · `ticket_active_counters` · `ticket_rate_windows`.

### Plano C — Admin plane: consola `/tickets` *(en construcción, 8 de 11 etapas)*

Servicio Cloud Run **separado** (`rag-tickets-console`) + **base Firestore nombrada dedicada** (`tickets-console-staging` / `tickets-console-prod`) + un **evidence broker** read-only aparte.

Reemplaza la hoja de cálculo de revisión de tickets con un **ciclo cerrado de mejora continua**:

```
revisar ticket  →  calificar (1–5) y clasificar la causa raíz
                →  agrupar reviews en un batch de remediación
                →  agente IA propone cambios (KB / prompt / código / workflow)
                →  verificación humana INDEPENDIENTE
                →  resuelto
```

- ~26 rutas bajo `/api/admin/v1`
- 5 roles: `viewer | reviewer | remediator | admin | agent`
- Auth: **IAP directo de Cloud Run + verificación criptográfica del JWT firmado** (`X-Goog-IAP-JWT-Assertion`) contra la audiencia esperada. Headers de identidad sin firma **nunca** se confían
- El navegador nunca recibe un token de DevRev, ni config de Firebase, ni acceso directo a Firestore
- UI vanilla HTML/CSS + ES modules, **sin build step**

Campos que preserva de la hoja original: `Ticket ID`, `Topic`, `Type`, `Rating` (1–5), `Reviewer`, `Comments`.
Campos que añade: estado de revisión, severidad, comportamiento esperado, taxonomía de causa raíz, target de remediación, respuesta real de DevRev, evidencia RAG disponible **y los huecos explícitos de evidencia**, batch/plan/branch/commit/PR/tests/verificación, y eventos de auditoría encadenados por hash.

Taxonomías cerradas (útiles como chips visuales):
- `observation_type`: `correct · knowledge_gap · knowledge_conflict · retrieval_miss · chunking_or_metadata · prompt_instruction · orchestration_logic · source_data · privacy_or_compliance · other`
- `severity`: `low · medium · high · critical`
- `remediation_target`: `kb · prompt · code · workflow · source_data · none · unknown`
- `correlation_status`: `linked · manual · unavailable`

---

## 4. Stack tecnológico por capa

| Capa | Tecnologías |
|---|---|
| **Cómputo** | Python 3.12 · FastAPI · Pydantic v2 · Uvicorn · Docker (`python:3.12-slim`, usuario no-root) · Cloud Run |
| **Datos** | Pinecone Serverless · Firestore Native · Cloud Storage (versionado) · BigQuery (`kb_analytics`) |
| **IA** | OpenAI GPT-5.5 (con reasoning) · Google Gemini 2.5 · Vertex AI (vía ADC, sin API key) · embeddings integrados `llama-text-embed-v2` |
| **Orquestación** | n8n (AWS EC2) · ForUsBots RPA (Render) · Cloud Tasks · Cloud Scheduler |
| **Frontend** | HTML / CSS / JavaScript vanilla + ES modules — **sin build step, sin frameworks** |
| **IaC / CI-CD** | Terraform 1.9.8 (3 roots + módulo reutilizable) · Cloud Build (11 pipelines) · Artifact Registry |
| **Seguridad** | Secret Manager (versiones numéricas) · IAM least-privilege · IAP · detect-secrets · SBOM + escaneo de imagen |
| **Testing** | pytest + pytest-asyncio + httpx · emulador Firestore · contract tests · smoke en contenedor |

---

## 5. Conocimiento y recuperación

### 5.1 Artículos de la Knowledge Base (esquema `kb_article_v2`)

**18 artículos JSON** versionados en `PA/`:

| Carpeta | Artículos |
|---|---|
| `PA/Distributions/` | 12 (rollovers, hardship, RMDs, force-out, EACA/ADP-ACP refunds, 60-day rule…) |
| `PA/Loans/` | 3 |
| `PA/Participant Dashboard/` | 3 (setup de cuenta, statements/beneficiarios, MFA) |

Un artículo **no es prosa**. Es un objeto estructurado con:

- `critical_flags` — `portal_required`, `mfa_relevant`, `record_keeper_must_be`
- `business_rules` — reglas agrupadas por categoría
- `required_data` — `must_have` / `nice_to_have` / `if_missing` / `disambiguation_notes`
- **4 marcos de respuesta orientados a resultado** (`response_frames`):

| Marco | Significado |
|---|---|
| `can_proceed` | El participante es elegible → entregar pasos |
| `blocked_not_eligible` | No califica |
| `blocked_missing_data` | Faltan datos del portal o del participante |
| `ambiguous_plan_rules` | Las reglas del plan no son claras → escalar |

La audiencia de los artículos es `"Internal AI Support Agent"` — **nunca se muestran al participante**.

### 5.2 Chunking multi-tier: el corazón del sistema

| Tier | Contenido | Regla de retención |
|---|---|---|
| **CRITICAL** | `required_data`, `decision_guide`, `response_frames`, guardrails, reglas críticas | **Nunca se descarta** por presupuesto de tokens |
| **HIGH** | pasos, detalles de fees, problemas comunes, ejemplos | Se incluye si hay presupuesto |
| **MEDIUM** | FAQs de alto impacto, ejemplos, datos `nice_to_have` | Rellena el espacio restante |
| **LOW** | FAQs normales, definiciones, notas adicionales, referencias | Último recurso |

> **La calidad de recuperación del sistema la determina este tiering, no el LLM.** Es el dato conceptual más valioso de toda la arquitectura RAG.

### 5.3 Configuración vectorial

| Atributo | Valor |
|---|---|
| Índice Pinecone | `kb-articles-production` |
| Namespace | `kb_articles` |
| Dimensión | 1024 |
| Métrica | cosine |
| Modelo de embeddings | `llama-text-embed-v2`, **integrado en Pinecone** (no hay llamada de embedding separada) |
| Estrategia de búsqueda | Filtrado por metadata **antes** de la búsqueda semántica; 7 lanes de búsqueda en paralelo |

### 5.4 Recordkeepers

**LT Trust es el recordkeeper propio de ForUsAll** → "procedimientos LT Trust" = "procedimientos ForUsAll". Los demás: Vanguard, Fidelity, Charles Schwab. Los artículos `global` (`record_keeper: null`) aplican a todos los recordkeepers.

---

## 6. Routing híbrido de LLM

Un **LLM Router** dirige cada tipo de tarea al modelo óptimo, con cadenas de fallback automáticas:

| Lane | Modelo | Por qué |
|---|---|---|
| `gr_outcome` (determinar elegibilidad) | **GPT-5.5 con reasoning** | Es *la* llamada crítica. Un `can_proceed` erróneo cuando el participante está bloqueado tiene impacto directo de negocio |
| `gr_response` (redactar el cuerpo) | GPT-5.5 / Gemini 2.5 Flash | El outcome ya está decidido; el modelo sigue un esquema prescriptivo |
| `classify` (clasificación/enrutamiento) | **Gemini 2.5 Flash** | Materialmente más barato y rápido para la misma calidad en esta lane |
| `decompose`, `required_data`, `knowledge`, `extract_inquiries`, `kb_question_synthesis`, `forusbots_field_map`, `ticket_field_extract` | Configurable por env var `LLM_ROUTE_*` | 11 lanes en total, validadas en el arranque contra las credenciales disponibles |

Dos llamadas al LLM por respuesta: **Phase 1 = decidir el outcome**, **Phase 2 = redactar según el marco correspondiente**. Temperatura muy baja (0.1) para consistencia.

**Tensión económica documentada — buen contraste visual:**

| | Costo |
|---|---|
| Con `gpt-4o-mini` (versión inicial) | ~**$0.0016 USD por ticket** (~600 tickets por dólar) |
| Con GPT-5.5 + reasoning en todas las lanes | ~**$0.70 USD por `generate-response`** → ~$2,100/mes a 100 req/día |

Ese salto de ~440× es exactamente lo que justifica el routing híbrido: mantener el modelo más fuerte sólo donde el error cuesta dinero real, y bajar a Flash en el resto.

---

## 7. Infraestructura GCP (datos verificados en vivo con `gcloud`)

### 7.1 Servicios GCP en uso

| Servicio | Rol | Detalle verificado |
|---|---|---|
| **Cloud Run** | Cómputo serverless | Servicio `kb-rag-system`: **CPU 1 · memoria 512 Mi · concurrency 80 · maxScale 5 · escala a 0 · startup-cpu-boost ON · timeout 300 s · puerto 8000**, 100% del tráfico a la última revisión |
| **Artifact Registry** | Registro de imágenes | Repo `kb-rag` (DOCKER, STANDARD), `us-central1`. Ruta: `us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system:<SHORT_SHA>` |
| **Cloud Build** | CI/CD | Trigger `deploy-kb-rag-system` sobre GitHub `ialvisti/ForUsGuide`, rama `^main$` |
| **Secret Manager** | Secretos | `api-key`, `openai-api-key`, `pinecone-api-key` — inyectados como env vars vía `secretKeyRef`, **nunca en la imagen** |
| **Firestore** (Native) | Auditoría / estado durable | DB `(default)`, `us-central1`. Colección `execution_logs` (un doc por request, escritura **async no bloqueante**) + las colecciones del plano B |
| **Cloud Storage** | Almacenamiento de objetos | Bucket `rag-kb-system-kb-articles` (JSON fuente en `articles/{article_id}.json`) + bucket de staging de Cloud Build |
| **BigQuery** | Analítica | Dataset `kb_analytics`: calidad de recuperación, latencia, uso de tokens, decisiones de routing |
| **Vertex AI** | IA gestionada | Acceso GCP-native a Gemini vía **Application Default Credentials** — evita guardar una API key de Google en Secret Manager |
| **Cloud Logging** | Logs | `stdout` de Cloud Run, JSON estructurado con correlation IDs |
| **Cloud Monitoring** | Alertas | 3 alertas: 5xx > 5/intervalo · latencia > 30 s · uptime check en `/health` |
| **Cloud Trace** | Tracing distribuido | API habilitada |
| **Cloud Tasks + Cloud Scheduler** | Ejecución durable | Queue `ticket-jobs-prod` · scheduler del reconciler cada 6 min |
| **IAM** | Identidad | El corazón de la seguridad — ver §7.2 |

### 7.2 Service accounts — seguridad por identidad, sin llaves de larga vida

| Service Account | Función | Roles IAM |
|---|---|---|
| `kb-rag-runner@…` | **Identidad de runtime** del contenedor | `aiplatform.user`, `datastore.user`, `secretmanager.secretAccessor`, `storage.objectViewer`, `logging.logWriter`, `monitoring.metricWriter` |
| `kb-rag-client@…` | **Identidad del llamador** — n8n invoca con token OIDC de Google | `run.invoker` (sobre el servicio), `aiplatform.user` |
| `900340137010-compute@…` | La usa Cloud Build para **desplegar** | `run.admin`, `artifactregistry.writer`, `storage.admin`, `logging.logWriter` |
| `firebase-adminsdk-fbsvc@…` | SDK Admin de Firebase | `firebase.sdkAdminServiceAgent`, `iam.serviceAccountTokenCreator` |

**Control de acceso al servicio — punto clave:**

- El ingress es `all`, **pero el IAM policy concede `roles/run.invoker` únicamente a `kb-rag-client@…`**. Sólo n8n, con un token OIDC válido, puede invocarlo. Cualquier otro request se rechaza **en la frontera de GCP, antes de llegar al contenedor**.
- **Doble capa de auth:** además del IAM de plataforma, la app valida el header `X-API-Key` en los endpoints de negocio.
- **Mínimo privilegio real:** el runner puede leer secretos y artículos, pero **no** puede administrar Cloud Run; eso es exclusivo del rol de deploy.

### 7.3 Pipeline de entrega continua (6 pasos, < 4 min)

| # | Paso | Qué pasa |
|---|---|---|
| 1 | **GitHub** | `git push` a `main` |
| 2 | **Cloud Build trigger** | Detecta el push, lee `cloudbuild.yaml`, encola el build |
| 3 | **Docker build** | Base `python:3.12-slim`, instala desde locks, copia el árbol de fuentes |
| 4 | **Push de imagen** | A Artifact Registry con dos tags: `$SHORT_SHA` (para rollback) y `latest` |
| 5 | **Deploy a Cloud Run** | `gcloud run deploy` con rolling update, secretos inyectados desde Secret Manager |
| 6 | **Health check** | La nueva revisión **sólo se promueve si `GET /health` pasa** — ese endpoint valida Pinecone y la config de LLM |

Los gates del build canónico cubren: **tests · lint · tipos · auditoría de dependencias · SBOM · escaneo de imagen**.

Terraform tiene **3 live roots** (`platform`, `staging`, `production`) + un módulo reutilizable `ticket_environment`, con sus propios tests `.tftest.hcl`. 21 índices Firestore declarados.

---

## 8. Los 8 invariantes de seguridad y gobernanza

Es la característica más distintiva de la ingeniería del proyecto: **el diseño está dominado por invariantes explícitos, no por features.**

| # | Invariante | Qué significa en concreto |
|---|---|---|
| 1 | **Fail-closed por defecto** | Identidad sin binding de rol → **denegada**, nunca degradada a solo-lectura. Producción rechaza en el arranque: dominios wildcard, auth local, versiones de secreto no numéricas, URLs no-HTTPS, la base Firestore `(default)` |
| 2 | **Sin procedencia fabricada** | Un ticket sólo puede ser `linked`, `manual` o `unavailable`. La similitud por timestamp o texto se muestra como *sugerencia*, **jamás** se persiste como vínculo confirmado sin acción explícita de un revisor |
| 3 | **Correlación que preserva privacidad** | El productor almacena sólo `HMAC-SHA256(lookup_key, DON)` con versionado de llaves y rotación auditada — **nunca el ID externo crudo** |
| 4 | **Auditoría encadenada por hash** | Cada mutación emite un evento append-only con `previous_event_hash` / `event_hash`. **No existe endpoint de update ni delete** de registros de auditoría |
| 5 | **Todo input externo es no confiable** | Títulos, cuerpos, entradas de timeline, comentarios de revisores y celdas de CSV importado: **nunca se siguen instrucciones contenidas ahí** (defensa explícita contra prompt injection) |
| 6 | **IA con humano en el bucle** | El agente de remediación trabaja con *lease*: **15 min, heartbeat cada 5 min, tope continuo de 2 h**. Sólo un humano **independiente** mueve `changes_proposed → verifying → completed`. Ningún agente puede desplegar, mergear, reindexar producción ni escribir en DevRev sin aprobación separada |
| 7 | **Idempotencia durable** | Misma `Idempotency-Key` → replay del job existente. Misma key con payload distinto → `409 IDEMPOTENCY_PAYLOAD_MISMATCH` (es un bug del productor; no reintentar) |
| 8 | **Minimización de PII** | Los cuerpos de mensaje viven sólo en caché acotado (TTL 24 h) y **nunca** entran en exports CSV, prompts, logs ni Git. Retención: **730 días** de producto, **2,555 días** de ledger de auditoría, con `legal_hold` que suprime el purgado |

Límites canónicos adicionales (single source of truth replicada en modelos, API, UI, tests y Terraform): páginas de 50 por defecto / 100 máximo · batch de remediación máximo 100 reviews · 200 referencias de evidencia por review · request JSON 1 MiB / CSV 10 MiB · 10,000 filas de CSV · breakpoint responsive 768 px · touch target 44 px.

---

## 9. Datos duros — KPIs para tarjetas de la infografía

| Métrica | Valor |
|---|---|
| Commits | **154** |
| Ventana de desarrollo | dic 2025 → ago 2026 (~8 meses) |
| Autores | 2 (`ialvist` 153 · `camilo-bello` 1) |
| Archivos versionados | **443** |
| Líneas versionadas | **~209,000** |
| **Tests recolectados** | **3,977** |
| LOC de tests | **61,362** |
| LOC `data_pipeline/` | 28,834 |
| LOC `ui/` | 15,683 |
| LOC `api/` | 15,028 |
| LOC `scripts/` | 11,676 |
| LOC Terraform | 7,083 |
| **Ratio test : producción** | **≈ 1.4 : 1** |
| Artículos KB | 18 (12 Distributions + 3 Loans + 3 Dashboard) |
| Índices Firestore declarados | 21 |
| Pipelines Cloud Build | 11 |
| Endpoints REST | ~14 (RAG) + ~26 (admin) |
| Latencia por request | 2–5 s |
| Accuracy en tests | 88% |
| Pico de actividad | **julio 2026 — 63 commits** |
| Duración del pipeline CI/CD | < 4 min |
| Lease del agente de remediación | 15 min (heartbeat 5 min, tope 2 h) |
| SLA de recuperación del reconciler | ≤ 10 min |

**Distribución de commits por mes** (útil para un sparkline / barra de actividad):

| Mes | Commits |
|---|---|
| 2025-12 | 6 |
| 2026-01 | 6 |
| 2026-02 | 7 |
| 2026-03 | 11 |
| 2026-04 | 12 |
| 2026-05 | 18 |
| 2026-06 | 6 |
| 2026-07 | **63** |
| 2026-08 | 25 |

---

## 10. Estado actual y roadmap

### Producción

El flujo RAG + `handle-ticket` está **activo en modo `full`**: producer `Ready`, worker `Ready`, reconciler listo, queue `RUNNING`, scheduler `ENABLED`.

El incidente de ejecuciones GCP del **2026-08-02 está cerrado**:
- 8 errores históricos tuvieron efecto upstream exitoso → **no deben reintentarse**
- 2 supuestos `PINECONE_TRANSIENT_FAILURE` resultaron ser bloqueos locales `UnsafeRetrievalQuery` → **no hubo outage real de Pinecone**
- ForUsBots ya ofrece idempotencia durable para las operaciones que usa el RAG

### Consola `/tickets` — 8 de 11 etapas completadas

| Etapa | Entregable | Estado |
|---|---|---|
| 1 | Contratos, configuración aislada, modelos, guard de alcance | ✅ |
| 2 | Cliente DevRev read-only resiliente | ✅ |
| 3 | Repositorio Firestore de revisión + auditoría encadenada | ✅ |
| 4 | Hidratación DevRev + procedencia RAG + evidence broker | ✅ |
| 5 | App admin, IAP, RBAC, API | ✅ |
| 6 | UI de cola de revisión | ✅ |
| 7 | Detalle de ticket, conversación, evaluación, historial | ✅ |
| 8 | Batches de remediación IA + CLI | ✅ |
| 9 | Migración / exportación CSV segura | ⏳ |
| 10 | Terraform, IAP, DB dedicada, IAM, secretos, retención, observabilidad | ⏳ |
| 11 | Verificación end-to-end y rollout por staging | ⏳ |
| 99 | Auditoría independiente y reparación final | ⏳ |

### Pendientes operativos

| Prio | Pendiente |
|---|---|
| **P0** | Validación de una ejecución real iniciada desde la instancia n8n de producción |
| **P1** | Adoptar el bootstrap de infraestructura en el state de Terraform, con un plan de **cero `delete` y cero `replace`** |
| **P1** | Completar etapas 9–11 + 99 de la consola `/tickets` |
| **P2** | Restaurar permiso mínimo de lectura de Artifact Registry para builds `test-only` |
| **P2** | Migrar el transporte hacia ForUsBots de HTTP legacy a **HTTPS o ingress privado** |
| **P2** | Reconciliar el inventario KB local ↔ Pinecone ↔ GCS (empezando por una auditoría de sólo lectura) |

---

## 11. Ángulos narrativos posibles para la infografía

Cualquiera funciona como hilo conductor. Los tres primeros son los más diferenciadores.

1. **"El viaje de un ticket"** — Las 9 etapas de §2 como línea de tiempo, con handoffs coloreados por proveedor. El más fácil de leer para audiencia no técnica.
2. **"Tres planos, tres fronteras"** — Los planos de §3 mostrando que la separación es *deliberada*: distinto servicio, distinta service account, distinta base de datos, distinta autenticación.
3. **"El bucle de mejora continua"** — Ticket → respuesta IA → revisión humana con calificación → batch de remediación → agente IA cambia KB/prompt/código → verificación humana independiente → mejor respuesta. **El sistema se audita y se corrige a sí mismo, con humanos en cada punto de decisión.**
4. **"Anclado en la realidad"** — Por qué existe ForUsBots: contraste entre lo que un LLM puede inventar y lo que sólo el portal del recordkeeper sabe.
5. **"Ingeniería fail-closed"** — Los 8 invariantes de §8 como cuadrícula de íconos, con el ratio 1.4:1 de test-a-producción como evidencia visual del rigor.

---

## 12. Guía visual y de estilo

**Paleta por proveedor** (consistente en todo el proyecto, ya usada en los diagramas existentes del repo):

| Color | Hex | Significado |
|---|---|---|
| Azul | `#4285F4` | Google Cloud Platform |
| Verde GCP | `#34A853` | Servicios de datos GCP |
| Amarillo | `#FBBC04` | Cloud Build / Storage |
| Rojo | `#EA4335` | Alertas / Monitoring |
| Naranja | `#FF9900` | AWS (host de n8n) |
| Teal | `#46E2C2` | Render (host de ForUsBots) |
| Púrpura | `#A855F7` | SaaS de terceros (DevRev, Pinecone) |
| Verde LLM | `#10A37F` | Proveedores de LLM (OpenAI, Gemini) |
| Indigo | `#4F46E5` | Base de datos vectorial |
| Verde brillante | `#22C55E` | Pipeline CI/CD |

**Reglas de composición:**

- Etiquetas en **español**; nombres de producto en inglés.
- Jerarquía clara: frontera GCP como contenedor grande, SaaS externo **fuera** de esa frontera.
- Flechas etiquetadas con protocolo/propósito: `HTTPS + token OIDC`, `secretKeyRef`, `query vectorial`, `LLM primario`, `LLM fallback`, `log async`.
- Consistencia: flechas sólidas = flujo de datos en runtime · punteadas = telemetría/observabilidad · color distinto = flujo de CI/CD.
- Densidad: ningún diagrama con más de ~12–15 nodos visibles. El detalle fino va a tablas o a paneles secundarios.
- **Nunca comunicar estado sólo por color** — siempre acompañar con texto o ícono (es un requisito de accesibilidad real del proyecto).
- Legibilidad: la infografía debe funcionar tanto en pantalla como impresa; sin overflow horizontal.
- Incluir leyenda que distinga: *servicio gestionado GCP* · *SaaS externo* · *identidad (SA)* · *flujo de datos* · *flujo de CI/CD*.

---

## 13. Glosario mínimo (para no confundir términos)

| Término | Significado en este proyecto |
|---|---|
| **Inquiry** | Una pregunta individual dentro de un ticket. Un ticket puede tener varias |
| **Recordkeeper** | La institución que administra el plan de retiro. LT Trust es el propio de ForUsAll |
| **DON** | *DevRev Object Name* — el identificador canónico de un objeto en DevRev |
| **Outcome** | El resultado de la evaluación de elegibilidad: uno de los 4 `response_frames` |
| **Lease** | Concesión temporal y renovable de trabajo exclusivo sobre un job o batch |
| **Evidence broker** | Servicio read-only aislado que traduce una referencia HMAC de ticket en un sobre de procedencia sanitizado |
| **Remediation batch** | Conjunto congelado de reviews que un agente IA toma para proponer cambios |
| **`kb_bundle_v1`** | El artefacto consolidado que n8n entrega al AI Agent de DevRev |
| **Fail-closed** | Ante duda, ambigüedad o error de configuración: **denegar**, no permitir |

---

## 14. ⚠️ Discrepancias conocidas — NO usar como hecho

Algunos archivos del repo están desactualizados. **Este documento gana.**

| Tema | Lo que dicen docs viejos | Realidad verificada — usa ESTA |
|---|---|---|
| **Hosting** | Render (`forusguide.onrender.com`) — en `SKILL.md` y `kb-rag-system/README.md` | **GCP Cloud Run** en el proyecto `rag-kb-system` |
| **LLM** | OpenAI GPT-4o-mini | **OpenAI GPT-5.5 con reasoning (primario) + Gemini 2.5 vía Vertex AI** |
| **Reranking** | "rerank con bge-reranker-v2-m3" | **No aplica en producción**; los embeddings integrados `llama-text-embed-v2` de Pinecone hacen el trabajo |
| **Endpoints** | 2 endpoints | **~14 en el data plane** (ver §3, plano A) |
| **Etapas de `/tickets`** | `PENDIENTES.md` dice "etapas 1–3 implementadas" | **8 de 11 completadas** (commit `806932d`) |
| **Vectores indexados** | ARCHITECTURE dice ~8,400 chunks / ~280 artículos (objetivo) | El inventario real observado fue **~547 vectores**. La reconciliación KB ↔ Pinecone ↔ GCS es deuda pendiente. **No presentes ninguno de los dos como cifra verificada** |
| **Inventario de Cloud Run** | El brief de `design/` lista un solo servicio `kb-rag-system` | Ese inventario **precede al split producer/worker/reconciler**. Hoy hay además `kb-rag-ticket-worker` y el Job del reconciler |

---

## 15. Fuentes dentro del repo

| Archivo | Qué aporta |
|---|---|
| `SKILL.md` | Esquema de artículos KB, chunking multi-tier, convenciones de código |
| `PENDIENTES.md` | Estado y backlog operativo (parcialmente desfasado — ver §14) |
| `kb-rag-system/ARCHITECTURE.md` / `_EN.md` | Explicación pedagógica del RAG (32 KB cada uno) |
| `kb-rag-system/Development Docs/INFRASTRUCTURE_DIAGRAM_EXPLAINED.md` | Walkthrough exhaustivo del diagrama de infraestructura, las 9 etapas del flujo y el payload del webhook de la etapa 8 |
| `kb-rag-system/Development Docs/HYBRID_LLM_ARCHITECTURE.md` | Router híbrido, tabla de lanes, análisis de costos |
| `kb-rag-system/Development Docs/HANDLE_TICKET_RUNBOOK.md` | Topología producer/worker/reconciler, estados, procedimientos |
| `kb-rag-system/Development Docs/TICKETS_REVIEW_ARCHITECTURE.md` | ADR de la consola `/tickets` |
| `tickets-development-plan/README.md` | Master plan de las 11 etapas, contratos de API/Firestore, límites canónicos |
| `design/instrucciones-claude-design.md` | Brief anterior con inventario GCP e IAM verificado en vivo |
| `design/walkthrough.md` | Walkthrough técnico del RAG (652 líneas) |
| `AUDITORIA_EJECUCIONES_GCP_2026-08-02.md` / `REMEDIACION_…` | Auditoría y remediación del incidente cerrado |
