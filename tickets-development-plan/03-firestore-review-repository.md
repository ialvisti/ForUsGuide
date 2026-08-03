# Stage 3 — Firestore Review, Audit, Cache, and Remediation Repository

> **For Claude Opus 5:** This is an executable implementation prompt. Read the master plan and Stage 1 artifacts. Build the repository test-first; do not access production Firestore.

**Goal:** Persist shared review state, application-append-only hash-chained
audit events, manual evidence links, remediation batches, durable import/export
metadata, and disposable DevRev cache entries with transactional correctness in
a dedicated named Firestore database.

**Architecture:** Put all business invariants in a repository facade with interchangeable in-memory and Firestore backends, following the tested pattern in `ticket_job_repository.py`. Firestore documents use server timestamps and transactions; user-facing updates use optimistic concurrency.

**Tech Stack:** Python 3.12, Firestore async client, Pydantic v2, asyncio, pytest.

---

## Prerequisites

```bash
set -euo pipefail
export PLAN_ROOT="${PLAN_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide/tickets-development-plan}"
export TICKETS_BASE_SHA="${TICKETS_BASE_SHA:-eed9b34967c59b8bfec34026c9a8637581f2036a}"
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
test -r "$PLAN_ROOT/README.md"
test "$(git -C "$IMPL_ROOT" rev-parse --show-toplevel)" = "$IMPL_ROOT"
test -x "$PYTHON_BIN"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
git -C "$IMPL_ROOT" merge-base --is-ancestor "$TICKETS_BASE_SHA" HEAD
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_models.py -q
```

Read:

- `data_pipeline/ticket_job_repository.py`
- `data_pipeline/ticket_job_models.py`
- `data_pipeline/execution_logger.py`
- `tests/test_ticket_job_repository.py`
- `firestore.indexes.json`
- `infra/terraform/modules/ticket_environment/firestore.tf`

Reuse transaction concepts, not collection names or ticket-job state semantics.

## Files

Create:

- `kb-rag-system/data_pipeline/ticket_review_repository.py`
- `kb-rag-system/tests/test_ticket_review_repository.py`
- `kb-rag-system/tests/integration/test_firestore_ticket_review_repository.py`

Modify:

- `kb-rag-system/firestore.indexes.json`
- `kb-rag-system/api/tickets_console_config.py`
- `kb-rag-system/tests/test_ticket_review_models.py`
- `kb-rag-system/tests/test_terraform_runtime_contract.py`

Terraform resources are implemented in Stage 10; this stage freezes the canonical index/TTL declaration and repository behavior.

## Step 1 — Write failing repository tests

Run against the in-memory backend. It must have the same externally observable contract as Firestore.

Required tests:

1. `create_or_get_review` is idempotent by deterministic review ID.
2. A different DON cannot collide with an existing review document.
3. `patch_review(expected_version=N)`:
   - succeeds transactionally at version N;
   - increments exactly once;
   - rejects stale N with a typed conflict containing only safe current version metadata;
   - never mutates immutable DevRev IDs or creation time.
4. Status transitions obey the Stage 1 transition table. Admin reopen is explicit and audited.
5. Authenticated actor comes from the service layer. Self-assignment may copy
   that identity into `assigned_reviewer`; admin reassignment accepts only a
   validated configured identity. CSV reviewer text goes only to
   `legacy_reviewer_display_name`.
6. Every successful mutation appends exactly one audit event with
   `previous_event_hash`/`event_hash` and the idempotency-key hash.
7. Failed/stale mutations append no success event and leave the document unchanged.
8. Audit events cannot be updated or deleted through the repository.
9. `devrev_message_cache` upsert is idempotent by hashed remote entry ID;
   remote `object_version`/modified date decides whether a newer TTL snapshot
   replaces an older one.
10. Message bodies, artifact metadata, and lists enforce model size/count limits.
11. Cache entries carry `expires_at`; review/audit documents do not inherit cache TTL.
12. Manual evidence link creation/unlinking is versioned, accepts only a
    service-validated broker candidate token/result (never an arbitrary
    reference), and is authorized/audited.
13. Exact ticket-ID lookup and the master query grammar are deterministic with
    a stable tiebreaker (`updated_at`, `review_id`). Any second facet or title
    substring query fails before the backend call.
14. Invalid/foreign cursors fail safely; cursors reveal no PII.
15. Remediation batch creation freezes unique `(review_id, review_version)`
    pairs into per-review item documents, keeps only count/item-set digest on
    the parent, and rejects empty/duplicate/oversized/over-byte-limit batches
    before any write.
16. Batch claim is atomic, lease-based, idempotent for the same agent, and unavailable to a second active claimant.
17. Expired claims can be reclaimed and produce audit events.
18. Heartbeat/renewal, two-hour continuous cap, release, progress, and result
    updates enforce lease token hash, owner, expiry, and expected version.
19. Resolving a review requires verification evidence; absence fails.
20. A partial multi-review update never silently marks unaffected reviews resolved.
21. Every unsafe repository operation deduplicates a repeated idempotency key
    and rejects the same key with a different request digest.
22. Import apply/reverse follows expected versions, never deletes history, and
    preserves conflicts; export/import global events enter
    `ticket_console_audit_events`.
23. Cache/import-staging/idempotency documents alone receive TTL fields;
    durable review/audit/batch/import/export documents receive
    `retention_expires_at` and `legal_hold`, never a collection-group TTL.
24. Firestore backend refuses `(default)` when environment is staging or
    production.
25. No durable review/audit/export field receives a DevRev title; a synthetic
    title containing email/phone remains confined to cache TTL data.
26. A serialized 100-item batch stays within per-document and transaction/
    batch-write limits and materializes through a bounded item cursor.
27. Retention preview/apply is bounded and idempotent, skips legal holds,
    purges review/evidence/batch/import/export state only after 730 days, and
    retains per-review/batch/global hash-chained audit ledgers until 2,555
    days. Parent product fields become a content-free tombstone while a
    younger audit ledger exists; tests prove the tombstone leaks no
    DON/display ID/title/comment/email/evidence/result.
28. Retention deletes only explicit capped document/subcollection IDs, never a
    database/collection recursively, and appends a content-free global
    retention audit event. After 2,555 days it removes exact ledger event IDs
    before the exact tombstone, including retry/idempotency tests.

Observe failure before implementation:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_repository.py -q
```

## Step 2 — Implement backend contract

Required classes:

```python
class InMemoryTicketReviewBackend: ...
class FirestoreTicketReviewBackend: ...
class TicketReviewRepository: ...
```

Required typed errors:

```python
class ReviewRepositoryError(Exception): ...
class ReviewNotFound(ReviewRepositoryError): ...
class ReviewVersionConflict(ReviewRepositoryError): ...
class InvalidReviewTransition(ReviewRepositoryError): ...
class BatchNotFound(ReviewRepositoryError): ...
class BatchVersionConflict(ReviewRepositoryError): ...
class BatchAlreadyClaimed(ReviewRepositoryError): ...
class BatchLeaseLost(ReviewRepositoryError): ...
```

Do not catch a broad exception and return `None`; the API layer needs typed failures.

Collections:

```text
ticket_reviews
ticket_reviews/{review_id}/audit_events
ticket_reviews/{review_id}/evidence_links
remediation_batches
remediation_batches/{batch_id}/items
remediation_batches/{batch_id}/events
ticket_imports
ticket_imports/{import_id}/rows
ticket_exports
ticket_console_audit_events
devrev_message_cache
ticket_console_cache
ticket_import_staging
idempotency_keys
```

Always select the configured named database. Staging uses
`tickets-console-staging`, production uses `tickets-console-prod`, and local
tests use an emulator database ID. Never emulate isolation with collection
prefixes and never fall back to `(default)`.

## Step 3 — Implement optimistic concurrency and audit atomically

For review mutations, the review write and audit-event create belong in the same transaction.

Audit event fields:

```json
{
  "event_id": "random stable id",
  "parent_kind": "review",
  "parent_id": "hash",
  "event_type": "review_updated",
  "actor_subject": "accounts.google.com:...",
  "actor_email": "reviewer@example.invalid",
  "actor_subject_hash": "sha256",
  "request_id_hash": "sha256",
  "idempotency_key_hash": "sha256",
  "hash_schema_version": 1,
  "occurred_at_unix_us": 1700000000000000,
  "previous_version": 2,
  "new_version": 3,
  "changed_fields": ["rating", "comments"],
  "metadata": {
    "reason_code": "bounded optional value"
  },
  "previous_event_hash": "hex-or-genesis",
  "event_hash": "sha256(versioned canonical hash payload)",
  "created_at": "Firestore SERVER_TIMESTAMP (excluded from hash)"
}
```

Do not duplicate full old/new ticket bodies or comments into every audit event.
Store changed field names and bounded operational metadata. The hash chain is
tamper-evident, not a claim that a datastore principal cannot alter data.
Firestore Data Access logs and the retention-controlled log sink are required
in Stage 10.

Freeze the hash input exactly. Before hashing, NFC-normalize validated string
values and construct an object containing only these literal keys:

```text
hash_schema_version, event_id, parent_kind, parent_id, event_type,
actor_subject_hash, request_id_hash, idempotency_key_hash,
previous_version, new_version, changed_fields, reason_code,
previous_event_hash, occurred_at_unix_us
```

Use JSON `null` for an absent old/new version or reason; never omit a key.
Sort/deduplicate `changed_fields`, serialize with Python
`json.dumps(payload, ensure_ascii=False, sort_keys=True,
separators=(",", ":"), allow_nan=False)`, encode UTF-8, and store lowercase
hex `SHA-256`. Genesis `previous_event_hash` is exactly 64 ASCII zeroes.
`occurred_at_unix_us` is an integer from the injected server-side clock.
Exclude unresolved Firestore `SERVER_TIMESTAMP`, raw actor email/subject,
display text, and every later-enriched field. Store
`created_at=SERVER_TIMESTAMP` separately. Tests recompute genesis and
multi-event hashes byte-for-byte from fixed golden bytes, reject
mutation/reordering/duplicate changed fields, and prove two concurrent
transactions leave one linear chain.

For Firestore timestamps, follow the finalization branch’s native timestamp pattern; do not serialize them to strings before writes.

## Step 4 — Implement opaque application cursors

Firestore review cursors are not DevRev cursors.

Encode only:

- schema version;
- normalized filter fingerprint;
- last ordered timestamp;
- last review ID.

Authenticated-encrypt the cursor with the dedicated
`TICKETS_CURSOR_AEAD_KEY` (a versioned 32-byte key supplied through Secret
Manager) and a cursor-type/version associated-data string. Never base64-encode
unsigned or merely obfuscated JSON and call it opaque.

Tests use an injected deterministic secret.

## Step 5 — Define canonical indexes and TTL

Extend `firestore.indexes.json` without deleting ticket-handler declarations.

Review indexes must match only the master query grammar:

- `status ASC, updated_at DESC, review_id ASC`
- for each one allowed facet, `status ASC, <facet> ASC, updated_at DESC,
  review_id ASC`, where facet is exactly:
  `assigned_reviewer.email`, `topic`, `rating`, `observation_type`,
  `remediation_target`, or `severity`;
- a single-field/index-supported exact normalized `devrev_display_id` lookup.

Do not create the Cartesian product, prefix/title tokens, or multi-facet
indexes. The repository rejects unsupported combinations with a typed error.

TTL only:

- `ticket_console_cache.expires_at`
- `devrev_message_cache.expires_at`
- `ticket_import_staging.expires_at`
- `idempotency_keys.expires_at`

Never TTL:

- `ticket_reviews`
- review audit events
- evidence links
- remediation batch summaries/events required for traceability
- durable import rows/summaries, export summaries, or global audit events

Implement repository-facade `preview_expired` and `purge_expired` methods here,
not in an infrastructure entrypoint. Product records/evidence links/batch
items/import/export metadata use 730-day `retention_expires_at`; every
hash-chained audit/event ledger uses 2,555 days. `legal_hold=true` on either
the parent or ledger suppresses its purge. Firestore does not cascade
subcollections when a parent is deleted; preserve and test that property so a
730-day parent purge cannot shorten the audit contract.

## Step 6 — Firestore emulator contract

Write the integration test against the real Firestore emulator API, not a
mock. It must prove:

- atomic review + audit transaction;
- stale version rollback;
- hash-chain continuity under concurrent mutations;
- idempotency-key dedupe;
- claim/heartbeat/reclaim race behavior;
- database ID selection is not `(default)`.

The current host has no Docker. Do not install it and do not silently downgrade
this to a mock. Ensure Stage 10 adds this exact file to the existing pinned
`firestore-emulator-tests` Cloud Build step. Record
`REMOTE_REQUIRED: tests/integration/test_firestore_ticket_review_repository.py`
in the stage handoff; Stage 11 cannot close without a successful pinned remote
run.

## Step 7 — Verify

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_repository.py -q
"$PYTHON_BIN" -m pytest tests/test_ticket_job_repository.py -q
"$PYTHON_BIN" -m pytest tests/test_ticket_review_models.py -q
"$PYTHON_BIN" -m pytest tests/test_terraform_runtime_contract.py -q
"$PYTHON_BIN" -m json.tool firestore.indexes.json >/dev/null
"$PYTHON_BIN" -m compileall -q data_pipeline api
git -C "$IMPL_ROOT" diff --check
```

Do not point any test at a real Firestore database.

## Definition of Done

- In-memory contract tests pass; the real emulator test exists and is marked
  as a mandatory remote gate (do not claim Firestore atomicity until it passes).
- Stale clients cannot overwrite newer reviewers.
- Review, disposable cache, durable import/export, and audit retention are
  separated.
- Product state and audit ledgers honor distinct 730/2,555-day policies with
  legal hold and non-cascading bounded purge.
- Batch claims and leases are concurrency-safe.
- List cursors are signed and filter-bound.
- Canonical index JSON includes only supported queries.
- No production data or GCP resource was touched.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/data_pipeline/ticket_review_repository.py \
  kb-rag-system/tests/test_ticket_review_repository.py \
  kb-rag-system/tests/integration/test_firestore_ticket_review_repository.py \
  kb-rag-system/api/tickets_console_config.py \
  kb-rag-system/tests/test_ticket_review_models.py \
  kb-rag-system/tests/test_terraform_runtime_contract.py \
  kb-rag-system/firestore.indexes.json
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/data_pipeline/ticket_review_repository.py \
  --allow kb-rag-system/tests/test_ticket_review_repository.py \
  --allow kb-rag-system/tests/integration/test_firestore_ticket_review_repository.py \
  --allow kb-rag-system/api/tickets_console_config.py \
  --allow kb-rag-system/tests/test_ticket_review_models.py \
  --allow kb-rag-system/tests/test_terraform_runtime_contract.py \
  --allow kb-rag-system/firestore.indexes.json
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit -m "feat(tickets): persist reviews and audit history"
```

Proceed to Stage 4 after Stages 2 and 3 both pass.
