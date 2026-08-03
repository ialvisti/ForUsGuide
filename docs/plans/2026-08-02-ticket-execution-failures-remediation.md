# Ticket Execution Failures Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminar los fallos post-ForUsBots de `generate_response`, hacer reanudables e idempotentes sus efectos, corregir la clasificación/observabilidad y desplegar la remediación P0–P2 con evidencia live.

**Architecture:** Un validador Firestore Standard puro y reutilizable protege tanto los diagnósticos previos al efecto externo como los documentos completos justo antes de cada escritura. El worker divide explícitamente las fases de ejecución y el repositorio conserva por merge el intent y cada ID externo observado inmediatamente después del submit; la clasificación distingue rechazo local de privacidad, fallo Pinecone transitorio y fallo no reintentable. Terraform adopta los recursos live mediante import, mantiene imágenes por digest y configura métricas, alertas, Cloud Tasks y el reconciliador sin recreaciones.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pytest/pytest-asyncio, Firestore Native/Emulator, Cloud Tasks, Cloud Run/Cloud Build, Terraform Google provider y n8n JSON.

---

### Task 1: Freeze live evidence and deployment inputs

**Files:**
- Create: `REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`
- Modify later: `docs/verification/handle-ticket/STATUS.md`

**Step 1:** Capture sanitized read-only JSON for producer/worker revisions and traffic, reconciler executions, queue retry/log settings, Scheduler cadence, Firestore edition, log metrics, alert policies and recent builds.

**Step 2:** Correlate only Job ID, Trace ID, timestamps, terminal code, external-ID presence and safe phase metadata for the eight `INTERNAL_ERROR` and two unsafe-query jobs. Never read or print request/result bodies.

**Step 3:** Record the current rollback revisions, deployed digest, current remote commit and the exact Terraform state/backend prerequisites.

**Step 4:** Stop any later apply if a reviewed plan contains delete/replace operations.

### Task 2: Reproduce the nested-array defect (RED)

**Files:**
- Modify: `kb-rag-system/tests/test_ticket_orchestrator.py`
- Modify: `kb-rag-system/tests/test_ticket_worker.py`
- Modify: `kb-rag-system/tests/integration/test_firestore_ticket_repository.py`

**Step 1:** Add a realistic GR fixture with deterministic mappings, source articles and 21 synthetic chunks containing no identity data.

**Step 2:** Assert that `_map_fields` returns `[{"module": ..., "field": ...}]` for every deterministic slug. Run the single test and confirm it fails because HEAD returns nested lists.

**Step 3:** Add a validator test asserting the old shape is rejected with sanitized structural statistics. Run it and confirm failure because the validator does not exist.

**Step 4:** Add a worker/repository test proving a final checkpoint cannot erase `forusbots_submit_intent` or a previously persisted external ID. Run and confirm the existing replacement behavior fails.

**Step 5:** Add Emulator assertions: old shape rejected before RPC; new full GR checkpoint persists and re-reads intact.

### Task 3: Add Firestore Standard durable validation (GREEN)

**Files:**
- Create: `kb-rag-system/data_pipeline/durable_document.py`
- Modify: `kb-rag-system/data_pipeline/ticket_orchestrator.py`
- Modify: `kb-rag-system/data_pipeline/ticket_job_repository.py`
- Modify: `kb-rag-system/api/ticket_worker.py`

**Step 1:** Implement `DurableDocumentStats` and `DurableDocumentValidationError` with a recursive walker that rejects directly nested arrays, unsupported/non-finite values, depth greater than 20 and estimated document size above a conservative Firestore limit.

**Step 2:** Return only size, maximum depth, invalid-array count and bounded reason codes; never include values or field paths in exception text.

**Step 3:** Change deterministic mappings to lists of `{module, field}` objects.

**Step 4:** Validate diagnostics after field mapping and before invoking the ForUsBots intent guard.

**Step 5:** Validate the converted entry in the worker and both complete control/payload documents in the repository transaction immediately before `view.set`.

**Step 6:** Run the focused tests and the relevant suite; keep the RED output and GREEN output in the final report.

### Task 4: Preserve intent and external IDs across failures

**Files:**
- Modify: `kb-rag-system/data_pipeline/forusbots_client.py`
- Modify: `kb-rag-system/data_pipeline/ticket_orchestrator.py`
- Modify: `kb-rag-system/data_pipeline/ticket_job_repository.py`
- Modify: `kb-rag-system/api/ticket_worker.py`
- Modify: `kb-rag-system/tests/test_forusbots_client.py`
- Modify: `kb-rag-system/tests/test_ticket_job_repository.py`
- Modify: `kb-rag-system/tests/test_ticket_worker.py`

**Step 1:** Add tests for a submit observer called immediately after a valid `jobId` is parsed and before the first poll.

**Step 2:** Add `record_forusbots_external_job` as a fenced transaction that appends/deduplicates `{kind, job_id}` and top-level reconciliation IDs without removing the submit intent.

**Step 3:** Merge immutable checkpoint fields (`forusbots_submit_intent`, intent epoch and external jobs/IDs) when `record_inquiry_result` replaces the mutable outcome.

**Step 4:** Install worker-owned intent/submitted hooks on the orchestrator and
use a stable job/inquiry scope for in-process deduplication. Send that scope
upstream only if a reviewed ForUsBots contract defines an idempotency header;
otherwise keep ambiguous POSTs fail-closed and block the `full` rollout.

**Step 5:** Inject a failure after submit-observer success and before final checkpoint; prove resume does not submit again and retains the ID.

**Step 6:** Treat an observer/checkpoint failure after submit as reconciliation-required and never log the raw ID or exception message.

### Task 5: Split worker phases and sanitize failure telemetry

**Files:**
- Modify: `kb-rag-system/api/ticket_worker.py`
- Modify: `kb-rag-system/api/metrics.py`
- Modify: `kb-rag-system/tests/test_ticket_worker.py`
- Modify: `kb-rag-system/tests/test_metrics_security.py`
- Modify: `kb-rag-system/tests/test_runtime_log_privacy.py`

**Step 1:** Add RED tests that observe the ordered phases `handle_inquiry`, `convert_outcome`, `validate_durable_document`, `persist_inquiry_result`, `mark_terminal`.

**Step 2:** Extract phase helpers without changing public behavior and attach the current phase to a sanitized error event.

**Step 3:** Allow only reviewed exception classes and gRPC codes; compute a stable fingerprint from structural metadata, never from messages or ticket content.

**Step 4:** Emit exactly phase, sanitized type/code, fingerprint, size, depth and invalid-array count; verify sentinel PII and raw exception messages never appear.

### Task 6: Correct unsafe retrieval and Pinecone classification

**Files:**
- Modify: `kb-rag-system/data_pipeline/inquiry_router.py`
- Modify: `kb-rag-system/data_pipeline/pinecone_uploader.py`
- Modify: `kb-rag-system/api/main.py`
- Modify: `kb-rag-system/data_pipeline/ticket_job_models.py`
- Modify: `kb-rag-system/api/ticket_worker.py`
- Modify: `kb-rag-system/api/metrics.py`
- Modify: `kb-rag-system/tests/test_api.py`
- Modify: `kb-rag-system/tests/test_inquiry_router.py`
- Modify: `kb-rag-system/tests/test_ticket_worker.py`

**Step 1:** Add RED tests for `UnsafeRetrievalQuery`, transient timeout/transport/429/5xx/circuit and non-transient Pinecone 4xx.

**Step 2:** Add a deterministic `blocked` coverage status and public `UNSAFE_RETRIEVAL_QUERY` code, non-retryable and routed to legacy/human.

**Step 3:** Carry a typed transient flag from `PineconeUploader`; reserve `PINECONE_TRANSIENT_FAILURE` only for the allowed transient classes.

**Step 4:** Verify the unsafe branch emits no Pinecone availability/retry metric.

### Task 7: Canonicalize logs and business monitoring

**Files:**
- Modify: `kb-rag-system/api/main.py`
- Modify: `kb-rag-system/api/metrics.py`
- Modify: `infra/terraform/modules/ticket_environment/monitoring.tf`
- Modify: `kb-rag-system/tests/test_metrics_security.py`
- Modify: `kb-rag-system/tests/test_monitoring_contract.py`

**Step 1:** Add a test proving production logging installs only one canonical handler/output path.

**Step 2:** Restrict every log metric to that canonical representation and add positive-value filters to reconciler errors, fenced leases and deadline terminalizations.

**Step 3:** Add explicit counters/alerts for `failed`, `partial`, `INTERNAL_ERROR`, manual reconciliation and active jobs beyond the documented SLA; retain HTTP 5xx as a secondary signal.

**Step 4:** In a controlled live window, compare canonical terminal log events with Monitoring points and require a 1:1 ratio.

### Task 8: Adopt Cloud Tasks/Scheduler safely and remove reconciler drift

**Files:**
- Modify: `infra/terraform/live/platform/environment_containers.tf`
- Modify: `infra/terraform/live/platform/variables.tf`
- Modify: `infra/terraform/live/platform/imports.tf`
- Modify: `kb-rag-system/data_pipeline/ticket_job_repository.py`
- Modify: `kb-rag-system/tests/test_ticket_reconciler.py`
- Modify: `kb-rag-system/tests/test_terraform_runtime_contract.py`

**Step 1:** Add import blocks for the pre-existing queues and schedulers before enabling their managed phase.

**Step 2:** Configure Cloud Tasks logging with a bounded sampling ratio and no payload logging.

**Step 3:** Replace deprecated positional Firestore filters with `FieldFilter` and `filter=`.

**Step 4:** Set the reconciler to `*/6 * * * *`, task timeout `300s` and `max_retries = 0`: 360 s leaves 60 s beyond the timeout, the next tick is the only recovery and the recovery SLA remains ≤10 min. Update the out-of-SLA alert consistently.

**Step 5:** Run `terraform fmt`, `terraform init`, `terraform validate`, `terraform test` and reviewed platform/production plans. Do not apply any plan with delete/replace actions.

### Task 9: Bound n8n polling

**Files:**
- Modify: `kb-rag-system/tests/fixtures/n8n_handle_ticket_polling.json`
- Modify: `kb-rag-system/tests/test_handle_ticket_contract.py`
- Create only if sanitizable: `flows_n8n/bounded_ticket_polling.json`

**Step 1:** Add RED contract checks for absolute deadline, maximum attempts, capped exponential backoff, durable attempt state and explicit terminal/deadline/exhausted branches.

**Step 2:** Update the sanitized contract fixture and create a prompt/payload-free importable subworkflow only if it contains no credentials, prompts, responses or PII.

**Step 3:** Validate JSON and graph connectivity; if the effective n8n instance/export is unavailable, document the exact import/activation action as an external blocker rather than claiming deployment.

### Task 10: Verify, build and deploy progressively

**Files:**
- Modify: `REMEDIACION_EJECUCIONES_GCP_2026-08-02.md`

**Estado 2026-08-03:** Steps 1–2 completados; commit/push completados en
`ba9c060ac9e7ced428b64aeb9b94fbb89b36de3e`; PR pendiente de autenticación
GitHub. El verify-only autoritativo `fe41ade9-1313-4413-9e27-e1e063b682f9`
terminó `SUCCESS`. No produce digest/SBOM/provenance/scan ni planes: esos
pertenecen al release gobernado. Step 8 completado con cero replays. Steps 5–7
siguen bloqueados por bootstrap/quorums, activación n8n y contrato upstream.

**Step 1:** Run focused tests, complete non-live suite, Ruff, strict mypy, config/secret scans and container contract tests in the locked Python 3.12 Cloud Build environment.

**Step 2:** Run Firestore Emulator integration proving old rejection and new round-trip.

**Step 3:** Commit verified source, push the branch and create a PR when authentication allows.

**Step 4:** Run Cloud Build; record build ID, source commit, immutable digest, SBOM/provenance gates and scan results.

**Step 5:** Produce and review Terraform plans. Stop before apply on any
delete/replace or unmet quorum. Deploy a no-traffic worker/producer revision
pinned to the new digest only if the upstream idempotency/reconciliation gate
is satisfied; otherwise preserve the production containment and document the
blocked rollout.

**Step 6:** Execute synthetic no-PII checks, then a small canary. Abort/rollback on any `INTERNAL_ERROR`, duplicated terminal metric, missing external ID or unsafe checkpoint.

**Step 7:** Observe at least 20 consecutive safe GR terminals, require event/metric parity, then perform full rollout only if every gate passes.

**Step 8:** Reconcile the eight historical jobs by safe metadata with ForUsBots; do not replay. Record each as confirmed-effect, confirmed-no-effect, or awaiting external lookup. Record the two unsafe-query jobs as classification corrections, not Pinecone outages.

**Step 9:** Complete the final report with exact commands/results, Terraform plan/apply status, build/digest/revisions, before/after metrics, rollback, PR and unresolved external ownership.
