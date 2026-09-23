// Shared source for the self-contained n8n Code node candidates.
// Only closed, consumer-relevant metadata is retained. No raw extraction notes.
function projectVerifiedFacts(value) {
  if (!value || value.identity_verified !== true || value.identity_resolution_status !== 'matched' || !value.facts || Array.isArray(value.facts)) return null;
  const sources = {first_name:'participant.census.First Name', account_balance:'participant.savings_rate.Account Balance',
    employee_deferral_balance:'participant.savings_rate.Employee Deferral Balance', roth_deferral_balance:'participant.savings_rate.Roth Deferral Balance',
    rollover_balance:'participant.savings_rate.Rollover Balance', employer_match_balance:'participant.savings_rate.Employer Match Balance',
    employer_match_vested_balance:'participant.savings_rate.Employer Match Vested Balance'};
  function date(value, timestamp) {
    if (typeof value !== 'string' || !(timestamp ? /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/ : /^\d{4}-\d{2}-\d{2}$/).test(value)) return null;
    const ms = Date.parse(value), calendar = Date.parse(value.slice(0,10));
    return Number.isFinite(ms) && Number.isFinite(calendar) && new Date(calendar).toISOString().slice(0,10) === value.slice(0,10) ? value : null;
  }
  const facts = {};
  for (const [key, source] of Object.entries(sources)) {
    const entry = value.facts[key];
    if (!entry || entry.status !== 'known' || entry.source !== source) continue;
    const observed = date(entry.observed_at,true), asOf = date(entry.as_of,false);
    if (!observed && !asOf) continue;
    const valid = key === 'first_name' ? typeof entry.value === 'string' && entry.value.trim().length > 0 && entry.value.length <= 80 && /^[\p{L} .'-]+$/u.test(entry.value) : typeof entry.value === 'number' && Number.isFinite(entry.value);
    if (valid) facts[key] = {value:entry.value,status:'known',source,observed_at:observed,as_of:asOf};
  }
  return Object.keys(facts).length ? {identity_verified:true,identity_resolution_status:'matched',facts} : null;
}
function rejectsAccountIdentity(value) {
  return value?.identity_verified === false ||
    ['ambiguous', 'not_found', 'access_error'].includes(value?.identity_resolution_status);
}
function projectMetadata(value, parentIdentityVeto = false) {
  const m = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const out = {};
  for (const key of ['human_review_required', 'participant_reply_safe', 'identity_verified', 'fallback']) {
    if (typeof m[key] === 'boolean') out[key] = m[key];
  }
  const enums = {
    identity_resolution_status: ['matched', 'ambiguous', 'not_found', 'access_error'],
    response_source_reason: ['general_knowledge', 'account_not_found', 'account_ambiguous', 'account_lookup_failed', 'account_context_required'],
  };
  for (const [key, allowed] of Object.entries(enums)) if (allowed.includes(m[key])) out[key] = m[key];
  if (Array.isArray(m.provided_identifiers)) out.provided_identifiers = [...new Set(m.provided_identifiers.filter(
    item => ['name', 'email', 'employer', 'date_of_birth', 'last_four_ssn'].includes(item)))];
  if (Array.isArray(m.requested_questions)) out.requested_questions = m.requested_questions.slice(0, 12).map(q => typeof q === 'string' ? q.slice(0, 1000) : '');
  if (Array.isArray(m.question_coverage)) out.question_coverage = m.question_coverage.slice(0, 12).filter(c =>
    c && Number.isInteger(c.question_index) && c.question_index >= 0 && c.question_index < 12
  ).map(c => ({question_index: c.question_index,
    status: ['answered', 'not_applicable'].includes(c.status) ? c.status : 'needs_verification',
    answer_reference: typeof c.answer_reference === 'string' ? c.answer_reference.slice(0, 600) : ''}));
  if (Number.isInteger(m.incomplete_question_count)) out.incomplete_question_count = Math.max(0, m.incomplete_question_count);
  if (m.handoff && typeof m.handoff === 'object') {
    out.handoff = {};
    for (const key of ['reason', 'next_action']) if (typeof m.handoff[key] === 'string') out.handoff[key] = m.handoff[key].slice(0, 1000);
  }
  const facts = parentIdentityVeto || rejectsAccountIdentity(m) ? null : projectVerifiedFacts(m.verified_participant_facts);
  if (facts) out.verified_participant_facts = facts;
  return out;
}
function requiresReview(value) {
  const m = projectMetadata(value);
  return m.human_review_required === true || m.participant_reply_safe === false || m.fallback === true ||
    m.incomplete_question_count > 0 || (m.question_coverage || []).some(c => c.status === 'needs_verification');
}
function inquiryRequiresReview(iq) {
  const gr = iq.generate_response;
  const kq = iq.knowledge_answer;
  if (requiresReview(iq) || requiresReview(gr?.metadata) || requiresReview(kq?.metadata)) return true;
  if (gr?.response) return ['blocked_missing_data', 'ambiguous_plan_rules'].includes(gr.response.outcome) || gr.response.escalation?.needed === true;
  if (kq) return kq.metadata?.response_source_reason !== 'general_knowledge';
  return true;
}

function escapeJSONStringForHTTP(obj) {
  const compact = JSON.stringify(obj);
  const escaped = compact
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
  return escaped;
}

// ---- BD1/BD4: plan-identifier transport and nested identity veto --------
// Added AFTER the shared consumer contract (PA/n8n/consumer-contract.js); that
// prefix stays byte-identical so format-kq.js keeps sharing it unchanged.
//
// BD1. PA/n8n/canonical-evidence.js is the authoritative shape for plan
// identifiers. Its rules are reproduced here, not relaxed: a closed three-field
// allowlist with exact source strings, a value pattern, a sentinel refusal,
// valid dates, and a container that must name the SAME plan the authenticated
// job reports. Nothing is passed through unchecked, and a model-supplied
// plan_id never binds itself.
const PLAN_FACT_SOURCES = {
  legal_plan_name: 'plan.basic_info.official_plan_name',
  rk_plan_id: 'plan.plan_design.rk_plan_id',
  record_keeper: 'plan.plan_design.record_keeper_id',
};
const PLAN_FACT_PATTERNS = {
  legal_plan_name: /^[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,199}$/,
  rk_plan_id: /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/,
  record_keeper: /^[A-Za-z0-9][A-Za-z0-9 .,'&()/-]{0,99}$/,
};
const PLAN_FACT_SENTINELS = ['unknown', 'n/a', 'na', 'none', 'null', '-', '--',
  'tbd', 'not available', 'not applicable', 'pending'];
// A plan_id is an internal plan selector: never an account number, never a
// recordkeeper plan code, and never read out of model metadata to bind itself.
const isPlanId = value => typeof value === 'string' && /^[1-9][0-9]{0,31}$/.test(value);
// Date shapes accepted by PA/n8n/canonical-evidence.js, reproduced here so the
// consumer and the canonical validator agree about which occurrence is valid.
const factTimestamp = value => {
  if (typeof value !== 'string' ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) ||
      !Number.isFinite(Date.parse(value))) return null;
  return new Date(Date.parse(value.slice(0, 10))).toISOString().slice(0, 10) === value.slice(0, 10)
    ? Date.parse(value) : null;
};
const factCalendarDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(value)) &&
  new Date(Date.parse(value)).toISOString().slice(0, 10) === value;
// CD2. canonical-evidence.js quarantines a PARTICIPANT field across the whole
// job when any occurrence of it is invalid, is observed after the job completed,
// or disagrees with another occurrence. projectMetadata belongs to the frozen
// shared contract and projects one container at a time, so that cross-inquiry
// rule is applied here, after it, over every container of the same job. A field
// that is simply repeated with the same value is NOT quarantined, and an
// unrelated valid field in the same container is untouched.
const PARTICIPANT_FACT_SOURCES = {
  first_name: 'participant.census.First Name',
  account_balance: 'participant.savings_rate.Account Balance',
  employee_deferral_balance: 'participant.savings_rate.Employee Deferral Balance',
  roth_deferral_balance: 'participant.savings_rate.Roth Deferral Balance',
  rollover_balance: 'participant.savings_rate.Rollover Balance',
  employer_match_balance: 'participant.savings_rate.Employer Match Balance',
  employer_match_vested_balance: 'participant.savings_rate.Employer Match Vested Balance',
};
// null means this occurrence alone must quarantine the field for the whole job.
function participantFactSignature(entry, key, source, completedAt) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return null;
  if (entry.status !== 'known' || entry.source !== source) return null;
  const observed = Object.hasOwn(entry, 'observed_at') ? entry.observed_at ?? null : null;
  const asOf = Object.hasOwn(entry, 'as_of') ? entry.as_of ?? null : null;
  const observedInstant = observed === null ? null : factTimestamp(observed);
  if (observed !== null && observedInstant === null) return null;
  if (asOf !== null && !factCalendarDate(asOf)) return null;
  if (observed === null && asOf === null) return null;
  const valueValid = key === 'first_name'
    ? typeof entry.value === 'string' && entry.value.trim().length > 0 &&
      entry.value.length <= 80 && /^[\p{L} .'-]+$/u.test(entry.value)
    : typeof entry.value === 'number' && Number.isFinite(entry.value);
  if (!valueValid) return null;
  // An observation cannot postdate the job that reported it. canonical compares
  // observed_at only for participant facts; as_of carries no time of day.
  if (observedInstant !== null && completedAt !== null && observedInstant > completedAt) return null;
  return JSON.stringify([entry.value, source, asOf, observedInstant]);
}
function participantFactSignatures(containers, completedAt) {
  const seen = new Map(), quarantined = new Set();
  for (const context of containers) {
    const facts = context && typeof context === 'object' && !Array.isArray(context) ? context.facts : null;
    if (!facts || typeof facts !== 'object' || Array.isArray(facts)) continue;
    for (const [key, source] of Object.entries(PARTICIPANT_FACT_SOURCES)) {
      if (!Object.hasOwn(facts, key)) continue;
      const signature = participantFactSignature(facts[key], key, source, completedAt);
      if (signature === null || (seen.has(key) && seen.get(key) !== signature)) quarantined.add(key);
      else seen.set(key, signature);
    }
  }
  return quarantined;
}
function planFactDates(entry) {
  const observed = Object.hasOwn(entry, 'observed_at') ? entry.observed_at ?? null : null;
  const asOf = Object.hasOwn(entry, 'as_of') ? entry.as_of ?? null : null;
  const timestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
  const calendar = /^\d{4}-\d{2}-\d{2}$/;
  const valid = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) &&
    new Date(Date.parse(value.slice(0, 10))).toISOString().slice(0, 10) === value.slice(0, 10);
  if (observed !== null && !(timestamp.test(observed) && valid(observed))) return null;
  if (asOf !== null && !(calendar.test(asOf) && valid(asOf))) return null;
  if (observed === null && asOf === null) return null;
  // An observation cannot postdate the job that reported it; a date-only as_of
  // has no time zone, so it is compared at the start of its UTC day only.
  return {observed, asOf, latest: Math.max(
    observed === null ? -Infinity : Date.parse(observed),
    asOf === null ? -Infinity : Date.parse(asOf + 'T00:00:00Z'))};
}
// Every projected plan fact of one job, keyed by field, so that two inquiries
// disagreeing about the same identifier quarantine it everywhere instead of
// disclosing whichever copy happened to be read first.
function planFactSignatures(containers, boundPlanId, completedAt) {
  const seen = new Map(), conflicting = new Set();
  for (const context of containers) {
    for (const [key, entry] of Object.entries(projectPlanFactEntries(context, boundPlanId, completedAt))) {
      const signature = JSON.stringify(entry);
      if (seen.has(key) && seen.get(key) !== signature) conflicting.add(key);
      else seen.set(key, signature);
    }
  }
  return conflicting;
}
function projectPlanFactEntries(value, boundPlanId, completedAt) {
  const facts = {};
  // CD3. The date-bound validation is never skipped: with no authoritative
  // completion instant from the correlated job there is nothing to bound the
  // observation against, so the disclosure fails closed rather than relaxing.
  if (completedAt === null || completedAt === undefined) return facts;
  if (!boundPlanId || !value || typeof value !== 'object' || Array.isArray(value)) return facts;
  if (value.identity_verified !== true || value.identity_resolution_status !== 'matched') return facts;
  if (!value.facts || typeof value.facts !== 'object' || Array.isArray(value.facts)) return facts;
  // A container naming another plan is a cross-plan claim: it yields nothing.
  if (value.plan_id !== boundPlanId) return facts;
  for (const [key, source] of Object.entries(PLAN_FACT_SOURCES)) {
    const entry = value.facts[key];
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    if (entry.status !== 'known' || entry.source !== source) continue;
    const dates = planFactDates(entry);
    if (!dates) continue;
    if (dates.latest > completedAt) continue;
    const text = typeof entry.value === 'string' ? entry.value.trim() : null;
    if (text === null || !PLAN_FACT_PATTERNS[key].test(text) ||
        PLAN_FACT_SENTINELS.includes(text.toLowerCase())) continue;
    facts[key] = {value: text, status: 'known', source, observed_at: dates.observed, as_of: dates.asOf};
  }
  return facts;
}
function projectVerifiedPlanFacts(value, boundPlanId, completedAt = null, quarantined = new Set()) {
  const facts = projectPlanFactEntries(value, boundPlanId, completedAt);
  for (const key of quarantined) delete facts[key];
  return Object.keys(facts).length ? {plan_id: boundPlanId, identity_verified: true,
    identity_resolution_status: 'matched', facts} : null;
}
// BD4. rejectsAccountIdentity reads FLAT keys only, and projectMetadata drops a
// nested wrapper entirely, so an identity-unresolved result carried inside
// `identity_context` (PA/n8n/candidates/identity-unresolved.js) used to leave a
// stale positive reference standing. The nested negative is authoritative here
// and the conflict fails closed. A nested POSITIVE authorizes nothing: the
// wrapper is still dropped and never promoted to a flat verified identity.
const NESTED_IDENTITY_KEYS = ['identity_context'];
function rejectsNestedAccountIdentity(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  return NESTED_IDENTITY_KEYS.some(key => {
    const nested = value[key];
    return !!nested && typeof nested === 'object' && !Array.isArray(nested) && rejectsAccountIdentity(nested);
  });
}
const rejectsAnyAccountIdentity = value =>
  rejectsAccountIdentity(value) || rejectsNestedAccountIdentity(value);
// projectMetadata is part of the shared contract and is not edited. Plan facts
// are attached here, after it, under the same identity veto as participant facts.
function withPlanFacts(metadata, identityVeto, boundPlanId, completedAt, quarantined,
  quarantinedParticipant = new Set()) {
  const out = projectMetadata(metadata, identityVeto);
  if (out.verified_participant_facts) {
    // CD4. Participant figures now fail closed exactly like plan identifiers
    // (CD3) and like the canonical poll, which refuses the whole evidence set
    // when the job carries no usable completion instant. Without an
    // authoritative `completed_at` from the correlated poll body there is
    // nothing to bound `observed_at` against, so a backdated or replayed
    // figure cannot be detected and none is disclosed. This withholds the
    // reference only; it invents no review requirement, so a supported
    // non-personal answer in the same job is unaffected.
    if (completedAt === null || completedAt === undefined) {
      delete out.verified_participant_facts;
    } else {
      // CD2: a participant field contested anywhere in this job is withheld
      // here too, so every inquiry of one payload agrees about it.
      for (const key of quarantinedParticipant) delete out.verified_participant_facts.facts[key];
      if (!Object.keys(out.verified_participant_facts.facts).length) delete out.verified_participant_facts;
    }
  }
  if (identityVeto || !metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return out;
  const planFacts = projectVerifiedPlanFacts(
    metadata.verified_plan_facts, boundPlanId, completedAt, quarantined);
  if (planFacts) out.verified_plan_facts = planFacts;
  return out;
}


// ---- inputs ------------------------------------------------------------
// Compare the producer's response to the independently accepted execution in
// this workflow run before copying any response text into a ticket note.
const correlationFailure = () => {
  throw new Error('PA execution correlation failed; no ticket write is permitted');
};
function oneResponse(raw) {
  if (Array.isArray(raw)) {
    if (raw.length !== 1) return correlationFailure();
    raw = raw[0];
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return correlationFailure();
  return raw;
}
const data = oneResponse($input.first().json);
const getFields = $('Get fields1').first().json;
if (typeof getFields?.ticketId !== 'string' || !getFields.ticketId.trim()) correlationFailure();
let accepted;
try { accepted = oneResponse($('Handle Ticket').first().json); }
catch (_) { correlationFailure(); }
let executionReference;
const pollFields = ['ticket_job_id', 'ticket_id', 'state', 'next_action', 'created_at', 'completed_at', 'expires_at'];
if (Object.hasOwn(accepted, 'ticket_job_id') || pollFields.some(key => Object.hasOwn(data, key))) {
  if (typeof accepted.ticket_job_id !== 'string' || !/^[0-9a-f]{32}$/.test(accepted.ticket_job_id) ||
      data.ticket_job_id !== accepted.ticket_job_id || data.ticket_id !== getFields.ticketId) correlationFailure();
  executionReference = {
    source: 'ticket_job_poll', ticket_id: data.ticket_id, ticket_job_id: data.ticket_job_id,
    created_at: data.created_at ?? null, completed_at: data.completed_at ?? null, expires_at: data.expires_at ?? null,
  };
} else {
  // v1 can return a KQ response inline. It has no public job reference; accept
  // only the same response returned by Handle Ticket, never an unrelated body.
  if (!data.primary || JSON.stringify(data) !== JSON.stringify(accepted)) correlationFailure();
  executionReference = {source: 'handle_ticket_inline', ticket_id: getFields.ticketId, ticket_job_id: null};
}
// Job dates describe execution/retention, not source freshness or whether the
// last participant clarification was consumed. They grant no new permission.
// A ticket job carries one selected participant account. Any explicit identity
// rejection in its enclosing or inquiry metadata vetoes all positive references.
const rawInquiries = [data.primary, ...(data.related || [])].filter(Boolean);
const identitySignals = [data, data.metadata, ...rawInquiries.flatMap(iq =>
  [iq, iq.metadata, iq.generate_response?.metadata, iq.knowledge_answer?.metadata])];
const identityVeto = identitySignals.some(rejectsAnyAccountIdentity);
const needsAccountContext = iq => !!iq.generate_response ||
  iq.knowledge_answer?.metadata?.response_source_reason !== 'general_knowledge';
const accountIdentityVeto = identityVeto && rawInquiries.some(needsAccountContext);
// Plan identifiers bind to the plan of the accepted job, taken from the poll
// body this run already correlated above — never from model metadata, and never
// from a value the draft could choose. If the accepted response names a plan
// too, the two must agree; a disagreement discloses no plan identifier at all.
// Job completion is the only trusted instant available here, and it is read
// from the correlated poll body, never from model output. CD3: without it a
// plan identifier is not disclosable at all (see projectPlanFactEntries).
// CD4: and neither is a participant figure (see withPlanFacts). The model
// controls its own metadata, so `metadata.completed_at` is never consulted;
// only the poll body this run already correlated can authorize a disclosure.
const jobCompletedAt = factTimestamp(data.completed_at);
const polledPlanId = isPlanId(data.plan_id) ? data.plan_id : null;
const acceptedPlanId = isPlanId(accepted.plan_id) ? accepted.plan_id : null;
const boundPlanId = jobCompletedAt !== null && polledPlanId !== null &&
  (acceptedPlanId === null || acceptedPlanId === polledPlanId) ? polledPlanId : null;
// CD6. The AGENT-3 contract binds plan evidence to a TOP-LEVEL `plan_id` on the
// payload and refuses every plan identifier without it. That binding is emitted
// here ONLY from `boundPlanId` above - the value this run already correlated
// between the accepted job and its poll body - so it carries exactly the same
// guarantees the reference projection carries: a valid `completed_at`, a
// polled plan identifier, and no disagreement with the accepted response.
// It is never read from `metadata.verified_plan_facts.plan_id`, from model
// prose or from any other container, because a reference that named itself
// would bind itself. An identity veto withholds the binding as well, so a
// vetoed job cannot present a plan identifier as disclosable; that keeps the
// emitted binding aligned with withPlanFacts, which attaches no reference
// under a veto. When the binding is not eligible the key is ABSENT, which the
// contract reads as "missing binding, no plan identifier may be shared".
const disclosablePlanId = identityVeto ? null : boundPlanId;
// Every fact container of this job, scanned once, so that two inquiries
// disagreeing about one field quarantine it everywhere instead of disclosing
// whichever copy happened to be read first.
// CD5. The job's top-level `data.metadata` is deliberately included.
// canonical-evidence.js reads fact containers only from inquiry
// generate_response/knowledge_answer metadata and ignores poll.metadata, so
// this scan is intentionally STRICTER than canonical, never looser: this
// consumer does project data.metadata into the payload's own top-level
// metadata, and a figure the payload emits at that level must not be able to
// disagree with the same field in its inquiries. A conflict anywhere -
// top level or inquiry - withholds the field everywhere. Parity with
// canonical is unaffected because canonical-judgeable wrappers carry their
// facts on inquiries.
const factContainerMetadata = identityVeto ? [] :
  [data.metadata, ...rawInquiries.flatMap(iq =>
    [iq.generate_response?.metadata, iq.knowledge_answer?.metadata])]
    .filter(metadata => metadata && typeof metadata === 'object' && !Array.isArray(metadata));
const quarantinedPlanFacts = planFactSignatures(
  factContainerMetadata.map(metadata => metadata.verified_plan_facts).filter(Boolean),
  boundPlanId, jobCompletedAt);
const quarantinedParticipantFacts = participantFactSignatures(
  factContainerMetadata.map(metadata => metadata.verified_participant_facts).filter(Boolean),
  jobCompletedAt);
const planFactsOf = metadata => withPlanFacts(metadata, identityVeto, boundPlanId,
  jobCompletedAt, quarantinedPlanFacts, quarantinedParticipantFacts);



const firstContact =
  getFields.caseData?.ticketData?.firstContact ??
  getFields.ticketData?.firstContact ??
  getFields.firstContact;


// ---- quedarse SOLO con lo que el LLM redactor necesita ------------------
function cleanInquiry(iq, inquiryIndex) {
  if (!iq) return null;


  const out = {
    inquiry_index: inquiryIndex,
    inquiry: iq.inquiry,
    topic: iq.topic,
    route: iq.route,
  };


  // Ruta generate_response: contenido accionable para el participante
  const r = iq.generate_response?.response;
  if (r) {
    out.decision = iq.generate_response.decision;
    out.confidence = iq.generate_response.confidence;
    out.coverage_gaps = iq.generate_response.coverage_gaps;
    out.data_gaps = r.data_gaps;
    out.outcome_reason = r.outcome_reason;
    out.metadata = planFactsOf(iq.generate_response.metadata);
    out.outcome = r.outcome;
    out.response_to_participant = r.response_to_participant; // opening, key_points, steps, warnings
    out.questions_to_ask = r.questions_to_ask;
    out.escalation = r.escalation;
  }


  // Ruta knowledge_question
  if (iq.knowledge_answer) {
    out.knowledge_answer = {
      answer: iq.knowledge_answer.answer,
      key_points: iq.knowledge_answer.key_points,
      confidence_note: iq.knowledge_answer.confidence_note,
      coverage_gaps: iq.knowledge_answer.coverage_gaps,
      metadata: planFactsOf(iq.knowledge_answer.metadata),
    };
  }


  // Ruta needs_more_info
  if (iq.needs_more_info_message) {
    out.needs_more_info_message = iq.needs_more_info_message;
  }


  out.human_review_required = (identityVeto && needsAccountContext(iq)) || inquiryRequiresReview(iq);
  out.participant_reply_safe = !out.human_review_required;
  return out;
}


// primary + related (soporta tickets multi-inquiry, p.ej. financiero + account_access)
const inquiries = [data.primary, ...(data.related || [])]
  .filter(Boolean)
  .map(cleanInquiry);


const humanReviewRequired = accountIdentityVeto || data.state !== 'succeeded' || data.next_action !== 'send_participant_reply' ||
  requiresReview(data) || requiresReview(data.metadata) || inquiries.length === 0 ||
  inquiries.some(iq => iq.human_review_required);
const payload = {
  ticketId: getFields.ticketId,
  ticket_job_id: data.ticket_job_id,
  execution_reference: executionReference,
  ...(disclosablePlanId === null ? {} : {plan_id: disclosablePlanId}),
  state: data.state,
  next_action: humanReviewRequired ? 'human_review' : 'send_participant_reply',
  participant_reply_safe: !humanReviewRequired,
  human_review_required: humanReviewRequired,
  metadata: planFactsOf(data.metadata),
  responseSource: "Generate-Response",
  total_inquiries_in_ticket: data.total_inquiries_in_ticket,
  inquiries,
};


const bodyPayload = `\`\`\`\\n\\n${escapeJSONStringForHTTP(payload)}\\n\\n`;


// ---- tags (sin cambios) ------------------------------------------------
let tags = [
  { id: "don:core:dvrv-us-1:devo/1is7v8y722:tag/2910" }
];


if (firstContact === false || firstContact === 'false') {
  tags.push({ id: "don:core:dvrv-us-1:devo/1is7v8y722:tag/2887" });
} else {
  tags.push({ id: "don:core:dvrv-us-1:devo/1is7v8y722:tag/2886" });
}


const tagString = tags.map(t => JSON.stringify(t)).join(",\n  ");


return {
  json: {
    ticketId: getFields.ticketId,
    body: bodyPayload,
    part: "CAPL-1",
    tag: tagString,
  }
};
