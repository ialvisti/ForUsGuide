/**
 * The detail view's controller.
 *
 * It owns four things and delegates the rest:
 *
 *   * **Hydration.** Bounded metadata first, then three subresources that page
 *     independently. A broker outage must not blank the conversation, and a
 *     failed conversation page must not claim the audit ledger is empty, so each
 *     one carries its own phase, cursor, warnings and error.
 *   * **Cancellation.** Opening a second ticket abandons the first request rather
 *     than racing it. The adapter's channels do the aborting; the serial counter
 *     here is the second guard, for the answer that was already in flight when
 *     the abort landed.
 *   * **Navigation.** A deep link, a row click, and the browser's own Back button
 *     all arrive at the same place, and none of them silently discards a draft.
 *   * **Writes.** Explicit save with a quoted version, a fresh idempotency key per
 *     attempt, and a stale-version conflict that is shown rather than resolved.
 *
 * Nothing here builds a URL out of anything a caller supplied. The ticket
 * reference is a bounded identifier that goes into a fixed path; an upstream link
 * is used only when the server sends a validated absolute one, and is refused
 * unless it parses, uses the secure scheme, and matches the host the server named
 * alongside it. Guessing a permalink from an organization slug would make this
 * console a redirector with a trustworthy label.
 */

import * as api from "./api.js";
import * as render from "./render.js";
import * as evaluation from "./evaluation.js";
import { el } from "./render.js";
import { translateUiText } from "./preferences.js";
import {
  BATCH_ACTIONS,
  BATCH_STATE_LABELS,
  CONVERSATION_FILTERS,
  WORKSPACE_PANELS,
  allowedBatchActions,
  canCurateBatches,
  feedHasMore,
  feedIsComplete,
  filterConversation,
} from "./state.js";
import { conversationStatusText, renderConversation } from "./conversation.js";
import {
  evidenceStatusText,
  renderExecutionEvidence,
  renderEvidence,
  renderEvidenceLinks,
} from "./evidence.js";
import {
  copyPromptToClipboard,
  renderBatchCard,
  renderRemediation,
} from "./remediation.js";

/** 64 zeroes: the first link of a hash chain has no parent. */
const GENESIS_EVENT_HASH = "0".repeat(64);

/** Conversation and evidence-link page size. Bounded well under the server cap. */
const FEED_PAGE_SIZE = 25;

/** Audit page size. The ledger comes back as one page, so this is the whole of it. */
const AUDIT_PAGE_SIZE = 50;

let context = null;
let dom = null;
let serial = 0;
let expandedEntries = new Set();
let lastRenderedRef = "";

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function collectDom() {
  const byId = (id) => document.getElementById(id);
  return {
    region: byId("ticket-detail"),
    heading: byId("detail-heading"),
    target: byId("detail-target"),
    crumb: byId("detail-crumb"),
    back: byId("detail-back"),
    close: byId("detail-close"),
    status: byId("detail-status"),
    runSummary: byId("run-summary"),
    runAnswer: byId("run-answer"),
    runRationale: byId("run-rationale"),
    runDiagnostics: byId("run-diagnostics"),
    runGaps: byId("run-gaps"),
    runSources: byId("run-sources"),
    runChunks: byId("run-chunks"),
    runMetadata: byId("run-metadata"),
    hydrationStatus: byId("hydration-status"),
    meta: {
      displayId: byId("meta-display-id"),
      title: byId("meta-title"),
      stage: byId("meta-stage"),
      state: byId("meta-state"),
      severity: byId("meta-severity"),
      channel: byId("meta-channel"),
      subtype: byId("meta-subtype"),
      owner: byId("meta-owner"),
      reporter: byId("meta-reporter"),
      created: byId("meta-created"),
      updated: byId("meta-updated"),
      reviewState: byId("meta-review-state"),
    },
    copyId: byId("detail-copy-id"),
    devrevLink: byId("detail-devrev-link"),
    devrevHelp: byId("detail-devrev-help"),
    addBatch: byId("detail-add-batch"),
    reload: byId("detail-reload"),
    tablist: byId("detail-tablist"),
    tabs: Array.from(document.querySelectorAll("#detail-tablist [role='tab']")),
    panels: {
      conversation: byId("panel-conversation"),
      evidence: byId("panel-evidence"),
      history: byId("panel-history"),
      remediation: byId("panel-remediation"),
    },
    conversationFilters: byId("conversation-filter-group"),
    conversationStatus: byId("conversation-status"),
    conversationList: byId("conversation-list"),
    conversationMore: byId("conversation-more"),
    conversationMoreHelp: byId("conversation-more-help"),
    evidenceStatus: byId("evidence-status"),
    evidenceExplanation: byId("evidence-explanation"),
    evidenceBody: byId("evidence-body"),
    evidenceLinksStatus: byId("evidence-links-status"),
    evidenceLinks: byId("evidence-links"),
    evidenceLinksMore: byId("evidence-links-more"),
    evidenceLinksMoreHelp: byId("evidence-links-more-help"),
    historyStatus: byId("history-status"),
    historyIntegrity: byId("history-integrity"),
    historyList: byId("history-list"),
    historyMore: byId("history-more"),
    historyMoreHelp: byId("history-more-help"),
    remediationStatus: byId("remediation-status"),
    remediationBody: byId("remediation-body"),
    batchCard: byId("batch-card"),
    batchStatus: byId("batch-status"),
    batchReason: byId("batch-reason"),
    batchPromptFallback: byId("batch-prompt-fallback"),
    actorLine: byId("evaluation-actor"),
    form: byId("evaluation-form"),
    ratingGroup: byId("eval-rating-group"),
    ratingClear: byId("eval-rating-clear"),
    observationType: byId("eval-observation-type"),
    severity: byId("eval-severity"),
    remediationTarget: byId("eval-remediation-target"),
    status: byId("eval-status"),
    statusHelp: byId("eval-status-help"),
    assignment: byId("eval-assignment"),
    assignmentHelp: byId("eval-assignment-help"),
    resolution: byId("eval-resolution"),
    outcome: byId("eval-outcome"),
    errors: byId("eval-errors"),
    save: byId("eval-save"),
    reset: byId("eval-reset"),
    dirty: byId("eval-dirty"),
    roleHelp: byId("eval-role-help"),
    conflictPanel: byId("conflict-panel"),
    conflictSummary: byId("conflict-summary"),
    conflictMine: byId("conflict-mine"),
    conflictTheirs: byId("conflict-theirs"),
    conflictReload: byId("conflict-reload"),
    conflictKeep: byId("conflict-keep"),
    conflictOverwrite: byId("conflict-overwrite"),
  };
}

/**
 * Install the detail view.
 *
 * The controller is handed the store, the toast surface and the icon map rather
 * than reaching for them, so the list view stays the one place that owns them.
 */
export function initDetail(host) {
  context = host;
  dom = collectDom();
  evaluation.populateChoices(dom);
  wireNavigation();
  wirePanels();
  wireConversation();
  wireEvidence();
  wireForm();
  wireBatchControls();
  wireUnloadGuard();
  return { open, close, render: renderDetail };
}

function state() {
  return context.store.getState();
}

function detailState() {
  return state().detail;
}

function dispatch(action) {
  context.store.dispatch(action);
}

function session() {
  return state().session;
}

function role() {
  return session()?.role ?? "viewer";
}

function reviewId() {
  return detailState().review?.review_id ?? "";
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

/**
 * Open one ticket.
 *
 * A pending draft is never discarded silently: leaving it is a decision, so the
 * reviewer is asked, and declining leaves them where they were.
 */
export async function open(ref, { source = null } = {}) {
  const current = detailState();
  if (current.dirty && current.ref !== ref && !confirmDiscard()) {
    return false;
  }
  if (current.ref === ref && current.phase !== "idle") {
    // Already here. Back and forward across the same selection must not re-fetch
    // and must not throw away a draft; `Reload ticket` is the deliberate refresh.
    moveWorkspaceIntoView();
    return true;
  }
  context.rememberFocus(source);
  dispatch({ type: "detail/open", id: ref });
  expandedEntries = new Set();
  moveWorkspaceIntoView();
  await load();
  return true;
}

function moveWorkspaceIntoView() {
  const reduceMotion = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  dom.region.scrollIntoView({
    behavior: reduceMotion ? "auto" : "smooth",
    block: "start",
  });
  dom.region.focus({ preventScroll: true });
}

export function close() {
  if (detailState().dirty && !confirmDiscard()) {
    return false;
  }
  dispatch({ type: "detail/close" });
  context.restoreFocus();
  return true;
}

function confirmDiscard(message = "This review has unsaved changes. Leaving now discards them. Continue?") {
  return globalThis.confirm(
    translateUiText(
      message,
      document.documentElement.lang
    )
  );
}

/** Hydrate metadata, then fan out to the three independent subresources. */
async function load() {
  const ref = detailState().ref;
  if (ref === "") {
    return;
  }
  const mine = (serial += 1);
  dispatch({ type: "feed/started", feed: "conversation" });
  try {
    const envelope = await api.getExecutionDetail(ref);
    if (mine !== serial) {
      return;
    }
    dispatch({
      type: "detail/loaded",
      execution: envelope?.execution ?? null,
      ticket: envelope?.ticket ?? null,
      review: envelope?.review ?? null,
      evidence: envelope?.evidence ?? null,
      partial: Boolean(envelope?.partial),
      warnings: Array.isArray(envelope?.warnings) ? envelope.warnings : [],
      diagnostics: envelope?.execution?.diagnostics ?? {},
    });

    await loadInitialConversation(ref);
    if (mine !== serial) {
      return;
    }

    if (envelope?.review) {
      await Promise.allSettled([loadAudit(), loadEvidenceLinks({ append: false })]);
    }
  } catch (error) {
    if (error instanceof api.AbortedError || mine !== serial) {
      return;
    }
    dispatch({ type: "detail/failed", error });
    dispatch({ type: "feed/failed", feed: "conversation", error });
    context.reportError(error);
  }
}

async function loadInitialConversation(ref) {
  try {
    const timeline = await api.getTimelinePage(ref, {
      cursor: null,
      pageSize: FEED_PAGE_SIZE,
    });
    if (detailState().ref !== ref) {
      return;
    }
    dispatch({
      type: "feed/loaded",
      feed: "conversation",
      items: Array.isArray(timeline?.messages) ? timeline.messages : [],
      nextCursor: timeline?.next_cursor ?? null,
      partial: Boolean(timeline?.partial) || Boolean(timeline?.truncated),
      warnings: Array.isArray(timeline?.warnings) ? timeline.warnings : [],
      diagnostics: Array.isArray(timeline?.diagnostics) ? timeline.diagnostics : [],
      append: false,
    });
  } catch (error) {
    if (error instanceof api.AbortedError || detailState().ref !== ref) {
      return;
    }
    dispatch({
      type: "feed/failed",
      feed: "conversation",
      error: {
        title: "The conversation is unavailable",
        detail: "The RAG execution remains available while DevRev conversation context is unavailable.",
      },
    });
  }
}

async function loadConversationPage() {
  const current = detailState();
  const cursor = current.conversation.nextCursor;
  if (cursor === null || current.ref === "") {
    return;
  }
  dispatch({ type: "feed/started", feed: "conversation" });
  try {
    const page = await api.getTimelinePage(current.ref, {
      cursor,
      pageSize: FEED_PAGE_SIZE,
    });
    dispatch({
      type: "feed/loaded",
      feed: "conversation",
      items: Array.isArray(page?.messages) ? page.messages : [],
      nextCursor: page?.next_cursor ?? null,
      partial: Boolean(page?.partial) || Boolean(page?.truncated),
      warnings: Array.isArray(page?.warnings) ? page.warnings : [],
      diagnostics: Array.isArray(page?.diagnostics) ? page.diagnostics : [],
      append: true,
    });
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    dispatch({ type: "feed/failed", feed: "conversation", error });
    context.reportError(error);
  }
}

async function loadAudit() {
  const id = reviewId();
  if (id === "") {
    return;
  }
  dispatch({ type: "feed/started", feed: "audit" });
  try {
    const page = await api.listAuditEvents(id, { pageSize: AUDIT_PAGE_SIZE });
    dispatch({
      type: "feed/loaded",
      feed: "audit",
      items: Array.isArray(page?.items) ? page.items : [],
      nextCursor: page?.next_cursor ?? null,
      partial: Boolean(page?.partial) || Boolean(page?.truncated),
      warnings: Array.isArray(page?.warnings) ? page.warnings : [],
      append: false,
    });
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    dispatch({ type: "feed/failed", feed: "audit", error });
  }
}

async function loadEvidenceLinks({ append }) {
  const id = reviewId();
  if (id === "") {
    return;
  }
  const cursor = append ? detailState().evidenceLinks.nextCursor : null;
  dispatch({ type: "feed/started", feed: "evidenceLinks" });
  try {
    const page = await api.listEvidenceLinks(id, { cursor, pageSize: FEED_PAGE_SIZE });
    dispatch({
      type: "feed/loaded",
      feed: "evidenceLinks",
      items: Array.isArray(page?.items) ? page.items : [],
      nextCursor: page?.next_cursor ?? null,
      partial: Boolean(page?.partial) || Boolean(page?.truncated),
      warnings: Array.isArray(page?.warnings) ? page.warnings : [],
      append,
    });
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    dispatch({ type: "feed/failed", feed: "evidenceLinks", error });
  }
}

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

async function save() {
  const current = detailState();
  if (current.review === null) {
    context.toast({
      title: "The linked review is unavailable",
      body: "This execution stays readable, but reviewer fields cannot be changed until its review record is available.",
      tone: "warning",
    });
    return;
  }
  const problems = evaluation.validate({
    review: current.review,
    draft: current.draft,
    role: role(),
  });
  if (problems.length > 0) {
    // The element carries `role="alert"`, so writing to it announces it without
    // moving focus away from the control the reviewer is in.
    dom.errors.textContent = problems.join(" ");
    return;
  }
  dom.errors.textContent = "";

  const plan = evaluation.buildSave({
    review: current.review,
    draft: current.draft,
    session: session(),
  });
  if (Object.keys(plan.patch).length === 0) {
    context.toast({ title: "Nothing to save", body: "No field differs from the stored review." });
    return;
  }

  dispatch({ type: "save/started" });
  try {
    let review = current.review;
    if (Object.keys(plan.patch).length > 0) {
      review = await api.patchReview(review.review_id, plan.patch, review.version);
    }
    dispatch({ type: "save/succeeded", review });
    context.toast({
      title: "Review saved",
      body: `Now at version ${review.version}.`,
    });
    await Promise.allSettled([loadAudit(), loadEvidenceLinks({ append: false })]);
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    dispatch({ type: "save/failed", error });
    if (error.status === 412) {
      // The draft is untouched and stays on screen. Retrying against the new
      // version without asking is exactly the silent overwrite the precondition
      // exists to stop, so the reviewer is shown both and decides.
      dispatch({
        type: "conflict/opened",
        conflict: evaluation.buildConflict({
          review: current.review,
          draft: current.draft,
          loadedVersion: current.version,
          currentVersion: error.currentVersion,
          changedAt: error.changedAt,
        }),
      });
      await refreshServerVersion();
      return;
    }
    context.reportError(error);
  }
}

/**
 * Re-read the stored review so the conflict panel shows real current values.
 *
 * Only the record is refreshed. The draft is deliberately left alone: this call
 * exists to tell the reviewer what they are choosing between, not to resolve it.
 */
async function refreshServerVersion() {
  const id = reviewId();
  if (id === "") {
    return;
  }
  try {
    const review = await api.getReview(id);
    const current = detailState();
    dispatch({
      type: "conflict/opened",
      conflict: evaluation.buildConflict({
        review,
        draft: current.draft,
        loadedVersion: current.version,
        currentVersion: review.version,
        changedAt: review.updated_at,
      }),
    });
  } catch {
    // The panel already carries the version and timestamp from the refusal
    // itself, which is enough to describe the conflict.
  }
}

async function confirmCandidate(index) {
  const current = detailState();
  const candidate = current.evidence?.candidate_links?.[index];
  if (candidate === undefined) {
    return;
  }
  const input = dom.evidenceBody.querySelector(`[data-candidate-reason="${index}"]`);
  const reason = (input?.value ?? "").trim();
  if (reason === "") {
    context.toast({
      title: "A reason is required",
      body: "Say why this evidence belongs to this ticket; it is written to the audit ledger.",
      tone: "error",
    });
    input?.focus();
    return;
  }
  try {
    const review = await api.createEvidenceLink(
      current.review.review_id,
      { candidateToken: candidate.candidate_token, reason },
      current.version
    );
    dispatch({ type: "save/succeeded", review });
    context.toast({ title: "Evidence linked", body: "The link and its reason are in the ledger." });
    await load();
  } catch (error) {
    if (!(error instanceof api.AbortedError)) {
      context.reportError(error);
    }
  }
}

async function unlinkEvidence(linkId) {
  const current = detailState();
  const input = dom.evidenceLinks.querySelector(`[data-unlink-reason="${linkId}"]`);
  const reason = (input?.value ?? "").trim();
  if (reason === "") {
    context.toast({
      title: "A reason is required",
      body: "An unexplained unlink cannot be told apart from tampering later.",
      tone: "error",
    });
    input?.focus();
    return;
  }
  const approved = globalThis.confirm(translateUiText(
    "Unlink this evidence from the review? The reason will remain in the audit ledger.",
    document.documentElement.lang
  ));
  if (!approved) return;
  try {
    const review = await api.deleteEvidenceLink(
      current.review.review_id,
      linkId,
      { reason },
      current.version
    );
    dispatch({ type: "save/succeeded", review });
    context.toast({ title: "Evidence unlinked" });
    await Promise.allSettled([loadAudit(), loadEvidenceLinks({ append: false })]);
  } catch (error) {
    if (!(error instanceof api.AbortedError)) {
      context.reportError(error);
    }
  }
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

function wireNavigation() {
  dom.close.addEventListener("click", () => close());
  dom.back.addEventListener("click", () => close());
  dom.reload.addEventListener("click", () => load());
  dom.copyId.addEventListener("click", async () => {
    const id = detailState().ref;
    try {
      await globalThis.navigator.clipboard.writeText(id);
      context.toast({ title: "Execution ID copied", body: id });
    } catch {
      // Clipboard access can be refused, and the identifier is already on
      // screen and selectable, so this is a convenience rather than the path.
      context.toast({
        title: "Could not copy automatically",
        body: `Select and copy it from the page: ${id}`,
        tone: "warning",
      });
    }
  });
}

function wirePanels() {
  for (const tab of dom.tabs) {
    tab.addEventListener("click", () => {
      dispatch({ type: "detail/panel", panel: tab.dataset.panel });
    });
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
        return;
      }
      event.preventDefault();
      const index = dom.tabs.indexOf(tab);
      const step = event.key === "ArrowRight" ? 1 : dom.tabs.length - 1;
      const next = dom.tabs[(index + step) % dom.tabs.length];
      next.focus();
      dispatch({ type: "detail/panel", panel: next.dataset.panel });
    });
  }
}

function wireConversation() {
  dom.conversationFilters.addEventListener("change", (event) => {
    const value = event.target.value;
    if (CONVERSATION_FILTERS.includes(value)) {
      dispatch({ type: "detail/conversation-filter", value });
    }
  });
  dom.conversationMore.addEventListener("click", () => loadConversationPage());
  dom.conversationList.addEventListener("click", (event) => {
    const control = event.target.closest("[data-action='toggle-entry']");
    if (control === null) {
      return;
    }
    const id = control.dataset.entryId;
    if (expandedEntries.has(id)) {
      expandedEntries.delete(id);
    } else {
      expandedEntries.add(id);
    }
    renderDetail(state());
  });
  dom.historyMore.addEventListener("click", () => loadAudit());
}

function wireEvidence() {
  dom.evidenceBody.addEventListener("click", (event) => {
    const control = event.target.closest("[data-action='confirm-candidate']");
    if (control !== null) {
      confirmCandidate(Number.parseInt(control.dataset.index, 10));
    }
  });
  dom.evidenceLinks.addEventListener("click", (event) => {
    const control = event.target.closest("[data-action='unlink-evidence']");
    if (control !== null) {
      unlinkEvidence(control.dataset.linkId);
    }
  });
  dom.evidenceLinksMore.addEventListener("click", () => loadEvidenceLinks({ append: true }));
}

function wireForm() {
  const capture = () => {
    dispatch({ type: "draft/patch", patch: evaluation.readForm(dom) });
  };
  // Every control reports on both events: a keystroke is an `input`, a select and
  // a radio are a `change`, and a paste is neither reliably.
  dom.form.addEventListener("input", capture);
  dom.form.addEventListener("change", capture);
  dom.form.addEventListener("submit", (event) => event.preventDefault());

  dom.ratingClear.addEventListener("click", () => {
    for (const box of Array.from(dom.ratingGroup.querySelectorAll("input[name='rating']"))) {
      box.checked = false;
    }
    dispatch({ type: "draft/patch", patch: { rating: "" } });
  });

  dom.save.addEventListener("click", () => save());
  dom.reset.addEventListener("click", () => {
    if (detailState().dirty && !confirmDiscard(
      "Discard your unsaved changes and restore the saved review?"
    )) return;
    dispatch({ type: "draft/reset" });
    dom.errors.textContent = "";
    renderDetail(state());
  });

  dom.conflictReload.addEventListener("click", async () => {
    dispatch({ type: "draft/reset" });
    dispatch({ type: "conflict/cleared" });
    await load();
  });
  dom.conflictKeep.addEventListener("click", () => {
    dispatch({ type: "conflict/cleared" });
    dom.save.focus();
  });
  dom.conflictOverwrite.addEventListener("click", async () => {
    // Explicit, admin-only, and never automatic: the reviewer's values are
    // re-sent against the version they have just been shown.
    dispatch({ type: "conflict/cleared" });
    await save();
  });
}

/**
 * Warn before the tab is closed with unsaved work.
 *
 * `preventDefault` on this event is what makes the browser ask. The prompt text
 * is the browser's own; a custom one has been ignored for years.
 */
function wireUnloadGuard() {
  globalThis.addEventListener("beforeunload", (event) => {
    if (!detailState().dirty) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function text(node, value, fallback = "Not recorded") {
  const shown = value === null || value === undefined || value === "" ? fallback : String(value);
  node.textContent = shown;
  node.dataset.absent = shown === fallback ? "true" : "false";
}

function timeInto(node, iso) {
  render.replaceChildren(node, []);
  if (!iso) {
    text(node, "");
    return;
  }
  node.dataset.absent = "false";
  node.appendChild(render.timeElement(iso));
}

/**
 * The upstream deep link, or nothing.
 *
 * Three conditions, all required: the server sent an absolute link, it parses
 * with the secure scheme, and its host is the one the server named beside it. A
 * link assembled here from a slug and a base would be a guess, and a guess that
 * navigates is an open redirect with an internal-looking label.
 */
export function resolveUpstreamLink(envelope) {
  const raw = envelope?.devrev_url ?? "";
  const allowedHost = envelope?.devrev_url_host ?? "";
  if (raw === "" || allowedHost === "") {
    return null;
  }
  let parsed = null;
  try {
    parsed = new URL(raw);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.host !== allowedHost) {
    return null;
  }
  return parsed.href;
}

function readable(value, absent = "Not recorded") {
  if (value === null || value === undefined || value === "") {
    return absent;
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function renderRunList(container, values, emptyText) {
  const items = Array.isArray(values)
    ? values
    : values !== null && typeof values === "object"
      ? Object.entries(values).map(([key, value]) => `${key}: ${readable(value)}`)
      : [];
  render.replaceChildren(
    container,
    items.length === 0
      ? [el("li", { text: emptyText })]
      : items.map((value) => el("li", { text: readable(value) }))
  );
}

function hydrationText(status) {
  if (status === "succeeded") {
    return "DevRev context loaded for this execution.";
  }
  return "This execution did not pass the authorized DevRev hydration boundary.";
}

/** Render immutable, bounded fields captured when this RAG route ran. */
function renderRun(current) {
  const execution = current.execution ?? {};
  const summary = [
    render.definitionRow("Execution ID", execution.executionId ?? current.ref),
    render.definitionRow("Invocation ID", execution.invocationId ?? execution.executionId ?? current.ref),
    render.definitionRow("Job ID", execution.jobId),
    render.definitionRow("Inquiry", execution.inquiryIndex),
    render.definitionRow("Attempt", execution.attempt),
    render.definitionRow("Lease epoch", execution.leaseEpoch),
    render.definitionRow("Route", execution.route),
    render.definitionRow("Run status", execution.runStatus),
    render.definitionRow("Started", execution.occurredAt),
  ];
  render.replaceChildren(dom.runSummary, summary);

  const generatedAnswer =
    execution.generatedAnswer ??
    execution.structuredResponse?.answer ??
    execution.structuredResponse?.response;
  dom.runAnswer.textContent = readable(generatedAnswer, "No generated answer was recorded.");

  const classificationReasoning = execution.classificationReasoning;
  const outcomeReason = execution.outcomeReason ?? execution.structuredResponse?.outcome_reason;
  render.replaceChildren(dom.runRationale, [
    render.definitionRow("Classification rationale", classificationReasoning),
    render.definitionRow("Outcome rationale", outcomeReason),
  ]);

  const diagnostics = execution.diagnostics;
  const gaps = execution.gaps;
  renderRunList(dom.runDiagnostics, diagnostics, "No diagnostics were recorded.");
  renderRunList(dom.runGaps, gaps, "No coverage gaps were recorded.");

  const sourceArticles = execution.sourceArticles ?? [];
  const chunkEvidence = execution.chunkEvidence ?? [];
  renderExecutionEvidence(dom.runSources, dom.runChunks, {
    sourceArticles,
    chunkEvidence,
  });

  render.replaceChildren(dom.runMetadata, [
    render.definitionRow("Model", readable(execution.modelMetadata)),
    render.definitionRow("Timing", readable(execution.timingMetadata)),
    render.definitionRow("Retrieval", readable(execution.retrievalMetadata)),
  ]);

  const hydrationStatus = execution.hydrationStatus ?? "unavailable";
  dom.hydrationStatus.textContent = hydrationText(hydrationStatus);
  dom.hydrationStatus.dataset.tone = hydrationStatus === "succeeded" ? "info" : "warning";
}

function renderMeta(current) {
  const ticket = current.ticket ?? {};
  const review = current.review ?? null;
  text(dom.meta.displayId, ticket.devrev_display_id ?? current.ref);
  text(dom.meta.title, ticket.title, "No summary is available");
  text(dom.meta.stage, ticket.stage);
  text(dom.meta.state, ticket.state);
  text(dom.meta.severity, ticket.severity);
  text(dom.meta.channel, ticket.source_channel);
  text(dom.meta.subtype, ticket.subtype);
  const owners = Array.isArray(ticket.owner_ids) ? ticket.owner_ids : [];
  text(dom.meta.owner, owners.length === 0 ? "" : owners.join(", "), "Unassigned upstream");
  text(dom.meta.reporter, ticket.reporter?.display_name ?? "", "Not recorded");
  timeInto(dom.meta.created, ticket.created_at);
  timeInto(dom.meta.updated, ticket.modified_at);
  text(
    dom.meta.reviewState,
    review === null
      ? "Linked review unavailable"
      : `Version ${review.version}, ${review.status ?? "unreviewed"}`
  );

  const link = resolveUpstreamLink(current.ticket);
  if (link === null) {
    dom.devrevLink.hidden = true;
    dom.devrevHelp.hidden = false;
  } else {
    dom.devrevLink.hidden = false;
    dom.devrevLink.setAttribute("href", link);
    dom.devrevLink.setAttribute("rel", "noreferrer noopener");
    dom.devrevLink.setAttribute("target", "_blank");
    dom.devrevHelp.hidden = true;
  }
}

function detailStatusText(current) {
  if (current.phase === "loading") {
    return { text: "Loading this RAG execution…", tone: "info" };
  }
  if (current.phase === "error") {
    const error = current.error ?? {};
    if (error.status === 404) {
      return {
        text: "That RAG execution is not available or is outside your scope.",
        tone: "error",
      };
    }
    if (error.status === 403) {
      return { text: "Your role does not allow reading this execution.", tone: "error" };
    }
    return { text: error.title ?? "This RAG execution could not be loaded.", tone: "error" };
  }
  const parts = [];
  let tone = "info";
  if (current.partial) {
    tone = "warning";
    parts.push(
      "Some secondary ticket subresources did not load. " +
        "Use Reload to retry those bounded reads."
    );
  }
  if (current.warnings.length > 0) {
    tone = "warning";
    parts.push(render.describeWarnings(current.warnings));
  }
  if (current.review === null && !current.partial) {
    parts.push("The linked review is temporarily unavailable; the execution is still auditable.");
  }
  return { text: parts.join(" "), tone };
}

/**
 * Check what a browser honestly can about the audit chain.
 *
 * The link continuity: every event names its parent's hash, and the parent is the
 * event before it. The hashes themselves are computed and checked server-side
 * from a frozen canonical payload, and recomputing them here would mean
 * reimplementing that canonicalisation in a second language — where a mismatch
 * would look like tampering and be a bug in this file. A broken *link* is
 * something this side can prove, so that is what is reported.
 */
export function auditChainProblem(events) {
  const ordered = [...events].sort(
    (left, right) => (left.occurred_at_unix_us ?? 0) - (right.occurred_at_unix_us ?? 0)
  );
  for (let index = 1; index < ordered.length; index += 1) {
    const parent = ordered[index - 1];
    const child = ordered[index];
    if (!parent.event_hash || !child.previous_event_hash) {
      continue;
    }
    if (child.previous_event_hash !== parent.event_hash) {
      return `Event ${child.event_id} does not link to the event recorded before it.`;
    }
  }
  return "";
}

function auditEvent(event) {
  const item = el("li", {
    className: "audit-event",
    attrs: { "data-event-id": event.event_id ?? "" },
  });
  const head = el("div", { className: "audit-head" });
  head.appendChild(
    el("span", { className: "audit-type", text: render.auditEventLabel(event.event_type) })
  );
  head.appendChild(el("span", { text: event.actor_email ?? "Actor not recorded" }));
  if (event.occurred_at_unix_us) {
    const when = el("span", { className: "entry-time" });
    when.appendChild(render.timeElement(new Date(event.occurred_at_unix_us / 1000).toISOString()));
    head.appendChild(when);
  }
  if (typeof event.new_version === "number") {
    head.appendChild(
      el("span", {
        className: "pill",
        text:
          event.previous_version === null || event.previous_version === undefined
            ? `Version ${event.new_version}`
            : `Version ${event.previous_version} to ${event.new_version}`,
        attrs: { "data-actor-class": "participant" },
      })
    );
  }
  item.appendChild(head);

  const fields = Array.isArray(event.changed_fields) ? event.changed_fields : [];
  if (fields.length > 0) {
    item.appendChild(
      el("p", {
        className: "audit-fields",
        text: `Changed: ${fields.map((field) => render.fieldLabel(field) || field).join(", ")}`,
      })
    );
  }
  const reason = event.metadata?.reason_code ?? "";
  if (reason !== "") {
    item.appendChild(el("p", { className: "audit-fields", text: `Reason code: ${reason}` }));
  }
  item.appendChild(
    el("p", {
      className: "entry-meta",
      text: `Event ${render.digest(event.event_id)}`,
    })
  );
  return item;
}

function renderPanels(current) {
  for (const name of WORKSPACE_PANELS) {
    const selected = current.panel === name;
    dom.panels[name].hidden = !selected;
    const tab = dom.tabs.find((node) => node.dataset.panel === name);
    if (tab !== undefined) {
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
    }
  }
}

function renderConversationPanel(current) {
  const feed = current.conversation;
  const all = feed.items;
  const visible = filterConversation(all, current.conversationFilter);
  renderConversation(dom.conversationList, visible, {
    expanded: expandedEntries,
    filter: current.conversationFilter,
  });
  render.setPanelStatus(
    dom.conversationStatus,
    conversationStatusText(feed, {
      shown: visible.length,
      total: all.length,
      filter: current.conversationFilter,
    })
  );
  const more = feedHasMore(feed);
  dom.conversationMore.disabled = !more || feed.phase === "loading";
  dom.conversationMore.hidden = false;
  dom.conversationMoreHelp.textContent = more
    ? "The upstream conversation pages forward only, so earlier pages stay above."
    : feedIsComplete(feed)
      ? "Every entry the ticket system returned is loaded."
      : "";
  for (const box of Array.from(
    dom.conversationFilters.querySelectorAll("input[name='conversation-filter']")
  )) {
    box.checked = box.value === current.conversationFilter;
  }
}

function executionEvidenceSummary(current) {
  const execution = current.execution ?? {};
  const sourceCount = Array.isArray(execution.sourceArticles)
    ? execution.sourceArticles.length
    : 0;
  const chunkCount = Array.isArray(execution.chunkEvidence)
    ? execution.chunkEvidence.length
    : 0;
  return {
    sourceCount,
    chunkCount,
    available: sourceCount > 0 || chunkCount > 0,
  };
}

function renderPersistedExecutionEvidence(current) {
  const sourceHeading = el("h5", {
    text: "Source articles captured by this execution",
    attrs: { id: "rag-run-evidence-sources-heading" },
  });
  const sourceList = el("ul", {
    className: "evidence-links",
    attrs: { "aria-labelledby": "rag-run-evidence-sources-heading" },
  });
  const chunkHeading = el("h5", {
    text: "Bounded chunks captured by this execution",
    attrs: { id: "rag-run-evidence-chunks-heading" },
  });
  const chunkList = el("ul", {
    className: "evidence-links",
    attrs: { "aria-labelledby": "rag-run-evidence-chunks-heading" },
  });
  renderExecutionEvidence(sourceList, chunkList, current.execution);
  render.replaceChildren(dom.evidenceBody, [
    sourceHeading,
    sourceList,
    chunkHeading,
    chunkList,
  ]);
}

function renderEvidencePanel(current) {
  const executionEvidence = executionEvidenceSummary(current);
  render.setPanelStatus(
    dom.evidenceStatus,
    executionEvidence.available
      ? {
          text:
            `This persisted RAG execution recorded ${executionEvidence.sourceCount} source ` +
            `article${executionEvidence.sourceCount === 1 ? "" : "s"} and ` +
            `${executionEvidence.chunkCount} bounded chunk` +
            `${executionEvidence.chunkCount === 1 ? "" : "s"}.`,
          tone: "info",
        }
      : evidenceStatusText(current)
  );
  const evidence = current.evidence;
  const unavailable =
    evidence === null ||
    evidence === undefined ||
    (evidence.correlation_status ?? "unavailable") === "unavailable";
  dom.evidenceExplanation.textContent = executionEvidence.available
    ? "This bounded evidence was stored with the RAG run; reviewer-confirmed links are listed separately below."
    : unavailable
      ? render.evidenceGapText(evidence?.unavailable_reason ?? "")
      : "";
  dom.evidenceExplanation.dataset.tone =
    executionEvidence.available || !unavailable ? "info" : "warning";
  if (executionEvidence.available) {
    renderPersistedExecutionEvidence(current);
  } else {
    renderEvidence(dom.evidenceBody, evidence, {
      canConfirm: evaluation.canEdit(role()) && current.review !== null,
      now: Date.now(),
    });
  }

  const feed = current.evidenceLinks;
  renderEvidenceLinks(dom.evidenceLinks, feed.items, {
    canUnlink: evaluation.canEdit(role()),
  });
  const linkTone = feed.phase === "error" ? "error" : feed.phase === "stale" ? "warning" : "info";
  render.setPanelStatus(dom.evidenceLinksStatus, {
    tone: linkTone,
    text:
      feed.phase === "loading"
        ? "Loading evidence links…"
        : feed.phase === "error"
          ? "Evidence links could not be loaded."
          : feed.phase === "stale"
            ? "Showing the links already loaded; the newest request failed."
            : current.review === null
              ? "A ticket has evidence links only once it has a durable review."
              : `${feed.items.length} link${feed.items.length === 1 ? "" : "s"} loaded.`,
  });
  const moreLinks = feedHasMore(feed);
  dom.evidenceLinksMore.disabled = !moreLinks || feed.phase === "loading";
  dom.evidenceLinksMoreHelp.textContent = moreLinks
    ? ""
    : feedIsComplete(feed)
      ? "Every evidence link is loaded."
      : "";
}

function renderHistoryPanel(current) {
  const feed = current.audit;
  if (feed.items.length === 0) {
    render.replaceChildren(dom.historyList, [
      el("li", {
        className: "audit-event",
        text:
          current.review === null
            ? "A ticket has review history only once it has a durable review."
            : feed.phase === "loading"
              ? "Loading review history…"
              : "No audit events have loaded for this review.",
      }),
    ]);
  } else {
    const ordered = [...feed.items].sort(
      (left, right) => (right.occurred_at_unix_us ?? 0) - (left.occurred_at_unix_us ?? 0)
    );
    render.replaceChildren(dom.historyList, ordered.map(auditEvent));
  }

  const problem = auditChainProblem(feed.items);
  dom.historyIntegrity.textContent =
    problem === ""
      ? feed.items.length > 0
        ? "The events on this page link to one another as an unbroken chain. The " +
          "hashes themselves are verified where they are written."
        : ""
      : `Integrity warning: ${problem} Treat this ledger as untrustworthy and report it.`;
  dom.historyIntegrity.dataset.tone = problem === "" ? "info" : "warning";

  const first = [...feed.items].sort(
    (left, right) => (left.occurred_at_unix_us ?? 0) - (right.occurred_at_unix_us ?? 0)
  )[0];
  const fromStart = first === undefined || first.previous_event_hash === GENESIS_EVENT_HASH;
  render.setPanelStatus(dom.historyStatus, {
    tone: feed.phase === "error" ? "error" : "info",
    text:
      feed.phase === "error"
        ? "Review history could not be loaded."
        : feed.items.length === 0
          ? ""
          : `${feed.items.length} event${feed.items.length === 1 ? "" : "s"}` +
            (fromStart ? ", from the first change recorded." : "; earlier events are not on this page."),
  });
  // The server returns this ledger as one bounded page and mints no forward
  // token, so the control says so instead of implying a page two.
  dom.historyMore.disabled = true;
  dom.historyMoreHelp.textContent =
    "The audit ledger is returned as a single bounded page, so there is no further " +
    "page to fetch. Reload the ticket to see events recorded since.";
}

function renderEvaluation(current) {
  const identity = session();
  const activeRole = role();
  dom.actorLine.textContent =
    identity === null
      ? "Loading who you are signed in as…"
      : `Changes will be recorded as ${identity.email}.`;

  evaluation.populateStatuses(dom, { review: current.review, role: activeRole });
  evaluation.syncForm(dom, {
    review: current.review,
    draft: current.draft,
    session: identity,
    force: current.ref !== lastRenderedRef || Object.keys(current.draft).length === 0,
  });
  evaluation.syncResolutionVisibility(dom, { review: current.review, draft: current.draft });
  evaluation.updateCounts(dom, { review: current.review, draft: current.draft });
  evaluation.applyRole(dom, {
    role: activeRole,
    review: current.review,
    saving: current.saving,
    dirty: current.dirty,
  });
  evaluation.renderConflict(dom, current.conflict, { role: activeRole });

  dom.dirty.hidden = !current.dirty;
  const flags = identity?.featureFlags ?? {};
  // Two independent reasons, and the help paragraph in the markup names both:
  // the deployment may have no agent configured, or this role may not curate.
  dom.addBatch.disabled =
    flags.remediation_enabled !== true || !canCurateBatches(activeRole);
}

// ---------------------------------------------------------------------------
// The remediation batch a resolved review belongs to
//
// A review learns its batch from `resolution.batch_id` and from nowhere else.
// That is deliberate at the API level: the master route table publishes no batch
// *list* endpoint and Stage 3 declared no index to support one, so batch access
// is always by id. The consequence here is that the card appears once a batch has
// closed the review — which is exactly when a verifier wants to read it.
//
// The batch is held in a module-level variable, never in browser storage. It is
// bounded, server-authoritative, and re-fetched on demand; persisting it would
// mean a stale version number surviving a reload and a 412 the reviewer cannot
// explain.
// ---------------------------------------------------------------------------

let loadedBatch = null;
let loadedBatchId = "";

function batchActionLabels() {
  const labels = { ...BATCH_STATE_LABELS };
  for (const [name, rule] of Object.entries(BATCH_ACTIONS)) {
    labels[`action:${name}`] = rule.label;
  }
  return labels;
}

async function loadBatchFor(review) {
  const batchId = review?.resolution?.batch_id ?? "";
  if (batchId === "" || (session()?.featureFlags ?? {}).remediation_enabled !== true) {
    loadedBatch = null;
    loadedBatchId = "";
    return;
  }
  if (batchId === loadedBatchId && loadedBatch !== null) {
    return;
  }
  loadedBatchId = batchId;
  try {
    loadedBatch = await api.getRemediationBatch(batchId);
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    loadedBatch = null;
    render.setPanelStatus(dom.batchStatus, {
      tone: "warning",
      text: `The remediation batch for this review could not be read: ${
        error.title ?? "unavailable"
      }.`,
    });
  }
}

function renderBatchPanel() {
  if (dom.batchCard === null) {
    return;
  }
  const activeRole = role();
  const actions =
    loadedBatch === null
      ? []
      : allowedBatchActions(String(loadedBatch.status ?? ""), { role: activeRole });
  renderBatchCard(dom.batchCard, loadedBatch, {
    actions,
    labels: batchActionLabels(),
  });
  render.mountIcons(dom.batchCard, context.icons());
}

/**
 * Run one human batch action, then re-read the batch.
 *
 * The version travels from the rendered control's own dataset, so the request
 * carries the version the reviewer was actually looking at. A stale one comes
 * back as a conflict naming the current version, which is reported rather than
 * retried: silently re-sending against a version the reviewer never saw is how a
 * console applies a decision to a state nobody reviewed.
 */
async function runBatchAction(name, batchId, expectedVersion) {
  const reason = (dom.batchReason?.value ?? "").trim();
  const rule = BATCH_ACTIONS[name];
  if (rule === undefined) {
    return;
  }
  if (name !== "ready" && reason === "") {
    render.setPanelStatus(dom.batchStatus, {
      tone: "warning",
      text: `“${rule.label}” needs a reason. It is recorded in the audit ledger.`,
    });
    dom.batchReason?.focus();
    return;
  }
  const version = Number.parseInt(expectedVersion, 10);
  if (!Number.isFinite(version)) {
    return;
  }
  try {
    let updated;
    if (name === "ready") {
      updated = await api.readyRemediationBatch(batchId, {
        expectedVersion: version,
        reason: reason === "" ? null : reason,
      });
    } else if (name === "cancel") {
      updated = await api.cancelRemediationBatch(batchId, {
        expectedVersion: version,
        reason,
      });
    } else if (name === "start-verification") {
      updated = await api.startBatchVerification(batchId, {
        expectedVersion: version,
        attestation: reason,
      });
    } else if (name === "complete") {
      // No per-review resolution is sent from here. Closing a review needs
      // machine-checked evidence a browser cannot honestly produce, so this
      // records the batch decision and leaves the reviews to the review form.
      const result = await api.completeRemediationBatch(batchId, {
        expectedVersion: version,
        decision: "accepted",
        reason,
      });
      updated = result?.batch ?? null;
    } else {
      updated = await api.extendBatchLease(batchId, {
        expectedVersion: version,
        additionalMinutes: 30,
        reason,
      });
    }
    loadedBatch = updated;
    if (dom.batchReason !== null) {
      dom.batchReason.value = "";
    }
    render.setPanelStatus(dom.batchStatus, {
      tone: "info",
      text: `${rule.label} applied to batch ${batchId}.`,
    });
    renderBatchPanel();
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    const current = error.currentVersion;
    render.setPanelStatus(dom.batchStatus, {
      tone: "error",
      text:
        current === null || current === undefined
          ? `${rule.label} was refused: ${error.title ?? "conflict"}.`
          : `The batch changed while you were reading it; it is now at version ` +
            `${current}. Reload the batch before deciding.`,
    });
  }
}

/** Fetch the prompt and hand it to the clipboard, or to a field the user can copy. */
async function copyBatchPrompt(batchId) {
  try {
    const text = await api.getRemediationBatchPrompt(batchId);
    const result = await copyPromptToClipboard(text, {
      fallbackField: dom.batchPromptFallback,
    });
    render.setPanelStatus(dom.batchStatus, {
      tone: "info",
      text:
        result.method === "clipboard"
          ? `The Codex prompt for batch ${batchId} is on the clipboard.`
          : result.method === "manual"
            ? "The clipboard is unavailable, so the prompt is selected in the field " +
              "below; copy it from there."
            : "The clipboard is unavailable and no fallback field is present.",
    });
  } catch (error) {
    if (error instanceof api.AbortedError) {
      return;
    }
    render.setPanelStatus(dom.batchStatus, {
      tone: "error",
      text: `The prompt could not be read: ${error.title ?? "unavailable"}.`,
    });
  }
}

function wireBatchControls() {
  if (dom.batchCard === null) {
    return;
  }
  dom.batchCard.addEventListener("click", (event) => {
    const control = event.target.closest("[data-action]");
    if (control === null) {
      return;
    }
    const action = control.dataset.action ?? "";
    const batchId = control.dataset.batchId ?? "";
    if (batchId === "") {
      return;
    }
    if (action === "copy-batch-prompt") {
      copyBatchPrompt(batchId);
      return;
    }
    if (action.startsWith("batch-")) {
      runBatchAction(
        action.slice("batch-".length),
        batchId,
        control.dataset.expectedVersion ?? ""
      );
    }
  });
}

/** One pass over the whole detail view. Cheap enough to run on every change. */
export function renderDetail(next) {
  const current = next.detail;
  if (current.ref === "") {
    dom.region.hidden = true;
    lastRenderedRef = "";
    return;
  }
  dom.region.hidden = false;
  const displayId = current.ticket?.devrev_display_id ?? current.ref;
  const title = current.ticket?.title ?? "";
  const ticketIdentity = title === "" ? displayId : `${displayId} · ${title}`;
  dom.target.textContent = ticketIdentity;
  dom.crumb.textContent = displayId;

  renderRun(current);
  renderMeta(current);
  render.setPanelStatus(dom.status, detailStatusText(current));
  renderPanels(current);
  renderConversationPanel(current);
  renderEvidencePanel(current);
  renderHistoryPanel(current);
  renderRemediation(dom.remediationBody, current.review, {
    flags: session()?.featureFlags ?? {},
    draft: current.draft,
  });
  // Fire-and-forget: the card renders as soon as the batch arrives, and a review
  // with no batch clears it synchronously. Awaiting here would make every detail
  // render wait on a request that most reviews do not need.
  loadBatchFor(current.review).then(renderBatchPanel);
  render.setPanelStatus(dom.remediationStatus, {
    tone: "info",
    text:
      current.review === null
        ? "No durable review, so nothing is planned or resolved yet."
        : "",
  });
  renderEvaluation(current);
  render.mountIcons(dom.region, context.icons());
  lastRenderedRef = current.ref;
}
