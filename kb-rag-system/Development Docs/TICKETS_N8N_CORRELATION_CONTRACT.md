# External Contract: authenticated n8n → RAG ticket correlation

**Status:** Proposed — **GATE NOT SATISFIED**. Automatic
`correlation_status="linked"` remains disabled until the n8n owner implements
this contract and supplies staging evidence (§9).
**Date:** 2026-08-04
**Stage:** 4 (`04-ticket-hydration-and-rag-provenance.md`)
**Applies to:** the active n8n workflow that calls
`POST /api/v1/handle-ticket`, the producer side of `kb-rag-system`, and the
read-only evidence broker.

This document is the *whole* contract. Until every clause is implemented and
proven, the RAG service records `correlation_trust="none"` or
`"candidate"` and the console renders `correlation_status="unavailable"`. That
is the intended default, not a degraded mode: a ticket with no defensible
correlation must look like a gap.

**Nothing in this stage edits or deploys the n8n flow.** No live external
mutation occurred.

---

## 1. Why a signature at all — the existing boundary is not enough

`POST /api/v1/handle-ticket` is already protected by two layers:

* Cloud Run IAM validates `Authorization` (the invoker is an n8n-owned service
  account, not a human);
* the application validates `X-API-Key`.

Both prove *who called*. Neither proves *which ticket this call was about*. A
DevRev DON supplied in a header or a body field is caller-controlled data: any
principal that can reach the endpoint could attach any DON to any request, and
the service would have no way to tell a genuine correlation from an asserted
one. Storing that as `linked` would let one caller graft real RAG evidence onto
an unrelated ticket — which is precisely the fabricated provenance the master
plan forbids.

So the trust levels are:

| What the request carries | `correlation_trust` | `correlation_status` | Stored? |
|---|---|---|---|
| Nothing | `none` | `unavailable` | no reference |
| A raw header/body DON, unsigned | `candidate` | `unavailable` | no reference |
| IAM + `X-API-Key` + valid signed context (§3) | `verified_workload` | `linked` | HMAC reference only |
| A reviewer explicitly linked broker evidence | `manual_reviewer` | `manual` | sanitized reference |

Implemented by `verify_ingress_context()` and `unverified_candidate()` in
`data_pipeline/ticket_review_provenance.py`; pinned by
`tests/test_ticket_review_provenance.py::TestUnverifiedCallersCannotLink`.

---

## 2. The request field carrying the DevRev DON

n8n sends the DON in the JSON body of `POST /api/v1/handle-ticket`:

```text
correlation.devrev_work_id      the DevRev work DON, e.g.
                                "don:core:dvrv-us-1:devo/<org>:ticket/<n>"
```

Rules:

* it is the **DON**, never the display id (`TKT-12345`). A display id is not
  globally unique across DevRev orgs and is not what `works.get` scopes on;
* maximum 256 characters (`MAX_ID_LENGTH`);
* it is **transient**. The service normalizes it (Unicode NFC + whitespace
  trim, **no** case folding — a DON is case-sensitive), derives the lookup
  HMAC, and discards it. It is never written to Firestore, an execution log, an
  error body, or an INFO line.

The console later sends the same raw DON to the evidence broker as a
`SecretStr` over an authenticated service-to-service call, and the broker
likewise hashes it in memory only.

---

## 3. The ingress signature

Three headers accompany the request:

```text
X-Tickets-Correlation-Timestamp   Unix seconds, integer, UTC
X-Tickets-Correlation-Key-Version numeric key version, integer >= 1
X-Tickets-Correlation-Signature   lowercase hex HMAC-SHA256
```

### 3.1 Canonical bytes

Exactly six newline-delimited fields, in this order, UTF-8 encoded:

```text
v1
<unix_seconds>
<normalized_devrev_work_id>
<request_sha256>
<idempotency_key_sha256>
<key_version>
```

* `v1` is the canonicalization version (`CORRELATION_HMAC_VERSION`). A future
  change bumps it so an old signature can never be mistaken for a new one.
* Newline delimiting is deliberate: plain concatenation would let one field's
  bytes be shifted into its neighbour and still produce a matching signature.
* `request_sha256` — SHA-256 over the exact request body bytes n8n sends,
  lowercase hex. Both sides must hash *the same* bytes; n8n must not
  re-serialize after signing.
* `idempotency_key_sha256` — SHA-256 of the `Idempotency-Key` header value that
  the same request carries.
* `key_version` is inside the signed bytes as well as in its header, so the
  version cannot be swapped independently of the signature.

Reference implementation:
`data_pipeline.ticket_review_provenance.canonical_ingress_bytes`.

### 3.2 Signature

```text
X-Tickets-Correlation-Signature = hex( HMAC-SHA256( ingress_key[key_version],
                                                    canonical_bytes ) )
```

### 3.3 Verification, in order, fail-closed

1. the context must be **complete** — a partial context is no verification at
   all, not a weaker one;
2. `key_version` must name a **currently active** ingress key. An unknown or
   retired version fails explicitly; the service never falls back to trying
   every key it holds;
3. the timestamp must be within **±5 minutes** (`INGRESS_SIGNATURE_MAX_SKEW_S`);
4. the signature must match using **`hmac.compare_digest`** (constant time);
5. the existing **durable idempotency receipt** must be present.

Step 5 is the actual replay boundary. A valid signature with a fresh timestamp
is still replayable on its own; the receipt is what makes a second delivery of
the same `(key, payload)` a replay and a same-key/different-payload delivery a
`409`.

Only after all five does the service derive the lookup reference. If the lookup
key is unavailable the outcome stays **unverified** — recording
`verified_workload` with no queryable reference would produce evidence nobody
could ever find.

---

## 4. Two keys, two jobs

| Key | Held by | Purpose | Delivery |
|---|---|---|---|
| ingress key + version | producer **and** n8n | verify the signature (§3) | dedicated secret version |
| current lookup key + version | producer only | derive the stored HMAC (§5) | distinct secret version |
| lookup keyring + allowed versions | broker only | recompute candidate HMACs | numeric keyring secret |

The keys **must differ**. Sharing one would let anybody able to verify a
signature also mint lookup references for arbitrary tickets.
`validate_producer_correlation_settings()` refuses equal keys, and
`api/tickets_console_config.py` refuses each plane's foreign secrets by
environment-variable name (`PRODUCER_FORBIDDEN_ENV_VARS`,
`BROKER_FORBIDDEN_ENV_VARS`, `CONSOLE_FORBIDDEN_ENV_VARS`). The console
revision receives **none** of the three.

---

## 5. Normalization and HMAC version

```text
normalization: Unicode NFC, whitespace runs collapsed to one space, trimmed.
               NO case folding.
hmac:          HMAC-SHA256( lookup_key, normalized_don )  ->  lowercase hex
version:       CORRELATION_HMAC_VERSION = 1
stored as:     provenance.correlation.ticket_lookup_hmac
               provenance.correlation.lookup_key_version
```

Case folding is excluded on purpose: it would merge distinct DONs and distinct
article ids, and a response hash must distinguish `"NO"` from `"no"`.

### 5.1 Rotation, because the raw DON is intentionally absent

There is no plaintext DON to re-hash, so rotation is versioned:

1. **add** the new lookup key to the broker keyring and to
   `TICKETS_CORRELATION_ALLOWED_KEY_VERSIONS`;
2. **switch** the producer to write with the new current version;
3. **observe** — the broker fans out over all active versions, so mixed history
   resolves;
4. **retire** the old version only after the evidence-retention window has
   passed.

Fan-out is bounded (`MAX_BROKER_KEY_VERSIONS = 5`): one indexed query per
active version per allowlisted collection. Retiring a key makes its historical
records explicitly `unavailable`; revocation is **never silent**. An active
version with no configured key is a startup error, not a skipped version —
otherwise retiring a key would make evidence vanish with nobody told.

Pinned by `tests/test_ticket_evidence_broker.py::TestKeyringFanOut`.

---

## 6. No raw identifier logging

The producer, the broker, and the console must never write a raw DON, display
id, or timeline entry id to:

* Firestore (`execution_logs`, `ticket_executions`, `ticket_jobs`, or any
  console collection);
* an execution log document, including its `error` field;
* an INFO/WARN/ERROR log line;
* an HTTP error body or a metric label.

The only permitted derivations are `HMAC-SHA256(lookup_key, DON)` and
`SHA-256` of a normalized value. The pre-existing `request_id_hash`
(SHA-256, first 24 hex chars) behaviour is unchanged.

Pinned by `tests/test_ticket_review_provenance.py::TestNoRawExternalIdentifiers`
and `tests/test_runtime_log_privacy.py`.

---

## 7. Who correlates what

| Responsibility | Owner |
|---|---|
| Send `correlation.devrev_work_id` + the three headers on every ticket call | **n8n** |
| Sign the canonical bytes with the current ingress key | **n8n** |
| Keep `Idempotency-Key` stable per logical delivery | **n8n** |
| Verify signature, window, receipt; derive the lookup HMAC | producer (`kb-rag-system`) |
| Store only the HMAC, key versions, trust, source, workload/principal hashes | producer |
| Recompute candidate HMACs and return a bounded sanitized envelope | evidence broker |
| Decide `linked` / `manual` / `unavailable` for display | console |
| Post the participant reply back onto the DevRev timeline | **n8n** (unchanged) |

The RAG service does **not** write to DevRev. Timeline-entry correlation on the
DevRev side stays n8n's responsibility, exactly as today.

---

## 8. Fixture request/response contract

Request (headers abbreviated; all synthetic):

```http
POST /api/v1/handle-ticket HTTP/1.1
Authorization: Bearer <Cloud Run ID token, n8n service account>
X-API-Key: <existing application key>
Idempotency-Key: synthetic-idempotency-key
X-Tickets-Correlation-Timestamp: 1785585600
X-Tickets-Correlation-Key-Version: 2
X-Tickets-Correlation-Signature: <hex hmac-sha256>
Content-Type: application/json

{
  "correlation": {
    "devrev_work_id": "don:core:dvrv-us-1:devo/synthetic:ticket/424242"
  },
  "...": "the existing handle-ticket body, unchanged"
}
```

The public response is **unchanged** — `200` inline or `202` plus
`GET /api/v1/tickets/{ticket_job_id}` polling, with the same schemas. Adding a
correlation context changes only what is stored internally. `used_chunks`
remains excluded from poll responses.

Stored fragment (additive; a legacy document simply has none):

```json
{
  "schema_version": 1,
  "provenance": {
    "provenance_schema_version": 1,
    "correlation": {
      "ticket_lookup_hmac": "<64 hex>",
      "lookup_key_version": 7,
      "ingress_key_version": 2,
      "correlation_source": "n8n_signed_ingress",
      "correlation_trust": "verified_workload",
      "correlation_status": "linked",
      "workload_binding_sha256": "<64 hex>",
      "principal_sha256": "<64 hex>",
      "hmac_version": 1
    },
    "job": { "internal_job_id": "<32 lowercase hex, server-minted>" },
    "pipeline": {
      "route": "knowledge_question",
      "model": "<model id>",
      "provider": "<provider>",
      "config_version": "<config version>",
      "prompt_template_id": "kb_rag_ticket_response_v1",
      "prompt_template_sha256": "<64 hex>",
      "rendered_prompt_trace_sha256": "<64 hex or null>",
      "rendered_prompt_trace_is_not_a_version": true,
      "deployed_revision": "<K_REVISION or null>",
      "index_name": "<pinecone index>",
      "index_version": null,
      "namespace": "<pinecone namespace>"
    },
    "retrieval": {
      "source_article_ids": ["<article id>"],
      "observed_chunks": [
        {
          "observed_vector_id": "<existing id, copied verbatim>",
          "article_id": "<article id>",
          "content_sha256": "<64 hex, computed at query time>",
          "chunk_ordinal": 0,
          "namespace": "<namespace>",
          "score": 0.77
        }
      ],
      "truncated": false
    },
    "response_sha256": "<64 hex>",
    "missing_provenance": ["index_version"]
  }
}
```

Notes a reviewer will want:

* `index_version` is **null** with an explicit `missing_provenance` flag. There
  is no index/content version in the system, so claiming the Pinecone contents
  were at a particular commit would be fabricated.
* `rendered_prompt_trace_sha256` is a correlation aid, never a semantic
  version — two functionally identical requests differ because participant text
  differs. The template version is
  `prompt_template_id` + `prompt_template_sha256`.
* `observed_vector_id` is copied verbatim. Stage 4 does not change
  `chunking.py` or the vector-ID scheme; an ID v2/reindex is a separate,
  approved project. `chunk_ordinal` is the **observed retrieval rank**, not the
  KB's `chunk_index` (which is 1-based and per chunker-run, hence not a stable
  per-article position).
* `content_sha256` is computed over the content read back at query time,
  because the stored `content_hash` is a truncated MD5 and cannot satisfy a
  SHA-256 contract.
* `internal_job_id` appears only when it matches the server-minted
  `^[0-9a-f]{32}$` shape. Caller-supplied text that merely looks like an id is
  refused, which is what keeps an attacker-chosen string out of the record.

---

## 9. Owner, rollout, rollback, and the proof required

**Owner:** the n8n workflow owner (external to this repository). The producer
side is owned by the `kb-rag-system` maintainers.

**Rollout**

1. Privacy/Security review of this document.
2. Create the ingress-key secret version; grant read to the producer revision
   and to n8n only.
3. Create the lookup-key secret version (producer) and the keyring secret
   version (broker). Confirm the console revision has **none** of the three.
4. Deploy the producer with correlation settings configured. Behaviour is
   unchanged for requests without a context, so this is safe to ship first.
5. n8n implements §2–§3 in **staging** and runs the proof below.
6. Enable automatic `linked` only after the proof is recorded and reviewed.

**Rollback**

Remove the ingress-key configuration from the producer revision, or have n8n
stop sending the headers. Both degrade to `correlation_trust="none"` and
`correlation_status="unavailable"`. There is no data migration: the stored
fragment is additive and already-written references stay valid and queryable
while their key version remains active.

**Proof required before the gate is satisfied**

| # | Evidence | Expected |
|---|---|---|
| 1 | Staging request with a valid signed context | stored `correlation_trust="verified_workload"`, `lookup_key_version` present |
| 2 | Broker lookup for the same DON | `correlation_status="linked"`, ≥1 record, bounded |
| 3 | Request with the headers omitted | unchanged public response; `unavailable` |
| 4 | Request with a tampered signature | `unavailable`, reason `ingress_signature_mismatch` |
| 5 | Request with a >5 min old timestamp | `unavailable`, reason `ingress_timestamp_outside_window` |
| 6 | Replay of the same `(Idempotency-Key, payload)` | existing replay semantics; no duplicate evidence |
| 7 | Same key, different payload | `409`, no correlation stored |
| 8 | Log/Firestore scan across the whole exercise | zero occurrences of the raw DON or display id |
| 9 | Rotation drill: add v(n+1), switch producer, look up | both versions resolve; retiring v(n) makes its records explicitly `unavailable` |
| 10 | Recorded n8n workflow revision id + reviewer | attached to this document |

**An optional or unverified header never satisfies this gate.** Until items
1–10 are recorded here, the release explicitly remains
`correlation_status="unavailable"`, and
`tests/test_ticket_review_provenance.py` is the enforcement that an unsigned
caller can produce at most a `candidate`.

---

## 10. Deferred to Stage 10 (infrastructure)

The broker's composite indexes are declared in code by
`data_pipeline.ticket_evidence_broker.broker_index_declarations()` and asserted
to cover every query the module can issue. They are **not** yet mirrored into
`kb-rag-system/firestore.indexes.json`, because
`tests/test_terraform_runtime_contract.py::test_firestore_json_mirror_matches_all_terraform_indexes_and_ttls`
compares that file's `(default)`-database indexes field-for-field against
`infra/terraform/modules/ticket_environment/firestore.tf`, and Terraform is
Stage 10's deliverable. Stage 10 must add all three indexes to both files and
extend that test's `expected_indexes`. Until then the code declaration is the
reviewable source of truth, and a production broker deployment is blocked on it.
