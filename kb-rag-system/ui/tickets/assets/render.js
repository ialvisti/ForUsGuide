/**
 * Every pixel of remote content is produced here, and only through
 * `document.createElement` and `textContent`.
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
export const COLUMN_COUNT = 11;

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

const OBSERVATION_LABELS = new Map([
  ["correct", "Correct"],
  ["knowledge_gap", "Knowledge gap"],
  ["knowledge_conflict", "Knowledge conflict"],
  ["retrieval_miss", "Retrieval miss"],
  ["chunking_or_metadata", "Chunking or metadata"],
  ["prompt_instruction", "Prompt instruction"],
  ["orchestration_logic", "Orchestration logic"],
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
  node.textContent = parsed.toLocaleString(undefined, {
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
 * assigned reviewer, a name carried over from the spreadsheet with no account
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
    wrap.appendChild(el("span", { className: "cell-legacy-tag", text: "Legacy sheet value" }));
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
 * `row` is the normalized shape the controller produces from either source:
 * `{ displayId, title, review, updatedAt, hasReview }`. Keeping the two wire
 * shapes out of this function is what lets both tabs share one table.
 */
export function ticketRow(row, { selected, canCreateReview, commentsAvailable }) {
  const tr = el("tr", {
    attrs: {
      "data-row": "ticket",
      "data-display-id": row.displayId,
      tabindex: "0",
      "aria-selected": selected ? "true" : "false",
    },
  });

  const select = cell("Select", "col-select");
  const box = el("input", {
    attrs: {
      type: "checkbox",
      "data-select": row.displayId,
      "aria-label": `Select ticket ${row.displayId}`,
    },
  });
  box.checked = selected;
  select.appendChild(box);
  tr.appendChild(select);

  const idCell = el("th", {
    className: "cell-id",
    attrs: { scope: "row", "data-label": "Ticket ID" },
  });
  idCell.appendChild(el("span", { className: "cell-id-value", text: row.displayId }));
  if (row.title) {
    idCell.appendChild(el("span", { className: "cell-title", text: clip(row.title, TITLE_PREVIEW_LIMIT) }));
  }
  tr.appendChild(idCell);

  const review = row.review ?? null;
  tr.appendChild(
    review && review.topic ? textCell("Topic", review.topic) : emptyCell("Topic", "No topic recorded")
  );
  tr.appendChild(
    review && review.legacy_type
      ? textCell("Legacy Type", review.legacy_type)
      : emptyCell("Legacy Type", "No legacy type recorded")
  );
  tr.appendChild(
    review && review.observation_type
      ? textCell("Observation", OBSERVATION_LABELS.get(review.observation_type) ?? review.observation_type)
      : emptyCell("Observation", "No observation recorded")
  );
  tr.appendChild(ratingCell(review ? review.rating ?? null : null));
  tr.appendChild(reviewerCell(review));

  const statusCell = cell("Status", "cell-status");
  if (review === null) {
    statusCell.appendChild(pill("Not reviewed", { "data-status": "unreviewed" }));
  } else {
    statusCell.appendChild(statusPill(review.status));
    const severity = severityPill(review.severity ?? null);
    if (severity !== null) {
      statusCell.appendChild(severity);
    }
    if (review.import_state === "reversed") {
      statusCell.appendChild(pill("Reversed import", { "data-status": "wont_fix" }));
    }
  }
  tr.appendChild(statusCell);

  const updated = cell("Updated", "cell-updated");
  updated.appendChild(timeElement(row.updatedAt));
  tr.appendChild(updated);

  tr.appendChild(commentCell(review, { available: commentsAvailable }));

  const actions = cell("Actions", "col-actions");
  if (review === null) {
    actions.appendChild(
      button({
        text: "Add to review queue",
        className: "button button-quiet",
        dataset: { action: "import", displayId: row.displayId },
        disabled: !canCreateReview,
        describedBy: canCreateReview ? "" : "bulk-remediation-help",
      })
    );
  } else {
    actions.appendChild(
      button({
        label: `Open ticket ${row.displayId}`,
        icon: "next",
        className: "icon-button",
        dataset: { action: "open", displayId: row.displayId },
      })
    );
  }
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
        selected: options.selectedIds.includes(row.displayId),
        canCreateReview: options.canCreateReview,
        commentsAvailable: options.commentsAvailable,
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
    node.appendChild(el("span", { className: "chip-name", text: `${chip.label}:` }));
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

export { STATUS_LABELS, OBSERVATION_LABELS, SEVERITY_LABELS };
