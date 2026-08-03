# Revisión post-merge de producción

Fecha: 2026-07-27  
Proyecto: `rag-kb-system` / `us-central1`

## Estado confirmado

- Runtime commit desplegado y contenido en `main`:
  `83965cb0b52cf6b69f96d3bc25830395dd544678` (PR #12).
- Cloud Build: `f3b850eb-b2e6-4235-b964-345b2def90bd`, `SUCCESS`.
- Imagen: `us-central1-docker.pkg.dev/rag-kb-system/kb-rag/kb-rag-system@sha256:fc84c5412e5cb7908e6aded51975f905cb925451af4fe505826bf566ef7f4c0b`.
- Evidencias write-once: SBOM, vulnerabilidades y política de escaneo bajo `runtime/83965cb0b52cf6b69f96d3bc25830395dd544678/`; el reporte no contiene vulnerabilidades ni excepciones.
- Suite local y Cloud Build: 1.359 pruebas pasaron; ruff, mypy, pip check, container smoke y los nueve pasos del build pasaron.
- Terraform production: plan posterior al apply con exit code `0` y `No changes`.

## Runtime productivo

- Producer `kb-rag-system-00052-gxw`: 100% del tráfico, `TICKET_HANDLER_MODE=full`, `/readyz` 200.
- Worker `kb-rag-ticket-worker-00004-zf8`: Ready, `TICKET_HANDLER_MODE=full`.
- Reconciler `ticket-reconciler-prod`: Ready, `TICKET_HANDLER_MODE=full`; una ejecución programada ya completó con `exit(0)` y cero errores.
- Queue `ticket-jobs-prod`: `RUNNING`, sin tasks pendientes después del smoke.
- Scheduler `ticket-reconciler-prod-tick`: `ENABLED`, cron cada minuto en esta
  observación histórica. El contrato correctivo posterior declara `*/6`,
  timeout de 300 s y cero retries; no considerarlo live hasta un apply aprobado.
- IAM verificado: `kb-rag-client` invoca producer; `ticket-task-signer-prod` invoca worker; `ticket-scheduler-prod` invoca reconciler.

Se conserva el contrato existente de n8n: Cloud Run IAM + `X-API-Key`, sin cuentas/keys AWS y sin WIF adicional. El directorio participant-plan es opcional; si algún día se configura, su health check sigue siendo fail-closed. También se conserva el origen legacy documentado de ForUsBots.

## Smoke de producción

Se envió sólo información sintética por `POST /api/v1/handle-ticket`.

- Primera respuesta: 202.
- Replay con la misma `Idempotency-Key`: mismo job, sin duplicado.
- Estado terminal: `succeeded`.
- `next_action`: `send_participant_reply`.
- La cola regresó a cero.
- No se observaron respuestas 4xx/5xx del worker en la ventana del smoke.

## Desviaciones conocidas

La queue, el scheduler, su rol custom/scoped IAM y los bindings `roles/run.invoker` de signer/scheduler fueron bootstrap manual y no forman parte del state del módulo production. No eliminarlos durante una revisión de drift; primero incorporarlos declarativamente en un cambio separado.

El rollback anchor `kb-rag-system-00048-bkc` se conserva. Ante una incidencia: pausar queue y scheduler, aplicar `release_phase=dark_no_traffic` con ese baseline y el digest aprobado, verificar tráfico/readiness y sólo después investigar.

## Checklist para el siguiente chat

1. Confirmar que `main` contiene el runtime commit y que build, digest, revisiones, modos y tráfico coinciden con este documento.
2. Confirmar queue `RUNNING`, scheduler `ENABLED`, últimas ejecuciones del reconciliador exitosas y backlog estable.
3. Repetir `/readyz` autenticado y revisar errores recientes de producer/worker/reconciler sin imprimir secretos ni PII.
4. Ejecutar Terraform plan con las mismas variables firmadas y exigir exit code `0`.
5. Revisar alertas/dashboard y la desviación manual indicada; no mutar Pinecone, secretos ni contratos n8n/ForUsBots durante una revisión de sólo lectura.
