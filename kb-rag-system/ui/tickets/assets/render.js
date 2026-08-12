/**
 * Every shared primitive for remote content is produced here, and only through
 * `document.createElement` and `textContent`. The structured audit presenter
 * composes these primitives in `structured.js`; it never introduces an HTML sink.
 *
 * The strings this module receives are a participant's ticket title, a
 * reviewer's typed comment, an email address, and an upstream stage name. Not
 * one of them is trusted markup, so nothing here assigns markup to the document
 * and no template string ever becomes HTML. The shipped
 * Content-Security-Policy is the second layer; this is the first, and it is the
 * one that still holds if the policy is ever widened by mistake.
 *
 * Two other rules live here because this is where they would be broken:
 *
 *   * this module is handed rows and never the signed-in caller, so the table's
 *     Reviewer column cannot accidentally become "whoever is logged in". The
 *     assigned reviewer, the legacy sheet's reviewer name, and the authenticated
 *     actor are three different facts;
 *   * exactly one function creates a `<button>`, and it refuses to build one
 *     without an accessible name. An icon-only button with no name is the most
 *     common way a table like this becomes unusable with a screen reader.
 */

/** Column count of the results table; a full-width state row spans all of it. */
export const COLUMN_COUNT = 8;

/** Longest live title preview rendered in a row, in characters. */
const TITLE_PREVIEW_LIMIT = 160;

/** Longest comment preview rendered in a row, in characters. */
const COMMENT_PREVIEW_LIMIT = 240;

const EM_DASH = "—";

/** Set once, from the icon sprite's own root element. */
let svgNamespace = null;

const STATUS_LABELS = new Map([
  ["unreviewed", "Unreviewed"],
  ["reviewed", "Reviewed"],
  ["triaged", "Triaged"],
  ["planned", "Planned"],
  ["in_progress", "In progress"],
  ["changes_proposed", "Changes proposed"],
  ["verifying", "Verifying"],
  ["resolved", "Resolved"],
  ["blocked", "Blocked"],
  ["wont_fix", "Will not fix"],
]);

const REQUEST_TYPE_LABELS = new Map([
  ["knowledge_question", "Knowledge Question"],
  ["generate_response", "Generate Response"],
]);

const OBSERVATION_LABELS = new Map([
  ["correct", "Correct"],
  ["knowledge_gap", "Knowledge gap"],
  ["knowledge_conflict", "Knowledge conflict"],
  ["retrieval_miss", "Retrieval miss"],
  ["chunking_or_metadata", "Chunking or metadata"],
  ["prompt_instruction", "Prompt instruction"],
  ["orchestration_logic", "Orchestration logic"],
  ["wrong_route", "Wrong route"],
  ["source_data", "Source data"],
  ["privacy_or_compliance", "Privacy or compliance"],
  ["other", "Other"],
]);

const SEVERITY_LABELS = new Map([
  ["low", "Low"],
  ["medium", "Medium"],
  ["high", "High"],
  ["critical", "Critical"],
]);

/** Server warning codes, in words a reviewer can use. */
const WARNING_LABELS = new Map([
  ["devrev_unavailable", "Live ticket data is unavailable; durable reviews are shown."],
  ["review_lookup_failed", "Some rows could not be matched to a durable review."],
  ["created_date_fallback_used", "Some creation dates fell back to the modification date."],
]);

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

/**
 * Create one element. `text` always goes through `textContent`.
 *
 * `attrs` is for accessibility and data attributes only; nothing here ever
 * writes an event-handler attribute, because the policy forbids inline handlers
 * and listeners belong to the controller.
 */
export function el(tag, { className = "", text = "", attrs = {}, children = [] } = {}) {
  const node = document.createElement(tag);
  if (className !== "") {
    node.className = className;
  }
  if (text !== "" && text !== null && text !== undefined) {
    node.textContent = String(text);
  }
  for (const [name, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children) {
    if (child !== null && child !== undefined) {
      node.appendChild(child);
    }
  }
  return node;
}

/** Text that only assistive technology reads. */
export function hiddenText(text) {
  return el("span", { className: "visually-hidden", text });
}

/**
 * The only place a `<button>` is created.
 *
 * A button with neither visible text nor `label` is refused outright rather than
 * shipped as an unnamed control, which is why this throws instead of warning.
 */
export function button({
  label = "",
  text = "",
  icon = "",
  className = "button",
  dataset = {},
  disabled = false,
  describedBy = "",
} = {}) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  if (text === "" && label === "") {
    throw new Error("a button needs visible text or an accessible name");
  }
  if (label !== "") {
    node.setAttribute("aria-label", label);
  }
  if (describedBy !== "") {
    node.setAttribute("aria-describedby", describedBy);
  }
  if (icon !== "") {
    node.appendChild(iconSlot(icon));
  }
  if (text !== "") {
    node.appendChild(el("span", { text }));
  }
  node.disabled = Boolean(disabled);
  for (const [key, value] of Object.entries(dataset)) {
    node.dataset[key] = String(value);
  }
  return node;
}

/** A placeholder the sprite fills in later; always decorative. */
export function iconSlot(name) {
  return el("span", {
    className: "icon",
    attrs: { "data-icon": name, "aria-hidden": "true" },
  });
}

// ---------------------------------------------------------------------------
// The icon sprite
// ---------------------------------------------------------------------------

/**
 * Parse the sprite as XML and keep only its `<symbol>` elements.
 *
 * `DOMParser` with `image/svg+xml` is an XML parser, not an HTML one, and this
 * input is a same-origin static asset rather than remote content — but the
 * filter is still applied, so a hand-edit that adds a `<script>` or a
 * `<foreignObject>` to the file cannot reach the document.
 */
export function parseSprite(text) {
  const symbols = new Map();
  const parsed = new DOMParser().parseFromString(text, "image/svg+xml");
  const root = parsed.documentElement;
  if (root === null || root.nodeName.toLowerCase() === "parsererror") {
    return symbols;
  }
  svgNamespace = root.namespaceURI;
  for (const node of Array.from(root.children)) {
    if (node.nodeName.toLowerCase() !== "symbol") {
      continue;
    }
    const id = node.getAttribute("id");
    if (id !== null && id !== "") {
      symbols.set(id, node);
    }
  }
  return symbols;
}

/** Fill every empty icon slot under `root` from the parsed sprite. */
export function mountIcons(root, symbols) {
  if (symbols.size === 0 || svgNamespace === null) {
    return;
  }
  for (const slot of Array.from(root.querySelectorAll("[data-icon]"))) {
    if (slot.firstElementChild !== null) {
      continue;
    }
    const symbol = symbols.get(slot.dataset.icon);
    if (symbol === undefined) {
      continue;
    }
    const svg = document.createElementNS(svgNamespace, "svg");
    svg.setAttribute("viewBox", symbol.getAttribute("viewBox") ?? "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (const child of Array.from(symbol.childNodes)) {
      svg.appendChild(document.importNode(child, true));
    }
    slot.appendChild(svg);
  }
}

// ---------------------------------------------------------------------------
// Cells
// ---------------------------------------------------------------------------

function clip(value, limit) {
  const text = String(value ?? "");
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function cell(label, className = "") {
  // `data-label` is what turns this cell into a labelled line of a card below
  // the narrow breakpoint, where the header row is not displayed.
  return el("td", { className, attrs: { "data-label": label } });
}

function emptyCell(label, note = "") {
  const node = cell(label, "cell-empty");
  node.appendChild(el("span", { text: EM_DASH, attrs: { "aria-hidden": "true" } }));
  node.appendChild(hiddenText(note === "" ? "Not set" : note));
  return node;
}

/**
 * A rating as five glyphs plus the same value in words.
 *
 * The glyphs are decorative: "★★☆☆☆" is not something a screen reader can
 * usefully announce, so `3 out of 5` is the real content and the stars are the
 * scannable duplicate.
 */
export function ratingCell(value) {
  if (typeof value !== "number") {
    return emptyCell("Rating", "No rating recorded");
  }
  const node = cell("Rating", "cell-rating");
  const stars = "★".repeat(value) + "☆".repeat(Math.max(0, 5 - value));
  node.appendChild(
    el("span", {
      className: "rating",
      text: stars,
      attrs: { "data-rating": String(value), "aria-hidden": "true" },
    })
  );
  node.appendChild(hiddenText(`${value} of 5`));
  return node;
}

/** A date as a machine-readable `<time>` rendered in the reader's own locale. */
export function timeElement(iso) {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return el("span", { className: "cell-empty", text: EM_DASH });
  }
  const node = document.createElement("time");
  node.dateTime = parsed.toISOString();
  const locale = document.documentElement.lang || undefined;
  node.textContent = parsed.toLocaleString(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  return node;
}

function pill(text, attrs) {
  return el("span", { className: "pill", text, attrs });
}

/** Status as a labelled pill; the glyph and the word both carry the meaning. */
export function statusPill(status) {
  const label = STATUS_LABELS.get(status) ?? "Unknown";
  return pill(label, { "data-status": status ?? "unknown" });
}

/** Severity as a labelled pill, or nothing when none was recorded. */
export function severityPill(severity) {
  if (severity === null || severity === undefined) {
    return null;
  }
  return pill(SEVERITY_LABELS.get(severity) ?? severity, { "data-severity": severity });
}

/**
 * Who is responsible for this review — never the caller reading the page.
 *
 * Three distinct answers, and the difference is stated rather than implied: an
 * assigned reviewer, a historical display name with no account
 * behind it, or nobody at all.
 */
export function reviewerCell(review) {
  const node = cell("Reviewer", "cell-reviewer");
  if (review === null || review === undefined) {
    node.appendChild(el("span", { className: "cell-empty", text: "Unassigned" }));
    return node;
  }
  const assigned =
    review.assigned_reviewer_email ??
    (review.assigned_reviewer ? review.assigned_reviewer.email : null);
  if (assigned) {
    node.appendChild(el("span", { text: assigned }));
    return node;
  }
  const legacy = review.legacy_reviewer_display_name;
  if (legacy) {
    const wrap = el("span", { className: "cell-legacy" });
    wrap.appendChild(el("span", { text: legacy }));
    wrap.appendChild(el("span", { className: "cell-legacy-tag", text: "Historical value" }));
    node.appendChild(wrap);
    return node;
  }
  node.appendChild(el("span", { className: "cell-empty", text: "Unassigned" }));
  return node;
}

/** A clamped, text-only comment preview. Never markup, never a tooltip alone. */
export function commentCell(review, { available }) {
  if (!available) {
    return emptyCell(
      "Comments",
      "Comments are not returned on the live list; open the ticket to read them."
    );
  }
  const comments = review === null || review === undefined ? "" : review.comments ?? "";
  if (comments === "") {
    return emptyCell("Comments", "No comments recorded");
  }
  const node = cell("Comments");
  node.appendChild(el("p", { className: "cell-comments", text: clip(comments, COMMENT_PREVIEW_LIMIT) }));
  if (comments.length > COMMENT_PREVIEW_LIMIT) {
    node.appendChild(hiddenText("Truncated. Open the ticket to read the full comment."));
  }
  return node;
}

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

/**
 * One row.
 *
 * `row` is the normalized execution shape from the adapter. The list endpoint
 * exposes only scope-authorized runs whose DevRev hydration succeeded.
 */
export function ticketRow(row, { selected }) {
  const tr = el("tr", {
    attrs: {
      "data-row": "ticket",
      "data-execution-id": row.executionId,
      "data-display-id": row.displayId,
      tabindex: "0",
      "aria-selected": selected ? "true" : "false",
    },
  });

  const select = cell("Select", "col-select");
  const box = el("input", {
    attrs: {
      type: "checkbox",
      "data-select": row.executionId,
      "aria-label": `Select ticket ${row.displayId || row.executionId}`,
    },
  });
  box.checked = selected;
  select.appendChild(box);
  tr.appendChild(select);

  const ticketCell = el("th", {
    className: "cell-id",
    attrs: { scope: "row", "data-label": "Ticket" },
  });
  ticketCell.appendChild(
    el("span", { className: "cell-id-value", text: row.displayId || "Not loaded" })
  );
  if (row.title) {
    ticketCell.appendChild(
      el("span", { className: "cell-title", text: clip(row.title, TITLE_PREVIEW_LIMIT) })
    );
  }
  tr.appendChild(ticketCell);

  const review = row.review ?? null;
  const reviewStatus = cell("Review status", "cell-status");
  reviewStatus.appendChild(statusPill(review?.status ?? "unreviewed"));
  tr.appendChild(reviewStatus);

  const requestType = cell("Request type", "cell-request-type");
  requestType.appendChild(
    el("span", { text: REQUEST_TYPE_LABELS.get(row.route) ?? (row.route || "Unknown") })
  );
  tr.appendChild(requestType);

  const received = cell("Received", "cell-updated");
  received.appendChild(timeElement(row.createdAt));
  tr.appendChild(received);

  tr.appendChild(ratingCell(review ? review.rating ?? null : null));
  tr.appendChild(reviewerCell(review));

  const actions = cell("Actions", "col-actions");
  const ticketLabel = row.displayId || row.executionId;
  actions.appendChild(
    button({
      label: `Review ticket ${ticketLabel}`,
      text: "Review",
      icon: "next",
      className: "button button-compact",
      dataset: { action: "open", executionId: row.executionId },
    })
  );
  tr.appendChild(actions);
  return tr;
}

function textCell(label, value) {
  const node = cell(label);
  node.appendChild(el("span", { text: value }));
  return node;
}

/** Replace a table body's contents without ever assigning markup. */
function replaceBody(tbody, nodes) {
  while (tbody.firstChild !== null) {
    tbody.removeChild(tbody.firstChild);
  }
  for (const node of nodes) {
    tbody.appendChild(node);
  }
}

/** Rows, or the state that stands in for them. */
export function renderRows(tbody, rows, options) {
  replaceBody(
    tbody,
    rows.map((row) =>
      ticketRow(row, {
        selected: options.selectedIds.includes(row.executionId),
      })
    )
  );
}

/**
 * Placeholder rows that occupy the same height as real ones.
 *
 * The point is that nothing moves when the answer arrives; a spinner that
 * collapses the table makes every load feel like a reflow.
 */
export function renderSkeletons(tbody, count = 6) {
  const rows = [];
  for (let index = 0; index < count; index += 1) {
    const tr = el("tr", { attrs: { "aria-hidden": "true" } });
    for (let column = 0; column < COLUMN_COUNT; column += 1) {
      const td = el("td");
      td.appendChild(el("span", { className: "skeleton-line" }));
      tr.appendChild(td);
    }
    rows.push(tr);
  }
  replaceBody(tbody, rows);
}

/** An empty, failed, or paused table body. Replaces skeletons, never overlays. */
export function renderStateRow(tbody, { title, body = "", requestId = "" }) {
  const td = el("td", {
    className: "state-cell",
    attrs: { colspan: String(COLUMN_COUNT) },
  });
  td.appendChild(el("p", { className: "state-title", text: title }));
  if (body !== "") {
    td.appendChild(el("p", { className: "state-body", text: body }));
  }
  if (requestId !== "" && requestId !== null) {
    td.appendChild(el("p", { className: "state-request-id", text: `Request id ${requestId}` }));
  }
  const tr = el("tr");
  tr.appendChild(td);
  replaceBody(tbody, [tr]);
}

// ---------------------------------------------------------------------------
// Chrome
// ---------------------------------------------------------------------------

/** Active filter chips, each with its own remove control. */
export function renderChips(container, chips) {
  while (container.firstChild !== null) {
    container.removeChild(container.firstChild);
  }
  if (chips.length === 0) {
    return;
  }
  for (const chip of chips) {
    const node = el("span", { className: "chip" });
    const name = el("span", { className: "chip-name" });
    name.appendChild(document.createTextNode(chip.label));
    name.appendChild(document.createTextNode(":"));
    node.appendChild(name);
    node.appendChild(el("span", { text: chip.value }));
    node.appendChild(
      button({
        label: `Remove the ${chip.label} filter`,
        icon: "close",
        className: "icon-button",
        dataset: { chipField: chip.field, chipItem: chip.item ?? "" },
      })
    );
    container.appendChild(node);
  }
  container.appendChild(
    button({
      text: "Clear all",
      className: "button button-quiet",
      dataset: { action: "clear-filters" },
    })
  );
}

/** Turn the server's warning codes into a sentence, keeping unknown ones visible. */
export function describeWarnings(warnings) {
  return warnings
    .map((code) => WARNING_LABELS.get(code) ?? `Reported condition: ${code}`)
    .join(" ");
}

/**
 * The one-line status above the table.
 *
 * A partial page says so. Rendering an incomplete answer as a complete one is
 * the single most expensive mistake this surface can make, because "no tickets"
 * and "we could not read the tickets" look identical.
 */
export function renderTableStatus(node, { phase, rows, partial, stale, warnings, cooldownS }) {
  let tone = "info";
  const parts = [];
  if (cooldownS > 0) {
    tone = "warning";
    parts.push(`Requests are paused for ${cooldownS} more second${cooldownS === 1 ? "" : "s"}.`);
  }
  if (phase === "refreshing") {
    parts.push("Refreshing…");
  }
  if (stale) {
    tone = "warning";
    parts.push("Showing the last successful answer; the newest request failed.");
  }
  if (partial) {
    tone = "warning";
    parts.push("This page is incomplete.");
  }
  if (warnings.length > 0) {
    tone = "warning";
    parts.push(describeWarnings(warnings));
  }
  if (parts.length === 0 && phase === "ready") {
    parts.push(`${rows} row${rows === 1 ? "" : "s"} on this page.`);
  }
  node.dataset.tone = tone;
  node.textContent = parts.join(" ");
}

/** A transient message in the live region. Text only, dismissible, self-clearing. */
export function pushToast(region, { title, body = "", tone = "info", requestId = "" }) {
  const node = el("div", { className: "toast", attrs: { "data-tone": tone } });
  node.appendChild(el("p", { className: "toast-title", text: title }));
  if (body !== "") {
    node.appendChild(el("p", { className: "toast-body", text: body }));
  }
  if (requestId !== "" && requestId !== null) {
    node.appendChild(el("p", { className: "toast-body", text: `Request id ${requestId}` }));
  }
  const dismiss = button({
    label: "Dismiss this message",
    icon: "close",
    className: "icon-button",
    dataset: { action: "dismiss-toast" },
  });
  node.appendChild(dismiss);
  region.appendChild(node);
  globalThis.setTimeout(() => node.remove(), tone === "error" ? 20000 : 8000);
  return node;
}

/**
 * Status checkboxes for the queue filter, built once and then updated in place.
 *
 * Rebuilding on every store change would destroy the node the reviewer just
 * ticked and take the keyboard focus with it, so the boxes are created once and
 * only their `checked`/`disabled` state is written afterwards.
 */
export function renderStatusChecks(container, statuses, selected, { disabled = false } = {}) {
  if (container.children.length !== statuses.length) {
    while (container.firstChild !== null) {
      container.removeChild(container.firstChild);
    }
    for (const status of statuses) {
      const id = `status-${status}`;
      const box = el("input", {
        attrs: { type: "checkbox", id, value: status, "data-status-filter": status },
      });
      const label = el("label", {
        text: STATUS_LABELS.get(status) ?? status,
        attrs: { for: id },
      });
      const wrap = el("span", { className: "check" });
      wrap.appendChild(box);
      wrap.appendChild(label);
      container.appendChild(wrap);
    }
  }
  for (const box of Array.from(container.querySelectorAll("[data-status-filter]"))) {
    box.checked = selected.includes(box.value);
    box.disabled = disabled;
  }
}

/**
 * Values a facet can take. Every entry is a closed server-side enumeration.
 *
 * `topic` and `assigned_reviewer.email` are deliberately absent: both match a
 * free-text stored value, so offering them without a lookup control would ship a
 * filter that cannot be completed — and one of them would write an email address
 * into the address bar.
 */
export const FACET_VALUES = new Map([
  ["observation_type", Array.from(OBSERVATION_LABELS.keys())],
  ["severity", Array.from(SEVERITY_LABELS.keys())],
  ["rating", ["1", "2", "3", "4", "5"]],
  ["remediation_target", ["kb", "prompt", "code", "workflow", "source_data", "none", "unknown"]],
]);

const REMEDIATION_LABELS = new Map([
  ["kb", "Knowledge base"],
  ["prompt", "Prompt"],
  ["code", "Code"],
  ["workflow", "Workflow"],
  ["source_data", "Source data"],
  ["none", "None"],
  ["unknown", "Unknown"],
]);

/**
 * Fill the facet-value control.
 *
 * Returns true when the chosen facet has no closed vocabulary, so the caller can
 * say why the value control is unavailable rather than leaving an empty select.
 */
export function renderFacetValues(select, facet) {
  if (select.dataset.facet === facet) {
    return !FACET_VALUES.has(facet) && facet !== "";
  }
  select.dataset.facet = facet;
  while (select.firstChild !== null) {
    select.removeChild(select.firstChild);
  }
  if (facet === "") {
    select.appendChild(el("option", { text: "Choose a facet first", attrs: { value: "" } }));
    select.disabled = true;
    return false;
  }
  const values = FACET_VALUES.get(facet);
  if (values === undefined) {
    select.appendChild(el("option", { text: "Not available for this facet", attrs: { value: "" } }));
    select.disabled = true;
    return true;
  }
  select.appendChild(el("option", { text: "Any value", attrs: { value: "" } }));
  for (const value of values) {
    select.appendChild(
      el("option", {
        text:
          OBSERVATION_LABELS.get(value) ??
          SEVERITY_LABELS.get(value) ??
          REMEDIATION_LABELS.get(value) ??
          value,
        attrs: { value },
      })
    );
  }
  select.disabled = false;
  return false;
}

// ---------------------------------------------------------------------------
// The detail workspace's shared primitives
//
// Everything below is still text-and-created-nodes only, and still knows nothing
// about who is signed in: it is handed values and labels, and the modules that
// know about roles decide what to hand it.
// ---------------------------------------------------------------------------

/** Longest conversation body shown before it is collapsed, in characters. */
export const BODY_COLLAPSE_LIMIT = 900;

/** Longest opaque digest rendered before it is shortened, in characters. */
const DIGEST_PREVIEW = 16;

const VISIBILITY_LABELS = new Map([
  ["public", "Public"],
  ["external", "Participant-visible"],
  ["internal", "Internal"],
  ["private", "Private"],
]);

const ACTOR_CLASS_LABELS = new Map([
  ["participant", "Participant"],
  ["human_agent", "Human agent"],
  ["ai_or_system", "AI or system"],
  ["event", "Ticket event"],
  ["unknown", "Unclassified author"],
]);

const BASIS_LABELS = new Map([
  ["configured_ai_author_id", "matched a configured AI author"],
  ["configured_system_author_id", "matched a configured system author"],
  ["configured_human_author_id", "matched a configured human author"],
  ["external_actor_type", "external actor type"],
  ["system_actor_type", "system actor type"],
  ["internal_actor_type", "internal actor type"],
  ["change_event", "a ticket change event"],
  ["ambiguous", "could not be decided from an actor type"],
  ["no_author", "carried no author"],
]);

const CORRELATION_STATUS_LABELS = new Map([
  ["linked", "Linked"],
  ["manual", "Manually linked"],
  ["unavailable", "Unavailable"],
]);

const CORRELATION_TRUST_LABELS = new Map([
  ["none", "No correlation"],
  ["candidate", "Suggested, unconfirmed"],
  ["verified_workload", "Verified by the producing workload"],
  ["manual_reviewer", "Confirmed by a reviewer"],
]);

const OUTCOME_LABELS = new Map([
  ["fixed", "Fixed"],
  ["no_change", "No change"],
  ["duplicate", "Duplicate"],
  ["accepted_risk", "Accepted risk"],
]);

const MISSING_LABELS = new Map([
  ["index_version", "index version"],
  ["deployed_revision", "deployed revision"],
  ["prompt_template", "prompt template"],
  ["model", "model"],
  ["observed_chunks", "observed vectors"],
  ["response_hash", "response hash"],
  ["source_articles", "source articles"],
  ["legacy_schema", "a pre-Stage-4 record shape"],
]);

/** Reasons the server gives for having no defensible evidence. */
const EVIDENCE_REASON_LABELS = new Map([
  [
    "no_defensible_identifiers_exist",
    "This ticket predates reliable ticket-to-RAG correlation, or its legacy " +
      "execution did not include a ticket-system identifier. The conversation " +
      "is available; retrieval and prompt provenance cannot be reconstructed " +
      "reliably.",
  ],
  [
    "evidence_broker_not_configured",
    "The evidence service is not configured for this deployment, so no " +
      "retrieval or prompt provenance can be read. Reviews and conversation " +
      "are unaffected.",
  ],
  [
    "evidence_broker_unavailable",
    "The evidence service did not answer. This is a gap in the lookup, not " +
      "proof that the ticket has no retrieval history.",
  ],
]);

/** Audit event types, in words. Unknown types are shown, never swallowed. */
const AUDIT_EVENT_LABELS = new Map([
  ["review_created", "Review created"],
  ["review_updated", "Review updated"],
  ["review_imported", "Review linked by the ticket system"],
  ["evidence_linked", "Evidence linked"],
  ["evidence_unlinked", "Evidence unlinked"],
  ["import_reversed", "Review linkage reversed"],
  ["legal_hold_set", "Legal hold set"],
  ["legal_hold_cleared", "Legal hold cleared"],
]);

/**
 * Stored field names, in the words the form uses.
 *
 * The audit ledger records which fields changed and not what they contained, so
 * these labels are the whole of what a history entry can say about a change —
 * which makes getting them right the difference between a readable ledger and a
 * list of column names.
 */
const FIELD_LABELS = new Map([
  ["topic", "Topic"],
  ["legacy_type", "Legacy Type"],
  ["observation_type", "Observation type"],
  ["rating", "Rating"],
  ["comments", "Comments"],
  ["expected_behavior", "Expected behavior"],
  ["severity", "Severity"],
  ["status", "Status"],
  ["remediation_target", "Remediation target"],
  ["assigned_reviewer", "Assigned reviewer"],
  ["legacy_reviewer_display_name", "Historical reviewer"],
  ["resolution", "Resolution"],
  ["outcome", "Outcome"],
  ["verification_summary", "Verification summary"],
  ["no_change_reason", "Verification rationale"],
  ["branch", "Branch"],
  ["commit_sha", "Commit"],
  ["import_state", "Review linkage state"],
  ["legal_hold", "Legal hold"],
  ["correlation_status", "Correlation status"],
]);

/** A label from a map, falling back to the raw value rather than hiding it. */
export function labelOf(map, value) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  return map.get(value) ?? String(value);
}

export function fieldLabel(name) {
  return labelOf(FIELD_LABELS, name);
}

export function visibilityLabel(value) {
  return labelOf(VISIBILITY_LABELS, value);
}

export function actorClassLabel(value) {
  return labelOf(ACTOR_CLASS_LABELS, value);
}

export function outcomeLabel(value) {
  return labelOf(OUTCOME_LABELS, value);
}

export function auditEventLabel(value) {
  return labelOf(AUDIT_EVENT_LABELS, value);
}

export function correlationStatusLabel(value) {
  return labelOf(CORRELATION_STATUS_LABELS, value);
}

export function correlationTrustLabel(value) {
  return labelOf(CORRELATION_TRUST_LABELS, value);
}

/** The reviewer-facing explanation of an evidence gap, keyed by server reason. */
export function evidenceGapText(reason) {
  if (reason === null || reason === undefined || reason === "") {
    return "No retrieval or prompt provenance is available for this ticket.";
  }
  return (
    EVIDENCE_REASON_LABELS.get(reason) ??
    `No retrieval or prompt provenance is available. Reported reason: ${reason}.`
  );
}

/** Absent provenance, named rather than left as a blank row. */
export function missingProvenanceText(missing) {
  const names = (missing ?? []).map((code) => labelOf(MISSING_LABELS, code));
  return names.length === 0 ? "" : `Not recorded for this execution: ${names.join(", ")}.`;
}

/**
 * A long opaque digest, shortened for reading but kept selectable in full.
 *
 * The visible half is enough to compare two records by eye; the full value stays
 * in the element's text so a copy still yields the whole hash.
 */
export function digest(value) {
  const text = String(value ?? "");
  if (text === "") {
    return "";
  }
  return text.length > DIGEST_PREVIEW * 2 ? `${text.slice(0, DIGEST_PREVIEW)}…` : text;
}

/**
 * One `<dt>`/`<dd>` pair inside a `<dl>`.
 *
 * An absent value is stated as absent and marked, because a blank row beside a
 * populated one reads as zero rather than as unknown — and on this panel the
 * difference between "no vectors were retrieved" and "we did not record which
 * vectors were retrieved" is the whole point.
 */
export function definitionRow(
  term,
  value,
  { absentNote = "Not recorded", full = "", recorded = true } = {}
) {
  const wrap = el("div");
  wrap.appendChild(el("dt", { text: term }));
  const text = value === null || value === undefined ? "" : String(value);
  if (text === "") {
    wrap.appendChild(el("dd", { text: absentNote, attrs: { "data-absent": "true" } }));
    return wrap;
  }
  const node = el("dd", {
    text,
    attrs: recorded ? { "data-audit-value": "", translate: "no" } : {},
  });
  if (full !== "" && full !== text) {
    // The shortened form is what is read; the whole value is what is announced
    // and copied, so nothing is actually lost by shortening.
    node.textContent = "";
    node.appendChild(el("span", { text, attrs: { "aria-hidden": "true" } }));
    node.appendChild(hiddenText(full));
  }
  wrap.appendChild(node);
  return wrap;
}

/** Replace an element's children without ever assigning markup. */
export function replaceChildren(node, children) {
  while (node.firstChild !== null) {
    node.removeChild(node.firstChild);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) {
      node.appendChild(child);
    }
  }
}

/**
 * Remote body text as paragraphs.
 *
 * Blank lines become paragraph breaks and nothing else is interpreted: a body
 * that arrives containing angle brackets renders as the characters a participant
 * typed, which is both correct and the only safe reading of untrusted text.
 */
export function paragraphs(text) {
  const blocks = String(text ?? "")
    .split(/\n\s*\n/)
    .map((block) => block.trim())
    .filter((block) => block !== "");
  if (blocks.length === 0) {
    return [];
  }
  return blocks.map((block) => el("p", { className: "entry-paragraph", text: block }));
}

/** The one scheme an outbound link may use. Spelled without a host on purpose. */
const REQUIRED_LINK_SCHEME = "https:";

/**
 * An outbound link, or plain text when the value cannot be trusted as one.
 *
 * The scheme is checked against a parsed URL rather than a string prefix,
 * because `javascript:` and `data:` are the two that matter and a prefix test is
 * how they get through. A value that fails is still shown — as text — because
 * hiding a stored value would leave a reviewer unable to see what is recorded.
 */
export function httpsLink(value, { text = "" } = {}) {
  const raw = String(value ?? "");
  if (raw === "") {
    return null;
  }
  let parsed = null;
  try {
    parsed = new URL(raw);
  } catch {
    parsed = null;
  }
  if (parsed === null || parsed.protocol !== REQUIRED_LINK_SCHEME) {
    const node = el("span", {
      className: "mono",
      text: raw,
      attrs: { "data-audit-value": "", translate: "no" },
    });
    node.appendChild(hiddenText("not a usable secure link; shown as text"));
    return node;
  }
  const anchor = el("a", {
    text: text === "" ? parsed.host + parsed.pathname : text,
    attrs: {
      href: parsed.href,
      rel: "noreferrer noopener",
      target: "_blank",
    },
  });
  anchor.appendChild(hiddenText("opens in a new tab"));
  return anchor;
}

/** A short status line with a tone, used by every panel. */
export function setPanelStatus(node, { text = "", tone = "info" } = {}) {
  node.dataset.tone = tone;
  node.textContent = text;
}

export {
  STATUS_LABELS,
  OBSERVATION_LABELS,
  SEVERITY_LABELS,
  REMEDIATION_LABELS,
  FIELD_LABELS,
};
