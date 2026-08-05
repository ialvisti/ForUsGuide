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
function authorName(message) {
  const actor = message.actor ?? null;
  if (actor === null) {
    return message.actor_class === "event" ? "Ticket system" : "Author not recorded";
  }
  const name = actor.display_name ?? "";
  return name === "" ? "Author not recorded" : name;
}

function pill(text, attrs) {
  return el("span", { className: "pill", text, attrs });
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
  const item = el("li", {
    className: "entry",
    attrs: {
      "data-entry": "conversation",
      "data-entry-id": message.entry_id ?? "",
      "data-actor": actorClass,
      "data-internal": internal ? "true" : "false",
      "data-kind": message.kind ?? "unsupported",
    },
  });

  const head = el("div", { className: "entry-head" });
  head.appendChild(el("span", { className: "entry-author", text: authorName(message) }));
  head.appendChild(
    pill(actorClassLabel(actorClass), { "data-actor-class": actorClass })
  );
  // Every entry carries a visibility badge, including the ones where it is
  // obvious: "obvious" is exactly the case a reviewer stops checking.
  head.appendChild(
    pill(visibilityLabel(message.visibility ?? "private"), {
      "data-visibility": message.visibility ?? "private",
    })
  );
  if (internal) {
    // Said in words, not only by the hatched background, and never phrased as
    // anything a participant saw.
    head.appendChild(pill("Not shown to the participant", { "data-internal": "true" }));
  } else if (message.participant_facing === true) {
    head.appendChild(pill("Participant saw this", { "data-participant": "true" }));
  }
  if (message.created_at) {
    const time = el("span", { className: "entry-time" });
    time.appendChild(timeElement(message.created_at));
    head.appendChild(time);
  }
  item.appendChild(head);

  if (message.in_reply_to) {
    item.appendChild(
      el("p", { className: "entry-meta", text: `In reply to entry ${message.in_reply_to}` })
    );
  }

  if (actorClass === "event" || message.kind === "change_event") {
    // A change event is summarized and kept visually apart from messages. It has
    // no body and no author by construction, so rendering it in the same shape as
    // a reply would invent both.
    item.appendChild(
      el("p", {
        className: "entry-body",
        text: message.change_summary || "A change was recorded with no summary.",
      })
    );
    return item;
  }

  if (message.rendering === "text" && message.body) {
    const long = String(message.body).length > BODY_COLLAPSE_LIMIT;
    const body = el("div", {
      className: "entry-body",
      attrs: { "data-collapsed": long && !expanded ? "true" : "false" },
    });
    replaceChildren(body, paragraphs(message.body));
    item.appendChild(body);
    if (long) {
      item.appendChild(
        button({
          text: expanded ? "Show less" : "Show the whole message",
          className: "link-button",
          dataset: { action: "toggle-entry", entryId: message.entry_id ?? "" },
        })
      );
    }
  } else {
    const body = el("p", { className: "entry-body", text: placeholderText(message) });
    item.appendChild(body);
    if (message.unsupported_type) {
      item.appendChild(
        el("p", {
          className: "entry-meta",
          text: `Upstream entry type: ${message.unsupported_type}`,
        })
      );
    }
    // The identifier, not the payload. It is what a support request can be
    // filed against; the raw object is not the reviewer's problem to read.
    item.appendChild(
      el("p", { className: "entry-meta", text: `Entry ${message.entry_id ?? "unknown"}` })
    );
  }

  if (message.body_length > 0 && message.rendering !== "text") {
    item.appendChild(
      el("p", {
        className: "entry-meta",
        text: `The upstream body was ${message.body_length} characters.`,
      })
    );
  }
  return item;
}

/** The whole visible conversation, or the state that stands in for it. */
export function renderConversation(list, messages, { expanded = new Set(), filter = "all" } = {}) {
  if (messages.length === 0) {
    const item = el("li", { className: "entry", attrs: { "data-entry": "empty" } });
    item.appendChild(
      el("p", {
        className: "entry-body",
        text:
          filter === "all"
            ? "No conversation entries have loaded for this ticket."
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
