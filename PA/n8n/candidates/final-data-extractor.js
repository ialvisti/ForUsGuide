// Correlation metadata is independent of model prose. The observed DevRev
// emitter can send an opaque string containing fenced JSON and unescaped quotes.
// Never repair that string to recover an identifier or a publication decision.
function normalizeFinalTransport(webhook) {
  const fail = () => { throw new Error('PA response correlation failed; no ticket write is permitted'); };
  const validId = value => typeof value === 'string' && /^TKT-[A-Za-z0-9-]{1,100}$/.test(value);
  const header = webhook?.headers?.['x-pa-ticket-id'];
  if (header !== undefined && !validId(header)) fail();
  let body = webhook?.body;
  if (typeof body === 'string') {
    if (!body.trim() || body.length > 131072) throw new Error('Invalid PA response contract');
    try { body = JSON.parse(body); } catch (_) { /* Opaque model text stays data. */ }
  }
  const structured = body && typeof body === 'object' && !Array.isArray(body) &&
    (Object.hasOwn(body, 'ticketId') || Object.hasOwn(body, 'agentResponse'));
  if (structured) {
    if (!validId(body.ticketId) || (header !== undefined && header !== body.ticketId)) fail();
    const response = body.agentResponse;
    if ((!response || typeof response !== 'object' || Array.isArray(response)) &&
        (typeof response !== 'string' || !response.trim())) throw new Error('Invalid PA response contract');
    if (JSON.stringify(response).length > 131072) throw new Error('Invalid PA response contract');
    return {ticketId:header ?? body.ticketId, agentResponse:response};
  }
  if (!validId(header)) fail();
  if (typeof webhook.body !== 'string' || !webhook.body.trim()) throw new Error('Invalid PA response contract');
  return {ticketId:header, agentResponse:webhook.body};
}
// Candidate for Decision, Response, Notes / Data extractor.
// Current transport contains a model response and ticketId, not a trusted job
// decision. Keep its recommendation advisory; never certify publication here.
const transport = normalizeFinalTransport($('Webhook').first().json);
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
