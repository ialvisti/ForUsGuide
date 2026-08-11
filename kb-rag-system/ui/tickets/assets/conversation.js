/**
 * The conversation panel.
 *
 * One rule shapes every line of this module: **the five kinds of author are
 * never conflated.** A participant wrote something the customer can see; a human
 * agent replied; the assistant answered; the ticket system recorded a change;
 * and an entry whose author could not be decided is *unclassified*, not a guess.
 * The server decides which of those an entry is, from configured identities and
 * upstream actor types — never from a display name, because a Rev user may be
 * called "Support Bot" and an automation may be called by a person's name.
 *
 * So this module reads `actor_class`, `internal` and `participant_facing` and
 * renders what they say. It never derives one from another, and it never labels
 * an internal note as participant-facing, which is the one mistake on this panel
 * that can end up quoted to a customer.
 *
 * The second rule is that a body is text. `rendering` is either `text` — a plain
 * body the server was willing to pass through — or `placeholder`, in which case
 * there is no body at all and the reason is shown instead. Nothing here ever
 * receives remote markup, so nothing here has to decide whether to trust it.
 */

import {
  BODY_COLLAPSE_LIMIT,
  actorClassLabel,
  button,
  el,
  hiddenText,
  paragraphs,
  replaceChildren,
  timeElement,
  visibilityLabel,
} from "./render.js";
import {
  parseStructuredText,
  renderStructuredData,
  tokenizeStructuredText,
} from "./structured.js";

/** Why a body was withheld, in words. Unknown reasons are shown as given. */
const PLACEHOLDER_REASONS = new Map([
  ["unsupported_body_type", "the upstream body was not plain text"],
  ["unsupported_entry_type", "this entry type is not modelled"],
  ["body_absent", "the entry carried no body"],
  ["body_too_long", "the body exceeded the bounded length"],
]);

function placeholderText(message) {
  const reason = message.placeholder_reason ?? "";
  const described = PLACEHOLDER_REASONS.get(reason);
  if (described !== undefined) {
    return `No body is shown here because ${described}.`;
  }
  if (reason !== "") {
    return `No body is shown here. Reported reason: ${reason}.`;
  }
  return "No body is shown here.";
}

/**
 * The author's name, or an honest stand-in.
 *
 * A display name is decoration on this panel: it is shown because it helps a
 * reviewer follow a thread, and it is never what decided the badge beside it.
 */
function authorPresentation(message) {
  const actor = message.actor ?? null;
  if (actor === null) {
    return {
      text: message.actor_class === "event" ? "Ticket system" : "Author not recorded",
      recorded: false,
    };
  }
  const name = actor.display_name ?? "";
  return name === ""
    ? { text: "Author not recorded", recorded: false }
    : { text: name, recorded: true };
}

function protectedRemoteText(value) {
  return el("span", {
    text: value,
    attrs: { "data-user-content": "", translate: "no" },
  });
}

function metadataWithRemoteValue(label, value) {
  const row = el("p", { className: "entry-meta" });
  row.appendChild(el("span", { text: label }));
  row.appendChild(protectedRemoteText(value));
  return row;
}

function bodyId(entryId) {
  const raw = String(entryId ?? "unknown");
  const slug = raw.replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 48)
    || "unknown";
  let hash = 2166136261;
  for (const character of raw) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return `conversation-body-${slug}-${hash.toString(16).padStart(8, "0")}`;
}

function messageTokens(value) {
  const whole = parseStructuredText(value);
  return whole === null
    ? tokenizeStructuredText(value)
    : [{ kind: "structured", value: whole }];
}

function messageBodyNodes(tokens, entryId) {
  const nodes = [];
  let structuredIndex = 0;
  for (const token of tokens) {
    if (token.kind === "structured") {
      nodes.push(renderStructuredData(token.value, {
        label: "Structured message",
        path: `$/conversation/${entryId ?? "unknown"}/structured/${structuredIndex}`,
        open: false,
      }));
      structuredIndex += 1;
    } else {
      nodes.push(...paragraphs(token.value));
    }
  }
  return nodes;
}

function messageSide(actorClass) {
  return actorClass === "participant" ? "outgoing" : "incoming";
}

function avatarText(author, actorClass) {
  if (author.recorded) {
    const words = author.text.trim().split(/\s+/u).filter(Boolean);
    const initials = words.slice(0, 2).map((word) => Array.from(word)[0] ?? "").join("");
    if (initials !== "") {
      return initials.toUpperCase();
    }
  }
  return new Map([
    ["participant", "P"],
    ["human_agent", "A"],
    ["ai_or_system", "AI"],
    ["unknown", "?"],
  ]).get(actorClass) ?? "?";
}

function audiencePresentation(message, internal) {
  if (internal) {
    return {
      text: "Internal · not shown to participant",
      attrs: { "data-internal": "true" },
    };
  } else if (message.participant_facing === true) {
    return {
      text: "Participant-visible",
      attrs: { "data-participant": "true" },
    };
  }
  return { text: visibilityLabel(message.visibility ?? "private"), attrs: {} };
}

function labelledBadge(className, text, attrs, glyphClass) {
  const badge = el("span", { className, attrs });
  badge.appendChild(el("span", {
    className: glyphClass,
    attrs: { "aria-hidden": "true" },
  }));
  badge.appendChild(el("span", { text }));
  return badge;
}

function audienceNode(message, internal) {
  const audience = audiencePresentation(message, internal);
  return labelledBadge(
    "message-audience",
    audience.text,
    {
      "data-visibility": message.visibility ?? "private",
      ...audience.attrs,
    },
    "message-audience-glyph",
  );
}

function eventEntry(message, item) {
  const internal = message.internal === true;
  item.className = "chat-event";
  const row = el("div", { className: "activity-row" });
  row.appendChild(labelledBadge(
    "message-role",
    actorClassLabel("event"),
    { "data-actor-class": "event" },
    "message-role-glyph",
  ));
  const hasRecordedSummary = Boolean(message.change_summary);
  row.appendChild(el("span", {
    className: "entry-body",
    text: hasRecordedSummary
      ? message.change_summary
      : "A change was recorded with no summary.",
    attrs: hasRecordedSummary
      ? { "data-user-content": "", translate: "no" }
      : {},
  }));
  if (message.created_at) {
    const time = el("span", { className: "entry-time" });
    time.appendChild(timeElement(message.created_at));
    row.appendChild(time);
  }
  row.appendChild(audienceNode(message, internal));
  item.appendChild(row);
  return item;
}

function technicalDetails(message) {
  const details = el("details", { className: "message-technical" });
  details.appendChild(el("summary", { text: "Technical details" }));
  if (message.unsupported_type) {
    details.appendChild(
      metadataWithRemoteValue("Upstream entry type: ", message.unsupported_type)
    );
  }
  if (message.entry_id === null || message.entry_id === undefined) {
    details.appendChild(el("p", { className: "entry-meta", text: "Entry unknown" }));
  } else {
    details.appendChild(metadataWithRemoteValue("Entry ", message.entry_id));
  }
  if (message.body_length > 0) {
    details.appendChild(
      el("p", {
        className: "entry-meta",
        text: `The upstream body was ${message.body_length} characters.`,
      })
    );
  }
  return details;
}

/**
 * One conversation entry.
 *
 * `expanded` is passed in rather than held here, so the controller owns which
 * entries are open and a re-render does not silently collapse the one being read.
 */
export function conversationEntry(message, { expanded = false } = {}) {
  const actorClass = message.actor_class ?? "unknown";
  const internal = message.internal === true;
  const side = messageSide(actorClass);
  const item = el("li", {
    className: "chat-message",
    attrs: {
      "data-entry": "conversation",
      "data-entry-id": message.entry_id ?? "",
      "data-actor": actorClass,
      "data-internal": internal ? "true" : "false",
      "data-kind": message.kind ?? "unsupported",
      "data-side": side,
      ...(message.in_reply_to
        ? { "data-reply-entry-id": message.in_reply_to }
        : {}),
    },
  });

  if (actorClass === "event" || message.kind === "change_event") {
    return eventEntry(message, item);
  }

  const author = authorPresentation(message);
  const avatar = el("span", {
    className: "chat-avatar",
    text: avatarText(author, actorClass),
    attrs: {
      "aria-hidden": "true",
      ...(author.recorded ? { "data-user-content": "", translate: "no" } : {}),
    },
  });
  item.appendChild(avatar);

  const bubble = el("article", { className: "chat-bubble" });
  const head = el("header", { className: "entry-head" });
  head.appendChild(el("span", {
    className: "entry-author",
    text: author.text,
    attrs: author.recorded ? { "data-user-content": "", translate: "no" } : {},
  }));
  head.appendChild(labelledBadge(
    "message-role",
    actorClassLabel(actorClass),
    { "data-actor-class": actorClass },
    "message-role-glyph",
  ));
  if (message.created_at) {
    const time = el("span", { className: "entry-time" });
    time.appendChild(timeElement(message.created_at));
    head.appendChild(time);
  }
  bubble.appendChild(head);

  if (message.in_reply_to) {
    bubble.appendChild(el("p", {
      className: "reply-context",
      text: "Replying to an earlier message",
    }));
  }

  if (message.rendering === "text" && message.body) {
    const tokens = messageTokens(message.body);
    const hasStructured = tokens.some((token) => token.kind === "structured");
    // A CSS line clamp is safe only for inert prose. Structured bodies contain
    // native disclosures; clipping those would leave invisible controls in the
    // keyboard order, so their own progressive hierarchy does the collapsing.
    const long = !hasStructured && String(message.body).length > BODY_COLLAPSE_LIMIT;
    const controlledBodyId = bodyId(message.entry_id);
    const body = el("div", {
      className: hasStructured ? "entry-body entry-body--structured" : "entry-body",
      attrs: {
        id: controlledBodyId,
        "data-collapsed": long && !expanded ? "true" : "false",
      },
    });
    replaceChildren(body, messageBodyNodes(tokens, message.entry_id));
    bubble.appendChild(body);
    if (long) {
      const control = button({
        text: expanded ? "Show less" : "Show the whole message",
        className: "link-button",
        dataset: { action: "toggle-entry", entryId: message.entry_id ?? "" },
      });
      control.setAttribute("aria-expanded", expanded ? "true" : "false");
      control.setAttribute("aria-controls", controlledBodyId);
      bubble.appendChild(control);
    }
  } else {
    const body = el("p", { className: "entry-body", text: placeholderText(message) });
    bubble.appendChild(body);
    bubble.appendChild(technicalDetails(message));
  }

  bubble.appendChild(audienceNode(message, internal));
  item.appendChild(bubble);
  return item;
}

/** The whole visible conversation, or the state that stands in for it. */
export function renderConversation(list, messages, { expanded = new Set(), filter = "all" } = {}) {
  if (messages.length === 0) {
    const item = el("li", { className: "chat-empty", attrs: { "data-entry": "empty" } });
    item.appendChild(
      el("p", {
        className: "entry-body",
        text:
          filter === "messages"
            ? "No messages have loaded for this ticket."
            : "No entries on the pages loaded so far match this filter.",
      })
    );
    if (filter !== "all") {
      item.appendChild(
        el("p", {
          className: "entry-meta",
          text: "A later page may contain some; the filter applies to what is loaded.",
        })
      );
    }
    replaceChildren(list, [item]);
    return;
  }
  replaceChildren(
    list,
    messages.map((message) =>
      conversationEntry(message, { expanded: expanded.has(message.entry_id) })
    )
  );
}

/**
 * The one-line status above the conversation.
 *
 * The hard requirement is the last branch: while a forward cursor exists this
 * must never say the conversation is complete. "3 entries" on a ticket with
 * forty is how a reviewer concludes the assistant was never asked the question.
 */
export function conversationStatusText(feed, { shown, total, filter }) {
  const parts = [];
  if (feed.phase === "loading" && feed.pages === 0) {
    return { text: "Loading the conversation…", tone: "info" };
  }
  if (feed.phase === "error") {
    return {
      text: feed.error?.title ?? "The conversation could not be loaded.",
      tone: "error",
    };
  }
  let tone = "info";
  if (feed.phase === "stale") {
    tone = "warning";
    parts.push("Showing the pages already loaded; the newest request failed.");
  }
  if (feed.phase === "loading") {
    parts.push("Loading another page…");
  }
  if (feed.partial) {
    tone = "warning";
    parts.push("This page is incomplete.");
  }
  const more = typeof feed.nextCursor === "string" && feed.nextCursor !== "";
  const loaded = `${total} entr${total === 1 ? "y" : "ies"} loaded`;
  if (more) {
    tone = tone === "info" ? "warning" : tone;
    parts.push(`${loaded}; more remain.`);
  } else if (feed.pages > 0) {
    parts.push(`${loaded}; that is the whole conversation.`);
  }
  if (filter !== "all") {
    parts.push(`${shown} shown by this filter.`);
  }
  return { text: parts.join(" "), tone };
}

/** Screen-reader-only text naming what the filter is currently hiding. */
export function filterSummary(shown, total) {
  return hiddenText(`${shown} of ${total} loaded entries shown`);
}
