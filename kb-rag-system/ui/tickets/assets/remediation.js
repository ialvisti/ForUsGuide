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
 * Remediation batches themselves are a later stage. The server reports them as
 * disabled and publishes no route for them, so the control that would create one
 * is disabled and says why, rather than being hidden (which reads as "this tool
 * cannot do that") or hopeful (which reads as a bug when it 404s).
 */

import {
  REMEDIATION_LABELS,
  STATUS_LABELS,
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
          "This ticket has no durable review yet, so it carries no remediation " +
          "target and no resolution. Add it to the review queue to start one.",
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
          ? "Batches are enabled for this deployment."
          : "The server reports batches as disabled in this build and publishes " +
            "no route for them, so a review cannot be added to one from here yet.",
    })
  );
  replaceChildren(container, blocks);
}
