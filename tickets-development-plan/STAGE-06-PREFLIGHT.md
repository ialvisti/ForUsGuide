# Stage 6 preflight — read this before `06-professional-tickets-list-ui.md`

Written 2026-08-04, immediately after Stage 5 was committed. Each stage runs in a
fresh chat, so everything here is what a new session cannot recover from the
stage prompt alone.

Every claim below was fact-checked against the running code by four independent
reviewers, and 16 corrections were applied as a result. It is still a
point-in-time record, not live state: verify anything load-bearing.

---

## 1. Repository state

| Fact | Value |
|---|---|
| Work in | `/Users/ivanalvis/Desktop/ForUsGuide` (the primary checkout) |
| Branch | `main` — commit **directly to it** |
| `main` tip | `aac3fd6` "feat(tickets): expose authenticated admin API" |
| `origin/main` | `5b7ab10` — `main` is **3 commits ahead**, deliberately |
| Feature branches | none. Stages 1–3 entered `main` through merge commit `91650af`; Stages 4–5 were fast-forwarded on top. All those branches are deleted. |
| Pushing | **never** — the user keeps this entirely local |

Stages 1–5 are all in `main`'s history. The stage files' `Commit` blocks say to
work in a sibling worktree and imply a branch; ignore that part. Everything else
in those blocks still applies, including `scripts/verify_staged_scope.py`.

## 2. Corrected environment

The `Prerequisites` block in every stage file names two paths that no longer
exist: `ForUsGuide-tickets-console` and `ForUsGuide-handle-ticket-finalization`.
Stage 6's block has been corrected in place; **stages 07–11 and 99 still carry
the stale exports**, so correct them the same way when you get there:

```bash
export IMPL_ROOT="/Users/ivanalvis/Desktop/ForUsGuide"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="/Users/ivanalvis/Desktop/ForUsGuide/.venv-local/bin/python"
```

`TICKETS_BASE_SHA=eed9b34967c59b8bfec34026c9a8637581f2036a` is still a valid
ancestor of `main`, so that assertion passes unchanged.

**Interpreters.** There is no `.venv` under `kb-rag-system`, but there *are* two
working ones, both Python **3.14.5** (not the 3.12 the lock and `pyproject.toml`
target):

- `/Users/ivanalvis/Desktop/ForUsGuide/.venv-local/bin/python` — use this one;
- `/Users/ivanalvis/Desktop/ForUsGuide/kb-rag-system/venv/bin/python` (no leading
  dot) — also functional, same behavior. Mentioned only so its existence does not
  read as a contradiction of this section.

Both lack `ruff`, `mypy`, `cachecontrol`, and the Cloud Tasks SDK
(`google.cloud.tasks_v2`), which is what produces the pre-existing failures below.

## 3. Test baseline — know it before you blame yourself

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/ -q \
  --ignore=tests/integration --ignore=tests/e2e \
  -m "not live_dependencies and not staging_e2e"
```

**In this checkout expect `2995 passed / 29 failed / 4 skipped`.** The clean
number — `3008 passed / 16 failed / 4 skipped` — only appears in a tree without
`kb-rag-system/.env`. Both were measured at `aac3fd6`.

The **16 pristine failures** are all missing-dependency, and land in exactly four
files:

- `tests/test_ticket_task_queue.py` — 10 (`google.cloud.tasks_v2` absent)
- `tests/test_ticket_worker.py::TestWorkerEndpointAuth` — 3
- `tests/test_ticket_security.py::test_google_certificate_transport_*` — 2 (`cachecontrol`)
- `tests/test_ticket_runtime_safety.py::test_malformed_cloud_task_*` — 1

The **13 extra** come from the gitignored `kb-rag-system/.env`, which
`pydantic-settings` loads (`ForUsBots max wait debe caber en inquiry budget`):

- `tests/test_api.py::TestTicketHandlerContainment` — 5
- `tests/test_app_role_startup.py` — 7
- `tests/test_ticket_task_queue.py::TestProductionFailClosedConfig::test_worker_uses_stable_audience_without_needing_its_target_uri` — 1

**None of those 29 are yours.** For authoritative numbers on your own change, run
in a throwaway tree with no `.env` and copy changed files in — never move the
user's file:

```bash
git -C /Users/ivanalvis/Desktop/ForUsGuide archive HEAD | tar -x -C "$(mktemp -d)"
```

Two more traps:

1. **`tests/e2e` must be excluded explicitly.** It raises 20 fixture-setup errors
   demanding 53 live `E2E_*` settings, and `-x` makes that the first thing you see.
2. **The clean-tree assertion is repo-wide.** `06-…md:29` runs
   `test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"`
   under `set -euo pipefail`, so *any* uncommitted file anywhere — including in
   `tickets-development-plan/` — aborts the block before it reaches the pytest
   gate. `.env` itself is correctly ignored (`kb-rag-system/.gitignore:28`) and
   never appears. To check only the code:
   `git status --porcelain=v1 --untracked-files=all -- kb-rag-system`.

Stage 6's own prerequisite gate passes today:

```bash
"$PYTHON_BIN" -m pytest tests/test_ticket_review_routes.py \
  tests/test_tickets_console_app.py -q      # 191 passed
```

## 4. Lint gates

`pyproject.toml:9-31` scopes ruff to **22 explicit patterns**; the ones Stage 6
touches are `api/**/*.py` and `data_pipeline/ticket_*.py`. **`tests/` is in none
of them**, so Stage 6's new test files are not linted at all (ruff prints "No
Python files found" for `tests/`).

`pyproject.toml:33-45`:

```toml
select = ["E", "F", "W", "B", "S", "ASYNC"]
ignore = ["E501", "S101"]              # long lines and bare assert are exempt
[tool.ruff.lint.per-file-ignores]
"api/main.py" = ["E402", "B008"]
"tests/**"    = ["S105", "S106", "S311"]
```

ruff is installed nowhere, so:

```bash
"$PYTHON_BIN" -m pip install --target /tmp/tools ruff
cd "$KBRAG_ROOT"
PATH=/tmp/tools/bin:$PATH PYTHONPATH=/tmp/tools ruff check api/tickets_console_main.py
```

Config is discovered by walking up from each *checked file*, not from cwd — but
run from `kb-rag-system/` anyway so relative paths and `ruff check .` resolve
against the right tree.

**Write `x: Annotated[T, Depends(f)]`, never `x: T = Depends(f)`.** B008 fires on
the second form and not the first (verified empirically). The repo's older answer
was the `api/main.py` per-file-ignore above, which suppresses 15 hits; Stage 5
used `Annotated` instead and is ruff-clean.

**Pre-existing lint debt — 8 findings repo-wide, none of them yours:**

- `api/tickets_evidence_broker_main.py` — 2× B008 (lines 210, 272)
- `data_pipeline/ticket_review_service.py` — 4× F401 (lines 38, 39, 40, 104),
  1× S105 (line 112), 1× B905 (line 497)

All five Stage 5 modules plus `ticket_review_models.py`, `tickets_console_config.py`
and `ticket_evidence_client.py` are ruff-clean. `[tool.mypy] files`
(`pyproject.toml:51-69`) lists none of the console modules, so `mypy --strict` is
not a gate here.

## 5. The Stage 5 API the UI must code against

Twelve operations across **ten OpenAPI paths**, all under `/api/admin/v1`, plus
the unschematized `/livez`, `/readyz`, `/tickets`, and `/tickets/{display_id}`:

```
GET    /session
GET    /tickets                                     (list + exact ticket_id)
GET    /tickets/{ticket_ref}                        (detail + one timeline page)
GET    /tickets/{ticket_ref}/timeline               (forward-only)
POST   /tickets/{ticket_ref}/review                 (201, reviewer role)
GET    /reviews
GET    /reviews/{review_id}
PATCH  /reviews/{review_id}
GET    /reviews/{review_id}/audit-events
GET    /reviews/{review_id}/evidence-links
POST   /reviews/{review_id}/evidence-links
DELETE /reviews/{review_id}/evidence-links/{link_id}
```

Remediation and import/export routes **do not exist** and are asserted absent
from OpenAPI. Do not stub them. `GET /session` returns
`feature_flags.remediation_enabled = false` and `import_export_enabled = false`
precisely so the UI can branch on it.

What the adapter must do, in the exact terms the server enforces:

- **Reads** need only the IAP assertion, which IAP injects. Nothing else.
- **Writes** need all five of:
  - exact `Origin` — browser-supplied; never try to set it from JS, it is a
    forbidden header;
  - `Sec-Fetch-Site: same-origin`;
  - `X-CSRF-Token`;
  - `Content-Type: application/json`. A `charset` parameter is accepted only as
    `utf-8` or `utf8` (case-insensitive); any *other* parameter is ignored rather
    than refused. A bad media type or charset is **415
    `UNSUPPORTED_MEDIA_TYPE`**, not 422.
  - `Idempotency-Key` matching `^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z` (8–128
    chars; a UUID qualifies; note `\Z`, so a trailing newline is refused).
    Missing or malformed is **400 `IDEMPOTENCY_KEY_REQUIRED`**.
- **Pagination** travels in the `X-Tickets-Cursor` **request header**. A raw
  `cursor`, `next_cursor`, `prev_cursor`, `page_token`, `next`, `before`, or
  `after` query parameter is rejected with **422** (case-insensitive match) — an
  explicit guard, not a style preference. Binding differs by route:
  - ticket-list and timeline tokens are console-sealed and bound to **endpoint,
    direction, filter set, and subject**;
  - the `/reviews` queue token is the repository's own, passed through unwrapped,
    and is bound to **endpoint and filter set only** — no direction, no subject.
  Any mismatch is 422 `CURSOR_REJECTED`.
- **Versioned writes** (`PATCH`, evidence `POST`/`DELETE`) require
  `If-Match: "vN"` and return `ETag: "v<N+1>"`. Missing → **428
  `PRECONDITION_REQUIRED`**, malformed → **422 `PRECONDITION_MALFORMED`**, stale
  → **412 `REVIEW_VERSION_CONFLICT`** carrying `current_version` and `changed_at`
  so the UI can offer a reload. **409** means a genuine business/idempotency
  conflict.
- **Errors** are always `{"error": {code, message, request_id, current_version?,
  changed_at?}}`, codes UPPER_SNAKE (see the `CODE_*` constants in
  `api/ticket_review_routes.py`). Messages never quote an upstream body.
- **Rate limits**: `429 RATE_LIMITED` from our own per-subject bound, which
  **always** carries `Retry-After`; or `429 UPSTREAM_RATE_LIMITED` from DevRev,
  which carries `Retry-After` **only when DevRev sent one** — so the UI needs a
  fallback backoff rather than trusting the header to be there.
- Every API and HTML response carries `Cache-Control: no-store`.

The CSRF token comes from `GET /session`, is session-bound to the IAP subject,
and lives in memory only — a cookie would be sent automatically and defeat the
point, and storage would survive an XSS long enough to be exfiltrated.

## 6. The CSP is the hard constraint on the UI

`api/tickets_console_main.py:131-140` emits, with no `'unsafe-inline'` and no
external host:

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'
```

So: no inline `<script>`, no inline `<style>`, no `style="..."` attributes, no
inline event handlers, no CDN, no remote fonts.
`tests/test_tickets_console_app.py:193` (`test_the_csp_allows_no_inline_script_or_style`)
asserts the absence of `unsafe-inline`, so relaxing the CSP fails a Stage 5 test
rather than silently widening the boundary.

## 7. Where the UI files go

Stage 5 shipped a placeholder served from three constants at
`api/tickets_console_main.py:105-107`:

```python
UI_DIRECTORY        = Path(__file__).resolve().parent / "tickets_ui"   # api/tickets_ui/
UI_ASSETS_DIRECTORY = UI_DIRECTORY / "assets"                          # mounted at /tickets/assets
UI_INDEX_FILE       = UI_DIRECTORY / "index.html"
```

Stage 6 puts the UI at `kb-rag-system/ui/tickets/` and lists
`api/tickets_console_main.py` in its own *Modify* section "to serve the real files
and assets", so repointing those constants is in-scope, expected work.
`Dockerfile:26` already copies `ui/`, so moving the UI out of `api/` does not
break the image, and `tickets_ui` appears nowhere else in the repo.

**Put the static files in `ui/tickets/assets/` and keep `index.html` at
`ui/tickets/index.html`.** Do not repoint the mount at `ui/tickets/` itself: the
comment on the mount (`api/tickets_console_main.py:535-536`) says mounting the UI
root "would expose templates and any stray file beside them", and with Stage 6's
flat file list that option would serve `index.html` — and any stray sibling —
under `/tickets/assets/`. No test pins the mounted *directory*, so that mistake
would pass the suite while reversing a documented Stage 5 security decision.

Keep the mount **URL** at `/tickets/assets` regardless:
`test_only_the_assets_directory_is_ever_mounted`
(`tests/test_tickets_console_app.py:356`) hardcodes that string.

Two Stage 5 tests are written against the constants, not literal paths, so they
keep passing — but their meaning changes:
`test_the_assets_mount_appears_only_when_the_directory_exists` starts asserting a
real mount, and `test_the_placeholder_says_the_ui_stage_is_not_installed`
**self-skips** once `UI_INDEX_FILE` exists (`tests/test_tickets_console_app.py:346`).
That skip is intended, not a regression.

## 8. The fixture server's authentication problem — solve this first

Stage 6 Step 7 wants a browser pointed at `http://127.0.0.1:8010/tickets`. Stage
5's local-auth path (`api/reviewer_auth.py:653-675`) requires **five** conditions:
`TICKETS_ENVIRONMENT=local`, `TICKETS_AUTH_MODE=local`,
`TICKETS_ALLOW_LOCAL_AUTH=true`, a loopback peer, and the header
`X-Tickets-Local-Reviewer: <email>`.

**A browser cannot set that header on a navigation**, and there is no alternative
browser-authenticable path: `_local_mode_active()` short-circuits the IAP branch,
and `PUBLIC_PATHS` is only `{"/livez", "/readyz"}`, so even `/tickets/assets/*`
401s. So the fixture app must not rely on local auth. The seam already exists —
`build_console_app` takes an injected authenticator:

```python
from api.tickets_console_main import build_console_app
app = build_console_app(settings, authenticator=FixtureAuth(), ...)
```

Anything with
`authenticate(headers, *, client_host=None, request_id=None) -> AuthenticatedReviewer`
satisfies it (`api/tickets_console_main.py:330-334`). This was verified
empirically: a fixed injected authenticator returns 200 for `GET /tickets`,
`GET /tickets/TKT-1`, and `GET /api/admin/v1/session` (csrf_token included) with
**no request headers at all**, and needs **zero changes to Stage 5 code**. Prefer
that over extending local auth, which would add a production code path to serve a
test.

`wire_console_state` injects nine collaborators — `devrev`, `repository`,
`service`, `evidence_client`, `authenticator`, `claims_verifier`, `clock`,
`rate_limiter`, `firestore_database` — which is everything needed to avoid ADC,
the metadata server, and real DevRev.

Two caveats about the worked example:

- `tests/test_ticket_review_routes.py:297-322` shows the collaborator injection
  (real service and repository over `InMemoryTicketReviewBackend`, fake DevRev),
  but it authenticates via `claims_verifier=` plus an `X-Goog-IAP-JWT-Assertion`
  header — **not** via an injected `authenticator`. The fixture authenticator is
  new code with no in-repo precedent; do not go looking for one.
- `tests/support/` does not exist yet. `tests/__init__.py` does, and `tests/e2e/`
  proves a subdirectory with no `__init__.py` still resolves as a namespace
  subpackage on this interpreter, so `tests.support` resolves as soon as the
  directory exists — provided commands run from `kb-rag-system/`. Adding
  `tests/support/__init__.py` is optional, matching `tests/integration/`.

## 9. Stage 5 facts a UI test might trip over

- `GET /tickets` with `ticket_id` returns a **singleton page with no cursors** via
  the scoped `works.get` path; combining it with any list filter or a cursor is a
  422 `UNSUPPORTED_FILTER_COMBINATION`.
- `GET /reviews` accepts a status set plus **at most one** facet (`facet` and
  `facet_value` must both be present); `title_contains` always 422s — even when
  empty — because there is no full-text search. This is the master query grammar,
  not an oversight.
- `audit-events` returns a hardcoded `next_cursor: null`
  (`api/ticket_review_routes.py:1209`). Do not build a "load more" control for it.
- `review_id` path params must be 64 lowercase hex chars; anything else is 422
  before any lookup.
- A ticket reference is a bounded display id (`TKT-123`, **trimmed** then
  upper-cased) or a `don:`-prefixed opaque ref. Interior whitespace and `..` are
  422 — but *surrounding* whitespace is silently trimmed and succeeds, and
  anything containing a slash (a URL, `../`) **404s on routing** before the
  validator runs. Do not assert 422 for those.
- A DevRev outage still returns the durable review with `partial: true` and
  `warnings: ["devrev_unavailable"]`. The UI must render that as a partial
  result, never as an empty queue.

## 10. Where the rest of the context lives

Persistent memory records the branching policy, the Stage 4/5 states, and the
recurring gotchas: `tickets-console-branching`, `tickets-stage5-admin-app`,
`tickets-stage4-provenance`, `tickets-console-stage1`,
`tickets-console-devrev-client`, `tickets-console-progress-gotchas`.
