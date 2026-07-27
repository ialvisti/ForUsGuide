# 15 — Revisión adversarial del diff (Tarea 15 Paso 5)

Primera pasada ejecutada 2026-07-14/15 con un workflow de 5 revisores por dimensión
(boundaries/idempotencia/fencing/tasks-reconciler/publicación) + verificación
adversarial por hallazgo. 17 hallazgos planteados; 8 verificados como
CONFIRMED antes de que 9 verificadores agotaran el límite de sesión. Los
hallazgos sin verificar se trataron por análisis propio (no se descartan por
falta de verdict).

## Confirmados y CORREGIDOS (RED primero)

| Sev | Hallazgo | Fix | Test |
|---|---|---|---|
| **P1** | ForusBots job IDs se pierden en checkpoints timeout/failed/unprocessed (`_collect_forusbots_ids_from_entries` sólo leía `entry['result']`) | `_entry_forusbots_ids` lee el bloque explícito `forusbots_job_ids` + `result.diagnostics`; checkpoints GR degradados marcan `manual_reconciliation_required` (no se afirma "sin efectos") | `test_forusbots_ids_preserved_when_inquiry_times_out` |
| **P1, superseded 2026-07-27** | Se trató el header WIF de aplicación como obligatorio aunque el workflow real usa Cloud Run IAM + `X-API-Key` | Terraform retiró WIF/AWS y v1/v2 conservan el contrato de `kb-rag-client`; la comprobación adicional queda sólo como compatibilidad opcional no desplegada | `test_v2_accepts_existing_n8n_auth_contract_without_custom_wif_header` |
| **P1** | El reconciliador terminaliza por deadline/payload sin fencear al worker vivo (sin bump de `lease_epoch`) → efectos externos sobre un job ya terminal | `_terminalize` llama a `fence_and_requeue` (epoch+1) antes de terminalizar | `test_reconciler_terminalizes_expired_deadline` (verifica estado terminal) + fencing en `fence_and_requeue` |
| **P2** | `done_indexes` trata `unprocessed` (retryable) como terminal → un retry con presupuesto fresco nunca la reprocesa | `unprocessed` excluido de `done_indexes` | `test_unprocessed_inquiry_reprocessed_on_resume` |
| **P2** | `record_keeper` del caller sobrevive cuando la fuente canónica devuelve None | el servidor SIEMPRE fija `record_keeper` desde la fuente (None incluido) | `test_caller_record_keeper_dropped_when_canonical_is_none` |
| **P2** | Shadow muestreado hace scrapes reales de ForusBots pero no traza sus job_ids | los ids del outcome real del shadow se propagan al checkpoint y a la agregación terminal | cubierto por la agregación (`_collect_forusbots_ids_from_entries`) |

## Confirmados como riesgo ACOTADO (documentados, no bug nuevo)

| Sev | Hallazgo | Razonamiento |
|---|---|---|
| P2 | Chequeo de lease advisory + efecto-antes-de-checkpoint: un lease perdido tras el efecto externo descarta el checkpoint → posible reproceso | At-least-once INTERNO acotado que el plan reconoce (Tarea 6 Paso 5): LLM repetible con hash/input; ForusBots/delivery quedan `manual_reconciliation_required` y no se reenvían a ciegas. El fencing por epoch impide publicar/guardar dos veces. |
| P2 | Extracción/clasificación se re-ejecutan ante crash ANTES de persistir el execution_plan | Mismo at-least-once acotado: el plan se persiste una sola vez; un crash previo repite sólo trabajo LLM idempotente, nunca efectos participant-facing ni doble publicación. |

## Segunda pasada independiente (2026-07-15)

La revisión de cierre volvió a ejecutar contratos reales y añadió carreras
deterministas. Corrigió RED-first estos falsos verdes de la primera pasada:

| Sev | Hallazgo confirmado | Corrección verificable |
|---|---|---|
| **P0** | El admission control construía `GetQueueRequest(read_mask=...)` con Cloud Tasks GA v2, cuyo request no admite `read_mask`; toda creación nueva terminaba 503 | cliente v2beta3 fijado sólo para `stats.tasks_count`/rate limits; v2 GA conserva create/get task |
| **P0** | El worker validaba configuración RAG/LLM/ForusBots pero Terraform no le entregaba `producer_core_env` | el worker recibe el mismo mapa core y secret refs que valida al arrancar |
| **P1** | `plan_type` generado por el LLM sobrescribía el valor canónico autorizado | sólo se usa el `plan_type` de la fuente/entrada canónica |
| **P1, superseded 2026-07-27** | GET v1 se evaluó contra un segundo factor inexistente en n8n | polling conserva Cloud Run IAM + `X-API-Key`; OpenAPI declara ese contrato real |
| **P1** | `enqueue_generation` se comprobaba antes, no dentro, del claim | CAS de generación dentro de la misma transacción que adquiere el lease; task stale responde 204 sin efecto |
| **P1** | Un worker que pierde lease durante un POST ForusBots podía provocar un segundo submit | intent durable por inquiry antes del POST; un worker posterior no reenvía y marca reconciliación manual |
| **P1** | `body.error` de ForusBots alcanzaba logs, checkpoints y polling | errores cerrados por código; se descarta texto upstream y se conserva sólo señal de reconciliación |
| **P1** | SSN espaciado, cuentas agrupadas y fechas textuales alcanzaban `query_chunks` | saneamiento adversarial antes de la frontera Pinecone, probado sobre el argumento real |
| **P2** | Replay idempotente podía cruzar un cambio de tenant bajo el mismo principal | tenant hash validado en receipt/control antes de replay/re-enqueue, incluida la carrera transaccional |
| **P2** | `ticket_executions` duplicaba IDs sin TTL | telemetría agregada sin IDs externos/raw y `expires_at` nativo con TTL Terraform |
| **P2** | 429/timeout ambiguo de ForusBots podía reintentarse sin contrato de dedupe | submit ambiguo falla cerrado a reconciliación manual; 429 no se reenvía a ciegas |

También se corrigieron: verificación OIDC de Cloud Tasks con audiencia, SA y
`email_verified`; heartbeat que no resucita leases expirados; privacidad de
mensajes de excepción; replay v1/v2 con fingerprint estable; y el gate
detect-secrets que intentaba capturar stdout vacío en vez del baseline
actualizado.

## Hallazgos de la primera pasada reevaluados

- **v1-bypass-WIF**: superseded por la decisión del owner del 2026-07-27; WIF
  de aplicación no forma parte del contrato desplegado.
- **stale-generation TOCTOU** se reclasificó como confirmado y quedó cerrado
  al plegar `expected_generation` dentro del claim transaccional.
- **malformed task body → 422 retryable** quedó cerrado: una task autenticada
  pero malformada se ACKea 204; un request no autenticado sigue fallando
  cerrado.

## Tercera pasada de cierre (2026-07-21)

La auditoría confirmó tres falsos claims adicionales y los corrigió RED-first:

- las claves idempotentes del diferencial no incluían la ejecución y podían
  certificar jobs históricos durante al menos 90 días; ahora incluyen el
  `CLOUD_RUN_EXECUTION` validado;
- el token de workload con audience v2 se enviaba también al origen legacy;
  ahora cada target recibe sólo su propio ID token en ambos headers;
- el supuesto gate semántico y la métrica de replies duplicados sólo observaban
  substrings y replay de admisión. Se renombraron como smoke léxico y replay
  idempotente; `semantic_quality_verified=false` permanece fail-closed hasta un
  receipt `semantic_review` externo ligado a los hashes exactos.

## Cuarta pasada: supply chain y aislamiento del controlador

La revisión del flujo privilegiado de Terraform/Cloud Build encontró los
siguientes fallos. Los tres primeros y la contención Firestore preceden al
build remoto; el cierre verify-only del publisher/controller es un delta
posterior y por eso no queda probado por ese build:

| Sev | Hallazgo confirmado | Corrección verificable |
|---|---|---|
| **P1** | El árbol candidato podía ejecutar expresiones Terraform y producir output antes del rechazo post-plan | policy estática fail-closed antes de `init/plan`: tipos, `data`, imports, rutas, funciones, provider overrides y ficheros implícitos limitados a la forma revisada |
| **P1** | `test-only` ejecutaba tests candidatos con red y ADC heredados | tests en contenedor non-root, `--network=none`, filesystem read-only, sin capabilities/ADC/metadata/socket y con límites CPU/memoria/PIDs |
| **P1** | El candidato podía sustituir locks, baseline o el verificador de secretos | inputs confiables byte-identical y verificador incluido en la imagen del controlador y montado read-only |
| **P1** | El recipe que construía el trust boundary ejecutaba tests/Dockerfile candidatos con la SA capaz de publicar/scanear el controller | se eliminó el publish/scan del recipe candidato, se declaró una SA verifier logging-only, el publisher quedó fuera de YAML/triggers y ambas SAs fuera de `platform-apply actAs`; no se afirma cerrado el bootstrap porque aún no existe publisher confiable/source-less |
| **P1** | El admission control Firestore agotaba los cinco retries SDK bajo 50 llamadas simultáneas del mismo principal | single-flight local ref-counted por principal, manteniendo Firestore como autoridad distribuida |
| **P2** | Los digest de `google-cloud-cli` eran válidos pero ya no correspondían a la versión declarada | pins canónicos de `577.0.0` y `577.0.0-emulators`, comprobados desde `ci/tool-images.env` |

La carrera Firestore pasó **12/12** en el emulador oficial y cinco repeticiones
consecutivas de 50 callers. Los conteos integrales actuales se registran en la
sección siguiente, en lugar de conservar snapshots focales obsoletos.

## Quinta pasada: efectos tardíos, privacidad e identidad runtime

Una revisión independiente posterior al build remoto añadió carreras
deterministas y siguió los datos desde ForusBots hasta prompts/checkpoints/poll:

| Sev | Hallazgo confirmado | Corrección verificable |
|---|---|---|
| **P1** | Cancelar el último waiter antes del semáforo podía dejar vivo el task compartido y emitir un POST tardío | refcount por waiter + submit boundary: sin waiters se cancela antes del efecto; después del límite ambiguo se conserva shield/reconciliación |
| **P1** | Un error presend-safe podía volver a abrir el límite y reintentar el POST después de cancelar el último waiter | el boundary observa el task huérfano y aborta antes del backoff/retry; RED determinista observó dos POST y GREEN sólo el intento ya iniciado |
| **P1** | warnings/errors/module errors/unknown fields podían contener PII y llegar al LLM o resultado público, incluso anidados dentro de módulos reconocidos | vocabulario cerrado, conteos y saneamiento recursivo antes de prompt/checkpoint/poll |
| **P1** | El producer construía el cliente y recibía el token ForusBots aunque sólo el worker ejecuta el efecto | cliente/orchestrator, `FORUSBOTS_AUTH_TOKEN` y su grant per-secret quedan worker-only; el producer conserva sólo el `FORUSBOTS_BASE_URL` no secreto del inventario core importado |
| **P1 abierto live** | La revisión productiva usa `kb-rag-runner`, que conserva `secretAccessor` project-wide y un grant directo al token; quitar el env no revoca autoridad efectiva | se modela una SA candidata productiva mínima separada y se conserva la SA legacy sólo para rollback. No se afirma aplicada: G1B/G6A/G6B y un probe effective-IAM producer=DENIED/worker=GRANTED siguen gateados |
| **P1** | La allowlist semántica rechazaba sus propios índices reales `platform_apply_actas_scheduler` y no distinguía todos los build targets | inventario exacto `scheduler-*` + `build-*`; verifier, publisher, legacy y claves arbitrarias quedan excluidos |
| **P1** | `container_phase=managed` aceptaba un plan vacío o con grants runtime omitidos | completitud exacta por entorno: Firestore, Vertex, telemetry, queue y signer; `disabled` exige inventario vacío |
| **P1** | El signer admitía el service agent Cloud Tasks de cualquier número de proyecto | igualdad exacta con `service-900340137010@gcp-sa-cloudtasks.iam.gserviceaccount.com` |
| **P2** | El guard de SA productiva dedicada sólo aplicaba al modo activo, aunque una template dark también ejecuta core | toda template production creada, incluidas `dark_no_traffic`/`dark_100`, exige `ticket-producer-prod`; la revisión rollback existente conserva `kb-rag-runner` |
| **P1** | Requests que cruzaban el circuit breaker antes de esperar el semáforo podían quedar en backlog y emitir POST después de abrirse el circuito | el circuito se evalúa dentro del semáforo, inmediatamente antes del submit; el backlog queda rechazado sin efecto |
| **P1** | HTTP 408 de un POST no idempotente caía como error genérico aunque el job pudo crearse | `ForusBotsAmbiguousSubmit`, sin retry y con reconciliación manual |
| **P1** | Un puerto inválido en la URL se saneaba en el mensaje, pero el valor crudo sobrevivía en la causa/traceback | error saneado con causa suprimida y test con sentinel |
| **P1** | El recipe integral de verificación no fijaba SA y podía usar una identidad default/privilegiada | SA exacta `ticket-controller-verify` + contrato que rechaza default, Compute y `kb-rag-runner`; build remoto bloqueado mientras esa SA no exista |

Los RED/contract tests incluyen cancelación antes del semáforo y después de un
`ConnectError`, conservación del scrape compartido con otro waiter,
diagnósticos top-level/anidados, startup producer sin ForusBots e inyección/IAM
worker-only.

## Verificación remota pre-delta y gate local actual

El build remoto pre-delta `5fe68b12-1381-4bb3-9b4f-594ca401fda0` terminó
**SUCCESS** con sus nueve pasos verdes. El gate Python 3.12/Linux registró
**1296 passed, 16 skipped, 23 deselected**, además de ruff, mypy, pip check,
pip-audit y secretos. Terraform ejecutó fmt/validate/tests; el contrato
Firestore pasó **12/12**; y las cuatro imágenes se construyeron y pasaron sus
smokes para aquel source. La evidencia detallada queda en
`03-image-and-locks.md` y `STATUS.md`.

El árbol actual recolectó **1380** tests; su selección local pasó **1341**, con
**16 skipped y 23 deselected**. Ruff, mypy, dependencias, vulnerabilidades y
secret scans pasaron. Terraform 1.9.8 local pasó fmt/validate/tests: platform
22, staging 1, production 0 y módulo 25.

El cierre verify-only y todos los P1 de la quinta pasada fueron posteriores a
ese source. `5fe68b12…` corrió como `kb-rag-runner`, no como la SA verifier; por
tanto no prueba esos deltas. La evidencia remota sólo puede actualizarse sobre
el nuevo SHA con una identidad verifier segura. Ese build por sí solo no crea
una ruta de publicación segura ni permite solicitar G1B.

## Riesgos y bloqueos que permanecen abiertos

- La garantía ForusBots sigue deliberadamente incompleta: el intent durable
  evita duplicar a ciegas, pero sin lookup/idempotencia upstream no puede decidir
  si un POST ambiguo creó un job. El contrato externo continúa bloqueando `full`.
- El parser HCL propio es conservador. El aislamiento absoluto del refresh de
  Terraform exigiría firmar/pinear el árbol completo o generar una
  representación confiable; por eso ningún `apply` se habilita automáticamente.
- El bootstrap del controller permanece circular: las SAs verifier/publisher
  aún no existen en GCP, el apply que las crearía necesita el digest del
  controller y no hay publisher confiable/source-less. El campo
  `serviceAccount` del YAML no impone la SA efectiva. Se requiere un PREP
  separado o build en cuarentena + scan/copia byte a byte que nunca ejecute
  código candidato con la identidad publicadora.
- No quedan contratos externos que bloqueen el merge. G1A–G10 siguen sin
  aprobar porque autorizan mutaciones de rollout, no porque falten owners.
  No se ejecutó rollout, mutación IAM/secrets/tráfico/n8n ni merge a `main`.
- La SA live `kb-rag-runner` conserva autoridad Secret Manager amplia. La nueva
  identidad candidata no existe aún en GCP; no puede afirmarse aislamiento
  efectivo hasta aplicar los gates y obtener una negativa efectiva sobre el
  token ForusBots sin romper el rollback legacy.
- `runtime_vertex` sigue la fase de containers. Si un contrato futuro cambia
  `USE_VERTEX_AI` a `false`, el grant debe hacerse condicional y volver a
  verificarse; el inventario productivo actual revisado usa `true`.
