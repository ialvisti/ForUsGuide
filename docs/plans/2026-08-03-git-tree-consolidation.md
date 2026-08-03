# Git Tree Consolidation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve all meaningful work from ForUsGuide and ForUsBots on `main`, remove obsolete or sensitive artifacts, and leave both local and GitHub repositories with no branches other than `main`.

**Architecture:** First create reviewable checkpoint commits from every dirty worktree so no source work is lost. Integrate those commits onto the current remote `main`, keeping the already-deployed incident and idempotency changes authoritative when resolving conflicts, then run focused and full verification. Delete auxiliary branches and worktrees only after ancestry, tests, remote refs, and production health have been rechecked.

**Tech Stack:** Git/GitHub CLI, Python/pytest, Node.js `node:test`, JSON validation with `jq`, Google Cloud CLI.

---

### Task 1: Inventory every ref and worktree

**Files:**
- Inspect: `/Users/ivanalvis/Desktop/ForUsGuide`
- Inspect: `/Users/ivanalvis/Desktop/ForUsBots`

**Step 1: Fetch current refs**

Run `git fetch --prune origin` in both repositories.

**Step 2: Classify branches**

Use `git rev-list`, `git cherry`, and `git merge-base --is-ancestor` against `origin/main`.

**Step 3: Classify dirty files**

Compare each tracked and untracked path with the blob at `origin/main`, separating real changes from files already incorporated upstream.

**Step 4: Record exclusions**

Exclude generated caches, `.DS_Store`, captured HTTP responses, captured portal HTML, empty environment placeholders, and generated operational reports from the public repositories.

### Task 2: Preserve the original ForUsGuide worktree

**Files:**
- Modify: `.gitignore`
- Modify: `PA/Distributions/LT: How to Request a 401(k) Termination Cash Withdrawal or Rollover.json`
- Add: `AUDITORIA_EJECUCIONES_GCP_2026-08-02.md`
- Add: `docs/plans/2026-07-27-n8n-gcp-stability.md`
- Add: `flows_n8n/participant_search.json`
- Add: `tests/test_participant_search_workflow.py`
- Add: `tickets-development-plan/*.md`
- Delete: `F3_MULTI_INQUIRY_SPLIT_PLAN.md`
- Delete: `KQ_PER_INQUIRY_SYNTHESIS_PLAN.md`

**Step 1: Add ignore rules**

Ignore Python caches and local response captures while preserving the existing `.vscode` rule.

**Step 2: Validate structured files**

Run `jq empty` on the changed knowledge article and n8n workflow. Expected: exit 0.

**Step 3: Run the workflow regression test**

Run `pytest -q tests/test_participant_search_workflow.py`. Expected: 5 passed.

**Step 4: Commit the checkpoint**

Stage only the listed source and documentation files, verify the staged diff, and commit without `response.json` or `__pycache__`.

### Task 3: Preserve the ForUsGuide tickets-review worktree

**Files:**
- Add/modify: `kb-rag-system/api/ticket_review_models.py`
- Add/modify: `kb-rag-system/api/tickets_console_config.py`
- Add: `kb-rag-system/data_pipeline/devrev_client.py`
- Add: `kb-rag-system/data_pipeline/ticket_review_repository.py`
- Modify: `kb-rag-system/firestore.indexes.json`
- Add/modify: related ticket-review unit and integration tests

**Step 1: Run focused unit tests**

Run the ticket-review model, DevRev client, repository, staged-scope, and Terraform contract suites. Expected: all collected unit tests pass.

**Step 2: Check emulator tests**

Collect the Firestore integration suite and record skips when the emulator is not active.

**Step 3: Commit the checkpoint**

Stage the complete coherent ticket-review implementation and commit it on the temporary feature branch.

### Task 4: Integrate and verify ForUsGuide `main`

**Files:**
- Merge: checkpoint commits from Tasks 2 and 3

**Step 1: Merge onto current main**

Merge the ticket-review branch and cherry-pick the original-worktree checkpoint onto the clean `main` worktree. Resolve conflicts with current incident-remediation code as the runtime baseline.

**Step 2: Run focused tests after integration**

Repeat the participant-workflow and ticket-review suites on the merged tree.

**Step 3: Run repository verification**

Run the complete feasible Python suite plus `git diff --check` and JSON validation. Record any environment-only skips explicitly.

**Step 4: Push main**

Push only after local `main` is clean and based on the current `origin/main`.

### Task 5: Preserve and integrate ForUsBots work

**Files:**
- Modify: `README.md`
- Modify: `docs/**`
- Add: `docs/sandbox/es/js/core/users-management-ui.js`
- Modify: `.gitignore`
- Delete: tracked `.DS_Store`

**Step 1: Create a checkpoint branch**

Move the dirty original checkout from its stale `main` to a temporary local branch and commit the documentation/UI work only.

**Step 2: Exclude unsafe or obsolete artifacts**

Do not commit captured portal HTML, `.DS_Store`, `.envv`, or generated annual-notice reports. Do not merge the old PostgreSQL-only report scripts because current production uses the Firestore queue and the scripts lack a compatible runtime dependency.

**Step 3: Merge onto current main**

Fast-forward local `main` to `origin/main`, merge the documentation checkpoint, and preserve the durable-idempotency API contract when resolving OpenAPI conflicts.

**Step 4: Run verification**

Run `npm test`, direct `node --test`, lint, JSON/YAML parsing, and the relevant documentation/idempotency contract tests. Expected: zero failures.

**Step 5: Push main and verify Cloud Build**

Push `main`, wait for the triggered build, and confirm the production service remains healthy.

### Task 6: Remove every non-main ref and auxiliary worktree

**Files:**
- Remove: auxiliary clean worktrees/clones after their commits are reachable from `main`

**Step 1: Prove reachability**

For every source branch being removed, verify its meaningful commits are ancestors of `main` or document why its generated/obsolete contents were intentionally excluded.

**Step 2: Delete remote branches**

Delete every GitHub branch except `main` in both repositories.

**Step 3: Remove auxiliary worktrees**

Prune already-missing worktrees, remove clean auxiliary worktrees, switch each canonical checkout to `main`, and delete all local branches except `main`.

**Step 4: Final verification**

Confirm in both repositories: `git status --porcelain` is empty, local heads contain only `main`, `git ls-remote --heads origin` contains only `refs/heads/main`, local and remote main SHAs match, no pull request is open, and production health checks pass.
