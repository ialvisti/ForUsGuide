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
