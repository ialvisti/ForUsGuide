# Readable Technical Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace raw JSON and flat technical dumps in `/tickets` with a guided, semantic audit view that remains complete, safe, bilingual, responsive, and useful to both technical and non-technical reviewers.

**Architecture:** Add a reusable DOM-only structured-data presenter to the existing rendering boundary, then use it for the generated answer, execution diagnostics, metadata, and structured conversation messages. Keep the technical audit collapsed by default, reveal long groups progressively with native disclosures, preserve every authorized leaf with a stable field path and raw key, and retain the existing server-side privacy boundaries for secrets, internal identifiers, full chunks, and provider reasoning.

**Tech Stack:** Browser-native ES modules and DOM APIs, semantic HTML, CSS custom properties, Python/pytest static UI contracts, Node syntax checks, the local FastAPI fixture server, and authenticated in-app browser verification.

---

### Task 1: Lock the safe structured-data contract in failing tests

**Files:**
- Modify: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Modify: `kb-rag-system/tests/support/tickets_console_fixture_app.py`

**Step 1: Write the failing tests**

Add contracts that require:

- a shared `parseStructuredText`, `renderStructuredData`, and `renderGeneratedAnswer` boundary;
- strict parsing of complete JSON documents and complete fenced JSON blocks, including safe tokenization when a complete fence is embedded between prose blocks;
- recursive `<dl>`, `<ul>`, `<ol>`, and `<details>` output built through `createElement`/`textContent`;
- `data-field-path` on every authorized leaf, including `0`, `false`, `null`, unknown keys, and hostile-looking text;
- raw technical keys and recorded values marked `translate="no"`/`data-audit-value`;
- no `JSON.stringify` display path in `detail.js` and no unsafe HTML sink anywhere;
- grouped audit markup while preserving all existing public IDs;
- representative fixture data with `opening`, `key_points`, `steps`, `warnings`, nested diagnostics, empty objects, booleans, and arrays.
- behavioral execution of the real browser modules with schema drift, deeply nested records, large arrays, mixed prose/JSON, hostile-looking values, and non-sequential recorded step numbers.

**Step 2: Run the focused contracts and verify RED**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_tickets_ui_contract.py -q
```

Expected: the new presenter, semantic markup, and fixture assertions fail while the pre-existing contracts remain green.

**Step 3: Commit only after the corresponding implementation is green**

Do not commit a deliberately red tree; keep the test diff local through Tasks 2–6.

### Task 2: Build the safe semantic presenter

**Files:**
- Create: `kb-rag-system/ui/tickets/assets/structured.js`
- Modify: `kb-rag-system/ui/tickets/assets/render.js`
- Test: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`

**Step 1: Implement strict structured-text parsing**

Implement `parseStructuredText(value)` so objects and arrays pass through and a string is parsed only when the entire trimmed value is a JSON object/array or one complete fenced JSON block. Add a tokenizer that recognizes only complete, valid JSON fences embedded in prose, preserving every surrounding character as text and leaving malformed fences literal. Never use `eval`, `Function`, `innerHTML`, or remote markup.

**Step 2: Implement lossless recursive presentation**

Implement `renderStructuredData(value, options)` with:

- human labels for known keys and a generic snake/camel/kebab-case fallback;
- the exact raw key retained in a secondary `<code translate="no">` label;
- scalar rows in `<dl>`, scalar arrays in `<ul>`, ordered procedure steps in `<ol>`, and nested groups in native `<details>`;
- localized booleans, null/empty states, dates, and numbers;
- `data-field-path` and `data-audit-value` on every leaf;
- bounded parsing and initial DOM materialization;
- lazy/chunked native disclosures for large or deep values so every leaf remains reachable without slicing, silent truncation, runaway recursion, or tens of thousands of eager DOM nodes.

**Step 3: Implement the guided generated-answer view**

Render `opening` as a lead, `key_points` as a list, `steps` as an ordered procedure with action/detail, and `warnings` as an explicit caution section. Mark a guided field consumed only after it has been rendered losslessly; schema-drifted known fields and every unrecognized field must go through the generic presenter. Preserve response-wrapper siblings and display the recorded `step_number` rather than replacing it with the array index.

**Step 4: Run the presenter contracts**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest tests/test_ticket_detail_ui_contract.py -q
node --check ui/tickets/assets/structured.js
node --check ui/tickets/assets/render.js
```

Expected: presenter and security contracts pass.

### Task 3: Restructure the technical audit for progressive reading

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`
- Modify: `kb-rag-system/ui/tickets/assets/detail.js`
- Test: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`

**Step 1: Group the audit without breaking stable IDs**

Keep `#technical-audit` closed by default. Inside it, create the following operational sections:

1. Answer and decision (open initially).
2. Evidence used.
3. Data coverage and diagnostics.
4. Runtime and exact identifiers.
5. Ticket snapshot at execution.

Preserve existing container IDs used by controllers and tests. Move the conversation/evidence/history workspace before deep diagnostics so a disclosure cannot push normal ticket work tens of thousands of pixels down the page.

Keep Reload beside the detail status and place Add to remediation batch in the Remediation workflow; do not hide either action inside exact identifiers.

**Step 2: Replace flat string serialization**

In `renderRun()`, prefer the already-structured response when it carries the participant response, parse compatibility JSON strings only through the strict presenter, localize timestamps with `<time>`, and render diagnostics/model/timing/retrieval through the semantic presenter. Empty objects must say “Not recorded,” not `{}`.

**Step 3: Surface safe execution dimensions currently dropped**

Present the authorized query, topic, classification confidence/metadata, correlation ID, event digest, hydration status/error, and execution error when present. Label machine IDs as technical identifiers, not as human owners.

In the canonical evidence workspace, present persisted run evidence and broker correlation/candidate evidence as separate subsections. Deduplicate exact records only, never suppress an entire evidence channel, and remove duplicate source/chunk presentations elsewhere in the audit.

**Step 4: Run focused contracts**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest tests/test_ticket_detail_ui_contract.py -q
node --check ui/tickets/assets/detail.js
```

Expected: all detail contracts pass and `detail.js` contains no JSON pretty-printer for visible output.

### Task 4: Render structured conversation messages without exposing markup

**Files:**
- Modify: `kb-rag-system/ui/tickets/assets/conversation.js`
- Modify if required for safe upstream normalization: `kb-rag-system/data_pipeline/ticket_review_service.py`
- Test: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`
- Test if server normalization changes: `kb-rag-system/tests/test_ticket_review_service.py`

**Step 1: Add failing cases for complete JSON, fenced JSON, plain text, malformed JSON, and hostile text**

Require structured messages to use the shared presenter while ordinary prose continues through safe paragraphs. Preserve actor and visibility badges exactly as the server classified them.

**Step 2: Render recognized structured text semantically**

Use the structured tokenizer before `paragraphs()`. A complete structured document or a complete valid fenced block inside prose gets a guided/lossless view; surrounding prose remains text and invalid fences remain literal. Structured messages rely on their own disclosures and are never line-clamped with focusable controls hidden. Do not interpret arbitrary Markdown or HTML.

**Step 3: Preserve the bounded server privacy contract**

If an upstream body type cannot be safely normalized from the data already available, keep an explicit placeholder and exact reason rather than forwarding raw HTML or unbounded payloads. “No omission” applies to authorized safe fields, not to protected content.

**Step 4: Run focused tests**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_review_service.py -q
node --check ui/tickets/assets/conversation.js
```

Expected: structured conversation bodies are readable, plain text behavior is unchanged, and security boundaries remain green.

### Task 5: Apply an operational visual hierarchy and responsive behavior

**Files:**
- Modify: `kb-rag-system/ui/tickets/assets/tickets.css`
- Test: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`
- Test: `kb-rag-system/tests/test_tickets_ui_contract.py`

**Step 1: Add audit-specific layout styles**

Add styles for `.audit-section`, `.audit-section-summary`, `.audit-section-body`, `.structured-answer`, `.answer-key-points`, `.answer-steps`, `.answer-warnings`, `.structured-group`, `.structured-list`, `.data-row`, `.data-label`, `.data-value`, `.identifier-value`, and neutral/warning callouts. Use spacing and typographic hierarchy instead of decorative KPI cards.

**Step 2: Make progressive disclosures accessible**

Provide visible `:focus-visible`, sufficiently large summary targets, textual state cues, stable open/closed layout, and `prefers-reduced-motion`. Do not use `transition: all` or imply clickability on non-interactive records.

**Step 3: Validate small screens and themes**

At 360–390 px, prevent horizontal overflow and long-ID overlap; keep prose at least 16 px and use `overflow-wrap: anywhere` only where needed. Validate explicit light default and dark token contrast.

**Step 4: Run CSS contracts**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_tickets_ui_contract.py -q
```

Expected: layout, accessibility, reduced-motion, and theme contracts pass.

### Task 6: Complete bilingual labels while protecting recorded values

**Files:**
- Modify: `kb-rag-system/ui/tickets/assets/preferences.js`
- Test: `kb-rag-system/tests/test_tickets_ui_contract.py`
- Test: `kb-rag-system/tests/test_ticket_detail_ui_contract.py`

**Step 1: Add English-to-Spanish labels**

Translate every new navigation label, section heading, empty state, structured key label, boolean/null state, warning, and disclosure summary. Include dynamic history/evidence phrases that remain visible in the same panel.

**Step 2: Protect source values from translation**

Extend protected selectors to include `[data-audit-value]`, raw keys, IDs, hashes, participant content, and all remote diagnostic leaves. Translate presentation labels only.

**Step 3: Run locale and syntax checks**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest \
  tests/test_tickets_ui_contract.py \
  tests/test_ticket_detail_ui_contract.py -q
node --check ui/tickets/assets/preferences.js
```

Expected: locale contracts pass and switching EN/ES does not alter recorded values.

### Task 7: Verify the representative fixture visually and functionally

**Files:**
- Modify: `kb-rag-system/tests/support/tickets_console_fixture_app.py`
- Test: `kb-rag-system/tests/test_ticket_detail_browser_contract.py`

**Step 1: Start the managed fixture server**

Run:

```bash
cd kb-rag-system
fixture_state_dir="$(mktemp -d -t tickets-fixture-state.XXXXXX)"
chmod 0700 "$fixture_state_dir"
../.venv-local/bin/python -m tests.support.tickets_fixture_server start \
  --state-dir "$fixture_state_dir" --host 127.0.0.1 --port 8010 --max-seconds 1800
```

Record the exact state directory and stop only through the matching fixture runner command.

**Step 2: Inspect the real rendering in the in-app browser**

Verify 1440×900, 768×1024, and 360×800 in light/dark and EN/ES. Confirm:

- no visible `"key_points":`, `"field_mapping":`, fenced JSON, or `{}` placeholder;
- every expected opening/key point/step/warning and nested diagnostic leaf remains accessible;
- initial audit content is concise and deeper groups are keyboard-operable disclosures;
- no overflow, overlapping identifiers, hidden focus, or untranslated presentation label.

**Step 3: Run the complete targeted suite**

Run:

```bash
cd kb-rag-system
../.venv-local/bin/pytest \
  tests/test_tickets_ui_contract.py \
  tests/test_ticket_detail_ui_contract.py \
  tests/test_ticket_detail_browser_contract.py -q
for file in ui/tickets/assets/*.js; do node --check "$file"; done
git diff --check
```

Expected: all tests and syntax checks pass with a clean diff check.

**Step 4: Stop the fixture safely**

Run:

```bash
../.venv-local/bin/python -m tests.support.tickets_fixture_server stop \
  --state-dir "$fixture_state_dir"
```

Expected: only the server owned by that state directory stops.

### Task 8: Commit, push, build, and promote the verified artifact

**Files:**
- Stage only the ticket UI, relevant tests/fixture, and this plan.
- Preserve unrelated changes in `PA/DevRev/SYSTEM_PROMPT.md` and `OVERVIEW_PARA_INFOGRAFIA.md`.

**Step 1: Review and commit scoped changes**

Run explicit-path `git add`, inspect `git diff --cached --name-only`, then commit with a focused message such as:

```bash
git commit -m "feat(tickets): make technical audit readable"
```

**Step 2: Push main and verify the immutable build**

Push `main`, wait for the exact source-SHA build, require tests/security scan/provenance success, and record the produced immutable image digest.

**Step 3: Create and inspect an exact Terraform plan**

Use the versioned production backend and current workload inputs. Require a baseline no-op plan with the deployed digest, then a saved candidate plan containing only three in-place Cloud Run image updates and zero create/delete/replace/IAM/secret changes.

**Step 4: Apply only the reviewed saved plan**

Promote the exact digest through Terraform so console, ingest, and broker remain state-consistent. Verify new ready revisions, resolved digest, 100% traffic, health, audit logs, state serial, and absence of drift.

**Step 5: Verify the selected production ticket**

In the authenticated in-app browser, reopen the supplied ticket and confirm the real answer, diagnostics, retrieval data, and structured conversation are guided and complete in both languages/themes. Confirm IAP, reviewer workflow, and durable review behavior remain unchanged.
