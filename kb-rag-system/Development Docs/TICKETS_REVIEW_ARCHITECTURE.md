# ADR: `/tickets` RAG Review Console Architecture

**Status:** Accepted (Stage 1 — contracts frozen)
**Date:** 2026-07-27
**Applies to:** `kb-rag-system` administrative plane, evidence broker, and the
producer-side correlation contract.

This ADR freezes the architecture, contracts, and non-goals of the `/tickets`
review console *before* any network, Firestore, UI, or infrastructure code is
written. Changing anything recorded here requires a new ADR and cross-layer
contract tests — the canonical limits table in particular is mirrored literally
in `api/ticket_review_models.py` and asserted in
`tests/test_ticket_review_models.py`.

---

## 0. Implementation base and its amendment

The plan package froze the audited implementation base at
`eed9b34967c59b8bfec34026c9a8637581f2036a`. At Stage 1 execution time the
clean finalization worktree
(`/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization`, branch
`codex/fix-cloudbuild-shell-escaping`) had advanced **11 commits** past that
SHA to `04fe252e9e03356f0f13e02e5a48714ac397b78b` ("Document production
handoff"), so the Stage 1 preflight equality gate failed.

**Decision:** the base was amended, with explicit approval, to `04fe252`.

Rationale, recorded so a later reviewer need not re-derive it:

* `eed9b34` remains an **ancestor** of `04fe252` — the advance is a clean
  fast-forward with no divergence, so the audited base is still contained in
  the implementation history.
* Every stage after Stage 1 asserts only
  `git merge-base --is-ancestor "$TICKETS_BASE_SHA" HEAD`. Because `eed9b34`
  is an ancestor of `04fe252`, all downstream preflights remain satisfied with
  no plan edits.
* The 11 commits touch 16 paths: `Dockerfile`, `cloudbuild.yaml`,
  `ci/tool-images.env`, `ci/cloudbuild.verify-local.yaml`,
  `infra/terraform/modules/ticket_environment/monitoring.tf`, `api/main.py`,
  `api/entrypoint.py`, `scripts/release_controller.py`,
  `scripts/upload_gcs_write_once.sh`,
  `Development Docs/handle-ticket/POST_MERGE_PRODUCTION_REVIEW.md`, and the
  `test_api` / `test_cloudbuild_artifact_contract` / `test_container_contract` /
  `test_deployment_contract` / `test_monitoring_contract` /
  `test_release_controller` suites.
* Stage 10 works on an overlapping subset of those paths — it references
  `Dockerfile`, `ci/cloudbuild.verify-local.yaml`, and `monitoring.tf` — so
  building on the older base would have created avoidable conflicts in the
  infrastructure stage. (It does not touch `cloudbuild.yaml`,
  `ci/tool-images.env`, `api/main.py`, or `api/entrypoint.py`, so the overlap is
  partial rather than total.)
* They do **not** touch `requirements.in`, the generated locks, or
  `firestore.indexes.json`, so the dependency and Firestore contracts this
  stage depends on are unchanged.

Stage 1's own deliverables are base-insensitive: none of them appears in that
diff.

---

## 1. Separate administrative plane vs the RAG data plane

The existing `kb-rag-system` Cloud Run service stays private, keeps its
n8n-facing service-account invoker, and its public API boundary is unchanged.
The console is a **separate** FastAPI entrypoint and a separate Cloud Run
service.

* The admin service serves `GET /tickets`, `GET /tickets/{display_id}`, and
  same-origin `/api/admin/v1/**`.
* The `/api/admin/v1` namespace deliberately avoids colliding with the
  existing `GET /api/v1/tickets/{ticket_job_id}` job-poll route.
* Firestore is reached only through server-side Google Cloud clients. There is
  no Firebase browser app, no Firebase config shipped to the UI, and no direct
  browser access to Firestore.

**Configuration isolation is a hard rule, not a preference.**
`api/config.py` constructs its `settings = Settings()` singleton at import
time and transitively imports `data_pipeline.forusbots_client` and
`data_pipeline.llm_router`, which pull in `httpx`, `cachetools`,
`openai.AsyncOpenAI`, and `google.genai`. `api/tickets_console_config.py`
therefore never imports `api.config` at module scope; a test asserts the
absence of that import in the module source. The console settings instance is
also never named `settings`, because that bare name means the RAG singleton
everywhere else in this repository.

## 2. Authentication: signed IAP JWT plus application RBAC

**Rejected:** the existing `X-API-Key` header with the key stored in browser
`localStorage`, as used by the current UI. A long-lived shared secret readable
by any script on the page, with no per-user identity, is not an acceptable
basis for an administrative plane that exposes reviewer identity and audit
history.

**Accepted:** production human access is fronted by direct Cloud Run IAP. The
application additionally verifies `X-Goog-IAP-JWT-Assertion` cryptographically
and checks the expected Cloud Run audience. Unsigned identity headers are
never trusted.

Roles are `viewer | reviewer | remediator | admin | agent`, resolved from
configuration, never from a client claim. `TICKETS_DEFAULT_ROLE` is `None` and
`TICKETS_ALLOW_UNBOUND_VIEWERS` is `false`: an identity with no binding is
**denied**, not silently downgraded to read-only.

The sole non-browser exception is an IAP-verified exact `agent`
service-account identity carrying no cookie or browser session. It may omit
the browser-only Origin/Fetch-Metadata/CSRF checks, but it still requires
strict content type, `Idempotency-Key`, a quoted ETag, lease checks, and a
narrow route allowlist. A cookie-bearing request never receives that
exception.

## 3. Request integrity: CSRF, origin, content type, idempotency, ETags

Every unsafe endpoint requires an exact same-origin `Origin`, an acceptable
`Sec-Fetch-Site`, an in-memory session-bound `X-CSRF-Token` (60-minute
lifetime, bound to subject/session), a strict content type, and an
`Idempotency-Key`.

`PATCH` and evidence `DELETE` additionally require a quoted strong validator
`If-Match: "vN"`, and responses return `ETag: "v<N+1>"`. The three
precondition outcomes are frozen in `http_status_for_precondition_error`; the
`409` row is listed for completeness and is *not* a precondition failure, so it
is deliberately not a branch of that function:

| Condition | Status |
|---|---:|
| `If-Match` absent | `428` |
| `If-Match` present but not a quoted `"vN"` | `422` |
| `If-Match` valid but stale | `412` |
| Valid version, business/lease/idempotency conflict | `409` |

Weak validators (`W/"v3"`), `*`, multiple validators, leading zeros, and a
trailing newline are all refused: a blind overwrite of another reviewer's work
is never acceptable. The header is length-bounded *before* parsing, because an
unbounded digit run would otherwise make `int()` raise a bare `ValueError`
(CPython's 4300-digit limit) and surface as a `500` instead of the contract's
`422`. A present-but-empty header is treated as *missing* (`428`, "supply a
version") rather than malformed, which is the more actionable answer.

A stale response returns only the safe current version and changed-at
metadata — never the other reviewer's unsaved content; `ErrorBody` declares
exactly those two extra fields. All mutating responses set
`Cache-Control: no-store`.

## 4. DevRev is server-side and read-only in the MVP

The browser never receives a PAT/SUT, a DevRev bearer token, or a Secret
Manager reference. The MVP uses a rotatable PAT owned by a dedicated DevRev
integration user limited to the approved parts and `ticket:read`. No
ticket-write privilege is accepted. PAT creation, rotation, and revocation
remain an **external DevRev-owner gate**.

MVP methods, with `X-Devrev-Version: 2022-10-20` pinned and no beta APIs in
the critical path:

* `POST /works.list` with `type=["ticket"]` forced server-side, structured
  filters, `mode=after|before`, cursor pagination;
* `GET /works.get?id=...`;
* `GET /timeline-entries.list?object=...`, always `mode=after`.

Access is fail-closed to configured `applies_to_part` DONs, ticket-visibility
integer IDs, and timeline visibility enums. A direct ticket ID cannot bypass
that scope check. Owner/tag *name* lookups are feature-gated off until
`/dev-users.list` and `/tags.list` scope, pagination, and privacy are
separately approved.

**Pagination invariant (the one that bites):** a page may contain fewer
entries, or zero entries, while another cursor still exists. Iteration stops
only when `next_cursor` is absent. A configured maximum-page/entry guard
produces an explicitly `partial`/`truncated` result — a guarded result is never
labelled complete — and Stage 2's client must raise a typed
`DevRevPaginationError` on a repeated cursor rather than looping forever. (That
exception type belongs to the Stage 2 client; Stage 1 freezes only the
`CursorPage`/`TimelinePage` envelope that carries the markers.)
`tests/fixtures/devrev/timeline_page_empty_with_cursor.json` exists solely to
freeze this case.

### 4.1 The `better_devrev_search` reference: what was taken and what was rejected

`/Users/ivanalvis/Desktop/better_devrev_search` was inspected read-only.

**Informative:** its structured-filter routing model (free text →
`search.core`; stage/owner/creator/reporter/tag → `works.list`) and its error
presentation shape usefully informed the typed adapter and the error envelope.
Its documented reason for two endpoints — `search.core` is full-text only and
silently ignores `state:` filters — is a real DevRev behavior worth recording.

**Explicitly rejected, and why:**

* **Browser-held PAT.** The credential is stored in `chrome.storage.local` and
  re-read on every request. A `type="password"` input is masking, not
  protection. Our token exists only in Secret Manager and server memory.
* **Persisted browser storage of results.** It writes every result's full
  `raw` DevRev object into a single overwritten `lastSearch` key. We never
  persist raw remote payloads client-side, and no console model has a `raw`
  field at all.
* **Incomplete cursor handling.** It has no pagination whatsoever: `works.list`
  is a single call with a hardcoded `limit: 50`, and the directory endpoints
  memoize a truncated first page as the permanent universe, producing silent
  false negatives. Its own docstring still claims a pagination loop that no
  longer exists.
* **Truncated count reported as a total.** `total = filtered.length` after a
  capped single-page fetch is simply wrong. We never fabricate a global result
  count DevRev does not provide; the UI shows page size and whether another
  cursor exists.
* **Inconsistent, human-only filter degradation.** A zero-match owner aborts
  the search, while a zero-match creator/reporter/tag is dropped and the search
  continues on a broader result set. The reference does surface this to the
  *user* as a note string, so it is not literally silent — but the degradation
  is not machine-visible, and the two policies differ per field. The console
  picks one explicit policy and makes partial results a typed field
  (`partial`/`truncated`/`warnings`) rather than prose.
* **No detail, no timeline, no durable audit.** It fetches list rows only and
  keeps no append-only record of what was queried, by whom, or what returned.

No credential handling or persisted browser-storage behavior was copied.

## 5. Storage: a dedicated named Firestore database

Console state lives in a dedicated `tickets-console-staging` /
`tickets-console-prod` **named** database. This is load-bearing:
`roles/datastore.user` is database-scoped, not collection-scoped, so the
console service account must never hold it on production `(default)`.
Startup rejects `(default)` in staging and production.

Access to the existing production `(default)` logs is mediated by a separate
`tickets-evidence-broker` Cloud Run service with its own read-only service
account. It exposes exactly one bounded service-to-service endpoint,
`POST /internal/v1/ticket-evidence:lookup`, invocable only by the console
service account. The broker initializes no Pinecone, OpenAI, DevRev, or
participant-response path. This is an explicit **application-level** narrowing
layered on top of a necessarily broad database role — IAM cannot narrow
Firestore access by collection, and this ADR records that limitation rather
than pretending otherwise.

Firestore is a structured review and audit store, **not** a blind mirror of
DevRev. Collections:

```text
ticket_reviews/{review_id}
  audit_events/{event_id}
  evidence_links/{link_id}
remediation_batches/{batch_id}
  items/{review_id}
  events/{event_id}
ticket_imports/{import_id}
  rows/{row_id}
ticket_exports/{export_id}
ticket_console_audit_events/{event_id}
devrev_message_cache/{message_id_hash}     # TTL
ticket_console_cache/{cache_key}           # TTL
ticket_import_staging/{staging_id}         # TTL
idempotency_keys/{key_hash}                # TTL
```

`review_id` is `sha256(normalized DON)` in lowercase hex. A DON contains `/`
and can never be a Firestore document ID directly.

Only cache, import-staging, and idempotency documents carry TTL fields.
Durable review/audit/batch/import/export documents carry
`retention_expires_at` and `legal_hold` instead. **Expiring a cache must never
delete a human review or audit record.**

## 6. Two distinct queues, and a deliberately small query grammar

* **All DevRev tickets** — live discovery through DevRev's opaque forward and
  backward cursors, with a Firestore review overlay.
* **Review queue** — durable Firestore records.

The Firestore query grammar is intentionally minimal:

* an exact normalized Ticket ID is a standalone lookup and cannot be combined
  with queue facets;
* a normal queue query uses a status set plus **at most one** of
  `topic | rating | assigned_reviewer.email | observation_type | severity |
  remediation_target`, an optional `updated_at` range, and the stable ordering
  `updated_at DESC, review_id ASC`;
* there is no substring or full-text title search in the MVP;
* unsupported combinations return `422 unsupported_filter_combination` and the
  UI disables them, rather than issuing an unindexed query.

## 7. Cursors never cross the boundary in the clear

Raw DevRev cursors and raw Firestore cursors are server-only. The API
authenticated-encrypts them into short-lived console tokens bound to
endpoint, direction, filter fingerprint, and subject.

The implementation uses **AES-256-GCM from the already pinned
`cryptography==49.0.0`** in `requirements.lock`, with the binding context
supplied as AES-GCM associated data. Base64-encoding unsigned JSON and calling
it opaque is explicitly rejected, and no bespoke cipher was added.

`TICKETS_CURSOR_AEAD_KEY` must decode to exactly 32 bytes. Staging and
production reject a missing or wrong-length key at startup; `local` does not
require one at all, because a fixture-backed local app that never issues a
cursor should not be forced to invent a key. Any code path that actually seals
or opens a cursor calls `decode_cursor_aead_key`, which raises regardless of
environment.

Two properties are enforced beyond "it's encrypted":

* **Canonical encoding.** Unpadded base64 leaves spare bits in the final
  character, so several distinct strings decode to identical bytes, and
  `base64.urlsafe_b64decode` silently discards non-alphabet bytes entirely.
  `open_cursor` therefore accepts only the exact base64url alphabet and
  re-encodes the decoded bytes to confirm the token is the one canonical
  encoding. Without this, a token is malleable: whitespace-injected,
  standard-alphabet, and last-character-flipped variants would all open the
  same cursor. A test mutates every character position of many tokens and
  requires every single mutation to be rejected.
* **Expiry.** An absolute expiry is sealed *inside* the ciphertext (`_exp`), so
  it is covered by the authentication tag and cannot be extended by a caller,
  and it is stripped before the payload is returned. The default window is the
  15-minute live-cache TTL, so a cursor cannot outlive the data it points at.

Tokens travel in the JSON response body and in the `X-Tickets-Cursor` request
header — never in a query string, URL, browser history, analytics event, or
application log. Replay against another endpoint/filter/subject, expired or
tampered tokens, non-canonical encodings, and raw cursor query parameters all
return a safe `422`.

> **Dependency note (carried to Stage 10/11):** `cryptography` is currently a
> *transitive* pin in `requirements.lock`, not a direct entry in
> `requirements.in`. Stage 1 imports it directly, so it should be promoted to
> a direct input at the next Python 3.12 lock regeneration. It was not
> promoted here because regenerating the hash-pinned lock requires the
> Python 3.12 Cloud Build workflow, which is the authoritative gate and is not
> available on this host.

## 8. No fabricated provenance

Every review renders exactly one correlation state:

* `linked` — direct ticket/job/request identifiers connect the ticket to RAG
  evidence;
* `manual` — a reviewer explicitly linked evidence;
* `unavailable` — no defensible correlation exists.

Timestamp or text similarity may be shown as a *suggestion*; it is never
stored as a confirmed link without a reviewer action. `RagProvenance` defaults
to `correlation_status=unavailable`, `correlation_trust=none`, and
`missing_provenance=True` — the model asserts nothing it cannot prove.

A record is auto-`linked` only when correlation metadata arrives from the
active n8n workflow through **both** its existing Cloud Run IAM boundary
**and** a replay-protected signed context (timestamp, normalized DON, request
digest, existing idempotency-key hash) using a dedicated **ingress** key. A
**different** lookup key derives `HMAC-SHA256(lookup_key, DON)` for storage
and query. The RAG service stores only that lookup HMAC, the ingress and
lookup key versions, the configured workload-binding hash, source/trust level,
and existing safe request hashes — never a raw external DON or display ID.
Unverified headers create a candidate suggestion only.

Lookup-key rotation is versioned precisely because the raw DON is
intentionally absent: the producer writes with one current version, the broker
holds a bounded keyring of active numeric versions, derives one candidate HMAC
per version, and queries the composite `(lookup_key_version,
ticket_lookup_hmac)` index with fixed fan-out and result limits. Old versions
survive the evidence-retention window or their records become explicitly
`unavailable`. Revocation is never silent.

**External-owner gate:** the active n8n owner must implement and verify this
contract, or the release explicitly ships with `correlation_status=unavailable`.
Optional client headers never satisfy this gate.

### 8.1 Three secret boundaries that refuse each other's keys

| Plane | Holds | Must never receive |
|---|---|---|
| Console | DevRev token, CSRF secret, cursor AEAD key | either correlation key, the broker keyring |
| Evidence broker | versioned lookup keyring + allowed versions | the ingress key, a DevRev token |
| Producer / n8n | ingress key + current lookup key (n8n gets ingress signing only) | the broker keyring, a DevRev token |

Each `validate_*_settings` function inspects the injected environment mapping
and **fails startup** when a foreign secret is present. A secret delivered to
the wrong revision is a deployment failure, not something to ignore.

## 9. Legacy sheet fields stay first-class, and stay separated

The six sheet columns are preserved: Ticket ID, Topic, Type, Rating (1–5),
Reviewer, Comments.

Two separations are load-bearing and are enforced by distinct model fields:

* `legacy_type` (bounded free text, imported/exported as `Type`) is **not**
  `observation_type` (the closed root-cause taxonomy). The legacy value is
  never silently coerced into the taxonomy; a versioned alias map may only
  *suggest* one, and an admin must confirm it.
* `assigned_reviewer` (an application identity; self-assignable by a reviewer,
  reassignable by an admin) is **not** `legacy_reviewer_display_name` (the
  original CSV text) and is **not** the authenticated audit actor, which every
  audit event records independently.

Rating validation rejects booleans explicitly, because `bool` is a subclass of
`int` in Python and a checkbox would otherwise become a rating of 1. Strings
and floats are rejected too, despite Pydantic's lax mode accepting them.

## 10. Audit integrity

Audit events are **application-append-only** and hash-chained via
`previous_event_hash` / `event_hash`. This ADR deliberately does not claim
Firestore is physically immutable: the chain is *tamper-evident*, and it is
paired with Firestore Data Access audit logs routed to a dedicated
retention-controlled Cloud Logging bucket. The app exposes no update or delete
endpoint for audit records, and the `AuditEvent` model is frozen.

The hash input is frozen exactly. Before hashing, validated string values are
NFC-normalized and an object is built containing only these literal keys:

```text
hash_schema_version, event_id, parent_kind, parent_id, event_type,
actor_subject_hash, request_id_hash, idempotency_key_hash,
previous_version, new_version, changed_fields, reason_code,
previous_event_hash, occurred_at_unix_us
```

Absent values are JSON `null`; a key is never omitted. `changed_fields` is
sorted and deduplicated. Serialization is
`json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
allow_nan=False)`, UTF-8 encoded, stored as lowercase hex SHA-256. Genesis
`previous_event_hash` is exactly 64 ASCII zeroes. `occurred_at_unix_us` comes
from an injected server-side clock.

Excluded from the hash: the unresolved Firestore `SERVER_TIMESTAMP`, the raw
actor email and subject, display text, and every later-enriched field.
`created_at` is stored separately as `SERVER_TIMESTAMP`. Audit events record
changed *field names* and bounded operational metadata — never a duplicate of
the full old/new comment bodies.

Because only `reason_code` from `metadata` enters that frozen payload, the rest
of the map would otherwise be durable free text that the chain does not cover.
`AuditEvent.metadata` is therefore hard-bounded (at most 8 keys, 64-character
keys, 200-character values) so it cannot become either a PII route into the
ledger or a place to hide unhashed content. A test asserts the frozen key set
literally, that absent values serialize as `null` rather than being omitted,
and that a comment-sized value is rejected.

Residual risk, recorded deliberately: `actor_subject_hash` is included in the
hash while the raw `actor_subject`/`actor_email` are not, and Stage 1 does not
bind the two. A principal with direct datastore write access could therefore
alter the *displayed* actor without breaking the chain. This is consistent with
the plan's explicit exclusion of raw actor identity from the hash input, and it
is why Firestore Data Access audit logs are a required, independent control
rather than an optional extra. Stage 3 should derive `actor_subject_hash`
server-side from the verified assertion and never from client input.

## 11. Field classification, retention, and legal hold

Durable review records store structured reviewer judgment, not a copy of the
DevRev conversation.

**The durable `TicketReview` has no DevRev title field at all.** Titles and
message bodies live only in live responses and the bounded TTL cache.
Truncation is not redaction.

Stage 1 tests prove the *model-level* half of this: the durable review declares
no title field, `extra="forbid"` rejects one, and no title survives a durable
review serialization, while the same synthetic title containing an email and a
phone number is accepted on the live/cache models. The
`works_list_page_1.json` fixture supplies exactly that material. Export and
log enforcement cannot be tested here because Stage 1 contains no export or
logging code; the master plan assigns those proofs to the Stage 3 repository
tests and the Stage 9 export tests, and they remain required.

| Data | Retention |
|---|---:|
| Durable review / batch / import / export | 730 days after final activity |
| Audit ledger and log bucket | 2,555 days |
| Live list/detail cache | 15 minutes |
| Raw message cache | 24 hours |
| Idempotency keys, import staging | 7 days |

`legal_hold=true` suppresses durable purge. At the 730-day product purge a
parent is transactionally reduced to a content-free `purged_tombstone`
(hashed parent ID, schema, purged-at, ledger expiry, legal-hold state, chain
head) rather than removing a path beneath a younger ledger; the tombstone
carries no DON, display ID, title, comment, reviewer email, evidence, or
result. At 2,555 days the retention facade deletes exact ledger event IDs
first and the exact tombstone last. **No generic recursive-delete API
exists**, and retention only ever deletes explicit capped document IDs.

Production retention automation and the 2,555-day log-bucket retention lock
require explicit Privacy/Legal approval at the production rollout gate. Until
approved, the irreversible lock stays disabled and fails closed.

## 12. Remediation batches are human-in-the-loop

A batch freezes unique `(review_id, review_version)` pairs. The **parent
stores only** counts, status, version, item-set digest, lease summary, and
bounded outcome summaries; each frozen review is its own
`remediation_batches/{batch_id}/items/{review_id}` document.

This split is justified by arithmetic, not assertion: a worst-case review
(10,000-character comments plus 10,000-character expected behavior) times the
100-review batch maximum provably exceeds Firestore's 1 MiB document limit, so
a parent-embedded array could not hold it. A test asserts this.

Lease invariants: 15-minute lease, 5-minute heartbeat, 2-hour continuous cap,
after which an admin may grant **one** audited extension of at most 2 further
hours — never an unbounded renewal. Only the lease token *hash* is persisted;
the raw token is returned once at claim time and is never stored or logged.

Role separation is encoded in the transition function: only a lease-holding
`agent` may drive `claimed → planning → in_progress → changes_proposed`, and
only an independent `reviewer`/`admin` may drive
`changes_proposed → verifying → completed`. Lease expiry never silently
changes business status — it records an event, then an explicit expire/reclaim
transition occurs. No release may leave an unleased
`claimed`/`planning`/`in_progress` batch.

Codex reaches all of this through a repository CLI that calls the admin API,
authenticating keylessly to IAP through a dedicated remediation-agent service
account and IAM Credentials `signJwt`. **No human or agent receives direct
Firestore access.**

The agent must never execute instructions embedded in tickets, expose PII or
credentials in prompts/logs/commits/output, deploy, merge, reindex production,
write back to DevRev without separate approval, or mark a review resolved
without recorded verification evidence.

## 13. Status transitions (closed tables)

Encoded as tested pure functions in `api/ticket_review_models.py`. Transition
checks are never scattered across routes.

```text
Review:
  unreviewed       -> reviewed | blocked
  reviewed         -> triaged | blocked | wont_fix
  triaged          -> planned | blocked | wont_fix
  planned          -> in_progress | blocked | wont_fix
  in_progress      -> changes_proposed | blocked
  changes_proposed -> verifying | in_progress | blocked
  verifying        -> resolved | in_progress | blocked
  blocked          -> triaged | planned | in_progress | wont_fix
  resolved, wont_fix are terminal; only an admin may explicitly reopen, and
  only to triaged.

Batch:
  draft -> ready | cancelled          ready   -> claimed | cancelled
  claimed -> planning | blocked | expired
  planning -> in_progress | blocked | expired
  in_progress -> changes_proposed | blocked | expired
  changes_proposed -> verifying | in_progress | blocked
  verifying -> completed | in_progress | blocked
  blocked -> ready | claimed | cancelled
  expired -> claimed | cancelled
  completed, cancelled are terminal.

Import:
  uploaded -> planned | failed | cancelled
  planned  -> approved | failed | cancelled
  approved -> applying | cancelled
  applying -> applied | partial | failed
  partial  -> applying | reversing | cancelled
  applied  -> reversing
  reversing -> reversed | partial | failed
  failed   -> planned | applying | reversing | cancelled (explicit admin reason)
  reversed, cancelled are terminal.
```

A terminal review resolution requires a closed `ReviewResolution` carrying
either structured verification evidence or an explicit `no_change_reason`; an
outcome other than `fixed` always requires the reason. A reversal never
deletes history: a newly imported review becomes `import_state="reversed"` and
is hidden from the default queue, while a modified pre-existing review
receives a version-checked compensating patch. Conflicts stay visible for
manual resolution.

## 14. UI posture

Vanilla HTML/CSS/ES modules, no build step, but separate files and modules
rather than another monolithic HTML document.

* No `innerHTML` with remote or user content.
* No credentials and no local PII cache in `localStorage`.
* CSP and security headers; same-origin API only.
* Links use validated `https:` origins with `rel="noopener noreferrer"`.
* Table becomes cards below 768 px; touch targets at least 44 px; no
  horizontal overflow at 360 px.
* Status is never communicated by color alone.
* An optimistic-concurrency conflict is surfaced explicitly and never silently
  overwrites another reviewer.

A DevRev deep-link template is **optional**. When it is absent the UI copies
the display ID and opens the configured DevRev organization root; it must not
invent a deep-link pattern.

## 15. Non-goals for the MVP

* No DevRev webhooks and no Cloud Tasks. Live reads, explicit refresh, and
  bounded cache hydration meet the initial need; webhooks would require HMAC
  verification, duplicate/out-of-order handling, a queue, and reconciliation.
  Cloud Tasks is disabled in the observed production project and this feature
  does not require enabling it.
* No DevRev writes of any kind.
* No direct Git, DevRev, or Pinecone production mutation from the web service.
* No owner/tag display-name resolution.
* No substring or full-text title search.
* No fabricated global result count.
* No Firebase browser SDK.
* No new Pinecone index or reindex until an approved remediation actually
  changes KB content.

## 16. Testing and tooling conventions this stage adopts

* Every **domain and envelope** model uses `ConfigDict(extra="forbid")`; unknown
  remote or client fields are errors. Unknown remote data is preserved only in a
  bounded, explicitly-typed diagnostic field
  (`DevRevTimelineEntry.unsupported_type`) — never via `extra="allow"`. The
  three **settings** classes are the deliberate exception: they use
  `extra="ignore"` because a single shared process environment necessarily
  contains variables belonging to other components, and a settings class that
  rejected them could not start. Cross-boundary secrets are caught explicitly by
  the `validate_*` functions instead of by `extra`.
* All datetimes are timezone-aware and normalized to UTC. Naive datetimes are
  rejected. A non-UTC aware value is converted rather than stored as-is, and a
  test asserts the conversion on both a top-level and a nested field.
* Secrets are `pydantic.SecretStr`, which masks them in `repr`, in
  `model_dump()`, and in `model_dump_json()`. This covers the DevRev token, CSRF
  secret, cursor key, lease tokens, the broker keyring, the correlation keys,
  and `ROLE_BINDINGS_JSON` — the last because the role map is the
  email-to-privilege authorization matrix and the plan delivers it as a
  confidential secret version. As a side benefit it keeps ruff/bandit `S105`
  quiet on module-level constants that merely *name* a secret environment
  variable; those constants carry an explicit `# noqa: S105` with a comment.
* Settings validation is a separate module-level function accumulating an
  `errors` list, mirroring the repository's existing `validate_settings()`
  idiom, and it takes an injectable `env` mapping so tests never depend on the
  developer's shell.
* `scripts/verify_staged_scope.py` is a read-only commit-scope guard. It uses
  NUL-delimited `git diff --cached --name-only -z` so paths containing spaces,
  quotes, or newlines are handled correctly, prints unexpected paths to
  stderr, and exits `2`. It never stages, unstages, or edits anything. Every
  later stage must use it with an exact allowlist before committing.

  It resolves the repository root from **its own script directory**, not the
  process working directory, with an explicit `--repo` override. This matters:
  the plan's commit blocks invoke the guard by absolute path
  (`"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py"`) without
  changing directory, so a cwd-anchored guard silently inspected whichever
  repository the shell happened to be in and exited `0` while the repository
  actually being committed held unreviewed paths. That failure was observed
  during Stage 1 — the guard's first real invocation passed vacuously — and a
  regression test now pins the anchoring. A guard that can pass vacuously is
  worse than no guard.
* DevRev fixtures are entirely synthetic. Display IDs use the `TKT-1234`
  range, all domains are `example.invalid`, and every DON carries the
  unmistakable `devo/SYNTHETIC00` tenant segment. Each fixture carries a
  `_meta` provenance/sanitization envelope with the payload under a
  `response` key, matching the existing `tests/fixtures/` convention.

## 17. Official sources that constrain the implementation

* [DevRev authentication](https://developer.devrev.ai/about/authentication)
* [DevRev pagination](https://developer.devrev.ai/about/pagination)
* [DevRev rate limits](https://developer.devrev.ai/about/rate-limits)
* [DevRev API errors](https://developer.devrev.ai/about/errors)
* [DevRev API versioning](https://developer.devrev.ai/about/versioning)
* [Works List](https://developer.devrev.ai/api-reference/works/list-post)
* [Works Get](https://developer.devrev.ai/api-reference/works/get)
* [Timeline Entries List](https://developer.devrev.ai/api-reference/timeline-entries/list)
* [DevRev webhooks guide](https://developer.devrev.ai/guides/webhooks) — reference only; out of MVP scope
* [Cloud Run direct IAP](https://cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run)
* [IAP signed JWT validation](https://cloud.google.com/iap/docs/signed-headers-howto)
* [IAP programmatic authentication](https://cloud.google.com/iap/docs/authentication-howto)
* [Terraform `google_cloud_run_v2_service`](https://registry.terraform.io/providers/hashicorp/google/7.41.0/docs/resources/cloud_run_v2_service)

---

## 18. Appendix A — configuration fail-closed rules

`ENVIRONMENT` is the switch that enables every hardening rule, so its **default
is `production`**, not `local`. This is deliberate and was changed after the
Stage 1 review found the opposite: with a `local` default, a revision whose
Terraform set `TICKETS_AUTH_MODE=iap` but omitted `TICKETS_ENVIRONMENT`
validated completely clean with no DevRev token, no IAP audience, no CSRF
secret, no cursor key, an empty Firestore database, and empty DevRev
allowlists. Local development now opts out explicitly with
`TICKETS_ENVIRONMENT=local`. `EvidenceBrokerSettings` defaults the same way for
the same reason. `VALID_ENVIRONMENTS` is exactly `local | staging | production`;
a fourth value would create another non-strict path.

Staging and production additionally reject:

| Rejected | Why |
|---|---|
| `AUTH_MODE != iap`, `ALLOW_LOCAL_AUTH=true` | unsigned identity |
| `ENABLE_SYNTHETIC_VERIFICATION=true` (production) | fake verification evidence |
| `ALLOW_UNBOUND_VIEWERS=true`, `DEFAULT_ROLE` set | an unbound identity must be denied |
| missing `IAP_AUDIENCE` / `ROLE_BINDINGS_JSON` | no audience or no authorization matrix |
| `ROLE_BINDINGS_JSON` that is not a non-empty object | an empty map authorizes nobody but looks configured |
| `ALLOWED_EMAIL_DOMAINS` empty, containing `*` anywhere, or with no dot | suffix/infix wildcards and bare TLDs match far more than intended |
| missing `DEVREV_TOKEN` / `CSRF_SIGNING_SECRET` / `CURSOR_AEAD_KEY` | required secrets |
| `CURSOR_AEAD_KEY` not decoding to exactly 32 bytes | wrong key size |
| `FIRESTORE_DATABASE` empty or `(default)` | database-scoped IAM |
| missing `GCP_PROJECT` / `GCP_REGION` | needed to pin the broker host |
| `EVIDENCE_BROKER_URL` non-https, or not the Cloud Run host for the configured region | prevents pointing the console at an arbitrary external host |
| empty DevRev part / ticket-visibility / timeline-visibility allowlists | fail-closed scope |
| `DEVREV_API_BASE` other than `https://api.devrev.ai` without the explicit non-production override | wrong origin |
| the override enabled at all in production | production talks only to DevRev |
| any numeric setting above its canonical value, or non-positive | see below |

**Canonical limits cannot be loosened by an environment variable.** The
`_canonical_bound_errors` check refuses a staging/production revision that
raises the DevRev response cap, enlarges a page or iterator guard, stretches a
lease or heartbeat, or extends a TTL or retention window beyond the value frozen
in `ticket_review_models.py`. Without it, the "single source of truth" claim
would hold only for code and not for deployment. `REMEDIATION_HEARTBEAT_S` must
also be strictly shorter than `REMEDIATION_LEASE_S`.

`DEVREV_API_BASE` may be plain `http` **only** for a local fixture server with
the non-official-base override enabled; staging and production always require
`https`.

## 19. Appendix B — exact API surface

`/api/admin/v1`, chosen to avoid the existing
`GET /api/v1/tickets/{ticket_job_id}` job-poll route.

| Method | Path | Min role |
|---|---|---:|
| GET | `/session` | viewer |
| GET | `/devrev-tickets` | viewer |
| GET | `/devrev-tickets/{id}` | viewer |
| GET | `/devrev-tickets/{id}/timeline` | viewer |
| POST | `/devrev-tickets/{id}/reviews` | reviewer |
| GET | `/ticket-reviews` | viewer |
| GET | `/ticket-reviews/{review_id}` | viewer |
| PATCH | `/ticket-reviews/{review_id}` | reviewer |
| GET | `/ticket-reviews/{review_id}/audit-events` | viewer |
| GET | `/ticket-reviews/{review_id}/evidence-links` | viewer |
| POST | `/ticket-reviews/{review_id}/evidence-links` | reviewer |
| DELETE | `/ticket-reviews/{review_id}/evidence-links/{link_id}` | reviewer |
| POST | `/remediation-batches` | remediator |
| GET | `/remediation-batches/{batch_id}` | object-scoped reviewer/remediator or claimed agent |
| GET | `/remediation-batches/{batch_id}/items` | object-scoped reviewer/remediator or claimed agent |
| POST | `/remediation-batches/{batch_id}:ready` | remediator/admin |
| POST | `/remediation-batches/{batch_id}/claim` | agent |
| POST | `/remediation-batches/{batch_id}/heartbeat` | agent |
| POST | `/remediation-batches/{batch_id}:materialize` | claimed agent |
| PATCH | `/remediation-batches/{batch_id}` | claimed agent |
| POST | `/remediation-batches/{batch_id}:release` | claimed agent |
| POST | `/remediation-batches/{batch_id}:start-verification` | reviewer/admin |
| POST | `/remediation-batches/{batch_id}:complete` | reviewer/admin |
| POST | `/remediation-batches/{batch_id}:extend-lease` | admin |
| POST | `/remediation-batches/{batch_id}:cancel` | remediator/admin |
| GET | `/remediation-batches/{batch_id}/prompt` | remediator |
| POST | `/imports/sheet-csv` | admin |
| POST | `/imports/{import_id}:apply` | admin |
| POST | `/imports/{import_id}:reverse` | admin |
| GET | `/exports/ticket-reviews.csv` | admin |

Not a browser API, and callable only by the console service account:

| Method | Path |
|---|---|
| POST | `/internal/v1/ticket-evidence:lookup` |

## 20. Appendix C — model inventory frozen by Stage 1

All in `api/ticket_review_models.py`, all `extra="forbid"`.

**Closed enums:** `ReviewStatus`, `ObservationType`, `Severity`,
`RemediationTarget`, `CorrelationStatus`, `CorrelationTrust`, `ImportState`,
`ReviewerRole`, `BatchStatus`, `ImportStatus`, `ResolutionOutcome`,
`TimelineEntryKind`, `TimelineVisibility`, `DevRevActorType`.

**Identity:** `ReviewerIdentity`, `DevRevActor`.

**Live/cache DevRev (may carry a title or body):** `DevRevTicketSummary`,
`DevRevTicketDetail`, `DevRevTimelineEntry`, `DevRevTicketFilters`.

**Evidence and provenance:** `ObservedChunkRef`, `RagProvenance`,
`EvidenceLink`, `VerificationEvidence`.

**Durable review:** `TicketReview` (no title field), `ReviewResolution`,
`ReviewPatch`.

**Audit:** `AuditEvent` (frozen), plus `audit_event_hash_payload` and
`compute_audit_event_hash`.

**Remediation:** `RemediationBatch` (parent: counts, digest, lease, bounded
test evidence), `RemediationBatchItem` (frozen `review_id`/`review_version`),
`BatchLease`, `BatchOutcome`.

**Import:** `TicketImport`, `TicketImportRow`.

**Pagination:** `CursorPage[T]`, `TimelinePage`.

**Closed mutation envelopes** (frozen here because Stage 5's commit allowlist
cannot reach this module, and because the plan's "unknown envelope fields fail
validation" rule needs a strict model to enforce it): `SessionResponse`,
`ErrorBody`, `ErrorResponse`, `CreateReviewRequest`,
`CreateEvidenceLinkRequest`, `DeleteEvidenceLinkRequest`, `ReviewRef`,
`CreateRemediationBatchRequest`, `ClaimBatchResponse`, `HeartbeatBatchRequest`,
`MaterializeBatchRequest`, `MaterializeBatchResponse`, `ReviewOutcome`,
`ReviewDecision`, `PatchBatchRequest`, `ReleaseBatchRequest`,
`ReadyBatchRequest`, `StartVerificationRequest`, `CompleteBatchRequest`,
`ExtendLeaseRequest`, `CancelBatchRequest`, `ImportRowIssue`,
`ImportDryRunResponse`, `ImportApplyOrReverseRequest`, `ImportChunkResponse`.

**Pure functions:** `review_id_for_devrev_work`, `utc_now`,
`allowed_review_transitions`, `assert_review_transition`,
`allowed_batch_transitions`, `assert_batch_transition`,
`allowed_import_transitions`, `assert_import_transition`,
`can_assign_reviewer`, `format_etag`, `parse_if_match`, `ensure_if_match`,
`http_status_for_precondition_error`, `seal_cursor`, `open_cursor`.

**Typed errors:** `TicketReviewContractError` and its subclasses
`InvalidReviewTransition`, `InvalidBatchTransition`, `InvalidImportTransition`,
`PreconditionError` (`MissingPreconditionError`, `MalformedPreconditionError`,
`StalePreconditionError`), `CursorError`.
