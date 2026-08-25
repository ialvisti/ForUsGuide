/**
 * The controller: DOM events in, store actions out, one render on every change.
 *
 * Design decisions worth stating, because each of them is a thing a reader would
 * otherwise reasonably want changed:
 *
 *   * **The server's refusals are shown, not avoided.** When a URL asks for an
 *     unsupported filter combination the request is sent as written and the
 *     resulting 422 is rendered. Filtering a page in the browser to make the
 *     screen agree with the URL would turn a documented refusal into a
 *     plausible-looking wrong answer.
 *   * **One ledger, one table.** A row can come only from a persisted,
 *     ticket-associated RAG invocation after DevRev scope validation and
 *     hydration succeeded. DevRev is never used to discover extra rows.
 *   * **Capability comes from the server.** The role and the feature flags in
 *     `GET /session` decide what is enabled. Nothing is inferred from an email
 *     address, and no route that the schema does not advertise is ever called.
 *   * **Text input is debounced; a select is not.** A keystroke should not spend
 *     a request against the per-subject bound, but a deliberate choice from a
 *     list should feel immediate.
 */

import * as api from "./api.js";
import * as render from "./render.js";
import { initDetail } from "./detail.js";
import { initPreferences } from "./preferences.js";
import {
  activeFilters,
  batchableSelection,
  canCurateBatches,
  canGoBack,
  createStore,
  readLocation,
  writeLocation,
} from "./state.js";

const SPRITE_URL = "/tickets/assets/icons.svg";
const TEXT_DEBOUNCE_MS = 300;
const QUEUE_REFRESH_MS = 15_000;

const store = createStore();

const dom = {
  environmentBadge: document.getElementById("environment-badge"),
  environmentValue: document.getElementById("environment-value"),
  sessionEmail: document.getElementById("session-email"),
  sessionRole: document.getElementById("session-role"),
  health: document.getElementById("health-state"),
  healthText: document.getElementById("health-text"),
  sheetToggle: document.getElementById("filter-sheet-toggle"),
  filterSheetClose: document.getElementById("filter-sheet-close"),
  filterForms: document.getElementById("filter-forms"),
  executionForm: document.getElementById("filters-executions"),
  activeFilters: document.getElementById("active-filters"),
  technicalBatch: document.getElementById("technical-batch-details"),
  bulkBar: document.getElementById("bulk-bar"),
  bulkCount: document.getElementById("bulk-count"),
  bulkClear: document.getElementById("bulk-clear"),
  bulkSelectAll: document.getElementById("bulk-select-all"),
  bulkRemediation: document.getElementById("bulk-remediation"),
  bulkRemediationHelp: document.getElementById("bulk-remediation-help"),
  caption: document.getElementById("tickets-caption"),
  selectAll: document.getElementById("select-all"),
  body: document.getElementById("tickets-body"),
  tableStatus: document.getElementById("table-status"),
  prev: document.getElementById("page-prev"),
  next: document.getElementById("page-next"),
  position: document.getElementById("page-position"),
  toasts: document.getElementById("toast-region"),
  detail: document.getElementById("ticket-detail"),
  sprite: document.getElementById("icon-sprite"),
  refresh: document.getElementById("executions-refresh"),
  themeToggle: document.getElementById("theme-toggle"),
  languageSelect: document.getElementById("language-select"),
};

let icons = new Map();
let requestSerial = 0;
let cooldownTimer = null;
let queueRefreshTimer = null;
let restoreFocusTo = null;
let filterSheetInerted = [];

/**
 * The detail view, installed once at boot.
 *
 * It is a separate controller rather than more of this file because the review
 * workspace has its own five subresource states and its own write path, and
 * because the two views share exactly three things: the store, the toast
 * surface, and the icon map. Those are handed over explicitly below.
 */
let detail = null;
let preferences = null;

/**
 * What the table body currently shows.
 *
 * The body is rebuilt only when one of these actually changes. A selection
 * toggle patches the existing rows instead, because rebuilding would destroy the
 * checkbox the reviewer just operated and take the keyboard focus with it.
 */
let rendered = { rows: null, phase: null, error: null, role: null };

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function debounce(fn, delay) {
  let handle = null;
  return (...args) => {
    if (handle !== null) {
      globalThis.clearTimeout(handle);
    }
    handle = globalThis.setTimeout(() => {
      handle = null;
      fn(...args);
    }, delay);
  };
}

function toast(fields) {
  render.pushToast(dom.toasts, fields);
  render.mountIcons(dom.toasts, icons);
}

function reportError(error) {
  toast({
    title: error.title ?? "Something went wrong",
    body: error.detail ?? "",
    tone: error.tone ?? "error",
    requestId: error.requestId ?? "",
  });
}

// ---------------------------------------------------------------------------
// Row normalization
// ---------------------------------------------------------------------------

/**
 * Flatten the persisted execution item into the one row the table renders.
 */
function normalizeExecutionRow(item) {
  const execution = item ?? {};
  return {
    executionId: execution.executionId ?? "",
    invocationId: execution.invocationId ?? execution.executionId ?? "",
    jobId: execution.jobId ?? "",
    inquiryIndex: execution.inquiryIndex ?? null,
    attempt: execution.attempt ?? null,
    leaseEpoch: execution.leaseEpoch ?? null,
    displayId: execution.displayId ?? execution.ticketId ?? "",
    title: execution.title ?? "",
    route: execution.route ?? "",
    runStatus: execution.runStatus ?? "",
    hydrationStatus: execution.hydrationStatus ?? "unavailable",
    review: execution.review ?? null,
    createdAt: execution.readyAt ?? execution.occurredAt ?? execution.createdAt ?? "",
    updatedAt: execution.readyAt ?? execution.occurredAt ?? execution.createdAt ?? "",
  };
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

async function load({ refresh = false } = {}) {
  const state = store.getState();
  const serial = (requestSerial += 1);
  store.dispatch({ type: "load/started", refresh });
  try {
    const page = await api.listExecutions({
      ...state.executionFilters,
      cursor: state.cursor,
      pageSize: state.pageSize,
    });
    if (serial !== requestSerial) {
      return;
    }
    const items = Array.isArray(page?.items) ? page.items : [];
    const rows = items.map(normalizeExecutionRow);
    store.dispatch({
      type: "load/succeeded",
      rows,
      partial: Boolean(page?.partial) || Boolean(page?.truncated),
      warnings: Array.isArray(page?.warnings) ? page.warnings : [],
      nextCursor: page?.next_cursor ?? null,
      prevCursor: page?.prev_cursor ?? null,
    });
  } catch (error) {
    if (error instanceof api.AbortedError || serial !== requestSerial) {
      return;
    }
    if (error.code === "CURSOR_REJECTED") {
      // The token was bound to an endpoint, a direction, a filter set, and a
      // subject; once any of those moves it is unusable and page one is the
      // only honest destination.
      store.dispatch({ type: "page/reset" });
      reportError(error);
      await load({ refresh: true });
      return;
    }
    if (error.code === "UNSUPPORTED_FILTER_COMBINATION") {
      store.dispatch({ type: "load/failed", error });
      reportError(error);
      return;
    }
    if (error.retryAfterS !== null && error.retryAfterS !== undefined) {
      startCooldown(error.retryAfterS);
    }
    if (error.status === 401) {
      store.dispatch({ type: "load/failed", error });
      reportError(error);
      return;
    }
    store.dispatch({ type: "load/failed", error });
    reportError(error);
  }
}

function refreshVisibleQueue() {
  const state = store.getState();
  const hasCurrentPage = state.phase === "ready";
  const canRecoverEmptyPage = state.phase === "error"
    && state.error?.recoverable === true
    && (
      state.error?.status === 0
      || state.error?.status === 429
      || state.error?.status >= 500
    );
  if (
    document.visibilityState !== "visible"
    || (!hasCurrentPage && !canRecoverEmptyPage)
    || state.selected !== ""
    || state.cursor !== null
    || api.cooldownRemainingS() > 0
  ) {
    return;
  }
  void load({ refresh: true });
}

function startQueueAutoRefresh() {
  if (queueRefreshTimer === null) {
    queueRefreshTimer = globalThis.setInterval(refreshVisibleQueue, QUEUE_REFRESH_MS);
  }
}

function stopQueueAutoRefresh() {
  if (queueRefreshTimer !== null) {
    globalThis.clearInterval(queueRefreshTimer);
    queueRefreshTimer = null;
  }
}

function handleVisibilityChange() {
  if (document.visibilityState === "visible") {
    refreshVisibleQueue();
  }
}

function handlePageShow(event) {
  if (!event.persisted) {
    return;
  }
  startQueueAutoRefresh();
  const phase = store.getState().phase;
  if (phase === "loading" || phase === "refreshing") {
    // pagehide aborts in-flight reads. A bfcache restore must supersede that
    // abandoned request instead of leaving the queue stuck in a loading phase.
    void load({ refresh: true });
    return;
  }
  refreshVisibleQueue();
}

function startCooldown(seconds) {
  store.dispatch({ type: "cooldown/set", seconds });
  if (cooldownTimer !== null) {
    return;
  }
  cooldownTimer = globalThis.setInterval(() => {
    const remaining = api.cooldownRemainingS();
    store.dispatch({ type: "cooldown/set", seconds: remaining });
    if (remaining === 0) {
      globalThis.clearInterval(cooldownTimer);
      cooldownTimer = null;
    }
  }, 1000);
}

// ---------------------------------------------------------------------------
// Filter controls
// ---------------------------------------------------------------------------

function executionFilterPatch() {
  return {
    executionId: document.getElementById("execution-id").value.trim(),
    displayId: document.getElementById("execution-ticket-id").value.trim().toUpperCase(),
    route: document.getElementById("execution-route").value,
    runStatus: document.getElementById("execution-status").value,
    reviewStatus: document.getElementById("execution-review-status").value,
  };
}

/**
 * Write a value onto a control without fighting the person using it.
 *
 * Text input is debounced, so there is always a window where the field holds
 * more than the store does. A render triggered inside that window — a list
 * request completing, say — must not push the older value back, or a reviewer's
 * keystrokes vanish mid-word. Between resets the focused control is therefore
 * the authority on its own value, and the store catches up when the debounce
 * fires.
 *
 * `force` inverts that for the cases where the state really is the authority: a
 * clear, a tab change, or a new address. Without it, clearing the filters while
 * a field still has focus would leave that field's text on screen — and, worse,
 * in the next submitted query, which the server then refuses as an unsupported
 * combination.
 */
function setFieldValue(node, value, force = false) {
  if (!force && node === document.activeElement) {
    return;
  }
  if (node.value !== value) {
    node.value = value;
  }
}

let lastFormGeneration = -1;

/**
 * Reflect the in-memory ledger filters onto their controls.
 */
function syncFilterControls(state) {
  const force = state.formGeneration !== lastFormGeneration;
  lastFormGeneration = state.formGeneration;

  const filters = state.executionFilters;
  setFieldValue(document.getElementById("execution-id"), filters.executionId, force);
  setFieldValue(document.getElementById("execution-ticket-id"), filters.displayId, force);
  setFieldValue(document.getElementById("execution-route"), filters.route, force);
  setFieldValue(document.getElementById("execution-status"), filters.runStatus, force);
  setFieldValue(document.getElementById("execution-review-status"), filters.reviewStatus, force);
}

/**
 * Which capabilities the server says are off, in the server's own terms.
 *
 * The flags exist so the interface can branch on them rather than guessing from
 * a 404, and saying which ones are off is more useful than a dimmed button with
 * no explanation.
 */
function describeDisabledCapabilities(flags) {
  const unavailable = [];
  if (flags.remediation_enabled !== true) {
    unavailable.push("remediation batches");
  }
  if (unavailable.length === 0) {
    return "";
  }
  return `The server reports these as unavailable in this build: ${unavailable.join(", ")}.`;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderAll(state) {
  document.body.classList.toggle("detail-open", state.selected !== "");
  dom.caption.textContent = "Tickets ready for evaluation, newest ready first.";

  const identity = state.session;
  dom.sessionEmail.textContent = identity?.email ?? "—";
  dom.sessionRole.textContent = identity?.role ?? "—";

  const readiness = state.readiness;
  const environment = readiness?.environment ?? "unknown";
  dom.environmentValue.textContent = environment;
  dom.environmentBadge.dataset.state = environment;
  if (readiness === null) {
    dom.health.dataset.state = "down";
    dom.healthText.textContent = "Console dependencies are not ready";
  } else if (readiness.evidence_broker === false) {
    dom.health.dataset.state = "degraded";
    dom.healthText.textContent = "Ready; evidence lookup is not configured";
  } else {
    dom.health.dataset.state = "ok";
    dom.healthText.textContent = "Ready";
  }

  render.renderChips(dom.activeFilters, activeFilters(state));
  renderTable(state);

  render.renderTableStatus(dom.tableStatus, {
    phase: state.phase,
    rows: state.rows.length,
    partial: state.partial,
    stale: state.stale,
    warnings: state.warnings,
    cooldownS: state.cooldownS,
  });

  const paused = state.cooldownS > 0;
  dom.prev.disabled = !canGoBack(state) || paused;
  dom.next.disabled = state.nextCursor === null || paused;
  dom.position.textContent = `Page ${state.pageNumber}`;
  dom.refresh.disabled = paused;

  const count = state.selectedIds.length;
  dom.bulkBar.dataset.selected = count > 0 ? "true" : "false";
  dom.bulkCount.textContent =
    count === 0
      ? "No executions selected"
      : `${count} execution${count === 1 ? "" : "s"} selected`;
  dom.bulkClear.disabled = count === 0;
  dom.bulkSelectAll.disabled = state.rows.length === 0;
  const flags = state.session?.featureFlags ?? {};
  // Three independent reasons this control can be unusable, and the help text
  // names whichever applies: the deployment has no agent configured, the role may
  // not curate, or nothing selected has a durable review to freeze.
  const role = state.session?.role ?? "viewer";
  const { refs: batchable } = batchableSelection(state.rows, state.selectedIds);
  const batchesOn = flags.remediation_enabled === true;
  dom.technicalBatch.hidden = !batchesOn || !canCurateBatches(role);
  if (dom.technicalBatch.hidden) {
    dom.technicalBatch.open = false;
  }
  dom.bulkRemediation.disabled =
    !batchesOn || !canCurateBatches(role) || batchable.length === 0 || paused;
  dom.bulkRemediationHelp.textContent = !batchesOn
    ? describeDisabledCapabilities(flags)
    : !canCurateBatches(role)
      ? "Freezing a batch needs the remediator role."
      : batchable.length === 0
        ? "Select executions with a versioned review to create a remediation batch."
        : `${batchable.length} review${batchable.length === 1 ? "" : "s"} will be frozen at the version shown.`;

  detail.render(state);

  const rowsOnPage = state.rows.map((row) => row.executionId);
  dom.selectAll.checked = rowsOnPage.length > 0 && rowsOnPage.every((id) => state.selectedIds.includes(id));
  dom.selectAll.indeterminate = !dom.selectAll.checked && state.selectedIds.length > 0;

  syncFilterControls(state);
  render.mountIcons(document.body, icons);
  syncLocation(state);
}

/**
 * Rebuild the table body, or patch the parts that changed.
 *
 * A full rebuild on a selection toggle would remove the checkbox the reviewer
 * just used, which loses keyboard focus mid-interaction.
 */
function renderTable(state) {
  const role = state.session?.role ?? "";
  const unchanged =
    state.rows === rendered.rows &&
    state.phase === rendered.phase &&
    state.error === rendered.error &&
    role === rendered.role;

  if (unchanged) {
    for (const row of Array.from(dom.body.querySelectorAll('[data-row="ticket"]'))) {
      const selected = state.selectedIds.includes(row.dataset.executionId);
      row.setAttribute("aria-selected", selected ? "true" : "false");
      const box = row.querySelector("[data-select]");
      if (box !== null) {
        box.checked = selected;
      }
    }
    return;
  }

  rendered = { rows: state.rows, phase: state.phase, error: state.error, role };

  if (state.phase === "loading") {
    render.renderSkeletons(dom.body);
    return;
  }
  if (state.phase === "error") {
    render.renderStateRow(dom.body, {
      title: state.error?.title ?? "That request failed",
      body: state.error?.detail ?? "",
      requestId: state.error?.requestId ?? "",
    });
    return;
  }
  if (state.rows.length === 0 && state.phase === "ready") {
    const hasOlderCandidates = state.nextCursor !== null;
    render.renderStateRow(dom.body, {
      title: hasOlderCandidates
        ? "No matches on this page; older executions remain"
        : "No RAG executions match these filters",
      body: hasOlderCandidates
        ? "Continue to the next page to search the remaining ready executions."
        : "Clear a filter, or verify the execution and ticket identifiers.",
    });
    return;
  }
  render.renderRows(dom.body, state.rows, {
    selectedIds: state.selectedIds,
    commentsAvailable: true,
  });
}

let lastSearch = null;
let lastSelected = null;

/**
 * True while the store is being rewritten *from* the address bar.
 *
 * Without it, going Back closes the ticket, the closed state is written to the
 * address bar as a *new* history entry, and that entry destroys the forward
 * stack — so Back works once and Forward never does. Applying a location and
 * publishing one are opposite directions of the same mapping, and only the second
 * may touch history.
 */
let applyingLocation = false;

/**
 * Keep the address bar current.
 *
 * Choosing a ticket is a navigation a reviewer expects Back to undo, so it
 * pushes. Editing a filter is not — a debounced text field would otherwise fill
 * the history stack one keystroke at a time — so it replaces.
 */
function syncLocation(state) {
  const search = writeLocation(state);
  if (search === lastSearch) {
    return;
  }
  const selectionChanged = lastSelected !== null && lastSelected !== state.selected;
  lastSearch = search;
  lastSelected = state.selected;
  if (applyingLocation) {
    // The browser already moved; the store is catching up to it.
    return;
  }
  const target = `${globalThis.location.pathname}${search}`;
  if (selectionChanged) {
    globalThis.history.pushState(null, "", target);
  } else {
    globalThis.history.replaceState(null, "", target);
  }
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

function openDetail(executionId, source) {
  detail.open(executionId, { source });
}

function closeDetail() {
  detail.close();
}

/**
 * Freeze the selected reviews into a remediation batch.
 *
 * The confirmation is not ceremony. Creating a batch pins every selected review
 * at the exact version on screen, and the server refuses the whole request if any
 * of them has since moved — so the reviewer needs to see the count *and* the
 * versions they are committing to before it goes, not a 412 afterwards.
 *
 * Rows without a versioned review are named and skipped rather than failing the
 * request: an execution stays visible even while its linked review is unavailable.
 */
async function createBatchFromSelection() {
  const state = store.getState();
  const role = state.session?.role ?? "viewer";
  if (!canCurateBatches(role)) {
    toast({
      title: "Your role does not allow this",
      body: "Freezing a remediation batch needs the remediator role.",
      tone: "error",
    });
    return;
  }
  const { refs, skipped } = batchableSelection(state.rows, state.selectedIds);
  if (refs.length === 0) {
    toast({
      title: "Nothing to freeze",
      body: "None of the selected tickets has a durable review yet.",
      tone: "warning",
    });
    return;
  }
  const spanish = document.documentElement.lang === "es";
  const listed = refs
    .slice(0, 10)
    .map((ref) => spanish
      ? `${ref.displayId} en la versión ${ref.reviewVersion}`
      : `${ref.displayId} at version ${ref.reviewVersion}`)
    .join("\n");
  const more = refs.length > 10
    ? (spanish ? `\n…y ${refs.length - 10} más` : `\n…and ${refs.length - 10} more`)
    : "";
  const note =
    skipped.length > 0
      ? (spanish
        ? `\n\n${skipped.length} ticket${skipped.length === 1 ? "" : "s"} `
          + `seleccionado${skipped.length === 1 ? "" : "s"} `
          + `${skipped.length === 1 ? "se omitirá" : "se omitirán"} por no tener revisión.`
        : `\n\n${skipped.length} selected ticket${skipped.length === 1 ? "" : "s"} `
          + "will be skipped for having no review.")
      : "";
  const proceed = globalThis.confirm(
    spanish
      ? `¿Fijar ${refs.length} revisión${refs.length === 1 ? "" : "es"} en un `
        + `lote de remediación?\n\n${listed}${more}${note}`
      : `Freeze ${refs.length} review${refs.length === 1 ? "" : "s"} into a `
        + `remediation batch?\n\n${listed}${more}${note}`
  );
  if (!proceed) {
    return;
  }
  try {
    const created = await api.createRemediationBatch(refs);
    const batchId = created?.batch?.batch_id ?? "";
    const planned = created?.planned_review_ids?.length ?? 0;
    toast({
      title: `Created remediation batch ${batchId}`,
      body:
        `${created?.batch?.item_count ?? refs.length} observation(s) frozen` +
        (planned > 0 ? `, ${planned} review(s) moved to planned` : "") +
        ". The batch is ready for agent handoff.",
      tone: "info",
    });
    store.dispatch({ type: "selection/clear" });
    await load({ refresh: true });
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    reportError(error);
  }
}

function wireFilters() {
  const submit = () => {
    store.dispatch({ type: "filters/patch", patch: executionFilterPatch() });
    load();
  };
  const debouncedSubmit = debounce(submit, TEXT_DEBOUNCE_MS);

  dom.executionForm.addEventListener("submit", (event) => event.preventDefault());

  // Typing is debounced so a keystroke does not spend a request; a deliberate
  // choice from a list is submitted at once.
  dom.executionForm.addEventListener("input", (event) => {
    if (event.target.tagName === "INPUT") {
      debouncedSubmit();
    }
  });
  dom.executionForm.addEventListener("change", (event) => {
    if (event.target.tagName === "SELECT") {
      submit();
    }
  });

  dom.refresh.addEventListener("click", () => load({ refresh: true }));

  document.getElementById("executions-clear").addEventListener("click", (event) => {
    event.preventDefault();
    store.dispatch({ type: "filters/clear" });
    load();
  });

  dom.activeFilters.addEventListener("click", (event) => {
    const control = event.target.closest("button");
    if (control === null) {
      return;
    }
    const state = store.getState();
    if (control.dataset.action === "clear-filters") {
      store.dispatch({ type: "filters/clear" });
      load();
      return;
    }
    const field = control.dataset.chipField;
    if (field === undefined) {
      return;
    }
    store.dispatch({ type: "filters/patch", patch: { [field]: "" } });
    load();
  });

  const filterSheetFocusable = () => Array.from(dom.filterForms.querySelectorAll(
    "button:not(:disabled), input:not(:disabled), select:not(:disabled), "
      + "textarea:not(:disabled), [href], [tabindex]:not([tabindex='-1'])"
  )).filter((node) => node.hidden !== true);

  const setFilterSheetBackgroundInert = (inert) => {
    if (!inert) {
      for (const node of filterSheetInerted) node.inert = false;
      filterSheetInerted = [];
      return;
    }

    let branch = dom.filterForms;
    while (branch.parentElement !== null && branch.parentElement !== document.documentElement) {
      for (const sibling of Array.from(branch.parentElement.children)) {
        if (sibling !== branch && sibling.inert !== true) {
          sibling.inert = true;
          filterSheetInerted.push(sibling);
        }
      }
      branch = branch.parentElement;
    }
  };

  const setFilterSheet = (open, { restoreFocus = true } = {}) => {
    dom.sheetToggle.setAttribute("aria-expanded", open ? "true" : "false");
    dom.filterForms.dataset.open = open ? "true" : "false";
    document.body.classList.toggle("filter-sheet-open", open);
    if (open) {
      dom.filterForms.setAttribute("role", "dialog");
      dom.filterForms.setAttribute("aria-modal", "true");
      setFilterSheetBackgroundInert(true);
      dom.filterSheetClose.focus();
      return;
    }

    dom.filterForms.removeAttribute("role");
    dom.filterForms.removeAttribute("aria-modal");
    setFilterSheetBackgroundInert(false);
    if (restoreFocus) dom.sheetToggle.focus();
  };

  dom.sheetToggle.addEventListener("click", () => {
    setFilterSheet(dom.sheetToggle.getAttribute("aria-expanded") !== "true");
  });
  dom.filterSheetClose.addEventListener("click", () => setFilterSheet(false));

  document.addEventListener("keydown", (event) => {
    if (dom.filterForms.dataset.open === "true") {
      if (event.key === "Escape") {
        event.preventDefault();
        setFilterSheet(false);
        return;
      }
      if (event.key === "Tab") {
        const focusable = filterSheetFocusable();
        const first = focusable[0];
        const last = focusable.at(-1);
        if (first === undefined || last === undefined) {
          event.preventDefault();
          dom.filterForms.focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
      return;
    }
    if (event.key !== "Escape") return;
    if (!dom.detail.hidden) {
      closeDetail();
    }
  });

  const narrowLayout = globalThis.matchMedia?.("(max-width: 768px)");
  narrowLayout?.addEventListener?.("change", (event) => {
    if (event.matches !== true && dom.filterForms.dataset.open === "true") {
      setFilterSheet(false, { restoreFocus: false });
    }
  });
}

function wireTable() {
  dom.body.addEventListener("click", (event) => {
    const box = event.target.closest("[data-select]");
    if (box !== null) {
      // A selection is not a navigation: the row click handler must not also
      // fire, or ticking a box would open a ticket.
      event.stopPropagation();
      store.dispatch({ type: "selection/toggle", id: box.dataset.select });
      return;
    }
    const control = event.target.closest("button");
    if (control !== null) {
      event.stopPropagation();
      if (control.dataset.action === "open") {
        openDetail(control.dataset.executionId, control);
      }
      return;
    }
  });

  dom.selectAll.addEventListener("change", () => {
    const state = store.getState();
    const ids = state.rows.map((row) => row.executionId);
    store.dispatch({
      type: "selection/set",
      ids: dom.selectAll.checked ? ids : [],
    });
  });

  dom.bulkClear.addEventListener("click", () => store.dispatch({ type: "selection/clear" }));
  dom.bulkSelectAll.addEventListener("click", () => {
    const state = store.getState();
    const ids = state.rows.map((row) => row.executionId);
    const all = ids.length > 0 && ids.every((id) => state.selectedIds.includes(id));
    store.dispatch({ type: "selection/set", ids: all ? [] : ids });
  });
  dom.bulkRemediation.addEventListener("click", () => {
    createBatchFromSelection();
  });

  dom.prev.addEventListener("click", () => {
    store.dispatch({ type: "page/back" });
    load();
  });
  dom.next.addEventListener("click", () => {
    store.dispatch({ type: "page/forward" });
    load();
  });

  dom.toasts.addEventListener("click", (event) => {
    const control = event.target.closest('[data-action="dismiss-toast"]');
    if (control !== null) {
      control.closest(".toast").remove();
    }
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function applyLocation() {
  const parsed = readLocation(globalThis.location.search);
  // A `/tickets/<id>` deep link is the same document; treat its path segment as
  // the selected execution. The
  // segment is a selection and never a URL to fetch: it is upper-cased, bounded
  // by the store's own reader, and only ever interpolated into a fixed path.
  const segments = globalThis.location.pathname.split("/").filter((part) => part !== "");
  const fromPath = segments.length > 1 ? decodeURIComponent(segments[1]) : "";
  store.dispatch({ type: "filters/patch", patch: parsed.executionFilters, reset: true });
  const selected = parsed.selected !== "" ? parsed.selected : fromPath;
  if (selected !== "") {
    detail.open(selected);
  } else if (store.getState().selected !== "") {
    // Back navigation out of a ticket. Closing goes through the controller so an
    // unsaved draft still gets its warning.
    detail.close();
  }
}

async function boot() {
  preferences = initPreferences({
    themeToggle: dom.themeToggle,
    languageSelect: dom.languageSelect,
  });

  let renderedPreferenceLanguage = document.documentElement.lang;
  document.addEventListener("preferenceschange", (event) => {
    render.mountIcons(document.body, icons);
    const nextLanguage = event.detail?.language ?? document.documentElement.lang;
    if (nextLanguage !== renderedPreferenceLanguage) {
      renderedPreferenceLanguage = nextLanguage;
      // Rebuild generated audit labels and count formatting from the original
      // state. Restoring translated text alone would preserve the previous
      // locale's number separators in dynamically created summaries.
      detail?.render(store.getState());
    }
  });

  // Installed before the first render, because `renderAll` draws the detail
  // region through it.
  detail = initDetail({
    store,
    toast,
    reportError,
    icons: () => icons,
    rememberFocus(source) {
      restoreFocusTo = source ?? null;
    },
    restoreFocus() {
      if (restoreFocusTo !== null && restoreFocusTo.isConnected) {
        restoreFocusTo.focus();
      }
      restoreFocusTo = null;
    },
  });

  store.subscribe(renderAll);
  wireFilters();
  wireTable();

  try {
    const response = await fetch(SPRITE_URL, { credentials: "same-origin", cache: "no-store" });
    if (response.ok) {
      icons = render.parseSprite(await response.text());
      render.mountIcons(document.body, icons);
    }
  } catch {
    // Icons are decorative and every control is named in text, so a missing
    // sprite degrades the look and nothing else.
  }

  applyLocation();

  const [readiness, sessionResult] = await Promise.allSettled([
    api.readiness(),
    api.loadSession(),
  ]);
  store.dispatch({
    type: "readiness/loaded",
    readiness: readiness.status === "fulfilled" ? readiness.value : null,
  });
  if (sessionResult.status === "fulfilled") {
    store.dispatch({ type: "session/loaded", session: sessionResult.value });
  } else {
    reportError(sessionResult.reason);
  }

  globalThis.addEventListener("popstate", () => {
    lastSearch = null;
    applyingLocation = true;
    try {
      applyLocation();
    } finally {
      applyingLocation = false;
    }
    load();
  });
  document.addEventListener("visibilitychange", handleVisibilityChange);
  globalThis.addEventListener("pageshow", handlePageShow);
  startQueueAutoRefresh();
  globalThis.addEventListener("pagehide", () => {
    stopQueueAutoRefresh();
    api.abortAll();
  });

  await load();
}

boot();
