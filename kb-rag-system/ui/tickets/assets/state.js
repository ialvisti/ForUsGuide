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

/** The two data sources the console lists. */
export const MODES = Object.freeze(["devrev", "reviews"]);

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
 * Every key the address bar may carry. This list is the security boundary for
 * the URL, so it is declared once and read by both directions of the mapping.
 */
const URL_FILTER_KEYS = Object.freeze([
  "tab",
  "selected",
  "ticket_id",
  "stage",
  "state",
  "source_channel",
  "subtype",
  "created_date",
  "modified_date",
  "display_id",
  "status",
  "facet",
  "facet_value",
  "updated_after",
  "updated_before",
  "include_reversed",
]);

const DEFAULT_DEVREV_FILTERS = Object.freeze({
  ticketId: "",
  stage: "",
  state: "",
  sourceChannel: "",
  subtype: "",
  createdDate: "",
  modifiedDate: "",
});

const DEFAULT_REVIEW_FILTERS = Object.freeze({
  displayId: "",
  statuses: [],
  facet: "",
  facetValue: "",
  updatedAfter: "",
  updatedBefore: "",
  includeReversed: false,
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
export function initialState() {
  return {
    mode: "devrev",
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
    filters: {
      devrev: { ...DEFAULT_DEVREV_FILTERS },
      reviews: { ...DEFAULT_REVIEW_FILTERS, statuses: [] },
    },
    session: null,
    readiness: null,
    cooldownS: 0,
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
export function hasServerBackwardPaging(mode) {
  // The live ticket list seals a token per direction, so backward paging is the
  // server's job there. The durable queue's token is the repository's own and is
  // bound to endpoint and filters only — it has no direction, so the only honest
  // way back is the stack of tokens this session has already used.
  return mode === "devrev";
}

export function reduce(state, action) {
  switch (action.type) {
    case "session/loaded":
      return { ...state, session: action.session };

    case "readiness/loaded":
      return { ...state, readiness: action.readiness };

    case "mode/set": {
      if (!MODES.includes(action.mode) || action.mode === state.mode) {
        return state;
      }
      return resetPaging(
        withoutSelection({
          ...state,
          mode: action.mode,
          rows: [],
          phase: "idle",
          error: null,
          formGeneration: state.formGeneration + 1,
        })
      );
    }

    case "filters/patch": {
      const current = state.filters[action.mode] ?? {};
      const filters = { ...state.filters, [action.mode]: { ...current, ...action.patch } };
      return resetPaging(
        withoutSelection({
          ...state,
          filters,
          // `reset` marks a patch that did not come from the controls — reading
          // an address, say — so the controls must be rewritten from it.
          formGeneration: action.reset ? state.formGeneration + 1 : state.formGeneration,
        })
      );
    }

    case "filters/clear": {
      const blank =
        action.mode === "devrev"
          ? { ...DEFAULT_DEVREV_FILTERS }
          : { ...DEFAULT_REVIEW_FILTERS, statuses: [] };
      return resetPaging(
        withoutSelection({
          ...state,
          filters: { ...state.filters, [action.mode]: blank },
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
      // Two sources, two mechanisms, for the reason described on
      // `hasServerBackwardPaging`.
      if (hasServerBackwardPaging(state.mode)) {
        if (state.prevCursor === null) {
          return state;
        }
        return withoutSelection({
          ...state,
          cursorStack: state.cursorStack.slice(0, -1),
          cursor: state.prevCursor,
          direction: "before",
          pageNumber: Math.max(1, state.pageNumber - 1),
        });
      }
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

    case "detail/open":
      return { ...state, selected: action.id };

    case "detail/close":
      return { ...state, selected: "" };

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
 * Read mode, filters, and the selected display id out of a query string.
 *
 * Anything not in `URL_FILTER_KEYS` is ignored rather than trusted, so a forged
 * link cannot introduce a key the interface does not model. A forged link *can*
 * still ask for an unsupported filter combination; that is answered by the
 * server's stable 422 and rendered as such, never by filtering a page in the
 * browser and calling it a result.
 */
export function readLocation(search) {
  const params = new URLSearchParams(search);
  const requested = params.get("tab");
  const mode = MODES.includes(requested) ? requested : "devrev";
  const statuses = params
    .getAll("status")
    .filter((value) => REVIEW_STATUSES.includes(value));
  const facet = firstOf(params, "facet");
  return {
    mode,
    selected: firstOf(params, "selected").toUpperCase(),
    devrev: {
      ticketId: firstOf(params, "ticket_id").toUpperCase(),
      stage: firstOf(params, "stage"),
      state: firstOf(params, "state"),
      sourceChannel: firstOf(params, "source_channel"),
      subtype: firstOf(params, "subtype"),
      createdDate: firstOf(params, "created_date"),
      modifiedDate: firstOf(params, "modified_date"),
    },
    reviews: {
      displayId: firstOf(params, "display_id").toUpperCase(),
      statuses,
      facet: REVIEW_FACETS.includes(facet) ? facet : "",
      facetValue: firstOf(params, "facet_value"),
      updatedAfter: firstOf(params, "updated_after"),
      updatedBefore: firstOf(params, "updated_before"),
      includeReversed: params.get("include_reversed") === "true",
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
  if (state.mode !== "devrev") {
    params.set("tab", state.mode);
  }
  if (state.selected !== "") {
    params.set("selected", state.selected);
  }
  const devrev = state.filters.devrev;
  const pairs = [
    ["ticket_id", devrev.ticketId],
    ["stage", devrev.stage],
    ["state", devrev.state],
    ["source_channel", devrev.sourceChannel],
    ["subtype", devrev.subtype],
    ["created_date", devrev.createdDate],
    ["modified_date", devrev.modifiedDate],
  ];
  const reviews = state.filters.reviews;
  pairs.push(
    ["display_id", reviews.displayId],
    ["facet", reviews.facet],
    ["facet_value", reviews.facetValue],
    ["updated_after", reviews.updatedAfter],
    ["updated_before", reviews.updatedBefore]
  );
  for (const [key, value] of pairs) {
    if (value !== "" && value !== null && value !== undefined) {
      params.set(key, value);
    }
  }
  for (const status of reviews.statuses) {
    params.append("status", status);
  }
  if (reviews.includeReversed) {
    params.set("include_reversed", "true");
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
  return hasServerBackwardPaging(state.mode)
    ? state.prevCursor !== null
    : state.cursorStack.length > 0;
}

/** The filters that are currently active, as label/value pairs for the chips. */
export function activeFilters(state) {
  const chips = [];
  if (state.mode === "devrev") {
    const filters = state.filters.devrev;
    const named = [
      ["Ticket ID", "ticketId", filters.ticketId],
      ["Stage", "stage", filters.stage],
      ["State", "state", filters.state],
      ["Source channel", "sourceChannel", filters.sourceChannel],
      ["Subtype", "subtype", filters.subtype],
      ["Created", "createdDate", filters.createdDate],
      ["Modified", "modifiedDate", filters.modifiedDate],
    ];
    for (const [label, field, value] of named) {
      if (value !== "") {
        chips.push({ label, field, value });
      }
    }
    return chips;
  }
  const filters = state.filters.reviews;
  if (filters.displayId !== "") {
    chips.push({ label: "Ticket ID", field: "displayId", value: filters.displayId });
  }
  for (const status of filters.statuses) {
    chips.push({ label: "Status", field: "statuses", value: status, item: status });
  }
  if (filters.facet !== "" && filters.facetValue !== "") {
    chips.push({ label: filters.facet, field: "facet", value: filters.facetValue });
  }
  if (filters.updatedAfter !== "") {
    chips.push({ label: "Updated from", field: "updatedAfter", value: filters.updatedAfter });
  }
  if (filters.updatedBefore !== "") {
    chips.push({ label: "Updated to", field: "updatedBefore", value: filters.updatedBefore });
  }
  if (filters.includeReversed) {
    chips.push({ label: "Reversed imports", field: "includeReversed", value: "included" });
  }
  return chips;
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
  let highSeverity = 0;
  let remediating = 0;
  for (const row of rows) {
    const review = row.review;
    if (review === null || review === undefined) {
      unreviewed += 1;
      continue;
    }
    if (review.status === "unreviewed") {
      unreviewed += 1;
    }
    if (typeof review.rating === "number" && review.rating <= 2) {
      lowRating += 1;
    }
    if (review.severity === "high" || review.severity === "critical") {
      highSeverity += 1;
    }
    if (ACTIVE_REMEDIATION_STATUSES.includes(review.status)) {
      remediating += 1;
    }
  }
  return { unreviewed, lowRating, highSeverity, remediating };
}
