# Ticket Console Readability and Team Access Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the ticket console faster to scan on desktop and mobile, and restore team access without weakening its two-layer authorization boundary.

**Architecture:** Keep the existing vanilla HTML/CSS/ES-module console and signed-IAP authentication. Simplify the primary list by removing duplicate headings, grouping route/date metadata with the ticket, hiding remediation-only selection controls until they are relevant, and compressing the mobile header. Treat access as two explicit configuration gates: IAP reachability plus application role bindings; do not deploy either gate alone.

**Tech Stack:** Vanilla HTML/CSS/ES modules, FastAPI static UI, pytest DOM/browser contracts, Google Cloud Run direct IAP, Secret Manager, Terraform 1.9.8, gcloud.

---

### Task 1: Pin the readable list contract

**Files:**
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_runtime_copy_i18n_contract.py`

**Step 1: Write the failing structural tests**

Add assertions that the list has one visible task heading, the queue heading is the list view's `h1`, the detail heading is its `h1`, request type and received time live inside the ticket cell instead of separate columns, the caption stays accessible but visually hidden, rows are no longer extra keyboard stops, and remediation-only selection UI is hidden unless the capability and disclosure are active.

```python
assert _by_id(dom, "queue-heading").tag == "h1"
assert _by_id(dom, "detail-heading").tag == "h1"
assert "operational-heading" not in ids
assert REQUIRED_EXECUTION_COLUMNS == (
    "Ticket", "Review status", "Rating", "Reviewer", "Actions"
)
assert "cell-ticket-meta" in scripts["render.js"]
```

Add English/Spanish round-trip coverage for the replacement list copy.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
cd kb-rag-system
./venv/bin/pytest -q tests/test_tickets_ui_contract.py tests/test_ticket_runtime_copy_i18n_contract.py
```

Expected: failures for the old duplicate heading, old 7-column visible contract, and missing grouped ticket metadata.

### Task 2: Simplify the document hierarchy and queue controls

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/app.js`
- Modify: `kb-rag-system/ui/tickets/assets/render.js`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`

**Step 1: Replace the duplicate list introduction**

Remove `#operational-heading`. Promote `#queue-heading` to `h1`, use the task-focused label “Tickets to review,” and keep one concise instruction below it. Promote `#detail-heading` to `h1` and `#evaluation-heading` to `h2` because the list and detail regions are mutually exclusive.

**Step 2: Group row metadata**

Render request type and `<time>` under the ticket title in `.cell-ticket-meta`, remove their standalone cells/headers, and change `COLUMN_COUNT` from 8 to 6. Preserve `Intl`/document-locale date rendering and text-only remote content.

Remove `tabindex` plus row click/keydown navigation; keep the explicit, named “Review ticket …” button as the single navigation action. Checkboxes remain independently operable only when batch selection is relevant.

**Step 3: Remove irrelevant batch controls**

Add `technicalBatch` to the DOM registry and set its `hidden` state from both `remediation_enabled` and `canCurateBatches(role)`. Keep selection state in memory; do not broaden any API capability.

**Step 4: Update localized copy**

Add exact English/Spanish mappings for “Tickets to review” and the concise instruction. Retain translations for runtime request-type and date content.

**Step 5: Run the focused tests and verify GREEN**

Run the command from Task 1. Expected: all selected tests pass.

### Task 3: Reduce visual density, especially on mobile

**Files:**
- Modify: `kb-rag-system/ui/tickets/assets/tickets.css`
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`

**Step 1: Write failing CSS contract assertions**

Pin these behaviors: the mobile header is not sticky, compact mobile controls preserve 44 px targets, the results heading and live status share one row, long ticket metadata wraps, and selection cells appear only while technical batch actions are expanded.

**Step 2: Verify RED**

Run:

```bash
cd kb-rag-system
./venv/bin/pytest -q tests/test_tickets_ui_contract.py
```

Expected: the new compact-layout assertions fail against the current stylesheet.

**Step 3: Implement the smallest CSS override**

Use the existing tokens and breakpoints. Remove redundant card/caption chrome, give prose a readable measure, increase functional secondary text from 10–11 px to at least 12 px, compact the mobile header into short rows, make it non-sticky below 768 px, and hide `.col-select` unless `#technical-batch-details[open]` is both available and open. Preserve focus rings, reduced motion, dark mode, touch targets, and the filter sheet's `overscroll-behavior`.

**Step 4: Verify GREEN**

Run the focused test again. Expected: pass with no warnings.

### Task 4: Browser-check the actual reading flow

**Files:**
- No product file changes unless the visual check exposes a regression.

**Step 1: Start the isolated fixture**

Use `tests.support.tickets_fixture_server` on `127.0.0.1` with a mode-0700 state directory and a bounded lifetime.

**Step 2: Check desktop**

Verify the first viewport exposes the queue and beginning of results without duplicate introductions; open a ticket and verify the evaluation form remains the dominant task.

**Step 3: Check mobile**

At 390 x 844, verify the header no longer consumes most of the viewport, filters remain a keyboard-accessible sheet, row cards have no horizontal overflow, and all controls retain 44 px targets.

**Step 4: Stop the verified fixture**

Use the fixture runner's nonce-checked `stop` command; do not kill by process pattern.

### Task 5: Remediate production team access as one atomic rollout

**Files:**
- Update only after the approved principals are known: the protected production Terraform inputs/promotion manifest and a new numeric version of `Tickets_Role_Bindings`.
- Do not change `kb-rag-system/api/reviewer_auth.py`; the fail-closed behavior is working as designed.

**Step 1: Collect the access matrix**

Obtain each teammate's primary Google Workspace email and application role (`viewer`, `reviewer`, `remediator`, or `admin`). If using a Google Group for IAP, also obtain its exact group address; group membership does not replace per-email application roles.

**Step 2: Prepare both gates together**

Add the approved users/group to `reviewer_iap_members`. Create a new Secret Manager version whose role map contains every approved primary email; keep Ivan's existing admin binding. Pin the new numeric secret version in the deployment input.

**Step 3: Review a saved Terraform plan**

Expected changes: the IAP accessor member set, the console revision's pinned role-binding secret version, and no unrelated IAM, database, service-account, or secret deletion/replacement.

**Step 4: Apply only after explicit approval**

Do not apply IAP alone or the role secret alone. Roll both changes through the existing release controller.

**Step 5: Verify with two identities**

Confirm Ivan retains `admin`; confirm one approved teammate reaches `/tickets`, `/api/admin/v1/session` reports the assigned role, and an unapproved identity remains denied. Inspect sanitized IAP/application logs without printing assertions, tokens, or secret payloads.

### Task 6: Final verification

**Files:**
- Review all modified files from Tasks 1–3.

**Step 1: Run the complete relevant suite**

```bash
cd kb-rag-system
./venv/bin/pytest -q \
  tests/test_tickets_ui_contract.py \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_runtime_copy_i18n_contract.py \
  tests/test_ticket_structured_presenter_browser_contract.py \
  tests/test_ticket_detail_browser_contract.py \
  tests/test_reviewer_auth.py \
  tests/test_ticket_evaluation_infrastructure_contract.py
```

Expected: zero failures.

**Step 2: Inspect the diff**

Confirm no user-owned untracked files changed, no credential/token/secret value entered Git, and no authorization policy was weakened.
