# Remediación del incidente de ejecuciones GCP

**Fecha:** 2026-08-02
**Última actualización:** 2026-08-03
**Proyecto:** `rag-kb-system`
**Región:** `us-central1`
**Documento de origen:** `AUDITORIA_EJECUCIONES_GCP_2026-08-02.md`
**Rama de trabajo:** `codex/fix-ticket-execution-failures`
**Commits de código:** `ae0a81d031dcb0d3cae7032e32ed74c2ef14103f`,
`ba9c060ac9e7ced428b64aeb9b94fbb89b36de3e`

## Estado ejecutivo

El incidente quedó **contenido en producción** y la corrección integral está
implementada en una rama limpia derivada de `origin/main`. No se usó ni se
alteró el checkout obsoleto `handle-ticket-hardening`, que conserva cambios del
usuario.

La rama y ambos commits están publicados en
`origin/codex/fix-ticket-execution-failures`. Una auditoría independiente no
encontró P0 residuales y los dos P1 y el P2 que detectó quedaron corregidos con
pruebas RED→GREEN antes del gate remoto final.

La ruta `generate_response` no debe volver a activarse en producción hasta
completar los gates live indicados al final de este documento. La razón no es
ya el defecto Firestore —corregido y cubierto— sino que ForUsBots 2.5 no ofrece
idempotency key ni lookup por correlation ID para resolver un POST ambiguo. Por
ello, el modo `full` permanece bloqueado; no se afirmará una finalización live
que todavía no existe.

## Contención productiva

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

El `dedupe_scope` evita coalescing entre tickets o tenants dentro del proceso,
pero **no se envía como header upstream**, porque el contrato ForUsBots 2.5 no
define uno. Esta limitación externa es el motivo para no habilitar `full`.

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

Estos cambios Terraform están implementados pero **todavía no aplicados**. La
lectura live de 2026-08-03 confirmó que `ticket-jobs-prod` sigue `RUNNING`, con
2 despachos/s, concurrencia 2, cinco intentos y sin logging; Scheduler sigue
habilitado cada minuto con deadline 180 s. No se mutarán hasta que el plan
remoto exacto confirme cero deletes/replaces y satisfaga los quorums del
controlador.

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
terminó `SUCCESS` sobre `ae0a81d`, pero el resultado autoritativo para el código
final es `fe41ade9…`. Digest promovible, SBOM/provenance, scan y planes remotos
pertenecen al build de release y a los triggers Terraform gobernados; no son
salidas del recipe verify-only y siguen correctamente pendientes.

## Gates cumplidos

1. Corrección y hardening publicados en los commits remotos exactos indicados.
2. Cloud Build verify-only verde sobre el commit final, incluido Emulator,
   Terraform y smokes de contenedores.
3. Contención live revalidada saludable y sin tráfico en la revisión `full`
   anterior.
4. Ocho efectos históricos confirmados como exitosos, con decisión de cero
   replays; dos falsos outages Pinecone reclasificados.

## Gates live pendientes y criterio de cierre

1. Build de release gobernado con digest inmutable, SBOM/provenance y scan;
   primero debe cerrarse el bootstrap publisher pre-G1B documentado.
2. Planes Terraform revisados: cero delete/replace; apply sólo con quorum exacto.
3. Revisión producer/worker corregida sin tráfico y probes sintéticos sin PII.
4. Importar/validar/activar el workflow n8n sanitizado en su instancia.
5. Contrato upstream ForUsBots HTTPS/privado con idempotencia o lookup de
   reconciliación; mientras falte, GR permanece legacy/knowledge-only.
6. Sólo después: canary, paridad 1:1 de evento terminal/métrica y al menos 20
   GR consecutivos seguros. Cualquier `INTERNAL_ERROR`, ID externo ausente o
   discrepancia de métricas aborta y revierte.

Hasta completar esos gates, el resultado correcto es **incidente contenido,
corrección implementada, rollout full bloqueado de forma segura**.
