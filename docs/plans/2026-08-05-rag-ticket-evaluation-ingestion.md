# RAG Ticket Evaluation Ingestion Implementation Plan

> **Executor:** REQUIRED SKILL: Use `executing-plans` to implement this plan
> task-by-task with its review checkpoints.

**Goal:** Make the ticket evaluation platform an internal, RAG-produced ledger: every ticket-associated RAG execution creates one durable evaluation record, only those records appear in the platform, and DevRev supplies the ticket context without any CSV or other file import/export path.

**Architecture:** Before an eligible ticket-associated RAG call with a
validated DevRev `ticket_id`, the worker persists an invocation intent in
`ticket_rag_invocations`. New producers
identify that attempt as
`{job_id}-e{lease_epoch}-a{attempt}:{inquiry_index}`; completing the intent and
writing its bounded event to `ticket_evaluation_outbox` are one transaction.
The reconciler converts a stale `started` intent into an answer-less failed
`RAG_INVOCATION_ABANDONED` event, so a crash cannot erase an invocation and a
retry receives a different ID. An authenticated publisher drains pending and
indexed due-retry outbox records independently of job scanning. Private
ingestion persists each received run as `quarantined` before `works.get`; only
a successful scoped DevRev lookup changes it to `authorized` and makes it
visible through the admin API. Not-found/out-of-scope records become `denied`,
while auth, configuration, rate-limit, transport, and outage failures remain
quarantined and retryable. Ticket-level review state remains keyed by the
DevRev DON. The evaluation ingestion/list/detail graph never calls
`works.list`, and no file-interchange path exists.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, Google Cloud Firestore, Cloud Run OIDC, Cloud Scheduler/Cloud Run Jobs, DevRev API, pytest, vanilla JavaScript.

---

## Product invariants

- Every ticket-associated RAG invocation has a durable intent before the RAG
  effect begins. An authorized platform row is a projection of that durable
  invocation, never a DevRev-discovered ticket.
- The journal/outbox contract applies only when upstream supplied a validated
  DevRev `ticket_id`. Legacy RAG calls without that identity may still execute,
  but cannot hydrate or appear in the console; never invent a ticket ID to make
  them eligible.
- New invocation/execution IDs use
  `{job_id}-e{lease_epoch}-a{attempt}:{inquiry_index}`. A transport replay of
  one immutable event is idempotent; a real RAG retry is a different attempt
  with a different ID and must not collapse into the prior invocation.
- Legacy `{job_id}:{inquiry_index}` events remain readable for compatibility,
  but the hardened worker no longer emits that identity for new invocations.
- Real ticket-associated `knowledge_question` and `generate_response` calls are
  the eligible RAG executions. Classification-only, `needs_more_info`, rollout
  `knowledge_only`, and `unprocessed` paths do not enter the evaluation ledger.
- Successful, partial, failed, timed-out, and crash-abandoned attempts remain
  auditable. Crash recovery emits `status=failed`, `answer=null`, error
  `RAG_INVOCATION_ABANDONED`, and explicit recovery diagnostics.
- Ingestion is persist-first but publish-to-users-last: every received run is
  durable and `quarantined` until `works.get` proves existence and configured
  scope. Quarantined and `denied` records are indistinguishable from absent at
  browser/admin list and detail routes.
- DevRev not-found or scope rejection is terminal `denied`. Authentication,
  configuration, rate-limit, transport, and service outage failures remain
  quarantined with bounded retry metadata.
- CSV/file import, export, download, upload, staging, reversal, serializers,
  models, settings, repository methods, routes, scripts, and manual “add
  DevRev ticket” paths are absent, not merely hidden or dormant.
- “Reasoning” means the system’s explicit structured rationale (`classification.reasoning`, outcome reason, diagnostics, gaps, and retrieval signals). Provider hidden chain-of-thought is neither captured nor exposed.
- Full answers and useful evidence are retained with explicit bounds and redaction; raw secrets, authorization headers, and unbounded source bodies are never stored.

## Task 1: Define the execution event contract

**Files:**

- Create: `kb-rag-system/api/ticket_evaluation_models.py`
- Test: `kb-rag-system/tests/test_ticket_evaluation_models.py`

1. Add failing validation tests for invocation-scoped execution IDs,
   `invocation_id`, `attempt`, `lease_epoch`, supported RAG routes,
   terminal/partial status, ticket identity, structured response payload,
   classification rationale, diagnostics, source references, chunk
   previews/hashes, timestamps, and schema version.
2. Prove the hardened ID uses
   `{job_id}-e{lease_epoch}-a{attempt}:{inquiry_index}`, while legacy
   `{job_id}:{inquiry_index}` events remain readable but are not newly emitted.
3. Prove classification-only, `needs_more_info`, rollout `knowledge_only`, and
   `unprocessed` payloads are rejected as platform executions. Prove a legacy
   RAG flow without validated DevRev
   `ticket_id` can continue its existing non-console behavior but creates no
   journal/outbox/platform row and never receives a fabricated identity.
4. Implement bounded Pydantic models and canonical serialization. Use flat, deterministic fields at the Firestore boundary and keep potentially large evidence in bounded lists.
5. Run the focused tests and retain the RED/GREEN evidence.

## Task 2: Capture each RAG execution transactionally

**Files:**

- Modify: `kb-rag-system/data_pipeline/ticket_job_repository.py`
- Modify: `kb-rag-system/api/ticket_worker.py`
- Test: `kb-rag-system/tests/test_ticket_job_repository.py`
- Test: `kb-rag-system/tests/test_ticket_worker.py`
- Test: `kb-rag-system/tests/test_ticket_rag_invocation_journal.py`

1. Add failing tests proving `begin_rag_invocation` writes a `started` intent
   to `ticket_rag_invocations` before the orchestrator/provider effect.
2. Complete the journal and outbox atomically with the inquiry checkpoint.
   `completed.event_digest` must equal the outbox digest.
3. Prove a lease retry gets a distinct invocation ID. Never deduplicate two
   provider calls merely because job ID and inquiry index match.
4. Scan due `started` intents independently. If the matching lease is still
   live, reschedule observation; otherwise recover the intent as `recovered`
   and emit an answer-less failed event with
   `RAG_INVOCATION_ABANDONED`, `failure_phase=rag_invocation_recovery`, and
   `invocation_outcome=abandoned_after_lease`.
5. Include success, partial, failure, timeout, post-effect crash, and retry
   cases. Assert bounded answer/rationale/diagnostics/sources/retrieval fields
   and no secrets.
6. Preserve the existing 24-hour worker payload policy. A non-terminal
   invocation intent has no TTL; only `completed`/`recovered` journals receive
   `expires_at`.
7. Run focused repository, worker, journal, and reconciler tests. Assert the
   exact reconciler counters `rag_invocations_scanned`,
   `rag_invocations_recovered`, `rag_invocations_rescheduled`, and
   `rag_invocation_errors`.

## Task 3: Publish pending snapshots reliably

**Files:**

- Modify: `kb-rag-system/data_pipeline/ticket_reconciler.py`
- Modify: `kb-rag-system/config.py`
- Create or modify: `kb-rag-system/data_pipeline/ticket_evaluation_publisher.py`
- Create: `kb-rag-system/api/replay_ticket_evaluation.py`
- Optional development wrapper: `kb-rag-system/scripts/replay_ticket_evaluation.py`
- Test: `kb-rag-system/tests/test_ticket_reconciler.py`
- Test: `kb-rag-system/tests/test_ticket_evaluation_publisher.py`

1. Add failing tests for OIDC-authenticated delivery, idempotent 2xx
   acknowledgement, transient retry, permanent validation/auth failure, and an
   unavailable destination.
2. Scan pending outbox documents and indexed due retries independently of all
   active/terminal job scans. The due query is ordered by
   `state`, `next_attempt_at`, and document name so future retries cannot hide
   an overdue event.
3. Mark delivery only after the evaluation endpoint acknowledges the same
   invocation ID/digest. Only `delivered` receives `expires_at`.
   `pending`, `retry`, and permanent/auth `dead_letter` records have no TTL.
4. Provide the authenticated, digest-bound
   `APP_ROLE=reconciler python -m api.replay_ticket_evaluation --execution-id ...
   --event-digest ...` operator path. It may move one `dead_letter` back to
   `pending` but never edits/sends the event directly; normal OIDC publisher
   delivery handles the replay and retains hashed audit metadata.
5. Keep destination URL, OIDC audience, exact publisher service account,
   timeout, batch limit, outbox index, and retention explicit in configuration.
6. Run focused publisher/reconciler/replay tests.

## Task 4: Add private, idempotent evaluation ingestion

**Files:**

- Create: `kb-rag-system/api/ticket_evaluation_ingest_app.py`
- Modify: `kb-rag-system/api/auth.py` only if a shared OIDC verifier is needed
- Modify: `kb-rag-system/api/ticket_review_models.py`
- Modify: `kb-rag-system/data_pipeline/ticket_review_repository.py`
- Modify: `kb-rag-system/data_pipeline/ticket_review_service.py`
- Test: `kb-rag-system/tests/test_ticket_evaluation_ingest.py`
- Test: `kb-rag-system/tests/test_ticket_review_repository.py`

1. Add failing tests for exact OIDC audience/service-account checks, path/body ID mismatch, first ingestion, byte-for-byte replay, conflicting replay, and persistence when DevRev is unavailable.
2. Persist immutable run records under invocation-scoped IDs with
   `authorization_status=quarantined` before any DevRev call. Automatically
   create/link the ticket-level review only after authorization succeeds.
3. Hydrate DevRev with `get_ticket`, constrained by configured parts/visibility. Never call `works.list` in ingestion.
4. Track hydration as `pending | succeeded | failed` separately from
   authorization `quarantined | authorized | denied`. Not-found/scope failure
   is non-retryable `denied`; auth/config/rate/transport/outage failure remains
   quarantined and retryable. A durable quarantined run is acknowledged to the
   publisher but is not visible to an admin/browser caller.
5. Keep the DevRev credential only in the evaluation service.

## Task 5: Make RAG runs the sole admin API collection

**Files:**

- Modify: `kb-rag-system/api/ticket_review_routes.py`
- Modify: `kb-rag-system/data_pipeline/ticket_review_service.py`
- Modify: `kb-rag-system/data_pipeline/ticket_review_repository.py`
- Test: `kb-rag-system/tests/test_ticket_review_routes.py`

1. Replace live DevRev discovery with a paginated, `authorized`-only execution
   collection endpoint. Its cursor must be opaque, sort stable, and filters constrained.
2. Require a persisted, authorized run before returning detail. Quarantined,
   denied, and absent IDs share the same safe not-found response without
   exposing hydration/error metadata.
3. Prove two authorized invocations for one ticket return two items, transport
   replay returns one, a real retry stays distinct, and DevRev-only,
   quarantined, or denied tickets return none.
4. Preserve optimistic concurrency for reviewer edits and evidence provenance. Define whether edits remain ticket-level while immutable run evidence is per execution.
5. Assert that no import/export routes exist and `works.list` is never invoked by list/detail.

## Task 6: Convert the UI to a single evaluation queue

**Files:**

- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/app.js`
- Modify: `kb-rag-system/ui/tickets/api.js`
- Modify: `kb-rag-system/ui/tickets/state.js`
- Modify: `kb-rag-system/ui/tickets/render.js`
- Modify: `kb-rag-system/ui/tickets/detail.js`
- Modify: `kb-rag-system/ui/tickets/evidence.js`
- Test: `kb-rag-system/tests/test_tickets_ui_contract.py`

1. Add failing contract tests that forbid “All DevRev tickets”, manual queue insertion, CSV/import/export/download/upload controls, and legacy API paths.
2. Render one queue of authorized RAG invocations with invocation/execution
   ID, attempt, execution status, route, ticket reference, timestamp, and
   review status.
3. Show the selected run’s generated answer, explicit structured rationale, diagnostics, gaps, source articles, bounded chunk evidence, model/timing metadata, and DevRev ticket context.
4. Do not render pending/failed hydration rows: they remain private quarantine
   state. Keep reviewer actions and conflict handling accessible for authorized
   runs.
5. Run the UI contract tests and a browser smoke check when the local app can be started.

## Task 7: Retire the CSV migration/export phase and stale scaffolding

**Files:**

- Rewrite: `tickets-development-plan/09-sheet-csv-migration-and-export.md`
- Modify: later ticket-platform plan documents that describe CSV as a dependency
- Delete: all executable dormant CSV/import/export models, settings,
  repositories, collections, routes, scripts, feature flags, and tests
- Test: `kb-rag-system/tests/test_no_ticket_file_interchange_contract.py`
- Test: `kb-rag-system/tests/test_ticket_review_routes.py`
- Test: `kb-rag-system/tests/test_tickets_ui_contract.py`

1. Turn Stage 9 into a superseded decision record explaining the internal RAG-to-platform flow and the no-file-egress invariant.
2. Remove the complete executable file-oriented implementation surface. The
   only allowed historical compatibility is passive parsing/filtering of an
   already stored `ImportState`/`import_state` and legacy reviewer field when
   required to read old reviews; there is no mutation, staging, apply,
   reversal, serialization, or export behavior behind it.
3. Add route/UI regression tests so CSV or other file interchange cannot reappear accidentally.
4. Update later plans to depend on the execution ledger rather than a Sheet migration.

## Task 8: Wire deployable services without weakening trust boundaries

**Files:**

- Create or modify the service entrypoints/Dockerfiles used by the repository’s Cloud Run convention
- Modify the applicable Terraform root under `infra/terraform/`
- Modify: `kb-rag-system/README.md` and deployment runbook/config documentation
- Test: Terraform validation and configuration tests

1. Package the private ingestion service separately from the reviewer-facing console so Cloud Run OIDC and IAP/user authorization are not conflated.
2. Grant the worker/reconciler publisher only invocation access to ingestion. Grant DevRev secret access only to ingestion/console components that hydrate ticket data.
3. Configure Firestore collections/indexes, TTL, environment variables,
   service accounts, and secret references explicitly. Include the
   `ticket_rag_invocations` recovery index
   (`state`, `next_recovery_at`, `__name__`) and the outbox due-retry index
   (`state`, `next_attempt_at`, `__name__`). Do not create/change Pinecone indexes.
4. Keep infrastructure application gated on the missing DevRev secret and confirmed allowed part/visibility scope. Planning and validation are safe; production mutation requires those concrete values.

## Task 9: Verify end to end

1. Run the focused RED/GREEN suites from each task, followed by the full relevant pytest suite.
2. Run static checks already established by the repository.
3. Exercise locally: intent-before-effect, post-effect crash recovery,
   distinct retry IDs, legacy ID readability, duplicate delivery, `dead_letter`
   replay, DevRev allowed/denied/quarantined outcomes, hydration retry,
   authorized-only list/detail, reviewer update, and total file-surface absence.
4. Inspect rendered UI at desktop and narrow widths.
5. Review the final diff for secrets, accidental unbounded payloads, unrelated changes, and stale CSV wording.
6. Report separately: code/tests complete, infrastructure validated, and production deployment blocked or completed. Never describe missing credentials as a successful connection.

## Deployment inputs still required

- A DevRev service-account token/PAT stored in Secret Manager, not in source or local files.
- The allowed DevRev part IDs and visibility values for `get_ticket` authorization.
- The final reviewer access policy/IAP group for the admin UI.
- Confirmation of the Cloud Run URL(s) after Terraform/service deployment, used as the OIDC audience and internal ingestion destination.

Execute this plan with the available `executing-plans`, TDD, API design,
FastAPI, RAG, and verification skills, preserving the task-by-task checkpoints
and evidence gates above.
