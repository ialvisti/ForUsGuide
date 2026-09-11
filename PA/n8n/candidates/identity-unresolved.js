// All legacy unresolved branches reach this node. Keep their evidence separate.
function readExecuted(name) {
  const node = $(name);
  return node.isExecuted ? node.first().json : null;
}
function lookupEvidence(data) {
  if (!data) return null;
  const state = data.state ?? data.jobs?.[0]?.state;
  if (['failed', 'canceled', 'cancelled'].includes(state)) return {status: 'access_error'};
  if (state !== 'succeeded') return null;
  const raw = data.result?.data?.count;
  const count = typeof raw === 'number' ? raw : (typeof raw === 'string' && /^\d+$/.test(raw.trim()) ? Number(raw) : NaN);
  if (!Number.isSafeInteger(count) || count < 0) return {status: 'access_error'};
  return {status: count === 0 ? 'not_found' : 'ambiguous'};
}
const lookups = ['Get Job', 'Get Job1'].map(name => lookupEvidence(readExecuted(name))).filter(Boolean);
// A branch reaching here has not selected a verified participant, even if a
// search returned one or more candidates. No search is not a zero-result search.
let status = 'ambiguous';
if (lookups.some(item => item.status === 'access_error')) status = 'access_error';
else if (lookups.some(item => item.status === 'ambiguous')) status = 'ambiguous';
else if (lookups.length) status = 'not_found';
const reason = {not_found:'account_not_found', ambiguous:'account_ambiguous', access_error:'account_lookup_failed'}[status];
const request = readExecuted('Participant Verifier') || {};
const extracted = readExecuted('Gather Info Specialist1')?.output || {};
const provided = [];
const present = value => typeof value === 'string' && value.trim() !== '' && !/^(not found|null|unknown|n\/a)$/i.test(value.trim());
if (present(request.userName)) provided.push('name');
if (present(request.userEmail) && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(request.userEmail)) provided.push('email');
const message = request.ticket_messages?.message_1;
if (present(extracted.company_name) && typeof message === 'string' && message.toLowerCase().includes(extracted.company_name.trim().toLowerCase())) provided.push('employer');
return {json: {
  Result: 'Participant was NOT found',
  participant_information: 'The account has not been resolved. Use the available identity clues and the approved verification process before providing account-specific instructions.',
  identity_context: {identity_resolution_status:status, response_source_reason:reason,
    identity_verified:false, provided_identifiers:provided},
  human_review_required:true,
}};
