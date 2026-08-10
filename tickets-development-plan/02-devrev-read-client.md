# Stage 2 — Resilient, Read-Only DevRev Client

> **For Claude Opus 5:** This is an executable implementation prompt. Read `tickets-development-plan/README.md` and verify Stage 1’s commit/tests first. Implement and verify the client; do not merely describe it.

**Goal:** Add a typed async DevRev adapter that hydrates a known ticket and
reads its timeline pages safely without leaking credentials or using DevRev
discovery as a source for the evaluation platform.

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
- <https://developer.devrev.ai/api-reference/works/get>
- <https://developer.devrev.ai/api-reference/timeline-entries/list>

## Files

Create:

- `kb-rag-system/data_pipeline/devrev_client.py`
- `kb-rag-system/tests/test_devrev_client.py`

Modify:

- `kb-rag-system/api/ticket_review_models.py` for `TimelinePage` only if Stage
  1 did not already create it;
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
2. `get_ticket` accepts a bounded DON or display ID and calls `GET /works.get`,
   then enforces configured `applies_to_part`/ticket-visibility scope on the
   returned object.
3. Evaluation ingestion, execution list, and execution detail never issue
   `works.list`; unknown tickets cannot be discovered or used to create
   evaluation rows. A pre-existing read adapter may retain `list_tickets` for
   unrelated legacy callers, but the ticket-evaluation route/service graph has
   no caller edge to it.
4. `list_timeline_page` always sends `mode=after`; it returns one bounded page,
   cursors, `truncated`, `partial`, and bounded warnings.
5. `iter_timeline_entries` continues after an empty page when `next_cursor` exists.
6. Iteration stops only when `next_cursor` is absent.
7. A repeated cursor raises a typed pagination error before an infinite loop.
8. Configured `max_pages` and `max_entries` return/raise a typed partial
    resource result; no public model calls the bounded result “complete.”
9. `429` honors integer or HTTP-date `Retry-After`, capped at the canonical
    60 seconds; tests patch the async sleeper.
10. `500` and `503` retry with exponential backoff + bounded jitter.
11. `400`, `401`, `403`, `404`, and `409` are not retried and map to typed exceptions with safe public messages.
12. Network timeout/transport errors retry only where the operation is idempotent. All MVP operations are reads.
13. A non-JSON or oversized error response is truncated at 4 KiB and never includes the bearer token.
14. Redirects do not automatically forward credentials to a different origin.
15. Rate-limit response headers are captured in a bounded diagnostic object without logging ticket content.
16. Cancellation propagates; do not turn `asyncio.CancelledError` into a DevRev error.
17. Unknown timeline entry types are preserved as a bounded unsupported entry, not a crash.
18. A direct ID outside configured part/ticket-visibility scope returns a typed
    scope denial. Timeline calls intersect the separate timeline visibility
    enum allowlist; callers cannot clear or broaden either scope.
19. `200` bodies are streamed and capped at
    `TICKETS_DEVREV_MAX_RESPONSE_BYTES` before `json()`/model parsing.
    Oversized declared `Content-Length`, oversized chunked get/timeline
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
- `TimelinePage` carries `next_cursor`, `partial`, `truncated`, and bounded
  warnings. Do not label a guarded iterator result “complete.”
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

- Get/timeline-page operations are typed, scoped, and tested; `works.list` is
  absent from the evaluation ingestion/list/detail flow and cannot create a
  platform row.
- Forward-only timeline cursors are locked.
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
