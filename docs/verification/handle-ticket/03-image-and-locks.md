# 03 — Imagen completa y resolución reproducible (Tarea 3)

Estado al 2026-07-14. Guías `.agents/PINECONE.md` y `.agents/PINECONE-python.md`
releídas antes de tocar dependencias (paquete `pinecone`, nunca `pinecone-client`;
retry sólo 429/5xx; namespace explícito obligatorio).

## Completado en este commit (3a)

| Paso | Resultado |
|---|---|
| 1. `.dockerignore` | `!data_pipeline/agent_prompts/` + `!data_pipeline/agent_prompts/*.md` añadidos tras la regla `*.md`; el test RED `test_dockerignore_ships_agent_prompts` ahora pasa |
| 2. Split de dependencias | `requirements.in` (runtime, incl. `google-auth` explícito para OIDC/WIF) y `requirements-dev.in` (`-r requirements.in` + pytest/pip-tools/ruff/mypy/pip-audit/detect-secrets) |
| 2. `pyproject.toml` | target py312; `ruff`/`mypy --strict` con scope explícito de módulos ticket/API; `ignore_missing_imports` sólo para SDKs sin stubs; `ignore_errors` prohibido |
| 2. `.secrets.baseline` | detect-secrets **1.5.0** (instalado fijado en el venv del worktree, no en el host); scan del árbol completo (excl. `.venv`, `__pycache__`, resultados de stress): **0 hallazgos**; revisado — no hay secretos reales marcados como falsos positivos |
| 2. `ci/tool-images.env` | Digests inmutables resueltos read-only contra los registries públicos (abajo) |
| 5. `scripts/container_smoke.py` | importa la app, verifica los 5 prompts + builders y los modelos del contrato; salida única sanitizada `container-smoke: ok`; exit != 0 ante ausencia |

### Digests registrados (resueltos el 2026-07-14, sin Docker local)

- `PYTHON_BASE_IMAGE=python:3.12-slim@sha256:64695412729fbe8cf054511723820c82bbe5a077d4a6b4070cd4a7225d3422ce` (Docker Hub manifest list)
- `FIRESTORE_EMULATOR_IMAGE=gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators@sha256:38132a268745db5a1dc2ebfecfe6f935d75de281dddc6922f0fe3780c5552b81` (gcr.io público, token anónimo)
- `SYFT_IMAGE=anchore/syft:v1.46.0@sha256:473a60e3a58e29aca3aedb3e99e787bb4ef273917e44d10fcbea4330a07320bb`

## DIFERIDO — bloqueado por STOP de autenticación gcloud (2026-07-14)

La sesión gcloud (`ivan.alvis@forusall.com`) funcionó durante la Tarea 0
(2026-07-13) y **expiró a mitad de ejecución** (`print-access-token` falla con
reauth de la política org). Regla del plan (Tarea 0 Paso 4): STOP para pasos
GCP; no se usa otra cuenta ni una key. Docker/Terraform no existen localmente,
así que la vía Cloud Build también queda bloqueada hasta que el usuario ejecute
`gcloud auth login --update-adc` en una terminal interactiva.

Pendiente para el commit 3b (Cloud Build sin permisos de deploy, imágenes
fijadas por los digests de `ci/tool-images.env`):

1. `pip index versions pinecone` dentro de `python:3.12-slim@sha256:6469…22ce`
   y ajuste del rango si el SDK actual difiere (`requirements.in` hoy fija
   `pinecone>=7.0.0,<8` conforme a la guía).
2. Compilar `requirements.lock` y `requirements-dev.lock` con
   `pip-compile --generate-hashes` (resolver pinneado en comentario de cabecera
   de cada lock), Linux/Python 3.12.
3. Reemplazar `requirements.txt` por los `.in`+locks y cambiar el Dockerfile a
   `FROM python:3.12-slim@sha256:…` + `pip install --no-cache-dir
   --require-hashes -r requirements.lock` (sin `--upgrade pip` sin versión).
4. `docker build --platform=linux/amd64` + `docker run … scripts/container_smoke.py`
   → esperado `container-smoke: ok`, exit 0; registrar build ID, digests de
   builders y logs sanitizados.
5. `pytest -q -rs` con `requirements-dev.lock` en el builder Python 3.12
   (gate autoritativo de suite; el venv local 3.14 es sólo bootstrap).
6. Verificación del componente del emulador dentro de `FIRESTORE_EMULATOR_IMAGE`
   (`gcloud components list --filter='id:cloud-firestore-emulator AND
   state.name:Installed'`).
7. `pip check` + `pip-audit` contra el lock: CRITICAL bloquea sin excepción;
   cada HIGH exige G5V por digest+CVE.

Ninguno de estos pasos se simula ni se marca como aprobado: los tests que
dependen de la imagen (smoke real, emulador de Tarea 5 Paso 4) permanecen
pendientes hasta ejecutarse de verdad.
