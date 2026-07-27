# 03 — Imagen completa y resolución reproducible (Tarea 3)

Estado revisado al 2026-07-21. Se siguieron `.agents/PINECONE.md` y
`.agents/PINECONE-python.md`: el runtime usa `pinecone` (no
`pinecone-client`), namespace explícito y retries/circuit breaker acotados.

## Artefactos reproducibles

| Artefacto | Evidencia |
|---|---|
| Base Python | `python:3.12-slim@sha256:64695412729fbe8cf054511723820c82bbe5a077d4a6b4070cd4a7225d3422ce` |
| Runtime lock | Linux/Python 3.12, hashes obligatorios, Pinecone 9.1.0; SHA-256 `f17b30f2a66305cdcb99bdf501693b02504eae99a531a5bc2ba9f04b4340c0d5` |
| Dev lock | runtime + pytest/ruff/mypy/pip-audit/detect-secrets; SHA-256 `ab751e0fcabe3bf500a21985e931f6ca6a0f5c73c358802ef44f18f0a907b198` |
| Provider locks | platform/staging/production, google + google-beta 5.45.2; cada archivo SHA-256 `d712e30625ab217e4dc7fe5ea3618e83d2e88755bf29b4a6bbcdfc48c43f45c8` |
| Google Cloud CLI | `gcr.io/google.com/cloudsdktool/google-cloud-cli:577.0.0@sha256:1ccec754a72b81280b047f476e92771afbef26b3072f3798937fc39101f7a60a` |
| Emulador Firestore | `gcr.io/google.com/cloudsdktool/google-cloud-cli:577.0.0-emulators@sha256:6e91e97e42d58ed28a42e1c660f0495fd7138eb48099daa4881ce8452947f651` |
| Terraform | `hashicorp/terraform:1.9.8@sha256:18f9986038bbaf02cf49db9c09261c778161c51dcc7fb7e355ae8938459428cd` |
| SBOM tool | Syft 1.46.0 por digest `473a60e3…20bb` |

El pin anterior del emulador `38132a268…` devolvía 404 y fue sustituido.
Los siete builders genéricos restantes también se alinearon con el único pin
canónico de `ci/tool-images.env`; un test impide nuevo drift.

## Locks Cloud Build

El build vigente de resolución y descarga de locks es
`838fbbf4-8b85-4adb-89aa-fd8a616340e5` (SUCCESS).
Los artefactos descargados coinciden byte a byte con los cinco locks del
worktree. `3269c077-f283-4213-afe3-dfd0fb3a3c80` fue un build intermedio
exitoso; `e3afaa76-2ca2-46a4-82e1-43d0d7ad4799` documenta el intento previo
fallido. Ninguno ejecutó deploy/apply.

## Gate remoto pre-delta Python 3.12/Linux

Build del source pre-delta: `5fe68b12-1381-4bb3-9b4f-594ca401fda0`
(**SUCCESS**), source
GCS generation `1784663752419811`.

| Paso | Resultado |
|---|---|
| `python-gates` | 1296 passed, 16 skipped, 23 deselected; ruff, mypy, pip check, pip-audit y secretos PASS |
| `terraform-gates` | fmt/validate PASS; platform 22, staging 1, production 0 y módulo 22 tests PASS |
| `firestore-emulator-contract` | imagen fijada contiene el componente oficial |
| `firestore-emulator-tests` | 12/12 PASS en 4.85 s |
| `container-build` + `container-smoke` | imagen runtime linux/amd64 construida y smoke sin red PASS |
| `ci-image-build` | imagen CI construida desde locks con hashes |
| `e2e-image-build-smoke` | imagen E2E construida; smoke `/app` sin red PASS |
| `release-controller-build-smoke` | controller con provider mirror construido; CLI `--help` sin red PASS |

El intento inmediatamente anterior,
`e8319d67-b610-4f27-a933-8d5c45f11c94`, terminó FAILURE después de que
Python/Terraform pasaran: Firestore obtuvo 11/12 y agotó cinco retries por
`Transaction lock timeout` en la carrera de 50 callers. Se corrigió con
single-flight local por principal, sin ampliar retries ni relajar la prueba,
y el build pre-delta exitoso demostró 12/12. Los cinco pasos de imagen no se
ejecutaron en el intento fallido.

## Alcance exacto

Este build construyó imágenes efímeras para verificarlas, pero no hizo push a
Artifact Registry, no generó SBOM/scan de promoción, no escribió evidence y no
desplegó. La publicación y el scan del runtime/E2E/release-controller siguen
siendo pasos de Tarea 12/G1B y no se sustituyen por este smoke.

Después de `5fe68b12…` se separó además el controller verifier del publisher:
el YAML candidato sólo contiene build/smoke local y declara la SA logging-only
`ticket-controller-verify`; no contiene push, scan ni `images:`. Esa declaración
no impone la identidad efectiva (la SA del trigger prevalece y un submitter
manual autorizado puede elegir otra). El publisher `ticket-controller-build`
no aparece en ningún recipe/trigger y `platform-apply` no puede asumir ninguna
de las dos identidades. El árbol posterior también corrige las ventanas de
cancelación/retry y la sanitización recursiva de ForusBots, elimina cliente/env
ForusBots del producer, declara el token por secreto sólo para el worker y
endurece expectativas Terraform de `actAs` e IAM runtime.

Estos deltas tienen evidencia RED/GREEN y gates integrales locales, pero no una
reconstrucción remota actual. `5fe68b12…` corrió como `kb-rag-runner`, no como
el verifier, y no los prueba. Además, ninguna de las dos
SAs existe todavía en el proyecto real: el apply que las crearía requiere el
digest del controller y no existe aún un publisher confiable/source-less para
producirlo. Una nueva verificación remota sólo es aceptable con una identidad
verifier segura; repetirla con `kb-rag-runner` no demuestra esa frontera y
mantiene permisos innecesarios. Tampoco resolvería la circularidad ni
habilitaría G1B.

## Gate local del árbol actual

Con el binario oficial Terraform 1.9.8 verificado se ejecutaron `fmt`,
`init -backend=false -lockfile=readonly`, `validate` y `test`: platform **22/22**,
staging **1/1**, production validate (**0 tests**) y módulo **25/25**. La suite
Python local recolectó **1380** casos y seleccionó 1357: **1341 passed, 16
skipped, 23 deselected**. Ruff, mypy, `pip check`, `pip-audit`, la baseline
exacta y el scan fail-empty externo pasaron.

Este gate local verifica código/HCL, no sustituye Python 3.12/Linux, el emulador
Firestore ni la reconstrucción/smoke de imágenes del árbol actual. Esas pruebas
remotas siguen bloqueadas hasta disponer de la identidad verifier segura. El
recipe ya declara exactamente `ticket-controller-verify` y no puede caer en
una SA default/legacy; al no existir todavía esa identidad en GCP, el fallo
pre-G1B es deliberado.

La configuración del build no contiene comandos de mutación de runtime, IAM,
state, secrets o tráfico. Como la SA elegida conserva permisos técnicos y red,
la contención se verificó además comparando snapshots GCP antes/después; los
cuatro hashes fueron idénticos (ver `STATUS.md`).
