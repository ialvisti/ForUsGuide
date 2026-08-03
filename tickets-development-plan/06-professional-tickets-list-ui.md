# Stage 6 — Professional `/tickets` List and Review Queue UI

> **For Claude Opus 5:** Execute this UI stage against the real Stage 5 API contract. Do not redesign the backend or add a frontend framework.

**Goal:** Replace the spreadsheet’s day-to-day list workflow with a polished, accessible, responsive ticket table and durable review queue at `/tickets`.

**Architecture:** Use a small no-build ES-module frontend with one state store and one API adapter. Render all remote/user strings through DOM text APIs, keep filters in the URL, and keep credentials/PII out of browser persistence.

**Tech Stack:** Semantic HTML, CSS, vanilla JavaScript ES modules, FastAPI static serving, pytest contract checks.

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
"$PYTHON_BIN" -m pytest tests/test_ticket_review_routes.py \
  tests/test_tickets_console_app.py -q
```

Inspect the existing UI visual tokens in `ui/index.html` and `ui/router.html`, but do not copy their inline-script, inline-style, API-key, or `localStorage` patterns.

## Files

Create:

- `kb-rag-system/ui/tickets/index.html`
- `kb-rag-system/ui/tickets/tickets.css`
- `kb-rag-system/ui/tickets/app.js`
- `kb-rag-system/ui/tickets/api.js`
- `kb-rag-system/ui/tickets/state.js`
- `kb-rag-system/ui/tickets/render.js`
- `kb-rag-system/ui/tickets/icons.svg`
- `kb-rag-system/tests/test_tickets_ui_contract.py`
- `kb-rag-system/tests/support/tickets_console_fixture_app.py`
- `kb-rag-system/tests/support/tickets_fixture_server.py`
- `kb-rag-system/tests/test_tickets_fixture_app.py`
- `kb-rag-system/tests/test_tickets_fixture_server.py`

Modify:

- `kb-rag-system/api/tickets_console_main.py` to serve the real files and assets.

Do not add CDN dependencies, remote fonts, inline event handlers, inline scripts, or inline styles.

## Step 1 — Write failing static/UI contract tests

Tests should parse the HTML and inspect JS/CSS sources.

Assert:

1. `/tickets` returns HTML; `/tickets/assets/*` has correct MIME types.
2. HTML has one `main`, real headings in order, a skip link, live-region, table caption, and labeled controls.
3. Required sheet columns appear: Ticket ID, Topic, Legacy Type, Rating,
   Reviewer, Comments; Observation remains a distinct added column.
4. Assigned reviewer and legacy reviewer fallback are not conflated with the
   authenticated session actor.
5. Scripts use `type="module"` and are same-origin.
6. No source uses:
   - `localStorage` or `sessionStorage` for tickets/auth;
   - `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, or `eval`;
   - hardcoded API key, DevRev token, participant email, or screenshot ticket ID;
   - inline `onclick`/event attributes.
7. CSP emitted by the app can load every asset without `'unsafe-inline'`.
8. CSS contains visible focus rules, reduced-motion handling, responsive breakpoints, and non-color status cues.
9. The table has an accessible card alternative/transform below the canonical
   768 px breakpoint.
10. Every icon-only button has an accessible name.

Run and observe failures:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_tickets_ui_contract.py \
  tests/test_tickets_fixture_app.py \
  tests/test_tickets_fixture_server.py -q
```

## Step 2 — Implement visual system and shell

Use a restrained professional palette aligned with the current indigo/slate product, not a replica of Google Sheets.

Required regions:

- skip link;
- header with product name, environment badge, authenticated user/role, health/sync state;
- KPI strip;
- two tabs: `All DevRev tickets` and `Review queue`;
- filter/search toolbar;
- bulk-selection action bar;
- data region;
- cursor pagination;
- global toast/live region;
- detail navigation target (Stage 7).

KPI labels:

- Unreviewed;
- Rating 1–2;
- High/Critical;
- Active remediation.

If the backend cannot provide an accurate global KPI yet, show `—` with a tooltip; never calculate a page count and label it global.

## Step 3 — Implement state and API adapter

`api.js`:

- only relative same-origin URLs;
- `credentials: "same-origin"`;
- request ID handling;
- JSON/error-envelope parsing;
- fetches `/session` once, keeps the CSRF token in memory, adds
  `X-CSRF-Token`/UUID `Idempotency-Key` to unsafe requests, and sends quoted
  ETags for versioned writes;
- never attempts to set the forbidden `Origin` header in JavaScript; the
  browser supplies it automatically, and integration tests prove the server
  accepts the correct browser origin and rejects missing/mismatched origins;
- sends only server-wrapped pagination tokens in `X-Tickets-Cursor`, never in
  a URL/query parameter;
- maps 401/403/409/412/428/429/502/503 to user-safe typed UI errors;
- honors `Retry-After` by disabling refresh and showing countdown;
- uses `AbortController` to cancel stale list/detail requests.

`state.js`:

- one immutable-ish state object/reducer;
- modes: `devrev` and `reviews`;
- loading/refreshing/partial/stale/error states;
- filters and cursor;
- selected row IDs;
- no ticket bodies or comments written to persistent browser storage;
- URL query string stores only non-sensitive filters and selected display ID.

Keep console cursor tokens and the back-stack in memory only. Send the current
token in `X-Tickets-Cursor`; never put a remote or wrapped cursor in the URL,
browser history state, analytics, storage, or log output. Reload intentionally
returns to the first page for the URL's non-sensitive filters.

## Step 4 — Implement filters

All DevRev:

- exact Ticket ID lookup;
- stage/state;
- source channel/subtype where supported;
- visibility ID from the server-provided allowed set;
- created/modified date;
- refresh.

Owner/creator/reporter/tag name lookups are not in MVP and must not appear as
half-working controls. Exact ID adapter support remains backend-only until the
lookup feature gate is approved.

Review queue:

- exact normalized Ticket ID lookup as a standalone mode;
- otherwise status set plus at most one allowed facet and optional updated
  date, exactly as the master query grammar;
- topic;
- observation type;
- rating;
- assigned reviewer;
- severity;
- remediation target;
- updated date;
- no title substring/full-text input.

Show active filter chips and `Clear all`. Debounce only text input; select changes submit immediately. Filter changes reset the cursor.

Disable a second facet in the UI and explain why. If a forged URL asks for an
unsupported combination, render the stable `422` response; do not fall back to
client-side page filtering.

## Step 5 — Render the table safely

Columns:

1. selection checkbox;
2. Ticket ID + bounded live/cache title preview;
3. Topic;
4. Legacy Type;
5. Observation;
6. Rating;
7. Assigned reviewer or labeled legacy fallback;
8. Status;
9. Updated;
10. Comments preview;
11. row action.

Requirements:

- sticky header;
- sortable only where backend supports it;
- every sort announces direction;
- skeletons preserve layout;
- errors and empty results replace skeletons;
- row click and keyboard Enter open detail;
- selection checkbox does not trigger row navigation;
- rating stars have textual `N of 5`;
- comment preview is text-only and clamped;
- title attributes are not the only way to access truncated content;
- dates use `<time datetime>` and user locale;
- remote content uses `textContent`/node creation exclusively.

For the live DevRev tab, an unimported ticket shows `Not reviewed` and an `Add to review queue` action. Creating a review requires reviewer role.

## Step 6 — Responsive behavior

At narrow widths:

- hide the desktop table header;
- each row becomes a semantic ticket card with labeled values;
- filters open in an accessible modal/sheet;
- bulk actions remain reachable;
- no horizontal body scroll at 360 px;
- 44 px minimum touch targets.

Respect `prefers-reduced-motion`. Do not animate skeletons indefinitely for reduced-motion users.

## Step 7 — Functional local smoke

Implement `tests.support.tickets_console_fixture_app` with only in-memory
repository, synthetic DevRev/evidence fakes, and deterministic error scenarios.
Importing it must set/verify fixture mode before importing the app factory; it
must fail if ADC, metadata server, real DevRev, or any non-loopback URL is
requested. Never point a browser test at production.

Implement the test-only `tests.support.tickets_fixture_server` runner with
`start | status | stop`. Tests must prove:

- `start` accepts only `127.0.0.1`, an explicit unoccupied port, a fresh
  caller-created `0700` state directory, and `--max-seconds` in `60..1800`;
- it sets fixture/local-auth mode plus invalid ADC/metadata endpoints itself,
  daemonizes only the exact Python child, waits for `/livez`, and returns after
  writing `pid`, random nonce, URL, start time, deadline, and log as individual
  `0600` files;
- `/__fixture/status` exists only in fixture mode and returns that nonce;
- `status` and `stop` verify state-directory ownership/mode, PID, nonce, URL,
  and live fixture endpoint before signalling anything;
- `stop` sends TERM, waits at most ten seconds, then may KILL only the same
  still-verified PID; it removes only its exact files and never uses a glob,
  `pkill`, or process-name match;
- stale PID, PID reuse/nonce mismatch, occupied port, non-loopback bind,
  startup failure, concurrent start, and auto-timeout are safe.

Start it:

```bash
cd "$KBRAG_ROOT"
fixture_state_dir="$(mktemp -d -t tickets-fixture-state.XXXXXX)"
chmod 0700 "$fixture_state_dir"
"$PYTHON_BIN" -m tests.support.tickets_fixture_server start \
  --state-dir "$fixture_state_dir" \
  --host 127.0.0.1 --port 8010 --max-seconds 1800
"$PYTHON_BIN" -m tests.support.tickets_fixture_server status \
  --state-dir "$fixture_state_dir" \
  --expect-url http://127.0.0.1:8010
printf 'FIXTURE_STATE_DIR=%s\n' "$fixture_state_dir"
```

Record the exact printed directory and reuse it literally in the later stop
command; do not assume a shell variable survives a tool call. Use browser
tooling against
`http://127.0.0.1:8010/tickets`, and verify:

- `/tickets` loads without console errors;
- both tabs render fixture/fake data;
- filters update URL/state;
- DevRev forward/back cursors and review-queue cursor stack work;
- 401, 429, empty, partial, and 503 states are intelligible;
- keyboard reaches every control in logical order;
- 360×800, 768×1024, and 1440×900 layouts.

After the browser checks—even after a failure—set the exact printed value,
close the fixture, and prove cleanup:

```bash
export FIXTURE_STATE_DIR='/exact/path/printed/by/start'
"$PYTHON_BIN" -m tests.support.tickets_fixture_server stop \
  --state-dir "$FIXTURE_STATE_DIR"
if "$PYTHON_BIN" -m tests.support.tickets_fixture_server status \
  --state-dir "$FIXTURE_STATE_DIR" \
  --expect-url http://127.0.0.1:8010; then
  echo "STOP: fixture unexpectedly remains live" >&2
  exit 1
fi
test -z "$(find "$FIXTURE_STATE_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
rmdir "$FIXTURE_STATE_DIR"
```

Replace the quoted placeholder with the literal path printed by `start`; never
execute it verbatim. If cleanup cannot run, report the exact state directory;
the runner's 30-minute auto-timeout is the final safety net, not a substitute
for `stop`.

## Step 8 — Verify

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_tickets_ui_contract.py \
  tests/test_tickets_fixture_app.py \
  tests/test_tickets_console_app.py \
  tests/test_ticket_review_routes.py -q
"$PYTHON_BIN" -m compileall -q api
git -C "$IMPL_ROOT" diff --check
```

Static security scan:

```bash
if rg -n \
  'innerHTML|outerHTML|insertAdjacentHTML|document\\.write|eval\\(|localStorage|sessionStorage|X-API-Key|Bearer ' \
  ui/tickets; then
  echo "STOP: prohibited browser sink/storage/credential pattern" >&2
  exit 1
fi
```

Expected: command exits 0 with no matches. Put explanatory negative examples in
Python tests, not shipped UI source.

## Definition of Done

- `/tickets` replaces the sheet’s list fields and adds workflow state.
- Both live DevRev and durable review queues paginate correctly.
- All remote/user content is rendered without HTML injection sinks.
- No credentials/PII are persisted in browser storage.
- Desktop, mobile, keyboard, loading, empty, partial, rate-limit, auth, and error states are usable.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/ui/tickets/index.html \
  kb-rag-system/ui/tickets/tickets.css \
  kb-rag-system/ui/tickets/app.js \
  kb-rag-system/ui/tickets/api.js \
  kb-rag-system/ui/tickets/state.js \
  kb-rag-system/ui/tickets/render.js \
  kb-rag-system/ui/tickets/icons.svg \
  kb-rag-system/api/tickets_console_main.py \
  kb-rag-system/tests/test_tickets_ui_contract.py \
  kb-rag-system/tests/support/tickets_console_fixture_app.py \
  kb-rag-system/tests/support/tickets_fixture_server.py \
  kb-rag-system/tests/test_tickets_fixture_app.py \
  kb-rag-system/tests/test_tickets_fixture_server.py
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/ui/tickets/index.html \
  --allow kb-rag-system/ui/tickets/tickets.css \
  --allow kb-rag-system/ui/tickets/app.js \
  --allow kb-rag-system/ui/tickets/api.js \
  --allow kb-rag-system/ui/tickets/state.js \
  --allow kb-rag-system/ui/tickets/render.js \
  --allow kb-rag-system/ui/tickets/icons.svg \
  --allow kb-rag-system/api/tickets_console_main.py \
  --allow kb-rag-system/tests/test_tickets_ui_contract.py \
  --allow kb-rag-system/tests/support/tickets_console_fixture_app.py \
  --allow kb-rag-system/tests/support/tickets_fixture_server.py \
  --allow kb-rag-system/tests/test_tickets_fixture_app.py \
  --allow kb-rag-system/tests/test_tickets_fixture_server.py
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "feat(tickets): add professional review queue UI"
```

Proceed to Stage 7.
