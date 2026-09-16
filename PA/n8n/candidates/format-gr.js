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
const identityVeto = identitySignals.some(rejectsAccountIdentity);
const needsAccountContext = iq => !!iq.generate_response ||
  iq.knowledge_answer?.metadata?.response_source_reason !== 'general_knowledge';
const accountIdentityVeto = identityVeto && rawInquiries.some(needsAccountContext);



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
    out.metadata = projectMetadata(iq.generate_response.metadata, identityVeto);
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
      metadata: projectMetadata(iq.knowledge_answer.metadata, identityVeto),
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
  state: data.state,
  next_action: humanReviewRequired ? 'human_review' : 'send_participant_reply',
  participant_reply_safe: !humanReviewRequired,
  human_review_required: humanReviewRequired,
  metadata: projectMetadata(data.metadata, identityVeto),
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
