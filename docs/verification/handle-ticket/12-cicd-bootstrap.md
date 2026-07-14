# 12 — CI/CD con controles: bootstrap (Tarea 12)

Estado al 2026-07-14.

## Completado localmente (código/config auditable)

| Artefacto | Estado |
|---|---|
| `kb-rag-system/cloudbuild.yaml` | CI de rama/PR **sin deploy**: lock con hashes, pytest CI (excluye markers live), ruff/mypy, pip-check/pip-audit, detect-secrets, build inmutable, container-smoke, push por SHA, SBOM Syft por digest, scan On-Demand; `requestedVerifyOption: VERIFIED`; sin `:latest`; sin `gcloud run deploy` |
| `cloudbuild.terraform-plan.yaml` / `-apply.yaml` | plan→gate→apply del artefacto exacto; apply verifica manifest y NO regenera plan |
| `cloudbuild.staging-attest.yaml` | promotion attestation write-once, SA sin deploy |
| `cloudbuild.evidence-manifest.yaml` | verifica diff docs-only + generations, manifest create-only |
| `cloudbuild.test-only.yaml` | gates Python 3.12 + revalida digest, sin build/deploy/state |
| `cloudbuild.e2e-image.yaml` | runner E2E por SHA (nunca digest de producción) |
| `Dockerfile.e2e` + `.dockerignore` | mismo base digest, dev lock con hashes, allowlist estricta |
| `scripts/{create,verify}_plan_manifest.py` | manifest write-once + verificación (state drift/root/digest/commit) |
| `scripts/{create,verify}_promotion_manifest.py` | attestation canónica + rechazo por SHA/digest/tampering |
| `scripts/smoke_deployed_ticket.py` | smoke de revisión desplegada (disabled, sin efectos) |
| `tests/test_release_manifests.py` | 16 casos: positive/tampering/wrong-digest/wrong-SHA/state-drift + filtro docs-only del trigger de `main` |
| `infra/terraform/live/platform/cloud_build.tf` | SAs distintas por pipeline; trigger `main` con `ignored_files` EXACTO; CI sin deploy |

Los YAML/scripts privilegiados del repo son **fuente auditable** para
construir el release-controller durante G1B; un cambio posterior no altera
triggers automáticamente (exige nuevo platform plan+gate y nuevo digest).

## BLOQUEADO (mutaciones sin aprobación / sin toolchain / sin sesión)

Ninguno de estos pasos se ejecutó; requieren aprobación explícita y/o
credenciales que no están disponibles en esta sesión:

- **Paso 2a — commit/push del SHA inmutable + PR draft**: `git push` es una
  mutación de Git; el usuario exigió aprobación exacta. No se hizo push ni se
  abrió PR. El branch `handle-ticket-production-finalization` vive local con
  todos los commits de las Tareas 0–12.
- **Paso 3 — bootstrap único de platform + neutralizar `deploy-kb-rag-system`
  (G1B)**: requiere Cloud Shell/runner con terraform, la sesión gcloud (que
  expiró con `invalid_rapt` el 2026-07-14) y la aprobación G1B. El plan binario
  y su hash se generan ahí, no localmente.
- **Paso 3a — migración Firestore project-wide→scoped (G1C)**: dos planes
  exactos (`prepare`/`enforce`) vía `handle-ticket-platform-plan/apply` con
  revisión G1C entre cada uno; bloqueado por lo mismo.
- **Paso 4 — correr `handle-ticket-ci` desde la rama**: requiere los triggers
  ya bootstrappeados y gcloud.
- **Paso 5 — commit/push de la evidencia de bootstrap**: depende de los
  anteriores.

`hashicorp/terraform` en los YAML lleva el placeholder
`@sha256:PINNED_AT_BOOTSTRAP`: el digest exacto se fija al construir el
controller en G1B (no se inventa un digest sin resolverlo contra el registry).

## Regla de contención vigente

Producción permanece en `kb-rag-system-00048-bkc` (disabled) al 100%. Hasta
G1B, el trigger legacy `deploy-kb-rag-system` (que despliega directo a
producción sin aprobación) **sigue activo**: su neutralización es parte del
apply de platform G1B, aún no ejecutado. Ningún push a `main` debe ocurrir
hasta que ese apply esté aplicado (no sólo en el PR).
