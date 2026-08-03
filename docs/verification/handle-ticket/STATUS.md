# Estado de remediación de handle-ticket

Revisado el 2026-08-03 después del rollout productivo final.

## Resultado actual

El incidente del 2026-08-02 está **corregido, desplegado y habilitado en
producción bajo `full`**. La contención `knowledge_only` cumplió su función y
permanece disponible como rollback, pero ya no recibe tráfico.

El cierre requirió dos releases coordinados:

- ForUsBots PR [#5](https://github.com/ialvisti/ForUsBots/pull/5), merge
  `70befb4478369800919eaab5a78516cb13e870ea`, build
  `f9fcea3d-a3c9-4f9e-8a45-6a1d36016ddf` `SUCCESS` e imagen
  `sha256:f9cebd31f2eb63ecd21e556695ca0f86c03afd6071943726ca8451202f969d1f`;
- ForUsGuide PR [#17](https://github.com/ialvisti/ForUsGuide/pull/17), merge
  `d60388b413379a21e04e552c7edf5b7af25e6462`, build canónico
  `bb9dd27f-71b5-4667-a46b-35b28675e788` `SUCCESS` e imagen
  `sha256:eb2c3af9cae3200b6b2d433bd2879d42048beeac123077f1d861ec8569068aca`.

Estado efectivo:

| Superficie | Estado productivo |
|---|---|
| ForUsBots MIG | plantilla `forusbots-template-70befb4-f9cebd31f2eb`, estable, digest `f9cebd31…` |
| Producer | `kb-rag-system-fulld60388b`, 100%, `Ready=True`, `TICKET_HANDLER_MODE=full` |
| Worker | `kb-rag-ticket-worker-incd60388b`, 100%, `Ready=True`, modo `full` |
| Reconciliador | generación 6, digest `eb2c3af9…`, modo `full`, timeout 300 s, cero retries |
| Cloud Tasks | `RUNNING`, cola vacía, 2/s, concurrencia 2, cinco intentos, logging 1.0 |
| Scheduler | `ENABLED`, `*/6 * * * *`, deadline 300 s, cero retries |

Los rollback anchors siguen intactos: producer
`kb-rag-system-incd60388bko` (`knowledge_only`), producer histórico
`kb-rag-system-inc8055c2a`, worker `kb-rag-ticket-worker-inc8055c2a` y la
imagen previa del reconciliador.

## Verificación fresca

- ForUsBots local: **60 passed, 1 skipped**; lint **0 errores**; instalación y
  parse de OpenAPI/Cloud Build verdes; contrato Firestore real pasó con cleanup.
- ForUsBots live: submit/replay/mismo job/conflicto `409` pasaron para
  participant y plan; ambos jobs terminaron `succeeded`; receipts y jobs
  durables conservaron TTL, fingerprint y referencia consistente.
- RAG local y remoto: **1527 passed, 16 skipped, 23 deselected**; Ruff, mypy,
  `pip check`, `pip-audit`, secret gates y container smoke verdes.
- La build canónica de `main` produjo SBOM, provenance y scan sobre el digest
  exacto antes de la promoción.
- Producer candidato `knowledge_only`: 13/13 probes; base productiva: 5/5;
  candidato `full`: 5/5; después de promoción `full`: 20/20 `/readyz`.
- Smoke GR autenticado y sin publicación externa: aceptación y replay al mismo
  ticket job, payload cambiado `409`, ruta `generate_response`, job upstream
  durable, terminal `succeeded`, cero inquiry failures, cero reconciliación
  manual y `next_action=send_participant_reply`.
- Receipt upstream del smoke: `scrape-participant/succeeded`, TTL y fingerprint
  presentes. Producer/worker nuevos: cero logs severos, `INTERNAL_ERROR`,
  `DURABLE_STATE_FAILED` o conflictos idempotentes inesperados.
- Reconciliador generación 6: ejecución manual `Completed=True` y primer tick
  automático posterior al rollout `Completed=True`.

Dos builds diagnósticos `test-only` ejecutaron y aprobaron todos los gates de
aplicación, pero fallaron únicamente al revalidar el digest por drift de permisos
de lectura Artifact Registry de las SAs manuales. No participaron en el release;
el build canónico `bb9dd27f…` y la revalidación operadora fueron verdes. Restaurar
ese permiso mínimo es mantenimiento de CI, no un bloqueo productivo.

## Efectos históricos y pendientes

- Los ocho `INTERNAL_ERROR` históricos continúan correlacionados con ocho
  `job.succeeded` de ForUsBots; decisión inalterada: **cero replays**.
- Los dos falsos `PINECONE_TRANSIENT_FAILURE` siguen clasificados como bloqueos
  locales `UnsafeRetrievalQuery`, no outages de Pinecone.
- No se ejecutó `terraform apply`; queue/scheduler/job están alineados en vivo,
  pero su adopción/state continúa bajo el bootstrap gobernado pre-G1B.
- El workflow n8n acotado permanece sanitizado e importable. Su operador hará
  la prueba desde la instancia real; este rollout no requirió acceso
  administrativo a n8n ni publicó respuestas a participantes.
- ForUsBots aún usa el origen HTTP legacy exacto. La migración a HTTPS/ingress
  privado es una mejora de seguridad separada; la idempotencia durable ya está
  activa.

El reporte completo está en
`REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`.
