# 12 — CI/CD con controles: bootstrap (Tarea 12)

Estado revisado al 2026-07-15.

## Artefactos locales presentes (aún no es un controller desplegable)

| Artefacto | Estado |
|---|---|
| `kb-rag-system/cloudbuild.yaml` | CI de rama/PR **sin deploy**: lock con hashes, pytest CI (excluye markers live), ruff/mypy, pip-check/pip-audit, detect-secrets, build inmutable, container-smoke, push por SHA, SBOM Syft por digest, scan On-Demand; `requestedVerifyOption: VERIFIED`; sin `:latest`; sin `gcloud run deploy` |
| `cloudbuild.terraform-plan.yaml` / `-apply.yaml` | plan→gate→apply del artefacto exacto; apply verifica manifest y NO regenera plan |
| `cloudbuild.staging-attest.yaml` | promotion attestation write-once, SA sin deploy |
| `cloudbuild.evidence-manifest.yaml` | verifica diff docs-only + generations, manifest create-only |
| `cloudbuild.test-only.yaml` | gates Python 3.12 + revalida digest, sin build/deploy/state |
| `cloudbuild.e2e-image.yaml` | ⚠️ builders fijados por digest; todavía falta cerrar scan + manifest canónico antes de G1B/G2 |
| `Dockerfile.e2e` + `.dockerignore` | mismo base digest, dev lock con hashes, allowlist estricta |
| `scripts/{create,verify}_plan_manifest.py` | manifest write-once + verificación (state drift/root/digest/commit) |
| `scripts/{create,verify}_promotion_manifest.py` | attestation canónica + rechazo por SHA/digest/tampering |
| `scripts/smoke_deployed_ticket.py` | smoke de revisión desplegada (disabled, sin efectos) |
| `tests/test_release_manifests.py` | 51 casos: positive/tampering/wrong-digest/wrong-SHA/state-drift, builders inmutables + filtro docs-only del trigger de `main` |
| `infra/terraform/live/platform/cloud_build.tf` | SAs distintas por pipeline; trigger `main` con `ignored_files` EXACTO; CI sin deploy |

### Hallazgo correctivo de la segunda revisión

`live/platform` exige un `release_controller_image_digest` y los triggers
privilegiados sólo invocan sus subcomandos. Sin embargo, el repositorio no
contiene todavía una receta/Dockerfile/entrypoint que implemente `plan`,
`apply`, `staging-attest`, `evidence-manifest`, `test-only` y `e2e-image` a
partir de los YAML/scripts revisados. Los YAML por sí solos no son una imagen
ejecutable ni quedan protegidos de cambios posteriores del candidate SHA.

Por tanto, **Tarea 12 no puede marcarse completa y G1B no debe solicitarse**
hasta que exista, se pruebe y se escanee ese controller (o que Terraform
materialice una configuración inline equivalente e inmutable). La validación
de `release_controller_image_digest` falla cerrada cuando no hay digest, pero
eso evita un bootstrap inseguro; no completa el controller.

## BLOQUEADO (gates, contratos y artefacto de controller)

Ninguno de estos pasos se ejecutó; requieren aprobación explícita y/o
credenciales que no están disponibles en esta sesión:

- **Paso 2a — commit/push del SHA inmutable + PR draft**: `git push` es una
  mutación de Git; el usuario exigió aprobación exacta. No se hizo push ni se
  abrió PR. La rama y las correcciones de esta auditoría viven sólo en el
  worktree aislado.
- **Paso 3 — bootstrap único de platform + neutralizar `deploy-kb-rag-system`
  (G1B)**: además de commit remoto y aprobación G1B, requiere primero el
  release-controller reproducible señalado arriba. No existe aún un digest
  válido que pueda suministrarse honestamente a `cicd_bootstrap`.
- **Paso 3a — migración Firestore project-wide→scoped (G1C)**: dos planes
  exactos (`prepare`/`enforce`) vía `handle-ticket-platform-plan/apply` con
  revisión G1C entre cada uno; bloqueado por lo mismo.
- **Paso 4 — correr `handle-ticket-ci` desde la rama**: requiere los triggers
  ya bootstrappeados y gcloud.
- **Paso 5 — commit/push de la evidencia de bootstrap**: depende de los
  anteriores.

Los builders Terraform/Python/Cloud SDK/Docker/Syft usados por los YAML están
fijados por digest. Esto corrige la mutabilidad del builder, pero no sustituye
el empaquetado pendiente del release-controller.

## Regla de contención vigente

Producción permanece en `kb-rag-system-00048-bkc` (disabled) al 100%. Hasta
G1B, el trigger legacy `deploy-kb-rag-system` (que despliega directo a
producción sin aprobación) **sigue activo**: su neutralización es parte del
apply de platform G1B, aún no ejecutado. Ningún push a `main` debe ocurrir
hasta que ese apply esté aplicado (no sólo en el PR).
