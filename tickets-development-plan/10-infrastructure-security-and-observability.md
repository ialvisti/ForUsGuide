# Stage 10 — Isolated Infrastructure, IAP, Security, and Observability

> **For Claude Opus 5:** This is an executable infrastructure prompt.
> Commit and review source changes before any remote build/plan. The current
> host has no Docker or Terraform. Use only the repository's pinned,
> least-privilege Cloud Build workflows after explicit approval; never install
> global tooling or fall back to a default/legacy service account.

**Goal:** Package the console and evidence broker, declare isolated named
Firestore databases and direct IAP, prove least privilege and retention, and
produce reviewed immutable staging artifacts without modifying the existing
private RAG service or its Terraform provider/state.

**Architecture:** Two new Cloud Run services are built from separate minimal
images: `rag-tickets-console` and `tickets-evidence-broker`. Two new Terraform
roots—one per environment—use their own state prefixes and Google provider
`7.41.0`; existing `platform`, `staging`, and `production` roots remain pinned
to their reviewed provider. The console owns only its named database. The
broker has read-only `(default)` access and only the console service account
may invoke it.

**Tech Stack:** Cloud Build, Python 3.12, Terraform 1.9.8, Google provider
7.41.0, Cloud Run v2 direct IAP, Firestore Native, Secret Manager, Cloud
Logging/Monitoring, pytest contract tests.

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
command -v gcloud >/dev/null
gcloud version
"$PYTHON_BIN" --version

if command -v docker >/dev/null; then
  docker --version
else
  echo "REMOTE_REQUIRED: Docker gates use pinned Cloud Build" >&2
fi
if command -v terraform >/dev/null; then
  terraform version
else
  echo "REMOTE_REQUIRED: Terraform gates use pinned Cloud Build" >&2
fi
```

Read completely:

- `infra/terraform/README.md`;
- all `versions.tf`, `backend.tf`, and lock files under
  `infra/terraform/live/{platform,staging,production}`;
- `kb-rag-system/ci/cloudbuild.verify-local.yaml`;
- `kb-rag-system/ci/cloudbuild.generate-terraform-locks.yaml`;
- `kb-rag-system/cloudbuild.terraform-plan.yaml`;
- `kb-rag-system/cloudbuild.terraform-apply.yaml`;
- release-controller and Terraform contract tests;
- official Cloud Run direct-IAP, IAP signed-header, IAP programmatic-auth, and
  provider 7.41.0 docs linked from the master plan.

Do not run `terraform init`, `terraform providers schema`, `docker build`, or
an unreviewed `gcloud builds submit` on this host.

## Locked ownership and security decisions

1. Existing roots/state/provider locks are not upgraded or repurposed.
2. Create:
   - `infra/terraform/live/tickets-console-staging`, state prefix
     `tickets-console/staging`;
   - `infra/terraform/live/tickets-console-production`, state prefix
     `tickets-console/production`;
   - shared module `infra/terraform/modules/tickets_console`.
3. New roots pin Terraform `1.9.8` and
   `hashicorp/google = 7.41.0` exactly. They use generated Linux checksums and
   `-lockfile=readonly` after the lock-generation gate.
4. Environment roots uniquely own:
   - named Firestore database;
   - console, broker, and remediation-agent service accounts;
   - console/broker Cloud Run services and service IAM;
   - environment Secret Manager containers/accessors;
   - direct-IAP accessors;
   - indexes/TTL/retention job;
   - dashboards, alerts, and audit-log bucket/sink.
5. The existing platform root owns only shared API activation and exact
   Cloud-Build/state-prefix bootstrap IAM for these roots. It must not declare
   an environment resource also owned by a new root.
6. Databases:
   - staging: `tickets-console-staging`;
   - production: `tickets-console-prod`;
   - deletion protection enabled;
   - console SA gets database-scoped `roles/datastore.user` only there;
   - console and agent SAs get no role on `(default)`.
7. Broker SA gets database-scoped `roles/datastore.viewer` on `(default)` and
   no write role. This is broader than collection-level because Firestore IAM
   cannot narrow by collection; broker code/output allowlists and audit logs
   are compensating controls.
8. Only console SA receives `roles/run.invoker` on the broker service. No human,
   IAP group, remediation agent, n8n identity, or `allUsers` may invoke it.
9. Console uses `iap_enabled = true`, never a public invoker. Enable
   `iap.googleapis.com`; grant the IAP service agent only the documented
   invoker role. Human access uses exact approved identities/groups.
10. The remediation-agent SA gets IAP access only. Approved remediators receive
    `roles/iam.serviceAccountTokenCreator` on that exact SA, never project-wide.
    The agent gets no Firestore, Secret Manager, deploy, Git, Pinecone, or
    DevRev role.
    The Terraform plan SA gets metadata-only access on the exact new secret
    containers to validate versions, never payload-access permission.
11. All secret references use numeric versions. No Terraform variable, state,
    plan, output, YAML substitution, or commit contains a secret payload.
12. No `local-exec`, `remote-exec`, raw `gcloud run deploy/update`, mutable
    image tag, `allUsers`, `latest` secret version, or Terraform secret-version
    payload resource is allowed.
13. Each environment root has an exact
    `deployment_phase = "foundation" | "workload"`:
    - `foundation` creates only the named database, service accounts, secret
      containers, state/artifact prerequisites, and non-workload controls. It
      creates no secret version, Cloud Run service, IAP accessor, broker
      `(default)` grant, or human/agent runtime access.
    - after that exact saved foundation plan is approved and applied, a
      designated secret owner—not Opus/Codex/Terraform—creates the payload
      versions through the approved secret-handling process and publishes a
      generation-qualified, payload-free manifest of exact secret resource
      names and numeric versions;
    - `workload` verifies that manifest and only then adds the services,
      secret references, runtime IAM, IAP, monitoring, and retention job.
    This two-phase state transition is mandatory; it prevents the initial
    Secret Manager container/version dependency cycle.

## Files

Create:

- `kb-rag-system/api/tickets_console_entrypoint.py`
- `kb-rag-system/api/tickets_evidence_broker_entrypoint.py`
- `kb-rag-system/Dockerfile.tickets-console`
- `kb-rag-system/Dockerfile.tickets-console.dockerignore`
- `kb-rag-system/Dockerfile.tickets-evidence-broker`
- `kb-rag-system/Dockerfile.tickets-evidence-broker.dockerignore`
- `kb-rag-system/cloudbuild.tickets-console.yaml`
- `kb-rag-system/ci/cloudbuild.tickets-console-verify.yaml`
- `kb-rag-system/ci/cloudbuild.tickets-console-generate-locks.yaml`
- `kb-rag-system/ci/cloudbuild.tickets-console-terraform-plan.yaml`
- `kb-rag-system/ci/cloudbuild.tickets-console-terraform-apply.yaml`
- `kb-rag-system/scripts/verify_tickets_console_plan.py`
- `kb-rag-system/scripts/verify_tickets_secret_manifest.py`
- `kb-rag-system/scripts/tickets_release_state.py`
- `kb-rag-system/scripts/tickets_retention.py`
- `kb-rag-system/tests/test_tickets_console_container_contract.py`
- `kb-rag-system/tests/test_tickets_evidence_broker_container_contract.py`
- `kb-rag-system/tests/test_tickets_console_deployment_contract.py`
- `kb-rag-system/tests/test_tickets_console_monitoring_contract.py`
- `kb-rag-system/tests/test_tickets_console_terraform_contract.py`
- `kb-rag-system/tests/test_verify_tickets_console_plan.py`
- `kb-rag-system/tests/test_verify_tickets_secret_manifest.py`
- `kb-rag-system/tests/test_tickets_release_state.py`
- `kb-rag-system/tests/test_tickets_retention.py`
- `kb-rag-system/tests/fixtures/tickets_stage10_allowed_paths.txt`
- `infra/terraform/modules/tickets_console/{main,variables,outputs,cloud_run,firestore,iam,monitoring}.tf`
- `infra/terraform/live/tickets-console-staging/{backend,main,providers,variables,outputs,versions}.tf`
- `infra/terraform/live/tickets-console-production/{backend,main,providers,variables,outputs,versions}.tf`
- `infra/terraform/live/platform/tickets_console_bootstrap.tf`
- `infra/terraform/live/platform/tickets_console_variables.tf`
- `infra/terraform/live/platform/tickets_console_outputs.tf`

Modify:

- `kb-rag-system/ci/cloudbuild.verify-local.yaml` to run the new unit,
  emulator, image, and Terraform contracts;
- `kb-rag-system/scripts/verify_staged_scope.py` and
  `kb-rag-system/tests/test_verify_staged_scope.py` to add exact
  newline-delimited `--allow-file` support.

Do not edit existing environment `versions.tf`, backends, provider constraints,
or locks as part of the new provider. The finalization base already pins
FastAPI, Uvicorn, Pydantic v2, `httpx`, `google-auth`, and Firestore; do not
change Python dependency inputs or locks in this stage. Firestore application
indexes were already owned and committed by Stages 3–4; read and validate that
file here, but do not restage or modify it.

## Step 1 — Write and run RED deployment contracts

Before creating infrastructure/runtime files, write tests that require:

- both entrypoints are shell-free and import only their intended app;
- console image contains UI and remediation prompt but no tests, `.env`, Git,
  real CSV, keys, or general RAG secrets;
- broker image excludes UI, prompt, DevRev client/token, Pinecone/OpenAI, and
  write-capable credentials;
- both run non-root on read-only filesystems and expose `/livez`/`readyz`;
- all master configuration-matrix settings are wired with correct
  local/staging/production defaults and startup failures;
- console, broker, producer, and n8n receive only their distinct correlation
  settings/secrets; lookup keyring rotation preserves numeric key versions;
- exact new root/module/state names and provider `7.41.0`;
- exact phase behavior: foundation contains no workload/secret-reference IAM,
  and workload requires a validated payload-free numeric-version manifest;
- existing root provider constraints/locks remain byte-identical to the Stage
  9 parent;
- `iap_enabled = true`, deletion protection, no public invoker;
- named database and database-scoped grants;
- no console/agent grant on `(default)`;
- broker viewer-only `(default)` grant and console-only invoker;
- agent Token Creator/IAP grants and no other roles;
- numeric secret versions and immutable image digests;
- API activation includes `iap.googleapis.com`;
- alert labels contain no ticket/review/user/cursor IDs;
- remote verify config adds
  `tests/integration/test_firestore_ticket_review_repository.py`.

Run and observe the intended failures:

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest \
  tests/test_tickets_console_container_contract.py \
  tests/test_tickets_evidence_broker_container_contract.py \
  tests/test_tickets_console_deployment_contract.py \
  tests/test_tickets_console_monitoring_contract.py \
  tests/test_tickets_console_terraform_contract.py \
  tests/test_verify_tickets_console_plan.py \
  tests/test_verify_tickets_secret_manifest.py \
  tests/test_tickets_release_state.py \
  tests/test_tickets_retention.py -q
```

## Step 2 — Build the minimal runtimes

Both entrypoints must honor `PORT`, bind `0.0.0.0`, forward signals, and use
the existing shell-free Python process pattern.

Console image:

- installs only the reviewed runtime lock;
- copies console API/service/repository, UI, prompt template, and only the
  exact `scripts/tickets_retention.py` maintenance command (no other scripts);
- excludes tests/fixtures/PA/infra/docs/git/env/credentials;
- initializes no Pinecone/OpenAI/ForusBots.

Broker image:

- copies only broker models/app/read-only Firestore dependencies;
- initializes no console DB, DevRev, Pinecone/OpenAI/ForusBots;
- has no secret except the correlation HMAC numeric version;
- response and request limits equal the master table.

Do not attempt a local build when Docker is absent. Contract tests plus the
approved remote build are both required.

## Step 3 — Wire the configuration matrix

Implement every row in the master configuration matrix as a Terraform
variable/env/secret reference and a startup/deployment contract test.

Additional fail-closed rules:

- `deployment_phase` accepts only `foundation` or `workload`;
- foundation has no Cloud Run/IAP/runtime grants/secret references, and
  workload cannot plan without the exact validated manifest;
- production database cannot equal `(default)` or staging ID;
- broker database must equal `(default)` and its Firestore client is
  read-only by construction;
- role bindings/default role/unbound-viewer policy match the master;
- DevRev part/visibility allowlists are non-empty;
- console/broker audiences are exact Terraform outputs;
- local/fixture flags are absent from deployed revisions;
- secret env values use numeric versions;
- author/scope/role config is a numeric config-secret version;
- retention values equal 24 h, 7 d, 730 d, and 2,555 d as applicable.

## Step 4 — Implement isolated Terraform roots

Use backend prefixes exactly:

```hcl
prefix = "tickets-console/staging"
prefix = "tickets-console/production"
```

New `versions.tf` files pin:

```hcl
terraform {
  required_version = "= 1.9.8"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 7.41.0"
    }
  }
}
```

Direct IAP must use the provider's GA `iap_enabled = true`. Do not use a v1
annotation or `gcloud` fallback. New module resources use lifecycle
preconditions/deletion protection to reject:

- `(default)` as console database;
- image without `@sha256:`;
- secret version `latest`/non-numeric;
- wildcard/public identity;
- console/agent database access outside the named database;
- broker write role;
- missing IAP or service-agent invoker.

Use stable resource addresses across phases. Moving from `foundation` to
`workload` may only add workload resources; it must not replace, delete,
rename, or re-import a foundation resource. A later workload plan must be
idempotent when rerun with the same manifest and immutable image digests.

Platform bootstrap declares only:

- `iap.googleapis.com` plus any not-already-owned required API;
- least-privilege identities named exactly
  `ticket-controller-verify`, `ticket-controller-build`,
  `ticket-controller-plan`, and `ticket-controller-apply`, or an existing
  identity proven byte-for-byte equivalent by the platform owner;
- state-prefix/artifact permissions;
- protected triggers/config bindings named exactly
  `rag-tickets-console-build`, `rag-tickets-console-staging-plan`,
  `rag-tickets-console-staging-apply`,
  `rag-tickets-console-production-plan`, and
  `rag-tickets-console-production-apply`.

Compare resource addresses across all roots and fail if one address/member is
owned twice.

## Step 5 — Retention, audit, and privacy controls

Declare:

- TTL: message/cache 24 h, import staging/idempotency 7 d;
- review/evidence/batch-item/import/export product state:
  `retention_expires_at` 730 d;
- per-review/per-batch/global hash-chained audit/event ledgers:
  `retention_expires_at` 2,555 d;
- `legal_hold` independently suppresses both policies, and a parent purge
  transactionally strips product fields into a content-free tombstone while a
  younger ledger exists; at 2,555 days exact ledger IDs are deleted before the
  exact tombstone;
- `scripts/tickets_retention.py` as a Cloud Run Job command that calls only the
  Stage 3 repository facade. It defaults to preview; apply requires exact
  environment/database, `--apply`, bounded `--max-documents`, a server-issued
  run/idempotency ID, and the approved runtime flag. It deletes only explicit
  repository-returned IDs in capped batches—never a recursive database/root/
  collection operation;
- the job reuses the immutable console image but overrides its command with
  the absolute shell-free Python argv for the one packaged retention script;
  container tests prove no other maintenance script is present;
- a dedicated schedule/job execution identity for that command, with only the
  named-database role and job-invocation permission;
- Firestore Data Access logs;
- dedicated console mutation log bucket/sink, 2,555-day retention;
- log-bucket lock only after explicit Privacy/Legal approval because it is
  irreversible;
- field-level structured logging allowlist with hashes/counts only.

Retention job service account receives only the named database role and no
broker/default access. Add preview/apply/idempotency/legal-hold,
730-vs-2,555-day, non-cascade, dry-run-default, exact-ID deletion, and
wrong-database negative tests. The job image/command must be covered by the
container and Terraform contracts.

## Step 6 — Observability

Use low-cardinality metrics:

- DevRev request count/latency/status class/429;
- cache hit/miss/degraded;
- review update/precondition/business conflict;
- CSRF/origin/auth/RBAC denial;
- import/export/reversal row counts;
- remediation claim/heartbeat/lease loss;
- broker lookup found/unavailable/error;
- audit-chain validation failure;
- retention preview/purge failures.

Alerts:

- console/broker 5xx and readiness;
- DevRev 401/403 and sustained 429;
- Firestore permission/index/transaction failures;
- IAP/auth anomaly;
- broker unauthorized caller;
- audit sink/chain/retention failure.

No metric label or log field contains ticket/review/batch/user/cursor/DON,
comment, message, token, prompt, response, or chunk content.

## Step 7 — Implement semantic plan verification

`scripts/verify_tickets_console_plan.py` accepts:

```text
--plan-json
--environment=staging|production
--phase=foundation|workload
--expected-source-sha
--expected-console-digest
--expected-broker-digest
--secret-version-manifest-json
--rollback-deployment-manifest-json (optional; workload only)
```

Digest and secret-manifest arguments are required for `workload`; foundation
rejects a secret manifest and ignores no supplied argument silently.
Rollback mode still creates a new plan against the current state serial. Its
generation-qualified prior deployment manifest supplies only previously
attested immutable image/config/numeric-secret targets; it never supplies or
reuses a historical plan binary. It may update only current tickets workload
resources and cannot delete/replace/foundation-change.
`scripts/verify_tickets_secret_manifest.py` validates the downloaded manifest
before planning: exact environment and required secret set, full resource
names, decimal numeric versions, immutable object generation and published
SHA-256, no unknown keys, and no `value`, payload, token, or credential field.
Through the plan service account's exact metadata-only custom role
(`secretmanager.secrets.get`/`secretmanager.versions.get`, never
`secretmanager.versions.access`), it also proves every named container/version
exists, is `ENABLED`, and has the expected replication/location policy without
reading payloads. The workload-plan verifier proves each runtime service
account will receive accessor on only its own listed secrets and that no
console/broker/producer boundary is crossed.

It exits non-zero for:

- a foundation plan containing Cloud Run, IAP/runtime IAM, `(default)` broker
  access, a secret version/reference, or an action outside the fixed
  foundation address allowlist;
- a workload plan that mutates a foundation resource, lacks exact manifest
  bindings, or uses a secret/version absent from the manifest;
- delete/replace of any resource;
- any change to existing RAG/worker/n8n service, traffic, SA, secret, database,
  bucket, queue, or state;
- `allUsers`/`allAuthenticatedUsers`;
- secret payload or non-numeric version;
- mutable image;
- missing direct IAP/deletion protection;
- wrong database ID;
- console/agent access to `(default)`;
- broker write role or non-console invoker;
- project-wide Token Creator;
- provider/root/state mismatch;
- unexpected resource address/action.

Tests prove a historical saved plan/current-state-serial mismatch is rejected,
whereas a newly generated rollback plan bound to the prior immutable
deployment manifest and current state passes only the narrow workload-update
allowlist.

Tests use sanitized positive and adversarial Terraform JSON fixtures generated
by hand from public schema—never a real plan containing live IDs/secrets.

### Release-state and authenticated-output protocol

Implement `scripts/tickets_release_state.py` before any remote command. It is
the only mechanism allowed to carry non-secret release values across Bash
fences and agent turns. The helper:

- derives its path itself with `git rev-parse --path-format=absolute
  --git-path codex-ticket-release/state.json`; callers cannot choose a path;
- creates the parent as `0700` and JSON state as `0600`, rejects symlinks,
  wrong owner/mode, unknown fields, non-absolute repository roots, and
  conflicting rewrites, and uses atomic fsync/replace;
- stores only project/region, source/tree SHA, exact build/trigger/config/
  service-account IDs, image digests, provider-lock hashes, state serials,
  revision names, approval references, and generation-qualified artifact
  URIs/hashes—never tokens, payloads, raw plan JSON, ticket/user data, or
  secret values;
- exposes `init`, `advance-source`, `ingest-build`, `record-external`,
  `materialize-locks`, `get`, and `require`; `get` prints one schema-validated
  scalar and never shell syntax;
- verifies every ingested synchronous `gcloud --format=json` Build result by a
  second `gcloud builds describe`, exact SUCCESS status, expected source SHA,
  trigger/config, substitutions, and least-privilege service account;
- derives the build-output object name from approved bucket + environment +
  source SHA + build ID + kind, obtains its exact generation from object
  metadata, downloads it to a private temporary file, verifies SHA-256/schema,
  and only then atomically advances state;
- refuses an artifact that names a different source/tree SHA, mutable image,
  missing object generation, stale state serial, unexpected identity/action,
  or later build than an already frozen deployment without an explicit
  `advance-source`.

Every tickets Cloud Build config publishes one payload-free `outputs.json`
with `x-goog-if-generation-match:0` at that deterministic path. Its closed
schema contains only safe fields for its kind (`verify`, `locks`, `images`,
`foundation-plan`, `foundation-apply`, `workload-plan`, `workload-apply`,
`deployment-evidence`). Contract tests prove unknown/missing fields, log
scraping, “latest” lookup, ambient-build identity, and secret-looking values
fail.

In every later Bash fence, enable strict mode, reconstruct constant roots,
derive `release_state_file` with `path`, call `require`, and retrieve each
dynamic value with a separate `get`. Never assume an exported variable from a
prior fence still exists and never source/eval the state file.

## Step 8 — Local verification

```bash
cd "$KBRAG_ROOT"
"$PYTHON_BIN" -m pytest \
  tests/test_tickets_console_container_contract.py \
  tests/test_tickets_evidence_broker_container_contract.py \
  tests/test_tickets_console_deployment_contract.py \
  tests/test_tickets_console_monitoring_contract.py \
  tests/test_tickets_console_terraform_contract.py \
  tests/test_verify_tickets_console_plan.py \
  tests/test_verify_tickets_secret_manifest.py \
  tests/test_tickets_release_state.py \
  tests/test_container_contract.py \
  tests/test_deployment_contract.py \
  tests/test_terraform_runtime_contract.py -q
"$PYTHON_BIN" -m compileall -q api data_pipeline scripts
"$PYTHON_BIN" -m json.tool firestore.indexes.json >/dev/null
git -C "$IMPL_ROOT" diff --check

if rg -n \
  'allUsers|allAuthenticatedUsers|secret_data|secretData|version *= *"latest"|local-exec|remote-exec|gcloud run (deploy|update)' \
  "$IMPL_ROOT/infra/terraform/modules/tickets_console" \
  "$IMPL_ROOT/infra/terraform/live/tickets-console-staging" \
  "$IMPL_ROOT/infra/terraform/live/tickets-console-production"; then
  echo "STOP: prohibited infrastructure pattern" >&2
  exit 1
fi
```

The final `rg` must produce no matches. Do not claim Docker, provider-schema,
emulator, Terraform validation, or Python 3.12 passed locally.

## Step 9 — Commit source before any remote gate

Stage only the explicit files in the Files section. Because the Terraform file
list is fixed, expand every brace to an exact path when invoking Git and the
scope helper; do not `git add infra/terraform` or another directory.

Stage this complete literal allowlist:

```bash
git -C "$IMPL_ROOT" add \
  kb-rag-system/api/tickets_console_entrypoint.py \
  kb-rag-system/api/tickets_evidence_broker_entrypoint.py \
  kb-rag-system/Dockerfile.tickets-console \
  kb-rag-system/Dockerfile.tickets-console.dockerignore \
  kb-rag-system/Dockerfile.tickets-evidence-broker \
  kb-rag-system/Dockerfile.tickets-evidence-broker.dockerignore \
  kb-rag-system/cloudbuild.tickets-console.yaml \
  kb-rag-system/ci/cloudbuild.tickets-console-verify.yaml \
  kb-rag-system/ci/cloudbuild.tickets-console-generate-locks.yaml \
  kb-rag-system/ci/cloudbuild.tickets-console-terraform-plan.yaml \
  kb-rag-system/ci/cloudbuild.tickets-console-terraform-apply.yaml \
  kb-rag-system/ci/cloudbuild.verify-local.yaml \
  kb-rag-system/scripts/verify_tickets_console_plan.py \
  kb-rag-system/scripts/verify_tickets_secret_manifest.py \
  kb-rag-system/scripts/tickets_release_state.py \
  kb-rag-system/scripts/tickets_retention.py \
  kb-rag-system/tests/test_tickets_console_container_contract.py \
  kb-rag-system/tests/test_tickets_evidence_broker_container_contract.py \
  kb-rag-system/tests/test_tickets_console_deployment_contract.py \
  kb-rag-system/tests/test_tickets_console_monitoring_contract.py \
  kb-rag-system/tests/test_tickets_console_terraform_contract.py \
  kb-rag-system/tests/test_verify_tickets_console_plan.py \
  kb-rag-system/tests/test_verify_tickets_secret_manifest.py \
  kb-rag-system/tests/test_tickets_release_state.py \
  kb-rag-system/tests/test_tickets_retention.py \
  kb-rag-system/tests/fixtures/tickets_stage10_allowed_paths.txt \
  kb-rag-system/scripts/verify_staged_scope.py \
  kb-rag-system/tests/test_verify_staged_scope.py \
  infra/terraform/modules/tickets_console/main.tf \
  infra/terraform/modules/tickets_console/variables.tf \
  infra/terraform/modules/tickets_console/outputs.tf \
  infra/terraform/modules/tickets_console/cloud_run.tf \
  infra/terraform/modules/tickets_console/firestore.tf \
  infra/terraform/modules/tickets_console/iam.tf \
  infra/terraform/modules/tickets_console/monitoring.tf \
  infra/terraform/live/tickets-console-staging/backend.tf \
  infra/terraform/live/tickets-console-staging/main.tf \
  infra/terraform/live/tickets-console-staging/providers.tf \
  infra/terraform/live/tickets-console-staging/variables.tf \
  infra/terraform/live/tickets-console-staging/outputs.tf \
  infra/terraform/live/tickets-console-staging/versions.tf \
  infra/terraform/live/tickets-console-production/backend.tf \
  infra/terraform/live/tickets-console-production/main.tf \
  infra/terraform/live/tickets-console-production/providers.tf \
  infra/terraform/live/tickets-console-production/variables.tf \
  infra/terraform/live/tickets-console-production/outputs.tf \
  infra/terraform/live/tickets-console-production/versions.tf \
  infra/terraform/live/platform/tickets_console_bootstrap.tf \
  infra/terraform/live/platform/tickets_console_variables.tf \
  infra/terraform/live/platform/tickets_console_outputs.tf
```

Then run:

```bash
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_staged_scope.py" \
  --allow-file "$KBRAG_ROOT/tests/fixtures/tickets_stage10_allowed_paths.txt"
test "$(git -C "$IMPL_ROOT" diff --cached --name-only | LC_ALL=C sort)" = \
  "$(LC_ALL=C sort "$KBRAG_ROOT/tests/fixtures/tickets_stage10_allowed_paths.txt")"
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "infra(tickets): isolate and secure review console"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
export SOURCE_SHA="$(git -C "$IMPL_ROOT" rev-parse HEAD)"
```

Initialize the private payload-free release state now, never before the source
commit:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
test -x "$PYTHON_BIN"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
source_sha="$(git -C "$IMPL_ROOT" rev-parse HEAD)"
tree_sha="$(git -C "$IMPL_ROOT" rev-parse HEAD^{tree})"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" init \
  --repo-root "$IMPL_ROOT" --project "$GCP_PROJECT" \
  --region "$GCP_REGION" --source-sha "$source_sha" --tree-sha "$tree_sha"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" require \
  --state-file "$release_state_file" \
  --source-sha "$source_sha" --keys project,region,source_sha,tree_sha
printf 'RELEASE_STATE_FILE=%s\n' "$release_state_file"
```

Before this `git add`, create
`tests/fixtures/tickets_stage10_allowed_paths.txt` from precisely the literal
paths in the command and sort it bytewise. Extend the Stage 1 helper in this
stage, with tests, to accept `--allow-file` if it does not already. The
allowlist file must include itself and contain no glob or directory prefix.
The literal equality check above proves that no listed file is missing and no
extra file is staged. If any other path is required, stop and amend this stage
plan before committing; do not append an ad hoc exception.

This commit occurs before Approval Gate A0. Do not include generated Terraform
locks until the approved lock-generation build returns them.

## Approval Gate A0 — Existing platform-root bootstrap

The source commit contains declarations for the new controller identities and
triggers, but it does not make them exist. Before Opus uses any ticket-specific
build identity or trigger, the owner of the existing `platform` root must
execute its already-reviewed G1B/release-controller workflow. This is an
external privileged handoff; Opus/Codex must not impersonate the owner,
bootstrap with the caller/default identity, or apply the platform root
locally.

Present to that owner:

- exact `SOURCE_SHA`, protected-repository ref, and clean status;
- the complete diff limited to the three
  `infra/terraform/live/platform/tickets_console_*` files;
- literal new service-account/trigger/resource-address allowlists;
- existing platform state serial/provider lock/controller digest;
- proof that the proposal contains no delete/replace, legacy-trigger
  neutralization, environment runtime, existing RAG/n8n IAM, or state-prefix
  ownership change.

Ask separately for authority to push this source commit, create the
platform-root plan, and apply the reviewed plan. If the existing protected
`handle-ticket-platform-plan` and `handle-ticket-platform-apply` triggers are
already materialized, the platform owner runs them by immutable SHA and
returns their authenticated output. If G1B itself is not materialized, the
owner must finish the repository's existing source-less/trusted G1B bootstrap
first; this plan does not invent a substitute path.

Required evidence from the owner:

- generation-qualified binary plan URI and sanitized JSON/text URI;
- SHA-256 for each artifact, prior state serial, provider-lock digest,
  `SOURCE_SHA`, release-controller digest, plan build ID/identity, and semantic
  verifier result;
- explicit approval receipt and apply build ID;
- after-state serial and an address/action diff showing only the fixed
  tickets bootstrap allowlist;
- zero delete/replace and zero changes to existing RAG/n8n/runtime resources.

After the owner reports success, verify only read-only:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" require \
  --state-file "$release_state_file" --source-sha "$source_sha" \
  --keys project,region,source_sha,tree_sha

for sa in \
  ticket-controller-verify \
  ticket-controller-build \
  ticket-controller-plan \
  ticket-controller-apply; do
  gcloud iam service-accounts describe \
    "$sa@$GCP_PROJECT.iam.gserviceaccount.com" \
    --project "$GCP_PROJECT" --format='value(email)'
done

for trigger in \
  rag-tickets-console-build \
  rag-tickets-console-staging-plan \
  rag-tickets-console-staging-apply \
  rag-tickets-console-production-plan \
  rag-tickets-console-production-apply; do
  gcloud builds triggers describe "$trigger" \
    --project "$GCP_PROJECT" --region "$GCP_REGION" \
    --format='value(name,serviceAccount,approvalConfig.approvalRequired)'
done
```

Compare each returned principal/config to the committed Terraform and the
approved after-state. Any absence or mismatch is an exact external blocker.
Then record the owner artifact, never values copied from chat:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
: "${PLATFORM_BOOTSTRAP_MANIFEST_URI:?generation-qualified owner artifact required}"
: "${PLATFORM_BOOTSTRAP_MANIFEST_SHA256:?owner hash required}"
: "${PLATFORM_BOOTSTRAP_APPROVAL_REF:?approval reference required}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  record-external --state-file "$release_state_file" \
  --kind platform-bootstrap \
  --manifest-uri "$PLATFORM_BOOTSTRAP_MANIFEST_URI" \
  --manifest-sha256 "$PLATFORM_BOOTSTRAP_MANIFEST_SHA256" \
  --approval-ref "$PLATFORM_BOOTSTRAP_APPROVAL_REF"
```

## Approval Gate A1 — Remote verification prerequisites

Present:

- source commit SHA and clean status;
- local test counts;
- exact proposed build configs/service accounts;
- new provider/root/state decision;
- all GCP operations the builds can perform;
- current fact that the host lacks Docker/Terraform.

Ask for explicit approval to submit verification/lock-generation Cloud Builds.
These builds create build/log/artifact records but must not deploy, apply,
write Firestore/DevRev, or create secret payloads.

Before submission, verify read-only:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" require \
  --state-file "$release_state_file" \
  --keys project,region,source_sha,tree_sha,platform_bootstrap
gcloud iam service-accounts describe \
  "ticket-controller-verify@$GCP_PROJECT.iam.gserviceaccount.com" \
  --project "$GCP_PROJECT" --format='value(email)'
```

If that exact least-privilege verifier is absent, stop at Gate A0. Never
substitute the default Cloud Build, Compute, legacy `kb-rag-runner`, or caller
identity.

## Step 10 — Approved remote verification and lock commit

Only after Gate A1 approval and verifier availability:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" require \
  --state-file "$release_state_file" --source-sha "$source_sha" \
  --keys project,region,source_sha,tree_sha,platform_bootstrap
test "$(git -C "$IMPL_ROOT" rev-parse HEAD)" = "$source_sha"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"

verify_build_json="$(mktemp -t tickets-verify-build.XXXXXX.json)"
locks_build_json="$(mktemp -t tickets-locks-build.XXXXXX.json)"
chmod 0600 "$verify_build_json" "$locks_build_json"
cleanup_build_json() {
  rm -f -- "$verify_build_json" "$locks_build_json"
}
trap cleanup_build_json EXIT INT TERM
gcloud builds submit "$IMPL_ROOT" \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --config "$KBRAG_ROOT/ci/cloudbuild.tickets-console-verify.yaml" \
  --substitutions "_CANDIDATE_SHA=$source_sha" \
  --format=json >"$verify_build_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" --kind verify \
  --build-json "$verify_build_json" --expected-source-sha "$source_sha" \
  --expected-config ci/cloudbuild.tickets-console-verify.yaml \
  --expected-service-account \
  "ticket-controller-verify@$GCP_PROJECT.iam.gserviceaccount.com"
gcloud builds submit "$IMPL_ROOT" \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --config "$KBRAG_ROOT/ci/cloudbuild.tickets-console-generate-locks.yaml" \
  --substitutions "_CANDIDATE_SHA=$source_sha" \
  --format=json >"$locks_build_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" --kind locks \
  --build-json "$locks_build_json" --expected-source-sha "$source_sha" \
  --expected-config ci/cloudbuild.tickets-console-generate-locks.yaml \
  --expected-service-account \
  "ticket-controller-verify@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_build_json
trap - EXIT INT TERM
```

Required remote evidence:

- Python 3.12 full/focused tests, ruff, mypy, pip check/audit, secret scan;
- real Firestore emulator including Stage 3 integration test;
- both image builds and non-root/read-only/offline smokes;
- Terraform 1.9.8 fmt/init/validate/test for both new roots/module;
- `terraform providers schema -json` proves `iap_enabled`;
- provider is exactly 7.41.0 and existing roots/locks are unchanged;
- generated lock artifacts have generation-qualified URIs and SHA-256.

Materialize only the two new root locks and module lock from the ingested
generation-qualified manifest. The helper downloads each exact object to a
private temporary file, checks the published SHA-256, parses HCL, requires
only `registry.terraform.io/hashicorp/google` at `7.41.0` with the expected
Linux checksums, rejects symlinks/pre-existing modifications, and atomically
writes only these three destinations:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  materialize-locks --state-file "$release_state_file" \
  --repo-root "$IMPL_ROOT" --expected-source-sha "$source_sha" \
  --destination \
  staging=infra/terraform/live/tickets-console-staging/.terraform.lock.hcl \
  --destination \
  production=infra/terraform/live/tickets-console-production/.terraform.lock.hcl \
  --destination \
  module=infra/terraform/modules/tickets_console/.terraform.lock.hcl
git -C "$IMPL_ROOT" diff --check -- \
  infra/terraform/live/tickets-console-staging/.terraform.lock.hcl \
  infra/terraform/live/tickets-console-production/.terraform.lock.hcl \
  infra/terraform/modules/tickets_console/.terraform.lock.hcl
git -C "$IMPL_ROOT" add \
  infra/terraform/live/tickets-console-staging/.terraform.lock.hcl \
  infra/terraform/live/tickets-console-production/.terraform.lock.hcl \
  infra/terraform/modules/tickets_console/.terraform.lock.hcl
git -C "$IMPL_ROOT" diff --cached --check
git -C "$IMPL_ROOT" diff --cached
git -C "$IMPL_ROOT" commit \
  -m "build(tickets): pin isolated terraform providers"
test -z "$(git -C "$IMPL_ROOT" status --porcelain=v1 --untracked-files=all)"
locked_source_sha="$(git -C "$IMPL_ROOT" rev-parse HEAD)"
locked_tree_sha="$(git -C "$IMPL_ROOT" rev-parse HEAD^{tree})"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  advance-source --state-file "$release_state_file" \
  --from-source-sha "$source_sha" --to-source-sha "$locked_source_sha" \
  --to-tree-sha "$locked_tree_sha" --require-locks-materialized

locked_verify_json="$(mktemp -t tickets-locked-verify.XXXXXX.json)"
chmod 0600 "$locked_verify_json"
cleanup_locked_verify() {
  rm -f -- "$locked_verify_json"
}
trap cleanup_locked_verify EXIT INT TERM
gcloud builds submit "$IMPL_ROOT" \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --config "$KBRAG_ROOT/ci/cloudbuild.tickets-console-verify.yaml" \
  --substitutions "_CANDIDATE_SHA=$locked_source_sha" \
  --format=json >"$locked_verify_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" --kind verify \
  --build-json "$locked_verify_json" \
  --expected-source-sha "$locked_source_sha" \
  --expected-config ci/cloudbuild.tickets-console-verify.yaml \
  --expected-service-account \
  "ticket-controller-verify@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_locked_verify
trap - EXIT INT TERM
```

Do not download “latest” artifacts or accept an unqualified GCS URI.
The second verifier run must use `init -backend=false -input=false
-lockfile=readonly` for both roots/module and must be SUCCESS before Gate B.
From this point onward, `SOURCE_SHA` means the lock-bearing commit just
verified above, never the pre-lock source commit. In a fresh fence, retrieve it
from release state into a local `source_sha`; do not rely on an exported
`SOURCE_SHA`.

## Approval Gate B — Build, plan, and staging

Present fresh verification results and ask separately for approval to:

1. push the verified lock-bearing follow-up commit (the pre-lock source commit
   was already pushed for Gate A0) and prove it descends from the platform
   bootstrap SHA;
2. build/push the two images by source SHA and resolve immutable digests;
3. create, review, and apply an exact staging `foundation` plan;
4. pause for the designated secret owner to create numeric payload versions
   and publish the payload-free manifest without exposing values to the
   executor;
5. create and review a staging `workload` plan bound to that exact manifest
   and the image digests;
6. apply that exact saved workload plan.

Production is not part of Gate B.

Every plan pipeline must run `verify_tickets_console_plan.py` with its exact
phase and use `-lockfile=readonly`. Never create a local `/tmp` plan that is
deleted before apply. Each apply must verify phase, source SHA, provider lock,
prior state serial, plan hash, manifest, build identity, and approval, then
apply the saved binary without replanning. The workload apply additionally
verifies image digests and the secret-version-manifest hash/generation.

## Step 11 — Staging only after Gate B approval

The protected source must contain `SOURCE_SHA`. If it has not been pushed to
the configured protected repository, stop and ask for explicit push authority;
do not build a deployable artifact from an unbound local upload.

Run the protected build trigger by immutable SHA:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
build_json="$(mktemp -t tickets-images-build.XXXXXX.json)"
chmod 0600 "$build_json"
cleanup_images_build() {
  rm -f -- "$build_json"
}
trap cleanup_images_build EXIT INT TERM
gcloud builds triggers run rag-tickets-console-build \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$source_sha" --format=json >"$build_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" --kind images \
  --build-json "$build_json" --expected-source-sha "$source_sha" \
  --expected-trigger rag-tickets-console-build \
  --expected-service-account \
  "ticket-controller-build@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_images_build
trap - EXIT INT TERM
```

The ingested outputs now supply the build ID, console/broker `@sha256:`
digests, SBOM, and scan evidence; never parse logs or copy them manually.

First create the foundation plan:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
foundation_plan_json="$(mktemp -t tickets-foundation-plan.XXXXXX.json)"
chmod 0600 "$foundation_plan_json"
cleanup_foundation_plan() {
  rm -f -- "$foundation_plan_json"
}
trap cleanup_foundation_plan EXIT INT TERM
gcloud builds triggers run rag-tickets-console-staging-plan \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$source_sha" \
  --substitutions "_DEPLOYMENT_PHASE=foundation" \
  --format=json >"$foundation_plan_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" \
  --kind staging-foundation-plan \
  --build-json "$foundation_plan_json" \
  --expected-source-sha "$source_sha" \
  --expected-trigger rag-tickets-console-staging-plan \
  --expected-service-account \
  "ticket-controller-plan@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_foundation_plan
trap - EXIT INT TERM
```

Retrieve generation-qualified foundation-plan URIs/hashes, prior state serial,
and verifier outcome only from release state.
The semantic result must show only the fixed foundation create allowlist and
no secret version/reference, runtime IAM, IAP, Cloud Run, or `(default)` grant.
Ask for exact-plan foundation apply approval. Only then run:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
: "${FOUNDATION_APPLY_APPROVAL_REF:?exact-plan approval required}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
foundation_plan_manifest_uri="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.foundation_plan.manifest_uri)"
foundation_plan_manifest_hash="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.foundation_plan.manifest_sha256)"
foundation_plan_uri="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key staging.foundation_plan.plan_uri)"
foundation_plan_sha256="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.foundation_plan.plan_sha256)"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  record-external --state-file "$release_state_file" \
  --kind staging-foundation-apply-approval \
  --approval-ref "$FOUNDATION_APPLY_APPROVAL_REF" \
  --bind-manifest-uri "$foundation_plan_manifest_uri" \
  --bind-manifest-sha256 "$foundation_plan_manifest_hash"
foundation_apply_json="$(mktemp -t tickets-foundation-apply.XXXXXX.json)"
chmod 0600 "$foundation_apply_json"
cleanup_foundation_apply() {
  rm -f -- "$foundation_apply_json"
}
trap cleanup_foundation_apply EXIT INT TERM
gcloud builds triggers run rag-tickets-console-staging-apply \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$source_sha" \
  --substitutions \
"_DEPLOYMENT_PHASE=foundation,_PLAN_MANIFEST_URI=$foundation_plan_manifest_uri,_PLAN_MANIFEST_HASH=$foundation_plan_manifest_hash,_PLAN_URI=$foundation_plan_uri,_PLAN_SHA256=$foundation_plan_sha256" \
  --format=json >"$foundation_apply_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" \
  --kind staging-foundation-apply \
  --build-json "$foundation_apply_json" \
  --expected-source-sha "$source_sha" \
  --expected-trigger rag-tickets-console-staging-apply \
  --expected-service-account \
  "ticket-controller-apply@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_foundation_apply
trap - EXIT INT TERM
```

After that succeeds, stop. The designated secret owner must provision all
required staging payload versions into the newly created containers through
the approved secret-handling channel. Opus/Codex must not receive, type,
redirect, log, or upload a payload. Accept back only:

- a generation-qualified `SECRET_VERSION_MANIFEST_URI`;
- `SECRET_VERSION_MANIFEST_HASH`;
- an owner approval reference;
- a payload-free DevRev-owner attestation that the token belongs to the
  dedicated integration user, is unexpired, limited to approved parts and
  `ticket:read`, and has no ticket-write privilege.

Download and validate the payload-free manifest without printing it:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
: "${SECRET_VERSION_MANIFEST_URI:?generation-qualified URI required}"
: "${SECRET_VERSION_MANIFEST_HASH:?SHA-256 required}"
: "${SECRET_OWNER_APPROVAL_REF:?secret-owner approval required}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  record-external --state-file "$release_state_file" \
  --kind staging-secret-manifest \
  --manifest-uri "$SECRET_VERSION_MANIFEST_URI" \
  --manifest-sha256 "$SECRET_VERSION_MANIFEST_HASH" \
  --approval-ref "$SECRET_OWNER_APPROVAL_REF"
secret_manifest_file="$(mktemp -t tickets-secret-manifest.XXXXXX.json)"
chmod 0600 "$secret_manifest_file"
cleanup_manifest() {
  rm -f -- "$secret_manifest_file"
}
trap cleanup_manifest EXIT INT TERM
gcloud storage cp "$SECRET_VERSION_MANIFEST_URI" "$secret_manifest_file"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/verify_tickets_secret_manifest.py" \
  --manifest-json "$secret_manifest_file" \
  --environment staging \
  --source-uri "$SECRET_VERSION_MANIFEST_URI" \
  --expected-sha256 "$SECRET_VERSION_MANIFEST_HASH" \
  --syntax-and-hash-only
cleanup_manifest
trap - EXIT INT TERM
test ! -e "$secret_manifest_file"
```

If the URI lacks an object generation, the hash differs, a required version is
non-numeric, or the manifest includes a value/payload field, stop. Existence,
enabled state, secret ownership, and replication/location are then checked
inside the protected workload-plan build running as the exact metadata-only
`ticket-controller-plan` identity—not with ambient local ADC. Never substitute
`latest` or grant the executor/plan identity payload access.

Now create the workload plan:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
console_image_digest="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key images.console_digest)"
broker_image_digest="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key images.broker_digest)"
secret_manifest_uri="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.secret_manifest.manifest_uri)"
secret_manifest_hash="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.secret_manifest.manifest_sha256)"
workload_plan_json="$(mktemp -t tickets-workload-plan.XXXXXX.json)"
chmod 0600 "$workload_plan_json"
cleanup_workload_plan() {
  rm -f -- "$workload_plan_json"
}
trap cleanup_workload_plan EXIT INT TERM
gcloud builds triggers run rag-tickets-console-staging-plan \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$source_sha" \
  --substitutions \
"_OPERATION=plan,_DEPLOYMENT_PHASE=workload,_CONSOLE_IMAGE_DIGEST=$console_image_digest,_BROKER_IMAGE_DIGEST=$broker_image_digest,_SECRET_VERSION_MANIFEST_URI=$secret_manifest_uri,_SECRET_VERSION_MANIFEST_HASH=$secret_manifest_hash" \
  --format=json >"$workload_plan_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" \
  --kind staging-workload-plan \
  --build-json "$workload_plan_json" \
  --expected-source-sha "$source_sha" \
  --expected-trigger rag-tickets-console-staging-plan \
  --expected-service-account \
  "ticket-controller-plan@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_workload_plan
trap - EXIT INT TERM
```

Read the generation-qualified workload plan/manifest URIs and hashes only from
release state. The ingested verifier result must only add the
allowlisted workload resources, preserve every foundation address, and bind
exact digests/versions. Ask for a separate exact-plan workload apply approval.
Immediately before that apply, download and validate the secret manifest again
from the same generation-qualified URI inside the protected plan identity, so
a plan-time check cannot substitute for an apply-time check:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
secret_manifest_uri="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.secret_manifest.manifest_uri)"
secret_manifest_hash="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.secret_manifest.manifest_sha256)"
secret_reverify_json="$(mktemp -t tickets-secret-reverify.XXXXXX.json)"
chmod 0600 "$secret_reverify_json"
cleanup_secret_reverify() {
  rm -f -- "$secret_reverify_json"
}
trap cleanup_secret_reverify EXIT INT TERM
gcloud builds triggers run rag-tickets-console-staging-plan \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$source_sha" \
  --substitutions \
"_OPERATION=verify-secret-manifest,_DEPLOYMENT_PHASE=workload,_SECRET_VERSION_MANIFEST_URI=$secret_manifest_uri,_SECRET_VERSION_MANIFEST_HASH=$secret_manifest_hash" \
  --format=json >"$secret_reverify_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" \
  --kind staging-secret-reverify \
  --build-json "$secret_reverify_json" \
  --expected-source-sha "$source_sha" \
  --expected-trigger rag-tickets-console-staging-plan \
  --expected-service-account \
  "ticket-controller-plan@$GCP_PROJECT.iam.gserviceaccount.com"
cleanup_secret_reverify
trap - EXIT INT TERM
```

Only then run:

```bash
set -euo pipefail
export IMPL_ROOT="${IMPL_ROOT:-/Users/ivanalvis/Desktop/ForUsGuide-tickets-console}"
export KBRAG_ROOT="$IMPL_ROOT/kb-rag-system"
export PYTHON_BIN="${PYTHON_BIN:-/Users/ivanalvis/Desktop/ForUsGuide-handle-ticket-finalization/kb-rag-system/.venv/bin/python}"
export GCP_PROJECT="${GCP_PROJECT:-rag-kb-system}"
export GCP_REGION="${GCP_REGION:-us-central1}"
: "${WORKLOAD_APPLY_APPROVAL_REF:?exact workload-plan approval required}"
release_state_file="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" path \
  --repo-root "$IMPL_ROOT")"
source_sha="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key source_sha)"
workload_plan_manifest_uri="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.workload_plan.manifest_uri)"
workload_plan_manifest_hash="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.workload_plan.manifest_sha256)"
workload_plan_uri="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" --key staging.workload_plan.plan_uri)"
workload_plan_sha256="$("$PYTHON_BIN" \
  "$KBRAG_ROOT/scripts/tickets_release_state.py" get \
  --state-file "$release_state_file" \
  --key staging.workload_plan.plan_sha256)"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  record-external --state-file "$release_state_file" \
  --kind staging-workload-apply-approval \
  --approval-ref "$WORKLOAD_APPLY_APPROVAL_REF" \
  --bind-manifest-uri "$workload_plan_manifest_uri" \
  --bind-manifest-sha256 "$workload_plan_manifest_hash"
workload_apply_json="$(mktemp -t tickets-workload-apply.XXXXXX.json)"
chmod 0600 "$workload_apply_json"
cleanup_workload_apply() {
  rm -f -- "$workload_apply_json"
}
trap cleanup_workload_apply EXIT INT TERM
gcloud builds triggers run rag-tickets-console-staging-apply \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --sha "$source_sha" \
  --substitutions \
"_DEPLOYMENT_PHASE=workload,_PLAN_MANIFEST_URI=$workload_plan_manifest_uri,_PLAN_MANIFEST_HASH=$workload_plan_manifest_hash,_PLAN_URI=$workload_plan_uri,_PLAN_SHA256=$workload_plan_sha256" \
  --format=json >"$workload_apply_json"
"$PYTHON_BIN" "$KBRAG_ROOT/scripts/tickets_release_state.py" \
  ingest-build --state-file "$release_state_file" \
  --kind staging-workload-apply \
  --build-json "$workload_apply_json" \
  --expected-source-sha "$source_sha" \
  --expected-trigger rag-tickets-console-staging-apply \
  --expected-service-account \
  "ticket-controller-apply@$GCP_PROJECT.iam.gserviceaccount.com" \
  --require-kind staging-secret-reverify
cleanup_workload_apply
trap - EXIT INT TERM
```

All URI variables above must include immutable object generations. Both apply
triggers verify their saved artifacts and never replan. If workload fails,
leave the inert foundation visible and produce a reviewed follow-up/rollback
plan; do not destroy secret containers or the named database ad hoc.

After apply, verify:

- IAP denies unapproved and allows approved identities;
- app JWT/RBAC/CSRF matrix;
- console SA can access only `tickets-console-staging`;
- broker SA can read but not write `(default)`;
- agent has API access but no Firestore/Secret/deploy permissions;
- console-only broker invocation;
- read-only scoped DevRev list/get/timeline;
- synthetic review/conflict/batch/import/export/reversal;
- logs/metrics contain no ticket/user/message/token content.

Do not create production resources, import a real CSV, deploy producer
instrumentation, change n8n, write DevRev, or reindex Pinecone.

## Definition of Done

- Source is committed before remote operations and scope-allowlisted.
- New provider/state/roots do not alter existing provider locks or ownership.
- Console database isolation and broker compensating boundary are explicit and
  contract-tested.
- Direct IAP, keyless agent access, RBAC, CSRF, secrets, retention, audit, and
  monitoring are declared fail closed.
- Foundation and workload plans are separately reviewed/applied, with external
  secret provisioning and no payload in Terraform, logs, chat, or Git.
- If approved remote gates ran, Python 3.12/emulator/images/Terraform evidence
  and immutable locks/artifacts are recorded.
- If verifier/bootstrap or approval is absent, exact remote gates remain
  blocked and production is unchanged; do not claim them passed.

Proceed to Stage 11 with a clean worktree. Stage 11 may perform all local
verification while an external gate is blocked, but cannot declare deployment
readiness without the missing evidence.
