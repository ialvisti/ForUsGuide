/**
 * The RAG evidence panel.
 *
 * This panel exists to answer one question — *can what the assistant did be
 * reconstructed?* — and its whole design is about not overstating the answer.
 *
 *   * A field the server did not record is rendered as **not recorded**, never as
 *     a blank beside a populated one. On this panel "no vectors were retrieved"
 *     and "we never recorded which vectors were retrieved" are opposite facts,
 *     and a blank row reads as the first.
 *   * An observed vector reference is labelled as *observed at query time*. Chunk
 *     ids are not stable across a reindex, so presenting one as an identity a
 *     reviewer can look up later would be wrong within a week.
 *   * A rendered-prompt hash is labelled a **trace**: it correlates one
 *     execution. The template id and the template's own hash are the version.
 *   * Chunk *content* never appears. The provenance collection is not a place to
 *     read the knowledge base from, and a browser-side vector query would put a
 *     search index credential in a page. If a reviewer needs the source, the
 *     article identifier is what they get.
 *   * A suggestion is not a link. It is rendered as a suggestion, it requires a
 *     typed reason, and confirming it is an explicit, audited, versioned write.
 */

import {
  button,
  correlationStatusLabel,
  correlationTrustLabel,
  definitionRow,
  digest,
  el,
  evidenceGapText,
  hiddenText,
  missingProvenanceText,
  replaceChildren,
  timeElement,
} from "./render.js";

const SOURCE_COLLECTION_LABELS = new Map([
  ["execution_logs", "Execution log"],
  ["ticket_executions", "Ticket execution"],
  ["ticket_jobs", "Ticket job"],
]);

function grid(rows) {
  const node = el("dl", { className: "field-grid" });
  replaceChildren(node, rows);
  return node;
}

/** A label derived by this UI, never a verbatim upstream value. */
function interfaceRow(term, value, options = {}) {
  return definitionRow(term, value, { ...options, recorded: false });
}

/** Preserve source order and multiplicity; repeated records are audit evidence. */
function recordedRecords(records) {
  return Array.isArray(records) ? records : [];
}

function countLabel(count, singular, plural) {
  return `${count.toLocaleString(document.documentElement.lang || "en")} ` +
    `${count === 1 ? singular : plural}`;
}

/** A counted disclosure whose body still contains every recorded record. */
function evidenceDisclosure({
  title,
  records,
  singular,
  plural,
  renderRecord,
  list = true,
  open = false,
}) {
  const details = el("details", {
    className: "evidence-collection",
    attrs: {
      "data-evidence-count": String(records.length),
      open,
    },
  });
  const summary = el("summary", { className: "evidence-collection-summary" });
  summary.appendChild(el("span", { text: title }));
  summary.appendChild(
    el("span", {
      className: "evidence-count",
      text: countLabel(records.length, singular, plural),
    })
  );
  details.appendChild(summary);
  const body = el(list ? "ul" : "div", {
    className: list ? "evidence-links evidence-collection-list" : "evidence-record-list",
  });
  replaceChildren(body, records.map(renderRecord));
  details.appendChild(body);
  return details;
}

function timeRow(term, iso) {
  const wrap = el("div");
  wrap.appendChild(el("dt", { text: term }));
  const value = el("dd");
  if (iso) {
    value.appendChild(timeElement(iso));
    value.appendChild(
      el("span", {
        className: "evidence-exact-time",
        children: [
          el("span", { text: "Exact timestamp" }),
          el("code", {
            text: iso,
            attrs: { "data-audit-value": "", translate: "no" },
          }),
        ],
      })
    );
  } else {
    value.textContent = "Not recorded";
    value.setAttribute("data-absent", "true");
  }
  wrap.appendChild(value);
  return wrap;
}

/**
 * One observed vector.
 *
 * Six fields exist on the record: the vector id, the article, the content hash,
 * the ordinal, the namespace and the score. A vector "type" and a retrieval
 * "tier" are *not* modelled anywhere in the contract, so they are not shown —
 * inventing two empty rows would suggest the pipeline records something it does
 * not.
 */
function observedChunk(chunk, index) {
  const item = el("li", { className: "evidence-record", attrs: { "data-kind": "chunk" } });
  item.appendChild(
    el("p", {
      className: "audit-type",
      text: `Observed vector ${index + 1}`,
    })
  );
  item.appendChild(
    grid([
      definitionRow("Observed vector id", digest(chunk.observed_vector_id), {
        full: chunk.observed_vector_id ?? "",
      }),
      definitionRow("Article id", chunk.article_id),
      definitionRow("Content hash", digest(chunk.content_sha256), {
        full: chunk.content_sha256 ?? "",
      }),
      definitionRow(
        "Chunk ordinal",
        typeof chunk.chunk_ordinal === "number" ? String(chunk.chunk_ordinal) : ""
      ),
      definitionRow("Namespace", chunk.namespace),
      definitionRow(
        "Score",
        typeof chunk.score === "number" ? String(chunk.score) : ""
      ),
    ])
  );
  return item;
}

function observedChunks(chunks) {
  const wrap = el("div");
  const records = recordedRecords(chunks);
  if (records.length === 0) {
    wrap.appendChild(el("h5", { text: "Vectors observed at query time" }));
    wrap.appendChild(
      el("p", {
        className: "panel-note",
        text:
          "No observed vectors were recorded for this execution. That is a gap " +
          "in what was logged, not proof that nothing was retrieved.",
      })
    );
    return wrap;
  }
  wrap.appendChild(
    el("p", {
      className: "panel-note",
      text:
        "These identifiers are what the retrieval step returned at the time. " +
        "They are not stable across a reindex, so treat them as a trace of that " +
      "one query rather than as addresses to look up later.",
    })
  );
  wrap.appendChild(
    evidenceDisclosure({
      title: "Vectors observed at query time",
      records,
      singular: "observed vector",
      plural: "observed vectors",
      renderRecord: (chunk, index) => observedChunk(chunk, index),
    })
  );
  return wrap;
}

function provenanceRecord(provenance, index) {
  const record = provenance ?? {};
  const wrap = el("div", {
    className: "evidence-record",
    attrs: { "data-kind": "provenance", "data-index": String(index) },
  });
  wrap.appendChild(el("p", { className: "audit-type", text: `Retrieval record ${index + 1}` }));
  wrap.appendChild(
    grid([
      interfaceRow("Retrieval correlation", correlationStatusLabel(record.correlation_status)),
      interfaceRow("Retrieval trust", correlationTrustLabel(record.correlation_trust)),
      definitionRow("Retrieval correlation source", record.correlation_source),
      interfaceRow(
        "Missing retrieval provenance",
        record.missing_provenance === true
          ? "Yes"
          : record.missing_provenance === false
            ? "No"
            : ""
      ),
      definitionRow("Index", record.index_name),
      definitionRow("Index version", record.index_version),
      definitionRow("Namespace", record.namespace),
      definitionRow("Deployed revision", record.deployed_revision),
      definitionRow("Prompt template id", record.prompt_template_id),
      definitionRow("Prompt template hash", digest(record.prompt_template_sha256), {
        full: record.prompt_template_sha256 ?? "",
      }),
      definitionRow("Response hash", digest(record.response_sha256), {
        full: record.response_sha256 ?? "",
      }),
    ])
  );
  wrap.appendChild(observedChunks(record.observed_chunks));
  return wrap;
}

function executionSource(source, index) {
  const value = typeof source === "string" ? { articleId: source } : source ?? {};
  const item = el("li", { className: "evidence-record", attrs: { "data-kind": "source" } });
  item.appendChild(el("p", { className: "audit-type", text: `Source article ${index + 1}` }));
  const rows = [definitionRow("Article id", value.articleId ?? value.id)];
  if (value.articleTitle) {
    rows.push(definitionRow("Article title", value.articleTitle));
  }
  if (value.title && value.title !== value.articleTitle) {
    rows.push(definitionRow("Source title", value.title));
  }
  rows.push(
    definitionRow("Recorded URL", value.url),
    definitionRow("Chunk types used", value.chunkTypesUsed),
    definitionRow("Relevance", value.relevance),
    definitionRow(
      "Used in answer",
      typeof value.usedInfo === "boolean" ? (value.usedInfo ? "Yes" : "No") : "",
      { recorded: false }
    ),
    definitionRow("Maximum score", value.maxScore)
  );
  item.appendChild(
    grid(rows)
  );
  return item;
}

function executionChunk(chunk, index) {
  const value = chunk ?? {};
  const contentHash = value.contentHash;
  const preview = value.preview;
  const item = el("li", { className: "evidence-record", attrs: { "data-kind": "chunk" } });
  item.appendChild(el("p", { className: "audit-type", text: `Retrieved chunk ${index + 1}` }));
  item.appendChild(
    grid([
      definitionRow("Source id", value.sourceId),
      definitionRow("Article id", value.articleId),
      definitionRow("Article title", value.articleTitle),
      definitionRow("Chunk id", value.chunkId),
      definitionRow("Chunk type", value.chunkType),
      definitionRow("Chunk tier", value.chunkTier),
      definitionRow("Score", value.score),
      definitionRow("Content hash", digest(contentHash), { full: contentHash ?? "" }),
      definitionRow("Bounded preview", preview),
    ])
  );
  return item;
}

function executionEvidenceRecords(execution) {
  return {
    sourceArticles: recordedRecords(execution?.sourceArticles),
    chunkEvidence: recordedRecords(execution?.chunkEvidence),
  };
}

/** Counts preserve the same source order and multiplicity as the rendered lists. */
export function executionEvidenceCounts(execution) {
  const records = executionEvidenceRecords(execution);
  return {
    sourceCount: records.sourceArticles.length,
    chunkCount: records.chunkEvidence.length,
    available: records.sourceArticles.length > 0 || records.chunkEvidence.length > 0,
  };
}

/** Render the bounded sourceArticles and chunkEvidence captured with a run. */
export function renderExecutionEvidence(sourceList, chunkList, execution) {
  const { sourceArticles, chunkEvidence } = executionEvidenceRecords(execution);
  replaceChildren(
    sourceList,
    sourceArticles.length === 0
      ? [el("p", { className: "panel-note", text: "No source articles were recorded." })]
      : [
          evidenceDisclosure({
            title: "Source articles",
            records: sourceArticles,
            singular: "source article",
            plural: "source articles",
            renderRecord: executionSource,
          }),
        ]
  );
  replaceChildren(
    chunkList,
    chunkEvidence.length === 0
      ? [el("p", { className: "panel-note", text: "No bounded chunk evidence was recorded." })]
      : [
          evidenceDisclosure({
            title: "Bounded chunks",
            records: chunkEvidence,
            singular: "bounded chunk",
            plural: "bounded chunks",
            renderRecord: executionChunk,
          }),
        ]
  );
}

/**
 * One sanitized execution record, showing only the fields it actually carries.
 *
 * The record is the broker's allowlisted shape: prompts, responses, chunk text,
 * participant data and raw upstream identifiers are absent from it by
 * construction, which is why the whole record can be rendered without filtering
 * here. Every field in that allowlisted shape remains available to an auditor.
 */
export function executionRecord(record, index) {
  const provenance = record.provenance ?? {};
  const wrap = el("div", {
    className: "evidence-record",
    attrs: { "data-kind": "execution", "data-failed": record.failed === true ? "true" : "false" },
  });

  wrap.appendChild(
    el("p", { className: "audit-type", text: `Execution ${index + 1}` })
  );
  if (record.failed === true) {
    wrap.appendChild(
      el("span", {
        className: "pill",
        text: "This execution failed",
        attrs: { "data-severity": "critical" },
      })
    );
  }

  wrap.appendChild(el("h5", { text: "Execution" }));
  wrap.appendChild(
    grid([
      interfaceRow("Source record", SOURCE_COLLECTION_LABELS.get(record.source_collection) ?? ""),
      interfaceRow(
        "Record schema",
        typeof record.schema_version === "number"
          ? record.schema_version === 0
            ? "0 (pre-provenance legacy shape)"
            : String(record.schema_version)
          : ""
      ),
      timeRow("Occurred at", record.occurred_at),
      interfaceRow(
        "Latency",
        typeof record.duration_ms === "number" ? `${String(record.duration_ms)} ms` : ""
      ),
      interfaceRow(
        "Outcome",
        record.failed === null || record.failed === undefined
          ? ""
          : record.failed
            ? "Failed"
            : "Completed"
      ),
      definitionRow("Endpoint", record.endpoint),
      definitionRow("Route", record.route),
      definitionRow("Correlation source", record.correlation_source),
      interfaceRow("Correlation trust", correlationTrustLabel(record.correlation_trust)),
    ])
  );

  wrap.appendChild(el("h5", { text: "Identifiers" }));
  wrap.appendChild(
    grid([
      definitionRow("Internal ticket job", record.internal_job_id),
      definitionRow("Lookup key version", record.lookup_key_version),
      definitionRow("Ingress key version", record.ingress_key_version),
      definitionRow("Request id hash", digest(record.request_id_hash), {
        full: record.request_id_hash ?? "",
      }),
      definitionRow("Actor principal hash", digest(record.principal_hash), {
        full: record.principal_hash ?? "",
      }),
      definitionRow("Evidence reference", digest(record.evidence_reference), {
        full: record.evidence_reference ?? "",
      }),
      definitionRow("Evidence digest", digest(record.evidence_digest), {
        full: record.evidence_digest ?? "",
      }),
    ])
  );

  wrap.appendChild(el("h5", { text: "Model and prompt" }));
  wrap.appendChild(
    grid([
      definitionRow("Model", record.model),
      definitionRow("Provider", record.provider),
      definitionRow("Prompt template id", provenance.prompt_template_id),
      definitionRow("Prompt template hash", digest(provenance.prompt_template_sha256), {
        full: provenance.prompt_template_sha256 ?? "",
      }),
      definitionRow("Prompt configuration version", record.config_version),
      definitionRow(
        "Rendered prompt trace hash",
        digest(record.rendered_prompt_trace_sha256),
        { full: record.rendered_prompt_trace_sha256 ?? "" }
      ),
      definitionRow("Response hash", digest(provenance.response_sha256), {
        full: provenance.response_sha256 ?? "",
      }),
    ])
  );
  wrap.appendChild(
    el("p", {
      className: "panel-note",
      text:
        "The rendered prompt hash identifies this one execution's prompt text. " +
        "The template id and template hash are what identify the version of the " +
        "prompt; the trace hash changes whenever the inputs do.",
    })
  );

  wrap.appendChild(el("h5", { text: "Index and deployment" }));
  wrap.appendChild(
    grid([
      interfaceRow(
        "Retrieval correlation",
        correlationStatusLabel(provenance.correlation_status)
      ),
      interfaceRow(
        "Retrieval trust",
        correlationTrustLabel(provenance.correlation_trust)
      ),
      definitionRow("Retrieval correlation source", provenance.correlation_source),
      interfaceRow(
        "Missing retrieval provenance",
        provenance.missing_provenance === true
          ? "Yes"
          : provenance.missing_provenance === false
            ? "No"
            : ""
      ),
      definitionRow("Index", provenance.index_name),
      definitionRow("Index version", provenance.index_version, {
        absentNote: "Unknown — this execution did not record an index version",
      }),
      definitionRow("Namespace", provenance.namespace),
      definitionRow("Deployed revision", provenance.deployed_revision),
      definitionRow("Deployed commit", digest(record.deployed_commit_sha), {
        full: record.deployed_commit_sha ?? "",
      }),
      definitionRow("Deployed image digest", digest(record.deployed_image_digest), {
        full: record.deployed_image_digest ?? "",
      }),
    ])
  );

  const articles = recordedRecords(record.source_article_ids);
  wrap.appendChild(el("h5", { text: "Source articles" }));
  if (articles.length === 0) {
    wrap.appendChild(
      el("p", {
        className: "panel-note",
        text: "No source article identifiers were recorded for this execution.",
      })
    );
  } else {
    const list = el("ul", { className: "checks" });
    replaceChildren(
      list,
      articles.map((id) => el("li", {
        className: "mono",
        text: id,
        attrs: { "data-audit-value": "", translate: "no" },
      }))
    );
    wrap.appendChild(list);
    wrap.appendChild(
      el("p", {
        className: "panel-note",
        text:
          "Article identifiers only. Article text is not read through this " +
          "console, so what is shown here cannot drift from what was indexed.",
      })
    );
  }

  wrap.appendChild(observedChunks(provenance.observed_chunks));

  const missing = missingProvenanceText(record.missing);
  if (missing !== "") {
    wrap.appendChild(el("p", { className: "panel-note", text: missing, attrs: { "data-tone": "warning" } }));
  }
  return wrap;
}

/**
 * One suggested correlation, with the confirmation it requires.
 *
 * The token is carried on the control and echoed back untouched. Nothing here
 * chooses an execution: the server minted this token after its own bounded
 * lookup and sealed the ticket, the review, the reviewer and an expiry inside it.
 */
export function candidateLink(candidate, index, { canConfirm, expired }) {
  const wrap = el("div", {
    className: "evidence-record",
    attrs: { "data-kind": "candidate", "data-index": String(index) },
  });
  wrap.appendChild(
    el("span", {
      className: "pill",
      text: "Suggested, not linked",
      attrs: { "data-partial": "true" },
    })
  );
  wrap.appendChild(el("p", { className: "audit-type", text: `Suggestion ${index + 1}` }));
  wrap.appendChild(
    grid([
      definitionRow("Evidence reference", digest(candidate.evidence_reference), {
        full: candidate.evidence_reference ?? "",
      }),
      definitionRow("Evidence digest", digest(candidate.evidence_digest), {
        full: candidate.evidence_digest ?? "",
      }),
      definitionRow("Lookup result digest", digest(candidate.broker_result_digest), {
        full: candidate.broker_result_digest ?? "",
      }),
      definitionRow("Why it was suggested", candidate.rationale),
      interfaceRow("Trust", correlationTrustLabel(candidate.correlation_trust)),
    ])
  );
  wrap.appendChild(timeRow("Suggestion expires", candidate.expires_at));

  if (expired) {
    wrap.appendChild(
      el("p", {
        className: "panel-note",
        attrs: { "data-tone": "warning" },
        text: "This suggestion has expired. Reload the ticket to get a current one.",
      })
    );
    return wrap;
  }

  const form = el("div", { className: "confirm-form" });
  const field = el("div", { className: "field" });
  const inputId = `candidate-reason-${index}`;
  field.appendChild(
    el("label", { text: "Why this evidence belongs to this ticket", attrs: { for: inputId } })
  );
  const input = el("input", {
    className: "reason-input",
    attrs: {
      type: "text",
      id: inputId,
      maxlength: "1000",
      autocomplete: "off",
      "data-candidate-reason": String(index),
      "aria-describedby": `${inputId}-help`,
    },
  });
  field.appendChild(input);
  field.appendChild(
    el("p", {
      className: "field-help",
      text: "Required. It is written to the audit ledger and cannot be edited later.",
      attrs: { id: `${inputId}-help` },
    })
  );
  form.appendChild(field);
  form.appendChild(
    button({
      text: "Confirm this link",
      dataset: { action: "confirm-candidate", index: String(index) },
      disabled: !canConfirm,
      describedBy: canConfirm ? "" : "eval-role-help",
    })
  );
  wrap.appendChild(form);
  return wrap;
}

/** One confirmed link, with the versioned, reasoned unlink it requires. */
export function confirmedLink(link, { canUnlink }) {
  const item = el("li", {
    className: "evidence-link",
    attrs: { "data-link-id": link.link_id ?? "" },
  });
  item.appendChild(
    el("span", {
      className: "pill",
      text: correlationTrustLabel(link.correlation_trust),
      attrs: { "data-actor-class": "participant" },
    })
  );
  item.appendChild(
    grid([
      definitionRow("Evidence reference", digest(link.evidence_reference), {
        full: link.evidence_reference ?? "",
      }),
      definitionRow("Evidence digest", digest(link.evidence_digest), {
        full: link.evidence_digest ?? "",
      }),
      definitionRow("Reason given", link.reason),
      definitionRow("Reviewer subject", link.linked_by?.subject),
      definitionRow("Reviewer display name", link.linked_by?.display_name),
      definitionRow("Linked by", link.linked_by?.email ?? ""),
      definitionRow(
        "Link version",
        typeof link.version === "number" ? String(link.version) : ""
      ),
    ])
  );
  item.appendChild(timeRow("Linked at", link.linked_at));
  item.appendChild(el("h5", { text: "Exact link identifiers" }));
  item.appendChild(
    grid([
      definitionRow("Review id", digest(link.review_id), { full: link.review_id ?? "" }),
      definitionRow("Link id", link.link_id),
      definitionRow("Source URL", link.source_url),
    ])
  );

  const form = el("div", { className: "confirm-form" });
  const field = el("div", { className: "field" });
  const inputId = `unlink-reason-${link.link_id ?? "unknown"}`;
  field.appendChild(el("label", { text: "Why this link is wrong", attrs: { for: inputId } }));
  field.appendChild(
    el("input", {
      className: "reason-input",
      attrs: {
        type: "text",
        id: inputId,
        maxlength: "1000",
        autocomplete: "off",
        "data-unlink-reason": link.link_id ?? "",
      },
    })
  );
  field.appendChild(
    el("p", {
      className: "field-help",
      text:
        "Required. An unexplained unlink is indistinguishable from tampering " +
        "when the record is read back years later.",
    })
  );
  form.appendChild(field);
  form.appendChild(
    button({
      text: "Unlink",
      className: "button button-quiet",
      dataset: { action: "unlink-evidence", linkId: link.link_id ?? "" },
      disabled: !canUnlink,
    })
  );
  item.appendChild(form);
  return item;
}

/**
 * The whole evidence body: correlation, executions, and suggestions.
 *
 * `now` is passed in so expiry is decided against one clock reading rather than
 * drifting between the records rendered in a single pass.
 */
export function renderEvidence(container, evidence, { canConfirm, now }) {
  if (evidence === null || evidence === undefined) {
    replaceChildren(container, [
      el("p", { className: "panel-note", text: "Evidence has not been loaded yet." }),
    ]);
    return;
  }

  const blocks = [];
  blocks.push(
    grid([
      interfaceRow("Correlation", correlationStatusLabel(evidence.correlation_status)),
      interfaceRow("Trust", correlationTrustLabel(evidence.correlation_trust)),
      definitionRow("Correlation source", evidence.correlation_source),
      definitionRow(
        "Confirmed links",
        typeof evidence.linked_count === "number" ? String(evidence.linked_count) : ""
      ),
      definitionRow("Unavailable reason", evidence.unavailable_reason),
      definitionRow("Lookup result digest", digest(evidence.result_digest), {
        full: evidence.result_digest ?? "",
      }),
      definitionRow(
        "Key versions queried",
        recordedRecords(evidence.key_versions_queried).map(String).join(", ")
      ),
      interfaceRow("Lookup truncated", evidence.truncated === true ? "Yes" : "No"),
      interfaceRow(
        "Evidence service",
        evidence.broker_available === true ? "Answered" : "Did not answer"
      ),
    ])
  );

  const warnings = recordedRecords(evidence.warnings);
  if (warnings.length > 0) {
    blocks.push(
      evidenceDisclosure({
        title: "Recorded evidence warnings",
        records: warnings,
        singular: "warning",
        plural: "warnings",
        renderRecord: (warning, index) =>
          el("li", {
            className: "evidence-record evidence-warning",
            attrs: { "data-kind": "warning", "data-index": String(index) },
            children: [
              el("p", {
                className: "panel-note",
                text: warning,
                attrs: { "data-audit-value": "", translate: "no" },
              }),
            ],
          }),
      })
    );
  }

  const executions = recordedRecords(evidence.executions);
  const provenance = recordedRecords(evidence.provenance);
  if (provenance.length > 0) {
    blocks.push(
      evidenceDisclosure({
        title: "Recorded retrieval provenance",
        records: provenance,
        singular: "retrieval record",
        plural: "retrieval records",
        renderRecord: (record, index) => provenanceRecord(record, index),
        list: false,
      })
    );
  }
  if (executions.length > 0) {
    blocks.push(
      evidenceDisclosure({
        title: "Executions found for this ticket",
        records: executions,
        singular: "execution",
        plural: "executions",
        renderRecord: (record, index) => executionRecord(record, index),
        list: false,
      })
    );
  }

  const candidates = recordedRecords(evidence.candidate_links);
  if (candidates.length > 0) {
    blocks.push(
      el("p", {
        className: "panel-note",
        text:
          "These were not produced by the workload that answered the ticket, so " +
          "the console will not treat them as evidence until a reviewer says " +
          "they belong and why.",
      })
    );
    blocks.push(
      evidenceDisclosure({
        title: "Suggested correlations awaiting confirmation",
        records: candidates,
        singular: "suggestion",
        plural: "suggestions",
        renderRecord: (candidate, index) => {
          const expiresAt = Date.parse(candidate.expires_at);
          return candidateLink(candidate, index, {
            canConfirm,
            expired: Number.isFinite(expiresAt) && expiresAt <= now,
          });
        },
        list: false,
      })
    );
  }

  if (executions.length === 0 && candidates.length === 0) {
    blocks.push(
      el("p", {
        className: "panel-note",
        attrs: { "data-tone": "warning" },
        text: evidenceGapText(evidence.unavailable_reason),
      })
    );
    blocks.push(hiddenText("No retrieval or prompt provenance is available for this ticket."));
  }
  replaceChildren(container, blocks);
}

/** The confirmed-link list, or an honest empty state. */
export function renderEvidenceLinks(list, links, { canUnlink }) {
  const records = recordedRecords(links);
  if (records.length === 0) {
    const item = el("li", { className: "evidence-link" });
    item.appendChild(
      el("p", {
        className: "panel-note",
        text: "No reviewer has confirmed an evidence link for this ticket.",
      })
    );
    replaceChildren(list, [item]);
    return;
  }
  replaceChildren(
    list,
    records.map((link) => confirmedLink(link, { canUnlink }))
  );
}

/** The evidence panel's own state, in one line. */
export function evidenceStatusText(detail) {
  if (detail.phase === "loading") {
    return { text: "Loading evidence…", tone: "info" };
  }
  if (detail.phase === "error") {
    return { text: "Evidence could not be loaded.", tone: "error" };
  }
  const evidence = detail.evidence;
  if (evidence === null || evidence === undefined) {
    return { text: "", tone: "info" };
  }
  const status = evidence.correlation_status ?? "unavailable";
  if (status === "unavailable") {
    return { text: "No defensible correlation for this ticket.", tone: "warning" };
  }
  const executions = recordedRecords(evidence.executions).length;
  const incomplete = evidence.truncated === true || evidence.warnings?.length > 0;
  const suffix = incomplete ? " Some evidence may be incomplete." : "";
  return {
    text:
      `${correlationStatusLabel(status)}: ${executions} execution` +
      `${executions === 1 ? "" : "s"} available.${suffix}`,
    tone: incomplete ? "warning" : "info",
  };
}
