# Estado de ejecución del plan de finalización handle-ticket

Actualizado 2026-07-15. Rama `handle-ticket-production-finalization` (worktree
`ForUsGuide-handle-ticket-finalization`), 15 commits sobre `3d48415`. **NO se
ha hecho push** (mutación Git sin aprobación). Suite CI local:
`569 passed, 15 skipped` en Python 3.14 (bootstrap NO autoritativo; el gate
real es Cloud Build Python 3.12).

Revisión adversarial (Tarea 15 Paso 5) ejecutada: 17 hallazgos, 8 confirmados;
los 3 P1 y 3 P2 corregidos RED-first en `da1efe4` (ver `15-review-checklist.md`).
Los P1 eran: v1 evadía el segundo factor WIF, pérdida de ForusBots job IDs en
checkpoints degradados, y el reconciliador terminalizaba sin fencear al worker.

## Tareas completadas localmente (0–12)

| Tarea | Commit | Estado |
|---|---|---|
| 0 preflight/worktree/contención | `0d6676f` | ✅ completa |
| 1 contratos externos | `d574884` | ✅ documentada; **4 contratos PENDIENTES** (ver `01-external-contracts.md`) |
| 2 pruebas RED | `98d5016` | ✅ 42 RED que codifican los 12 bloqueos |
| 3 imagen/locks | `687cfc7` | ⚠️ **3a local completa**; 3b (locks 3.12, Dockerfile por digest, smoke real, pip-audit) DIFERIDA a Cloud Build |
| 4 auth v2/WIF/roles | `a1ed02a` | ✅ completa |
| 5 Firestore/TTL/cuotas | `2ddf28e` | ✅ código completo; emulador real DIFERIDO (sin docker) |
| 6 worker resume/fencing | `8e4b468` | ✅ completa |
| 7 Cloud Tasks/reconciler | `b8d10ff` | ✅ completa |
| 8 dependencias/probes | `26aa3ee` | ✅ completa; probes live gateadas (skip sin creds) |
| 9 diferencial/contratos | `3a760e3` | ⚠️ arnés + tests completos; **export n8n real BLOQUEADO (G3/Tarea 1)** |
| 10 Terraform | `035ca2d` | ⚠️ declaración completa; `fmt/validate`/locks/`apply` DIFERIDOS a Cloud Build |
| 11 observabilidad | `e7b8c3f` | ✅ métricas/readiness/runbook/drill; entrega de alertas a canales DIFERIDA (staging) |
| 12 CI/CD | `0630204` | ⚠️ YAML/scripts/tests completos; **bootstrap G1B + push/PR BLOQUEADOS** |

## Tareas 13–18: BLOQUEADAS (ninguna ejecutada)

Cada una requiere staging/producción activos y una o más de estas cosas que
NO están disponibles ni aprobadas en esta sesión:

- **Sesión gcloud** para read/write (expiró con `invalid_rapt` el 2026-07-14).
- **Toolchain** `terraform`/`docker` (ausentes; no se instala sin aprobación).
- **`git push`** (mutación Git; el usuario exigió aprobación exacta).
- **Contratos de la Tarea 1** (participant-plan, ForusBots HTTPS+idempotencia,
  export n8n real, entrega final idempotente): los cuatro pendientes.
- **Gates de aprobación** G1A/G1B/G1C/G2/G3/G4/G5/G6A/G6B/G7/G8/G9/G10: ninguno
  registrado (tabla vacía en `approvals.md`).

| Tarea | Gate(s) | Bloqueo principal |
|---|---|---|
| 13 aplicar staging | G2 (+G1A/G1B/G1C/G3) | backend state, platform aplicada, base/cola/SAs, versiones sandbox |
| 14 E2E/caos staging | G4 | staging activo + contratos live + datos sintéticos |
| 15 rollback/merge/digest canónico | G5 (+G5V) | CI verde remoto, review, push, merge |
| 16 baseline prod endurecida disabled | G6A/G6B | attestation staging, secret versions numéricas, plan/apply prod |
| 17 shadow/knowledge/full | G7/G8/G9 | cohorts n8n, observación ≥24h/≥200 jobs por escalón |
| 18 retiro legacy + evidencia final | G10 | 7 días/1.000 jobs full verdes |

## Acciones operativas pendientes (para el usuario/owners)

1. `gcloud auth login --update-adc` en terminal interactiva (desbloquea GCP).
2. Aprobar y ejecutar `git push` de la rama + abrir PR draft (Tarea 12 Paso 2a).
3. Obtener los 4 contratos de la Tarea 1 de sus owners (ver
   `01-external-contracts.md` §Registro de bloqueos).
4. Registrar cada aprobación en `approvals.md` con el texto exacto
   `APROBADO <GATE> <ALCANCE>` antes de la mutación correspondiente.
5. Ejecutar Tareas 3b/10-validate en Cloud Build (imágenes fijadas por digest).

## Definition of Done

Abierto. El estado correcto es **"hardening/rollout en progreso"**, no "plan
completo": producción sigue en `kb-rag-system-00048-bkc` (disabled) y no existe
aún el rollback anchor `hardened-disabled` (Tarea 16). El trabajo de código,
IaC, CI y pruebas está listo para pasar por los gates cuando el usuario
desbloquee sesión, push y contratos.
