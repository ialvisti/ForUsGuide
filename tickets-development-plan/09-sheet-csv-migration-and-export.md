# Stage 9 — Superseded: Internal RAG Execution Ledger and No File Egress

**Status:** Superseded by product decision on 2026-08-05.

This filename remains only so links from the original stage sequence resolve.
It is not an instruction to build CSV, spreadsheet, upload, download, import,
export, escrow, or migration functionality.

## Decision

The ticket evaluation platform is the system of record for this workflow.
Ticket evaluations are created only by ticket-associated RAG executions and
are reviewed in `/tickets`. No file interchange path is part of the product.

Consequences:

- DevRev-only tickets never appear in the platform.
- Each `knowledge_question` or `generate_response` attempt creates one durable
  intent before the RAG effect. New invocation/execution IDs use
  `{job_id}-e{lease_epoch}-a{attempt}:{inquiry_index}`.
- Classification-only, `needs_more_info`, rollout `knowledge_only`, and
  `unprocessed` paths are not real eligible effects and never enter the ledger.
- This requires a validated upstream DevRev `ticket_id`. A legacy RAG call with
  no ticket identity may still run outside the console path, but it cannot
  create a ticket-evaluation invocation, hydrate, or appear in `/tickets`; no
  component fabricates the missing ID.
- A transport replay of one immutable event is idempotent. A real RAG retry
  has a new attempt/lease and a distinct invocation ID; retries never collapse.
- Legacy `{job_id}:{inquiry_index}` events remain readable, but new worker
  invocations do not use that identity.
- A crash after intent creation produces a durable answer-less failed event
  with `RAG_INVOCATION_ABANDONED`; it does not erase the attempt or borrow the
  later retry's answer.
- Ingestion persists the run as `quarantined` before DevRev enrichment. It is
  durable to the private retry plane but is not visible in `/tickets` until
  `works.get` validates existence and configured scope.
- DevRev not-found/scope rejection becomes terminal `denied`. Auth,
  configuration, rate-limit, transport, and outage failures remain
  quarantined and retryable.
- The linked review is created by trusted ingestion, never by a browser action.
- The UI and API expose no file-oriented route, control, or fallback format.

## Replacement data flow

1. Immediately before an eligible ticket-associated RAG call, the worker writes
   a `started` intent to `ticket_rag_invocations`.
2. Success/failure checkpoint, journal completion, and the bounded outbox
   event commit atomically. A reconciler turns an abandoned `started` intent
   into `recovered` plus an explicit failed outbox event.
3. An authenticated publisher scans pending and indexed due-retry outbox
   records independently of job scanning and delivers them to private
   ingestion service with deterministic idempotency.
4. Permanent validation/auth delivery failures become durable `dead_letter`
   records with no TTL. The authenticated, digest-bound
   `APP_ROLE=reconciler python -m api.replay_ticket_evaluation` CLI returns one
   to `pending`; it never edits or directly sends the event.
5. Ingestion persists the immutable execution as `quarantined` and then
   attempts scoped DevRev hydration with `get_ticket`; it never discovers rows
   with `works.list`.
6. `/api/admin/v1/tickets` lists only `authorized` executions. Quarantined,
   denied, and absent IDs share the same safe external absence.
7. `/api/admin/v1/tickets/{execution_id}` combines the immutable run, linked
   review, and available DevRev context.

## Required evaluation detail

Each durable run retains bounded, redacted fields needed to evaluate the RAG
behavior:

- generated answer or structured response;
- explicit classification rationale and outcome rationale;
- route, run status, inquiry, topic, timestamps, and correlation IDs;
- diagnostics, coverage gaps, retrieval metadata, model metadata, and timing;
- source article references and bounded chunk previews with content hashes;
- DevRev hydration state and safe retry/error metadata.

“Rationale” means explicit fields produced for audit and product explanation.
Provider hidden chain-of-thought is not collected, stored, or displayed.

## Permanent no-file-egress invariant

The following are intentionally absent:

- file body parsers or serializers for ticket evaluation data;
- browser file pickers or file transfer controls;
- migration/apply/reverse workflows;
- downloadable review or execution snapshots;
- file staging, file hashes, file manifests, or file-specific retention;
- compatibility endpoints that return a different representation of the
  evaluation ledger.

This invariant applies to the UI, public admin API, private ingestion API,
scripts, runbooks, staging gates, and production verification. If an external
data transfer is proposed later, it requires a new product and privacy decision;
it must not be inferred from this historical stage name.

## Verification contract

The active regression suite must prove:

1. every eligible ticket-associated RAG call has a durable intent before effect;
2. a post-effect crash yields `RAG_INVOCATION_ABANDONED`, while its retry has a
   distinct invocation ID;
3. transport replay stays singular and legacy `{job_id}:{index}` remains readable;
4. only scoped `authorized` executions appear in list/detail;
5. not-found/scope becomes `denied`, while auth/config/outage remains
   quarantined and retryable without metadata leakage;
6. due outbox retries are indexed and job-scan-independent; permanent/auth
   failure is a non-expiring `dead_letter` with authenticated manual replay;
7. list and detail never use DevRev discovery;
8. the browser cannot create a review manually;
9. no file interchange code, routes, collections, settings, scripts, or visible
   controls exist;
10. full bounded run evidence is rendered without unsafe HTML sinks or browser
   persistence.

Focused checks:

```bash
"$PYTHON_BIN" -m pytest \
  tests/test_ticket_rag_invocation_journal.py \
  tests/test_ticket_evaluation_publisher.py \
  tests/test_no_ticket_file_interchange_contract.py \
  tests/test_ticket_review_routes.py \
  tests/test_tickets_ui_contract.py -q
```

Stage 10, Stage 11, and Stage 99 consume the execution ledger and this
no-file-egress decision. They must not reinstate the superseded workflow as an
infrastructure, rollout, or final-verification gate.
