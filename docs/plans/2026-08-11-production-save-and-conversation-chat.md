# Production Save and Conversation Chat Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix self-assignment saves rejected in production and replace the noisy ticket timeline with a safe, read-only chat that shows participant, N8N AI, and human-agent messages correctly.

**Architecture:** Preserve the verified IAP subject at the browser network boundary so self-assignment produces a valid `ReviewerIdentity`. Keep author classification server-side and identity-based, expand the documented DevRev comment-body allowlist to include `snap_widget`, and make the browser's default transcript contain comments rather than ticket change events. Render comments as semantic chat rows while keeping technical events available through their explicit filter.

**Tech Stack:** FastAPI, Pydantic v2, vanilla ES modules, CSS, pytest, Node-based DOM contract tests, Cloud Run, DevRev Timeline API.

---

### Task 1: Reproduce the production save rejection in the browser pipeline

**Files:**
- Create: `kb-rag-system/tests/test_ticket_save_browser_contract.py`
- Modify: `kb-rag-system/ui/tickets/assets/api.js`

**Step 1: Write the failing test**

Create a Node-backed pytest that stubs `GET /api/admin/v1/session` with a complete `identity`, calls the shipped `api.loadSession()`, passes that result to `evaluation.buildSave()` with `assigned_reviewer: "self"`, and asserts:

```python
assert result["session_subject"] == "accounts.google.com:synthetic-reviewer"
assert result["assigned_reviewer"] == {
    "subject": "accounts.google.com:synthetic-reviewer",
    "email": "reviewer@example.invalid",
    "display_name": "Synthetic Reviewer",
}
```

**Step 2: Run the test to verify it fails**

Run: `pytest -q tests/test_ticket_save_browser_contract.py`

Expected: FAIL because `loadSession()` currently discards `identity.subject` and `buildSave()` emits an empty subject.

**Step 3: Write the minimal implementation**

Add this field to the session normalization in `api.js`:

```javascript
subject: body?.identity?.subject ?? "",
```

**Step 4: Run the test to verify it passes**

Run: `pytest -q tests/test_ticket_save_browser_contract.py`

Expected: PASS.

### Task 2: Preserve documented DevRev `snap_widget` message bodies

**Files:**
- Create: `kb-rag-system/tests/fixtures/devrev/timeline_page_chat.json`
- Modify: `kb-rag-system/tests/test_devrev_client.py`
- Modify: `kb-rag-system/tests/test_ticket_review_service.py`
- Modify: `kb-rag-system/api/ticket_review_models.py`

**Step 1: Add a sanitized production-shaped fixture**

Represent only synthetic content with these four shapes:

```json
[
  {"type":"timeline_comment","visibility":"external","body_type":"snap_widget","created_by":{"type":"rev_user"}},
  {"type":"timeline_comment","visibility":"internal","body_type":"text","created_by":{"type":"dev_user","display_name":"N8N Workflow"}},
  {"type":"timeline_comment","visibility":"external","body_type":"snap_widget","created_by":{"type":"dev_user"}},
  {"type":"timeline_change_event","visibility":"internal"}
]
```

**Step 2: Write failing adapter and service tests**

Assert that the adapter preserves the `snap_widget` body, paragraphs, author, and visibility, then assert that service normalization returns:

```python
assert message.rendering is MessageRendering.TEXT
assert message.body == "Synthetic participant question.\n\nSecond paragraph."
assert message.body_type == "snap_widget"
```

Keep a separate hostile `text/html` case as a placeholder, proving the change is an allowlist expansion rather than rendering arbitrary markup.

**Step 3: Run the tests to verify the service test fails**

Run: `pytest -q tests/test_devrev_client.py tests/test_ticket_review_service.py`

Expected: adapter test PASS; service test FAIL with `MessageRendering.PLACEHOLDER` for `snap_widget`.

**Step 4: Write the minimal implementation**

Expand the model's renderable text body types to the documented DevRev comment types (`data`, `snap_kit`, `snap_widget`, `text`) while retaining existing plain-text aliases and keeping HTML/unknown types blocked.

**Step 5: Run the tests to verify they pass**

Run: `pytest -q tests/test_devrev_client.py tests/test_ticket_review_service.py`

Expected: PASS.

### Task 3: Make comments, not ticket events, the default transcript

**Files:**
- Modify: `kb-rag-system/tests/test_ticket_structured_presenter_browser_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`
- Modify: `kb-rag-system/ui/tickets/assets/state.js`
- Modify: `kb-rag-system/ui/tickets/index.html`

**Step 1: Write failing filter tests**

Execute the shipped `filterConversation()` with five comments and thirteen change events. Assert the new `messages` filter returns the five comments, while `event` still returns all thirteen events. Assert the checked HTML radio is `messages` and no message composer (`textarea`, message input, or send button) exists.

**Step 2: Run the tests to verify they fail**

Run: `pytest -q tests/test_ticket_structured_presenter_browser_contract.py tests/test_ticket_detail_ui_contract.py`

Expected: FAIL because the store and HTML currently default to `all`.

**Step 3: Write the minimal implementation**

Add `messages` to `CONVERSATION_FILTERS`, make it the initial detail filter, have it select non-event comments, and replace the checked `All` option with `Messages`. Keep the `Events` filter as the explicit route to technical activity.

**Step 4: Run the tests to verify they pass**

Run the same command. Expected: PASS.

### Task 4: Render a safe, read-only chat UI

**Files:**
- Modify: `kb-rag-system/tests/test_ticket_structured_presenter_browser_contract.py`
- Modify: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`
- Modify: `kb-rag-system/ui/tickets/assets/conversation.js`
- Modify: `kb-rag-system/ui/tickets/assets/tickets.css`
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`

**Step 1: Write failing behavior and contract tests**

Assert all of the following against shipped modules and DOM output:

```text
participant comment -> outgoing/right chat row
AI comment -> incoming AI chat row
human-agent comment -> incoming agent chat row
event -> compact centered activity row
author, role, timestamp, and body remain visible
ordinary messages do not expose upstream entry IDs
structured N8N JSON remains text-safe and disclosure-accessible
long prose keeps an aria-expanded control
the panel states that it is read-only and has no composer
```

**Step 2: Run the tests to verify they fail**

Run: `pytest -q tests/test_ticket_structured_presenter_browser_contract.py tests/test_ticket_detail_ui_contract.py tests/test_ticket_runtime_copy_i18n_contract.py`

Expected: FAIL because entries are currently full-width audit cards and the new copy/classes do not exist.

**Step 3: Implement the minimal semantic structure**

In `conversation.js`, retain `textContent`-only rendering and server-provided `actor_class`, but create an avatar/identity column and bubble column. Use explicit text role labels and one audience status, avoiding the current stack of redundant pills. Keep events in a separate compact branch.

**Step 4: Implement responsive styles and translations**

Add bounded bubble widths, participant/right and responder/left alignment, distinct non-color role labels, dark-mode-compatible surfaces, mobile full-width bubbles, and `prefers-reduced-motion` safety. Add English/Spanish strings for `Messages`, `Read-only conversation`, and the role/audience labels.

**Step 5: Run the focused tests**

Run the command from Step 2. Expected: PASS.

### Task 5: Verify the whole change and visually inspect the real UI

**Files:**
- Verify only; no new production files expected.

**Step 1: Run syntax and focused quality gates**

Run:

```bash
find ui/tickets/assets -type f -name '*.js' -exec node --check '{}' \;
pytest -q \
  tests/test_ticket_save_browser_contract.py \
  tests/test_devrev_client.py \
  tests/test_ticket_review_service.py \
  tests/test_ticket_structured_presenter_browser_contract.py \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_runtime_copy_i18n_contract.py
ruff check api/ticket_review_models.py tests/test_ticket_review_service.py tests/test_devrev_client.py
```

Expected: all commands exit 0.

**Step 2: Run the complete non-live test suite**

Run: `pytest -q -rs -m "not live_dependencies and not staging_e2e"`

Expected: 0 failures.

**Step 3: Start the local fixture app and inspect in a browser**

Open the fixture ticket detail at desktop and mobile widths. Confirm the transcript reads chronologically, the five actor states are distinguishable without color, no composer exists, long/structured messages remain usable, and focus/keyboard behavior is intact.

**Step 4: Apply the current Web Interface Guidelines**

Fetch the current guideline source and audit `conversation.js`, `index.html`, and the relevant `tickets.css` rules. Resolve any actionable accessibility or interaction findings, then rerun the focused tests.

### Task 6: Promote safely and correct production author identity configuration

**Files:**
- External deployment configuration for `rag-tickets-console-prod`; do not hardcode organization-specific DONs in the repository.

**Step 1: Build the verified source at an immutable digest**

Use the repository's Cloud Build gates and record the resulting Artifact Registry digest. Do not deploy from an unverified mutable tag.

**Step 2: Correct identity sets during promotion**

Move the observed `N8N Workflow` DevRev identity from `TICKETS_DEVREV_HUMAN_AUTHOR_IDS` into `TICKETS_DEVREV_AI_AUTHOR_IDS`. Keep a separately observed human-agent identity in the human set and preserve the sets' disjointness.

**Step 3: Deploy a new Cloud Run revision with rollback available**

Promote the immutable digest and configuration to `rag-tickets-console-prod`, preserving the previous revision for immediate rollback.

**Step 4: Run production smoke checks**

Verify read-only endpoints and one bounded target timeline page. Confirm:

```text
snap_widget participant body -> rendering=text
N8N Workflow -> actor_class=ai_or_system
real agent -> actor_class=human_agent
default browser transcript -> comments only
self-assignment save -> HTTP 200 and version increments exactly once
```

Avoid modifying the user's existing review during smoke verification; use a controlled review record or a reversible assignment change.
