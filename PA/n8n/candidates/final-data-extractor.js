// Candidate for Decision, Response, Notes / Data extractor.
// Current transport contains a model response and ticketId, not a trusted job
// decision. Keep its recommendation advisory; never certify publication here.
const transport = $('Webhook').first().json.body;
const parsed = JSON.parse($input.first().json.output);
if (!transport || typeof transport.ticketId !== 'string' || !transport.ticketId.trim() ||
    !parsed || Array.isArray(parsed) || parsed.ticketId !== transport.ticketId) {
  throw new Error('PA response correlation failed; no ticket write is permitted');
}
let original;
try {
  original = typeof transport.agentResponse === 'string' ? JSON.parse(transport.agentResponse) : transport.agentResponse;
} catch (_) {
  original = null;
}
if (typeof parsed.participant_reply !== 'string' || typeof parsed.stage_reason !== 'string' ||
    !(parsed.internal_notes === null || typeof parsed.internal_notes === 'string')) {
  throw new Error('Invalid PA response contract; retain for internal review');
}
const doubleLineBreaks = str => typeof str === 'string' ? str.replace(/\n/g, '\n\u3164\n\n') : str;
const modelRecommendedSolved = original?.set_stage_solved === true && parsed.set_stage_solved === true;
return [{json: {
  ticketId: transport.ticketId,
  participant_reply: doubleLineBreaks(parsed.participant_reply),
  set_stage_solved: false,
  stage_reason: 'Advisor review is required. ' + parsed.stage_reason,
  internal_notes: doubleLineBreaks(parsed.internal_notes),
  model_recommended_solved: modelRecommendedSolved,
  human_review_required: true,
  participant_reply_safe: false,
  publication_authorized: false,
  verification_reason: 'The current transport does not contain an independently correlated backend decision.',
}}];
