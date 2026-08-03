# Stage 11 — End-to-End Verification, Runbook, and Controlled Rollout

> **For Claude Opus 5:** This is the final Opus executor. Evidence precedes
> claims. Repair in-scope defects; do not waive failed gates. Production,
> external n8n, real CSV, DevRev writes, and Pinecone changes require separate
> explicit approvals.

**Goal:** Trace every requirement to evidence, run the full local and pinned
Python 3.12/emulator/container/Terraform gates, validate the synthetic browser
workflow, verify approved staging, and prepare a reversible production decision.

**Architecture:** Verification proceeds from a clean committed candidate
through local tests, fixture browser flows, least-privilege remote verification,
immutable staging artifacts, and explicit production approval. Local feedback
never substitutes for the missing host Docker/Terraform or an unapplied
external gate.

---

## Mandatory preflight

```bash
set -euo pipefail
export PLAN_ROOT="${PLAN_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide/tickets-development-plan}"
export TICKETS_BASE_SHA="${TICKETS_BASE_SHA:-eed9b34967c59b8bfec34026c9a8637581f2036a}"
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"

test -r "$PLAN_ROOT/README.md"
test "$(git -C "$IMPL_ROOT" rev-parse --show-toplevel)" = "$IMPL_ROOT"
test -x "$PYTHON_BIN"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
git -C "$IMPL_ROOT" merge-base --is-ancestor "$TICKETS_BASE_SHA" HEAD
git -C "$IMPL_ROOT" log --oneline --decorate -20
git -C "$IMPL_ROOT" diff --stat "$TICKETS_BASE_SHA"...HEAD
```

Confirm Stages 1–10 each have a scoped commit. If Stage 10 is externally
blocked, retain its exact `REMOTE_REQUIRED`/approval status; do not invent a
pass or use a different service account.

## Files

Create:

- `kb-rag-system/Development Docs/TICKETS_REVIEW_RUNBOOK.md`
- `kb-rag-system/scripts/verify_tickets_staging.py`
- `kb-rag-system/tests/test_verify_tickets_staging.py`
- `kb-rag-system/tests/fixtures/tickets_staging_synthetic.json`
- `docs/verification/tickets/REQUIREMENTS_TRACEABILITY.md`
- `docs/verification/tickets/VERIFICATION_TEMPLATE.md`

Modify only if a failing verification proves it:

- exact source/test/CI/IaC file that owns the defect;
- `kb-rag-system/ci/cloudbuild.tickets-console-verify.yaml`;
- the Stage 10 allowed-path fixture/helper tests.

Do not commit real screenshots, ticket payloads, tokens, signed URLs, build
logs, plan binaries, or participant/user data.

## Step 1 — Requirements traceability

Read every file in `PLAN_ROOT` completely. In
`REQUIREMENTS_TRACEABILITY.md`, create one row for every master Definition of
Done item and every stage Definition of Done:

| Requirement | Code | Automated test | Browser/staging evidence | Rollback | Status |
|---|---|---|---|---|---|

Allowed status:

- `verified_local`;
- `verified_remote`;
- `verified_staging`;
- `blocked_external`;
- `failed`.

“Implemented,” “looks good,” and a file path without a test/runtime result are
not evidence.

## Step 2 — Fresh local gates

From the implementation candidate:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest -q -rs \
  -m "not live_dependencies and not staging_e2e"
"$PYTHON_BIN" -m pytest --collect-only -q >/dev/null
tool_bin="$(dirname "$PYTHON_BIN")"
"$tool_bin/ruff" check .
"$tool_bin/mypy"
"$PYTHON_BIN" -m pip check
"$tool_bin/pip-audit" --strict --require-hashes \
  -r requirements.lock
"$PYTHON_BIN" -m compileall -q api data_pipeline scripts
git -C "$IMPL_ROOT" diff --check
```

If the implementation worktree has no `.venv`, use the explicit
`PYTHON_BIN`-sibling tools only when their shebang/runtime works; otherwise mark
host lint/audit `REMOTE_REQUIRED`. Do not install globally. Record exact test
counts, failures, skips, and host Python version.

Run the focused feature suite explicitly so a marker/config error cannot hide
it:

```bash
"$PYTHON_BIN" -m pytest \
  tests/test_ticket_review_models.py \
  tests/test_devrev_client.py \
  tests/test_ticket_review_repository.py \
  tests/test_ticket_review_service.py \
  tests/test_ticket_review_provenance.py \
  tests/test_ticket_evidence_broker.py \
  tests/test_reviewer_auth.py \
  tests/test_tickets_csrf.py \
  tests/test_ticket_review_routes.py \
  tests/test_ticket_review_batch_routes.py \
  tests/test_ticket_review_import_export_routes.py \
  tests/test_tickets_console_app.py \
  tests/test_tickets_ui_contract.py \
  tests/test_tickets_fixture_app.py \
  tests/test_tickets_fixture_server.py \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_review_cli.py \
  tests/test_ticket_review_remediation_prompt.py \
  tests/test_ticket_review_migration.py \
  tests/test_tickets_console_container_contract.py \
  tests/test_tickets_evidence_broker_container_contract.py \
  tests/test_tickets_console_deployment_contract.py \
  tests/test_tickets_console_monitoring_contract.py \
  tests/test_tickets_console_terraform_contract.py \
  tests/test_verify_tickets_console_plan.py -q
```

For every failure: reproduce narrowly, find root cause, write/adjust a
regression test, implement the smallest general fix, rerun focused and affected
suite. Never weaken assertions or skip a required gate.

## Step 3 — Static security/privacy gates

```bash
cd "$KBRAG_ROOT"
if rg -n \
  'innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|localStorage|sessionStorage|X-API-Key|Bearer ' \
  ui/tickets; then
  echo "STOP: unsafe UI pattern" >&2
  exit 1
fi
if rg -n -i '@forusall\.com|TKT-[89][0-9]{5}' \
  tests/fixtures ui/tickets data_pipeline/agent_prompts; then
  echo "STOP: real-data fingerprint" >&2
  exit 1
fi
if git -C "$IMPL_ROOT" grep -n -I -E \
  'BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|AIza[0-9A-Za-z_-]{20,}|Bearer [A-Za-z0-9._-]{20,}'; then
  echo "STOP: credential fingerprint" >&2
  exit 1
fi
```

Then run the repository's full `detect-secrets` baseline workflow through the
pinned remote verifier. Manually inspect code matches for token/auth/message/
chunk fields; scanners do not prove data-flow safety.

## Step 4 — Synthetic fixture browser verification

Reuse the tested Stage 6 runner exactly; do not start Uvicorn directly or use a
process-name cleanup. Start/status already enforce loopback, invalid
ADC/metadata endpoints, readiness, nonce/PID ownership, bounded lifetime, and
synthetic-only dependencies:

```bash
cd "$KBRAG_ROOT"
fixture_state_dir="$(mktemp -d -t tickets-fixture-stage11.XXXXXX)"
chmod 0700 "$fixture_state_dir"
"$PYTHON_BIN" -m tests.support.tickets_fixture_server start \
  --state-dir "$fixture_state_dir" \
  --host 127.0.0.1 --port 8010 --max-seconds 1800
"$PYTHON_BIN" -m tests.support.tickets_fixture_server status \
  --state-dir "$fixture_state_dir" \
  --expect-url http://127.0.0.1:8010
printf 'FIXTURE_STATE_DIR=%s\n' "$fixture_state_dir"
```

Record that exact printed directory before using browser automation. Confirm
the fixture process makes no external network request.

Use browser automation against `http://127.0.0.1:8010/tickets` at:

- 1440×900;
- 1024×768;
- 768×1024;
- 390×844;
- 360×800.

Execute and record this deterministic matrix:

1. session/role header and skip link;
2. live and review queues, supported filters, disabled second facet;
3. DevRev next/previous and review cursor navigation;
4. exact Ticket ID mode;
5. direct detail, back/forward, aborted stale request;
6. paginated conversation/audit/evidence including empty-page-with-cursor;
7. participant/human/AI/event/visibility filters;
8. legacy Type vs Observation and assignment vs audit actor;
9. rating keyboard, long/multiline comments, canonical counters;
10. first import/save and quoted ETag;
11. 428 client bug, 412 data-preserving stale panel, 409 business conflict;
12. manual evidence link/unlink with reason;
13. linked/manual/candidate/unavailable evidence;
14. viewer/reviewer/remediator/admin/agent controls;
15. remediation batch, copied identifier-only prompt;
16. synthetic CSV dry-run, chunked apply, export, formula safety, reversal;
17. loading/empty/partial/stale/401/403/404/422/429/502/503;
18. zoom 200%, visible focus, reduced motion, 44 px touch targets;
19. no 360 px page overflow, console error, failed CSP request, cross-origin
    request, browser credential, or persistent ticket/comment storage.

Use only synthetic screenshots if needed. Fix every reproducible defect and
repeat the complete matrix after the final fix.

Even if a browser assertion fails, stop with the exact printed directory and
prove that the runner removed only its own state:

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

## Step 5 — Build the staging verifier

`verify_tickets_staging.py` must be safe by default, unit-tested, and
single-role per process. It accepts:

```text
--console-url
--expected-environment=staging
--synthetic-fixture-json
--read-only-devrev-ticket (optional, explicitly approved)
--role-profile=viewer|reviewer|remediator|agent|admin
--phase=read-only|reviewer-write|batch-create|agent-work|human-verify|admin-import|cleanup
--run-id
--handoff-in (required after the first mutating phase)
--handoff-out (0600, required for each non-terminal mutating phase)
--apply-synthetic-writes (required on every mutating phase)
```

Without `--apply-synthetic-writes`, every role/phase is read-only and performs
only:

- `/livez`, `/readyz`, `/session`;
- IAP/app identity and exact requested-role assertions;
- no-secret response checks;
- bounded list/detail/timeline only when an approved synthetic/demo ticket is
  supplied.

One process authenticates as exactly one approved identity and cannot
impersonate or switch roles. Mutating phases are deliberately split:

- `reviewer-write`: a reviewer creates one tagged synthetic review/evidence
  link and proves `428`/`412`/`409` and idempotency behavior;
- `batch-create`: a remediator creates and readies a batch plus a second
  release-path batch;
- `agent-work`: the exact agent claims, heartbeats, materializes, submits the
  first batch only to `changes_proposed`, and safely releases/blocks the
  second;
- `human-verify`: a different human reviewer/admin starts verification,
  records bounded evidence, completes the batch, and only then resolves the
  applicable review;
- `admin-import`: an admin runs only synthetic CSV dry-run/apply/export/reverse;
- `cleanup`: an admin cleans only the exact created product-state IDs while
  preserving required audit/tombstone evidence.

Each successful mutating response returns a short-lived, server-signed staging
verification handoff. It is domain-separated from cursor/CSRF tokens and bound
to environment, run ID, prior digest, next role/phase, exact server-created
resource IDs/versions, and expiry. It is not authorization: the next request
still performs signed-IAP, RBAC, BOLA, ETag, idempotency, and state-machine
checks. Handoff JSON contains no email, ticket title/body, comment,
conversation, CSV row, token, secret, or IAP credential; it is mode `0600`,
never logged, and deleted after exact cleanup. Production rejects the
verification header/flow at startup and request time. Tests cover replay,
tamper, wrong run/environment/role/phase, expiry, reordering, injected IDs,
and attempts to widen authority.

It refuses:

- production environment/URL;
- a ticket not identified as approved synthetic/demo;
- `(default)` console database;
- DevRev write method;
- real CSV path;
- wildcard cleanup;
- missing expected IAP audience/role;
- a role/phase mismatch or multi-role credential;
- unsigned/stale/non-chain-contiguous handoff;
- a handoff path outside the caller-created `0700` directory or with a mode
  other than `0600`.

## Step 6 — Commit Stage 11 source before remote evidence

Run local tests for the new verifier/runbook, then stage exact paths:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest tests/test_verify_tickets_staging.py -q
git -C "$IMPL_ROOT" add \
  "kb-rag-system/Development Docs/TICKETS_REVIEW_RUNBOOK.md" \
  kb-rag-system/scripts/verify_tickets_staging.py \
  kb-rag-system/tests/test_verify_tickets_staging.py \
  kb-rag-system/tests/fixtures/tickets_staging_synthetic.json \
  docs/verification/tickets/REQUIREMENTS_TRACEABILITY.md \
  docs/verification/tickets/VERIFICATION_TEMPLATE.md
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow "kb-rag-system/Development Docs/TICKETS_REVIEW_RUNBOOK.md" \
  --allow kb-rag-system/scripts/verify_tickets_staging.py \
  --allow kb-rag-system/tests/test_verify_tickets_staging.py \
  --allow kb-rag-system/tests/fixtures/tickets_staging_synthetic.json \
  --allow docs/verification/tickets/REQUIREMENTS_TRACEABILITY.md \
  --allow docs/verification/tickets/VERIFICATION_TEMPLATE.md
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "test(tickets): verify and document review console rollout"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
export SOURCE_SHA="$(git -C "$IMPL_ROOT" rev-parse HEAD)"
```

If verification fixes changed other stage-owned files, commit them separately
with exact scope/tests before this documentation commit.

## Candidate and deployment evidence binding

After all Stage 11 source/test/runbook changes are committed, freeze:

```bash
export FINAL_SOURCE_SHA="$(git -C "$IMPL_ROOT" rev-parse HEAD)"
export FINAL_TREE_SHA="$(git -C "$IMPL_ROOT" rev-parse HEAD^{tree})"
test "$FINAL_SOURCE_SHA" = "$SOURCE_SHA"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
```

Create a payload-free deployment-evidence manifest outside Git. It binds the
final source/tree SHA to provider-lock hashes, remote verification build ID,
both immutable image digests/SBOM/scan attestations, foundation and workload
plan/manifest generation URIs and hashes, before/after state serials, exact
secret-version-manifest generation/hash, Cloud Run revision names, staging
verification run ID, and approval receipts. Publish it only through the
approved evidence pipeline and retain its generation-qualified URI/hash.

Evidence from an older source SHA is not silently inherited. If a change after
staging touches runtime, IaC, config, build context, dependencies, tests,
fixtures, verifier, or deployment/runbook commands, rebuild, replan as
required, reapply the exact newly approved plan, regenerate this manifest, and
rerun every affected remote/staging gate. A docs-only exception is allowed
only when an explicit diff allowlist proves every changed path is unbundled
documentation, both image build-context digests and Terraform inputs are
unchanged, and that attestation is included in the manifest. A later repair
invalidates the manifest until this rule is satisfied again.

## Approval Gate A — Fresh pinned remote verification

If the user already approved Stage 10's least-privilege verifier and it exists,
run a fresh build for the Stage 11 candidate:

```bash
gcloud iam service-accounts describe \
  "ticket-controller-verify@$GCP_PROJECT.iam.gserviceaccount.com" \
  --project "$GCP_PROJECT" --format='value(email)'
gcloud builds submit "$IMPL_ROOT" \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --config "$KBRAG_ROOT/ci/cloudbuild.tickets-console-verify.yaml" \
  --substitutions "_CANDIDATE_SHA=$SOURCE_SHA"
```

Required SUCCESS evidence:

- full Python 3.12 tests with exact counts/skips;
- ruff/mypy/pip check/pip-audit/detect-secrets;
- real Firestore emulator parity;
- console and broker builds/smokes;
- Terraform 1.9.8 fmt/init `-lockfile=readonly`/validate/test for both isolated
  roots/module;
- provider schema with direct IAP;
- existing roots/provider locks unchanged.

If approval/verifier is absent, mark each row `blocked_external`; do not run
with another identity and do not call the candidate production-ready.

## Approval Gate B — Approved staging runtime

Request the currently deployed staging source SHA and Stage 10 artifact chain.
Because Stage 11 adds a verifier/test fixture after Stage 10, an older staging
deployment normally cannot satisfy final-SHA evidence. Compare:

```bash
test -n "${STAGING_DEPLOYED_SOURCE_SHA:-}"
git -C "$IMPL_ROOT" merge-base --is-ancestor \
  "$STAGING_DEPLOYED_SOURCE_SHA" "$FINAL_SOURCE_SHA"
git -C "$IMPL_ROOT" diff --name-only \
  "$STAGING_DEPLOYED_SOURCE_SHA" "$FINAL_SOURCE_SHA"
```

If the SHAs differ and the strict docs-only exception above is not proven,
rerun Stage 10's protected immutable image build and staging procedure with
`SOURCE_SHA="$FINAL_SOURCE_SHA"`. If foundation already exists, its fresh
foundation plan must be a verified no-op; never reapply or recreate it merely
for ceremony. Revalidate the same generation-qualified secret manifest, create
and separately approve/apply a new workload plan bound to the final SHA and
current immutable digests, then record the new state serial/revisions. If
foundation does not exist, execute the complete foundation → external secret
owner → workload sequence. Do not verify an obsolete revision.

Only after the exact final-SHA (or attested docs-only-equivalent) staging
revision exists, request:

- `TICKETS_STAGING_URL`;
- expected IAP audience;
- approved identity matrix;
- explicitly designated synthetic/demo DevRev ticket, if any;
- permission for synthetic Firestore writes.

Run read-only first:

```bash
export VERIFY_RUN_ID="$("$PYTHON_BIN" -c \
  'import uuid; print(uuid.uuid4())')"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile viewer \
  --phase read-only \
  --run-id "$VERIFY_RUN_ID" \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json"
```

After separate synthetic-write approval, create a local handoff directory and
run these commands one at a time. Before each command, the designated person
must authenticate as the exact listed principal and `/session` must match the
requested role. Opus must stop for that identity handoff; it may not
impersonate, reuse another role's credential, or run one omnipotent process.

```bash
export VERIFY_HANDOFF_DIR="$(mktemp -d -t tickets-staging-handoff.XXXXXX)"
chmod 0700 "$VERIFY_HANDOFF_DIR"

# Execute as the approved reviewer writer.
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile reviewer --phase reviewer-write \
  --run-id "$VERIFY_RUN_ID" --apply-synthetic-writes \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json" \
  --handoff-out "$VERIFY_HANDOFF_DIR/reviewer.json"

# Execute as the approved remediator.
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile remediator --phase batch-create \
  --run-id "$VERIFY_RUN_ID" --apply-synthetic-writes \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json" \
  --handoff-in "$VERIFY_HANDOFF_DIR/reviewer.json" \
  --handoff-out "$VERIFY_HANDOFF_DIR/remediator.json"

# Execute as the exact remediation-agent service account.
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile agent --phase agent-work \
  --run-id "$VERIFY_RUN_ID" --apply-synthetic-writes \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json" \
  --handoff-in "$VERIFY_HANDOFF_DIR/remediator.json" \
  --handoff-out "$VERIFY_HANDOFF_DIR/agent.json"

# Execute as an independent approved reviewer/admin, never the agent.
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile reviewer --phase human-verify \
  --run-id "$VERIFY_RUN_ID" --apply-synthetic-writes \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json" \
  --handoff-in "$VERIFY_HANDOFF_DIR/agent.json" \
  --handoff-out "$VERIFY_HANDOFF_DIR/verifier.json"

# Execute as the approved admin.
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile admin --phase admin-import \
  --run-id "$VERIFY_RUN_ID" --apply-synthetic-writes \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json" \
  --handoff-in "$VERIFY_HANDOFF_DIR/verifier.json" \
  --handoff-out "$VERIFY_HANDOFF_DIR/admin.json"

# Execute as the same approved admin; preserve ledgers/tombstones.
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_staging.py" \
  --console-url "$TICKETS_STAGING_URL" \
  --expected-environment staging \
  --role-profile admin --phase cleanup \
  --run-id "$VERIFY_RUN_ID" --apply-synthetic-writes \
  --synthetic-fixture-json \
  "$KBRAG_ROOT/tests/fixtures/tickets_staging_synthetic.json" \
  --handoff-in "$VERIFY_HANDOFF_DIR/admin.json"

rm -f -- \
  "$VERIFY_HANDOFF_DIR/reviewer.json" \
  "$VERIFY_HANDOFF_DIR/remediator.json" \
  "$VERIFY_HANDOFF_DIR/agent.json" \
  "$VERIFY_HANDOFF_DIR/verifier.json" \
  "$VERIFY_HANDOFF_DIR/admin.json"
test -z "$(find "$VERIFY_HANDOFF_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
rmdir "$VERIFY_HANDOFF_DIR"
```

Test IAP/RBAC with:

| Identity | IAP | App role | Expected |
|---|---|---|---|
| unapproved | deny | none | no app |
| approved viewer | allow | viewer | read only |
| approved reviewer | allow | reviewer | review/evidence writes |
| approved remediator | allow | remediator | create/read batch |
| remediation SA | allow | agent | claim/heartbeat/update batch only |
| approved admin | allow | admin | import/export/reverse/reopen |

Also prove console SA cannot read `(default)`, broker cannot write it, and the
agent/humans cannot access either database directly. Require zero `failed` and
zero `blocked_external` results for every mandatory remote/staging row before
production planning. The optional real CSV and producer/n8n gates may remain
unapproved, but must be labeled out of production-console scope rather than
counted as a passed console gate.

## External n8n correlation gate

Automatic future `linked` provenance is verified only if the n8n owner supplies:

- approved workflow revision;
- verified service-account audience/principal;
- contract fixture;
- staging HMAC lookup evidence;
- response/timeline association;
- rollback.

If absent, keep automatic correlation disabled and mark the limitation. Do not
advance `TICKET_HANDLER_MODE` or edit n8n as part of `/tickets`.

## Runbook contents

`TICKETS_REVIEW_RUNBOOK.md` must match implemented names/commands and cover:

- architecture/trust boundaries/database isolation/broker limitation;
- local fixture setup without global installs;
- DevRev token/scope/author allowlist rotation and revoke;
- IAP access, role config, agent signJwt, CSRF/idempotency;
- collections/indexes/TTL/730-day retention/2,555-day audit/legal hold;
- health/readiness and low-cardinality alerts;
- DevRev 401/403/429/5xx and partial pagination;
- review/ETag/evidence conflict recovery;
- lease heartbeat/reclaim;
- CSV dry-run/apply/resume/reverse/export;
- audit-chain/log-sink incident response;
- staging/build/plan/apply artifact chain;
- rollback by creating a new protected plan against current state that targets
  the prior immutable image/config/secret-version set;
- disable console/IAP access without changing private RAG;
- historical-provenance and external-n8n limitations.

## Approval Gate C — Two-phase production plan/apply

Do not enter Gate C with a mandatory `failed` or `blocked_external` row.
Present the traceability matrix, final deployment-evidence manifest, exact
local/remote/staging results, immutable image/SBOM/scan attestations, n8n
status, retention decision, known limitations, and rollback target. Production
plan artifacts do not exist yet; first ask only for authority to create the
foundation plan.

### Gate C1 — Production foundation

After explicit foundation-plan approval:

```bash
export SOURCE_SHA="$FINAL_SOURCE_SHA"
gcloud builds triggers run rag-tickets-console-production-plan \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$SOURCE_SHA" \
  --substitutions "_DEPLOYMENT_PHASE=foundation"
```

Capture the generation-qualified binary/JSON/text/manifest URIs, hashes, prior
state serial, provider-lock digest, build identity, source SHA, and semantic
verifier result. Require only the fixed production-foundation create allowlist,
with no delete/replace, Cloud Run/IAP/runtime IAM, secret reference/version,
broker `(default)` grant, or existing RAG/n8n change.

Present those newly created artifacts and ask separately for exact foundation
apply approval. Only then:

```bash
gcloud builds triggers run rag-tickets-console-production-apply \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$SOURCE_SHA" \
  --substitutions \
"_DEPLOYMENT_PHASE=foundation,_PLAN_MANIFEST_URI=$PROD_FOUNDATION_PLAN_MANIFEST_URI,_PLAN_MANIFEST_HASH=$PROD_FOUNDATION_PLAN_MANIFEST_HASH,_PLAN_URI=$PROD_FOUNDATION_PLAN_URI,_PLAN_SHA256=$PROD_FOUNDATION_PLAN_SHA256"
```

Record the apply build ID and after-state serial. Then stop for the designated
production secret owner. The owner creates numeric payload versions through
the approved channel and returns only a generation-qualified payload-free
`PROD_SECRET_VERSION_MANIFEST_URI`, its SHA-256, and approval reference.
Opus/Codex never receives or uploads payloads. Validate metadata without
payload access. The DevRev owner also returns a payload-free attestation that
the production PAT belongs to the dedicated integration user, is unexpired,
limited to approved parts and `ticket:read`, and has no ticket-write
privilege:

```bash
prod_secret_manifest_file="$(mktemp -t tickets-prod-secret.XXXXXX.json)"
cleanup_prod_manifest() {
  rm -f -- "$prod_secret_manifest_file"
}
trap cleanup_prod_manifest EXIT INT TERM
gcloud storage cp \
  "$PROD_SECRET_VERSION_MANIFEST_URI" "$prod_secret_manifest_file"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_secret_manifest.py" \
  --manifest-json "$prod_secret_manifest_file" \
  --environment production \
  --source-uri "$PROD_SECRET_VERSION_MANIFEST_URI" \
  --expected-sha256 "$PROD_SECRET_VERSION_MANIFEST_HASH" \
  --verify-gcp-metadata
cleanup_prod_manifest
trap - EXIT INT TERM
test ! -e "$prod_secret_manifest_file"
```

### Gate C2 — Production workload

Ask separately for workload-plan creation. After approval:

```bash
gcloud builds triggers run rag-tickets-console-production-plan \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$SOURCE_SHA" \
  --substitutions \
"_DEPLOYMENT_PHASE=workload,_CONSOLE_IMAGE_DIGEST=$CONSOLE_IMAGE_DIGEST,_BROKER_IMAGE_DIGEST=$BROKER_IMAGE_DIGEST,_SECRET_VERSION_MANIFEST_URI=$PROD_SECRET_VERSION_MANIFEST_URI,_SECRET_VERSION_MANIFEST_HASH=$PROD_SECRET_VERSION_MANIFEST_HASH"
```

Capture and review the new generation-qualified plan chain. The semantic
verifier must preserve every foundation address, bind the exact final-SHA
digests and numeric secret versions, and show only allowlisted workload
creates. The irreversible log-bucket retention lock is `false` unless
Privacy/Legal separately approves it; changing that flag requires its own new
plan/review/apply and is never implied by console deployment.

Present the workload artifacts and ask separately for exact saved-plan apply.
Immediately before apply, download and validate the same production secret
manifest generation again using the Gate C1 validation block. Only then:

```bash
gcloud builds triggers run rag-tickets-console-production-apply \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$SOURCE_SHA" \
  --substitutions \
"_DEPLOYMENT_PHASE=workload,_PLAN_MANIFEST_URI=$PROD_WORKLOAD_PLAN_MANIFEST_URI,_PLAN_MANIFEST_HASH=$PROD_WORKLOAD_PLAN_MANIFEST_HASH,_PLAN_URI=$PROD_WORKLOAD_PLAN_URI,_PLAN_SHA256=$PROD_WORKLOAD_PLAN_SHA256"
```

The apply pipeline rechecks source/tree SHA, phase, state serial, provider
locks, plan hashes/generations, image attestations, secret-manifest
hash/generation, build identity, and approval, then applies the saved binary
without replanning. Record the production revision/state serial in a newly
generated final deployment-evidence manifest.

After apply, verify deny/allow, `/livez`, `/readyz`, `/tickets`, one explicitly
approved bounded read-only DevRev ticket, named-database isolation,
logs/alerts, and unchanged existing RAG IAM/traffic/revision. Production keeps
synthetic-verification mode disabled. Real CSV dry-run, real CSV apply,
producer instrumentation/n8n rollout, and log-bucket lock each require their
own later approval; none is implied by Gate C1/C2.

On auth, isolation, secret, plan, or data uncertainty, stop traffic/access and
prepare rollback. Never reuse a historical saved plan because its state serial
is stale. Create a new protected workload plan against current state, using
the current trusted source plus the previous deployment manifest's immutable
console/broker digests, numeric secret-version manifest, and config. Verify
the new plan semantically, obtain emergency approval for its exact artifact
hashes/generations, apply that new saved binary without replanning, and rerun
post-deploy isolation/health checks.

```bash
gcloud builds triggers run rag-tickets-console-production-plan \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$SOURCE_SHA" \
  --substitutions \
"_DEPLOYMENT_PHASE=workload,_ROLLBACK_DEPLOYMENT_MANIFEST_URI=$PREVIOUS_DEPLOYMENT_MANIFEST_URI,_ROLLBACK_DEPLOYMENT_MANIFEST_HASH=$PREVIOUS_DEPLOYMENT_MANIFEST_HASH"

# Populate NEW_ROLLBACK_* only from that authenticated plan output, review it,
# and obtain emergency approval before this separate command.
gcloud builds triggers run rag-tickets-console-production-apply \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$SOURCE_SHA" \
  --substitutions \
"_DEPLOYMENT_PHASE=workload,_PLAN_MANIFEST_URI=$NEW_ROLLBACK_PLAN_MANIFEST_URI,_PLAN_MANIFEST_HASH=$NEW_ROLLBACK_PLAN_MANIFEST_HASH,_PLAN_URI=$NEW_ROLLBACK_PLAN_URI,_PLAN_SHA256=$NEW_ROLLBACK_PLAN_SHA256"
```

## Definition of Done

- Traceability maps every requirement to concrete evidence or an exact external
  blocker.
- Local suite and synthetic browser matrix pass after final fixes.
- Pinned Python 3.12/emulator/container/Terraform gates pass when authorized;
  an external blocker is honest during development but prevents production
  Gate C.
- Approved staging proves IAP/RBAC/database isolation/read-only DevRev,
  conflict, remediation, CSV, export, and reversal.
- Runbook matches real artifacts and safe rollback.
- Production remains unchanged unless each explicit Gate C action was approved
  and verified.
- No failed gate is waived.

After Stage 11, execute
`99-codex-final-verification-and-repair.md` from `PLAN_ROOT`.
