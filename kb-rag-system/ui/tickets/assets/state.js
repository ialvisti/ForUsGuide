/**
 * One store, one reducer, one shape.
 *
 * Two rules are structural rather than stylistic.
 *
 * **Nothing here persists.** No ticket title, no reviewer comment, no email
 * address, and no CSRF token is written anywhere that outlives the tab. The
 * store is a plain object in memory; when the page closes, the customer-linked
 * data it held is gone.
 *
 * **A page cursor is not a location.** `URL_FILTER_KEYS` is the complete list of
 * things allowed into the address bar, and it holds filters and a selected
 * display id only. The cursor and the back-stack live in the store and nowhere
 * else, because a token in a URL reaches bookmarks, history, referrers, and
 * access logs. The consequence is deliberate: reloading returns to the first page
 * of the URL's filters rather than to the exact page, which is the right trade
 * against handing out a bearer-shaped token in a link.
 */

/** Routes that represent a ticket-associated RAG execution. */
export const EXECUTION_ROUTES = Object.freeze(["knowledge_question", "generate_response"]);

/** Durable outcomes retained by the execution ledger. */
export const RUN_STATUSES = Object.freeze(["succeeded", "partial", "failed", "timeout"]);

/**
 * The queue's facet vocabulary, exactly as the server's grammar defines it.
 *
 * A status set plus **at most one** of these. A second facet is refused, so the
 * interface disables the rest once one is chosen instead of discovering the 422
 * after a round trip.
 */
export const REVIEW_FACETS = Object.freeze([
  "topic",
  "observation_type",
  "rating",
  "assigned_reviewer.email",
  "severity",
  "remediation_target",
]);

/** Durable review lifecycle, in the order a reviewer walks it. */
export const REVIEW_STATUSES = Object.freeze([
  "unreviewed",
  "reviewed",
  "triaged",
  "planned",
  "in_progress",
  "changes_proposed",
  "verifying",
  "resolved",
  "blocked",
  "wont_fix",
]);

/** Statuses that mean remediation is under way. */
export const ACTIVE_REMEDIATION_STATUSES = Object.freeze([
  "planned",
  "in_progress",
  "changes_proposed",
  "verifying",
]);

/**
 * The closed review transition table, mirrored from the server's own.
 *
 * Mirrored rather than derived, because the API returns a review's current
 * status and not the set of statuses reachable from it. Offering a move the
 * table forbids would turn a documented refusal into a failed save after the
 * reviewer has finished typing, so the options are narrowed here and the server
 * still has the last word.
 */
export const REVIEW_TRANSITIONS = Object.freeze({
  unreviewed: Object.freeze(["reviewed", "blocked"]),
  reviewed: Object.freeze(["triaged", "blocked", "wont_fix"]),
  triaged: Object.freeze(["planned", "blocked", "wont_fix"]),
  planned: Object.freeze(["in_progress", "blocked", "wont_fix"]),
  in_progress: Object.freeze(["changes_proposed", "blocked"]),
  changes_proposed: Object.freeze(["verifying", "in_progress", "blocked"]),
  verifying: Object.freeze(["resolved", "in_progress", "blocked"]),
  blocked: Object.freeze(["triaged", "planned", "in_progress", "wont_fix"]),
  resolved: Object.freeze([]),
  wont_fix: Object.freeze([]),
});

/** Edges only an administrator may take. Mirrored from the server's own. */
export const ADMIN_EXTRA_TRANSITIONS = Object.freeze({
  reviewed: Object.freeze(["resolved"]),
  triaged: Object.freeze(["resolved"]),
});

/** Statuses no ordinary patch can leave. Only an admin reopen does, to `triaged`. */
export const TERMINAL_REVIEW_STATUSES = Object.freeze(["resolved", "wont_fix"]);

/** The single status an admin reopen may target. */
export const REOPEN_TARGET = "triaged";

/** Root-cause taxonomy, the server's closed set. */
export const OBSERVATION_TYPES = Object.freeze([
  "correct",
  "knowledge_gap",
  "knowledge_conflict",
  "retrieval_miss",
  "chunking_or_metadata",
  "prompt_instruction",
  "orchestration_logic",
  "wrong_route",
  "source_data",
  "privacy_or_compliance",
  "other",
]);

export const SEVERITIES = Object.freeze(["low", "medium", "high", "critical"]);

export const REMEDIATION_TARGETS = Object.freeze([
  "kb",
  "prompt",
  "code",
  "workflow",
  "source_data",
  "none",
  "unknown",
]);

export const RESOLUTION_OUTCOMES = Object.freeze([
  "fixed",
  "no_change",
  "duplicate",
  "accepted_risk",
]);

/**
 * Field lengths, mirrored from the canonical model bounds.
 *
 * Declared once here so the character counters, the `maxlength` attributes and
 * the contract test all read the same numbers. A counter that disagrees with the
 * server is worse than none: it tells a reviewer their 10,050-character comment
 * is fine and then loses it to a 422.
 */
export const FIELD_LIMITS = Object.freeze({
  topic: 80,
  legacy_type: 80,
  comments: 10000,
  expected_behavior: 10000,
  verification_summary: 5000,
  no_change_reason: 1000,
  branch: 256,
  commit_sha: 256,
  reason: 1000,
});

/** Author classes the server assigns to a conversation entry. */
export const ACTOR_CLASSES = Object.freeze([
  "participant",
  "human_agent",
  "ai_or_system",
  "event",
  "unknown",
]);

/** The conversation filters, in the order they are offered. */
export const CONVERSATION_FILTERS = Object.freeze([
  "messages",
  "participant",
  "internal",
  "ai_or_system",
  "human_agent",
  "event",
  "unclassified",
]);

/** The four workspace panels of the detail view. */
export const WORKSPACE_PANELS = Object.freeze([
  "conversation",
  "evidence",
  "history",
  "remediation",
]);

/** Every evaluation field the reviewer may edit, i.e. the patch surface. */
export const EVALUATION_FIELDS = Object.freeze([
  "topic",
  "legacy_type",
  "observation_type",
  "rating",
  "comments",
  "expected_behavior",
  "severity",
  "status",
  "remediation_target",
  "remediation_summary",
  "modified_surfaces",
  "assigned_reviewer",
  "resolution",
]);

/**
 * Every key the address bar may carry. This list is the security boundary for
 * the URL, so it is declared once and read by both directions of the mapping.
 */
const URL_FILTER_KEYS = Object.freeze([
  "selected",
  "execution_id",
  "display_id",
  "route",
  "run_status",
  "review_status",
]);

const DEFAULT_EXECUTION_FILTERS = Object.freeze({
  executionId: "",
  displayId: "",
  route: "",
  runStatus: "",
  reviewStatus: "",
});

export const DEFAULT_PAGE_SIZE = 25;

/**
 * `phase` is the load state, and it is one value rather than several booleans so
 * "loading" and "error" cannot both be true:
 *
 *   idle       nothing requested yet
 *   loading    first page of a filter set; the table shows skeletons
 *   refreshing rows are on screen and a newer page is on the way
 *   ready      rows are current
 *   error      the request failed and there is nothing usable to show
 *
 * `partial` and `stale` are orthogonal to the phase. `partial` is the server's
 * own admission that a page is incomplete — a live-data outage still returns the
 * durable review, and rendering that as an empty queue would be a lie. `stale`
 * means a refresh failed while rows from an earlier answer are still displayed.
 */
/**
 * One paged subresource of the detail view.
 *
 * Conversation, audit history and evidence links each get one of these, and
 * that separation is the point: a broker outage must not blank the conversation,
 * and a conversation page failing must not claim the audit ledger is empty.
 *
 * `nextCursor === null` after at least one successful page means "that is all of
 * it". Before the first page it means nothing at all, which is why `pages` is
 * tracked separately rather than inferred from `items.length`: a genuinely empty
 * page that still carries a cursor is a real server answer, and treating it as
 * the end would silently hide every entry after it.
 */
function emptyFeed() {
  return {
    phase: "idle",
    items: [],
    nextCursor: null,
    pages: 0,
    partial: false,
    warnings: [],
    diagnostics: {},
    error: null,
  };
}

function initialDetail() {
  return {
    ref: "",
    phase: "idle",
    error: null,
    ticket: null,
    review: null,
    execution: null,
    evidence: null,
    version: null,
    partial: false,
    warnings: [],
    diagnostics: [],
    panel: "conversation",
    conversationFilter: "messages",
    conversation: emptyFeed(),
    audit: emptyFeed(),
    evidenceLinks: emptyFeed(),
    // The reviewer's edits, field name to value. Empty means "nothing typed",
    // which is not the same as "every field equals the server", because a field
    // can be deliberately set back to its saved value.
    draft: {},
    dirty: false,
    saving: false,
    // Set on 412 only. Holds the values the reviewer had, the values the server
    // now has, and which fields differ — never a merge, which is a decision only
    // the reviewer can make.
    conflict: null,
  };
}

export function initialState() {
  return {
    phase: "idle",
    partial: false,
    stale: false,
    warnings: [],
    error: null,
    rows: [],
    pageSize: DEFAULT_PAGE_SIZE,
    cursor: null,
    direction: "after",
    nextCursor: null,
    prevCursor: null,
    cursorStack: [],
    pageNumber: 1,
    selectedIds: [],
    selected: "",
    executionFilters: { ...DEFAULT_EXECUTION_FILTERS },
    session: null,
    readiness: null,
    cooldownS: 0,
    detail: initialDetail(),
    // Incremented whenever the *state* becomes the authority on what the filter
    // controls should show: a reset, a tab change, or a new address. Between
    // those, a control the reviewer is typing into is the authority on its own
    // value, because text input is debounced and the store is deliberately
    // behind. See `setFieldValue` in the controller.
    formGeneration: 0,
  };
}

function withoutSelection(state) {
  return { ...state, selectedIds: [] };
}

/** A filter change always returns to page one: a cursor is bound to its filters. */
function resetPaging(state) {
  return {
    ...state,
    cursor: null,
    direction: "after",
    nextCursor: null,
    prevCursor: null,
    cursorStack: [],
    pageNumber: 1,
  };
}

/** Whether the current source hands back a backward cursor of its own. */
export function hasServerBackwardPaging() {
  return false;
}

export function reduce(state, action) {
  switch (action.type) {
    case "session/loaded":
      return { ...state, session: action.session };

    case "readiness/loaded":
      return { ...state, readiness: action.readiness };

    case "filters/patch": {
      return resetPaging(
        withoutSelection({
          ...state,
          executionFilters: { ...state.executionFilters, ...action.patch },
          // `reset` marks a patch that did not come from the controls — reading
          // an address, say — so the controls must be rewritten from it.
          formGeneration: action.reset ? state.formGeneration + 1 : state.formGeneration,
        })
      );
    }

    case "filters/clear": {
      return resetPaging(
        withoutSelection({
          ...state,
          executionFilters: { ...DEFAULT_EXECUTION_FILTERS },
          // An explicit clear is a command about these very fields, so it
          // overrides whichever one currently has focus.
          formGeneration: state.formGeneration + 1,
        })
      );
    }

    case "load/started":
      return {
        ...state,
        phase: state.rows.length > 0 && action.refresh ? "refreshing" : "loading",
        error: null,
      };

    case "load/succeeded":
      return {
        ...state,
        phase: "ready",
        rows: action.rows,
        partial: Boolean(action.partial),
        stale: false,
        warnings: action.warnings ?? [],
        nextCursor: action.nextCursor ?? null,
        prevCursor: action.prevCursor ?? null,
        error: null,
      };

    case "load/failed":
      // Rows that are still on screen stay there, marked stale. Blanking a
      // usable page because a refresh failed loses a reviewer's place for no
      // gain.
      return state.rows.length > 0
        ? { ...state, phase: "ready", stale: true, error: action.error }
        : { ...state, phase: "error", stale: false, rows: [], error: action.error };

    case "page/forward": {
      if (state.nextCursor === null) {
        return state;
      }
      return withoutSelection({
        ...state,
        cursorStack: [...state.cursorStack, state.cursor],
        cursor: state.nextCursor,
        direction: "after",
        pageNumber: state.pageNumber + 1,
      });
    }

    case "page/back": {
      if (state.cursorStack.length === 0) {
        return state;
      }
      const stack = state.cursorStack.slice(0, -1);
      const cursor = state.cursorStack[state.cursorStack.length - 1];
      return withoutSelection({
        ...state,
        cursorStack: stack,
        cursor: cursor ?? null,
        direction: "after",
        pageNumber: Math.max(1, state.pageNumber - 1),
      });
    }


    case "page/reset":
      return resetPaging(withoutSelection(state));

    case "selection/toggle": {
      const has = state.selectedIds.includes(action.id);
      return {
        ...state,
        selectedIds: has
          ? state.selectedIds.filter((item) => item !== action.id)
          : [...state.selectedIds, action.id],
      };
    }

    case "selection/set":
      return { ...state, selectedIds: [...action.ids] };

    case "selection/clear":
      return withoutSelection(state);

    case "detail/open": {
      if (action.id === state.selected && state.detail.ref === action.id) {
        return state;
      }
      // Opening a different ticket discards the previous one entirely, including
      // any draft. There is no honest way to carry an unsaved comment from one
      // ticket to another, and asking would be a modal in the middle of a click.
      return {
        ...state,
        selected: action.id,
        detail: { ...initialDetail(), ref: action.id, phase: "loading" },
      };
    }

    case "detail/close":
      return { ...state, selected: "", detail: initialDetail() };

    case "detail/loaded": {
      const review = action.review ?? null;
      return {
        ...state,
        detail: {
          ...state.detail,
          phase: "ready",
          error: null,
          ticket: action.ticket ?? null,
          review,
          execution: action.execution ?? null,
          evidence: action.evidence ?? null,
          version: typeof review?.version === "number" ? review.version : null,
          partial: Boolean(action.partial),
          warnings: action.warnings ?? [],
          diagnostics: action.diagnostics ?? {},
        },
      };
    }

    case "detail/failed":
      return {
        ...state,
        detail: { ...state.detail, phase: "error", error: action.error },
      };

    case "detail/panel": {
      if (!WORKSPACE_PANELS.includes(action.panel)) {
        return state;
      }
      return { ...state, detail: { ...state.detail, panel: action.panel } };
    }

    case "detail/conversation-filter": {
      if (!CONVERSATION_FILTERS.includes(action.value)) {
        return state;
      }
      return { ...state, detail: { ...state.detail, conversationFilter: action.value } };
    }

    case "feed/started": {
      const feed = state.detail[action.feed];
      if (feed === undefined) {
        return state;
      }
      return {
        ...state,
        detail: {
          ...state.detail,
          [action.feed]: { ...feed, phase: "loading", error: null },
        },
      };
    }

    case "feed/loaded": {
      const feed = state.detail[action.feed];
      if (feed === undefined) {
        return state;
      }
      const items = action.append ? [...feed.items, ...action.items] : [...action.items];
      return {
        ...state,
        detail: {
          ...state.detail,
          [action.feed]: {
            phase: "ready",
            items,
            nextCursor: action.nextCursor ?? null,
            pages: feed.pages + 1,
            partial: Boolean(action.partial),
            warnings: action.warnings ?? [],
            diagnostics: action.diagnostics ?? [],
            error: null,
          },
        },
      };
    }

    case "feed/failed": {
      const feed = state.detail[action.feed];
      if (feed === undefined) {
        return state;
      }
      // Pages already fetched stay on screen. Replacing a read conversation with
      // an error because page four failed loses the reviewer's context for no
      // gain; the status line says the newest request failed.
      return {
        ...state,
        detail: {
          ...state.detail,
          [action.feed]: {
            ...feed,
            phase: feed.pages > 0 ? "stale" : "error",
            error: action.error,
          },
        },
      };
    }

    case "draft/patch": {
      const draft = { ...state.detail.draft, ...action.patch };
      return {
        ...state,
        detail: { ...state.detail, draft, dirty: draftDiffers(draft, state.detail.review) },
      };
    }

    case "draft/reset":
      return {
        ...state,
        detail: { ...state.detail, draft: {}, dirty: false, conflict: null },
      };

    case "save/started":
      return { ...state, detail: { ...state.detail, saving: true } };

    case "save/succeeded": {
      const review = action.review ?? state.detail.review;
      return {
        ...state,
        detail: {
          ...state.detail,
          saving: false,
          review,
          version: typeof review?.version === "number" ? review.version : null,
          draft: {},
          dirty: false,
          conflict: null,
        },
      };
    }

    case "save/failed":
      // The draft survives on purpose. A refused save is exactly the moment a
      // reviewer's twenty minutes of typing is most easily thrown away.
      return { ...state, detail: { ...state.detail, saving: false } };

    case "conflict/opened":
      return {
        ...state,
        detail: { ...state.detail, saving: false, conflict: action.conflict },
      };

    case "conflict/cleared":
      return { ...state, detail: { ...state.detail, conflict: null } };

    case "cooldown/set":
      return { ...state, cooldownS: Math.max(0, action.seconds) };

    default:
      return state;
  }
}

/** A tiny observable store. No framework, no proxies, no hidden re-entrancy. */
export function createStore() {
  let state = initialState();
  const listeners = new Set();
  let notifying = false;

  return {
    getState() {
      return state;
    },
    dispatch(action) {
      const next = reduce(state, action);
      if (next === state) {
        return state;
      }
      state = next;
      if (notifying) {
        // A listener that dispatches would otherwise re-enter this loop and
        // notify out of order.
        return state;
      }
      notifying = true;
      try {
        for (const listener of listeners) {
          listener(state);
        }
      } finally {
        notifying = false;
      }
      return state;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

// ---------------------------------------------------------------------------
// The address bar
// ---------------------------------------------------------------------------

function firstOf(params, key) {
  const value = params.get(key);
  return value === null ? "" : value.slice(0, 120);
}

/**
 * Read run-ledger filters and the selected execution id out of a query string.
 *
 * Anything not in `URL_FILTER_KEYS` is ignored rather than trusted, so a forged
 * link cannot introduce a key the interface does not model. A forged link *can*
 * still ask for an unsupported filter combination; that is answered by the
 * server's stable 422 and rendered as such, never by filtering a page in the
 * browser and calling it a result.
 */
export function readLocation(search) {
  const params = new URLSearchParams(search);
  return {
    selected: firstOf(params, "selected"),
    executionFilters: {
      executionId: firstOf(params, "execution_id"),
      displayId: firstOf(params, "display_id").toUpperCase(),
      route: EXECUTION_ROUTES.includes(firstOf(params, "route")) ? firstOf(params, "route") : "",
      runStatus: RUN_STATUSES.includes(firstOf(params, "run_status"))
        ? firstOf(params, "run_status")
        : "",
      reviewStatus: REVIEW_STATUSES.includes(firstOf(params, "review_status"))
        ? firstOf(params, "review_status")
        : "",
    },
  };
}

/**
 * Serialize the shareable part of the state.
 *
 * The output is checked against `URL_FILTER_KEYS` before it is returned, so a
 * later edit cannot add a key — a cursor above all — without failing here.
 */
export function writeLocation(state) {
  const params = new URLSearchParams();
  if (state.selected !== "") {
    params.set("selected", state.selected);
  }
  const filters = state.executionFilters;
  const pairs = [
    ["execution_id", filters.executionId],
    ["display_id", filters.displayId],
    ["route", filters.route],
    ["run_status", filters.runStatus],
    ["review_status", filters.reviewStatus],
  ];
  for (const [key, value] of pairs) {
    if (value !== "" && value !== null && value !== undefined) {
      params.set(key, value);
    }
  }
  for (const key of params.keys()) {
    if (!URL_FILTER_KEYS.includes(key)) {
      throw new Error("only declared filter keys may enter the address bar");
    }
  }
  const search = params.toString();
  return search === "" ? "" : `?${search}`;
}

/** Whether a previous page is reachable from where the store currently is. */
export function canGoBack(state) {
  return state.cursorStack.length > 0;
}

/** The filters that are currently active, as label/value pairs for the chips. */
export function activeFilters(state) {
  const chips = [];
  const filters = state.executionFilters;
  const named = [
    ["Execution", "executionId", filters.executionId],
    ["Ticket ID", "displayId", filters.displayId],
    ["Outcome", "route", filters.route],
    ["Run status", "runStatus", filters.runStatus],
    ["Review status", "reviewStatus", filters.reviewStatus],
  ];
  for (const [label, field, value] of named) {
    if (value !== "") {
      chips.push({ label, field, value });
    }
  }
  return chips;
}

// ---------------------------------------------------------------------------
// The detail view's selectors
// ---------------------------------------------------------------------------

/**
 * Where each editable control's saved value lives on the durable review.
 *
 * The resolution fields are flat in the form and nested on the record, so the
 * mapping is declared rather than guessed at two call sites.
 */
const SAVED_VALUE_PATHS = Object.freeze({
  topic: ["topic"],
  legacy_type: ["legacy_type"],
  observation_type: ["observation_type"],
  rating: ["rating"],
  comments: ["comments"],
  expected_behavior: ["expected_behavior"],
  severity: ["severity"],
  status: ["status"],
  remediation_target: ["remediation_target"],
  outcome: ["resolution", "outcome"],
  verification_summary: ["resolution", "verification_summary"],
  no_change_reason: ["resolution", "no_change_reason"],
  branch: ["resolution", "branch"],
  commit_sha: ["resolution", "commit_sha"],
});

/** The saved value of one form control, as the string a control would hold. */
export function savedValue(review, field) {
  const path = SAVED_VALUE_PATHS[field];
  if (path === undefined || review === null || review === undefined) {
    return "";
  }
  let current = review;
  for (const step of path) {
    if (current === null || current === undefined) {
      return "";
    }
    current = current[step];
  }
  return current === null || current === undefined ? "" : String(current);
}

/**
 * Whether anything in the draft actually differs from what is stored.
 *
 * Typing a character and deleting it again leaves the form clean, which is what
 * decides whether the navigation warning fires. `assigned_reviewer` is the one
 * exception: its control is a command ("assign to me", "unassign") rather than a
 * value, so `keep` is the only clean setting.
 */
export function draftDiffers(draft, review) {
  for (const [field, value] of Object.entries(draft)) {
    if (field === "assigned_reviewer") {
      if (value !== "keep") {
        return true;
      }
      continue;
    }
    if (String(value ?? "") !== savedValue(review, field)) {
      return true;
    }
  }
  return false;
}

/** The names of the fields the reviewer has actually changed. */
export function dirtyFieldNames(draft, review) {
  const names = [];
  for (const [field, value] of Object.entries(draft)) {
    if (field === "assigned_reviewer") {
      if (value !== "keep") {
        names.push(field);
      }
      continue;
    }
    if (String(value ?? "") !== savedValue(review, field)) {
      names.push(field);
    }
  }
  return names;
}

/**
 * The statuses a reviewer may move this review to.
 *
 * An admin may additionally reopen a terminal review, and only onto `triaged`.
 * Everyone else sees an empty list on a closed review rather than a control that
 * always fails.
 */
export function allowedNextStatuses(status, { role = "viewer" } = {}) {
  if (TERMINAL_REVIEW_STATUSES.includes(status)) {
    return role === "admin" ? [REOPEN_TARGET] : [];
  }
  const allowed = [...(REVIEW_TRANSITIONS[status] ?? [])];
  if (role === "admin") {
    allowed.push(...(ADMIN_EXTRA_TRANSITIONS[status] ?? []));
  }
  return [...new Set(allowed)];
}

/** Whether a chosen status needs a closed resolution object to be accepted. */
export function statusNeedsResolution(status) {
  return TERMINAL_REVIEW_STATUSES.includes(status);
}

/**
 * Apply one conversation filter to a page of classified messages.
 *
 * `participant` reads the server's own `participant_facing` flag rather than
 * inferring from visibility, and `internal` reads `internal`. Those two are
 * decided server-side from configured identities and actor types, and a
 * browser-side guess is exactly how an agent-only note gets shown as something
 * the customer saw.
 */
export function filterConversation(messages, filter) {
  if (filter === "messages") {
    return messages.filter((message) => message.kind === "comment");
  }
  if (filter === "participant") {
    return messages.filter((message) => message.participant_facing === true);
  }
  if (filter === "internal") {
    return messages.filter(
      (message) => message.internal === true && message.actor_class !== "event"
    );
  }
  if (filter === "unclassified") {
    return messages.filter((message) => message.actor_class === "unknown");
  }
  return messages.filter((message) => message.actor_class === filter);
}

/** Whether a feed has proven there is another page to fetch. */
export function feedHasMore(feed) {
  return typeof feed?.nextCursor === "string" && feed.nextCursor !== "";
}

/**
 * Whether a feed can honestly claim to be complete.
 *
 * One successful page and no forward cursor. Before the first page there is
 * nothing to claim either way, and saying "no conversation" then would be a
 * statement about the network dressed up as a statement about the ticket.
 */
export function feedIsComplete(feed) {
  return feed?.pages > 0 && !feedHasMore(feed);
}

/**
 * Page-scoped tallies for the indicator strip.
 *
 * Named `page…` because that is all they can be: the administrative API returns
 * bounded pages and no aggregate, so a global figure is not available and is not
 * invented here.
 */
export function pageTallies(rows) {
  let unreviewed = 0;
  let lowRating = 0;
  let remediating = 0;
  for (const row of rows) {
    const review = row.review;
    if (review === null || review === undefined) {
      unreviewed += 1;
    } else if (review.status === "unreviewed") {
      unreviewed += 1;
    }
    if (["failed", "partial", "timeout"].includes(row.runStatus)) {
      lowRating += 1;
    }
    if (ACTIVE_REMEDIATION_STATUSES.includes(review?.status)) {
      remediating += 1;
    }
  }
  return { unreviewed, lowRating, remediating };
}

// ---------------------------------------------------------------------------
// Remediation batches
//
// A separate mirror from the review lifecycle on purpose. The two share four
// names — `planned`-ish, `changes_proposed`, `verifying`, `blocked` — and differ
// everywhere else, and `REVIEW_TRANSITIONS` above is asserted to mirror the
// server's review table exactly. Folding the batch machine into it would make
// that assertion pass while describing something no server rule enforces.
// ---------------------------------------------------------------------------

export const BATCH_STATES = Object.freeze([
  "draft",
  "ready",
  "claimed",
  "planning",
  "in_progress",
  "changes_proposed",
  "verifying",
  "completed",
  "blocked",
  "cancelled",
  "expired",
]);

export const BATCH_STATE_LABELS = Object.freeze({
  draft: "Draft",
  ready: "Ready to claim",
  claimed: "Claimed by the agent",
  planning: "Planning",
  in_progress: "In progress",
  changes_proposed: "Changes proposed",
  verifying: "Being verified",
  completed: "Completed",
  blocked: "Blocked",
  cancelled: "Cancelled",
  expired: "Lease expired",
});

/**
 * Which human action each batch state offers, and to whom.
 *
 * Only the five human transitions appear. The agent's own edges — `planning`,
 * `in_progress`, `changes_proposed` — are absent by construction, so a control
 * for one cannot be rendered for a person even by accident.
 */
export const BATCH_ACTIONS = Object.freeze({
  ready: Object.freeze({
    states: Object.freeze(["draft", "blocked"]),
    roles: Object.freeze(["remediator", "admin"]),
    label: "Mark ready",
  }),
  cancel: Object.freeze({
    states: Object.freeze(["draft", "ready", "blocked", "expired"]),
    roles: Object.freeze(["remediator", "admin"]),
    label: "Cancel",
  }),
  "start-verification": Object.freeze({
    states: Object.freeze(["changes_proposed"]),
    roles: Object.freeze(["reviewer", "admin"]),
    label: "Start verification",
  }),
  complete: Object.freeze({
    states: Object.freeze(["verifying"]),
    roles: Object.freeze(["reviewer", "admin"]),
    label: "Complete",
  }),
  "extend-lease": Object.freeze({
    states: Object.freeze(["claimed", "planning", "in_progress"]),
    roles: Object.freeze(["admin"]),
    label: "Extend lease",
  }),
});

/** Which actions this role may take on a batch in this state, in order. */
export function allowedBatchActions(status, { role = "viewer" } = {}) {
  return Object.entries(BATCH_ACTIONS)
    .filter(([, rule]) => rule.states.includes(status) && rule.roles.includes(role))
    .map(([name]) => name);
}

/** Whether this role may freeze a new batch at all. */
export function canCurateBatches(role) {
  return role === "remediator" || role === "admin";
}

/**
 * The selected rows that can actually be frozen, as `{reviewId, reviewVersion}`.
 *
 * A row with no durable review is skipped rather than rejected. This can only be
 * a transient or historical inconsistency now that RAG ingestion creates the
 * review; the browser has no path that creates or imports one.
 */
export function batchableSelection(rows, selectedIds) {
  const chosen = new Set(selectedIds);
  const refs = [];
  const skipped = [];
  for (const row of rows) {
    if (!chosen.has(row.executionId)) {
      continue;
    }
    const reviewId = row.review?.review_id ?? null;
    const version = row.review?.version ?? null;
    if (typeof reviewId === "string" && reviewId !== "" && typeof version === "number") {
      refs.push({ reviewId, reviewVersion: version, displayId: row.displayId });
    } else {
      skipped.push(row.displayId);
    }
  }
  return { refs, skipped };
}
