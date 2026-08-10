/**
 * The remediation and resolution panel.
 *
 * Two separate things live here, and keeping them separate is the point.
 *
 * **The remediation target** is a judgment a reviewer records: where the fix
 * belongs — the knowledge base, a prompt, code, a workflow, or the source data.
 * It is editable in the evaluation form and displayed here.
 *
 * **The resolution** is the closed record a terminal review must carry, and most
 * of it is not typed by a person. Machine-checked verification evidence — a
 * command label, an exit code, test counts, an output hash and a runtime — is
 * produced by the remediation agent, so this panel *reads* it and never offers
 * to author it. A browser cannot honestly produce an output hash.
 *
 * **Remediation batches** are the third thing, and they arrive at the end of this
 * file rather than here. A batch is a different object with a different
 * lifecycle: a remediator freezes a group of observations, one verified agent
 * identity works on them, and a second person verifies the result. The server
 * reports whether this deployment has one configured, and the control that
 * creates one stays visible-but-disabled with a reason when it does not — hiding
 * it would read as "this tool cannot do that", and enabling it hopefully would
 * read as a bug when it 503s.
 */

import {
  REMEDIATION_LABELS,
  STATUS_LABELS,
  button,
  definitionRow,
  digest,
  el,
  httpsLink,
  labelOf,
  outcomeLabel,
  replaceChildren,
  timeElement,
} from "./render.js";

function grid(rows) {
  const node = el("dl", { className: "field-grid" });
  replaceChildren(node, rows);
  return node;
}

function timeRow(term, iso) {
  const wrap = el("div");
  wrap.appendChild(el("dt", { text: term }));
  const value = el("dd");
  if (iso) {
    value.appendChild(timeElement(iso));
  } else {
    value.textContent = "Not recorded";
    value.setAttribute("data-absent", "true");
  }
  wrap.appendChild(value);
  return wrap;
}

function linkRow(term, url) {
  const wrap = el("div");
  wrap.appendChild(el("dt", { text: term }));
  const value = el("dd");
  const link = httpsLink(url);
  if (link === null) {
    value.textContent = "Not recorded";
    value.setAttribute("data-absent", "true");
  } else {
    value.appendChild(link);
  }
  wrap.appendChild(value);
  return wrap;
}

/**
 * One recorded verification run.
 *
 * The output hash is shown and the output is not: a bounded record of *whether*
 * a command passed is defensible evidence, whereas pasting test output into a
 * durable review would put unbounded text — and whatever it happens to contain —
 * into a record kept for seven years.
 */
function verificationRun(entry, index) {
  const wrap = el("div", {
    className: "evidence-record",
    attrs: { "data-kind": "verification", "data-passed": entry.exit_code === 0 ? "true" : "false" },
  });
  wrap.appendChild(el("p", { className: "audit-type", text: `Verification ${index + 1}` }));
  wrap.appendChild(
    el("span", {
      className: "pill",
      text: entry.exit_code === 0 ? "Passed" : `Exited ${entry.exit_code}`,
      attrs: entry.exit_code === 0 ? { "data-status": "resolved" } : { "data-status": "blocked" },
    })
  );
  wrap.appendChild(
    grid([
      definitionRow("Command", entry.command_label),
      definitionRow(
        "Tests",
        `${entry.passed ?? 0} passed, ${entry.failed ?? 0} failed, ${entry.skipped ?? 0} skipped`
      ),
      definitionRow(
        "Runtime",
        typeof entry.runtime_s === "number" ? `${entry.runtime_s.toFixed(1)} s` : ""
      ),
      definitionRow("Output hash", digest(entry.output_sha256), {
        full: entry.output_sha256 ?? "",
      }),
    ])
  );
  wrap.appendChild(timeRow("Ran at", entry.occurred_at));
  return wrap;
}

/** What a reviewer still has to supply before this review can be closed. */
export function closingRequirements(review, draft) {
  const outcome = draft.outcome ?? review?.resolution?.outcome ?? "";
  const summary = draft.verification_summary ?? review?.resolution?.verification_summary ?? "";
  const rationale = draft.no_change_reason ?? review?.resolution?.no_change_reason ?? "";
  const evidence = review?.resolution?.test_evidence ?? [];
  const missing = [];
  if (outcome === "") {
    missing.push("an outcome");
  }
  if (String(summary).trim() === "") {
    missing.push("a verification summary");
  }
  // The contract accepts machine-checked evidence *or* a written rationale. Only
  // the second can come from a browser, so it is required whenever the first is
  // absent — including for a `fixed` outcome, which otherwise closes a review on
  // nothing but an assertion.
  if (evidence.length === 0 && String(rationale).trim() === "") {
    missing.push("a verification rationale, since no machine-checked test evidence is attached");
  }
  return missing;
}

/** The whole panel. */
export function renderRemediation(container, review, { flags = {}, draft = {} } = {}) {
  if (review === null || review === undefined) {
    replaceChildren(container, [
      el("p", {
        className: "panel-note",
        text:
          "This execution has no linked review available, so remediation fields " +
          "cannot be changed yet. The captured RAG result remains readable.",
      }),
    ]);
    return;
  }

  const blocks = [];
  blocks.push(
    grid([
      definitionRow(
        "Remediation target",
        labelOf(REMEDIATION_LABELS, review.remediation_target) || "Unknown"
      ),
      definitionRow("Review status", labelOf(STATUS_LABELS, review.status)),
      definitionRow(
        "Observation type",
        review.observation_type === null || review.observation_type === undefined
          ? ""
          : String(review.observation_type)
      ),
    ])
  );

  const resolution = review.resolution ?? null;
  if (resolution === null) {
    blocks.push(el("h5", { text: "Not closed" }));
    const missing = closingRequirements(review, draft);
    blocks.push(
      el("p", {
        className: "panel-note",
        text:
          missing.length === 0
            ? "This review carries everything a closing status needs."
            : `Closing this review needs ${missing.join(", ")}.`,
      })
    );
  } else {
    blocks.push(el("h5", { text: "Resolution" }));
    blocks.push(
      grid([
        definitionRow("Outcome", outcomeLabel(resolution.outcome)),
        definitionRow("Verification summary", resolution.verification_summary),
        definitionRow("Verification rationale", resolution.no_change_reason),
        definitionRow("Branch", resolution.branch),
        definitionRow("Commit", digest(resolution.commit_sha), {
          full: resolution.commit_sha ?? "",
        }),
        definitionRow("Remediation batch", resolution.batch_id),
        definitionRow("Verified by", resolution.verified_by?.email ?? ""),
      ])
    );
    blocks.push(linkRow("Change request", resolution.pr_url));
    blocks.push(timeRow("Verified at", resolution.verified_at));
    blocks.push(timeRow("Closed at", review.resolved_at));

    const runs = Array.isArray(resolution.test_evidence) ? resolution.test_evidence : [];
    blocks.push(el("h5", { text: "Machine-checked verification" }));
    if (runs.length === 0) {
      blocks.push(
        el("p", {
          className: "panel-note",
          attrs: { "data-tone": "warning" },
          text:
            "No machine-checked test evidence is attached, so this review is " +
            "closed on its written rationale alone.",
        })
      );
    } else {
      for (const [index, entry] of runs.entries()) {
        blocks.push(verificationRun(entry, index));
      }
    }
  }

  blocks.push(el("h5", { text: "Remediation batches" }));
  blocks.push(
    el("p", {
      className: "panel-note",
      text:
        flags.remediation_enabled === true
          ? "Batches are enabled for this deployment. Select reviews in the queue " +
            "to freeze a group of observations for the agent."
          : "The server reports batches as disabled in this deployment, so a " +
            "review cannot be added to one from here.",
    })
  );
  replaceChildren(container, blocks);
}

// ---------------------------------------------------------------------------
// The remediation batch card
//
// Appended rather than folded into `renderRemediation` above: that function's
// prose is pinned by the Stage 7 contract tests, and its subject is one review's
// resolution. This subject is the *batch* — a different object with a different
// lifecycle and a different set of people allowed to move it.
//
// Two rules run through everything below.
//
// **No agent control is ever rendered.** Claim, heartbeat, materialize, patch,
// and release belong to one verified service account. `allowedBatchActions`
// returns only human edges, so there is no branch here that could produce one.
//
// **The lease is shown as a window, never as a credential.** The server's
// human-facing view carries no token and no token hash, so there is nothing here
// to accidentally print.
// ---------------------------------------------------------------------------

function batchRow(term, value) {
  return definitionRow(term, value === null || value === undefined ? "" : String(value));
}

/** One recorded verification run, reusing the evidence renderer above. */
function evidenceBlock(heading, runs, emptyNote) {
  const blocks = [el("h5", { text: heading })];
  const entries = Array.isArray(runs) ? runs : [];
  if (entries.length === 0) {
    blocks.push(el("p", { className: "panel-note", text: emptyNote }));
    return blocks;
  }
  for (const [index, entry] of entries.entries()) {
    blocks.push(verificationRun(entry, index));
  }
  return blocks;
}

/**
 * The batch card: status, who holds it, what was produced, and what was proved.
 *
 * `actions` is the list `allowedBatchActions` produced for this role and state.
 * The caller owns the click handlers; this function only renders the controls it
 * was told are legal, which keeps the role rules in one place instead of two.
 */
export function renderBatchCard(container, batch, { actions = [], labels = {} } = {}) {
  if (batch === null || batch === undefined) {
    replaceChildren(container, [
      el("p", {
        className: "panel-note",
        text:
          "No remediation batch is selected. Choose reviews in the queue and " +
          "create one to hand a group of observations to the agent.",
      }),
    ]);
    return;
  }

  const blocks = [];
  blocks.push(
    el("span", {
      className: "pill",
      text: labels[batch.status] ?? String(batch.status ?? ""),
      attrs: { "data-status": String(batch.status ?? "") },
    })
  );
  blocks.push(
    grid([
      batchRow("Batch", batch.batch_id),
      batchRow("Frozen observations", batch.item_count),
      batchRow("Version", batch.version),
      batchRow("Created by", batch.created_by?.email ?? ""),
      batchRow("Claimed by", batch.claimed_by?.email ?? ""),
      batchRow("Prompt template", batch.prompt_template_version),
    ])
  );

  const lease = batch.lease ?? null;
  blocks.push(el("h5", { text: "Claim" }));
  if (lease === null) {
    blocks.push(
      el("p", {
        className: "panel-note",
        text: "No agent holds this batch. Nothing is being renewed.",
      })
    );
  } else {
    blocks.push(grid([batchRow("Holder", lease.holder)]));
    blocks.push(timeRow("Lease expires", lease.expires_at));
    blocks.push(timeRow("Last heartbeat", lease.last_heartbeat_at));
    blocks.push(timeRow("Continuous since", lease.continuous_since));
  }

  blocks.push(el("h5", { text: "Proposed change" }));
  blocks.push(
    grid([
      batchRow("Plan", batch.plan_artifact),
      batchRow("Branch", batch.branch),
      definitionRow("Commit", digest(batch.commit_sha), {
        full: batch.commit_sha ?? "",
      }),
      batchRow("Uncommitted because", batch.uncommitted_reason),
      batchRow("Summary", batch.verification_summary),
    ])
  );
  blocks.push(linkRow("Change request", batch.pr_url));

  const changed = Array.isArray(batch.changed_files) ? batch.changed_files : [];
  if (changed.length > 0) {
    const list = el("ul", { className: "changed-files" });
    replaceChildren(
      list,
      changed.map((path) => el("li", { text: path }))
    );
    blocks.push(el("h5", { text: `Changed files (${changed.length})` }));
    blocks.push(list);
  }

  blocks.push(
    ...evidenceBlock(
      "What the agent ran",
      batch.test_evidence,
      "The agent recorded no test evidence, so there is nothing here to check."
    )
  );
  blocks.push(
    ...evidenceBlock(
      "What the verifier ran",
      batch.verification_evidence,
      "No independent verification evidence is recorded yet."
    )
  );
  if (batch.verification_attestation) {
    blocks.push(
      grid([
        batchRow("Verifier attestation", batch.verification_attestation),
        batchRow("Verified by", batch.verified_by?.email ?? ""),
      ])
    );
  }
  if (batch.outcome) {
    blocks.push(el("h5", { text: "Outcome" }));
    blocks.push(
      grid([
        batchRow("Decision", batch.outcome.decision),
        batchRow("Reason", batch.outcome.reason),
      ])
    );
  }

  const controls = el("div", { className: "batch-actions" });
  const buttons = [
    button({
      text: "Copy Codex prompt",
      className: "button",
      dataset: { action: "copy-batch-prompt", batchId: String(batch.batch_id ?? "") },
    }),
  ];
  for (const name of actions) {
    buttons.push(
      button({
        text: labels[`action:${name}`] ?? name,
        className: "button button-quiet",
        dataset: {
          action: `batch-${name}`,
          batchId: String(batch.batch_id ?? ""),
          expectedVersion: String(batch.version ?? ""),
        },
      })
    );
  }
  replaceChildren(controls, buttons);
  blocks.push(el("h5", { text: "Actions" }));
  blocks.push(controls);
  blocks.push(
    el("p", {
      className: "field-help",
      attrs: { id: "batch-actions-help" },
      text:
        "The agent's own controls are not offered here. Claiming, reading the " +
        "frozen records, and proposing changes belong to the verified agent " +
        "identity, and completing a batch belongs to somebody who did not write " +
        "the change.",
    })
  );
  replaceChildren(container, blocks);
}

/**
 * Put the prompt on the clipboard, with a fallback a keyboard user can finish.
 *
 * The Clipboard API needs a secure context and a user gesture, and it can still
 * be refused by permission policy. When it is, the text is put into a focused,
 * selected, read-only field instead of being dropped: the reviewer can then copy
 * it the ordinary way. Nothing is written to browser storage on either path — the
 * prompt is a handoff, not a document this console keeps.
 */
export async function copyPromptToClipboard(text, { fallbackField = null } = {}) {
  const clipboard = globalThis.navigator?.clipboard ?? null;
  if (clipboard && typeof clipboard.writeText === "function") {
    try {
      await clipboard.writeText(text);
      return { copied: true, method: "clipboard" };
    } catch {
      // Fall through to the visible field.
    }
  }
  if (fallbackField === null) {
    return { copied: false, method: "none" };
  }
  fallbackField.value = text;
  fallbackField.hidden = false;
  fallbackField.readOnly = true;
  fallbackField.focus();
  if (typeof fallbackField.select === "function") {
    fallbackField.select();
  }
  return { copied: false, method: "manual" };
}
