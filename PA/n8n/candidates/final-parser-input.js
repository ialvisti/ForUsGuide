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

return JSON.stringify(normalizeFinalTransport($('Webhook').first().json));
