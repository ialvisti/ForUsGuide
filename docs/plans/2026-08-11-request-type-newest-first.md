# Request Type Column and Newest-First Queue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show each RAG request type beside review status and guarantee that the queue is paginated from the newest execution timestamp to the oldest.

**Architecture:** Reuse the existing `route` value already present in every `TicketEvaluationSummary` and render it with reviewer-facing labels; no API schema change is needed. Move queue ordering into the repository's Firestore query over `event.occurred_at`, descending with execution ID as a deterministic descending tie-breaker, and seal both cursor components so ordering remains correct across pages and filters. Keep all work on `main` because the repository was explicitly consolidated to one branch.

**Tech Stack:** FastAPI/Pydantic, Firestore async queries, signed opaque cursors, vanilla HTML/ES modules, pytest, Node browser contracts, Cloud Build, Terraform, Cloud Run.

---

### Task 1: Pin the reviewer-facing request type column

**Files:**
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_runtime_copy_i18n_contract.py`
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/render.js`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`

**Step 1: Write the failing table contract**

Require the operational column sequence to contain `Request type` immediately after `Review status`, and require the row renderer to consume `row.route` through a closed label map.

**Step 2: Write the failing translation contract**

Round-trip `Request type` / `Tipo de solicitud`; reuse the existing translations for `Knowledge Question` and `Generate Response`.

**Step 3: Run the focused tests and confirm RED**

Run: `cd kb-rag-system && ../.venv-local/bin/python -m pytest -q tests/test_tickets_ui_contract.py::TestColumns tests/test_ticket_runtime_copy_i18n_contract.py`

Expected: FAIL because the table currently jumps directly from review status to received time and the new label is absent.

**Step 4: Implement the minimal column**

Add the header beside review status, increase the table column count, and render only the two enum values as `Knowledge Question` or `Generate Response`. Preserve a safe visible fallback for any future unknown value.

**Step 5: Run the focused tests and confirm GREEN**

Run the command from Step 3.

Expected: PASS.

### Task 2: Make server pagination newest-first

**Files:**
- Modify: `kb-rag-system/tests/test_ticket_evaluation_ingest.py`
- Modify: `kb-rag-system/tests/test_firestore_query_contract.py`
- Modify: `kb-rag-system/data_pipeline/ticket_review_repository.py`

**Step 1: Write a failing cross-page ordering test**

Persist runs whose execution IDs conflict with chronological order, include equal timestamps, request one item per page, and assert:

```python
assert execution_ids == [
    "ccc-new-high:0",
    "bbb-new-low:0",
    "zzz-mid:0",
    "aaa-old:0",
]
```

This pins descending `event.occurred_at` and descending execution ID for ties.

**Step 2: Write a failing Firestore query construction test**

Require descending `order_by("event.occurred_at")`, descending `order_by("__name__")`, and a cursor containing both the datetime and execution ID.

**Step 3: Run both tests and confirm RED**

Run: `cd kb-rag-system && ../.venv-local/bin/python -m pytest -q tests/test_ticket_evaluation_ingest.py tests/test_firestore_query_contract.py`

Expected: FAIL because evaluation pages currently scan by ascending document ID.

**Step 4: Implement the descending backend primitive and cursor**

Add one typed backend method for a descending field query. Mirror Firestore semantics in memory, including nested-field lookup and ID tie-breaking. Seal `occurred_at_unix_us` together with `execution_id` and the filter digest; reject malformed or stale cursor shapes.

**Step 5: Run both tests and confirm GREEN**

Run the command from Step 3.

Expected: PASS.

### Task 3: Update queue copy and verify the product behavior

**Files:**
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_runtime_copy_i18n_contract.py`
- Modify: `kb-rag-system/ui/tickets/assets/app.js`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`

**Step 1: Replace the obsolete stable-order caption**

Use `Tickets ready for evaluation, newest received first.` and translate it as `Tickets listos para evaluar, del más reciente al más antiguo.`

**Step 2: Run UI and repository suites**

Run all ticket UI, evaluation ingest, repository, route, and translation contracts.

**Step 3: Validate locally in EN and ES**

Open the fixture console, confirm the new column is beside review status, confirm both route labels translate, and compare every visible timestamp pair to ensure non-increasing order.

### Task 4: Build, deploy, and prove the final state

**Files:**
- Use: `kb-rag-system/cloudbuild.yaml`
- Use: `kb-rag-system/infra/terraform/live/tickets-console-production/`

**Step 1: Commit and push clean `main`**

Run `git diff --check`, inspect the exact staged patch, commit, push, and verify that `main` remains the sole local/remote branch and worktree.

**Step 2: Run the clean Cloud Build gate**

Build the exact commit. Require the full pytest suite, Node syntax checks, Ruff, mypy, dependency audit, secret scan, container smoke, image push, SBOM, and vulnerability gate to succeed.

**Step 3: Promote the immutable digest**

Use Terraform in the production console root. Accept only the expected Cloud Run image/revision update.

**Step 4: Validate production**

Confirm the new revision is Ready at 100% traffic, request type appears beside review status in Spanish, visible rows are newest-first, both request-type filters work, and fresh error logs remain empty.
