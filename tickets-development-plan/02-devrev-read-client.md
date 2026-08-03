# Stage 2 — Resilient, Read-Only DevRev Client

> **For Claude Opus 5:** This is an executable implementation prompt. Read `tickets-development-plan/README.md` and verify Stage 1’s commit/tests first. Implement and verify the client; do not merely describe it.

**Goal:** Add a typed async DevRev adapter that lists tickets, gets one work item, and reads every timeline page safely without leaking credentials or corrupting pagination.

**Architecture:** A single shared `httpx.AsyncClient` owns base URL, auth/version headers, timeout, retries, and response parsing. Public methods return normalized strict models plus opaque cursors; raw response handling remains private.

**Tech Stack:** Python 3.12, `httpx`, asyncio, Pydantic v2, pytest `MockTransport`.

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

All must pass and the worktree must be clean before this stage.

Read the official contracts again:

- <https://developer.devrev.ai/about/authentication>
- <https://developer.devrev.ai/about/pagination>
- <https://developer.devrev.ai/about/rate-limits>
- <https://developer.devrev.ai/about/errors>
- <https://developer.devrev.ai/about/versioning>
- <https://developer.devrev.ai/api-reference/works/list-post>
- <https://developer.devrev.ai/api-reference/works/get>
- <https://developer.devrev.ai/api-reference/timeline-entries/list>

## Files

Create:

- `kb-rag-system/data_pipeline/devrev_client.py`
- `kb-rag-system/tests/test_devrev_client.py`

Modify:

- `kb-rag-system/api/ticket_review_models.py` for
  `DevRevTicketFilters`/`TimelinePage` only if Stage 1 did not already create
  them;
- `kb-rag-system/tests/test_ticket_review_models.py` for their contract tests.

Use the Stage 1 fixtures. Do not add owner/tag lookup endpoints in this stage.

## Step 1 — Write failing client tests

Use `httpx.MockTransport`; do not add `respx` just for this stage.

Required tests:

1. Every request uses:
   - `Authorization: Bearer <token>`;
   - `Accept: application/json`;
   - `X-Devrev-Version: 2022-10-20`;
   - the exact `https://api.devrev.ai` origin by default.
2. `list_tickets` uses `POST /works.list` and always forces `type=["ticket"]`, even if a caller tries to pass another type.
3. Structured filters map only an allowlisted set. The exact JSON wire shape is:
   - `type: ["ticket"]`;
   - top-level `stage`, `state`, `applies_to_part`, `owned_by`, `created_by`,
     `reported_by`, `tags`, `created_date`, `modified_date`, `cursor`, `mode`,
     and `limit`;
   - nested `ticket.source_channel`, `ticket.subtype`, and integer
     `ticket.visibility` IDs.
   Unknown filters and caller-supplied `type` fail before network I/O.
4. `mode` is closed to `after | before`; list responses preserve both
   `next_cursor` and `prev_cursor`, with round-trip forward/back tests.
5. `get_ticket` accepts a bounded DON or display ID and calls `GET /works.get`,
   then enforces configured `applies_to_part`/ticket-visibility scope on the
   returned object.
6. `list_timeline_page` always sends `mode=after`; it returns one bounded page,
   cursors, `truncated`, `partial`, and bounded warnings.
7. `iter_timeline_entries` continues after an empty page when `next_cursor` exists.
8. Iteration stops only when `next_cursor` is absent.
9. A repeated cursor raises a typed pagination error before an infinite loop.
10. Configured `max_pages` and `max_entries` return/raise a typed partial
    resource result; no public model calls the bounded result “complete.”
11. `429` honors integer or HTTP-date `Retry-After`, capped at the canonical
    60 seconds; tests patch the async sleeper.
12. `500` and `503` retry with exponential backoff + bounded jitter.
13. `400`, `401`, `403`, `404`, and `409` are not retried and map to typed exceptions with safe public messages.
14. Network timeout/transport errors retry only where the operation is idempotent. All MVP operations are reads.
15. A non-JSON or oversized error response is truncated at 4 KiB and never includes the bearer token.
16. Redirects do not automatically forward credentials to a different origin.
17. Rate-limit response headers are captured in a bounded diagnostic object without logging ticket content.
18. Cancellation propagates; do not turn `asyncio.CancelledError` into a DevRev error.
19. Unknown timeline entry types are preserved as a bounded unsupported entry, not a crash.
20. A direct ID outside configured part/ticket-visibility scope returns a typed
    scope denial; it cannot bypass list filters.
21. Every list request intersects/injects configured `applies_to_part` and
    ticket-visibility scope; timeline calls intersect the separate timeline
    visibility enum allowlist. Callers cannot clear or broaden either.
22. `200` bodies are streamed and capped at
    `TICKETS_DEVREV_MAX_RESPONSE_BYTES` before `json()`/model parsing.
    Oversized declared `Content-Length`, oversized chunked list/get/timeline
    responses, and decompression expansion fail with a typed resource-limit
    error without retaining/logging the body.

Run and observe the import failure:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_devrev_client.py -q
```

## Step 2 — Implement typed errors and client lifecycle

Required exception hierarchy:

```python
class DevRevError(Exception): ...
class DevRevAuthenticationError(DevRevError): ...
class DevRevPermissionError(DevRevError): ...
class DevRevNotFoundError(DevRevError): ...
class DevRevConflictError(DevRevError): ...
class DevRevRateLimitError(DevRevError): ...
class DevRevTransientError(DevRevError): ...
class DevRevProtocolError(DevRevError): ...
class DevRevPaginationError(DevRevError): ...
class DevRevResourceLimitError(DevRevError): ...
```

The client:

```python
class DevRevClient:
    async def aclose(self) -> None: ...
    async def list_tickets(
        self,
        filters: DevRevTicketFilters,
        *,
        cursor: str | None = None,
        mode: Literal["after", "before"] = "after",
        limit: int | None = None,
    ) -> CursorPage[DevRevTicketSummary]: ...
    async def get_ticket(self, work_id: str) -> DevRevTicketDetail: ...
    async def list_timeline_page(
        self,
        object_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> TimelinePage: ...
    async def iter_timeline_entries(
        self,
        object_id: str,
    ) -> AsyncIterator[DevRevTimelineEntry]: ...
```

Rules:

- Accept only the configured server-side bearer value; MVP deployment metadata
  identifies it as a PAT for a dedicated read-only DevRev integration user.
  Never accept a browser/user-supplied token or implement AAT/SUT/session
  exchange implicitly.
- One shared `httpx.AsyncClient`.
- `follow_redirects=False`.
- Connect/read/write/pool timeouts are explicit.
- Retry only `429`, `500`, `503`, and transport failures for these reads.
- Honor `Retry-After` before calculated backoff.
- Jitter must be injectable/deterministic in tests.
- Cap error-body parsing and log only endpoint name/status/request ID/rate headers—not query bodies, auth headers, ticket text, or full remote payloads.
- Use `AsyncClient.stream`; reject an oversized `Content-Length` before
  reading, count decoded streamed bytes through the canonical 4 MiB cap, abort
  immediately on overflow, and only then parse JSON. Never call
  `response.json()` on an unbounded success body.
- Reject base URLs with user info, query, fragment, or non-HTTPS in production.
- Treat DevRev cursors as opaque strings; never parse or synthesize them.
- A short or empty page is not terminal when `next_cursor` exists.
- `CursorPage` carries `next_cursor`, `prev_cursor`, `partial`, `truncated`,
  and bounded warnings. Do not label a guarded iterator result “complete.”
- Deduplicate timeline entry IDs across iterator pages while preserving source
  order, and record a bounded diagnostic count when duplicates occur.
- The page adapter preserves DevRev order. A separate bounded hydration helper
  may sort an explicitly loaded set using parsed aware timestamps plus stable
  original position; malformed dates go last and surface a warning.
- Owner/tag display-name lookups are not implemented in MVP. Accept only typed
  exact IDs in the adapter, and do not expose those fields in the UI until the
  lookup feature gate is approved.

## Step 3 — Normalize defensively

Normalize:

- internal DON and display ID separately;
- title/body with length bounds;
- created/modified dates;
- stage/state, severity, owners, reporter, tags, source channel, visibility, object version;
- timeline entry ID/type/body/body type/author/visibility/thread/reply relation/created/modified dates;
- change events without treating them as authored participant replies.

Do not infer participant vs human vs AI identity inside the low-level client. Preserve the actor identity/type for the service layer in Stage 4.

Do not persist the `raw` object in normalized models.

## Step 4 — Add safe diagnostics

Expose per-call diagnostics suitable for structured logs:

```json
{
  "endpoint": "timeline-entries.list",
  "status": 200,
  "attempts": 1,
  "pages": 2,
  "items": 3,
  "rate_limit_remaining": 812,
  "request_id": "remote request id if present"
}
```

Never include cursor values in INFO logs; hash them if correlation is required.

## Step 5 — Verify

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_devrev_client.py -q
"$PYTHON_BIN" -m pytest tests/test_ticket_review_models.py -q
"$PYTHON_BIN" -m compileall -q data_pipeline api
git -C "$IMPL_ROOT" diff --check
```

Run a token-leak static check:

```bash
rg -n 'Authorization|TICKETS_DEVREV_TOKEN|DEVREV.*TOKEN' data_pipeline/devrev_client.py tests/test_devrev_client.py
```

Manually inspect every match. No test assertion failure or exception representation may print an actual token.

Do not perform a live DevRev request in this stage.

## Definition of Done

- List/get/timeline-page operations are typed, scoped, and tested.
- Forward/back list cursors and forward-only timeline cursors are locked.
- Empty-page-with-cursor, repeated cursor, max pages, max entries, and duplicate entry behavior are locked.
- `Retry-After` and transient retries are deterministic under tests.
- Credentials cannot follow cross-origin redirects or enter logs/errors.
- No DevRev, Firestore, GCP, or Pinecone write occurred.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/data_pipeline/devrev_client.py \
  kb-rag-system/tests/test_devrev_client.py \
  kb-rag-system/api/ticket_review_models.py \
  kb-rag-system/tests/test_ticket_review_models.py
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/data_pipeline/devrev_client.py \
  --allow kb-rag-system/tests/test_devrev_client.py \
  --allow kb-rag-system/api/ticket_review_models.py \
  --allow kb-rag-system/tests/test_ticket_review_models.py
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit -m "feat(tickets): add resilient DevRev reader"
```

Proceed to Stage 3.
