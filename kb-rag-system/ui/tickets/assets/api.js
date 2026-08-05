/**
 * The only module that talks to the server.
 *
 * Everything the administrative API enforces is enforced here once, rather than
 * at each call site:
 *
 *   * every URL is a root-absolute same-origin path, and `credentials` is
 *     `same-origin`, so the identity proxy's assertion rides along and nothing
 *     is ever sent cross-origin;
 *   * a page cursor travels in the `X-Tickets-Cursor` request header. The server
 *     refuses a cursor-shaped query parameter outright, because a URL is the one
 *     place a token is guaranteed to reach an access log — so the query builder
 *     below refuses to write one even by accident;
 *   * an unsafe request carries a CSRF token from `GET /session`, a UUID
 *     `Idempotency-Key`, and `Content-Type: application/json`. The two remaining
 *     conditions the server checks — the request's own origin and its
 *     fetch-metadata site header — are forbidden headers in `fetch`: the browser
 *     supplies both and a script attempt to set them is dropped silently. Code
 *     that pretended to set them would look correct and produce a console whose
 *     every write is refused;
 *   * the CSRF token lives in a module variable and nowhere else. A cookie would
 *     be attached automatically and defeat its purpose; browser storage would
 *     outlive the tab and be readable by anything that ever achieves script
 *     execution here.
 *
 * Failures become one typed `ApiError` carrying a message written for a
 * reviewer, never the server's own wording, because that wording is addressed
 * to an operator and the request id is the only part a reviewer can act on.
 */

const API_ROOT = "/api/admin/v1";
const READINESS_PATH = "/readyz";

const CURSOR_HEADER = "X-Tickets-Cursor";
const CSRF_HEADER = "X-CSRF-Token";
const IDEMPOTENCY_HEADER = "Idempotency-Key";
const REQUEST_ID_HEADER = "X-Request-ID";
const RETRY_AFTER_HEADER = "Retry-After";
const JSON_MEDIA_TYPE = "application/json";

/**
 * Used when a 429 arrives without `Retry-After`.
 *
 * Our own per-subject bound always sends the header; the upstream's does so only
 * when the upstream did. Trusting the header to be present would leave the
 * console retrying against a service that has just asked it to stop.
 */
export const FALLBACK_RETRY_AFTER_S = 20;

/** A cursor may never be spelled as a query parameter. */
const FORBIDDEN_QUERY_KEYS = new Set([
  "cursor",
  "next_cursor",
  "prev_cursor",
  "page_token",
  "next",
  "before",
  "after",
]);

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Re-fetch the session token this many seconds before it expires. */
const CSRF_RENEW_MARGIN_S = 120;

// ---------------------------------------------------------------------------
// Typed failures
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(fields) {
    super(fields.title);
    this.name = "ApiError";
    this.status = fields.status ?? 0;
    this.code = fields.code ?? "UNKNOWN";
    this.title = fields.title;
    this.detail = fields.detail ?? "";
    this.tone = fields.tone ?? "error";
    this.requestId = fields.requestId ?? null;
    this.retryAfterS = fields.retryAfterS ?? null;
    this.currentVersion = fields.currentVersion ?? null;
    this.changedAt = fields.changedAt ?? null;
    this.recoverable = Boolean(fields.recoverable);
  }
}

/** A request this module cancelled because a newer one replaced it. */
export class AbortedError extends Error {
  constructor() {
    super("superseded");
    this.name = "AbortedError";
  }
}

/**
 * Map a response onto words a reviewer can act on.
 *
 * Keyed by code first and status second: the code is the stable contract, and
 * two different 429s (ours and the upstream's) need different advice.
 */
function describe(status, code) {
  switch (code) {
    case "UNAUTHENTICATED":
      return {
        title: "Your session ended",
        detail: "Reload the page to sign in again. Nothing was saved.",
        recoverable: true,
      };
    case "FORBIDDEN":
      return {
        title: "Your role does not allow this",
        detail: "Ask an administrator for the reviewer role, then reload.",
        recoverable: false,
      };
    case "NOT_FOUND":
      return {
        title: "That record is not available",
        detail: "It may have been removed, or it may be outside your scope.",
        recoverable: false,
      };
    case "UNSUPPORTED_FILTER_COMBINATION":
      return {
        title: "That combination of filters is not supported",
        detail:
          "The queue accepts a status set plus at most one facet, and an exact " +
          "ticket lookup runs on its own. Clear a filter and try again.",
        recoverable: true,
      };
    case "CURSOR_REJECTED":
      return {
        title: "That page is no longer available",
        detail: "Returning to the first page of the current filters.",
        tone: "warning",
        recoverable: true,
      };
    case "PRECONDITION_REQUIRED":
      return {
        title: "This change needs the record version",
        detail: "Reload the ticket and try again.",
        recoverable: true,
      };
    case "PRECONDITION_MALFORMED":
      return {
        title: "The record version was not usable",
        detail: "Reload the ticket and try again.",
        recoverable: true,
      };
    case "REVIEW_VERSION_CONFLICT":
      return {
        title: "Someone else changed this review",
        detail: "Reload to see their version before saving yours.",
        recoverable: true,
      };
    case "REVIEW_CONFLICT":
      return {
        title: "That change conflicts with the review's current state",
        detail: "Reload the review to see where it is now.",
        recoverable: true,
      };
    case "EVIDENCE_LINK_REJECTED":
      return {
        title: "That evidence suggestion is no longer linkable",
        detail:
          "A suggestion is short-lived and is bound to the ticket, the review, " +
          "and you. Reload the ticket to get a current one.",
        recoverable: true,
      };
    case "IDEMPOTENCY_CONFLICT":
      return {
        title: "That request was already used for something else",
        detail: "Retry the action; a fresh key is generated each time.",
        recoverable: true,
      };
    case "RATE_LIMITED":
      return {
        title: "Too many requests",
        detail: "Refresh is paused briefly so the console stays within its bound.",
        tone: "warning",
        recoverable: true,
      };
    case "UPSTREAM_RATE_LIMITED":
      return {
        title: "The ticket system is rate limiting this console",
        detail:
          "Durable review data is unaffected. Live ticket data will return " +
          "shortly.",
        tone: "warning",
        recoverable: true,
      };
    case "UPSTREAM_UNAVAILABLE":
      return {
        title: "Live ticket data is unavailable",
        detail:
          "The durable review queue is unaffected, so the Review queue tab " +
          "still works.",
        tone: "warning",
        recoverable: true,
      };
    case "UPSTREAM_PROTOCOL_ERROR":
      return {
        title: "The ticket system returned something unusable",
        detail: "This is a service-side problem; the request id below identifies it.",
        recoverable: false,
      };
    case "EVIDENCE_UNAVAILABLE":
      return {
        title: "Evidence lookup is unavailable",
        detail: "Reviews and tickets are unaffected.",
        tone: "warning",
        recoverable: true,
      };
    case "REQUEST_BODY_TOO_LARGE":
      return {
        title: "That request was too large",
        detail: "Shorten the text and try again.",
        recoverable: true,
      };
    case "IDEMPOTENCY_KEY_REQUIRED":
      return {
        title: "The request was missing its safety key",
        detail: "Retry the action.",
        recoverable: true,
      };
    case "UNSUPPORTED_MEDIA_TYPE":
      return {
        title: "The console sent an unusable content type",
        detail: "Reload the page; this indicates a stale script.",
        recoverable: true,
      };
    case "NOT_INITIALIZED":
    case "NOT_READY":
      return {
        title: "The console is still starting",
        detail: "Try again in a moment.",
        tone: "warning",
        recoverable: true,
      };
    case "VALIDATION_FAILED":
      return {
        title: "The server refused that request",
        detail: "Check the filters and try again.",
        recoverable: true,
      };
    default:
      break;
  }
  if (status === 401) {
    return { title: "Your session ended", detail: "Reload to sign in again.", recoverable: true };
  }
  if (status === 403) {
    return { title: "Not permitted", detail: "Your role does not allow this.", recoverable: false };
  }
  if (status === 409) {
    return { title: "That change conflicts", detail: "Reload and try again.", recoverable: true };
  }
  if (status === 412 || status === 428) {
    return {
      title: "This change needs the current record version",
      detail: "Reload and try again.",
      recoverable: true,
    };
  }
  if (status === 429) {
    return {
      title: "Too many requests",
      detail: "Requests are paused briefly.",
      tone: "warning",
      recoverable: true,
    };
  }
  if (status === 502) {
    return {
      title: "A dependency returned something unusable",
      detail: "This is a service-side problem.",
      recoverable: false,
    };
  }
  if (status === 503) {
    return {
      title: "A dependency is unavailable",
      detail: "Durable review data is unaffected.",
      tone: "warning",
      recoverable: true,
    };
  }
  if (status >= 500) {
    return {
      title: "Something went wrong on the server",
      detail: "The request id below identifies this failure in the logs.",
      recoverable: true,
    };
  }
  return {
    title: "The request could not be completed",
    detail: "Try again, or reload the page.",
    recoverable: true,
  };
}

// ---------------------------------------------------------------------------
// Session and CSRF token — memory only
// ---------------------------------------------------------------------------

let sessionState = null;
let sessionPromise = null;
let cooldownUntilMs = 0;

/** Seconds left on a server-requested pause, or 0. */
export function cooldownRemainingS() {
  const remaining = Math.ceil((cooldownUntilMs - Date.now()) / 1000);
  return remaining > 0 ? remaining : 0;
}

function noteCooldown(seconds) {
  const until = Date.now() + Math.max(1, seconds) * 1000;
  if (until > cooldownUntilMs) {
    cooldownUntilMs = until;
  }
}

function tokenIsFresh() {
  if (sessionState === null) {
    return false;
  }
  const expiresAt = Date.parse(sessionState.csrfExpiresAt);
  if (Number.isNaN(expiresAt)) {
    return false;
  }
  return expiresAt - Date.now() > CSRF_RENEW_MARGIN_S * 1000;
}

/**
 * Fetch the verified caller, their role, the feature flags, and a CSRF token.
 *
 * Concurrent callers share one in-flight request: four widgets asking who is
 * signed in must not mint four tokens.
 */
export async function loadSession({ force = false } = {}) {
  if (!force && tokenIsFresh()) {
    return sessionState;
  }
  if (sessionPromise === null) {
    sessionPromise = requestJson(`${API_ROOT}/session`, { channel: "session" })
      .then((body) => {
        sessionState = {
          email: body?.identity?.email ?? "",
          displayName: body?.identity?.display_name ?? "",
          role: body?.role ?? "viewer",
          csrfToken: body?.csrf_token ?? "",
          csrfExpiresAt: body?.csrf_expires_at ?? "",
          featureFlags: body?.feature_flags ?? {},
        };
        return sessionState;
      })
      .finally(() => {
        sessionPromise = null;
      });
  }
  return sessionPromise;
}

/** The cached session without triggering a fetch. */
export function currentSession() {
  return sessionState;
}

function newIdempotencyKey() {
  const source = globalThis.crypto;
  if (source && typeof source.randomUUID === "function") {
    return source.randomUUID();
  }
  // A secure context always provides randomUUID; this keeps a non-secure
  // origin from silently sending a weak, guessable key instead of failing.
  const bytes = new Uint8Array(16);
  source.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------
// Cancellation
// ---------------------------------------------------------------------------

const channels = new Map();

/**
 * Abort whatever the named channel was doing and return a fresh signal.
 *
 * A reviewer typing in the ticket-id box produces a request per keystroke; the
 * last answer must win, and the earlier ones must stop consuming the
 * per-subject rate bound.
 */
function beginChannel(name) {
  if (name === null || name === undefined) {
    return undefined;
  }
  const previous = channels.get(name);
  if (previous !== undefined) {
    previous.abort();
  }
  const controller = new AbortController();
  channels.set(name, controller);
  return controller.signal;
}

/** Cancel every in-flight request; used when the page is being left. */
export function abortAll() {
  for (const controller of channels.values()) {
    controller.abort();
  }
  channels.clear();
}

// ---------------------------------------------------------------------------
// The one request path
// ---------------------------------------------------------------------------

function buildUrl(path, query) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (FORBIDDEN_QUERY_KEYS.has(key.toLowerCase())) {
      // Not defensive theatre: the server rejects these outright, and the
      // reason it does is that a URL ends up in logs and history.
      throw new Error("a page cursor must travel in a request header");
    }
    if (value === null || value === undefined || value === "") {
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== null && item !== undefined && item !== "") {
          params.append(key, String(item));
        }
      }
      continue;
    }
    params.append(key, String(value));
  }
  const search = params.toString();
  return search === "" ? path : `${path}?${search}`;
}

async function requestJson(path, options = {}) {
  const {
    method = "GET",
    query = null,
    cursor = null,
    body = null,
    ifMatch = null,
    channel = null,
  } = options;

  const upper = method.toUpperCase();
  const headers = { Accept: JSON_MEDIA_TYPE };
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    headers[CURSOR_HEADER] = cursor;
  }
  if (!SAFE_METHODS.has(upper)) {
    const session = await loadSession();
    headers[CSRF_HEADER] = session.csrfToken;
    headers[IDEMPOTENCY_HEADER] = newIdempotencyKey();
    headers["Content-Type"] = JSON_MEDIA_TYPE;
  }
  if (ifMatch !== null && ifMatch !== undefined) {
    headers["If-Match"] = `"v${ifMatch}"`;
  }

  let response;
  try {
    response = await fetch(buildUrl(path, query), {
      method: upper,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: beginChannel(channel),
      body: body === null ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new AbortedError();
    }
    throw new ApiError({
      status: 0,
      code: "NETWORK_UNAVAILABLE",
      title: "The console could not reach the server",
      detail: "Check the connection and try again.",
      tone: "warning",
      recoverable: true,
    });
  }

  const requestId = response.headers.get(REQUEST_ID_HEADER);
  let payload = null;
  if (response.status !== 204) {
    const text = await response.text();
    if (text !== "") {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }
  }

  if (response.ok) {
    return payload;
  }

  const envelope = payload && typeof payload === "object" ? payload.error ?? {} : {};
  const code = typeof envelope.code === "string" ? envelope.code : "";
  const words = describe(response.status, code);
  const header = response.headers.get(RETRY_AFTER_HEADER);
  const parsed = header === null ? Number.NaN : Number.parseInt(header, 10);
  const retryAfterS =
    response.status === 429
      ? Number.isFinite(parsed) && parsed > 0
        ? parsed
        : FALLBACK_RETRY_AFTER_S
      : null;
  if (retryAfterS !== null) {
    noteCooldown(retryAfterS);
  }
  if (response.status === 401) {
    sessionState = null;
  }
  throw new ApiError({
    status: response.status,
    code: code === "" ? `HTTP_${response.status}` : code,
    title: words.title,
    detail: words.detail,
    tone: words.tone ?? "error",
    recoverable: words.recoverable,
    requestId: typeof envelope.request_id === "string" ? envelope.request_id : requestId,
    retryAfterS,
    currentVersion:
      typeof envelope.current_version === "number" ? envelope.current_version : null,
    changedAt: typeof envelope.changed_at === "string" ? envelope.changed_at : null,
  });
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------

/** Configuration-derived readiness. Public, so it also works before sign-in. */
export async function readiness() {
  try {
    return await requestJson(READINESS_PATH, { channel: "readiness" });
  } catch (error) {
    if (error instanceof AbortedError) {
      throw error;
    }
    return null;
  }
}

/**
 * One live ticket page, or exactly one ticket by identifier.
 *
 * An exact identifier is mutually exclusive with every list filter and with a
 * cursor, and the whole request is sent as asked even when it conflicts. The
 * server's 422 is the answer a forged or stale link deserves; dropping the
 * conflicting half here would make the interface disagree with its own URL and
 * hide a real refusal behind a plausible-looking result.
 */
export async function listTickets({
  ticketId = "",
  stage = "",
  state = "",
  sourceChannel = "",
  subtype = "",
  createdDate = "",
  modifiedDate = "",
  cursor = null,
  mode = "after",
  pageSize = 25,
} = {}) {
  const query = {
    stage: stage === "" ? null : [stage],
    state: state === "" ? null : [state],
    ticket_source_channel: sourceChannel === "" ? null : [sourceChannel],
    ticket_subtype: subtype === "" ? null : [subtype],
    created_date: createdDate,
    modified_date: modifiedDate,
    page_size: pageSize,
  };
  if (ticketId !== "") {
    query.ticket_id = ticketId;
  } else {
    query.mode = mode;
  }
  return requestJson(`${API_ROOT}/tickets`, { query, cursor, channel: "list" });
}

/** Start of day, in UTC, for a `YYYY-MM-DD` control value. */
function dayStart(value) {
  return value === "" ? null : `${value}T00:00:00Z`;
}

/** End of day, in UTC, so an inclusive "on or before" really includes it. */
function dayEnd(value) {
  return value === "" ? null : `${value}T23:59:59Z`;
}

/**
 * One durable review page.
 *
 * The grammar is a status set plus at most one facet, or an exact identifier on
 * its own. There is no substring search to offer, so none is sent.
 */
export async function listReviews({
  displayId = "",
  statuses = [],
  facet = "",
  facetValue = "",
  updatedAfter = "",
  updatedBefore = "",
  includeReversed = false,
  cursor = null,
  pageSize = 25,
} = {}) {
  const query = {
    statuses,
    updated_after: dayStart(updatedAfter),
    updated_before: dayEnd(updatedBefore),
    page_size: pageSize,
  };
  if (displayId !== "") {
    // Sent alongside whatever else is asked for, so an unsupported combination
    // produces the server's stable refusal rather than a quietly narrowed query.
    query.devrev_display_id = displayId;
  }
  if (includeReversed) {
    query.include_reversed = "true";
  }
  if (facet !== "" && facetValue !== "") {
    query.facet = facet;
    query.facet_value = facetValue;
  }
  return requestJson(`${API_ROOT}/reviews`, { query, cursor, channel: "list" });
}

/**
 * Import a live ticket into the durable queue.
 *
 * Idempotent by key: a double click creates one review, and a retry after a
 * timeout does not create a second.
 */
export async function createReview(ticketRef, seed = {}) {
  return requestJson(`${API_ROOT}/tickets/${encodeURIComponent(ticketRef)}/review`, {
    method: "POST",
    body: seed,
    channel: null,
  });
}

/** One review, by identifier. */
export async function getReview(reviewId) {
  return requestJson(`${API_ROOT}/reviews/${encodeURIComponent(reviewId)}`, {
    channel: "detail",
  });
}

/** A versioned update. `version` is the value the row was read at. */
export async function patchReview(reviewId, patch, version) {
  return requestJson(`${API_ROOT}/reviews/${encodeURIComponent(reviewId)}`, {
    method: "PATCH",
    body: patch,
    ifMatch: version,
    channel: null,
  });
}

// ---------------------------------------------------------------------------
// Detail subresources
//
// Four independent channels, deliberately. A reviewer who opens a ticket, moves
// to the evidence tab, and then opens a different ticket must not have the
// second ticket's conversation cancelled by the first ticket's evidence request
// — and the *newest* request on each channel must always win.
// ---------------------------------------------------------------------------

/**
 * One ticket's live data, durable review, first conversation page, and evidence.
 *
 * `timelineCursor` pages the embedded first conversation page only; every later
 * page comes from `getTimelinePage`, which is the route that exists for it.
 */
export async function getTicketDetail(ticketRef, { timelineCursor = null } = {}) {
  return requestJson(`${API_ROOT}/tickets/${encodeURIComponent(ticketRef)}`, {
    cursor: timelineCursor,
    channel: "detail",
  });
}

/**
 * One bounded conversation page.
 *
 * Forward only: the upstream's timeline pagination has no backward mode in the
 * allowlisted surface, so the server mints no `before` token and none is asked
 * for. Going back means the pages already on screen.
 */
export async function getTimelinePage(ticketRef, { cursor = null, pageSize = 25 } = {}) {
  return requestJson(`${API_ROOT}/tickets/${encodeURIComponent(ticketRef)}/timeline`, {
    query: { page_size: pageSize },
    cursor,
    channel: "timeline",
  });
}

/**
 * One page of the append-only audit ledger.
 *
 * The server returns a null `next_cursor` here by design — the repository's
 * audit read is ordered and bounded but mints no token — so the caller must
 * render that as "this is the whole page", never as "there is more".
 */
export async function listAuditEvents(reviewId, { pageSize = 50 } = {}) {
  return requestJson(`${API_ROOT}/reviews/${encodeURIComponent(reviewId)}/audit-events`, {
    query: { page_size: pageSize },
    channel: "audit",
  });
}

/** One page of confirmed evidence links. This one does page. */
export async function listEvidenceLinks(reviewId, { cursor = null, pageSize = 25 } = {}) {
  return requestJson(`${API_ROOT}/reviews/${encodeURIComponent(reviewId)}/evidence-links`, {
    query: { page_size: pageSize },
    cursor,
    channel: "evidence",
  });
}

/**
 * Confirm a server-minted suggestion as a durable, reasoned link.
 *
 * The token is echoed back exactly as received. It is sealed by the server and
 * bound to the ticket, the review, the reviewer, and an expiry, so there is
 * nothing in it for this module to inspect, shorten, or rebuild — and no
 * execution identifier is ever chosen here.
 */
export async function createEvidenceLink(reviewId, { candidateToken, reason }, version) {
  return requestJson(`${API_ROOT}/reviews/${encodeURIComponent(reviewId)}/evidence-links`, {
    method: "POST",
    body: { broker_candidate_token: candidateToken, reason },
    ifMatch: version,
    channel: null,
  });
}

/** Retire a link. The reason is mandatory and lands in the audit ledger. */
export async function deleteEvidenceLink(reviewId, linkId, { reason }, version) {
  const path =
    `${API_ROOT}/reviews/${encodeURIComponent(reviewId)}` +
    `/evidence-links/${encodeURIComponent(linkId)}`;
  return requestJson(path, {
    method: "DELETE",
    body: { reason },
    ifMatch: version,
    channel: null,
  });
}

// ---------------------------------------------------------------------------
// Remediation batches
//
// The whole human half of the Stage 8 handoff. The agent half — claim,
// heartbeat, materialize, patch, release — is deliberately absent: those routes
// belong to one verified service account, and a browser that could call them
// would be a browser that could impersonate the agent.
//
// Every write here sends the version in the JSON body rather than in `If-Match`.
// That is the batch envelope's own contract, and it is why these functions do
// not take a `version` argument in the position the review functions use.
// ---------------------------------------------------------------------------

const BATCHES_ROOT = `${API_ROOT}/remediation-batches`;

/**
 * Freeze the selected reviews at the versions the reviewer just saw.
 *
 * `refs` is `[{reviewId, reviewVersion}]`. Each version is a precondition: a
 * review that moved since the row was rendered fails the whole creation rather
 * than being frozen at a state nobody chose.
 */
export async function createRemediationBatch(refs, { transitionToPlanned = false } = {}) {
  return requestJson(BATCHES_ROOT, {
    method: "POST",
    body: {
      review_refs: refs.map((ref) => ({
        review_id: ref.reviewId,
        review_version: ref.reviewVersion,
      })),
      transition_to_planned: transitionToPlanned === true,
    },
    channel: null,
  });
}

/** One batch, without its lease credential. */
export async function getRemediationBatch(batchId) {
  return requestJson(`${BATCHES_ROOT}/${encodeURIComponent(batchId)}`, {
    channel: "batch",
  });
}

/** One bounded page of frozen items. Never carries conversation. */
export async function getRemediationBatchItems(batchId, { cursor = null, pageSize = 25 } = {}) {
  return requestJson(`${BATCHES_ROOT}/${encodeURIComponent(batchId)}/items`, {
    query: { page_size: pageSize },
    cursor,
    channel: "batch",
  });
}

function batchAction(batchId, action, body) {
  return requestJson(`${BATCHES_ROOT}/${encodeURIComponent(batchId)}:${action}`, {
    method: "POST",
    body,
    channel: null,
  });
}

/** `draft`/`blocked` -> `ready`, after the server re-checks for drift. */
export async function readyRemediationBatch(batchId, { expectedVersion, reason = null }) {
  return batchAction(batchId, "ready", {
    expected_version: expectedVersion,
    reason: reason === null || reason === "" ? null : reason,
  });
}

/** Terminal, reasoned cancellation. */
export async function cancelRemediationBatch(batchId, { expectedVersion, reason }) {
  return batchAction(batchId, "cancel", {
    expected_version: expectedVersion,
    reason,
  });
}

/** `changes_proposed` -> `verifying`, by somebody who did not author it. */
export async function startBatchVerification(batchId, { expectedVersion, attestation, reason = null }) {
  return batchAction(batchId, "start-verification", {
    expected_version: expectedVersion,
    independent_verifier_attestation: attestation,
    reason: reason === null || reason === "" ? null : reason,
  });
}

/**
 * `verifying` -> `completed`, with the verifier's own evidence.
 *
 * `decisions` is `[{reviewId, decision, resolution}]`. A `fixed` resolution
 * without evidence is refused by the server, which is the rule that stops a
 * review closing on the agent's own say-so.
 */
export async function completeRemediationBatch(
  batchId,
  { expectedVersion, decision, evidence = [], decisions = [], reason = null }
) {
  return batchAction(batchId, "complete", {
    expected_version: expectedVersion,
    decision,
    verification_evidence: evidence,
    per_review_decisions: decisions.map((entry) => ({
      review_id: entry.reviewId,
      decision: entry.decision,
      resolution: entry.resolution ?? null,
    })),
    reason: reason === null || reason === "" ? null : reason,
  });
}

/** The one bounded, admin-only extension past the continuous cap. */
export async function extendBatchLease(batchId, { expectedVersion, additionalMinutes, reason }) {
  return batchAction(batchId, "extend-lease", {
    expected_version: expectedVersion,
    additional_minutes: additionalMinutes,
    reason,
  });
}

/**
 * The reusable prompt, as text.
 *
 * A separate path from `requestJson` because the response is
 * `text/plain; charset=utf-8`, and because the result must never be persisted:
 * it is returned to the caller, handed to the clipboard, and dropped. It is not
 * cached in this module and it never enters browser storage.
 */
export async function getRemediationBatchPrompt(batchId) {
  const url = buildUrl(`${BATCHES_ROOT}/${encodeURIComponent(batchId)}/prompt`, null);
  let response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "text/plain" },
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new AbortedError();
    }
    throw new ApiError({
      status: 0,
      code: "NETWORK_UNAVAILABLE",
      title: "The console could not reach the server",
      detail: "Check the connection and try again.",
      tone: "warning",
      recoverable: true,
    });
  }
  if (!response.ok) {
    const words = describe(response.status, "");
    throw new ApiError({
      status: response.status,
      code: `HTTP_${response.status}`,
      title: words.title,
      detail: words.detail,
      tone: words.tone ?? "error",
      recoverable: words.recoverable,
      requestId: response.headers.get(REQUEST_ID_HEADER),
    });
  }
  return response.text();
}
