# Home Outcome Filter and Main Consolidation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expose the persisted RAG outcome as a primary home-page filter, then preserve all pending work while consolidating the repository onto a clean `main` with no additional branches or worktrees.

**Architecture:** Reuse the existing `route` filter and URL parameter because the API already models the two outcomes as `knowledge_question` and `generate_response`. Move that control out of the collapsed technical disclosure, rename only its reviewer-facing copy to Outcome, and retain the existing state and request wiring. After verification, fast-forward `main`, commit the user's unrelated documentation separately, push `main`, and remove only branches/worktrees proven to be fully merged and clean.

**Tech Stack:** Vanilla HTML/CSS/ES modules, FastAPI static UI, pytest DOM contracts, Node translation harness, Git, Cloud Build, Terraform, Cloud Run.

---

### Task 1: Pin the primary Outcome filter contract

**Files:**
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_runtime_copy_i18n_contract.py`

**Step 1: Write the failing DOM test**

Assert that `execution-route` is a direct primary field of `filters-executions`, is not nested under `technical-filter-details`, has label `Outcome`, and exposes exactly the stable wire values:

```python
assert options == [
    ("", "Any outcome"),
    ("knowledge_question", "Knowledge Question"),
    ("generate_response", "Generate Response"),
]
```

**Step 2: Write the failing state-label test**

Assert that `activeFilters()` presents the persisted `route` field as `Outcome` while the underlying field remains `route`.

**Step 3: Write the failing i18n test**

Round-trip the new English/Spanish filter copy, including `Outcome`/`Resultado`, `Any outcome`/`Cualquier resultado`, and both option labels.

**Step 4: Run tests to verify they fail**

Run: `cd kb-rag-system && pytest -q tests/test_tickets_ui_contract.py tests/test_ticket_runtime_copy_i18n_contract.py`

Expected: FAIL because the selector is currently technical and its labels still say RAG route / Generated response.

### Task 2: Promote the existing route selector

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/state.js`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`

**Step 1: Move the selector into the primary toolbar**

Place the existing `execution-route` select alongside ticket ID and review status. Use the reviewer-facing label `Outcome` and retain `name="route"` plus the existing enum values.

**Step 2: Rename the active-filter chip**

Change only the display label in `activeFilters()` from `Route` to `Outcome`; do not change the state key, URL parameter, or API query.

**Step 3: Add exact bilingual copy**

Add round-trip translations for `Any outcome`, `Knowledge Question`, and `Generate Response`, reusing the existing `Outcome` translation.

**Step 4: Run focused tests to verify they pass**

Run: `cd kb-rag-system && pytest -q tests/test_tickets_ui_contract.py tests/test_ticket_runtime_copy_i18n_contract.py`

Expected: PASS.

### Task 3: Verify the complete change

**Files:**
- Verify: `kb-rag-system/ui/tickets/index.html`
- Verify: `kb-rag-system/ui/tickets/assets/state.js`
- Verify: `kb-rag-system/ui/tickets/assets/preferences.js`

**Step 1: Run formatting and static checks**

Run the repository's documented lint/type/static checks through the same commands used by Cloud Build.

**Step 2: Run the full automated suite**

Run: `cd kb-rag-system && pytest -q`

Expected: all tests PASS.

**Step 3: Confirm the patch scope**

Inspect `git diff --check`, the staged diff, and the effective list of changed files. Confirm that the wire field remains `route`.

### Task 4: Preserve pending work and consolidate Git

**Files:**
- Preserve: `PA/DevRev/SYSTEM_PROMPT.md`
- Preserve: `OVERVIEW_PARA_INFOGRAFIA.md`
- Preserve: `metodologias-agiles/`

**Step 1: Inspect pending files and scan for accidental secrets**

Review the exact file inventory and run the repository's secret scanner before staging the user's unrelated work.

**Step 2: Commit the Outcome feature**

Commit the implementation, tests, and this plan as one focused feature commit.

**Step 3: Commit pre-existing documentation separately**

Preserve the unrelated pending files in a distinct documentation commit so cleaning the tree does not discard them or mix them into the product change.

**Step 4: Fast-forward and push `main`**

Switch to `main`, fast-forward it to the verified history, and push `origin/main`.

**Step 5: Remove only proven-safe branches and worktrees**

Re-check that the secondary worktree is clean and its branch is an ancestor of `main`. Remove that exact worktree, delete both merged local feature branches, and delete the merged remote feature branch.

**Step 6: Prove the requested final state**

Run `git status --short --branch`, `git branch -a`, and `git worktree list`. Expected: clean `main`, only `origin/main`, and one worktree.

### Task 5: Deploy and validate production

**Files:**
- Use: `kb-rag-system/cloudbuild.yaml`
- Use: `kb-rag-system/infra/terraform/live/tickets-console-production/`

**Step 1: Build the exact `main` commit**

Submit the clean `main` source through Cloud Build and record the immutable image digest.

**Step 2: Promote only that digest**

Run Terraform plan and apply for the production console, accepting only the expected Cloud Run image/revision change.

**Step 3: Validate production**

Confirm the new revision is Ready, receives 100% traffic, and serves the Outcome filter with both values in English and Spanish. Verify filtering preserves the existing `route` query contract and check fresh error logs.
