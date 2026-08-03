# Stage 9 — Safe Sheet CSV Migration and Review Export

> **For Claude Opus 5:** This is an executable implementation prompt. Build a reversible, dry-run-first migration for a Google Sheets CSV export. No real sheet data has been provided; use synthetic fixtures only until the user supplies an export.

**Goal:** Import the existing columns (`Ticket ID`, `Topic`, `Type`, `Rating`, `Reviewer`, `Comments`) into durable reviews without clobbering newer work, and export the review queue back to CSV for audit/escrow.

**Architecture:** A shared migration service powers bounded, resumable admin
API operations. Both browser and CLI call that API; neither receives Firestore
access. Dry-run parses/validates without business writes; apply resolves each
display ID through DevRev, then performs version-aware fill-empty merges.
Reversal is a version-checked compensating patch, never history deletion.

**Tech Stack:** Python `csv`, DevRev reader, Firestore repository, FastAPI upload, pytest.

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
"$PYTHON_BIN" -m pytest tests/test_ticket_review_repository.py \
  tests/test_devrev_client.py \
  tests/test_ticket_review_routes.py -q
```

## Files

Create:

- `kb-rag-system/data_pipeline/ticket_review_migration.py`
- `kb-rag-system/scripts/import_ticket_reviews_csv.py`
- `kb-rag-system/tests/test_ticket_review_migration.py`
- `kb-rag-system/tests/test_ticket_review_import_export_routes.py`
- `kb-rag-system/tests/fixtures/ticket_reviews_sheet_synthetic.csv`

Modify:

- `kb-rag-system/api/ticket_review_routes.py`
- `kb-rag-system/api/ticket_review_models.py`
- `kb-rag-system/ui/tickets/app.js`
- `kb-rag-system/ui/tickets/api.js`
- `kb-rag-system/ui/tickets/render.js`
- `kb-rag-system/ui/tickets/index.html`
- relevant tests

## Step 1 — Write failing CSV parser tests

Synthetic fixture rows must cover:

- integer ratings `1`–`5`;
- star strings such as `★★☆☆☆` and `★★★★☆`;
- blank optional cells;
- multiline comments;
- Unicode;
- duplicate ticket IDs;
- unknown/bounded legacy Type and topic;
- malformed rating;
- formula-looking values with spaces, tabs, CR/LF, or control characters before
  `=`, `+`, `-`, or `@`;
- extra columns;
- missing required headers;
- BOM and common UTF-8 CSV line endings.

Required behavior:

1. Headers are trimmed/case-normalized but mapped only through an explicit alias table.
2. `Ticket ID` is required and must match a bounded display-ID pattern.
3. Rating parser accepts only documented integer/star forms.
4. Comments remain text and never execute/interpret formulas.
5. `Type` always maps to `legacy_type`; it never silently becomes the new
   `observation_type`. A separate explicit/versioned alias map may suggest an
   observation with a warning, but the admin must confirm it.
6. Duplicate IDs produce a deterministic row-level conflict; do not silently take last-write-wins.
7. File byte size, rows, columns, cell length, and total comment bytes are bounded.
8. Dry-run performs no repository or DevRev write.
9. CSV error reports contain row number, field, safe code, and bounded message—not the whole row.
10. Export formula detection examines the first effective character after
    Unicode whitespace/control prefixes and spreadsheet-significant tab/CR/LF;
    round-trip tests cover Sheets-style parsing.

Run:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_migration.py \
  tests/test_ticket_review_import_export_routes.py -q
```

## Step 2 — Implement dry-run parser

Canonical legacy mapping:

| Sheet column | Review field |
|---|---|
| Ticket ID | DevRev display ID, later resolved to DON |
| Topic | `topic` |
| Type | `legacy_type` |
| Rating | `rating` |
| Reviewer | import attribution metadata only; authenticated applying admin remains audit actor |
| Comments | `comments` |

Never treat the CSV reviewer name as authenticated identity. Store it under something like `legacy_reviewer_display_name`/import metadata while the applying IAP admin is the audit actor.

`observation_type` is a separate new column in exports and an optional new
column in later imports; it is never inferred silently from legacy `Type`.

Formula injection:

- internal storage preserves the literal bounded text;
- CSV export prefixes a cell when its first effective character—after any
  whitespace/control/tab/CR/LF prefix—is `=`, `+`, `-`, or `@`; the exact
  single-quote encoding and round trip are tested;
- UI renders text only.

## Step 3 — Implement import plan and apply

Dry-run output:

```json
{
  "import_id": "content hash / generated id",
  "file_sha256": "...",
  "rows_total": 20,
  "rows_valid": 18,
  "rows_invalid": 2,
  "would_create": 10,
  "would_fill_empty": 6,
  "would_skip_unchanged": 2,
  "conflicts": [],
  "errors": []
}
```

Apply rules:

1. Dry-run accepts strict `text/csv; charset=utf-8`, CSRF, idempotency, and the
   canonical size/row caps. It stores bounded parsed staging rows for seven
   days, not the raw CSV body.
2. Apply requires admin, the same file SHA-256, dry-run import ID, immutable
   plan hash, explicit approval flag, and a new idempotency key.
3. Default merge strategy is `fill_empty`.
4. Resolve each `TKT-*` through `works.get` to obtain the scoped DevRev DON and object version before creating a review.
5. Bound DevRev concurrency and honor the client’s rate-limit/retry policy.
6. Unresolvable/out-of-scope tickets remain row errors; do not create a fake review.
7. Existing non-empty rating/topic/legacy_type/comments are not overwritten by default.
8. Optional overwrite is CLI-only, requires expected versions and a second
   explicit confirmation flag, and produces per-field audit events.
9. Import writes are idempotent by file hash + row hash.
10. Apply processes at most 100 rows per request and returns a signed resume
    cursor; UI/CLI repeats `POST /imports/{import_id}:apply`.
11. A partial import reports completed/failed/conflicted rows; never claim
    atomic all-or-nothing across a large CSV.
12. Reversal uses `POST /imports/{import_id}:reverse`, max 100 rows/request,
    the original row before-image and expected current version. It:
    - restores only fields still at the imported version;
    - marks newly created reviews `import_state=reversed` instead of deleting;
    - leaves later-edited rows conflicted and visible;
    - appends row/global audit events;
    - is idempotent and resumable.
13. In staging synthetic-verification mode only, dry-run/apply/export/reverse
    consume and advance the server-signed role/phase handoff without placing
    CSV content in it; production rejects the verification headers.

For the HTTP endpoint, cap synchronous apply to a conservative row count. Larger imports must use the CLI so Cloud Run request timeouts are not a data-consistency mechanism.

Run route tests red before adding the endpoints:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_import_export_routes.py -q
```

## Step 4 — Implement CLI

Commands:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" scripts/import_ticket_reviews_csv.py \
  tests/fixtures/ticket_reviews_sheet_synthetic.csv \
  --console-url http://127.0.0.1:8010 \
  --environment local --repo-id synthetic-repo --dry-run
```

Requirements:

- dry-run default;
- explicit console URL/environment/repo ID;
- reuse the keyless IAP API client from `ticket_review_cli.py`; never
  instantiate Firestore or accept project/database/collection paths;
- progress reports use ticket display ID/row only, no comment contents;
- apply/reverse loop over signed resume cursors and stop on conflict;
- exact stable exit codes from the remediation CLI;
- output JSON option for automation;
- no real CSV committed to the repository.

Add explicit `--apply --import-id --plan-sha256
--confirm-approved-plan-sha256` and `--reverse --import-id
--confirm-reverse` modes. Never include a production apply example in this
stage.

## Step 5 — Add admin UI

Admin-only `Import CSV` dialog:

- file chooser;
- expected columns/help;
- dry-run first;
- table of counts/errors/conflicts;
- apply disabled until dry-run succeeds and confirmation is checked;
- no browser parsing as source of truth;
- no file contents persisted in browser storage;
- large-file message points to CLI;
- refreshes review queue after successful apply.

## Step 6 — Implement safe export

`GET /api/admin/v1/exports/ticket-reviews.csv`:

- admin only;
- bounded filters/date range;
- server streams rows with fixed headers;
- includes DevRev ID, six sheet fields, new workflow fields, remediation summary, versions/timestamps;
- never includes raw timeline messages, participant email, DevRev DON unless explicitly requested for admin audit, tokens, IAP claims, or chunk contents;
- formula-safe cells;
- UTF-8 BOM only if a test shows it is needed for Sheets compatibility;
- `Content-Disposition` has a server-generated safe filename;
- `ticket_exports/{export_id}` and `ticket_console_audit_events` record
  actor/filter/count/file SHA-256, not CSV contents.

## Step 7 — Verify

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_migration.py \
  tests/test_ticket_review_import_export_routes.py \
  tests/test_ticket_review_routes.py \
  tests/test_ticket_review_repository.py \
  tests/test_tickets_ui_contract.py -q
"$PYTHON_BIN" scripts/import_ticket_reviews_csv.py \
  tests/fixtures/ticket_reviews_sheet_synthetic.csv \
  --console-url http://127.0.0.1:8010 \
  --environment local --repo-id synthetic-repo --offline-parse-only --dry-run
"$PYTHON_BIN" -m compileall -q data_pipeline scripts api
git -C "$IMPL_ROOT" diff --check
```

Expected CLI dry-run: deterministic synthetic counts and zero external writes.

Scan the synthetic fixture for real data:

```bash
if rg -n -i '@forusall\\.com|TKT-[89][0-9]{5}' \
    tests/fixtures/ticket_reviews_sheet_synthetic.csv; then
  echo "STOP: synthetic CSV contains a real-data fingerprint" >&2
  exit 1
fi
```

Expected: no matches.

## Definition of Done

- The spreadsheet can be exported as CSV and migrated dry-run-first.
- Real DevRev DONs are resolved server-side before review creation.
- Existing reviews are not silently overwritten.
- Partial/resumable imports and compensating reversal are auditable.
- Export is formula-safe, bounded, and excludes conversations/credentials.
- No real sheet data was committed or imported during implementation.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/data_pipeline/ticket_review_migration.py \
  kb-rag-system/scripts/import_ticket_reviews_csv.py \
  kb-rag-system/tests/test_ticket_review_migration.py \
  kb-rag-system/tests/test_ticket_review_import_export_routes.py \
  kb-rag-system/tests/fixtures/ticket_reviews_sheet_synthetic.csv \
  kb-rag-system/api/ticket_review_routes.py \
  kb-rag-system/api/ticket_review_models.py \
  kb-rag-system/ui/tickets/app.js \
  kb-rag-system/ui/tickets/api.js \
  kb-rag-system/ui/tickets/render.js \
  kb-rag-system/ui/tickets/index.html \
  kb-rag-system/tests/test_ticket_review_routes.py \
  kb-rag-system/tests/test_ticket_review_repository.py \
  kb-rag-system/tests/test_tickets_ui_contract.py
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/data_pipeline/ticket_review_migration.py \
  --allow kb-rag-system/scripts/import_ticket_reviews_csv.py \
  --allow kb-rag-system/tests/test_ticket_review_migration.py \
  --allow kb-rag-system/tests/test_ticket_review_import_export_routes.py \
  --allow kb-rag-system/tests/fixtures/ticket_reviews_sheet_synthetic.csv \
  --allow kb-rag-system/api/ticket_review_routes.py \
  --allow kb-rag-system/api/ticket_review_models.py \
  --allow kb-rag-system/ui/tickets/app.js \
  --allow kb-rag-system/ui/tickets/api.js \
  --allow kb-rag-system/ui/tickets/render.js \
  --allow kb-rag-system/ui/tickets/index.html \
  --allow kb-rag-system/tests/test_ticket_review_routes.py \
  --allow kb-rag-system/tests/test_ticket_review_repository.py \
  --allow kb-rag-system/tests/test_tickets_ui_contract.py
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "feat(tickets): add safe sheet migration and export"
```

Proceed to Stage 10.
