# Estado de remediación de handle-ticket

Revisado el 2026-08-03 en la rama
`codex/fix-ticket-execution-failures`.

## Resultado actual

El incidente del 2026-08-02 está **contenido**, y la remediación P0–P2 está
implementada y verificada localmente en un worktree limpio derivado de
`origin/main`. El checkout obsoleto `handle-ticket-hardening` no fue modificado.

La última observación autenticada de producción dejó el productor en la
revisión `kb-rag-system-00053-jmx`, `Ready=True`, con 100% del tráfico y
`TICKET_HANDLER_MODE=knowledge_only`. Conserva el mismo digest y service
account que la revisión anterior; `kb-rag-system-00052-gxw` permanece como
rollback técnico, pero restaurarla reabriría la ruta defectuosa.

El modo `full` continúa bloqueado. ForUsBots 2.5 no ofrece idempotency key ni
lookup por correlation ID y usa un origen HTTP legacy; un POST ambiguo no puede
reconciliarse de forma segura. La corrección evita reenvíos ciegos, pero no
inventa una garantía upstream inexistente.

## Verificación local fresca

- suite Python no-live: **1489 passed, 16 skipped, 23 deselected**;
- controlador/IAM/monitoring/Terraform contracts: **322 passed**;
- Terraform 1.9.8 oficial verificado por checksum: fmt/init/validate y tests
  platform **21 passed**, staging **1 passed**, production validado y módulo
  **26 passed**;
- Ruff, mypy configurado, `pip check`, `pip-audit`, secret baseline, secret
  scan de inputs externos y `git diff --check`: pass;
- Firestore Emulator RPC y los smokes de imágenes quedan para Cloud Build,
  porque Docker no está instalado localmente.

## Producción y efectos históricos

- Los ocho `INTERNAL_ERROR` se correlacionaron con ocho
  `job.succeeded` de ForUsBots; se decidió **cero replays**.
- Los dos falsos `PINECONE_TRANSIENT_FAILURE` se documentaron como bloqueos
  locales `UnsafeRetrievalQuery`, no outages del proveedor.
- No se aplicaron los cambios Terraform: Cloud Tasks live sigue sin logging y
  Scheduler mantiene la cadencia anterior hasta un plan remoto revisado con
  cero deletes/replaces.
- El workflow n8n acotado está sanitizado e importable, pero no está importado
  ni activado en la instancia efectiva.

## Estado de entrega

| Área | Estado |
|---|---|
| Firestore-safe diagnostics y validación durable | implementado y probado |
| Intent, external IDs, resume y reconciliación | implementado y probado |
| Pinecone, privacidad, logging y métricas de negocio | implementado y probado |
| Terraform/IAM/Cloud Tasks/reconciliador | implementado; apply pendiente |
| Polling n8n | artefacto listo; activación externa pendiente |
| Cloud Build autoritativo Python 3.12/Emulator/imágenes | pendiente |
| Commit/push/PR | pendiente |
| Canary y 20 GR consecutivos | bloqueado por gates upstream/live |

El reporte completo y los IDs de reconciliación sanitizados están en
`REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`.
