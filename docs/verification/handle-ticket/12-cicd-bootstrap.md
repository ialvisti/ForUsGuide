# 12 — CI/CD con controles: bootstrap (Tarea 12)

Estado revisado al 2026-07-21.

## Artefactos locales presentes y verificados

| Artefacto | Estado |
|---|---|
| `kb-rag-system/cloudbuild.yaml` | CI de rama/PR **sin deploy**: lock con hashes, pytest CI (excluye markers live), ruff/mypy, pip-check/pip-audit, detect-secrets, build inmutable, container-smoke, push por SHA, SBOM Syft por digest, scan On-Demand; `requestedVerifyOption: VERIFIED`; sin `:latest`; sin `gcloud run deploy` |
| `cloudbuild.terraform-plan.yaml` / `-apply.yaml` | plan→gate→apply del artefacto exacto; apply verifica manifest y NO regenera plan |
| `cloudbuild.staging-attest.yaml` | promotion attestation write-once, SA sin deploy |
| `cloudbuild.evidence-manifest.yaml` | verifica diff docs-only + generations, manifest create-only |
| `cloudbuild.test-only.yaml` | revalida el digest y ejecuta candidate code en imagen CI sin red/ADC/metadata, filesystem read-only y no-root |
| `cloudbuild.e2e-image.yaml` | build, push por SHA, resolución de digest, smoke, scan y manifest canónico; ejecución real sigue gateada |
| `Dockerfile.e2e` + `.dockerignore` | mismo base digest, dev lock con hashes, allowlist estricta |
| `Dockerfile.release-controller` + `ci/cloudbuild.release-controller.yaml` | recipe candidato efímero que declara `ticket-controller-verify` y sólo hace tests, build local y smoke aislado; **sin push, scan, `images:` ni artefacto promovible**. La SA declarada no es por sí sola una frontera de enforcement |
| `ci/cloudbuild.verify-local.yaml` | gate integral sin publish/apply que declara la misma SA verifier exacta; contratos rechazan SA default, Compute y `kb-rag-runner`. Mientras la SA no exista pre-G1B, una repetición remota falla cerrado |
| `scripts/{create,verify}_plan_manifest.py` | manifest write-once + verificación (state drift/root/digest/commit) |
| `scripts/{create,verify}_promotion_manifest.py` | attestation canónica + rechazo por SHA/digest/tampering |
| `scripts/smoke_deployed_ticket.py` | smoke de revisión desplegada (disabled, sin efectos) |
| `tests/test_release_manifests.py` | contratos positive/tampering/wrong-digest/wrong-SHA/state-drift, pins canónicos y filtro docs-only del trigger de `main` |
| `infra/terraform/live/platform/cloud_build.tf` | SAs distintas por pipeline; trigger `main` con `ignored_files` EXACTO; CI sin deploy |

## Excepción técnica de ownership y gates de containers

Cloud Run, Cloud Tasks, Cloud Scheduler e IAM de service accounts no admiten
una única IAM Condition heredada por `resource.name` que limite de forma
operativa todos los permisos create/update requeridos por Terraform. Para no
conceder `roles/run.admin`, `roles/cloudtasks.queueAdmin` ni Security Admin a
los roots de entorno, los límites que necesitan un broker de parent quedan con
**ownership único** en el state `live/platform`: databases Firestore,
containers Secret Manager (nunca versiones/payload), queues, schedulers,
custom queue roles y sus bindings runtime. Los roots `staging`/`production` no
declaran ni importan copias de esos recursos; sólo poseen recursos hijos
(schemas/TTL, accessor IAM, observabilidad y workloads Run).

Esta es una desviación deliberada del ownership nominal del plan, no una
ampliación de G1B. `environment_container_phase` nace con ambos entornos en
`disabled`; por contrato y por test, un apply G1B puro renderiza **cero**
databases, secrets, queues, schedulers o runtime IAM de entorno. Las únicas
transiciones permitidas son:

1. staging `disabled→managed` con receipts Cloud Build exactos **G1B+G2**
   ligados al candidate y al URI/hash del plan; production debe seguir
   disabled;
2. production `disabled→managed` con receipts exactos **G1B+G6B** ligados al
   plan;
3. con containers managed, handoff Run `disabled→bootstrap`: creator temporal
   project-wide contiene sólo `run.services.create`/`run.jobs.create`;
4. tras crear y atestar los workloads en el state del entorno, platform añade
   `roles/run.developer` directamente a cada service/job inventariado y cambia
   a `managed`, lo que revoca el creator en el mismo plan aprobado.

El manifest de plan fija las fases, inventarios, hashes de inputs y el scope
que debe aprobarse. **Después del bootstrap único manual de Tarea 12 Paso 3**,
los receipts se crean después de cada plan: cada rol usa un trigger y SA
exclusivos, con allowlist literal en la configuración trusted (nunca una
substitution controlable por el caller). `apply` describe de nuevo
build+trigger, exige `APPROVED/SUCCESS`, principals distintos y coincidencia
exacta de candidate, controller, plan URI, plan hash y scope. `approvals.md`
queda sólo como auditoría para esos applies posteriores. La excepción inicial
usa el texto G1B externo, el SHA remoto y el hash del plan binario exactamente
como prescribe el plan; no puede depender de triggers que ese mismo apply crea.
Apply vuelve además a verificar plan binario, JSON semántico, state y manifest.
Las fases no pueden retroceder y
queues/schedulers/databases/secrets tienen delete rechazado tanto por lifecycle
como por el controller. Así G2 no puede materializar recursos production ni
G6B recursos staging.

El catálogo declarativo contiene **22 receipts** independientes: los 12 de
G1B/G2/G6B/G1C y, además, cinco actores disjuntos de G4 (requester, n8n,
participant-plan, ForUsBots y delivery), dos de G5 (maintainer + requester) y
tres de G5V (security + release + requester). Evidence manifest, staging
attestation y runtime attestation reciben los build IDs autenticados; ningún
hash o fila de `approvals.md` aportado por el candidate sustituye esos builds.

`environment_release_phase` también queda fijado en el manifest/scope del
plan. Cloud Scheduler permanece `paused` en `disabled`, `infra_only` y ambas
fases dark; inventariar el Run Job no lo habilita. Mientras no exista un
receipt que ligue simultáneamente plan y evidencia G4, cualquier platform
apply que intente una fase activa staging se rechaza; production activa sigue
rechazada hasta G7–G9.

Cloud Tasks no expone `state=PAUSED` en el provider Terraform fijado. El
controller cierra ese hueco sin fingir declaratividad: antes de tocar una queue
existente ejecuta PauseQueue sobre su nombre exacto, exige `PAUSED` y lista de
tasks vacía; después del apply repite la misma verificación para una queue
recién creada. La SA sólo puede listar metadata de tasks mediante un custom
role de permiso único y `google_cloud_tasks_queue_iam_member` directo en cada
cola; no recibe `tasks.get/run`, `queues.resume` ni un grant project-wide. No
existe auto-resume: resume es una operación separada, gateada y auditada.

G1C usa quorums independientes para `prepare` y `enforce` (GCP owner, API
owner y operations). `enforce` permanece fail-closed hasta que un smoke
inmutable posterior al apply `prepare` valide esquema, lineage, plan/apply,
los tres receipts y todos los checks core. Un URI o JSON aportado por el
candidate no satisface esa condición.

### Cierre del release-controller y límites de confianza

`live/platform` exige un `release_controller_image_digest` y los triggers
privilegiados sólo invocan sus subcomandos. El build histórico de verificación
`5fe68b12-1381-4bb3-9b4f-594ca401fda0` construyó la imagen y ejecutó su CLI
sin red, pero corrió con `kb-rag-runner`, no con la nueva identidad verificadora.
Demuestra el resultado de aquel source y que esa ejecución concreta no mutó
las superficies comparadas; **no demuestra la frontera añadida después**.
`test-only` usa locks/baseline/verificador trusted y ejecuta código candidato
no-root, sin red/ADC/metadata, read-only y sin Docker socket.

La revisión posterior dejó el recipe
`ci/cloudbuild.release-controller.yaml` en modo verify-only: no contiene push,
scan, resolución de digest ni `images:`. Declara `ticket-controller-verify`,
identidad que en Terraform sólo recibe ejecución de Cloud Build y
`roles/logging.logWriter`, y queda ausente de Artifact Registry, On-Demand
Scanning, evidence, state y runtime. Ese campo `serviceAccount` **no impone la
identidad efectiva**: la SA configurada en un trigger prevalece y un submitter
manual con `actAs` puede seleccionar otra. Las fronteras estáticas adicionales
son que ningún recipe/trigger referencia `ticket-controller-build` y que
`platform-apply` no puede asumir ni el verificador ni el publisher.

La situación pre-G1B sigue siendo circular y, por tanto, fail-closed. En el
proyecto real no existen aún `ticket-controller-verify` ni
`ticket-controller-build`; este mismo root platform las materializaría, pero
G1B exige previamente un digest publicado y escaneado del controller. El árbol
modela al publisher con writer+scan, pero **no contiene una ruta declarada y
confiable para usarlo**. No es seguro resolverlo ejecutando tests o el
Dockerfile candidato bajo esa SA, porque recrearía el hallazgo P1.

La salida futura requiere un paso PREP separado y aprobado, o una cadena que
construya el OCI exacto en cuarentena con una identidad no privilegiada y que
un publisher source-less lo escanee y copie byte a byte sin ejecutar ningún
`RUN` candidato. Esa cadena debe exigir SHA remoto completo y provenance
`resolvedGitSource`/`resolvedRepoSource`. Hasta que exista y se revise, no hay
«excepción bootstrap externa» operable y no debe intentarse la publicación.

Antes de `terraform init/plan`, el controller aplica allowlists de resource
types, único data source exacto, imports exactos y bloquea declassificación,
filesystem functions, endpoints/provider overrides y caches/inputs
implícitos. El plan semántico se vuelve a validar después.

Esto desactiva el camino vulnerable conocido, pero **no completa Tarea 12 ni
habilita G1B**. Siguen faltando el publisher confiable/source-less, el digest
inmutable escaneado y su vínculo con el plan y los receipts reales. El
analizador HCL propio es conservador y no equivale a firmar el cuerpo completo
de todo el árbol Terraform.

### Identidad del producer productivo

La revisión live `kb-rag-system-00048-bkc` usa `kb-rag-runner`, que conserva
`roles/secretmanager.secretAccessor` project-wide y un member directo sobre el
token ForusBots. Los grants per-secret worker-only del módulo son aditivos: no
revocan esa autoridad heredada. Por ello el hardening declara una SA
`ticket-producer-prod` separada para la revisión candidata, con permisos core
mínimos y secretos del producer por recurso, excluyendo ForusBots. La SA legacy
no se elimina ni modifica y sigue perteneciendo al rollback anchor.

La frontera sólo existe después de G1B/G6A/G6B. Antes de cualquier fase activa,
el gate debe probar sobre la versión exacta del secreto que
`ticket-producer-prod` obtiene DENIED y `ticket-worker-prod` GRANTED mediante
Policy Troubleshooter/effective IAM; una precondition o un mapa Terraform no
sustituyen esa prueba porque puede haber grants heredados o drift.

## BLOQUEADO (gates, contratos y artefactos promovibles)

La autenticación gcloud está disponible y el build remoto pre-delta pasó. El
**Paso 2a** quedó completo: commit y push de
`handle-ticket-production-finalization`, con
[PR draft #1](https://github.com/ialvisti/ForUsGuide/pull/1). Esto no autoriza
merge ni ningún gate de rollout.

Los pasos restantes no se ejecutaron porque requieren gates/contratos distintos
de la autorización para verificar:

- **Paso 3 — bootstrap único de platform + neutralizar `deploy-kb-rag-system`
  (G1B)**: requiere digest publicado+escaneado, SHA remoto limpio, emails
  contractuales y quorum G1B externo sobre el plan binario exacto. Está además
  bloqueado por la circularidad descrita arriba: las SAs bootstrap aún no
  existen y no hay recipe confiable/source-less que publique el controller. El
  YAML candidato verify-only no sirve para publicar. Ese apply crearía los
  receipt triggers para los applies posteriores.
- **Paso 3a — migración Firestore project-wide→scoped (G1C)**: dos planes
  exactos (`prepare`/`enforce`) vía `handle-ticket-platform-plan/apply` con
  revisión G1C entre cada uno; bloqueado por lo mismo.
- **Paso 4 — correr `handle-ticket-ci` privilegiado**: requiere los triggers
  ya bootstrappeados; el build `5fe68b12…` fue sólo verificación sin publicación.
- **Paso 5 — commit/push de la evidencia de bootstrap**: depende de los
  anteriores.

Los builders Terraform/Python/Google Cloud CLI/Docker/Syft usados por los YAML
están fijados por digest. Google Cloud CLI y emulador usan pins 577.0.0
separados; un contrato exige el pin genérico canónico en todos los manifests.
El build/smoke efímero no sustituye la publicación+scan del digest promovible.

## Regla de contención vigente

Producción permanece en `kb-rag-system-00048-bkc` (disabled) al 100%. Hasta
G1B, el trigger legacy `deploy-kb-rag-system` (que despliega directo a
producción sin aprobación) **sigue activo**: su neutralización es parte del
apply de platform G1B, aún no ejecutado. Ningún push a `main` debe ocurrir
hasta que ese apply esté aplicado (no sólo en el PR).
