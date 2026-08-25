# Ticket Evaluation Loss and Latency Remediation Plan

> **Executor:** REQUIRED SKILL: Use `executing-plans` to implement this plan
> task-by-task with its review checkpoints.

**Goal:** Ensure every ticket-associated, answer-producing RAG invocation is durably represented in `/tickets`, and make the normal visibility path immediate while retaining the six-minute reconciler as recovery.

**Architecture:** Preserve the existing invocation journal and transactional outbox as the source of truth. Add a standalone journal path for ticket-associated calls made through the legacy direct response endpoints, and add an exact-ID publisher operation that sends only the event just committed. Both the durable worker and the direct API producer invoke that operation after commit on a best-effort basis; a failed immediate delivery remains in `pending`/`retry` for the scheduled reconciler. Supporting endpoints (`route-inquiry` and `required-data`) propagate the optional ticket identity but do not create review rows because they do not produce the participant answer being evaluated. The private ingestion service continues to authorize every ticket against DevRev before making it visible.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, Google Cloud Firestore, Cloud Run OIDC/IAM, Terraform, pytest.

---

## Local implementation status (not deployed)

- Tasks 1 through 5b are implemented in this checkout. The direct response endpoints now journal ticket-associated invocations before the RAG effect, atomically stage their outbox event, and offer that exact event for immediate delivery after commit. The durable worker uses the same fast path and the reconciler remains the repair path.
- Direct correlation now rejects `context.ticket_id` unless the canonical top-level `ticket_id` is also present and equal. The local GR builder prompts no longer ask an LLM to copy ticket identity, and raw `error`/`details` diagnostic text is removed before a degraded response becomes durable evidence.
- Reviewer visibility now orders by `ready_at`, filters successful hydration server-side, resolves exact execution IDs by point read, scans filtered internal chunks transparently instead of returning false empty pages, and refreshes the visible first page every 15 seconds with transient-error and bfcache recovery.
- Inline hydration has a 5-second deadline; exact participant-path delivery has a 10-second total deadline; scheduled hydration recovery is capped at 25 rows, five concurrent calls, and 45 seconds. Per-item errors are isolated and reported separately from successfully acquired hydration attempts.
- Verification after the final changes: 1,070 affected tests passed; the full non-live suite passed with 4,266 tests, 36 environment-dependent skips, and 5 explicit deselections. Python compilation, JavaScript syntax checks, and `git diff --check` passed. Terraform contract tests passed, but no Terraform/OpenTofu binary is installed locally; the Firestore emulator component is present but cannot start because no Java runtime is installed.
- A clipboard-style n8n graph was supplied on 2026-08-24 under `n8n_workflow/`. The local copy now validates the trigger lookup ID, re-establishes ticket authority from the authenticated DevRev `works.get` response, rebuilds the direct KQ body from the parsed question plus that canonical ID, keeps identity outside the LLM, derives `Handle Ticket` idempotency from a stable canonical-input fingerprint, and gates polled output on `succeeded + send_participant_reply + !fallback`. Static workflow contracts cover those invariants. The supplied JSON still is not a complete inactive workflow export: it lacks workflow identity/settings/activation/error-workflow metadata, and there is no authenticated n8n editor/API session. Production must not be described as fixed until the corrected clone is imported, smoked, and activated in the coordinated rollout.
- The direct KQ route now journals and publishes ticket-associated evaluations, but it does not yet offer idempotent response replay. Before activation, either add an API-backed stable `Idempotency-Key` contract for direct KQ calls and configure the n8n node to retry/replay it, or replace that ticket path with an equivalent durable job endpoint. A header added only in n8n is not sufficient while the endpoint ignores it.
- The supplied snapshot also exposed three copies of a literal DevRev bearer and pinned participant data. Both were removed from the local JSON without reading or reproducing the bearer, but the credential must be revoked/rotated in DevRev/n8n because the original export must be treated as compromised. The public webhook declares no authentication or durable pre-ACK handoff; those operational controls require the complete workflow/upstream delivery configuration.
- Rolling compatibility requires deploying the new publisher/reconciler before the new ingestion receiver. The new publisher accepts both two-field and three-field hydration ACKs; an old publisher rejects the additive `errors` field on exceptional batches (without losing durable data, but with degraded telemetry until its next retry).

## Confirmed production baseline

- `/api/admin/v1/tickets` reads `ticket_evaluation_runs`, not live DevRev tickets, and intentionally exposes only `authorization_status=authorized` records.
- Since the ledger rollout, production recorded 118 accepted `/handle-ticket` jobs. Of those, 34 reached an answer-producing terminal RAG route (30 `generate_response`, 4 `knowledge_question`), and all 34 have both an ingestion record and an authorized console row. There is no current outbox, retry, dead-letter, or hydration-quarantine backlog.
- Calls to the four legacy direct RAG endpoints only write aggregate `execution_logs`; they do not create an invocation journal or outbox event. Historical direct calls cannot be backfilled from that log because it intentionally stores neither ticket identity nor raw request content.
- Production contains 46 successful n8n `POST /api/v1/knowledge-question` calls outside the journal between 2026-07-23 and 2026-08-21. The deployed request contract accepts only `question`, even though the workflow prompt receives `ticketData.ticketId`, so ticket identity is discarded before the request reaches the backend. Cloud Logging intentionally contains neither request bodies nor ticket identity, so those 46 historical calls cannot be safely attributed or backfilled from GCP alone.
- A read-only recheck on 2026-08-24 found no deployment after 2026-08-19. Since 2026-08-21, production accepted 24 additional `/handle-ticket` jobs (22 succeeded, 2 partial); all 7 new answer-producing runs are authorized `generate_response` rows with no journal/outbox/hydration backlog. The 4 direct KQ calls on 2026-08-21 are already included in the historical total of 46, and there have been none since 2026-08-22. For the 7 durable rows, accepted-to-visible latency remained 249.5 seconds at the median and 463.5 seconds at the maximum while DevRev hydration itself remained 1.107 seconds at the median, confirming the six-minute scheduler—not hydration—as the dominant delay.
- Delivery currently waits for the reconciler scheduled every six minutes. Measured outbox-to-delivery latency was about 197 seconds at the median and 349 seconds at the maximum; end-to-console latency was about 290 seconds at the median and 445 seconds at p95.
- The console used the RAG occurrence timestamp for ordering and loaded only at boot/manual refresh. A row hydrated late could therefore become visible behind page one, and a transient empty-page failure or bfcache restore could stop automatic recovery.

## Invariants

- The invocation intent exists before the RAG provider effect whenever a valid ticket identity is supplied.
- Completing an invocation and staging its immutable outbox event remain one transaction.
- Immediate delivery happens only after that transaction commits and addresses the exact invocation ID; it never scans an arbitrary one-item batch.
- Failure to publish immediately never loses or rolls back a completed RAG response. The outbox remains retryable by the reconciler.
- Calls without a ticket identity retain their existing general-purpose behavior and do not fabricate a ticket or console row.
- Direct ticket-associated response calls require a bounded DevRev display ID or DON. Supporting calls propagate the identity to the suggested downstream payload.
- Ticket identity is caller/workflow-owned, never LLM-owned. Any n8n body-builder output is untrusted for identity and must be overwritten from the canonical DevRev trigger immediately before each HTTP request.
- The ingestion service remains the authorization boundary. A caller cannot make an out-of-scope ticket visible by supplying an ID.
- Production IAM and the application allowlist authorize only the producer, worker, and reconciler service accounts to invoke private ingestion.

## Task 1: Reproduce exact-event delivery latency

**Files:**

- Modify: `kb-rag-system/tests/test_ticket_evaluation_publisher.py`
- Modify: `kb-rag-system/data_pipeline/ticket_job_repository.py`
- Modify: `kb-rag-system/data_pipeline/ticket_evaluation_publisher.py`

1. Add a failing test proving `publish_execution(invocation_id)` sends that exact pending event even when another event sorts first.
2. Add failure-path tests proving a transient delivery records retry state and a delivered/dead-letter event is not resent.
3. Add the bounded repository lookup and refactor the publisher's single-event transport logic so batch and exact-ID publishing share validation, OIDC, ACK, and state transitions.
4. Run the focused publisher and repository tests and retain RED/GREEN evidence.

## Task 2: Instrument legacy direct response calls durably

**Files:**

- Modify: `kb-rag-system/api/models.py`
- Modify: `kb-rag-system/api/main.py`
- Modify: `kb-rag-system/api/ticket_evaluation_models.py`
- Modify: `kb-rag-system/data_pipeline/ticket_job_repository.py`
- Create: `kb-rag-system/tests/test_direct_ticket_evaluation.py`
- Modify: `kb-rag-system/tests/test_api.py`

1. Add failing tests for an optional, validated `ticket_id` on direct requests and propagation from `route-inquiry` into its suggested downstream payload.
2. Add failing tests proving `knowledge-question` and `generate-response` create the journal before calling the engine, then atomically complete the journal/outbox for success and sanitized failure.
3. Prove calls without `ticket_id` retain their current behavior and create no ledger record.
4. Add a standalone invocation-begin operation using a hashed request correlation ID and the existing deterministic invocation identity/event model.
5. Add small endpoint helpers for begin/complete/publish; do not duplicate the durable event contract in route handlers.
6. Run the focused model, API, journal, and new regression tests.

## Task 3: Publish worker results immediately

**Files:**

- Modify: `kb-rag-system/api/main.py`
- Modify: `kb-rag-system/api/ticket_worker.py`
- Modify: `kb-rag-system/tests/test_ticket_worker.py`
- Modify: `kb-rag-system/tests/test_api_lifespan.py` or the nearest runtime-wiring test

1. Add a failing worker test proving the exact invocation is offered to the publisher only after its checkpoint/outbox commit.
2. Add a failing test proving publisher failure does not change the ticket result and leaves recovery to the outbox.
3. Build one process-owned publisher for producer/worker roles when enabled, close it during shutdown, and make it available through `app.state`.
4. Invoke the exact-ID fast path after durable worker completion in all success/failure completion paths.
5. Run focused worker and lifespan tests.

## Task 4: Authorize the fast path in infrastructure

**Files:**

- Modify: `infra/terraform/modules/ticket_environment/cloud_run.tf`
- Modify: `infra/terraform/modules/ticket_environment/variables.tf`
- Modify: `infra/terraform/modules/tickets_console/main.tf`
- Modify: relevant Terraform outputs/variables and live environment wiring
- Modify: `kb-rag-system/tests/test_deployment_contract.py`

1. Add failing deployment-contract assertions that producer and worker receive the ingestion URL, audience, enabled flag, timeout, and their own publisher service-account identity.
2. Replace the ingestion application's singular expected publisher with a closed JSON allowlist containing producer, worker, and reconciler identities.
3. Grant `roles/run.invoker` on private ingestion to those exact service accounts.
4. Keep the reconciler schedule unchanged as repair, not the normal delivery path.
5. Run Terraform formatting/validation and deployment-contract tests.

## Task 5: Harden visibility diagnostics

**Files:**

- Modify: `kb-rag-system/data_pipeline/ticket_evaluation_publisher.py`
- Modify: `kb-rag-system/api/metrics.py` and/or existing closed-schema telemetry tests
- Modify: relevant Terraform monitoring definitions if supported by the existing metric contract

1. Preserve the private ACK's `hydration_status` in safe delivery counters/metrics so a delivered-but-quarantined event is distinguishable from an authorized visible row.
2. Add bounded metrics for immediate publish outcome and recovery-path age/depth without exposing ticket IDs or response content.
3. Do not expose quarantined or denied records through the public admin API; that boundary protects DevRev scope.

## Task 5a: Make the reviewer queue truthful and self-recovering

1. Filter successful hydration in Firestore before pagination so legacy authorized rows without the newer `authorization_status` field remain queryable and a large quarantined prefix cannot produce a false empty page.
2. Order/cursor by the server-owned hydration update time, expose it as `ready_at`, and keep the original RAG `occurred_at` for audit/detail.
3. Provision the matching composite index and make the console revision depend on index readiness.
4. Poll the visible first page every 15 seconds; recover after transient network/429/5xx failures and restart idempotently after a bfcache restore.
5. Resolve an exact `execution_id` with a Firestore point read rather than a bounded 1,000-row scan. For other filtered pages, never claim global absence while a continuation cursor exists.

## Task 5b: Bound the private ACK path

1. Put a 5-second absolute deadline around inline DevRev hydration and the visibility-link transaction; on timeout, persist `devrev_timeout` as retryable and ACK the durable failed hydration state.
2. Keep participant-facing exact publish at a 10-second total wall-clock deadline with 5-second connect/write/pool phase bounds. The deadline includes the Firestore outbox lookup, OIDC token minting, HTTP/ACK validation, and the local delivery-state transition. An ambiguous timeout leaves the outbox recoverable instead of extending the endpoint response by a full Cloud Run request timeout.
3. Cap hydration recovery at 25 rows, five concurrent DevRev calls, and a 45-second reconciler-only request timeout so one scheduled batch fits inside the receiver's 60-second Cloud Run limit. The recovery deadline includes OIDC token minting as well as the HTTP request.

## Task 5c: Fix workflow-owned correlation

A partial nodes/connections clipboard export is now present and patched locally, but it is not sufficient for an activation-ready release. Do not make the LLM emit or copy `ticket_id` as a substitute.

1. Export a sanitized, inactive copy of the active workflow.
2. Capture and validate the DevRev ticket ID immediately after the trigger in a non-LLM branch.
3. Build fresh request objects immediately before each HTTP node, overwriting KQ `ticket_id`, GR `ticket_id` plus `context.ticket_id`, and required-data `ticket_id` from that canonical value.
4. Add adversarial workflow tests where the AI output contains a different valid ticket ID and prove the HTTP request still uses the trigger ID; include missing/invalid identity and multi-inquiry cases.
5. Smoke the inactive copy in staging, then activate it in a coordinated API/workflow rollout.

## Task 6: Verification and rollout

1. Run focused tests after each task, then the complete affected Python and Terraform contract suites.
2. Inspect the final diff against the pre-existing dirty UI work and confirm no unrelated files were overwritten.
3. Run `terraform fmt -check` and `terraform validate` in each changed module/live root where providers are initialized.
4. Produce a read-only pre-deploy inventory of Cloud Run revisions, service accounts, IAM, scheduler, outbox, invocation, authorized, quarantined, and dead-letter counts.
5. Apply only through the repository's established Terraform/release workflow. After deployment, submit one synthetic in-scope ticket-associated call, verify the row appears without waiting for a scheduler tick, and confirm a forced transient publisher failure drains on the next reconciler run.
6. Roll out the Firestore index to READY before the console revision. First deploy the new publisher-capable image to worker/reconciler (while the old two-field receiver ACK remains compatible), then deploy private ingestion plus its allowlist/IAM. Coordinate the producer revision and corrected n8n clone as one paused/atomic cutover: the new backend deliberately rejects GR requests that supply only LLM-controlled `context.ticket_id`, while the old backend ignores the new KQ top-level identity. Enable traffic only after both sides pass the inactive-clone smoke test. Reversing the IAM order can turn 401/403 delivery failures into dead letters; deploying the receiver's additive error ACK before the new reconciler temporarily degrades hydration telemetry.
