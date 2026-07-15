# 03 — Imagen completa y resolución reproducible (Tarea 3)

Estado revisado al 2026-07-15. Se siguieron
`.agents/PINECONE.md` y `.agents/PINECONE-python.md`: el runtime usa
`pinecone` (no `pinecone-client`), namespace explícito y retries acotados.

## Artefactos reproducibles cerrados

| Artefacto | Evidencia |
|---|---|
| Base Python | `python:3.12-slim@sha256:64695412729fbe8cf054511723820c82bbe5a077d4a6b4070cd4a7225d3422ce` |
| Runtime lock | Linux/Python 3.12, hashes obligatorios, Pinecone 9.1.0; SHA-256 `4b03c260687a15715b1c50248573fd2e77ba06c2441dc188f19b345bdff6dafb` |
| Dev lock | runtime + pytest/ruff/mypy/pip-audit/detect-secrets; SHA-256 `625dec99a9e2dc2e8396467fbe931ab6c610ef41ddd2d67e0d82ce28fc5a243a` |
| Provider locks | platform/staging/production, google + google-beta 5.45.2; cada archivo SHA-256 `e44069405ae4a611e1d6ae500c42210ad6f5272e8e5b8426b04b8675ac8ed44e` |
| Dockerfile runtime | base por digest, `--require-hashes`, usuario no-root y prompts incluidos |
| Emulador | `gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators@sha256:38132a268745db5a1dc2ebfecfe6f935d75de281dddc6922f0fe3780c5552b81` |
| SBOM | Syft 1.46.0 por digest `473a60e3…20bb` |

## Builds Cloud Build ya ejecutados (sin deploy/apply)

| Build ID | Resultado |
|---|---|
| `6c5b340d-338e-407d-a81e-a03df2d5eb58` | SUCCESS: compiló ambos locks Python 3.12 y ejecutó `pip check` |
| `5716e603-bee8-4655-8255-0a01cc431864` | SUCCESS del resolver, pero evidencia **rechazada**: Cloud Build aplanó tres nombres `.terraform.lock.hcl` y publicó sólo uno |
| `a9ccc924-68a2-48af-87da-53a55ce9fff9` | SUCCESS tras RED→GREEN del controller: publicó tres locks con nombres únicos; los hashes locales coinciden byte por byte |

Los artefactos viven bajo el bucket Cloud Build existente. Ninguno de estos
builds publicó una imagen, ejecutó Terraform apply, tocó runtime/tráfico/IAM
ni usó un gate de rollout.

## Verificación local integrada

- Suite completa: **717 passed, 18 skipped** de 735 tests.
- Selección CI: **717 passed, 15 skipped, 3 deselected**.
- Los 11 skips del repositorio Firestore corresponden al emulador real.
- Los skips live corresponden a ForusBots/Pinecone/staging y a los contratos
  externos aún inexistentes; no se fabricaron fixtures.
- Todos los Cloud Build YAML parsean y sus scripts embebidos pasan
  `bash/sh -n`; `compileall` y `git diff --check` pasan.

El Python local es 3.14 y por eso esta suite es bootstrap, no el gate
autoritativo del plan.

## Gate remoto Python 3.12/Linux pendiente

`ci/cloudbuild.verify-local.yaml` está preparado y probado estáticamente para:

1. instalar el dev lock con hashes;
2. ejecutar pytest/collect-only, ruff, mypy, pip check, pip-audit y
   detect-secrets fail-closed;
3. ejecutar fmt/validate/test de los tres roots Terraform con locks readonly;
4. verificar el componente del emulador y correr sus 11 tests reales;
5. construir la imagen linux/amd64 y ejecutar `container_smoke.py`.

El controller no contiene publicación, `terraform apply`, deploy, escritura
de evidencia ni cambio de runtime. El submit final todavía no obtuvo build ID:
la política de reautenticación volvió a invalidar el token. Se abrió
`gcloud auth login --update-adc`, pero el callback no llegó durante más de 20
minutos y el listener se canceló limpiamente. Debe completarse el comando en
una terminal interactiva antes de reintentar. No se cambia de cuenta ni se usa
una key como atajo.
