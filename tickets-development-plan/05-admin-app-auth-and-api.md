# Stage 5 — Standalone Admin App, Signed-IAP Authentication, RBAC, and API

> **For Claude Opus 5:** This is an executable implementation prompt. Build a separate admin app; do not expose or weaken the existing RAG service.

**Goal:** Serve `/tickets` and the Stage 5 subset of `/api/admin/v1` from a
standalone FastAPI application with cryptographically verified identity,
deny-by-default RBAC, CSRF/origin protection, idempotency, and HTTP
preconditions.

**Architecture:** The admin application initializes only DevRev, the dedicated
named Firestore review repository, hydration service, and an authenticated
client to the evidence broker. Production trusts only a verified IAP JWT for
the configured Cloud Run audience. Same-origin browser requests call a
dedicated router; the n8n-facing `api.main` remains private and retains its
current auth.

**Tech Stack:** FastAPI, Google Auth, IAP JWT, Pydantic, Firestore, `httpx`, pytest TestClient.

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
"$PYTHON_BIN" -m pytest tests/test_devrev_client.py \
  tests/test_ticket_review_repository.py \
  tests/test_ticket_review_service.py \
  tests/test_ticket_review_provenance.py \
  tests/test_ticket_evidence_broker.py -q
```

Read:

- `api/main.py` middleware/error patterns;
- `api/auth.py`;
- `api/entrypoint.py`;
- `tests/test_ticket_security.py`;
- official IAP signed-header validation docs.

## Files

Create:

- `kb-rag-system/api/reviewer_auth.py`
- `kb-rag-system/api/tickets_csrf.py`
- `kb-rag-system/api/ticket_review_routes.py`
- `kb-rag-system/api/tickets_console_main.py`
- `kb-rag-system/data_pipeline/ticket_evidence_client.py`
- `kb-rag-system/tests/test_reviewer_auth.py`
- `kb-rag-system/tests/test_tickets_csrf.py`
- `kb-rag-system/tests/test_ticket_review_routes.py`
- `kb-rag-system/tests/test_tickets_console_app.py`
- `kb-rag-system/tests/test_ticket_evidence_client.py`

Modify:

- `kb-rag-system/requirements.in` only if `google-auth` is not already a declared direct dependency needed by runtime;
- lock files through the repository’s Python 3.12 lock process;
- no existing Cloud Run IAM or `api.main` route in this stage.

## Step 1 — Write failing authentication tests

Inject a fake JWT verifier; unit tests must not fetch Google public keys.

Cover:

1. Missing `X-Goog-IAP-JWT-Assertion` returns 401 in production/IAP mode.
2. Bad signature, issuer, expiry, issued-at, or audience returns 401.
3. Valid token must include non-empty `sub`, non-empty email, and expected
   issuer. Do not require a top-level `email_verified` claim that standard IAP
   JWTs do not guarantee; if `hd` is present it must agree with domain policy.
4. Email domain comparison is normalized and exact; `user@forusall.com.attacker.tld` fails.
5. Unsigned `X-Goog-Authenticated-User-Email` alone is ignored.
6. Actor identity is derived only from verified claims.
7. IAP-authorized but unbound user is denied. The production configuration
   cannot enable a default viewer role.
8. Exact role bindings cannot be overridden by query/body/header.
9. Role hierarchy:
   - viewer: reads only;
   - reviewer: viewer + create/update reviews/evidence;
   - remediator: reviewer + remediation batch operations;
   - admin: all + CSV import/export and explicit reopen.
   - agent: only lease-scoped show/claim/heartbeat/materialize/update of an
     already-created remediation batch; no general ticket/review list,
     verification/completion, import/export, role, or direct Firestore
     mutation.
10. Local auth works only with `TICKETS_ENVIRONMENT=local`, fixture
    dependencies, loopback bind, `TICKETS_AUTH_MODE=local`, and explicit
    local-auth opt-in. Any staging/production combination fails at startup.
11. Authentication logs contain subject hash/request ID, not the full JWT.
12. Unsafe-method protection rejects missing/mismatched Origin, cross-site
    `Sec-Fetch-Site`, missing/expired/wrong-session CSRF token, missing
    `Idempotency-Key`, or wrong content type.

Run:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_reviewer_auth.py \
  tests/test_tickets_csrf.py -q
```

## Step 2 — Implement signed IAP verification

Use `google.auth` verification against:

```text
certs_url = https://www.gstatic.com/iap/verify/public_key
issuer    = https://cloud.google.com/iap
audience  = /projects/{PROJECT_NUMBER}/locations/{REGION}/services/{SERVICE_NAME}
```

The expected audience is a required production setting, not derived from a request header.

Expose:

```python
async def authenticated_reviewer(request: Request) -> ReviewerIdentity: ...
def require_role(minimum: ReviewerRole): ...
```

Use dependency injection for verifier/time/cert cache in tests.

Do not use a client-supplied email as reviewer attribution.

The dedicated remediation-agent service account reaches IAP using a
short-lived keyless service-account JWT whose programmatic-request audience is
the validated `https://<exact-console-host>/*`, following the official IAP
path-wildcard flow. A bare base-URL audience is forbidden because the CLI
calls multiple `/api/admin/v1/**` paths. This audience is distinct from the
IAP-signed assertion audience
`/projects/{PROJECT_NUMBER}/locations/{REGION}/services/{SERVICE_NAME}` that
the app verifies after IAP. Application RBAC maps only that exact
service-account email to `agent`; no JSON key is created or accepted. Tests
cover wrong host, missing `/*`, another service/path, expiry, and confused use
of one audience in the other validation step.

Implement `/api/admin/v1/session` so it issues a short-lived, session-bound
CSRF token derived with `TICKETS_CSRF_SIGNING_SECRET`; the browser keeps it in
memory only. Unsafe routes require:

- exact configured `Origin`;
- `Sec-Fetch-Site: same-origin` (or a documented non-browser agent exception
  authenticated as `agent`);
- `X-CSRF-Token` for browser roles;
- strict `application/json`, except CSV import which later uses `text/csv`;
- valid `Idempotency-Key`.

The agent exception is allowed only after signed-IAP verification, exact
service-account RBAC, absence of browser cookies, and an agent-route allowlist.
It bypasses only Origin/Fetch-Metadata/CSRF; content type, idempotency, quoted
ETag/version, batch lease, and audit remain mandatory. Negative tests cover a
human/cookie request attempting to claim the exception.

Implement a shared staging-verification handoff helper for the later batch and
import routes. It is enabled only when
`TICKETS_ENABLE_SYNTHETIC_VERIFICATION=true` and the environment is `staging`
or the loopback fixture; production must reject that combination at startup.
When a mutating request carries a validated UUID
`X-Tickets-Verification-Run`, exact phase, and (after the first phase) the
prior handoff token, the response may include a short-lived opaque handoff
signed with an HKDF domain-separated subkey derived from
`TICKETS_CURSOR_AEAD_KEY`. Bind
environment, run ID, prior-token digest, next exact role/phase, server-created
resource IDs/versions, and expiry. The token grants no authority and every
normal IAP/RBAC/BOLA/ETag/idempotency/state check still runs. Never include
email, ticket/comment/conversation/CSV content, secrets, or bearer material.
Tests reject production enablement, tampering, replay, expiry, reordered
phases, wrong actor role, and injected IDs.

## Step 3 — Write failing API tests

Override DevRev/service/repository/evidence-client dependencies with fakes.

First write and run red tests for only the endpoints owned by Stages 1–5:

- session;
- DevRev ticket list/detail/timeline page;
- review create/list/detail/patch;
- audit-event list;
- evidence-link list/create/delete.

Remediation routes are absent until Stage 8; import/export/reverse routes are
absent until Stage 9. Assert they are not present in OpenAPI now rather than
creating misleading stubs.

For each Stage 5 endpoint cover:

- happy path and minimum role;
- 401 unauthenticated;
- 403 insufficient role;
- 404 not found without leaking whether a forbidden object exists;
- `428` missing quoted `If-Match`, `412` stale version, and `409` only for a
  valid-version business conflict;
- 422 malformed filters/IDs/body;
- 429 bounded request rate/remote DevRev limit mapping;
- 502/503 typed upstream degradation;
- immutable audit actor;
- raw DevRev cursors remain server-only; authenticated-encrypted, TTL/
  endpoint/direction/filter/subject-bound console cursors round-trip only via
  JSON response plus `X-Tickets-Cursor` request header;
- forward/back list mode and forward-only timeline cursor, with cross-route,
  cross-filter, cross-subject, expired, tampered, and replay negative tests;
- signed/filter-bound Firestore cursor uses the same header transport;
- raw cursor query parameters are rejected and URL/request logs never contain
  the remote cursor or wrapper token;
- exact `ticket_id` query is mutually exclusive with list filters, calls the
  scoped singleton service path/`works.get`, and returns zero/one item without
  list cursors; unsupported combinations return
  `422 unsupported_filter_combination`;
- timeline partial-result envelope;
- paginated audit/evidence envelopes;
- explicit evidence unlink reason/audit;
- CSRF/origin/content-type/idempotency negative matrix;
- no response includes token/config secrets;
- no response includes raw DevRev objects or unbounded chunk text.

Run the red suite before implementing routes:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_ticket_review_routes.py \
  tests/test_ticket_evidence_client.py -q
```

`PATCH` and evidence DELETE require `If-Match: "vN"`. Return
`ETag: "v<N+1>"`. On stale precondition, return safe current version and
changed-at metadata so the UI can reload; do not return another reviewer’s full
unsaved data.

All mutating responses set `Cache-Control: no-store`.

## Step 4 — Implement router

Prefix:

```python
APIRouter(prefix="/api/admin/v1", tags=["Ticket Review Console"])
```

Use service/repository methods rather than Firestore or DevRev calls inside route functions.

`ticket_evidence_client.py` calls only the configured broker URL with a
Google-signed service-to-service ID token whose audience equals
`TICKETS_EVIDENCE_BROKER_AUDIENCE`. Stream and cap the decoded response at the
canonical 512 KiB before JSON parsing, reject oversized declared/chunked/
decompressed bodies and redirects, and map degradation to an explicit
`evidence unavailable` envelope. Never fall back to a direct `(default)`
Firestore client.

Validate ticket references:

- bounded display IDs such as `TKT-123`;
- URL-safe opaque references emitted by the list API;
- never accept an arbitrary full URL;
- never concatenate input into a Firestore path.

Map typed exceptions centrally to a stable error envelope:

```json
{
  "error": {
    "code": "REVIEW_VERSION_CONFLICT",
    "message": "The review changed. Reload before saving.",
    "request_id": "..."
  }
}
```

Public messages must not echo remote bodies or stack traces.

## Step 5 — Build standalone app lifecycle

`api/tickets_console_main.py` must:

- validate tickets-console settings at startup;
- initialize one DevRev client, Firestore review backend/repository, service, and auth verifier on `app.state`;
- initialize one authenticated evidence-broker HTTP client; never a production
  `(default)` Firestore client;
- close the shared HTTP client on shutdown;
- serve `/livez` without external checks;
- serve `/readyz` with bounded Firestore/DevRev configuration checks (do not call a participant ticket);
- serve `/tickets` and `/tickets/{display_id}` from the UI directory once Stage 6 files exist; until then, a tested placeholder may say the UI stage is not installed;
- mount only the tickets assets directory at `/tickets/assets`;
- include request ID, size limit, structured logging, security headers, and safe exception middleware;
- include Origin/Fetch-Metadata/CSRF/idempotency middleware in the tested order;
- use strict same-origin CORS or no CORS—not `*`;
- not import `api.main` or initialize Pinecone/OpenAI/ForusBots.

No route in this app accepts the existing RAG `X-API-Key`.

## Step 6 — Security headers

At minimum:

```text
Content-Security-Policy:
  default-src 'self';
  script-src 'self';
  style-src 'self';
  img-src 'self' data:;
  connect-src 'self';
  object-src 'none';
  base-uri 'none';
  frame-ancestors 'none';
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Permissions-Policy: camera=(), microphone=(), geolocation=()
Cache-Control: no-store   (HTML and authenticated API)
```

Do not add inline script/style exceptions. Stage 6 assets must be external files.

## Step 7 — Verify

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_reviewer_auth.py \
  tests/test_tickets_csrf.py \
  tests/test_ticket_review_routes.py \
  tests/test_tickets_console_app.py \
  tests/test_ticket_evidence_client.py -q
"$PYTHON_BIN" -m pytest tests/test_ticket_security.py tests/test_api.py -q
"$PYTHON_BIN" -m compileall -q api data_pipeline
"$PYTHON_BIN" -m pip check
git -C "$IMPL_ROOT" diff --check
```

Inspect OpenAPI:

```bash
"$PYTHON_BIN" - <<'PY'
from api.tickets_console_main import app
schema = app.openapi()
assert all(path.startswith(("/api/admin/v1", "/livez", "/readyz", "/tickets")) for path in schema["paths"])
paths = set(schema["paths"])
assert not any("remediation-batches" in path for path in paths)
assert not any("/imports/" in path or "/exports/" in path for path in paths)
print("admin OpenAPI paths:", len(schema["paths"]))
PY
```

Do not start a production deployment.

## Definition of Done

- Admin app does not import/initialize the RAG data plane.
- Signed IAP JWT is the only production human identity source.
- RBAC is enforced server-side on every route.
- No direct console/agent access to production `(default)` exists.
- CSRF/origin/content-type/idempotency and canonical ETag rules are enforced.
- Stale writes are impossible without a visible conflict.
- Security headers/CORS/request bounds are tested.
- Existing RAG tests remain green.

## Commit

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/api/reviewer_auth.py \
  kb-rag-system/api/tickets_csrf.py \
  kb-rag-system/api/ticket_review_routes.py \
  kb-rag-system/api/tickets_console_main.py \
  kb-rag-system/data_pipeline/ticket_evidence_client.py \
  kb-rag-system/tests/test_reviewer_auth.py \
  kb-rag-system/tests/test_tickets_csrf.py \
  kb-rag-system/tests/test_ticket_review_routes.py \
  kb-rag-system/tests/test_tickets_console_app.py \
  kb-rag-system/tests/test_ticket_evidence_client.py
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow kb-rag-system/api/reviewer_auth.py \
  --allow kb-rag-system/api/tickets_csrf.py \
  --allow kb-rag-system/api/ticket_review_routes.py \
  --allow kb-rag-system/api/tickets_console_main.py \
  --allow kb-rag-system/data_pipeline/ticket_evidence_client.py \
  --allow kb-rag-system/tests/test_reviewer_auth.py \
  --allow kb-rag-system/tests/test_tickets_csrf.py \
  --allow kb-rag-system/tests/test_ticket_review_routes.py \
  --allow kb-rag-system/tests/test_tickets_console_app.py \
  --allow kb-rag-system/tests/test_ticket_evidence_client.py
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit -m "feat(tickets): expose authenticated admin API"
```

If an already-declared dependency is missing, stop and use the repository's
Python 3.12 lock-generation workflow in Stage 10; do not hand-edit a lock.
Proceed to Stage 6.
