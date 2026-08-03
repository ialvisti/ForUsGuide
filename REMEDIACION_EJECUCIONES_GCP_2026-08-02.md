# Remediación del incidente de ejecuciones GCP

**Fecha:** 2026-08-02
**Última actualización:** 2026-08-03
**Proyecto:** `rag-kb-system`
**Región:** `us-central1`
**Documento de origen:** `AUDITORIA_EJECUCIONES_GCP_2026-08-02.md`
**Rama de trabajo:** `codex/fix-ticket-execution-failures`
**Commits de código:** `ae0a81d031dcb0d3cae7032e32ed74c2ef14103f`,
`ba9c060ac9e7ced428b64aeb9b94fbb89b36de3e`,
`d60388b413379a21e04e552c7edf5b7af25e6462`
**PRs de código:** [ForUsGuide #14](https://github.com/ialvisti/ForUsGuide/pull/14),
[#16](https://github.com/ialvisti/ForUsGuide/pull/16),
[#17](https://github.com/ialvisti/ForUsGuide/pull/17) y
[ForUsBots #5](https://github.com/ialvisti/ForUsBots/pull/5)
**Merge final de código en `main`:** `d60388b413379a21e04e552c7edf5b7af25e6462`

## Estado ejecutivo

El incidente quedó **corregido, desplegado y habilitado en producción bajo
`full`**. La corrección Firestore inicial se integró mediante el PR #14; el
fail-closed de required-data mediante #16; el contrato idempotente final del
cliente mediante #17; y la garantía durable upstream mediante ForUsBots #5.

Una auditoría independiente no encontró P0 residuales y los dos P1 y el P2 que
detectó quedaron corregidos con pruebas RED→GREEN antes del gate remoto final.
El producer, worker y reconciliador productivos usan ahora el digest
`sha256:eb2c3af9cae3200b6b2d433bd2879d42048beeac123077f1d861ec8569068aca`.
ForUsBots usa el digest
`sha256:f9cebd31f2eb63ecd21e556695ca0f86c03afd6071943726ca8451202f969d1f`.
La ruta `generate_response` fue validada en una revisión `full` sin tráfico y
sólo después promovida al 100%.

## Cierre productivo `full` — 2026-08-03

ForUsBots 2.6 reserva atómicamente receipt + job en Firestore antes de encolar,
devuelve el mismo job para la misma `Idempotency-Key` y request, rechaza con
`409` un request cambiado, cerca running/terminal por owner+epoch y no
reejecuta un job huérfano tras reinicio. El build
`f9fcea3d-a3c9-4f9e-8a45-6a1d36016ddf` terminó `SUCCESS`; el MIG quedó estable
en `forusbots-template-70befb4-f9cebd31f2eb` y el smoke live participant/plan
confirmó replay, conflicto, terminales `succeeded`, receipts, fingerprints y
TTL.

ForUsGuide `main` build
`bb9dd27f-71b5-4667-a46b-35b28675e788` terminó `SUCCESS` sobre el merge exacto
`d60388b…`, con tests, secret gates, locks, container smoke, SBOM, provenance y
scan. Se promovieron de forma controlada:

| Superficie | Resultado final |
|---|---|
| Producer | `kb-rag-system-fulld60388b`, 100%, `Ready=True`, `full` |
| Worker | `kb-rag-ticket-worker-incd60388b`, 100%, `Ready=True`, `full` |
| Reconciliador | generación 6, digest final, timeout 300 s, cero retries |
| Rollback inmediato | `kb-rag-system-incd60388bko`, `knowledge_only`, sin tráfico |
| Cola / scheduler | `RUNNING` / `ENABLED`, sin backlog, `*/6`, deadline 300 s, cero retries |

El smoke GR autenticado usó un ticket sintético y el par participant/plan
autorizado sin registrar esos valores. Confirmó aceptación y replay al mismo
ticket job, `409` para payload cambiado, ruta `generate_response`, external job
ID durable, terminal `succeeded`, cero fallos/reconciliación manual y
`next_action=send_participant_reply`. No publicó nada al participante. El
reconciliador tuvo una ejecución manual y el primer tick automático posterior al
rollout con `Completed=True`; producer y worker nuevos quedaron sin logs severos,
`INTERNAL_ERROR` ni `DURABLE_STATE_FAILED`.

Las secciones siguientes conservan el detalle de la contención y promoción
iniciales como historial del incidente; el estado vigente es el cierre `full`
anterior.

## Contención productiva inicial (histórico)

Al revalidar producción se encontraron dos admisiones posteriores al corte de
la auditoría, a las 18:37 y 21:10 UTC. Ambas terminaron `succeeded`, pero seguían
entrando por la revisión defectuosa en modo `full`. Se aplicó una contención
reversible que cambia **únicamente** el modo del productor:

| Campo | Antes | Contención |
|---|---|---|
| Revisión | `kb-rag-system-00052-gxw` | `kb-rag-system-00053-jmx` |
| Tráfico | 100% | 100% |
| Imagen | `sha256:fc84c5412e5cb7908e6aded51975f905cb925451af4fe505826bf566ef7f4c0b` | mismo digest |
| `TICKET_HANDLER_MODE` | `full` | `knowledge_only` |
| Service account | `ticket-producer-prod@rag-kb-system.iam.gserviceaccount.com` | sin cambio |

La revisión nueva quedó `Ready=True`; los probes autenticados `/health` y
`/readyz` devolvieron `healthy` y `ready`. No se cambió el worker, la cola, el
scheduler, Firestore, secretos ni datos. `knowledge_only` conserva KQ y polling,
pero coacciona GR a una salida sin ForUsBots.

La lectura live de 2026-08-03 16:13–16:15 UTC confirmó de nuevo 100% del
tráfico en `kb-rag-system-00053-jmx`, mismo digest y service account, y HTTP
200 autenticado tanto en `/health` como en `/readyz`. La revisión anterior no
tiene tráfico activo.

Rollback de contención, sólo si lo autoriza el incident commander:

```bash
gcloud run services update-traffic kb-rag-system \
  --project=rag-kb-system --region=us-central1 \
  --to-revisions=kb-rag-system-00052-gxw=100
```

Ese rollback reabriría el defecto original y no debe usarse para activar GR.

## Promoción productiva inicial del código corregido (histórico)

El merge `8055c2a2d4aaed283e043c9ff41a1b6d85d08d52` activó únicamente el
trigger CI de `main`: no contiene deploy, apply ni cambio de tráfico. Cloud
Build `b77a08c7-aec5-4a49-8ec8-8b0f7fd0e910` terminó `SUCCESS` en sus nueve
pasos (2026-08-03 16:41:49–16:46:21 UTC), con source provenance ligado al
merge exacto, SLSA build level 3, SBOM write-once y scan sin vulnerabilidades
ni excepciones. El artefacto promovido fue:

`us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:0711f1f55e5e38d9becbac77fa2853fb96996369b8ed4fb8d8f03bff28b6a9c4`.

La promoción se hizo de forma gradual con queue y scheduler pausados, revisiones
a 0%, probes autenticados y canary del producer antes del 100%:

| Superficie | Resultado productivo |
|---|---|
| Producer | `kb-rag-system-inc8055c2a`, 100%, `Ready=True`, `knowledge_only`, SA `ticket-producer-prod` |
| Worker | `kb-rag-ticket-worker-inc8055c2a`, 100%, `Ready=True`, SA `ticket-worker-prod` |
| Reconciliador | generación 5, digest nuevo, timeout 300 s, `maxRetries=0`, SA `ticket-reconciler-prod` |
| Cloud Tasks | `RUNNING`, 2/s, concurrencia 2, 5 intentos, logging sampling 1.0 |
| Scheduler | `ENABLED`, `*/6 * * * *`, deadline 300 s, cero retries |

El canary del producer sirvió 40/40 `/readyz` con 3 respuestas 200 atribuidas
a la revisión candidata y cero errores. Después de promoverlo al 100%, otras
20/20 respuestas fueron 200. Un smoke funcional v2 con datos exclusivamente
sintéticos confirmó `queued → running → succeeded`, replay de la misma
`Idempotency-Key` al mismo job, `error=none` y
`next_action=send_participant_reply`. `knowledge_only` impidió cualquier efecto
ForUsBots.

El reconciliador tuvo una ejecución manual controlada
`ticket-reconciler-prod-f2rmc` y el primer tick automático
`ticket-reconciler-prod-t8nmv`; ambas terminaron `Completed=True` con el digest
nuevo y sin retry. Queue y logs quedaron sin backlog ni errores al cerrar la
ventana. Los anchors inmediatos de rollback son las revisiones anteriores
`kb-rag-system-00053-jmx` (también `knowledge_only`) y
`kb-rag-ticket-worker-00004-zf8`; no se eliminó ninguna revisión ni dato.

## Causa raíz confirmada y corrección

La causa de alta confianza de los ocho `INTERNAL_ERROR` era un documento
durable incompatible con Firestore Standard: el diagnóstico
`field_mapping.deterministic_mapped` contenía arrays directamente anidados. El
fallo ocurría después de que ForUsBots terminaba y antes de persistir el
checkpoint GR.

La corrección reemplaza cada par por objetos `{module, field}` y agrega un
validador Firestore Standard reutilizable antes del efecto externo y antes de
cada escritura. El validador rechaza, sin reflejar valores ni rutas:

- arrays directamente anidados;
- valores no soportados o no finitos;
- nombres/caminos de campo inválidos o demasiado largos;
- profundidad mayor a 20;
- GeoPoints fuera de rango;
- referencias y documentos por encima de límites conservadores;
- strings o claves que no sean UTF-8 válidos.

El fixture realista recorre la ruta de producción
`_map_fields -> InquiryOutcome -> _entry_from_outcome -> repository` con 10
artículos y 21 chunks sintéticos. La forma antigua se rechaza antes de RPC y la
nueva forma se conserva en el round-trip del Emulator.

## Efectos externos e idempotencia

El worker ahora persiste el intent de ForUsBots antes del submit y registra el
ID externo inmediatamente después de recibirlo. Los checkpoints posteriores
hacen merge monotónico de intent, IDs y receipts; una snapshot terminal vieja
no puede borrar un ID registrado por el observer.

Ante reinicio, un receipt confirmado usa `resume_job` y sólo hace polling; no
repite el POST. Timeout, fallo de polling, error de checkpoint o circuito
abierto al reanudar conservan el ID y exigen reconciliación. Los POST ambiguos
continúan fail-closed: nunca se reintentan a ciegas.

Un timeout de una inquiry GR marca reconciliación manual y queda no
reintentable. En la agregación, esa señal domina cualquier error transitorio de
otra inquiry y publica `FORUSBOTS_NEEDS_RECONCILIATION`; así un consumidor no
puede convertir un efecto posiblemente aceptado en un replay ciego.

El `dedupe_scope` evita coalescing entre tickets o tenants dentro del proceso.
Desde el cierre final deriva además una `Idempotency-Key` opaca y estable por
job/inquiry/operación, registrada con el contrato `forusbots-submit-v1`. Sólo se
envía en los POST participant/plan scoped. Timeouts, 408, 429 y 5xx reutilizan la
misma key; `409`, `INTERRUPTED` y `DURABLE_STATE_FAILED` permanecen fail-closed
para reconciliación. Un worker con lease nuevo ejecuta su propio observer
cercado aunque comparta la identidad upstream del job.

## Reconciliación de los ocho casos históricos

Se correlacionó cada `submit_success` de KB RAG con el evento
`job.accepted` de ForUsBots por timestamp (diferencia aproximada de 3–4 ms) y
luego se consultó únicamente `jobId`, tipo y timestamp. Los ocho llegaron a
`job.succeeded`:

| Ticket job | ForUsBots job | Estado observado |
|---|---|---|
| `686c63d0846e4e2f90bc1439700d3d89` | `8b6d6939-9c1b-46db-8a61-3bf0e2a50367` | confirmed-effect / `job.succeeded` |
| `43d41fd2f7074c9da8236246dac8b62d` | `faf2439a-672c-4566-832c-dd5de2f2cc79` | confirmed-effect / `job.succeeded` |
| `c40b82db8f94428e8059def48dea9e67` | `06ecda5c-0c58-47cc-b01d-e6d00100b5cc` | confirmed-effect / `job.succeeded` |
| `b6a1b32782d146a28b75f1382a489c1e` | `5cc22315-7198-4d16-815f-23c7f344be08` | confirmed-effect / `job.succeeded` |
| `1aef5819caad4f2399f35ea71510ac52` | `76b82fb8-50b1-4007-bf9d-184d753096f0` | confirmed-effect / `job.succeeded` |
| `a219e9f8b9a34c5f876755df5ec6e5c3` | `bafc0ba6-fe71-4151-97c1-720b15f02698` | confirmed-effect / `job.succeeded` |
| `0dd41f7ea62a4912acf0dcc62fc52ba6` | `57ac3546-ceb8-40c4-8ff2-aec0523264b0` | confirmed-effect / `job.succeeded` |
| `cba61d63bb2640f08c0ed25e3f046fbe` | `82b9fa40-8649-4578-8d1e-92389180aef2` | confirmed-effect / `job.succeeded` |

**Decisión:** cero replays. El cierre participant-facing de cada caso pertenece
al owner operativo/legacy con esta evidencia; volver a ejecutar ForUsBots
duplicaría un efecto confirmado.

Los jobs `f4e30e1e36c540cd829a9981cd70eb2b` y
`19074ee4c334410faba544e0503cace5` se registran como correcciones de
clasificación: fueron bloqueos locales `UnsafeRetrievalQuery`, no outages de
Pinecone y no son reintentables.

## Clasificación y resiliencia de dependencias

- `UnsafeRetrievalQuery` se resuelve determinísticamente antes del LLM y usa
  `UNSAFE_RETRIEVAL_QUERY`, no reintentable, sin incrementar métricas Pinecone.
- `PINECONE_TRANSIENT_FAILURE` queda reservado para timeout, transporte, 429,
  5xx o circuito abierto atribuible al proveedor.
- La búsqueda Pinecone usa el endpoint data-plane REST 2025-10 con transporte
  `httpx` de cero retries. El uploader es la única autoridad: un 408 hace una
  request y un 503 hace como máximo tres, en vez de 4 y 12 respectivamente con
  los retries internos del SDK 9.1 multiplicados.
- Cada retry vuelve a consultar el circuito después del backoff; una consulta
  ya en vuelo se detiene si otra rama abrió el circuito y no multiplica
  requests concurrentes obsoletos.
- Los upserts tampoco reintentan 4xx, 408 ni errores locales de contrato; sólo
  timeout/transporte, 429 y 5xx consumen el presupuesto acotado.
- `requirements.txt`, `requirements.in` y ambos locks quedan alineados con
  Pinecone 9.1; una instalación de desarrollo ya no puede degradar el SDK a
  una major distinta de la verificada en CI/runtime.
- Los pools HTTP de Pinecone y ForUsBots se cierran durante shutdown.
- KQ y GR almacenan únicamente taxonomías cerradas; cuerpos de error y mensajes
  de excepciones no cruzan logs, checkpoints ni API.

Además, un fallo técnico de `get_required_data` ya no se interpreta como
"cero campos requeridos": conserva una taxonomía cerrada, corta GR antes de
mapping, ForUsBots, extracción y generación, y nunca produce una respuesta
publicable con datos vacíos. Sólo un error tipificado de Pinecone puede
publicarse como transitorio de Pinecone; el resto falla cerrado.

## Persistencia, fases y privacidad

Las fases observables son, en orden:

1. `handle_inquiry`;
2. `convert_outcome`;
3. `validate_durable_document`;
4. `persist_inquiry_result`;
5. `mark_terminal`.

La telemetría de fallo contiene sólo fase, tipo/código allowlisted, fingerprint
estructural, tamaño estimado, profundidad y contador de arrays inválidos. El
fingerprint no depende del mensaje ni del contenido del ticket. Pruebas con
sentinels confirman que email, SSN, account number y mensajes upstream no se
reflejan.

La agregación multi-inquiry aplica prioridad coherente: reconciliación manual e
`INTERNAL_ERROR` no pueden quedar ocultos por una inquiry anterior ni producir
combinaciones contradictorias como `UNSAFE_RETRIEVAL_QUERY` reintentable.

## Observabilidad e infraestructura deseada

El código instala logging estructurado canónico en staging y producción. Las
métricas del productor/worker filtran exclusivamente esa representación; el
reconciliador batch conserva su salida canónica de texto. Se agregaron:

- accepted, terminal e inquiry-terminal por ruta/código cerrado;
- alertas inmediatas de `failed`, `partial` e `INTERNAL_ERROR`;
- razón terminal/accepted <99% durante 15 minutos;
- reconciliación manual y jobs fuera del SLA;
- tiempo de aplicación del reconciliador separado de provisioning.

Terraform conserva `value > 0` para acciones del reconciliador, habilita logs de
Cloud Tasks con sampling 1.0 durante la ventana del incidente, adopta mediante
imports la queue/scheduler/rol preexistentes y evita delete/replace. El
controlador valida valores operativos exactos y sólo añade
`cloudtasks.queues.resume`; si el apply o la verificación falla, restaura la
cola pausada.

El plan de plataforma recibe lectura `objectViewer` condicionada únicamente a
`environment-inputs/` del bucket de evidencia, necesaria para validar los
manifests versionados antes de adoptar contenedores de secretos. No se añadió a
la lectura auxiliar de todo el bucket. La primera concesión requiere un plan
bootstrap con containers deshabilitados; sólo un plan posterior puede usar
inputs managed.

El reconciliador deseado usa una ejecución cada 6 minutos, timeout de 300 s y
cero retries automáticos: el siguiente tick es la recuperación idempotente y
el intervalo excede el timeout, evitando solapamiento. Con el máximo observado
de 191 s, la recuperación esperada permanece por debajo del SLA de 10 minutos.

Estos cambios Terraform están implementados pero **todavía no se aplicaron por
Terraform**. Durante la promoción operativa autorizada se alinearon en vivo,
sin deletes/replaces, los valores de bajo riesgo: Cloud Tasks conserva 2
despachos/s, concurrencia 2 y cinco intentos, ahora con logging sampling 1.0;
Scheduler usa `*/6`, deadline 300 s y cero retries; el Run Job usa timeout 300 s
y cero retries. Su adopción en state y la comprobación de un plan sin drift
siguen pendientes del bootstrap gobernado pre-G1B; no se afirmará convergencia
Terraform hasta completar ese flujo.

El plan remoto tampoco se fabricó por fuera del flujo gobernado. Antes de G1B
falta una ruta publisher confiable/source-less que publique y escanee el
release-controller por digest; el recipe actual es deliberadamente
verify-only. La primera adopción debe mantener containers deshabilitados e
incluir la lectura de `environment-inputs/`; sólo después de un apply aprobado
puede producirse el plan managed. Saltar esta secuencia recrearía el límite de
confianza que el controlador bloquea.

## Polling n8n

El contrato sanitizado exige deadline absoluto, estado de intento durable,
máximo de intentos, backoff exponencial acotado y ramas explícitas para todos
los terminales, deadline y agotamiento. La espera base debe ser al menos 65 s
para que n8n descargue el estado de ejecución a la base; intervalos menores no
constituyen un checkpoint durable, según la documentación oficial del nodo
[Wait](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/).

El workflow importable no contiene credenciales, prompts, payloads ni PII. Su
importación, credenciales y activación en la instancia n8n real permanecen como
acción externa; no se afirmará que está desplegado sin esa evidencia.

## Verificación

Resultados frescos sobre el árbol final y el gate remoto:

- suite local no-live completa: **1502 passed, 16 skipped, 23 deselected**;
- la suite final incluye las regresiones de fixture/worker/repositorio P0,
  required-data fail-closed, reconciliación, controlador/IAM y n8n;
- Pinecone uploader final: **28 passed**; el conjunto Pinecone/privacidad
  también forma parte de la suite completa;
- Ruff, mypy configurado, `pip check`, `pip-audit`, secret baseline,
  collect-only, árbol Terraform fail-closed y `git diff --check`: pass;
- el upload default-deny de Cloud Build incluye explícitamente el único
  workflow n8n sanitizado requerido por las pruebas y los tres recipes CI lo
  incluyen en el secret scan externo fail-closed;
- Terraform 1.9.8 oficial, verificado por checksum: fmt/init/validate y tests
  platform **21 passed**, staging **1 passed**, production **0 tests / validate
  pass**, módulo **26 passed**;
- Firestore Emulator RPC local: no disponible (Docker no está instalado);
  Cloud Build ejecutó **16 passed** contra el Emulator real.

Cloud Build verify-only
`fe41ade9-1313-4413-9e27-e1e063b682f9` terminó `SUCCESS` sobre el árbol limpio
del commit `ba9c060ac9e7ced428b64aeb9b94fbb89b36de3e` (2026-08-03
16:23:10–16:29:41 UTC). Sus nueve pasos cubrieron Python 3.12, secret gates,
Ruff, mypy, `pip check`, `pip-audit`, Terraform 1.9.8, Firestore Emulator y
smokes de las imágenes runtime/CI/E2E/release-controller. Usó
`ticket-controller-verify@rag-kb-system.iam.gserviceaccount.com` y no publicó
imágenes, no desplegó, no ejecutó `terraform apply` y no escribió evidencia de
release.

El build diagnóstico anterior `e94b1116-bde2-4648-bb6d-aeee433990f5` también
terminó `SUCCESS` sobre `ae0a81d`, pero el resultado autoritativo de verificación
pre-merge es `fe41ade9…`. El build de `main`
`b77a08c7-aec5-4a49-8ec8-8b0f7fd0e910` aportó además el digest promovible,
SBOM, provenance y scan write-once del merge exacto. Los planes y applies
Terraform continúan perteneciendo al flujo gobernado y no se sustituyeron por
este build.

La verificación final añadió **1527 passed, 16 skipped, 23 deselected**, Ruff,
mypy, `pip check`, `pip-audit`, secrets y el build canónico `bb9dd27f…`
`SUCCESS`. Dos builds manuales `test-only` aprobaron todos esos gates y fallaron
únicamente en `revalidate-digest` por drift de permisos Artifact Registry de sus
SAs; no fueron artefactos de release. El digest se revalidó con la identidad
operadora y el pipeline canónico `ticket-ci` publicó/escaneó el artefacto final.

## Gates cumplidos

1. Corrección y hardening publicados, PR #14 integrado y `main` local/remota
   sincronizadas.
2. Cloud Build verify-only verde y build de `main` verde con digest inmutable,
   SBOM, provenance SLSA 3 y scan sin hallazgos.
3. Producer/worker/reconciliador corregidos en producción; canary, probes,
   smoke v2 idempotente y tick automático completados sin errores.
4. Queue y scheduler reanudados con la configuración operativa corregida.
5. Ocho efectos históricos confirmados como exitosos, con decisión de cero
   replays; dos falsos outages Pinecone reclasificados.
6. ForUsBots 2.6 desplegado con receipts/jobs durables, replay al mismo job,
   conflicto de payload y recuperación fail-closed verificados en vivo.
7. Cliente RAG idempotente desplegado; canary GR real terminal, promoción
   `full` al 100%, ejecución manual y tick automático del reconciliador verdes.

## Pendientes posteriores al cierre

1. Bootstrap publisher/controlador pre-G1B y planes Terraform revisados: cero
   delete/replace; adopción/apply sólo con quorum exacto.
2. Validar el workflow real desde n8n con sus credenciales existentes. El owner
   operativo no otorgó acceso administrativo y realizará esa prueba; el backend
   `full` ya está listo y verificado.
3. Migrar el origen HTTP legacy de ForUsBots a HTTPS/ingress privado. Es deuda
   de transporte, no de idempotencia; no requiere desactivar `full`.
4. Restaurar el permiso mínimo de lectura Artifact Registry para la SA manual
   de `test-only`, sin ampliar privilegios de deploy.
5. Mantener observación de ejecuciones reales y revertir a
   `kb-rag-system-incd60388bko` ante cualquier `INTERNAL_ERROR`, external ID
   ausente o discrepancia durable.

El resultado correcto es **remediación desplegada, verificada y operativa en
producción bajo `full`, con rollback `knowledge_only` preservado y la prueba
n8n real delegada a su operador**.
