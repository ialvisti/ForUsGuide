# Reviewer-First Ticket Workflow Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Convert `/tickets` from a technical RAG dashboard into a fast operational review queue where support agents rate completed answers first and auditors expand technical evidence only when needed.

**Architecture:** Keep the existing static, same-origin HTML/CSS/ES-module application and all stable form/API identifiers. Remove promotional and aggregate surfaces, reduce the queue to reviewer-relevant columns, move the evaluation form to the top of the detail view, and place execution, evidence, conversation, history, and remediation inside a collapsed native disclosure. Make light mode deterministic in the initial HTML and preference controller while preserving the manual dark-mode toggle.

**Tech Stack:** Static HTML, CSS, vanilla ES modules, FastAPI fixture server, pytest contract/browser tests.

---

### Task 1: Lock the reviewer-first page contract

**Files:**
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`

**Step 1: Write failing structural tests**

Assert that the shipped document has no `.page-hero` or `#kpi-strip`, uses the queue heading as its only `h1`, exposes only reviewer-relevant result columns, places `#evaluation-form` before technical content, and wraps technical content in a closed `<details id="technical-audit-details">` with an accessible `<summary>`.

**Step 2: Write the failing theme test**

Assert that `<html data-theme="light">` is shipped and `preferences.js` initializes `theme` to `"light"` without using the system color scheme to choose the first theme.

**Step 3: Run the focused tests and confirm RED**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/python -m pytest \
  tests/test_tickets_ui_contract.py::TestReviewerFirstLayout \
  tests/test_ticket_detail_ui_contract.py::TestReviewerFirstWorkflow -q
```

Expected: failures for the existing hero, KPI strip, technical-first detail order, full technical table, and system-derived initial theme.

### Task 2: Simplify the queue into an operational worklist

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/app.js`
- Modify: `kb-rag-system/ui/tickets/assets/render.js`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`
- Modify: `kb-rag-system/ui/tickets/assets/tickets.css`

**Step 1: Remove promotional and aggregate surfaces**

Delete the announcement rail, `.page-hero`, and KPI section. Promote the queue heading to `h1`, rename it “Ticket reviews”, and use one short instruction: “Open a completed ticket, rate the answer, and record only what needs correction.”

**Step 2: Remove KPI dependencies**

Delete the KPI DOM cache and `pageTallies` rendering from `app.js`; leave the pure state helper untouched for backward compatibility.

**Step 3: Reduce the result table**

Render seven columns: selection, ticket, review status, received, rating, reviewer, and action. Keep execution IDs in row datasets for deep links and API calls, not as reviewer-facing content. Change `COLUMN_COUNT` and table captions accordingly.

**Step 4: Demote technical filters and batch actions**

Keep ticket ID and review status visible. Place execution ID, route, and run status inside a closed native “Technical filters” disclosure. Place remediation batch controls inside a closed “Technical batch actions” disclosure without changing their IDs or behavior.

**Step 5: Add EN/ES copy and compact operational styling**

Add exact translations for the new reviewer copy and disclosures. Replace hero/KPI spacing with a flat queue layout, compact header, and clear row action.

**Step 6: Run focused tests and confirm GREEN**

Run the two contract modules and verify every new assertion passes.

### Task 3: Put the evaluation before all technical evidence

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`
- Modify: `kb-rag-system/ui/tickets/assets/tickets.css`

**Step 1: Move the evaluation shell**

Place `.evaluation-shell` immediately after the detail title. Add the short instruction “Rate the final answer and document the correction, if any.”

**Step 2: Reorder primary fields**

Order the form as rating, comments, expected behavior, observation type, severity, assignment, and status. Preserve every existing field ID, name, validation rule, and save behavior.

**Step 3: Collapse specialist fields**

Put topic, legacy type, remediation target, and resolution-only metadata inside `<details class="reviewer-advanced-fields">` with summary “Advanced review fields”. Keep resolution visibility controlled by the existing `hidden` contract.

**Step 4: Collapse the technical workspace**

Wrap `.detail-overview`, tabs, conversation, evidence, history, and remediation in `<details id="technical-audit-details">`. Default it closed and describe it as containing execution diagnostics and audit evidence.

**Step 5: Style the form for fast completion**

Use a flat, wide evaluation surface; make rating the first full-width control; keep the save bar visually persistent on desktop without obscuring content; and make all disclosures touch-friendly and keyboard-visible.

**Step 6: Run focused tests and confirm GREEN**

Run the detail UI and browser-contract modules.

### Task 4: Make light the deterministic default

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`
- Test: `kb-rag-system/tests/test_tickets_ui_contract.py`

**Step 1: Ship the initial theme**

Set `data-theme="light"` on the root HTML element and keep the light `theme-color` meta value.

**Step 2: Initialize preferences from light**

Start the controller with `let theme = "light"`. Preserve manual switching and accessibility labels; do not change theme in response to `prefers-color-scheme` until the user presses the control.

**Step 3: Verify theme tests**

Run the theme/language contract class and JavaScript syntax checks.

### Task 5: Live QA, verification, commit, and push

**Files:**
- Verify all scoped files above.

**Step 1: Run live reviewer workflow QA**

Start the bounded fixture server, reload `/tickets`, verify the flat queue in light mode, open a row, confirm the evaluation is immediately visible, expand technical audit details, switch EN/ES and light/dark, and inspect mobile at 390 px.

**Step 2: Run the complete ticket verification suite**

Run:

```bash
cd kb-rag-system
for ticket_js in ui/tickets/assets/*.js; do node --check "$ticket_js" || exit 1; done
../.venv-local/bin/python -m pytest -q \
  tests/test_tickets_ui_contract.py \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_detail_browser_contract.py \
  tests/test_tickets_fixture_app.py \
  tests/test_tickets_fixture_server.py \
  tests/test_tickets_console_app.py \
  tests/test_tickets_csrf.py \
  tests/test_no_ticket_file_interchange_contract.py
```

Expected: all collected tests pass except documented skips.

**Step 3: Inspect the exact release scope**

Run `git diff --check`, review `git diff`, and stage only ticket UI/tests and both implementation plans. Explicitly exclude `PA/DevRev/SYSTEM_PROMPT.md` and `OVERVIEW_PARA_INFOGRAFIA.md`.

**Step 4: Commit and push**

Commit with `feat: streamline ticket reviewer workflow`, verify the commit contents, fetch/rebase only if the remote advanced, and push `main` to `origin`.
