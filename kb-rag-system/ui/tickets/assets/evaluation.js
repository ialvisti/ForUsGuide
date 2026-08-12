/**
 * The evaluation form: reviewer fields, the structured judgment this
 * console adds, and the concurrency machinery that keeps two reviewers from
 * quietly overwriting one another.
 *
 * Four decisions here are load-bearing.
 *
 * **Saving is explicit.** There is no autosave. A reviewer's comment is a
 * considered paragraph, and a debounced write of a half-finished sentence would
 * put it in a durable record — and in the audit ledger — before it was meant.
 *
 * **Only changed fields are sent.** The patch surface treats a key that is
 * present as a value to write, including a present `null`. Sending the whole
 * form would therefore rewrite fields the reviewer never touched, which is how a
 * concurrent edit to a field nobody was arguing about gets lost.
 *
 * **A refused save keeps the reviewer's text.** On a stale-version conflict the
 * draft survives untouched and the two versions are shown side by side. Nothing
 * is merged automatically and nothing is retried against the new version: a
 * blind retry is precisely the silent overwrite the version check exists to
 * prevent.
 *
 * **The authenticated actor and the assignment are different facts.** Who is
 * signed in is shown in the page header. Who the review is assigned to is a
 * field. A historical display name is a third fact. This module never lets one
 * become another.
 */

import {
  FIELD_LIMITS,
  MODIFIED_SURFACES,
  OBSERVATION_TYPES,
  REMEDIATION_TARGETS,
  RESOLUTION_OUTCOMES,
  SEVERITIES,
  allowedNextStatuses,
  dirtyFieldNames,
  savedValue,
  statusNeedsResolution,
} from "./state.js";
import {
  MODIFIED_SURFACE_LABELS,
  OBSERVATION_LABELS,
  REMEDIATION_LABELS,
  SEVERITY_LABELS,
  STATUS_LABELS,
  definitionRow,
  el,
  fieldLabel,
  outcomeLabel,
  replaceChildren,
} from "./render.js";
import { closingRequirements } from "./remediation.js";

/** Roles the server lets change a review. Mirrored so controls can be honest. */
const MUTATING_ROLES = new Set(["reviewer", "remediator", "admin"]);

/** Where the character counter turns from informative into a warning. */
const NEAR_LIMIT_FRACTION = 0.9;

/** Every text control, with the limit its counter and `maxlength` must agree on. */
export const COUNTED_FIELDS = Object.freeze([
  ["eval-comments", "comments"],
  ["eval-expected-behavior", "expected_behavior"],
  ["eval-remediation-summary", "remediation_summary"],
  ["eval-verification-summary", "verification_summary"],
  ["eval-no-change-reason", "no_change_reason"],
]);

/** The form control that carries each draft key. */
const CONTROL_IDS = Object.freeze({
  observation_type: "eval-observation-type",
  comments: "eval-comments",
  expected_behavior: "eval-expected-behavior",
  severity: "eval-severity",
  remediation_target: "eval-remediation-target",
  remediation_summary: "eval-remediation-summary",
  status: "eval-status",
  assigned_reviewer: "eval-assignment",
  outcome: "eval-outcome",
  verification_summary: "eval-verification-summary",
  no_change_reason: "eval-no-change-reason",
  branch: "eval-branch",
  commit_sha: "eval-commit",
});

export function canEdit(role) {
  return MUTATING_ROLES.has(role ?? "");
}

function option(value, text, { disabled = false } = {}) {
  return el("option", { text, attrs: { value, disabled: disabled ? "" : null } });
}

function fillSelect(node, values, labels, { keepFirst = true } = {}) {
  const first = keepFirst && node.firstElementChild !== null ? node.firstElementChild : null;
  const children = first === null ? [] : [first];
  for (const value of values) {
    children.push(option(value, labels.get(value) ?? value));
  }
  replaceChildren(node, children);
}

/**
 * Fill every closed vocabulary once, at boot.
 *
 * The options are the server's own enumerations. Typing a value the server does
 * not accept is not a thing this form can do, which is what keeps a validation
 * round trip out of the ordinary path.
 */
export function populateChoices(dom) {
  fillSelect(dom.observationType, OBSERVATION_TYPES, OBSERVATION_LABELS);
  fillSelect(dom.severity, SEVERITIES, SEVERITY_LABELS);
  fillSelect(dom.remediationTarget, REMEDIATION_TARGETS, REMEDIATION_LABELS);
  replaceChildren(
    dom.modifiedSurfaces,
    MODIFIED_SURFACES.map((value) => {
      const id = `eval-surface-${value.replace(/_/g, "-")}`;
      const box = el("input", {
        attrs: { type: "checkbox", id, name: "modified_surfaces", value },
      });
      const label = el("label", {
        text: MODIFIED_SURFACE_LABELS.get(value) ?? value,
        attrs: { for: id },
      });
      const wrap = el("span", { className: "check chip-check" });
      wrap.appendChild(box);
      wrap.appendChild(label);
      return wrap;
    })
  );
  fillSelect(
    dom.outcome,
    RESOLUTION_OUTCOMES,
    new Map(RESOLUTION_OUTCOMES.map((value) => [value, outcomeLabel(value)]))
  );
}

/**
 * Offer only the statuses the closed transition table allows from here.
 *
 * An admin additionally sees the single reopen target on a closed review. Nobody
 * sees a move the table refuses, because discovering that through a failed save
 * after twenty minutes of writing is the worst possible time to learn it.
 */
export function populateStatuses(dom, { review, role }) {
  const current = review?.status ?? "unreviewed";
  // Only an administrator decides a status. For a reviewer it is a consequence
  // of saving, derived by the server, so a control here would offer a choice
  // that is not theirs to make.
  const visible = role === "admin";
  dom.statusField.hidden = !visible;
  if (!visible) {
    replaceChildren(dom.status, [option("", "Leave unchanged")]);
    dom.status.disabled = true;
    dom.statusHelp.textContent = "";
    return [];
  }
  const allowed = allowedNextStatuses(current, { role });
  const children = [option("", "Leave unchanged")];
  for (const value of allowed) {
    children.push(option(value, STATUS_LABELS.get(value) ?? value));
  }
  replaceChildren(dom.status, children);
  dom.status.disabled = allowed.length === 0 || !canEdit(role);
  dom.statusHelp.textContent = describeStatusHelp(current, allowed, role);
  return allowed;
}

function describeStatusHelp(current, allowed, role) {
  const label = STATUS_LABELS.get(current) ?? current;
  if (allowed.length === 0) {
    return (
      `${label} is a closed status. The transition table refuses every move out ` +
      `of it, and only an administrator can reopen one.`
    );
  }
  if (statusNeedsResolution(allowed[0]) || allowed.includes("resolved")) {
    return `Currently ${label}. Closing it needs a documented, defensible outcome.`;
  }
  if (role === "admin" && allowed.length === 1 && allowed[0] === "triaged") {
    return `Reopening this review sends it back to Triaged and clears its closing date.`;
  }
  return `Currently ${label}. Only the moves the review lifecycle allows are offered.`;
}

/** Whether a reviewer may take the assignment, in the server's own terms. */
export function assignmentOptions(review, session) {
  const role = session?.role ?? "viewer";
  const email = session?.email ?? "";
  const assigned = review?.assigned_reviewer ?? null;
  const mine = assigned !== null && email !== "" && assigned.email === email;
  const claimable = role === "admin" || assigned === null || mine;
  return {
    canSelfAssign: canEdit(role) && claimable && !mine,
    canUnassign: canEdit(role) && assigned !== null,
    mine,
    assignedEmail: assigned === null ? "" : (assigned.email ?? ""),
    role,
  };
}

/**
 * Say who holds the review, and why an arbitrary reassignment is not offered.
 *
 * Setting the assignment to a third person needs that person's verified subject,
 * which only the identity proxy can produce and which no route publishes. A
 * picker built from email addresses would look like it worked and would either
 * fail validation or, worse, record an unverified subject.
 */
export function describeAssignment(review, session) {
  const state = assignmentOptions(review, session);
  const parts = [];
  if (state.assignedEmail === "") {
    parts.push("Unassigned. Saving your evaluation assigns it to you.");
  } else if (state.mine) {
    parts.push("Assigned to you.");
  } else {
    parts.push(`Assigned to ${state.assignedEmail}.`);
  }
  if (!canEdit(state.role)) {
    parts.push("Your role cannot change the assignment.");
  } else if (state.role === "admin") {
    parts.push(
      "As an administrator you may take it or clear it. Handing it to a third " +
        "person needs their verified sign-in identity, which no route publishes, " +
        "so it is not offered here."
    );
  } else {
    parts.push(
      "You may take an unassigned review or release your own. Reassigning " +
        "someone else's is an administrator action."
    );
  }
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// Reading and writing the controls
// ---------------------------------------------------------------------------

function ratingValue(dom) {
  const chosen = dom.ratingGroup.querySelector("input[name='rating']:checked");
  return chosen === null ? "" : chosen.value;
}

/** The chosen surfaces, as the sorted, comma-joined string the draft carries. */
function modifiedSurfacesValue(dom) {
  const chosen = Array.from(
    dom.modifiedSurfaces.querySelectorAll("input[name='modified_surfaces']:checked")
  ).map((box) => box.value);
  return chosen.sort().join(",");
}

/** Everything the controls currently hold, keyed the way the draft is. */
export function readForm(dom) {
  const values = {};
  for (const [field, id] of Object.entries(CONTROL_IDS)) {
    const node = document.getElementById(id);
    if (node === null) {
      continue;
    }
    values[field] = node.value;
  }
  values.rating = ratingValue(dom);
  values.modified_surfaces = modifiedSurfacesValue(dom);
  return values;
}

/**
 * Write the saved review, overlaid with the draft, onto the controls.
 *
 * `force` exists for the same reason it does on the list filters: a render
 * triggered while someone is mid-sentence must not push an older value back into
 * the field they are typing in, but loading a different ticket or discarding
 * changes is a command *about* those fields and has to win.
 */
export function syncForm(dom, { review, draft, session, force = false }) {
  for (const [field, id] of Object.entries(CONTROL_IDS)) {
    if (field === "assigned_reviewer" || field === "rating") {
      continue;
    }
    const node = document.getElementById(id);
    if (node === null) {
      continue;
    }
    const value = field in draft ? String(draft[field] ?? "") : savedValue(review, field);
    if (!force && node === document.activeElement) {
      continue;
    }
    if (node.value !== value) {
      node.value = value;
    }
  }

  const rating = "rating" in draft ? String(draft.rating ?? "") : savedValue(review, "rating");
  for (const box of Array.from(dom.ratingGroup.querySelectorAll("input[name='rating']"))) {
    box.checked = box.value === rating;
  }

  const surfaces =
    "modified_surfaces" in draft
      ? String(draft.modified_surfaces ?? "")
      : savedValue(review, "modified_surfaces");
  const wanted = new Set(surfaces === "" ? [] : surfaces.split(","));
  for (const box of Array.from(
    dom.modifiedSurfaces.querySelectorAll("input[name='modified_surfaces']")
  )) {
    box.checked = wanted.has(box.value);
  }

  const assignment = "assigned_reviewer" in draft ? draft.assigned_reviewer : "keep";
  if (force || dom.assignment !== document.activeElement) {
    dom.assignment.value = assignment;
  }
  const options = assignmentOptions(review, session);
  for (const node of Array.from(dom.assignment.options)) {
    if (node.value === "self") {
      node.disabled = !options.canSelfAssign;
    } else if (node.value === "none") {
      node.disabled = !options.canUnassign;
    }
  }
  dom.assignmentHelp.textContent = describeAssignment(review, session);
}

/** Enable or disable the whole form according to what the role may do. */
export function applyRole(dom, { role, review, saving, dirty }) {
  const editable = canEdit(role);
  dom.form.setAttribute("aria-busy", saving ? "true" : "false");
  dom.save.textContent = saving ? "Saving…" : "Save review";
  for (const id of Object.values(CONTROL_IDS)) {
    const node = document.getElementById(id);
    if (node !== null && id !== "eval-status") {
      node.disabled = !editable || saving;
    }
  }
  for (const box of Array.from(dom.ratingGroup.querySelectorAll("input[name='rating']"))) {
    box.disabled = !editable || saving;
  }
  for (const box of Array.from(
    dom.modifiedSurfaces.querySelectorAll("input[name='modified_surfaces']")
  )) {
    box.disabled = !editable || saving;
  }
  dom.ratingClear.disabled = !editable || saving;
  dom.save.disabled = !editable || saving || !dirty;
  dom.reset.disabled = !editable || saving || !dirty;
  dom.roleHelp.textContent = editable
    ? review === null
      ? "This ticket has no durable review yet. Saving creates one and then " +
        "applies your evaluation to it."
      : ""
    : "Your role can read this review but not change it. Ask an administrator " +
      "for the reviewer role.";
}

/** Show the resolution fieldset exactly when a closing status is in play. */
export function syncResolutionVisibility(dom, { review, draft }) {
  const chosen = draft.status ?? "";
  const current = review?.status ?? "";
  const stored = review?.resolution ?? null;
  const closing =
    statusNeedsResolution(chosen) || statusNeedsResolution(current) || stored !== null;
  dom.resolution.hidden = !closing;
  return closing;
}

/**
 * Show the remediation record exactly when it applies.
 *
 * Below 5 is the reviewer saying something went wrong, which is the only case
 * where "what fixed it" has an answer. A record that already exists stays
 * visible whatever the rating now says, because hiding stored words is how they
 * get lost.
 */
export function syncRemediationVisibility(dom, { review, draft }) {
  const chosen =
    "rating" in draft ? String(draft.rating ?? "") : savedValue(review, "rating");
  const rating = chosen === "" ? null : Number.parseInt(chosen, 10);
  const stored =
    Boolean(review?.remediation_summary) || (review?.modified_surfaces ?? []).length > 0;
  const applies = stored || (Number.isInteger(rating) && rating < 5);
  dom.remediationRecord.hidden = !applies;
  return applies;
}

function displayLocale() {
  return document.documentElement.lang === "es" ? "es" : "en";
}

/** Update every character counter, and mark the ones approaching their bound. */
export function updateCounts(dom, { review, draft }) {
  for (const [id, field] of COUNTED_FIELDS) {
    const node = document.getElementById(id);
    const counter = document.getElementById(`${id}-count`);
    if (node === null || counter === null) {
      continue;
    }
    const limit = FIELD_LIMITS[field];
    const used = String(node.value ?? "").length;
    counter.textContent = `${used.toLocaleString(displayLocale())} of ${limit.toLocaleString(displayLocale())} characters`;
    counter.dataset.state =
      used > limit ? "over" : used >= Math.floor(limit * NEAR_LIMIT_FRACTION) ? "near" : "ok";
  }
}

// ---------------------------------------------------------------------------
// Validation and the patch body
// ---------------------------------------------------------------------------

/**
 * Everything the server would refuse, said before the round trip.
 *
 * Deliberately not a replacement for the server's own checks — it is the same
 * rules, stated early. The save still goes out and the server still has the last
 * word, so a rule that drifts produces a refusal rather than a wrong write.
 */
export function validate({ review, draft, role }) {
  const problems = [];
  if (!canEdit(role)) {
    problems.push("Your role cannot change this review.");
    return problems;
  }
  for (const [field, limit] of Object.entries(FIELD_LIMITS)) {
    if (!(field in draft)) {
      continue;
    }
    const used = String(draft[field] ?? "").length;
    if (used > limit) {
      problems.push(
        `${fieldLabel(field) || field} is ${used.toLocaleString(displayLocale())} characters; ` +
          `the limit is ${limit.toLocaleString(displayLocale())}.`
      );
    }
  }
  if ("rating" in draft && draft.rating !== "") {
    const rating = Number.parseInt(draft.rating, 10);
    if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
      problems.push("A rating has to be a whole number from 1 to 5.");
    }
  }
  const status = draft.status ?? "";
  if (status !== "") {
    const allowed = allowedNextStatuses(review?.status ?? "unreviewed", { role });
    if (!allowed.includes(status)) {
      problems.push(
        `${STATUS_LABELS.get(status) ?? status} is not reachable from ` +
          `${STATUS_LABELS.get(review?.status ?? "unreviewed") ?? "here"}.`
      );
    }
    if (statusNeedsResolution(status)) {
      for (const missing of closingRequirements(review, draft)) {
        problems.push(`Closing this review needs ${missing}.`);
      }
    }
  }
  return problems;
}

function textOrNull(value) {
  const text = String(value ?? "").trim();
  return text === "" ? null : text;
}

/**
 * Assemble the resolution object a closing status needs.
 *
 * Built by overlaying the changed fields onto whatever the review already
 * carries, because the stored object holds machine-checked test evidence this
 * form cannot author — and sending a fresh object without it would erase the only
 * defensible part of the record.
 */
function resolutionBody(review, draft) {
  const existing = review?.resolution ?? null;
  const body = existing === null ? {} : { ...existing };
  const mapping = {
    outcome: "outcome",
    verification_summary: "verification_summary",
    no_change_reason: "no_change_reason",
    branch: "branch",
    commit_sha: "commit_sha",
  };
  for (const [key, target] of Object.entries(mapping)) {
    if (key in draft) {
      body[target] = textOrNull(draft[key]);
    }
  }
  if (!body.outcome) {
    return null;
  }
  return body;
}

/**
 * Turn the draft into the versioned patch this save needs. The RAG ingestion
 * path creates the linked review; the browser never creates one manually.
 */
export function buildSave({ review, draft, session }) {
  const changed = dirtyFieldNames(draft, review);
  const patch = {};
  const NULLABLE_TEXT = new Set([
    "comments",
    "expected_behavior",
    "remediation_summary",
  ]);
  const NULLABLE_ENUM = new Set(["observation_type", "severity"]);

  for (const field of changed) {
    if (NULLABLE_TEXT.has(field)) {
      patch[field] = textOrNull(draft[field]);
    } else if (NULLABLE_ENUM.has(field)) {
      patch[field] = draft[field] === "" ? null : draft[field];
    } else if (field === "remediation_target") {
      // Not nullable on the record: its own "unknown" member is the absence.
      patch[field] = draft[field] === "" ? "unknown" : draft[field];
    } else if (field === "rating") {
      patch[field] = draft[field] === "" ? null : Number.parseInt(draft[field], 10);
    } else if (field === "modified_surfaces") {
      // A repeated field travels as a list. An empty draft is an explicit clear,
      // not an omission: the field only reaches `changed` when it differs from
      // what is stored.
      patch[field] = draft[field] === "" ? [] : String(draft[field]).split(",");
    } else if (field === "status") {
      if (draft[field] !== "") {
        patch[field] = draft[field];
      }
    } else if (field === "assigned_reviewer") {
      if (draft[field] === "self") {
        patch[field] = {
          subject: session?.subject ?? "",
          email: session?.email ?? "",
          display_name: session?.displayName ?? null,
        };
      } else if (draft[field] === "none") {
        patch[field] = null;
      }
    }
  }

  const closing = statusNeedsResolution(patch.status ?? "");
  const resolutionChanged = changed.some((field) =>
    ["outcome", "verification_summary", "no_change_reason", "branch", "commit_sha"].includes(field)
  );
  if (closing || resolutionChanged) {
    const body = resolutionBody(review, draft);
    if (body !== null) {
      patch.resolution = body;
    }
  }

  return { patch };
}

// ---------------------------------------------------------------------------
// The stale-version conflict panel
// ---------------------------------------------------------------------------

/** A value as the conflict panel should read it, never as a raw enum member. */
function readable(field, value) {
  if (value === null || value === undefined || value === "") {
    return "Not set";
  }
  if (field === "status") {
    return STATUS_LABELS.get(value) ?? String(value);
  }
  if (field === "observation_type") {
    return OBSERVATION_LABELS.get(value) ?? String(value);
  }
  if (field === "severity") {
    return SEVERITY_LABELS.get(value) ?? String(value);
  }
  if (field === "remediation_target") {
    return REMEDIATION_LABELS.get(value) ?? String(value);
  }
  if (field === "outcome") {
    return outcomeLabel(value);
  }
  if (field === "rating") {
    return `${value} of 5`;
  }
  return String(value);
}

/**
 * Show both versions and let the reviewer decide.
 *
 * The reviewer's own values are listed first and are never discarded by this
 * panel appearing. Everything else on it is a deliberate action: reload and lose
 * them, keep editing, or — for an administrator only — reapply them over the
 * newer version.
 */
export function renderConflict(dom, conflict, { role }) {
  if (conflict === null || conflict === undefined) {
    dom.conflictPanel.hidden = true;
    return;
  }
  dom.conflictPanel.hidden = false;

  const changed = new Set(conflict.changedFields ?? []);
  const fields = conflict.fields ?? [];
  dom.conflictSummary.textContent =
    `You loaded version ${conflict.loadedVersion}; the server is now at ` +
    `version ${conflict.currentVersion}` +
    (conflict.changedAt === "" || conflict.changedAt === null
      ? ". "
      : `, changed ${new Date(conflict.changedAt).toLocaleString(displayLocale())}. `) +
    (changed.size === 0
      ? "Nothing you edited differs from the saved version."
      : `These fields differ: ${[...changed].map((f) => fieldLabel(f) || f).join(", ")}.`);

  replaceChildren(
    dom.conflictMine,
    fields.map((field) =>
      definitionRow(fieldLabel(field) || field, readable(field, conflict.mine?.[field]), {
        absentNote: "Not set",
      })
    )
  );
  replaceChildren(
    dom.conflictTheirs,
    fields.map((field) => {
      const row = definitionRow(
        fieldLabel(field) || field,
        readable(field, conflict.theirs?.[field]),
        { absentNote: "Not set" }
      );
      if (changed.has(field)) {
        const value = row.querySelector("dd");
        if (value !== null) {
          value.setAttribute("data-changed", "true");
        }
      }
      return row;
    })
  );
  // Reapplying over someone else's save is an administrator decision, and it is
  // never the default: the button is absent for everyone else rather than
  // disabled, because a disabled destructive control still reads as an option.
  dom.conflictOverwrite.hidden = role !== "admin";
}

/**
 * The comparison the conflict panel renders.
 *
 * `mine` is what the reviewer has on screen, `theirs` is what the server now
 * holds, and `changedFields` is the intersection that actually disagrees. No
 * merge is computed anywhere: the whole point is that the choice is a person's.
 */
export function buildConflict({ review, draft, loadedVersion, currentVersion, changedAt }) {
  const fields = dirtyFieldNames(draft, review);
  const mine = {};
  const theirs = {};
  const changedFields = [];
  for (const field of fields) {
    mine[field] = field === "assigned_reviewer" ? draft[field] : String(draft[field] ?? "");
    theirs[field] = savedValue(review, field);
    if (String(mine[field] ?? "") !== String(theirs[field] ?? "")) {
      changedFields.push(field);
    }
  }
  return {
    loadedVersion,
    currentVersion,
    changedAt: changedAt ?? "",
    fields,
    mine,
    theirs,
    changedFields,
  };
}
