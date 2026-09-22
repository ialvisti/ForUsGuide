'use strict';

/**
 * LOCAL contract preparation, not wired into a workflow or deployed.
 *
 * validateCanonicalEvidence({reference, poll, now}) is a pure verifier for a
 * single GET /api/v1/tickets/{job_id} response. The adapter MUST obtain `poll`
 * using the existing authenticated producer credential, at a fixed origin.
 * Passing arbitrary JSON here DOES NOT authenticate its origin. No boolean,
 * note, header copied by a model, or model-generated reference authenticates it.
 *
 * `reference` is independent workflow state, with exactly these fields:
 *   ticket_id: exact TKT display ID or DON (never convert one into the other)
 *   ticket_job_id: the accepted producer job, 32 lowercase hexadecimal digits
 *   conversation_reference: the current independent conversation reference
 * `now` is an explicit RFC3339 timestamp supplied by the caller's trusted clock.
 * Neither URLs nor response prose are accepted as reference sources.
 *
 * Conversation reference supplied by the deployed backend from durable input:
 *   {type: 'devrev_conversation_snapshot', schema_version: 1,
 *    hash_algorithm: 'sha256', digest: <64 lowercase hexadecimal digits>,
 *    complete: true, partial: false, truncated: false}
 * Both reference.conversation_reference and poll.conversation_reference must
 * match. The latter must come from the durable accepted input, outside model
 * metadata. conversation-snapshot.js and the backend share canonical encoding
 * of the ordered input messages, IDs/authorship/visibility/dates, title/body
 * and bounded-hydration diagnostics.
 * This module does not produce a digest, infer completeness, or select a job.
 * Completeness diagnostics must come from both trusted hydration paths, never
 * from model assertions. Missing/incomplete binding hides all facts. Digest
 * equality does not establish that the underlying account facts are current.
 *
 * Only inquiry.generate_response.metadata.verified_participant_facts and
 * inquiry.knowledge_answer.metadata.verified_participant_facts are fact inputs.
 * Source/date/value fields are projected through the existing seven-field
 * disclosure contract. Identity rejection in an enclosing/inquiry context wins.
 * Conflicting/invalid occurrences quarantine that field across all inquiries;
 * independent valid fields remain available. Dates are preserved, with no new
 * policy declaring an old source stale. An observation after job completion
 * cannot belong to that job. A date-only as_of has no time zone and is not
 * compared as an instant. Job expiry is result retention only. Failed answers
 * cannot supply facts; unrelated review requirements do not invalidate facts.
 *
 * inquiry.generate_response.metadata.verified_plan_facts and the knowledge_answer
 * equivalent are the only plan-identifier inputs, projected through a separate
 * three-field allowlist (legal plan name, recordkeeper plan code, recordkeeper).
 * They require poll.plan_id — the plan of the durable accepted request, never a
 * generated or metadata-copied value — to equal the container's bound plan_id.
 * A container naming another plan yields no plan facts and does not disturb the
 * participant facts. plan_id is NOT part of the closed independent reference,
 * is not an account number, and is never treated as a recordkeeper plan code.
 * Plan and participant facts are quarantined independently; an identity veto
 * still removes both. Evidence may be `matched` on plan facts alone.
 *
 * Output `matched` means these supplied contracts match; it does not certify
 * authentication, correctness of arbitrary prose, eligibility, or permission.
 * Publication, participant_reply_safe and set_stage_solved remain false even
 * when facts match. Human review remains required. Errors contain fixed codes
 * only. The adapter must bound its HTTP response before decoding JSON; no HTTP,
 * credentials, I/O, or mutable process state belongs in this function.
 */

const FACT_SOURCES = Object.freeze({
  first_name: 'participant.census.First Name',
  account_balance: 'participant.savings_rate.Account Balance',
  employee_deferral_balance: 'participant.savings_rate.Employee Deferral Balance',
  roth_deferral_balance: 'participant.savings_rate.Roth Deferral Balance',
  rollover_balance: 'participant.savings_rate.Rollover Balance',
  employer_match_balance: 'participant.savings_rate.Employer Match Balance',
  employer_match_vested_balance: 'participant.savings_rate.Employer Match Vested Balance',
});

// Plan identifiers are PLAN attributes, projected separately from participant
// account figures. `poll.plan_id` is the plan the authenticated request selected
// and is the ONLY thing that may bind them; a plan fact never selects its own
// plan, and this identifier is not an account number or a recordkeeper code.
const PLAN_FACT_SOURCES = Object.freeze({
  legal_plan_name: 'plan.basic_info.official_plan_name',
  rk_plan_id: 'plan.plan_design.rk_plan_id',
  record_keeper: 'plan.plan_design.record_keeper_id',
});
const PLAN_FACT_PATTERNS = Object.freeze({
  legal_plan_name: /^[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,199}$/,
  rk_plan_id: /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/,
  record_keeper: /^[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,99}$/,
});
const PLAN_FACT_SENTINELS = Object.freeze(['unknown', 'n/a', 'na', 'none', 'null',
  '-', '--', 'tbd', 'not available', 'not applicable', 'pending']);
const planId = value => typeof value === 'string' && /^[1-9][0-9]{0,31}$/.test(value);

const own = (value, key) => Object.hasOwn(value, key);
const record = value => value !== null && typeof value === 'object' &&
  !Array.isArray(value) && Object.prototype.toString.call(value) === '[object Object]';
const jobId = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const ticketId = value => typeof value === 'string' && (
  /^TKT-[A-Za-z0-9-]{1,100}$/.test(value) || /^don:[A-Za-z0-9_.:/+-]{1,252}$/.test(value));

function calendarDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split('-').map(Number);
  if (year < 1 || month < 1 || month > 12) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return day >= 1 && day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
}

function timestamp(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match || !calendarDate(match[1]) || Number(match[2]) > 23 ||
      Number(match[3]) > 59 || Number(match[4]) > 59) return null;
  if (match[6] !== 'Z' && (Number(match[6].slice(1, 3)) > 23 ||
      Number(match[6].slice(4)) > 59 || match[6] === '-00:00')) return null;
  const millis = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}${match[6]}`);
  if (!Number.isFinite(millis)) return null;
  // Preserve microsecond chronology instead of Date.parse's millisecond rounding.
  return BigInt(millis) * 1000n + BigInt((match[5] || '').padEnd(6, '0'));
}

function conversationReference(value) {
  return record(value) && Object.keys(value).length === 7 &&
    value.type === 'devrev_conversation_snapshot' && value.schema_version === 1 &&
    value.hash_algorithm === 'sha256' && typeof value.digest === 'string' &&
    /^[0-9a-f]{64}$/.test(value.digest) &&
    ['complete', 'partial', 'truncated'].every(key => typeof value[key] === 'boolean');
}

function unavailableEvidence(value) {
  return (own(value, 'error') && value.error != null) ||
    (own(value, 'fallback') && value.fallback !== false);
}

function result(reasonCodes, facts = {}, planFacts = {}, boundPlanId = null) {
  const hasFacts = Object.keys(facts).length > 0;
  const hasPlanFacts = Object.keys(planFacts).length > 0;
  return {
    evidence_status: hasFacts || hasPlanFacts ? (reasonCodes.length ? 'partial' : 'matched') : 'unavailable',
    reason_codes: [...new Set(reasonCodes)],
    verified_participant_facts: hasFacts ? {
      identity_verified: true, identity_resolution_status: 'matched', facts,
    } : null,
    verified_plan_facts: hasPlanFacts ? {
      plan_id: boundPlanId, identity_verified: true,
      identity_resolution_status: 'matched', facts: planFacts,
    } : null,
    publication_authorized: false,
    participant_reply_safe: false,
    set_stage_solved: false,
    human_review_required: true,
  };
}

function validateCanonicalEvidence(input) {
  if (!record(input) || !record(input.reference)) return result(['invalid_reference']);
  const {reference, poll, now} = input;
  if (!own(reference, 'ticket_id') || !own(reference, 'ticket_job_id') ||
      !ticketId(reference.ticket_id) || !jobId(reference.ticket_job_id) ||
      Object.keys(reference).some(key => !['ticket_id', 'ticket_job_id', 'conversation_reference'].includes(key))) {
    return result(['invalid_reference']);
  }
  if (!record(poll) || !own(poll, 'ticket_id') || !own(poll, 'ticket_job_id') ||
      !ticketId(poll.ticket_id) || !jobId(poll.ticket_job_id) ||
      !record(poll.primary) || !Array.isArray(poll.related) ||
      Array.from(poll.related).some(item => !record(item))) return result(['invalid_poll']);
  if (poll.ticket_id !== reference.ticket_id) return result(['ticket_mismatch']);
  if (poll.ticket_job_id !== reference.ticket_job_id) return result(['job_mismatch']);
  if (poll.state !== 'succeeded') return result(['job_not_succeeded']);
  if (poll.error !== undefined && poll.error !== null) return result(['job_error']);

  const current = timestamp(now);
  if (current === null) return result(['invalid_now']);
  const created = timestamp(poll.created_at);
  const completed = timestamp(poll.completed_at);
  const expires = timestamp(poll.expires_at);
  if (created === null || completed === null || expires === null ||
      completed < created || completed > current || expires <= completed) {
    return result(['job_timestamps_invalid']);
  }
  if (expires <= current) return result(['job_expired']);

  if (reference.conversation_reference == null || poll.conversation_reference == null) {
    return result(['conversation_binding_missing']);
  }
  if (!conversationReference(reference.conversation_reference) || !conversationReference(poll.conversation_reference)) {
    return result(['conversation_binding_invalid']);
  }
  if ([reference.conversation_reference, poll.conversation_reference].some(value =>
    !value.complete || value.partial || value.truncated)) return result(['conversation_binding_incomplete']);
  if (reference.conversation_reference.digest !== poll.conversation_reference.digest) {
    return result(['conversation_binding_mismatch']);
  }

  const signals = [poll];
  const containers = [];
  const planContainers = [];
  const reasons = [];
  const appendMetadata = owner => {
    if (!own(owner, 'metadata')) return true;
    if (!record(owner.metadata)) return false;
    signals.push(owner.metadata);
    return true;
  };
  if (!appendMetadata(poll)) return result(['invalid_poll']);
  if (unavailableEvidence(poll) || unavailableEvidence(poll.metadata || {})) {
    return result(['job_evidence_unavailable']);
  }
  for (const inquiry of [poll.primary, ...poll.related]) {
    signals.push(inquiry);
    if (!appendMetadata(inquiry)) return result(['invalid_poll']);
    const inquiryUnavailable = ['failed', 'timeout'].includes(inquiry.scrape_status) ||
      unavailableEvidence(inquiry) || unavailableEvidence(inquiry.metadata || {});
    for (const key of ['generate_response', 'knowledge_answer']) {
      const answer = inquiry[key];
      if (answer == null) continue;
      if (!record(answer) || !appendMetadata(answer)) return result(['invalid_poll']);
      signals.push(answer);
      const metadata = answer.metadata;
      if (!metadata) continue;
      const unavailable = inquiryUnavailable || unavailableEvidence(answer) || unavailableEvidence(metadata);
      for (const [name, sink] of [['verified_participant_facts', containers],
        ['verified_plan_facts', planContainers]]) {
        if (!own(metadata, name)) continue;
        const context = metadata[name];
        if (!record(context)) return result(['identity_context_invalid']);
        // The existing backend projection returns {} when no disclosure facts
        // apply (for example, a related educational answer). This is absence,
        // not a conflicting claim about the account or plan another inquiry used.
        if (Object.keys(context).length === 0) continue;
        signals.push(context);
        if (unavailable) reasons.push('inquiry_evidence_unavailable');
        else sink.push(context);
      }
    }
  }
  if (signals.some(value => value.identity_verified === false ||
      ['ambiguous', 'not_found', 'access_error'].includes(value.identity_resolution_status))) {
    return result(['identity_veto']);
  }
  if (signals.some(value => (own(value, 'identity_verified') && typeof value.identity_verified !== 'boolean') ||
      (own(value, 'identity_resolution_status') && value.identity_resolution_status !== 'matched')) ||
      [...containers, ...planContainers].some(value => value.identity_verified !== true ||
        value.identity_resolution_status !== 'matched')) {
    return result(['identity_context_invalid']);
  }

  const projected = {};
  const signatures = new Map();
  const quarantined = new Set();
  for (const context of containers) {
    if (!record(context.facts)) return result(['invalid_fact']);
    for (const [key, source] of Object.entries(FACT_SOURCES)) {
      if (!own(context.facts, key)) continue;
      const entry = context.facts[key];
      const observed = record(entry) ? entry.observed_at ?? null : null;
      const asOf = record(entry) ? entry.as_of ?? null : null;
      const observedInstant = timestamp(observed);
      const datesValid = (observed === null || observedInstant !== null) &&
        (asOf === null || calendarDate(asOf)) && (observed !== null || asOf !== null);
      const valueValid = record(entry) && (key === 'first_name' ?
        typeof entry.value === 'string' && entry.value.trim().length > 0 && entry.value.length <= 80 &&
          /^[\p{L} .'-]+$/u.test(entry.value) : typeof entry.value === 'number' && Number.isFinite(entry.value));
      if (!record(entry) || entry.status !== 'known' || entry.source !== source || !datesValid || !valueValid) {
        quarantined.add(key);
        reasons.push('invalid_fact');
        continue;
      }
      if (observedInstant !== null && observedInstant > completed) {
        quarantined.add(key);
        reasons.push('fact_observed_after_completion');
        continue;
      }
      const signature = JSON.stringify([entry.value, source, asOf, observedInstant?.toString() ?? null]);
      if (signatures.has(key) && signatures.get(key) !== signature) {
        quarantined.add(key);
        reasons.push('fact_conflict');
        continue;
      }
      signatures.set(key, signature);
      projected[key] = {value: entry.value, status: 'known', source, observed_at: observed, as_of: asOf};
    }
  }
  for (const key of quarantined) delete projected[key];

  // Plan identifiers bind to the plan this authenticated poll reports for the
  // job, taken from the durable request. A container naming any other plan is a
  // cross-plan claim: it contributes nothing, and it does not disturb the
  // participant facts already projected above.
  const boundPlanId = planId(poll.plan_id) ? poll.plan_id : null;
  const planProjected = {};
  const planSignatures = new Map();
  const planQuarantined = new Set();
  for (const context of planContainers) {
    if (boundPlanId === null) {
      reasons.push('plan_binding_missing');
      continue;
    }
    if (context.plan_id !== boundPlanId) {
      reasons.push('plan_binding_mismatch');
      continue;
    }
    if (!record(context.facts)) return result(['invalid_plan_fact'], projected);
    for (const [key, source] of Object.entries(PLAN_FACT_SOURCES)) {
      if (!own(context.facts, key)) continue;
      const entry = context.facts[key];
      const observed = record(entry) ? entry.observed_at ?? null : null;
      const asOf = record(entry) ? entry.as_of ?? null : null;
      const observedInstant = timestamp(observed);
      const datesValid = (observed === null || observedInstant !== null) &&
        (asOf === null || calendarDate(asOf)) && (observed !== null || asOf !== null);
      const text = record(entry) && typeof entry.value === 'string' ? entry.value.trim() : null;
      const valueValid = text !== null && PLAN_FACT_PATTERNS[key].test(text) &&
        !PLAN_FACT_SENTINELS.includes(text.toLowerCase());
      if (!record(entry) || entry.status !== 'known' || entry.source !== source ||
          !datesValid || !valueValid) {
        planQuarantined.add(key);
        reasons.push('invalid_plan_fact');
        continue;
      }
      const asOfInstant = asOf === null ? null : timestamp(`${asOf}T00:00:00Z`);
      if (asOfInstant !== null && asOfInstant > completed) {
        planQuarantined.add(key);
        reasons.push('plan_fact_as_of_after_completion');
        continue;
      }
      if (observedInstant !== null && observedInstant > completed) {
        planQuarantined.add(key);
        reasons.push('plan_fact_observed_after_completion');
        continue;
      }
      const signature = JSON.stringify([text, source, asOf, observedInstant?.toString() ?? null]);
      if (planSignatures.has(key) && planSignatures.get(key) !== signature) {
        planQuarantined.add(key);
        reasons.push('plan_fact_conflict');
        continue;
      }
      planSignatures.set(key, signature);
      planProjected[key] = {value: text, status: 'known', source, observed_at: observed, as_of: asOf};
    }
  }
  for (const key of planQuarantined) delete planProjected[key];

  if (Object.keys(projected).length === 0 && Object.keys(planProjected).length === 0) {
    reasons.push('no_verified_facts');
  }
  return result(reasons, projected, planProjected, boundPlanId);
}

module.exports = {validateCanonicalEvidence};
