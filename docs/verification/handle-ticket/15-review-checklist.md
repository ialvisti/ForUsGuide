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
| **P1** | v1 alcanza el pipeline durable completo con sólo X-API-Key, evadiendo el segundo factor WIF de v2 | v1 añade `Depends(verify_workload_identity)` (no-op durante la migración legacy; cierra el bypass en cuanto WIF se configura) | `TestV1AlsoRequiresWorkloadIdentity` (rechaza sin token con WIF activo; sigue con X-API-Key durante migración) |
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
| **P1** | GET v1 devolvía estado y job IDs con sólo API key | poll v1 exige también workload identity cuando WIF está configurado; OpenAPI declara ambos factores |
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

- **v1-bypass-WIF**: confirmado por su propio verificador (P1, arriba).
- **stale-generation TOCTOU** se reclasificó como confirmado y quedó cerrado
  al plegar `expected_generation` dentro del claim transaccional.
- **malformed task body → 422 retryable** quedó cerrado: una task autenticada
  pero malformada se ACKea 204; un request no autenticado sigue fallando
  cerrado.

## Estado

Suite completa local tras integrar ambas pasadas: **717 passed, 18 skipped**
sobre **735 tests** (Python 3.14 bootstrap). Los skips de Firestore se ejecutan
contra el emulador en el build remoto; live/staging siguen gateados. El gate
autoritativo Python 3.12/Linux se registra aparte en `03-image-and-locks.md`.

La garantía ForusBots sigue deliberadamente incompleta: el intent durable
evita duplicar a ciegas, pero sin lookup/idempotencia upstream no puede decidir
si un POST ambiguo creó un job. El contrato externo de Tarea 1 continúa como
bloqueo real para `full` y no se marca cerrado por esta mitigación.
