# 15 — Revisión adversarial del diff (Tarea 15 Paso 5)

Ejecutada 2026-07-14/15 con un workflow de 5 revisores por dimensión
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

## No confirmados (verificador agotó sesión) — evaluados por análisis

- **v1-bypass-WIF**: confirmado por su propio verificador (P1, arriba).
- **stale-generation TOCTOU** (gen-check y claim no atómicos): el `claim` es
  atómico (un solo epoch gana); un intento stale que pasó el gen-check no
  puede publicar/guardar dos veces por el fencing. Riesgo acotado; nota para
  hardening futuro (plegar la generación dentro del claim).
- **malformed task body → 422 retryable**: FastAPI valida `_TaskBody` antes del
  handler; sólo ocurre si Cloud Tasks envía un body sin `job_id` (no sucede: el
  productor/reconciliador construyen el body). Defensa en profundidad menor.

## Estado

Suite CI local tras correcciones: **569 passed, 15 skipped** (Python 3.14
bootstrap; gate autoritativo Cloud Build 3.12). Todos los P1 confirmados
cerrados con RED primero. Los gates afectados (G2/G4 en staging) se repiten
cuando staging esté activo (bloqueado por contratos/aprobaciones).
