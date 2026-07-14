# Infraestructura handle-ticket (Terraform/OpenTofu)

Terraform es el **único** controlador de Cloud Run, su configuración, escalado
y tráfico. Cloud Build construye/atesta el digest y un pipeline aprobado
ejecuta `plan`/`apply`; **ningún YAML** usa `gcloud run deploy/update/
services update-traffic`.

## Roots y estado (aislados)

| Root | Backend bucket/prefix | Ownership |
|---|---|---|
| `live/platform` | `rag-kb-system-tfstate-platform-900340137010/state` | APIs, Artifact Registry, bucket de evidencia, SAs/build triggers compartidos, pool/provider AWS WIF |
| `live/staging` | `rag-kb-system-tfstate-staging-900340137010/state` | base `ticket-staging`, cola, producer/worker/reconciler staging, IAM/monitoring |
| `live/production` | `rag-kb-system-tfstate-production-900340137010/state` | recursos equivalentes sobre `(default)` + imports existentes |

Ningún member/resource vive en dos states. Los bindings `roles/datastore.user`
de `kb-rag-runner` (project-wide y scoped) viven **sólo** en `live/platform`
(migración G1C).

## Modelo de release cerrado

`release_phase ∈ {infra_only, dark_no_traffic, dark_100, shadow,
knowledge_only, full}`. Invariantes (precondition en `modules/.../main.tf`):

- `infra_only` no crea servicios Run (sólo base/cola/SAs/monitoring).
- `dark_*` / `infra_only` fuerzan `ticket_handler_mode=disabled`.
- **n8n es el único sampler de cohorts**: `shadow_sample_rate=100` sólo con
  `release_phase=shadow`; 0 en toda otra fase (es un invariant, no el
  porcentaje del cohort). Tras `dark_100` el producer durable queda 100% y el
  cohort se controla en n8n, no por split de tráfico.

Todo servicio exige secret versions **numéricas** de Secret Manager
(`secret_version_refs`, validado contra `latest`); imagen por `@sha256:`.

## Aislamiento por base Firestore

La base nombrada es el límite IAM (no un prefijo de colección). Staging usa
`ticket-staging`; producción `(default)`. Las pruebas negativas (ninguna SA
staging accede `(default)` y viceversa) se validan contra staging real
(Tarea 14); el módulo declara el binding database-scoped.

## Flujo de aplicación (todo gateado)

```
fmt/validate (sin backend)        → cualquiera, sin mutar GCP
plan  (trigger *-plan)            → genera .tfplan binario + hash → evidencia
gate  (humano registra APROBADO)  → G1A/G1B/G1C/G2/G6B según root/fase
apply (trigger *-apply)           → terraform apply saved.tfplan (no replanifica)
```

- Platform bootstrap/apply: **G1B** (Tarea 12 Paso 3).
- Migración Firestore project-wide→scoped: **G1C** (Tarea 12 Paso 3a).
- Staging: **G2** (Tarea 13).
- Producción dark: **G6B** (Tarea 16).

## Validación local diferida

`terraform`/`tofu` **no están disponibles** en el host de ejecución y el plan
prohíbe instalar software sin aprobación. Por tanto:

```bash
terraform fmt -check -recursive infra/terraform
for root in platform staging production; do
  terraform -chdir="infra/terraform/live/$root" init -backend=false -input=false
  terraform -chdir="infra/terraform/live/$root" validate
done
```

se ejecutan en Cloud Build/Cloud Shell con la imagen `hashicorp/terraform`
fijada por digest (Tarea 12) **antes** del primer `plan`. Los
`.terraform.lock.hcl` de cada root se generan con ese `init` (dependen del
provider resuelto en Linux) y se commitean entonces; no se pueden fabricar sin
`terraform init`. `.tfplan`/`.terraform/` nunca se versionan.

Si se usa OpenTofu, sustituir consistentemente todos los comandos y registrar
la versión; no mezclar motores en un state.
