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
 *   * **Two tabs, one panel, one table.** The panel's `aria-labelledby` follows
 *     the selected tab and its contents are rebuilt, so an eleven-column table,
 *     a bulk bar, and a pagination control exist once each.
 *   * **Capability comes from the server.** The role and the feature flags in
 *     `GET /session` decide what is enabled. Nothing is inferred from an email
 *     address, and no route that the schema does not advertise is ever called.
 *   * **Text input is debounced; a select is not.** A keystroke should not spend
 *     a request against the per-subject bound, but a deliberate choice from a
 *     list should feel immediate.
 */

import * as api from "./api.js";
import * as render from "./render.js";
import {
  REVIEW_FACETS,
  REVIEW_STATUSES,
  activeFilters,
  canGoBack,
  createStore,
  pageTallies,
  readLocation,
  writeLocation,
} from "./state.js";

const SPRITE_URL = "/tickets/assets/icons.svg";
const TEXT_DEBOUNCE_MS = 300;

/** Facets whose values are free text rather than a closed vocabulary. */
const FREE_TEXT_FACETS = new Set(["topic", "assigned_reviewer.email", "remediation_target"]);

const store = createStore();

const dom = {
  environmentBadge: document.getElementById("environment-badge"),
  environmentValue: document.getElementById("environment-value"),
  sessionEmail: document.getElementById("session-email"),
  sessionRole: document.getElementById("session-role"),
  health: document.getElementById("health-state"),
  healthText: document.getElementById("health-text"),
  kpi: {
    unreviewed: document.getElementById("kpi-unreviewed-scope"),
    lowRating: document.getElementById("kpi-low-rating-scope"),
    highSeverity: document.getElementById("kpi-severity-scope"),
    remediating: document.getElementById("kpi-remediation-scope"),
  },
  tabs: Array.from(document.querySelectorAll('[role="tab"]')),
  panel: document.getElementById("tickets-panel"),
  sheetToggle: document.getElementById("filter-sheet-toggle"),
  filterForms: document.getElementById("filter-forms"),
  devrevForm: document.getElementById("filters-devrev"),
  reviewsForm: document.getElementById("filters-reviews"),
  statusChecks: document.getElementById("reviews-statuses"),
  facet: document.getElementById("reviews-facet"),
  facetValue: document.getElementById("reviews-facet-value"),
  facetHelp: document.getElementById("reviews-facet-help"),
  activeFilters: document.getElementById("active-filters"),
  bulkBar: document.getElementById("bulk-bar"),
  bulkCount: document.getElementById("bulk-count"),
  bulkImport: document.getElementById("bulk-import"),
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
  detailTarget: document.getElementById("detail-target"),
  detailClose: document.getElementById("detail-close"),
  sprite: document.getElementById("icon-sprite"),
  refresh: {
    devrev: document.getElementById("devrev-refresh"),
    reviews: document.getElementById("reviews-refresh"),
  },
};

let icons = new Map();
let requestSerial = 0;
let cooldownTimer = null;
let restoreFocusTo = null;

/**
 * What the table body currently shows.
 *
 * The body is rebuilt only when one of these actually changes. A selection
 * toggle patches the existing rows instead, because rebuilding would destroy the
 * checkbox the reviewer just operated and take the keyboard focus with it.
 */
let rendered = { rows: null, phase: null, mode: null, error: null, role: null };

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

/** Whether the caller may create a durable review. */
function canCreateReview() {
  const role = store.getState().session?.role ?? "viewer";
  return role === "reviewer" || role === "remediator" || role === "admin";
}

// ---------------------------------------------------------------------------
// Row normalization
// ---------------------------------------------------------------------------

/**
 * Flatten either wire shape into the one row the table renders.
 *
 * The live list returns `{ticket, review}` with a bounded review *summary*, and
 * the queue returns the durable review itself. Merging them in the renderer
 * instead would put two wire contracts into the one place that must not know
 * about either.
 */
function normalizeLiveRow(item) {
  const ticket = item?.ticket ?? {};
  const review = item?.review ?? null;
  return {
    displayId: ticket.devrev_display_id ?? "",
    title: ticket.title ?? "",
    review,
    updatedAt: review?.updated_at ?? ticket.modified_at ?? ticket.created_at ?? "",
  };
}

function normalizeReviewRow(review) {
  return {
    displayId: review?.devrev_display_id ?? "",
    title: "",
    review: review ?? null,
    updatedAt: review?.updated_at ?? "",
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
    const page =
      state.mode === "devrev"
        ? await api.listTickets({
            ...state.filters.devrev,
            cursor: state.cursor,
            mode: state.direction,
            pageSize: state.pageSize,
          })
        : await api.listReviews({
            ...state.filters.reviews,
            cursor: state.cursor,
            pageSize: state.pageSize,
          });
    if (serial !== requestSerial) {
      return;
    }
    const items = Array.isArray(page?.items) ? page.items : [];
    const rows =
      state.mode === "devrev" ? items.map(normalizeLiveRow) : items.map(normalizeReviewRow);
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

function devrevFilterPatch() {
  return {
    ticketId: document.getElementById("devrev-ticket-id").value.trim().toUpperCase(),
    stage: document.getElementById("devrev-stage").value.trim(),
    state: document.getElementById("devrev-state").value,
    sourceChannel: document.getElementById("devrev-source-channel").value.trim(),
    subtype: document.getElementById("devrev-subtype").value.trim(),
    createdDate: document.getElementById("devrev-created").value,
    modifiedDate: document.getElementById("devrev-modified").value,
  };
}

function reviewFilterPatch() {
  const statuses = Array.from(
    dom.statusChecks.querySelectorAll("[data-status-filter]")
  )
    .filter((box) => box.checked)
    .map((box) => box.value);
  return {
    displayId: document.getElementById("reviews-display-id").value.trim().toUpperCase(),
    statuses,
    facet: dom.facet.value,
    facetValue: dom.facetValue.disabled ? "" : dom.facetValue.value,
    updatedAfter: document.getElementById("reviews-updated-after").value,
    updatedBefore: document.getElementById("reviews-updated-before").value,
    includeReversed: document.getElementById("reviews-include-reversed").checked,
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
 * Reflect the state onto the controls, including what the grammar forbids.
 *
 * An exact identifier is a standalone mode, and the queue accepts at most one
 * facet. Both are enforced by the server; disabling the conflicting controls
 * here is what stops a reviewer discovering that through a 422.
 */
function syncFilterControls(state) {
  const force = state.formGeneration !== lastFormGeneration;
  lastFormGeneration = state.formGeneration;

  const devrev = state.filters.devrev;
  setFieldValue(document.getElementById("devrev-ticket-id"), devrev.ticketId, force);
  setFieldValue(document.getElementById("devrev-stage"), devrev.stage, force);
  setFieldValue(document.getElementById("devrev-state"), devrev.state, force);
  setFieldValue(document.getElementById("devrev-source-channel"), devrev.sourceChannel, force);
  setFieldValue(document.getElementById("devrev-subtype"), devrev.subtype, force);
  setFieldValue(document.getElementById("devrev-created"), devrev.createdDate, force);
  setFieldValue(document.getElementById("devrev-modified"), devrev.modifiedDate, force);

  const exactLive = devrev.ticketId !== "";
  for (const id of [
    "devrev-stage",
    "devrev-state",
    "devrev-source-channel",
    "devrev-subtype",
    "devrev-created",
    "devrev-modified",
  ]) {
    const node = document.getElementById(id);
    node.disabled = exactLive;
    node.setAttribute("aria-describedby", "devrev-ticket-id-help");
  }

  const reviews = state.filters.reviews;
  setFieldValue(document.getElementById("reviews-display-id"), reviews.displayId, force);
  setFieldValue(document.getElementById("reviews-updated-after"), reviews.updatedAfter, force);
  setFieldValue(document.getElementById("reviews-updated-before"), reviews.updatedBefore, force);
  document.getElementById("reviews-include-reversed").checked = reviews.includeReversed;

  const exactQueue = reviews.displayId !== "";
  render.renderStatusChecks(dom.statusChecks, REVIEW_STATUSES, reviews.statuses, {
    disabled: exactQueue,
  });

  setFieldValue(dom.facet, REVIEW_FACETS.includes(reviews.facet) ? reviews.facet : "", force);
  const freeText = render.renderFacetValues(dom.facetValue, dom.facet.value);
  if (!dom.facetValue.disabled) {
    setFieldValue(dom.facetValue, reviews.facetValue, force);
  }
  // One facet at a time. A second one is a refusal on the server, so the rest of
  // the list is visibly unavailable rather than discovered through a 422.
  for (const option of Array.from(dom.facet.options)) {
    const otherFacetChosen = dom.facet.value !== "" && option.value !== dom.facet.value;
    option.disabled =
      option.value !== "" && (otherFacetChosen || FREE_TEXT_FACETS.has(option.value));
  }
  dom.facetHelp.textContent = describeFacetHelp(dom.facet.value, freeText);

  for (const id of [
    "reviews-facet",
    "reviews-facet-value",
    "reviews-updated-after",
    "reviews-updated-before",
    "reviews-include-reversed",
  ]) {
    const node = document.getElementById(id);
    if (exactQueue) {
      node.disabled = true;
    } else if (id !== "reviews-facet-value") {
      node.disabled = false;
    }
  }
}

function describeFacetHelp(facet, freeText) {
  if (facet === "") {
    return (
      "A status set plus at most one facet. Two facets match a free-text value " +
      "and need a lookup control that is not approved yet."
    );
  }
  if (freeText) {
    return "This facet matches a free-text value, so it has no list to choose from.";
  }
  return "One facet at a time. Clear this one to choose another.";
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
  if (flags.import_export_enabled !== true) {
    unavailable.push("spreadsheet import and export");
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
  const activeTab = state.mode === "devrev" ? "tab-devrev" : "tab-reviews";
  for (const tab of dom.tabs) {
    const selected = tab.dataset.mode === state.mode;
    tab.setAttribute("aria-selected", selected ? "true" : "false");
    tab.tabIndex = selected ? 0 : -1;
  }
  dom.panel.setAttribute("aria-labelledby", activeTab);
  dom.devrevForm.hidden = state.mode !== "devrev";
  dom.reviewsForm.hidden = state.mode !== "reviews";
  dom.caption.textContent =
    state.mode === "devrev"
      ? "Live tickets from the ticket system, with the durable review overlaid on each row."
      : "The durable review queue, newest change first.";

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

  const tallies = pageTallies(state.rows);
  dom.kpi.unreviewed.textContent = `${tallies.unreviewed} on this page`;
  dom.kpi.lowRating.textContent = `${tallies.lowRating} on this page`;
  dom.kpi.highSeverity.textContent = `${tallies.highSeverity} on this page`;
  dom.kpi.remediating.textContent = `${tallies.remediating} on this page`;

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
  for (const node of Object.values(dom.refresh)) {
    node.disabled = paused;
  }

  const count = state.selectedIds.length;
  dom.bulkBar.dataset.selected = count > 0 ? "true" : "false";
  dom.bulkCount.textContent =
    count === 0 ? "No tickets selected" : `${count} ticket${count === 1 ? "" : "s"} selected`;
  const importable = state.rows.filter(
    (row) => state.selectedIds.includes(row.displayId) && row.review === null
  ).length;
  dom.bulkImport.disabled = importable === 0 || !canCreateReview() || paused;
  dom.bulkClear.disabled = count === 0;
  dom.bulkSelectAll.disabled = state.rows.length === 0;
  const flags = state.session?.featureFlags ?? {};
  dom.bulkRemediation.disabled = flags.remediation_enabled !== true;
  dom.bulkRemediationHelp.textContent = describeDisabledCapabilities(flags);

  if (state.selected === "") {
    dom.detail.hidden = true;
  } else {
    dom.detail.hidden = false;
    dom.detailTarget.textContent = state.selected;
  }

  const rowsOnPage = state.rows.map((row) => row.displayId);
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
    state.mode === rendered.mode &&
    state.error === rendered.error &&
    role === rendered.role;

  if (unchanged) {
    for (const row of Array.from(dom.body.querySelectorAll('[data-row="ticket"]'))) {
      const selected = state.selectedIds.includes(row.dataset.displayId);
      row.setAttribute("aria-selected", selected ? "true" : "false");
      const box = row.querySelector("[data-select]");
      if (box !== null) {
        box.checked = selected;
      }
    }
    return;
  }

  rendered = { rows: state.rows, phase: state.phase, mode: state.mode, error: state.error, role };

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
    render.renderStateRow(dom.body, {
      title: "No tickets match these filters",
      body:
        state.mode === "devrev"
          ? "Clear a filter, or check the exact identifier."
          : "Nothing in the durable queue matches. A ticket appears here once it has been added.",
    });
    return;
  }
  render.renderRows(dom.body, state.rows, {
    selectedIds: state.selectedIds,
    canCreateReview: canCreateReview(),
    commentsAvailable: state.mode === "reviews",
  });
}

let lastSearch = null;
let lastSelected = null;

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

function openDetail(displayId, source) {
  restoreFocusTo = source ?? null;
  store.dispatch({ type: "detail/open", id: displayId });
  dom.detail.focus();
}

function closeDetail() {
  store.dispatch({ type: "detail/close" });
  if (restoreFocusTo !== null && restoreFocusTo.isConnected) {
    restoreFocusTo.focus();
  }
  restoreFocusTo = null;
}

async function importSelected(displayIds) {
  if (!canCreateReview()) {
    toast({
      title: "Your role does not allow this",
      body: "Creating a review needs the reviewer role.",
      tone: "error",
    });
    return;
  }
  let created = 0;
  const failures = [];
  for (const displayId of displayIds) {
    try {
      await api.createReview(displayId);
      created += 1;
    } catch (error) {
      if (error instanceof api.AbortedError) {
        continue;
      }
      failures.push(error);
      if (error.retryAfterS !== null && error.retryAfterS !== undefined) {
        startCooldown(error.retryAfterS);
        break;
      }
    }
  }
  if (created > 0) {
    toast({
      title: `Added ${created} ticket${created === 1 ? "" : "s"} to the review queue`,
      tone: "info",
    });
  }
  if (failures.length > 0) {
    reportError(failures[0]);
  }
  store.dispatch({ type: "selection/clear" });
  await load({ refresh: true });
}

function wireTabs() {
  for (const tab of dom.tabs) {
    tab.addEventListener("click", () => {
      store.dispatch({ type: "mode/set", mode: tab.dataset.mode });
      load();
    });
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
        return;
      }
      event.preventDefault();
      const index = dom.tabs.indexOf(tab);
      const next = dom.tabs[(index + (event.key === "ArrowRight" ? 1 : dom.tabs.length - 1)) % dom.tabs.length];
      next.focus();
      store.dispatch({ type: "mode/set", mode: next.dataset.mode });
      load();
    });
  }
}

function wireFilters() {
  const submitDevrev = () => {
    store.dispatch({ type: "filters/patch", mode: "devrev", patch: devrevFilterPatch() });
    load();
  };
  const submitReviews = () => {
    store.dispatch({ type: "filters/patch", mode: "reviews", patch: reviewFilterPatch() });
    load();
  };
  const debouncedDevrev = debounce(submitDevrev, TEXT_DEBOUNCE_MS);
  const debouncedReviews = debounce(submitReviews, TEXT_DEBOUNCE_MS);

  dom.devrevForm.addEventListener("submit", (event) => event.preventDefault());
  dom.reviewsForm.addEventListener("submit", (event) => event.preventDefault());

  // Typing is debounced so a keystroke does not spend a request; a deliberate
  // choice from a list is submitted at once.
  dom.devrevForm.addEventListener("input", (event) => {
    if (event.target.tagName === "INPUT") {
      debouncedDevrev();
    }
  });
  dom.devrevForm.addEventListener("change", (event) => {
    if (event.target.tagName === "SELECT") {
      submitDevrev();
    }
  });
  dom.reviewsForm.addEventListener("input", (event) => {
    if (event.target.type === "search") {
      debouncedReviews();
    }
  });
  dom.reviewsForm.addEventListener("change", (event) => {
    if (event.target.type !== "search") {
      submitReviews();
    }
  });

  for (const [mode, node] of Object.entries(dom.refresh)) {
    node.addEventListener("click", () => {
      if (store.getState().mode !== mode) {
        store.dispatch({ type: "mode/set", mode });
      }
      load({ refresh: true });
    });
  }

  for (const [mode, id] of [["devrev", "devrev-clear"], ["reviews", "reviews-clear"]]) {
    document.getElementById(id).addEventListener("click", (event) => {
      event.preventDefault();
      store.dispatch({ type: "filters/clear", mode });
      load();
    });
  }

  dom.activeFilters.addEventListener("click", (event) => {
    const control = event.target.closest("button");
    if (control === null) {
      return;
    }
    const state = store.getState();
    if (control.dataset.action === "clear-filters") {
      store.dispatch({ type: "filters/clear", mode: state.mode });
      load();
      return;
    }
    const field = control.dataset.chipField;
    if (field === undefined) {
      return;
    }
    if (field === "statuses") {
      const remaining = state.filters.reviews.statuses.filter(
        (item) => item !== control.dataset.chipItem
      );
      store.dispatch({ type: "filters/patch", mode: "reviews", patch: { statuses: remaining } });
    } else if (field === "facet") {
      store.dispatch({
        type: "filters/patch",
        mode: "reviews",
        patch: { facet: "", facetValue: "" },
      });
    } else if (field === "includeReversed") {
      store.dispatch({ type: "filters/patch", mode: "reviews", patch: { includeReversed: false } });
    } else {
      const blank = field === "statuses" ? [] : "";
      store.dispatch({ type: "filters/patch", mode: state.mode, patch: { [field]: blank } });
    }
    load();
  });

  dom.sheetToggle.addEventListener("click", () => {
    const open = dom.sheetToggle.getAttribute("aria-expanded") !== "true";
    dom.sheetToggle.setAttribute("aria-expanded", open ? "true" : "false");
    dom.filterForms.dataset.open = open ? "true" : "false";
    if (open) {
      const form = store.getState().mode === "devrev" ? dom.devrevForm : dom.reviewsForm;
      const first = form.querySelector("input, select");
      if (first !== null) {
        first.focus();
      }
    } else {
      dom.sheetToggle.focus();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    if (dom.filterForms.dataset.open === "true") {
      dom.sheetToggle.click();
      return;
    }
    if (!dom.detail.hidden) {
      closeDetail();
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
      if (control.dataset.action === "import") {
        importSelected([control.dataset.displayId]);
      } else if (control.dataset.action === "open") {
        openDetail(control.dataset.displayId, control);
      }
      return;
    }
    const row = event.target.closest('[data-row="ticket"]');
    if (row !== null) {
      openDetail(row.dataset.displayId, row);
    }
  });

  dom.body.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    const row = event.target.closest('[data-row="ticket"]');
    if (row === null || event.target !== row) {
      return;
    }
    event.preventDefault();
    openDetail(row.dataset.displayId, row);
  });

  dom.selectAll.addEventListener("change", () => {
    const state = store.getState();
    const ids = state.rows.map((row) => row.displayId);
    store.dispatch({
      type: "selection/set",
      ids: dom.selectAll.checked ? ids : [],
    });
  });

  dom.bulkClear.addEventListener("click", () => store.dispatch({ type: "selection/clear" }));
  dom.bulkSelectAll.addEventListener("click", () => {
    const state = store.getState();
    const ids = state.rows.map((row) => row.displayId);
    const all = ids.length > 0 && ids.every((id) => state.selectedIds.includes(id));
    store.dispatch({ type: "selection/set", ids: all ? [] : ids });
  });
  dom.bulkImport.addEventListener("click", () => {
    const state = store.getState();
    const targets = state.rows
      .filter((row) => state.selectedIds.includes(row.displayId) && row.review === null)
      .map((row) => row.displayId);
    importSelected(targets);
  });

  dom.prev.addEventListener("click", () => {
    store.dispatch({ type: "page/back" });
    load();
  });
  dom.next.addEventListener("click", () => {
    store.dispatch({ type: "page/forward" });
    load();
  });

  dom.detailClose.addEventListener("click", closeDetail);

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
  // the selection so an older shared link still lands on the right ticket.
  const segments = globalThis.location.pathname.split("/").filter((part) => part !== "");
  const fromPath = segments.length > 1 ? decodeURIComponent(segments[1]).toUpperCase() : "";
  store.dispatch({ type: "mode/set", mode: parsed.mode });
  store.dispatch({ type: "filters/patch", mode: "devrev", patch: parsed.devrev, reset: true });
  store.dispatch({ type: "filters/patch", mode: "reviews", patch: parsed.reviews, reset: true });
  const selected = parsed.selected !== "" ? parsed.selected : fromPath;
  if (selected !== "") {
    store.dispatch({ type: "detail/open", id: selected });
  }
}

async function boot() {
  store.subscribe(renderAll);
  wireTabs();
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
    applyLocation();
    load();
  });
  globalThis.addEventListener("pagehide", () => api.abortAll());

  await load();
}

boot();
