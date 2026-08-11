/**
 * Lossless, DOM-only presentation for authorized structured audit values.
 *
 * The API deliberately exposes bounded dictionaries whose shape can grow as the
 * RAG pipeline records new diagnostics. Flattening those dictionaries into JSON
 * makes them hard to read; filtering them through a fixed schema silently loses
 * new fields. This module does neither. Known fields receive human labels and
 * useful hierarchy, while every unknown key and every leaf remains available
 * with its exact source key and a stable JSON-pointer-like field path.
 *
 * Remote values are never markup. Everything is created through the same `el`
 * primitive used by the rest of the console, which assigns text with
 * `textContent`. Complete JSON documents may be parsed as data; HTML, partial
 * JSON, Markdown and JavaScript are never interpreted.
 */

import { el, replaceChildren, timeElement } from "./render.js";

const LABELS = new Map([
  ["opening", "Opening"],
  ["key_points", "Key points"],
  ["steps", "Recommended steps"],
  ["warnings", "Warnings"],
  ["action", "Action"],
  ["detail", "Details"],
  ["step_number", "Step"],
  ["answer", "Answer"],
  ["response", "Response"],
  ["response_to_participant", "Response to participant"],
  ["responseSource", "Response source"],
  ["response_source", "Response source"],
  ["inquiries", "Inquiries"],
  ["outcome", "Outcome"],
  ["outcome_reason", "Outcome rationale"],
  ["structured_outcome_reason", "Structured outcome rationale"],
  ["detected_inquiry", "Detected inquiry"],
  ["detected_topic", "Detected topic"],
  ["classification_confidence", "Classification confidence"],
  ["classification_rationale", "Classification rationale"],
  ["final_outcome", "Final outcome"],
  ["classification", "Classification"],
  ["classifier", "Classifier"],
  ["confidence", "Confidence"],
  ["reasoning", "Recorded rationale"],
  ["route", "Route"],
  ["topic", "Topic"],
  ["checkpoint", "Checkpoint"],
  ["diagnostics", "Diagnostics"],
  ["retrieval", "Retrieval"],
  ["retrieval_metadata", "Retrieval metadata"],
  ["minimum_score_met", "Minimum score met"],
  ["match_count", "Match count"],
  ["audience", "Audience"],
  ["fallbacks_used", "Fallbacks used"],
  ["field_mapping", "Field mapping"],
  ["source_plan", "Source plan"],
  ["destination_account", "Destination account"],
  ["llm_called", "LLM invoked"],
  ["field", "Field"],
  ["required", "Required"],
  ["unmapped_fields", "Fields not mapped"],
  ["mapped_modules", "Mapped modules"],
  ["scrape_meta", "Data collection"],
  ["participant_reply_safe", "Safe for participant reply"],
  ["core_eligibility_supported", "Core eligibility supported"],
  ["dominance_top_signal", "Primary decision signal"],
  ["set_stage_solved", "Mark ticket solved"],
  ["stage_reason", "Stage reason"],
  ["escalation", "Escalation"],
  ["requires_escalation", "Requires escalation"],
  ["model", "Model"],
  ["duration_ms", "Duration"],
  ["latency_ms", "Latency"],
  ["input_tokens", "Input tokens"],
  ["output_tokens", "Output tokens"],
  ["total_tokens", "Total tokens"],
  ["metadata", "Metadata"],
  ["correlation", "Correlation"],
  ["trace_id", "Trace ID"],
  ["namespace", "Namespace"],
  ["matches", "Matches"],
  ["fixture", "Fixture record"],
]);

const IDENTIFIER_KEY = /(?:^|_)(?:id|ids|hash|sha|digest|trace|namespace|model)(?:$|_)/i;
const ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$/;
const COMPLETE_FENCE = /^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$/i;
const EMBEDDED_FENCE = /```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```/gi;
const ANSWER_WRAPPERS = ["response_to_participant", "answer", "response"];
const GUIDED_ANSWER_KEYS = ["opening", "key_points", "steps", "warnings"];

/**
 * Hard safety bounds for one synchronous presentation pass.
 *
 * These are presentation bounds, never data-loss bounds. Large collections and
 * deep branches continue behind lazy native disclosures; a JSON string that is
 * too large or complex to parse synchronously remains literal text.
 */
export const STRUCTURED_LIMITS = Object.freeze({
  maxParseBytes: 524_288,
  maxParseDepth: 64,
  maxParseNodes: 250_000,
  maxRenderDepth: 8,
  maxInitialNodes: 800,
  childrenPerChunk: 24,
});

const MAX_ENCODED_JSON_LAYERS = 3;

function isStructured(value) {
  return value !== null && typeof value === "object";
}

function isPlainRecord(value) {
  return isStructured(value) && !Array.isArray(value);
}

function locale() {
  return document.documentElement.lang === "es" ? "es" : "en";
}

function byteLength(value) {
  if (typeof TextEncoder === "function") {
    return new TextEncoder().encode(value).length;
  }
  // A conservative fallback: UTF-16 code units never under-count ASCII by more
  // than this factor, and refusing an unusually large value leaves it visible as
  // literal text rather than dropping it.
  return value.length * 2;
}

function withinJsonShapeLimits(value) {
  let depth = 0;
  let nodes = 1;
  let quoted = false;
  let escaped = false;
  for (const character of value) {
    if (quoted) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === '"') {
        quoted = false;
      }
      continue;
    }
    if (character === '"') {
      quoted = true;
      continue;
    }
    if (character === "{" || character === "[") {
      depth += 1;
      nodes += 1;
      if (depth > STRUCTURED_LIMITS.maxParseDepth) return false;
    } else if (character === "}" || character === "]") {
      depth -= 1;
      if (depth < 0) return false;
    } else if (character === "," || character === ":") {
      nodes += 1;
      if (nodes > STRUCTURED_LIMITS.maxParseNodes) return false;
    }
  }
  return depth === 0 && !quoted;
}

function unfenced(value) {
  const match = value.match(COMPLETE_FENCE);
  if (match !== null) return match[1].trim();

  // Some upstream conversation records contain a Markdown opening marker but
  // no closing marker (for example, ```{"responseSource": ...}). Recover that
  // one production shape without turning general unfinished Markdown into a
  // parser. The caller still applies the byte/shape bounds and JSON.parse, and
  // this branch accepts only a remainder that begins as an object or array.
  if (!value.startsWith("```")) return value;
  let remainder = value.slice(3);
  if (/^json(?=[ \t]*(?:\r?\n|[\[{]))/i.test(remainder)) {
    remainder = remainder.slice(4);
  }
  remainder = remainder.trim();
  return remainder.startsWith("{") || remainder.startsWith("[")
    ? remainder
    : value;
}

/** Parse only a complete, bounded JSON object/array, including encoded layers. */
export function parseStructuredText(value) {
  if (isStructured(value)) {
    return value;
  }
  if (typeof value !== "string") {
    return null;
  }
  let candidate = unfenced(value.trim());
  for (let layer = 0; layer < MAX_ENCODED_JSON_LAYERS; layer += 1) {
    const container =
      (candidate.startsWith("{") && candidate.endsWith("}")) ||
      (candidate.startsWith("[") && candidate.endsWith("]"));
    const encoded = candidate.startsWith('"') && candidate.endsWith('"');
    if ((!container && !encoded) ||
        byteLength(candidate) > STRUCTURED_LIMITS.maxParseBytes ||
        !withinJsonShapeLimits(candidate)) {
      return null;
    }
    try {
      const parsed = JSON.parse(candidate);
      if (isStructured(parsed)) return parsed;
      if (typeof parsed !== "string") return null;
      candidate = unfenced(parsed.trim());
    } catch {
      return null;
    }
  }
  return null;
}

function appendTextToken(tokens, value) {
  if (value === "") return;
  const previous = tokens[tokens.length - 1];
  if (previous?.kind === "text") {
    previous.value += value;
  } else {
    tokens.push({ kind: "text", value });
  }
}

/**
 * Split prose around complete fenced JSON blocks without interpreting Markdown.
 * Invalid fences, HTML-looking text and every non-fence character remain text.
 */
export function tokenizeStructuredText(value) {
  if (isStructured(value)) return [{ kind: "structured", value }];
  const text = String(value ?? "");
  const whole = parseStructuredText(text);
  if (whole !== null) return [{ kind: "structured", value: whole }];

  const tokens = [];
  let cursor = 0;
  EMBEDDED_FENCE.lastIndex = 0;
  for (let match = EMBEDDED_FENCE.exec(text); match !== null; match = EMBEDDED_FENCE.exec(text)) {
    const parsed = parseStructuredText(match[0]);
    appendTextToken(tokens, text.slice(cursor, match.index));
    if (parsed === null) {
      appendTextToken(tokens, match[0]);
    } else {
      tokens.push({ kind: "structured", value: parsed });
    }
    cursor = match.index + match[0].length;
  }
  appendTextToken(tokens, text.slice(cursor));
  return tokens.length > 0 ? tokens : [{ kind: "text", value: text }];
}

function humanizeKey(key) {
  const exact = String(key);
  const known = LABELS.get(exact);
  if (known !== undefined) {
    return known;
  }
  const words = exact
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim();
  if (words === "") {
    return "Unnamed field";
  }
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`;
}

function escapedPathSegment(value) {
  return String(value).replace(/~/g, "~0").replace(/\//g, "~1");
}

function childPath(path, key) {
  return `${path}/${escapedPathSegment(key)}`;
}

function fieldKey(key) {
  return el("code", {
    className: "structured-key",
    text: String(key),
    attrs: { "data-field-key": "", translate: "no" },
  });
}

function labelNode(key, { suffix = "" } = {}) {
  const label = el("span", { className: "data-label" });
  label.appendChild(el("span", { text: humanizeKey(key) }));
  if (suffix !== "") {
    label.appendChild(el("span", { className: "data-count", text: suffix }));
  }
  label.appendChild(fieldKey(key));
  return label;
}

function scalarClass(value, key) {
  if (value === null || value === undefined || value === "") {
    return "data-value data-value--absent";
  }
  if (typeof value === "string" && (value.length > 120 || value.includes("\n"))) {
    return "data-value data-value--prose";
  }
  if (IDENTIFIER_KEY.test(String(key))) {
    return "data-value data-value--token identifier-value";
  }
  return "data-value";
}

function formattedNumber(value) {
  return new Intl.NumberFormat(locale(), {
    maximumFractionDigits: 20,
    useGrouping: true,
  }).format(value);
}

function scalarText(value) {
  if (value === null || value === undefined) {
    return "Not recorded";
  }
  if (value === "") {
    return "Empty text";
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? formattedNumber(value) : String(value);
  }
  return String(value);
}

function scalarValue(value, {
  path,
  key = "",
  ordered = false,
  derivedText = "",
  recordedKind = "",
} = {}) {
  const attrs = {
    "data-field-path": path,
  };
  const isPresentationText =
    derivedText !== "" || value === null || value === undefined || value === "" ||
    typeof value === "boolean";
  if (isPresentationText) {
    attrs["data-recorded-value"] = value === null || value === undefined
      ? recordedKind || "null"
      : String(value);
  } else {
    attrs["data-audit-value"] = "";
    attrs.translate = "no";
  }
  if (typeof value === "number") {
    attrs["data-audit-number"] = "";
    attrs["data-raw-value"] = String(value);
  }
  if (value === null || value === undefined || value === "" || derivedText !== "") {
    attrs["data-absent"] = "true";
  }
  const wrap = el(ordered ? "div" : "span", {
    className: scalarClass(value, key),
    attrs,
  });
  if (derivedText !== "") {
    wrap.textContent = derivedText;
  } else if (typeof value === "string" && ISO_TIME.test(value) && !Number.isNaN(Date.parse(value))) {
    wrap.appendChild(timeElement(value));
    wrap.appendChild(el("code", {
      className: "data-exact-value",
      text: value,
      attrs: { "data-audit-value": "", translate: "no" },
    }));
  } else if (typeof value === "number" && Number.isFinite(value)) {
    const displayed = formattedNumber(value);
    wrap.textContent = displayed;
    if (String(value).toLowerCase().includes("e") ||
        (value !== 0 && displayed === formattedNumber(0))) {
      wrap.appendChild(el("code", {
        className: "data-exact-value",
        text: String(value),
        attrs: { "data-audit-value": "", translate: "no" },
      }));
    }
  } else {
    wrap.textContent = scalarText(value);
  }
  return wrap;
}

function emptyCollection(path, kind) {
  return scalarValue(null, {
    path,
    derivedText: kind === "array" ? "None recorded" : "No fields recorded",
    recordedKind: `empty-${kind}`,
  });
}

function countText(value) {
  const count = Array.isArray(value) ? value.length : Object.keys(value).length;
  const noun = Array.isArray(value)
    ? count === 1 ? "item" : "items"
    : count === 1 ? "field" : "fields";
  return `${formattedNumber(count)} ${noun}`;
}

function normalizedValue(value) {
  return typeof value === "string" ? parseStructuredText(value) ?? value : value;
}

function groupName(key, value, { verb = "Show" } = {}) {
  return `${humanizeKey(key)} — ${verb.toLowerCase()} ${countText(value)}`;
}

function renderContext() {
  return { remaining: STRUCTURED_LIMITS.maxInitialNodes };
}

function mountDisclosureBody(details, makeBody) {
  let mounted = false;
  const mount = () => {
    if (mounted || !details.open) return;
    mounted = true;
    details.appendChild(makeBody());
  };
  details.addEventListener("toggle", mount);
  mount();
}

function lazyDisclosure(name, makeBody, {
  className = "structured-group structured-group--lazy",
  attrs = {},
  open = false,
} = {}) {
  const details = el("details", {
    className,
    attrs: { ...attrs, "data-lazy-structured": "true", open },
  });
  details.appendChild(el("summary", {
    className: "structured-group-summary",
    text: name,
    attrs: { "aria-label": name },
  }));
  mountDisclosureBody(details, makeBody);
  return details;
}

function deferredValue(value, { path, key, reason }) {
  const depth = reason === "depth";
  const name = `Continue into ${humanizeKey(key)} — ${countText(value)}`;
  return lazyDisclosure(
    name,
    () => renderValue(value, { path, key, depth: 0, context: renderContext() }),
    {
      attrs: {
        "data-field-group": path,
        "data-depth-boundary": depth ? "true" : null,
        "data-node-boundary": depth ? null : "true",
      },
    }
  );
}

function detailsFor(key, value, path, {
  open = false,
  repeatLabel = true,
  depth = 0,
  context = renderContext(),
} = {}) {
  const name = groupName(key, value);
  const details = el("details", {
    className: "structured-group",
    attrs: { "data-field-group": path, "data-lazy-structured": "true", open },
  });
  const summary = el("summary", {
    className: "structured-group-summary",
    attrs: { "aria-label": name },
  });
  if (repeatLabel) {
    summary.appendChild(labelNode(key, { suffix: countText(value) }));
  } else {
    summary.appendChild(el("span", { text: name }));
  }
  details.appendChild(summary);
  mountDisclosureBody(
    details,
    () => renderValue(value, { path, key, depth, context })
  );
  return details;
}

function arraySlice(values, path, key, {
  start = 0,
  end = values.length,
  depth = 0,
  context = renderContext(),
} = {}) {
  const normalized = values.slice(start, end).map(normalizedValue);
  const structured = normalized.some(isStructured);
  const listOptions = {
    className: structured
      ? "structured-list structured-list--records"
      : "structured-list",
    attrs: structured && start > 0 ? { start: String(start + 1) } : {},
  };
  const list = structured ? el("ol", listOptions) : el("ul", listOptions);
  replaceChildren(
    list,
    normalized.map((value, relativeIndex) => {
      const index = start + relativeIndex;
      const itemPath = childPath(path, index);
      const item = el("li", { className: isStructured(value) ? "structured-record" : "" });
      if (isStructured(value)) {
        const name = `Item ${index + 1} — ${countText(value)}`;
        const group = el("details", {
          className: "structured-group structured-group--item",
          attrs: { "data-field-group": itemPath, "data-lazy-structured": "true" },
        });
        group.appendChild(el("summary", {
          className: "structured-group-summary",
          text: `Item ${index + 1}`,
          attrs: { "aria-label": name },
        }));
        mountDisclosureBody(
          group,
          () => renderValue(value, {
            path: itemPath,
            key: String(index),
            depth: depth + 1,
            context,
          })
        );
        item.appendChild(group);
      } else {
        item.appendChild(scalarValue(value, { path: itemPath, key, ordered: true }));
      }
      return item;
    })
  );
  return list;
}

function arrayChunkDisclosures(values, path, key, startAt = 0, endAt = values.length) {
  const wrap = el("div", {
    className: "structured-data structured-chunks structured-chunks--array",
    attrs: { "data-structured-chunks": String(endAt - startAt) },
  });
  const chunks = [];
  for (let start = startAt; start < endAt; start += STRUCTURED_LIMITS.childrenPerChunk) {
    const end = Math.min(endAt, start + STRUCTURED_LIMITS.childrenPerChunk);
    const name = `Items ${start + 1}–${end} of ${values.length} in ${humanizeKey(key)}`;
    chunks.push(lazyDisclosure(
      name,
      () => arraySlice(values, path, key, {
        start,
        end,
        depth: 0,
        context: renderContext(),
      }),
      { attrs: { "data-chunk-start": String(start), "data-chunk-end": String(end) } }
    ));
  }
  replaceChildren(wrap, chunks);
  return wrap;
}

function chunkedArray(values, path, key) {
  const pageSize = STRUCTURED_LIMITS.childrenPerChunk ** 2;
  if (values.length <= pageSize) {
    return arrayChunkDisclosures(values, path, key);
  }
  const wrap = el("div", {
    className: "structured-data structured-chunks structured-chunks--array structured-chunks--pages",
    attrs: { "data-structured-chunks": String(values.length) },
  });
  const pages = [];
  for (let start = 0; start < values.length; start += pageSize) {
    const end = Math.min(values.length, start + pageSize);
    const name = `Items ${start + 1}–${end} of ${values.length} in ${humanizeKey(key)}`;
    pages.push(lazyDisclosure(
      name,
      () => arrayChunkDisclosures(values, path, key, start, end),
      {
        className: "structured-group structured-group--lazy structured-group--page",
        attrs: { "data-page-start": String(start), "data-page-end": String(end) },
      }
    ));
  }
  replaceChildren(wrap, pages);
  return wrap;
}

function objectRows(value, path, {
  entries = Object.entries(value),
  depth = 0,
  context = renderContext(),
} = {}) {
  const list = el("dl", { className: "structured-data-list" });
  const rows = [];
  for (const [key, recorded] of entries) {
    const itemPath = childPath(path, key);
    const shown = normalizedValue(recorded);
    if (isStructured(shown)) {
      const row = el("div", { className: "data-row data-row--group" });
      const term = el("dt");
      term.appendChild(labelNode(key, { suffix: countText(shown) }));
      row.appendChild(term);
      const description = el("dd");
      description.appendChild(detailsFor(key, shown, itemPath, {
        repeatLabel: false,
        depth: depth + 1,
        context,
      }));
      row.appendChild(description);
      rows.push(row);
      continue;
    }
    const row = el("div", { className: "data-row" });
    const term = el("dt");
    term.appendChild(labelNode(key));
    row.appendChild(term);
    const description = el("dd");
    description.appendChild(scalarValue(shown, { path: itemPath, key }));
    row.appendChild(description);
    rows.push(row);
  }
  replaceChildren(list, rows);
  return list;
}

function objectChunkDisclosures(value, entries, path, key, startAt = 0, endAt = entries.length) {
  const wrap = el("div", {
    className: "structured-data structured-chunks structured-chunks--object",
    attrs: { "data-structured-chunks": String(endAt - startAt) },
  });
  const chunks = [];
  for (let start = startAt; start < endAt; start += STRUCTURED_LIMITS.childrenPerChunk) {
    const end = Math.min(endAt, start + STRUCTURED_LIMITS.childrenPerChunk);
    const name = `Fields ${start + 1}–${end} of ${entries.length} in ${humanizeKey(key)}`;
    chunks.push(lazyDisclosure(
      name,
      () => objectRows(value, path, {
        entries: entries.slice(start, end),
        depth: 0,
        context: renderContext(),
      }),
      { attrs: { "data-chunk-start": String(start), "data-chunk-end": String(end) } }
    ));
  }
  replaceChildren(wrap, chunks);
  return wrap;
}

function chunkedObject(value, path, key) {
  const entries = Object.entries(value);
  const pageSize = STRUCTURED_LIMITS.childrenPerChunk ** 2;
  if (entries.length <= pageSize) {
    return objectChunkDisclosures(value, entries, path, key);
  }
  const wrap = el("div", {
    className: "structured-data structured-chunks structured-chunks--object structured-chunks--pages",
    attrs: { "data-structured-chunks": String(entries.length) },
  });
  const pages = [];
  for (let start = 0; start < entries.length; start += pageSize) {
    const end = Math.min(entries.length, start + pageSize);
    const name = `Fields ${start + 1}–${end} of ${entries.length} in ${humanizeKey(key)}`;
    pages.push(lazyDisclosure(
      name,
      () => objectChunkDisclosures(value, entries, path, key, start, end),
      {
        className: "structured-group structured-group--lazy structured-group--page",
        attrs: { "data-page-start": String(start), "data-page-end": String(end) },
      }
    ));
  }
  replaceChildren(wrap, pages);
  return wrap;
}

function renderValue(value, {
  path = "$",
  key = "value",
  depth = 0,
  context = renderContext(),
} = {}) {
  const shown = normalizedValue(value);
  if (isStructured(shown)) {
    if (depth >= STRUCTURED_LIMITS.maxRenderDepth) {
      return deferredValue(shown, { path, key, reason: "depth" });
    }
    if (context.remaining <= 0) {
      return deferredValue(shown, { path, key, reason: "nodes" });
    }
    context.remaining -= 1;
  }
  if (Array.isArray(shown)) {
    if (shown.length === 0) return emptyCollection(path, "array");
    if (shown.length > STRUCTURED_LIMITS.childrenPerChunk) {
      return chunkedArray(shown, path, key);
    }
    return arraySlice(shown, path, key, { depth, context });
  }
  if (isPlainRecord(shown)) {
    const size = Object.keys(shown).length;
    if (size === 0) return emptyCollection(path, "object");
    if (size > STRUCTURED_LIMITS.childrenPerChunk) {
      return chunkedObject(shown, path, key);
    }
    return objectRows(shown, path, { depth, context });
  }
  return scalarValue(shown, { path, key });
}

/** Render every authorized leaf of a structured value without JSON syntax. */
export function renderStructuredData(value, {
  label = "Recorded details",
  path = "$",
  open = false,
} = {}) {
  const recorded = normalizedValue(value);
  const root = el("div", {
    className: "structured-data",
    attrs: { "data-structured-root": path },
  });
  if (label !== "") {
    root.appendChild(el("p", { className: "structured-data-title", text: label }));
  }
  if (isStructured(recorded)) {
    if (label !== "" && (Array.isArray(recorded) ? recorded.length : Object.keys(recorded).length) > 4) {
      const name = `${label} — ${countText(recorded)}`;
      const disclosure = lazyDisclosure(
        name,
        () => renderValue(recorded, { path, key: label, context: renderContext() }),
        {
          className: "structured-group structured-group--root",
          attrs: { "data-field-group": path },
          open,
        }
      );
      root.appendChild(disclosure);
    } else {
      root.appendChild(renderValue(recorded, { path, key: label, context: renderContext() }));
    }
  } else {
    root.appendChild(renderValue(recorded, { path, key: label, context: renderContext() }));
  }
  return root;
}

function answerFieldHeading(label, key) {
  const heading = el("h4", { className: "answer-section-heading" });
  heading.appendChild(el("span", { text: label }));
  heading.appendChild(fieldKey(key));
  return heading;
}

function hasGuidedField(value) {
  return isPlainRecord(value) && GUIDED_ANSWER_KEYS.some((key) => Object.hasOwn(value, key));
}

function locateGuidedAnswer(value, {
  path = "$/answer",
  wrappers = [],
  seen = new WeakSet(),
} = {}) {
  const current = normalizedValue(value);
  if (!isPlainRecord(current) || seen.has(current)) return null;
  if (hasGuidedField(current)) return { record: current, path, wrappers };
  seen.add(current);
  for (const key of ANSWER_WRAPPERS) {
    if (!Object.hasOwn(current, key)) continue;
    const found = locateGuidedAnswer(current[key], {
      path: childPath(path, key),
      wrappers: [...wrappers, key],
      seen,
    });
    if (found !== null) return found;
  }
  return null;
}

function hasRecordedContent(value) {
  if (Array.isArray(value)) return value.length > 0;
  if (isPlainRecord(value)) return Object.keys(value).length > 0;
  return value !== undefined;
}

function remainingAnswer(value, wrappers, consumed) {
  const current = normalizedValue(value);
  if (!isPlainRecord(current)) return current;
  if (wrappers.length === 0) {
    return Object.fromEntries(
      Object.entries(current).filter(([key]) => !consumed.has(key))
    );
  }
  const [wrapper, ...rest] = wrappers;
  const entries = [];
  for (const [key, recorded] of Object.entries(current)) {
    if (key !== wrapper) {
      entries.push([key, recorded]);
      continue;
    }
    const nested = remainingAnswer(recorded, rest, consumed);
    if (hasRecordedContent(nested)) entries.push([key, nested]);
  }
  return Object.fromEntries(entries);
}

function appendAnswerValue(parent, value, { path, key, className = "" }) {
  const node = renderValue(value, { path, key, context: renderContext() });
  if (className !== "") node.classList.add(className);
  parent.appendChild(node);
  return node;
}

function answerWrapperContext(wrappers, basePath) {
  const wrap = el("div", { className: "answer-wrapper-context" });
  let path = basePath;
  for (const key of wrappers) {
    path = childPath(path, key);
    const row = el("p", {
      className: "structured-data-title answer-wrapper-label",
      attrs: { "data-field-path": path },
    });
    row.appendChild(labelNode(key));
    wrap.appendChild(row);
  }
  return wrap;
}

function answerStepField(item, step, key, itemPath, className) {
  if (!Object.hasOwn(step, key)) return false;
  const block = el("div", { className });
  block.appendChild(labelNode(key));
  block.appendChild(renderValue(step[key], {
    path: childPath(itemPath, key),
    key,
    context: renderContext(),
  }));
  item.appendChild(block);
  return true;
}

function answerSteps(value, path) {
  const recorded = normalizedValue(value);
  if (!Array.isArray(recorded) || recorded.length > STRUCTURED_LIMITS.childrenPerChunk) {
    return renderValue(recorded, { path, key: "steps", context: renderContext() });
  }
  if (recorded.length === 0) return emptyCollection(path, "array");

  const list = el("ol", { className: "answer-steps" });
  replaceChildren(
    list,
    recorded.map((rawStep, index) => {
      const step = normalizedValue(rawStep);
      const itemPath = childPath(path, index);
      const attrs = {};
      if (isPlainRecord(step) && Object.hasOwn(step, "step_number")) {
        attrs["data-has-recorded-step-number"] = "true";
        if (Number.isInteger(step.step_number)) {
          attrs.value = String(step.step_number);
          attrs["data-recorded-step-number"] = String(step.step_number);
        }
      }
      const item = el("li", { className: "answer-step", attrs });
      if (!isPlainRecord(step)) {
        item.appendChild(renderValue(step, {
          path: itemPath,
          key: "step",
          context: renderContext(),
        }));
        return item;
      }

      const consumed = new Set();
      if (Object.hasOwn(step, "step_number")) {
        const number = el("div", { className: "answer-step-number structured-badge" });
        number.appendChild(labelNode("step_number"));
        number.appendChild(renderValue(step.step_number, {
          path: childPath(itemPath, "step_number"),
          key: "step_number",
          context: renderContext(),
        }));
        item.appendChild(number);
        consumed.add("step_number");
      }
      if (answerStepField(item, step, "action", itemPath, "answer-step-action")) {
        consumed.add("action");
      }
      if (answerStepField(item, step, "detail", itemPath, "answer-step-detail")) {
        consumed.add("detail");
      }

      const additional = Object.fromEntries(
        Object.entries(step).filter(([key]) => !consumed.has(key))
      );
      if (Object.keys(additional).length > 0) {
        item.appendChild(renderStructuredData(additional, {
          label: "Additional step details",
          path: itemPath,
        }));
      }
      return item;
    })
  );
  return list;
}

function proseFallback(value, {
  absent,
  empty,
  path,
}) {
  const wrap = el("div", { className: "structured-answer structured-answer--text" });
  const missing = value === null || value === undefined;
  const emptyRecorded = value === "";
  const text = missing ? absent : emptyRecorded ? empty : String(value);
  const attrs = { "data-field-path": path };
  if (missing) {
    attrs["data-absent"] = "true";
    attrs["data-recorded-value"] = value === null || value === undefined ? "null" : "";
  } else if (emptyRecorded) {
    attrs["data-empty-recorded"] = "true";
    attrs["data-recorded-value"] = "";
  } else {
    attrs["data-audit-value"] = "";
    attrs.translate = "no";
  }
  const blocks = text.split(/\n\s*\n/).map((block) => block.trim()).filter(Boolean);
  replaceChildren(
    wrap,
    (blocks.length > 0 ? blocks : [absent]).map((block) =>
      el("p", {
        className: "answer-opening data-value data-value--prose",
        text: block,
        attrs,
      })
    )
  );
  return wrap;
}

/** Render the participant answer as prose, points, ordered steps and cautions. */
export function renderGeneratedAnswer(value, {
  absent = "No generated answer was recorded.",
  empty = "An empty generated answer was recorded.",
  path = "$/answer",
} = {}) {
  if (typeof value === "string") {
    const tokens = tokenizeStructuredText(value);
    const mixed = tokens.length > 1 && tokens.some((token) => token.kind === "structured");
    if (mixed) {
      const wrap = el("div", { className: "structured-answer structured-answer--mixed" });
      replaceChildren(
        wrap,
        tokens.map((token, index) => {
          const tokenPath = childPath(childPath(path, "segments"), index);
          return token.kind === "structured"
            ? renderGeneratedAnswer(token.value, { absent, empty, path: tokenPath })
            : proseFallback(token.value, { absent, empty, path: tokenPath });
        })
      );
      return wrap;
    }
  }
  const recorded = normalizedValue(value);
  if (isStructured(recorded) && !isPlainRecord(recorded)) {
    return renderStructuredData(recorded, { label: "Recorded answer details", path });
  }
  if (!isPlainRecord(recorded)) {
    return proseFallback(recorded, { absent, empty, path });
  }
  const located = locateGuidedAnswer(recorded, { path });
  if (located === null) {
    return renderStructuredData(recorded, { label: "Recorded answer details", path });
  }

  const wrap = el("section", { className: "structured-answer" });
  if (located.wrappers.length > 0) {
    wrap.appendChild(answerWrapperContext(located.wrappers, path));
  }
  const guided = located.record;
  const consumed = new Set();
  if (Object.hasOwn(guided, "opening")) {
    wrap.appendChild(answerFieldHeading("Opening", "opening"));
    appendAnswerValue(wrap, guided.opening, {
      path: childPath(located.path, "opening"),
      key: "opening",
      className: "answer-opening",
    });
    consumed.add("opening");
  }
  if (Object.hasOwn(guided, "key_points")) {
    wrap.appendChild(answerFieldHeading("Key points", "key_points"));
    appendAnswerValue(wrap, guided.key_points, {
      path: childPath(located.path, "key_points"),
      key: "key_points",
      className: "answer-key-points",
    });
    consumed.add("key_points");
  }
  if (Object.hasOwn(guided, "steps")) {
    wrap.appendChild(answerFieldHeading("Recommended steps", "steps"));
    wrap.appendChild(answerSteps(guided.steps, childPath(located.path, "steps")));
    consumed.add("steps");
  }
  if (Object.hasOwn(guided, "warnings")) {
    wrap.appendChild(answerFieldHeading("Warnings", "warnings"));
    const callout = el("div", {
      className: "answer-warnings audit-callout",
      attrs: { "data-tone": "warning" },
    });
    callout.appendChild(renderValue(guided.warnings, {
      path: childPath(located.path, "warnings"),
      key: "warnings",
      context: renderContext(),
    }));
    wrap.appendChild(callout);
    consumed.add("warnings");
  }

  const additional = remainingAnswer(recorded, located.wrappers, consumed);
  if (hasRecordedContent(additional) && Object.keys(additional).length > 0) {
    const disclosure = el("details", { className: "structured-group answer-additional" });
    disclosure.appendChild(el("summary", {
      className: "structured-group-summary",
      text: "Additional answer details",
      attrs: { "aria-label": "Additional answer details" },
    }));
    disclosure.appendChild(renderStructuredData(additional, {
      label: "",
      path,
    }));
    wrap.appendChild(disclosure);
  }
  return wrap;
}
