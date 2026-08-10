# Tickets Visual Refresh and Localization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the `/tickets` review console with an Archimedea-inspired editorial SaaS aesthetic, explicit light/dark controls, English/Spanish localization, smoother motion, and a calmer focused-detail workflow.

**Architecture:** Keep the existing CSP-safe, dependency-free FastAPI static application and its stable DOM/API contracts. Add one same-origin preferences module that owns in-memory theme and locale state, translates only known interface copy, and observes newly rendered UI nodes without persisting ticket data. Reshape the static shell and layer a new token-driven visual system over the existing functional selectors while preserving accessibility, responsive cards, and secure text-only rendering.

**Tech Stack:** FastAPI static assets, semantic HTML, modern CSS, browser-native ES modules, pytest contract tests, loopback fixture server, in-app browser verification.

---

### Task 1: Pin the new preferences and focused-workspace contract

**Files:**
- Modify: `kb-rag-system/tests/test_tickets_ui_contract.py`

**Step 1: Write the failing tests**

Add contract coverage that requires:

```python
def test_theme_and_language_controls_are_labeled(dom):
    ids = {node.get("id") for node in dom.walk() if node.get("id")}
    assert {"theme-toggle", "language-select"} <= ids

def test_preferences_are_reached_from_the_entry_module(scripts):
    assert 'from "./preferences.js"' in scripts["app.js"]

def test_explicit_themes_override_the_system_scheme(css_source):
    assert 'html[data-theme="light"]' in css_source
    assert 'html[data-theme="dark"]' in css_source

def test_spanish_is_a_complete_selectable_locale(scripts):
    preferences = scripts["preferences.js"]
    assert '"es"' in preferences
    assert "document.documentElement.lang" in preferences

def test_detail_mode_focuses_the_workspace(scripts, css_source):
    assert 'classList.toggle("detail-open"' in scripts["app.js"]
    assert "body.detail-open" in css_source
```

**Step 2: Run tests to verify RED**

Run: `../.venv-local/bin/python -m pytest tests/test_tickets_ui_contract.py -q`

Expected: FAIL because the controls, preferences module, explicit themes, and focused-detail state do not exist yet.

### Task 2: Add theme and locale behavior

**Files:**
- Create: `kb-rag-system/ui/tickets/assets/preferences.js`
- Modify: `kb-rag-system/ui/tickets/assets/app.js`
- Modify: `kb-rag-system/ui/tickets/assets/icons.svg`

**Step 1: Implement the minimal preferences controller**

Create a same-origin module that:

```javascript
export function initPreferences({ themeToggle, languageSelect }) {
  // Resolve the initial theme from prefers-color-scheme.
  // Keep locale and theme in memory only.
  // Update html[data-theme], html[lang], labels, and known UI copy.
  // Observe text-only UI nodes added by the existing renderers.
}
```

The translator must use an explicit English-to-Spanish dictionary and bounded template patterns. It must never translate arbitrary ticket, comment, email, or API content and must never use browser persistence.

**Step 2: Wire preferences before the first application render**

Import the module from `app.js`, initialize it from the two static controls, and notify it after normal renders. Toggle `body.detail-open` from the store's selected execution so list and detail modes are visually distinct without changing routing.

**Step 3: Add local theme icons**

Extend the existing safe SVG symbol sprite with sun, moon, language, spark, and user symbols. Keep symbols stroke-only, same-origin, and free of executable or foreign content.

**Step 4: Run tests to verify GREEN for behavior contracts**

Run: `../.venv-local/bin/python -m pytest tests/test_tickets_ui_contract.py -q`

Expected: PASS.

### Task 3: Reshape the semantic shell

**Files:**
- Modify: `kb-rag-system/ui/tickets/index.html`

**Step 1: Build the new application header**

Preserve session, environment, health, and all existing stable ids. Group them into a calmer product header with a compact brand lockup, status cluster, labeled locale selector, and accessible theme button.

**Step 2: Replace the plain page heading with an editorial overview**

Add a concise eyebrow, large outcome-led heading, supporting copy, and a small decorative ticket-to-RAG-to-review signal map inspired by the reference's modular workflow storytelling. Do not copy Archimedea branding or artwork.

**Step 3: Reduce visible queue copy and improve grouping**

Keep required contract text and headings while moving secondary explanations into quieter notes. Group filters, bulk actions, results, and pagination inside one clearly bounded workspace card.

**Step 4: Keep the detail workspace structurally intact**

Preserve every id, label, form control, live region, tab, and heading level required by the existing review workflow and tests.

**Step 5: Run the HTML/detail contract tests**

Run: `../.venv-local/bin/python -m pytest tests/test_tickets_ui_contract.py tests/test_ticket_detail_ui_contract.py -q`

Expected: PASS.

### Task 4: Implement the visual system and motion

**Files:**
- Modify: `kb-rag-system/ui/tickets/assets/tickets.css`

**Step 1: Replace the core design tokens**

Use warm paper surfaces, midnight navy, blueprint blue, restrained cyan/green/amber signals, larger radii, softer shadows, and a compact system type scale. Define explicit light and dark token overrides while retaining `prefers-color-scheme: dark` as the no-script fallback.

**Step 2: Style the header and overview**

Create the reference-inspired thin announcement rail, modular navigation bar, editorial hero, technical grid texture, and signal-map card using CSS only.

**Step 3: Restyle the review queue**

Make filters calmer, KPIs more scannable, the table shell clearer, rows interactive without relying on color, and bulk controls visually subordinate until active. Preserve sticky headers and contained horizontal scrolling.

**Step 4: Focus ticket detail**

When `body.detail-open` is set, hide overview/queue regions and present the detail as a dedicated workspace with clearer information hierarchy, sticky navigation, readable cards, and less visual noise.

**Step 5: Add smooth, respectful motion**

Add short entrance, hover, state, drawer, toast, and skeleton transitions. Disable nonessential transitions and animations inside `@media (prefers-reduced-motion: reduce)`.

**Step 6: Preserve responsive behavior**

Retain the canonical 768px breakpoint, 44px touch targets, mobile filter sheet, labeled row cards, focus rings, non-color status cues, and narrow detail reflow.

### Task 5: Verify the live experience

**Files:**
- No production file changes unless verification reveals a defect.

**Step 1: Run focused tests**

Run: `../.venv-local/bin/python -m pytest tests/test_tickets_ui_contract.py tests/test_ticket_detail_ui_contract.py tests/test_tickets_fixture_app.py tests/test_tickets_fixture_server.py -q`

Expected: PASS with zero failures.

**Step 2: Run the broader console test slice**

Run: `../.venv-local/bin/python -m pytest tests/test_tickets_console_app.py tests/test_tickets_csrf.py -q`

Expected: PASS with zero failures.

**Step 3: Inspect desktop light and dark modes in the live fixture**

Reload `http://127.0.0.1:8010/tickets`, test theme switching, verify readable contrast and no console errors, and capture a screenshot of each mode.

**Step 4: Inspect English and Spanish**

Switch locales in place, verify the document language changes, controls remain labeled, dynamic counts/statuses translate, and ticket/customer content remains untouched.

**Step 5: Inspect the focused detail workflow**

Open a ticket, verify list chrome leaves the visual flow, tabs/forms remain functional, then return with Back and confirm focus/navigation state.

**Step 6: Inspect mobile and reduced-motion behavior**

Use a 390px viewport, verify the filter sheet and ticket cards, then emulate or inspect reduced-motion CSS and confirm motion is suppressed.

**Step 7: Run final verification**

Run the focused test suite again after the last visual adjustment, inspect browser logs, and review `git diff --check` plus the exact scoped diff before reporting completion.

No commit is created automatically because the shared worktree already contains unrelated user changes; commit/staging remains an explicit handoff choice.
