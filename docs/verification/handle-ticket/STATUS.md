# Estado de ejecución del plan de finalización handle-ticket

Revisado el 2026-07-21 en el worktree aislado
`ForUsGuide-handle-ticket-finalization`, rama
`handle-ticket-production-finalization`. Este corte documental precede al
commit/push y al PR draft de cierre. El worktree original del usuario se
mantuvo intacto.

## Resultado ejecutivo

- La verificación remota Python 3.12/Linux del source anterior terminó
  **SUCCESS** en Cloud Build `5fe68b12-1381-4bb3-9b4f-594ca401fda0`. El árbol
  actual contiene un delta de seguridad posterior y aún no tiene build remoto
  propio con una identidad verifier segura.
- En ese source pre-delta, el gate remoto ejecutó **1296 passed, 16 skipped,
  23 deselected**; ruff,
  mypy (17 módulos), `pip check`, `pip-audit`, baseline de secretos git-less y
  scan fail-empty de inputs externos pasaron.
- Sobre el árbol actual, la suite local completa pasó **1341 passed, 16
  skipped, 23 deselected** de **1380** recolectados; ruff, mypy (17 módulos),
  `pip check`, `pip-audit` y ambos gates de secretos pasaron.
- Terraform 1.9.8 local pasó fmt/init/validate/test sobre el árbol actual:
  platform **22/22**, staging **1/1**, production validate (**0 tests**) y
  módulo **25/25**.
- En el source pre-delta, el emulador oficial de Firestore pasó **12/12**,
  incluida la carrera de 50
  callers que había fallado en el intento anterior.
- En ese mismo build, runtime, imagen CI, imagen E2E y release-controller se construyeron; los
  smokes sin red del runtime, E2E y controller pasaron. Ninguna imagen se
  publicó desde este build de verificación.
- Producción quedó byte a byte intacta: revisión
  `kb-rag-system-00048-bkc`, 100% de tráfico, `TICKET_HANDLER_MODE=disabled`.
- No hubo Terraform apply, deploy, cambio de IAM/tráfico/secrets/n8n, creación
  de runtime ni aprobación implícita de gates.

La frase correcta sigue siendo **hardening local verificado; rollout no
iniciado**. No se afirma que el plan completo ni su Definition of Done estén
cerrados.

## Hallazgos cerrados en la revisión del 2026-07-21

Además de las correcciones anteriores, se cerraron RED-first:

- contención Firestore: single-flight local por principal evita que una misma
  instancia agote los cinco retries nativos sobre receipt+counter; Firestore
  conserva la autoridad transaccional entre instancias;
- release-controller: `data`, imports, tipos resource, declassificación y
  lecturas de filesystem se validan fail-closed antes de `terraform init/plan`;
- `test-only`: candidate code corre sin red/ADC/metadata, no-root, filesystem
  read-only, sin capabilities ni workspace/Docker socket escribible;
- controller bootstrap: el camino P1 conocido quedó fail-closed; el recipe
  candidato sólo verifica, declara `ticket-controller-verify` y no contiene
  push/scan/images; ningún YAML/trigger referencia `ticket-controller-build` y
  `platform-apply` no puede hacer `actAs` de ninguna de esas dos SAs. Esto
  desactiva la ruta vulnerable, pero no crea un publisher bootstrap confiable;
- ForusBots: cancelar el último waiter antes del submit, incluida la ventana
  después de un error de transporte presend-safe, cancela el trabajo huérfano
  y no permite un POST/retry tardío; después del límite ambiguo se conserva la
  reconciliación fail-closed;
- circuit/ambigüedad ForusBots: el circuito se revalida dentro del semáforo
  inmediatamente antes del POST, por lo que corta el backlog al abrirse; HTTP
  408 de un POST se trata como submit ambiguo sin retry, y un puerto inválido
  no conserva el valor sensible en `__cause__`/traceback;
- privacidad ForusBots: warnings/errors/module errors/unknown fields se reducen
  a estados cerrados y conteos, también cuando el upstream los anida dentro de
  módulos reconocidos; no alcanzan prompts, checkpoints ni polling público;
- separación de roles: el producer no construye el cliente ForusBots ni recibe
  `FORUSBOTS_AUTH_TOKEN` ni su grant per-secret; conserva únicamente el
  `FORUSBOTS_BASE_URL` no secreto como parte del inventario core importado. El
  worker es el único consumidor declarado del cliente y del token. La SA
  candidata usa `ticket-producer-prod` con core mínimo y no puede volver a
  `kb-rag-runner` ni siquiera en dark. La SA legacy aún tiene autoridad amplia
  live y se trata como rollback/bloqueo de aplicación, no como una frontera ya
  desplegada;
- controller/IAM: el plan platform exige inventario runtime completo por
  entorno managed, acepta sólo los índices `actAs` scheduler/build reales,
  excluye verifier/publisher/legacy y fija el Cloud Tasks service agent al
  proyecto `900340137010`; un plan vacío/incompleto o cross-project falla
  cerrado;
- verificación remota: `ci/cloudbuild.verify-local.yaml` declara la SA exacta
  logging-only `ticket-controller-verify` y un contrato rechaza identidades
  default/legacy. Como la SA aún no existe live, el build actual queda
  fail-closed hasta PREP/G1B en vez de caer en `kb-rag-runner`;
- secretos: scan real `--all-files --no-verify`, baseline exacta de 66
  hallazgos revisados y scan vacío de PA/External agents/drill;
- API key del UI deja de persistirse en Web Storage;
- Google Cloud CLI/emulador usan pins 577.0.0 separados y resolubles;
- smokes de imágenes usan `/app` + `PYTHONPATH=/app` y E2E arranca como módulo;
- validator Terraform rechaza también `.tf.json`, override files, tfvars
  implícitos, imports no revisados y backends fuera de la allowlist.

## Estado por tarea

| Tarea | Estado real |
|---|---|
| 0 preflight/contención | completa; snapshot GCP, source allowlist de 241 archivos y worktree aislado |
| 1 contratos externos | inventario completo; **4 contratos externos siguen ausentes** |
| 2 regresiones RED | completa y ampliada por las revisiones adversariales |
| 3 imagen/locks | locks actuales; build/smokes remotos del source pre-delta completos; el árbol actual exige una nueva verificación remota con SA verifier segura |
| 4 auth/roles | código local fail-closed; activación bloqueada por participant-plan real |
| 5 Firestore/TTL/cuotas | código/IaC cerrado; 12/12 contra emulador real |
| 6 worker/fencing | cerrado localmente, incluida carrera de generación e intent externo durable |
| 7 Cloud Tasks/reconciler | cerrado localmente; staging real no ejecutado |
| 8 dependencias/probes | cerrado localmente y smoke de imagen pasado; probes live gateadas |
| 9 diferencial/contratos | arnés real ejecutable; smoke léxico no sustituye review semántica independiente |
| 10 Terraform | módulo/roots/locks/fmt/validate/tests verificados localmente con Terraform 1.9.8; ningún backend/apply |
| 11 observabilidad | métricas/alertas/dashboard/runbook locales; canales reales no aplicados |
| 12 CI/CD | **bloqueada**: el recipe candidato es verify-only y el publisher está inaccesible, pero faltan una ruta publisher confiable/source-less, digest+scan y bootstrap G1B; las SAs bootstrap aún no existen en GCP |
| 13–18 rollout | no iniciadas; bloqueadas por gates, contratos y artefactos de promoción |

## Evidencia Cloud Build

| Build | Resultado verificable |
|---|---|
| `3dcad9c8-a11d-4315-ab96-e9904c85e289` | SUCCESS histórico consultado por autorización del owner |
| `e3afaa76-2ca2-46a4-82e1-43d0d7ad4799` | FAILURE durante resolución de locks intermedia |
| `3269c077-f283-4213-afe3-dfd0fb3a3c80` | SUCCESS de locks intermedios |
| `6d76056b-a7ee-429c-abb1-f1f8abc7e5ce` | CANCELLED al detectar source equivocado; objeto inseguro borrado y confirmado 404 |
| `838fbbf4-8b85-4adb-89aa-fd8a616340e5` | SUCCESS; locks actuales descargados y verificados por hash |
| `5c10842b-e737-4e1c-bd3e-69436fe78a86` | FAILURE: source allowlist omitía PA/drill |
| `75959295-ef94-491b-8b4b-c78c9b5abc41` | FAILURE: detect-secrets dependía de Git en source empaquetado |
| `e8319d67-b610-4f27-a933-8d5c45f11c94` | FAILURE: Python/Terraform pasaron; Firestore 11/12 por lock timeout; imágenes quedaron sin ejecutar |
| `5fe68b12-1381-4bb3-9b4f-594ca401fda0` | **SUCCESS pre-delta**: los nueve pasos de aquel source pasaron; no prueba ningún delta posterior de controller, runtime, secretos o Terraform |

Source del build pre-delta: `gs://rag-kb-system_cloudbuild/source/1784663750.163419-553b24ebf0054d4eb6012780306ae436.tgz#1784663752419811`.
El objeto de source del intento mínimo fallido
`source/1784658440.790492-43ee2c7ce65d42b19071d752fa032903.tgz`
también fue borrado y confirmado 404.

## Contención de producción verificada

Los snapshots antes/después del build son idénticos (`cmp` byte a byte):

| Superficie | SHA-256 antes y después |
|---|---|
| trigger `deploy-kb-rag-system` | `d34c54f0bd3b2910bd10c784d97671734da506267726d0404a9fcdd0c998e44f` |
| IAM del proyecto | `d1e971d81e4def2df58c2c8c92b087249b71265c8c22d6350f821cfbd55153cd` |
| IAM de `kb-rag-system` | `74a8a8216ca78a389fc1473a8763add62e0d5a9114d0d88991d02185bcc292b0` |
| definición de `kb-rag-system` | `0b4949fe97fb7d75994510ac8f067e36b4cabc92936f8788180d176534749a9f` |

El trigger legacy `deploy-kb-rag-system` sigue activo y deploy-capable. No se
debe mergear a `main` antes de neutralizarlo mediante el plan binario aprobado
en G1B.

## Bloqueos reales

1. Contratos externos: participant-plan, ForUsBots HTTPS+idempotencia/lookup,
   export n8n real + WIF y entrega final idempotente.
2. Receipt `semantic_review` independiente e inmutable ligado a main SHA,
   image digest, rúbrica y hashes exactos del diferencial.
3. Resolver el bootstrap circular del release-controller: las SAs verifier y
   publisher aún no existen, G1B requiere previamente su digest y el árbol no
   contiene un publisher confiable/source-less. Sólo después se podrá publicar,
   escanear y ligar el digest inmutable antes de solicitar G1B.
4. Aplicar y probar la identidad productiva endurecida: hoy la revisión live
   usa `kb-rag-runner`, con `roles/secretmanager.secretAccessor` project-wide y
   un grant directo sobre `FORUSBOTS_AUTH_TOKEN`. El rollback legacy se preserva;
   una revisión candidata no puede activarse hasta usar la SA mínima nueva y
   demostrar con effective-IAM que producer=DENIED y worker=GRANTED sobre ese
   secreto.
5. Registrar cada aprobación real como `APROBADO <GATE> <ALCANCE>` en
   `approvals.md`; la tabla permanece deliberadamente vacía.
6. Ejecutar staging/producción sólo en el orden G1A–G10 del plan.

## Riesgos residuales documentados

- El validador HCL es conservador y propio; puede rechazar sintaxis válida
  nueva. La allowlist limita superficies pre-plan, pero no firma el cuerpo
  completo de cada resource.
- El build de verificación no es un sandbox criptográfico de la SA elegida: la
  configuración auditada no contiene mutaciones, pero la SA conserva sus
  permisos técnicos y red. La comparación antes/después demuestra que esta
  ejecución concreta no alteró las cuatro superficies críticas.
- El campo `serviceAccount` del YAML candidato sólo declara una identidad; un
  trigger usa su propia SA y un submitter manual autorizado puede seleccionar
  otra. El enforcement actual depende también de IAM, de excluir ambas SAs de
  `platform-apply actAs` y de que el publisher no aparezca en recipes/triggers.
- `ticket-controller-build` está modelada con writer+scan pero permanece sin
  una ruta de uso segura. Ejecutar el Dockerfile candidato bajo esa identidad
  recrearía el P1; se necesita publicación source-less desde un OCI en
  cuarentena con provenance de un SHA remoto completo.
- Sin contratos upstream no se puede resolver automáticamente un POST
  ForUsBots ambiguo; el sistema falla cerrado a reconciliación manual.
- El aislamiento de env/per-secret del código actual aún no revoca la autoridad
  efectiva del producer live: `kb-rag-runner` conserva grants Secret Manager
  amplios. Retirarlos a ciegas rompería el contrato de rollback/core; la
  contención correcta es una SA candidata separada y un probe IAM gateado.
- `runtime_vertex` se concede por fase de containers, no por el valor futuro de
  `USE_VERTEX_AI`. La configuración revisada usa `true`; si un contrato lo
  cambia a `false`, debe retirarse `aiplatform.user` y probarse el plan antes de
  aplicar.

Todos los cierres posteriores —controller verify-only, carreras/sanitización
ForusBots, separación de secretos y correcciones Terraform— son posteriores al
source de `5fe68b12…`. Tienen evidencia RED/GREEN y gates locales, pero aquel
build corrió como `kb-rag-runner`, no como `ticket-controller-verify`, y no los
prueba. Un nuevo build remoto debe usar una identidad verifier segura; por sí
solo tampoco resuelve la ruta de publicación ni desbloquea G1B.

## Definition of Done

**Abierta.** No existe staging activo ni el rollback anchor
`hardened-disabled` en producción, y no hay evidencia/aprobaciones para
G1A–G10. El resultado verificable de esta sesión es código endurecido con
pruebas integrales locales, un build remoto exitoso del source pre-delta y
producción intacta; no un rollout completo ni un bootstrap publicable.
