// Shared source for the self-contained n8n Code node candidates.
// Only closed, consumer-relevant metadata is retained. No raw extraction notes.
function projectMetadata(value) {
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
const raw = $input.first().json;
// /handle-ticket devuelve un array (o un objeto en la ruta inline). Normalizamos.
const data = Array.isArray(raw) ? raw[0] : raw;


const getFields = $('Get fields1').first().json;
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
    out.metadata = projectMetadata(iq.generate_response.metadata);
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
      metadata: projectMetadata(iq.knowledge_answer.metadata),
    };
  }


  // Ruta needs_more_info
  if (iq.needs_more_info_message) {
    out.needs_more_info_message = iq.needs_more_info_message;
  }


  out.human_review_required = inquiryRequiresReview(iq);
  out.participant_reply_safe = !out.human_review_required;
  return out;
}


// primary + related (soporta tickets multi-inquiry, p.ej. financiero + account_access)
const inquiries = [data.primary, ...(data.related || [])]
  .filter(Boolean)
  .map(cleanInquiry);


const humanReviewRequired = data.state !== 'succeeded' || data.next_action !== 'send_participant_reply' ||
  requiresReview(data) || requiresReview(data.metadata) || inquiries.length === 0 ||
  inquiries.some(iq => iq.human_review_required);
const payload = {
  ticketId: getFields.ticketId,
  ticket_job_id: data.ticket_job_id,
  state: data.state,
  next_action: humanReviewRequired ? 'human_review' : 'send_participant_reply',
  participant_reply_safe: !humanReviewRequired,
  human_review_required: humanReviewRequired,
  metadata: projectMetadata(data.metadata),
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
