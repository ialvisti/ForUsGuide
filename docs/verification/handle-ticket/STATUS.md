# Estado de remediación de handle-ticket

Revisado el 2026-08-03 después del rollout productivo del PR #14.

## Resultado actual

El incidente del 2026-08-02 está **corregido y desplegado en producción bajo
`knowledge_only`**. El PR
[#14](https://github.com/ialvisti/ForUsGuide/pull/14) se integró en `main` como
`8055c2a2d4aaed283e043c9ff41a1b6d85d08d52`; `main` local y remota se
sincronizaron sin modificar el checkout obsoleto `handle-ticket-hardening`.

Producer `kb-rag-system-inc8055c2a`, worker
`kb-rag-ticket-worker-inc8055c2a` y reconciliador generación 5 usan el digest
`sha256:0711f1f55e5e38d9becbac77fa2853fb96996369b8ed4fb8d8f03bff28b6a9c4`.
Los dos servicios están `Ready=True` y al 100%; el producer conserva
`TICKET_HANDLER_MODE=knowledge_only`.

El modo `full` continúa bloqueado. ForUsBots 2.5 no ofrece idempotency key ni
lookup por correlation ID y usa un origen HTTP legacy; un POST ambiguo no puede
reconciliarse de forma segura. La corrección evita reenvíos ciegos, pero no
inventa una garantía upstream inexistente.

## Verificación local fresca

- suite Python no-live: **1502 passed, 16 skipped, 23 deselected**;
- controlador/IAM/monitoring/Terraform contracts: **322 passed**;
- Terraform 1.9.8 oficial verificado por checksum: fmt/init/validate y tests
  platform **21 passed**, staging **1 passed**, production validado y módulo
  **26 passed**;
- Ruff, mypy configurado, `pip check`, `pip-audit`, secret baseline, secret
  scan de inputs externos y `git diff --check`: pass;
- Cloud Build `fe41ade9-1313-4413-9e27-e1e063b682f9`: `SUCCESS` en los nueve
  pasos sobre `ba9c060…`; Emulator **16 passed**, Terraform y smokes de
  runtime/CI/E2E/release-controller verdes. Fue verify-only: no publish,
  deploy ni apply.
- Cloud Build de `main` `b77a08c7-aec5-4a49-8ec8-8b0f7fd0e910`:
  `SUCCESS`, source exacto `8055c2a…`, nueve pasos verdes, SLSA build level 3,
  SBOM write-once y scan sin vulnerabilidades ni excepciones.

## Producción y efectos históricos

- Los ocho `INTERNAL_ERROR` se correlacionaron con ocho
  `job.succeeded` de ForUsBots; se decidió **cero replays**.
- Los dos falsos `PINECONE_TRANSIENT_FAILURE` se documentaron como bloqueos
  locales `UnsafeRetrievalQuery`, no outages del proveedor.
- Cloud Tasks está `RUNNING`, vacío, 2/s, concurrencia 2, cinco intentos y
  logging sampling 1.0. Scheduler está `ENABLED`, cada 6 minutos, deadline 300
  s y cero retries. El reconciliador usa timeout 300 s y cero retries.
- Canary: 40/40 `/readyz` verdes, con respuestas atribuidas al candidato y
  cero errores; post-promoción: 20/20 verdes.
- Smoke funcional v2 sintético: replay idempotente, `queued → running →
  succeeded`, `error=none`, `next_action=send_participant_reply`.
- Reconciliación manual controlada `ticket-reconciler-prod-f2rmc` y primer
  tick automático `ticket-reconciler-prod-t8nmv`: `Completed=True` con el
  digest nuevo.
- No hubo `terraform apply`: los ajustes live quedaron alineados manualmente;
  la adopción/state y el plan sin drift siguen bloqueados pre-G1B.
- El workflow n8n acotado está sanitizado e importable, pero no está importado
  ni activado en la instancia efectiva.

## Estado de entrega

| Área | Estado |
|---|---|
| Firestore-safe diagnostics y validación durable | implementado y probado |
| Intent, external IDs, resume y reconciliación | implementado y probado |
| Pinecone, privacidad, logging y métricas de negocio | implementado y probado |
| Runtime producer/worker/reconciliador | desplegado y verificado al digest `0711f1f…` |
| Cloud Tasks/Scheduler | configuración correctiva live; adopción Terraform pendiente pre-G1B |
| Polling n8n | artefacto listo; activación externa pendiente |
| Cloud Build verify + release de `main` | `SUCCESS` (`fe41ade9…`, `b77a08c7…`) |
| GitHub/main local y remoto | PR #14 integrado; `8055c2a…` sincronizado |
| Canary knowledge-only | completado; smoke funcional `succeeded` |
| Canary y 20 GR consecutivos | bloqueado por idempotencia upstream; `full` no habilitado |

El reporte completo y los IDs de reconciliación sanitizados están en
`REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`.
