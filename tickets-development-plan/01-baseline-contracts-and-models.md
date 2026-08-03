# Stage 1 — Clean Baseline, Contracts, Models, and Fixtures

> **For Claude Opus 5:** REQUIRED SUB-SKILL: use plan execution and test-driven development. This is an executable implementation prompt. Read `tickets-development-plan/README.md` first, then make the changes; do not return a substitute plan.

**Goal:** Establish a clean implementation base and freeze the `/tickets` domain, API, configuration, and DevRev fixture contracts before network, Firestore, UI, or infrastructure code is built.

**Architecture:** Add a self-contained tickets-console domain alongside the existing API instead of expanding `api/main.py`. Define strict Pydantic models and settings that both the standalone admin app and repository/service layers will share.

**Tech Stack:** Python 3.12, Pydantic v2, pydantic-settings, pytest, JSON fixtures.

---

## Mandatory preflight

The plan package is external to the implementation worktree. Run exactly:

```bash
set -euo pipefail
export PLAN_ROOT="${PLAN_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide/tickets-development-plan}"
export TICKETS_BASE_SHA="${TICKETS_BASE_SHA:-eed9b34967c59b8bfec34026c9a8637581f2036a}"
export SOURCE_REPO="${SOURCE_REPO:-/Users/ivanalvis/Desktop/ForUsGuide}"
export FINALIZATION_ROOT="${FINALIZATION_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization}"
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export TICKETS_BRANCH="${TICKETS_BRANCH:-codex/tickets-review-console}"

test -r "$PLAN_ROOT/README.md"
test "$(git -C "$SOURCE_REPO" rev-parse --show-toplevel)" = "$SOURCE_REPO"
test "$(git -C "$FINALIZATION_ROOT" rev-parse --show-toplevel)" = \
  "$FINALIZATION_ROOT"
test -z "$(git -C "$FINALIZATION_ROOT" status \
  --porcelain=v1 --untracked-files=all)"
test "$(git -C "$FINALIZATION_ROOT" rev-parse HEAD)" = "$TICKETS_BASE_SHA"
git -C "$FINALIZATION_ROOT" merge-base --is-ancestor 11cdc51 \
  "$TICKETS_BASE_SHA"
git -C "$SOURCE_REPO" cat-file -e "$TICKETS_BASE_SHA^{commit}"

if ! git -C "$IMPL_ROOT" rev-parse --show-toplevel >/dev/null 2>&1; then
  test ! -e "$IMPL_ROOT"
  if git -C "$SOURCE_REPO" show-ref --verify --quiet \
      "refs/heads/$TICKETS_BRANCH"; then
    echo "STOP: target branch exists but has no validated worktree" >&2
    exit 1
  fi
  git -C "$SOURCE_REPO" worktree add -b "$TICKETS_BRANCH" \
    "$IMPL_ROOT" "$TICKETS_BASE_SHA"
fi

export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
if test -z "${PYTHON_BIN:-}"; then
  if test -x "$KBRAG_ROOT/.venv/bin/python"; then
    export PYTHON_BIN="$KBRAG_ROOT/.venv/bin/python"
  else
    export PYTHON_BIN="/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python"
  fi
fi
test "$(git -C "$IMPL_ROOT" rev-parse --show-toplevel)" = "$IMPL_ROOT"
test "$(git -C "$IMPL_ROOT" branch --show-current)" = "$TICKETS_BRANCH"
test -d "$KBRAG_ROOT"
test -x "$PYTHON_BIN"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
git -C "$IMPL_ROOT" merge-base --is-ancestor "$TICKETS_BASE_SHA" HEAD
git -C "$IMPL_ROOT" rev-parse HEAD
"$PYTHON_BIN" --version
```

Expected:

- exact implementation root and feature branch;
- empty porcelain output;
- ancestry command exits 0.

If not, stop and report the exact blocker. Do not reconcile, reuse, delete, or
clean an unexpected worktree/branch yourself. Python 3.14 is acceptable for
fast local RED/GREEN feedback only; the Stage 10/11 Cloud Build gate is the
authoritative Python 3.12 result.

Read completely:

- `AGENTS.md`
- `.agents/PINECONE.md`
- `.agents/PINECONE-python.md`
- `kb-rag-system/api/config.py`
- `kb-rag-system/api/models.py`
- `kb-rag-system/data_pipeline/ticket_job_models.py`
- `kb-rag-system/data_pipeline/execution_logger.py`
- `kb-rag-system/requirements.in`
- `kb-rag-system/requirements-dev.in`
- `kb-rag-system/firestore.indexes.json`
- `infra/terraform/README.md`

Inspect the supplied reference read-only:

```bash
test -r /Users/ivanalvis/Desktop/better_devrev_search/devrev.js
sed -n '1,220p' /Users/ivanalvis/Desktop/better_devrev_search/devrev.js
sed -n '1,130p' /Users/ivanalvis/Desktop/better_devrev_search/README.md
```

Record in the ADR that structured-filter routing/error presentation may inform
the implementation, but its browser PAT, incomplete cursor handling, and lack
of detail/timeline/durable audit are explicitly rejected. Do not copy any
credential or persisted browser-storage behavior.

## Files

Create:

- `kb-rag-system/api/tickets_console_config.py`
- `kb-rag-system/api/ticket_review_models.py`
- `kb-rag-system/tests/test_ticket_review_models.py`
- `kb-rag-system/tests/fixtures/devrev/works_list_page_1.json`
- `kb-rag-system/tests/fixtures/devrev/works_list_page_2.json`
- `kb-rag-system/tests/fixtures/devrev/work_get_ticket.json`
- `kb-rag-system/tests/fixtures/devrev/timeline_page_empty_with_cursor.json`
- `kb-rag-system/tests/fixtures/devrev/timeline_page_final.json`
- `kb-rag-system/scripts/verify_staged_scope.py`
- `kb-rag-system/tests/test_verify_staged_scope.py`
- `kb-rag-system/Development Docs/TICKETS_REVIEW_ARCHITECTURE.md`

Modify only if a new direct dependency is proven necessary:

- `kb-rag-system/requirements.in`
- `kb-rag-system/requirements-dev.in`
- generated lock files using the repository’s documented Python 3.12 lock workflow

Do not edit `requirements.txt` directly on the finalization base.

## Step 1 — Write failing model/config tests

Tests must initially fail because the new modules do not exist. Cover:

1. DevRev DON hashes produce a stable lowercase SHA-256 review ID and never contain `/`.
2. Rating accepts integers 1–5 or `None`, rejects booleans, 0, 6, strings, and floats.
3. All enum values in the master plan are closed; unknown strings fail.
4. Reviewer identity requires non-empty IAP subject and normalized lowercase
   email; `assigned_reviewer`, `legacy_reviewer_display_name`, and authenticated
   mutation actor are separate.
5. `TicketReview` rejects unknown fields and starts at `version=1`.
6. Remote strings are length-bounded; overlong comments, title, IDs, and URLs fail clearly.
7. `ReviewPatch` has at least one allowed mutable field and rejects immutable identifiers/version timestamps in the body.
8. `DevRevTicketSummary`, `DevRevTimelineEntry`, `EvidenceLink`, `AuditEvent`,
   `VerificationEvidence`, `ReviewResolution`, `RemediationBatch`,
   `TicketImport`, and forward/backward cursor response envelopes round-trip
   through JSON mode.
9. `TicketConsoleSettings` defaults to disabled/local-safe behavior and never provides a token default.
10. Production configuration rejects:
    - missing DevRev token reference/value at runtime;
    - local auth mode;
    - synthetic-verification mode;
    - wildcard allowed email domains;
    - missing IAP audience;
    - an API base that is not `https://api.devrev.ai` unless an explicit non-production override is enabled.
11. Every numeric/string/list bound equals the canonical master-plan table.
12. `legacy_type` and `observation_type` serialize independently.
13. `If-Match` helpers accept only quoted `"vN"` values and distinguish
    missing (`428`) from stale (`412`) at the API mapping layer.
14. Review, remediation-batch, and import state transitions match their closed
    tables; terminal review resolution requires structured verification or a
    no-change reason.
15. The staged-scope helper exits non-zero for one extra file and handles
    spaces/NUL-delimited Git paths.
16. Durable `TicketReview` has no DevRev title field; a title containing a
    synthetic email/phone can exist only in live/cache models.
17. Batch parent/items round-trip separately and worst-case bounded item
    serialization cannot create a 1 MiB parent document.
18. Console, evidence-broker, and producer-correlation settings reject secret
    fields that belong to another service boundary.
19. The cursor AEAD key decodes to exactly 32 bytes; authenticated-encryption
    round trips hide plaintext cursor fields and reject nonce/tag/context
    tampering. The already pinned runtime lock must contain the approved AEAD
    implementation; do not add an ad hoc cipher.

Run:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_models.py \
  tests/test_verify_staged_scope.py -q
```

Expected before implementation: collection/import failure naming the missing new modules.

## Step 2 — Implement closed domain types

In `api/ticket_review_models.py`, use `ConfigDict(extra="forbid")` throughout and aware UTC datetimes.

Required types:

- `ReviewStatus`
- `ObservationType`
- `Severity`
- `RemediationTarget`
- `CorrelationStatus`
- `CorrelationTrust`
- `ImportState`
- `ReviewerRole` (`viewer | reviewer | remediator | admin | agent`)
- `ReviewerIdentity`
- `DevRevActor`
- `DevRevTicketSummary`
- `DevRevTicketDetail`
- `DevRevTimelineEntry`
- `RagProvenance`
- `ObservedChunkRef` (existing vector ID + article/content hash + ordinal +
  namespace; do not redefine Pinecone IDs)
- `EvidenceLink`
- `VerificationEvidence`
- `ReviewResolution`
- `TicketReview`
- `ReviewPatch`
- `AuditEvent`
- `RemediationBatchItem` containing frozen `review_id` + `review_version`
- `BatchStatus`, `BatchLease`, `BatchOutcome`
- `RemediationBatch`
- `ImportStatus`, `TicketImport`, `TicketImportRow`
- `CursorPage[T]`
- request/response envelopes needed by the master API table

Use a helper:

```python
def review_id_for_devrev_work(devrev_work_id: str) -> str:
    normalized = devrev_work_id.strip()
    if not normalized:
        raise ValueError("devrev_work_id is required")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

Do not store full raw DevRev payloads inside these public API models. Preserve unknown remote fields only in a deliberately bounded diagnostic structure, never by setting `extra="allow"`.

Required review lifecycle rule function:

```text
unreviewed -> reviewed | blocked
reviewed -> triaged | blocked | wont_fix
triaged -> planned | blocked | wont_fix
planned -> in_progress | blocked | wont_fix
in_progress -> changes_proposed | blocked
changes_proposed -> verifying | in_progress | blocked
verifying -> resolved | in_progress | blocked
blocked -> triaged | planned | in_progress | wont_fix
resolved and wont_fix are terminal unless an admin explicitly reopens to triaged
```

Encode this as a tested pure function. Do not scatter transition checks across routes.

Also encode and test:

- the exact `RemediationBatch` lifecycle and lease/heartbeat invariants in the
  master plan;
- the exact `TicketImport` lifecycle and version-checked reversal;
- `assigned_reviewer` self-assignment/admin-reassignment rules separately from
  the authenticated audit actor;
- a terminal review resolution transaction that accepts only a closed
  `ReviewResolution`.

Use the canonical limits/defaults table from the master plan; do not invent
per-file bounds.

## Step 3 — Implement isolated console settings

`TicketConsoleSettings` must use an environment prefix such as `TICKETS_` and must not import or instantiate the main RAG `settings` singleton.

Minimum configuration:

```text
TICKETS_ENABLED
TICKETS_ENVIRONMENT
TICKETS_AUTH_MODE=iap|local
TICKETS_ALLOW_LOCAL_AUTH
TICKETS_ENABLE_SYNTHETIC_VERIFICATION
TICKETS_ALLOW_UNBOUND_VIEWERS
TICKETS_IAP_AUDIENCE
TICKETS_ALLOWED_EMAIL_DOMAINS
TICKETS_ROLE_BINDINGS_JSON
TICKETS_DEFAULT_ROLE
TICKETS_CSRF_SIGNING_SECRET
TICKETS_CSRF_TOKEN_TTL_S
TICKETS_GCP_PROJECT
TICKETS_FIRESTORE_DATABASE
TICKETS_CURSOR_AEAD_KEY
TICKETS_EVIDENCE_BROKER_URL
TICKETS_EVIDENCE_BROKER_AUDIENCE
TICKETS_DEVREV_API_BASE
TICKETS_DEVREV_TOKEN
TICKETS_DEVREV_ORG_SLUG
TICKETS_DEVREV_TICKET_URL_TEMPLATE
TICKETS_DEVREV_AI_AUTHOR_IDS
TICKETS_DEVREV_SYSTEM_AUTHOR_IDS
TICKETS_DEVREV_HUMAN_AUTHOR_IDS
TICKETS_DEVREV_ALLOWED_PART_DONS
TICKETS_DEVREV_ALLOWED_TICKET_VISIBILITY_IDS
TICKETS_DEVREV_ALLOWED_TIMELINE_VISIBILITIES
TICKETS_DEVREV_VERSION
TICKETS_DEVREV_TIMEOUT_S
TICKETS_DEVREV_MAX_RETRIES
TICKETS_DEVREV_MAX_RESPONSE_BYTES
TICKETS_DEVREV_MAX_PAGES
TICKETS_DEVREV_PAGE_SIZE
TICKETS_CACHE_TTL_S
TICKETS_MESSAGE_CACHE_TTL_S
TICKETS_IDEMPOTENCY_TTL_S
TICKETS_IMPORT_STAGING_TTL_S
TICKETS_REVIEW_RETENTION_DAYS
TICKETS_AUDIT_RETENTION_DAYS
TICKETS_RETENTION_JOB_ENABLED
TICKETS_MAX_TIMELINE_ENTRIES
TICKETS_MAX_CSV_BYTES
TICKETS_MAX_JSON_BYTES
TICKETS_MAX_CSV_ROWS
TICKETS_MAX_BATCH_REVIEWS
TICKETS_REMEDIATION_LEASE_S
TICKETS_REMEDIATION_HEARTBEAT_S
TICKETS_REMEDIATION_MAX_CONTINUOUS_LEASE_S
TICKETS_REPO_ID
TICKETS_EXPECTED_BASE_REF
TICKETS_AGENT_SERVICE_ACCOUNT
TICKETS_AGENT_IAP_TARGET_AUDIENCE
```

`EvidenceBrokerSettings` is a separate class/entrypoint and contains only its
database ID, console caller/audience, response limits, and
`TICKETS_CORRELATION_LOOKUP_KEYRING_JSON` plus the allowed key-version set.
`ProducerCorrelationSettings` is separate from both and contains only the
ingress-verification key/version plus the current lookup key/version. The
external n8n producer receives only its ingress-signing key/version. The
console must fail validation if either correlation key is injected into its
revision; the broker must reject an ingress key, and n8n/producer identities
must never receive the broker keyring.

Rules:

- Defaults must be safe for unit tests and local development.
- `TICKETS_DEFAULT_ROLE=None` and
  `TICKETS_ALLOW_UNBOUND_VIEWERS=false`; an unbound identity is denied.
- The token is a runtime secret injected by Cloud Run; never read Secret Manager directly from application code.
- `TICKETS_DEVREV_VERSION` defaults to `2022-10-20`.
- Page sizes, retries, timeouts, leases, text/list/request caps, and TTLs use
  the canonical master-plan values exactly.
- DevRev successful bodies are streamed and capped at the canonical byte limit
  before JSON parsing.
- A URL template is optional. When absent, the UI must copy the display ID and open the configured DevRev organization root; it must not invent a deep-link pattern.
- Provide a pure `validate_ticket_console_settings(settings)` called by app startup.
- Staging/production reject empty DevRev part/visibility allowlists, the
  `(default)` database, local auth, unbound viewers, wildcard domains, missing
  secrets/broker audiences, and non-HTTPS URLs.

## Step 4 — Add sanitized fixtures

Build fixtures from the public DevRev response shapes and synthetic names/text only. Never capture a real participant, employee, email, ticket body, PAT, DON tenant segment, or attachment URL.

The timeline fixtures must prove the documented edge case:

- page 1 has `timeline_entries: []` and a non-empty `next_cursor`;
- page 2 has entries and no `next_cursor`.

Include at least:

- participant/external comment;
- internal human-agent comment;
- configured AI/system author comment;
- change event;
- an unknown timeline entry type that normalization safely marks unsupported without crashing.

## Step 5 — Document the locked ADR

`Development Docs/TICKETS_REVIEW_ARCHITECTURE.md` must record:

- separate admin plane vs RAG data plane;
- why existing `X-API-Key`/localStorage is rejected;
- signed IAP JWT + app RBAC;
- CSRF/origin/content-type/idempotency and quoted ETag semantics;
- DevRev read-only MVP;
- dedicated named Firestore database, no direct human/agent access, and a
  read-only evidence broker for production `(default)`;
- Firestore as structured review/audit store, not a blind raw mirror;
- live DevRev list vs Firestore review queue;
- no fabricated RAG provenance;
- verified n8n/HMAC correlation contract and the explicit external-owner gate;
- no webhook/Cloud Tasks in MVP;
- no direct Git/DevRev/Pinecone production mutation from the web service;
- field classification, exact retention/legal-hold decisions, application
  append-only hash-chained audit plus Cloud Audit Logs;
- legacy Type/reviewer separation and the supported query grammar;
- the exact APIs, models, status transitions, and non-goals.

Link only official DevRev and Google Cloud sources listed in the master plan.

Implement `scripts/verify_staged_scope.py` as a read-only Git helper. It
accepts repeated exact `--allow` paths, compares them with the NUL-delimited
staged file list from the repository root, prints unexpected paths to stderr,
and returns 2. It must never stage, unstage, or edit files.

## Step 6 — Verify

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_models.py \
  tests/test_verify_staged_scope.py -q
"$PYTHON_BIN" -m pytest tests/test_handle_ticket_contract.py \
  tests/test_ticket_job_repository.py -q
"$PYTHON_BIN" -m compileall -q api data_pipeline scripts
git -C "$IMPL_ROOT" diff --check
```

Expected: all commands exit 0.

Check fixture hygiene:

```bash
if rg -n -i \
  'bearer|pat[-_ ]|api[_-]?key|@forusall\\.com|TKT-[89][0-9]{5}' \
  tests/fixtures/devrev; then
  echo "STOP: fixture contains a credential/real-data fingerprint" >&2
  exit 1
fi
```

Expected: command exits 0 after `rg` finds no matches. Synthetic display IDs
use the `TKT-1234` range and `example.invalid`.

## Definition of Done

- Clean-base ancestry was proven.
- Strict models and transitions exist with focused tests.
- Console settings are independent of main RAG startup.
- Legacy sheet fields, remediation/import models, limits, ETags, CSRF, and
  state machines are frozen before downstream work.
- All DevRev fixtures are synthetic and include the empty-page-with-cursor edge case.
- ADR freezes architecture and non-goals.
- No network, Firestore, Pinecone, GCP, or DevRev mutation occurred.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/api/tickets_console_config.py \
  kb-rag-system/api/ticket_review_models.py \
  kb-rag-system/tests/test_ticket_review_models.py \
  kb-rag-system/tests/test_verify_staged_scope.py \
  kb-rag-system/tests/fixtures/devrev/works_list_page_1.json \
  kb-rag-system/tests/fixtures/devrev/works_list_page_2.json \
  kb-rag-system/tests/fixtures/devrev/work_get_ticket.json \
  kb-rag-system/tests/fixtures/devrev/timeline_page_empty_with_cursor.json \
  kb-rag-system/tests/fixtures/devrev/timeline_page_final.json \
  kb-rag-system/scripts/verify_staged_scope.py \
  "kb-rag-system/Development Docs/TICKETS_REVIEW_ARCHITECTURE.md"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/api/tickets_console_config.py \
  --allow kb-rag-system/api/ticket_review_models.py \
  --allow kb-rag-system/tests/test_ticket_review_models.py \
  --allow kb-rag-system/tests/test_verify_staged_scope.py \
  --allow kb-rag-system/tests/fixtures/devrev/works_list_page_1.json \
  --allow kb-rag-system/tests/fixtures/devrev/works_list_page_2.json \
  --allow kb-rag-system/tests/fixtures/devrev/work_get_ticket.json \
  --allow kb-rag-system/tests/fixtures/devrev/timeline_page_empty_with_cursor.json \
  --allow kb-rag-system/tests/fixtures/devrev/timeline_page_final.json \
  --allow kb-rag-system/scripts/verify_staged_scope.py \
  --allow "kb-rag-system/Development Docs/TICKETS_REVIEW_ARCHITECTURE.md"
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit -m "feat(tickets): define review console contracts"
```

If dependency inputs/locks were legitimately changed, stage those exact files too. Then proceed to Stage 2.
