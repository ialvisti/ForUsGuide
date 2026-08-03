# Stage 7 — Ticket Detail, Conversation, RAG Evidence, and Evaluation UI

> **For Claude Opus 5:** Execute this stage on the Stage 6 UI. Preserve its security and accessibility rules. Implement the full review workspace, not a visual mock.

**Goal:** Let a reviewer understand the ticket, see participant/human/AI responses and available RAG evidence, score the outcome, document expected behavior/root cause, and resolve edit conflicts.

**Architecture:** `/tickets/{display_id}` and in-app navigation load bounded
metadata first, then independently cursor-page conversation, audit history, and
evidence links. A focused workspace uses explicit save with quoted ETags; user
edits survive a precondition failure until the reviewer reconciles them.

**Tech Stack:** Vanilla ES modules, semantic HTML/CSS, Stage 5 API, pytest UI contract tests.

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
"$PYTHON_BIN" -m pytest tests/test_tickets_ui_contract.py \
  tests/test_ticket_review_service.py \
  tests/test_ticket_review_routes.py -q
```

## Files

Create:

- `kb-rag-system/ui/tickets/detail.js`
- `kb-rag-system/ui/tickets/evaluation.js`
- `kb-rag-system/ui/tickets/conversation.js`
- `kb-rag-system/ui/tickets/evidence.js`
- `kb-rag-system/ui/tickets/remediation.js`
- `kb-rag-system/tests/test_ticket_detail_ui_contract.py`
- `kb-rag-system/tests/test_ticket_detail_browser_contract.py`

Modify:

- `kb-rag-system/ui/tickets/index.html`
- `kb-rag-system/ui/tickets/tickets.css`
- `kb-rag-system/ui/tickets/app.js`
- `kb-rag-system/ui/tickets/api.js`
- `kb-rag-system/ui/tickets/state.js`
- `kb-rag-system/ui/tickets/render.js`
- backend response models/routes only for a proven missing field with tests

## Step 1 — Write failing detail contract tests

Assert source/served HTML includes:

1. Detail heading and breadcrumb/back action.
2. Ticket metadata with display ID, title, stage, owner, created/updated, severity/channel, and safe DevRev action.
3. Tabs/panels: Conversation, RAG evidence, Review history, Remediation.
4. Evaluation fields:
   - Topic;
   - Legacy Type and separate Observation type;
   - Rating 1–5;
   - Assigned reviewer plus labeled legacy reviewer fallback;
   - Comments;
   - Expected behavior;
   - Severity;
   - Remediation target;
   - Status.
5. Character counts and max lengths match the canonical master limits.
6. Rating uses a real radio group or equivalent accessible control, not decorative Unicode alone.
7. Conversation items expose author class, author, timestamp, visibility, body, and reply relation.
8. Evidence states: linked, manual, candidate, unavailable, loading, partial, error.
9. Save, cancel/reset, reload conflict, add evidence link, add to batch actions have role-aware disabled states.
10. No injection sinks or persistent storage regressions.
11. Conversation, audit events, and evidence links each have independent
    loading/partial/error/cursor states and accessible `Load more`.

Run:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_detail_browser_contract.py -q
```

## Step 2 — Implement detail navigation and hydration

Support:

- direct `/tickets/TKT-123` load;
- list row navigation without full application reload when History API is available;
- browser back/forward;
- aborting the previous detail request when another row opens;
- retry of partial DevRev failures;
- safe not-found and permission-denied states.

Do not accept/execute an arbitrary URL from query parameters.

DevRev deep link:

- use only the server-validated `devrev_url` if present;
- enforce `https:` and the configured allowed host;
- otherwise show `Copy Ticket ID` and `Open DevRev home`;
- do not infer a permalink from `orgSlug` unless the operator configured and verified `TICKETS_DEVREV_TICKET_URL_TEMPLATE`.

## Step 3 — Render conversation

Conversation display:

- page-aware chronological ordering with an explicit `Load more`; never claim
  all history is loaded while `next_cursor` exists;
- clear participant, human agent, AI/system, internal event, and unknown labels;
- visibility badge on every entry;
- internal/private entries visually distinct and never labeled participant-facing;
- body text preserves paragraphs but not remote markup;
- large entries collapsed with an accessible expand control;
- attachments show bounded metadata and a safe “Open in DevRev” path rather than proxying unknown URLs;
- change events summarized separately from messages;
- unknown entry/body types show a safe fallback and diagnostic ID, not raw JSON.

Provide filters:

- all;
- participant-facing;
- internal;
- AI/system;
- human agent;
- events.

## Step 4 — Render RAG evidence honestly

For each linked execution show only available fields:

- correlation status/reason;
- internal ticket job ID and request/trace hashes (copyable, bounded);
- never display raw external
  identifiers;
- endpoint/route;
- model/provider;
- prompt template ID/static hash/config version; rendered hash only as a trace;
- deployed revision/commit;
- index/namespace and explicit unknown index version;
- source article IDs;
- observed vector ID/type/tier/score/article ID/content hash/ordinal, labeled
  as observed rather than stable across reindexing;
- response hash;
- timestamp/latency/error.

Never show full chunk content from the provenance collection. If a reviewer is authorized to inspect a KB source, link to a separate bounded server endpoint or show the checked-in article identifier, not a browser Pinecone query.

When unavailable, say why:

> This ticket predates reliable ticket-to-RAG correlation, or its legacy execution did not include a DevRev identifier. DevRev conversation is available; retrieval/prompt provenance cannot be reconstructed reliably.

Candidate links must require explicit reviewer confirmation and a reason. Confirmation produces a manual link and audit event.

## Step 5 — Implement evaluation form

Behavior:

- Authenticated actor is shown separately from assignment. Reviewers may
  self-assign; admins may choose an exact configured identity; imported
  reviewer text remains a labeled legacy fallback.
- A ticket can be viewed before import; first save imports it idempotently and then applies the review.
- Do not autosave long comments.
- Dirty state is visible and triggers an accessible navigation warning.
- Save sends quoted `If-Match: "vN"`, CSRF token, and a fresh idempotency key.
- Successful save updates version/ETag and clears dirty state.
- `412` opens a stale-version conflict panel; `409` is only a state/lease
  business conflict and `428` means a client bug/missing precondition. The
  stale panel contains:
  - local unsaved values;
  - safe current server values;
  - changed field names/version/timestamp;
  - `Reload server`, `Keep editing`, and admin-only explicit overwrite/reapply flow.
- Never silently retry a stale patch against the new version.
- Transition options are limited to valid next states returned/calculated from the closed transition table.
- `resolved` requires verification summary, tests/evidence, and a linked remediation or documented `no_change` rationale.

Rating guidance visible in the UI:

- 1: unsafe/incorrect;
- 2: major correction required;
- 3: partially useful;
- 4: correct with minor improvement;
- 5: correct, complete, and appropriately scoped.

## Step 6 — Review history and audit

Fetch the cursor-paginated audit endpoint and render application-append-only,
hash-chained audit events as a timeline:

- event type;
- actor;
- timestamp;
- version change;
- changed field labels;
- bounded reason/commit/test metadata.
- visible integrity warning if chain validation fails.

Do not reconstruct old comment contents if audit design intentionally stores only changed-field names.
Load evidence links from their own paginated endpoint and unlink only with the
current ETag plus a required reason.

## Step 7 — Local functional verification

Execute the Stage 6 `tests.support.tickets_fixture_server` start/status blocks
literally, record its printed `FIXTURE_STATE_DIR`, and execute its exact
stop/status/empty-directory block even after a failure. With those synthetic
scenarios, use browser tooling to verify:

- direct deep-link and back button;
- conversation filter;
- multiple conversation/audit/evidence pages, empty page with next cursor, and
  partial warning;
- rating keyboard navigation;
- validation and character counts;
- successful first import/save;
- stale version conflict without data loss;
- manual evidence confirmation;
- versioned evidence unlink with reason;
- unavailable evidence explanation;
- viewer read-only behavior;
- reviewer edit behavior;
- 360×800 and desktop layouts.

Inspect browser console and network tab: no remote CDN, token, API key, raw IAP JWT, or cross-origin request.

## Step 8 — Automated verification

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_detail_browser_contract.py \
  tests/test_tickets_ui_contract.py \
  tests/test_ticket_review_routes.py \
  tests/test_ticket_review_service.py -q
"$PYTHON_BIN" -m compileall -q api data_pipeline
git -C "$IMPL_ROOT" diff --check
```

```bash
if rg -n \
  'innerHTML|outerHTML|insertAdjacentHTML|document\\.write|eval\\(|localStorage|sessionStorage|X-API-Key|Bearer ' \
  ui/tickets; then
  echo "STOP: unsafe browser pattern" >&2
  exit 1
fi
```

Expected: no unsafe matches.

## Definition of Done

- A reviewer can reach a complete, understandable review workspace from `/tickets`.
- Participant, human, AI/system, internal, and event entries are not conflated.
- RAG evidence and historical evidence gaps are explicit.
- All six sheet fields are preserved; assignment/legacy reviewer and
  legacy/observation Type remain distinct under RBAC.
- Concurrent reviewers cannot silently overwrite one another.
- Mobile/keyboard/error/partial/read-only flows work.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/ui/tickets/detail.js \
  kb-rag-system/ui/tickets/evaluation.js \
  kb-rag-system/ui/tickets/conversation.js \
  kb-rag-system/ui/tickets/evidence.js \
  kb-rag-system/ui/tickets/remediation.js \
  kb-rag-system/ui/tickets/index.html \
  kb-rag-system/ui/tickets/tickets.css \
  kb-rag-system/ui/tickets/app.js \
  kb-rag-system/ui/tickets/api.js \
  kb-rag-system/ui/tickets/state.js \
  kb-rag-system/ui/tickets/render.js \
  kb-rag-system/tests/test_ticket_detail_ui_contract.py \
  kb-rag-system/tests/test_ticket_detail_browser_contract.py
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/ui/tickets/detail.js \
  --allow kb-rag-system/ui/tickets/evaluation.js \
  --allow kb-rag-system/ui/tickets/conversation.js \
  --allow kb-rag-system/ui/tickets/evidence.js \
  --allow kb-rag-system/ui/tickets/remediation.js \
  --allow kb-rag-system/ui/tickets/index.html \
  --allow kb-rag-system/ui/tickets/tickets.css \
  --allow kb-rag-system/ui/tickets/app.js \
  --allow kb-rag-system/ui/tickets/api.js \
  --allow kb-rag-system/ui/tickets/state.js \
  --allow kb-rag-system/ui/tickets/render.js \
  --allow kb-rag-system/tests/test_ticket_detail_ui_contract.py \
  --allow kb-rag-system/tests/test_ticket_detail_browser_contract.py
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "feat(tickets): add evidence-based review workspace"
```

The Stage 5 API already owns the required paginated subresources. If it does
not, stop and repair Stage 5 with red route tests before proceeding. Do not
silently change backend contracts in this UI commit. Proceed to Stage 8.
