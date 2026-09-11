// Approved criterion: the account selected by the PA workflow may supply its
// own name/figures in an internal draft. This is not permission to send or close.
const selected = $('Participant Search').first().json;
const request = $('Include Ticket data').item.json;
function canonicalId(value) {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0 ? String(value) : null;
  return typeof value === 'string' && /^[1-9]\d*$/.test(value.trim()) ? value.trim() : null;
}
const participant = canonicalId(selected.userData?.pptId);
const plan = canonicalId(selected.userData?.planId);
if (selected.Result !== 'Participant was found' || !participant || !plan ||
    participant !== canonicalId(request.userData?.pptId) || plan !== canonicalId(request.userData?.planId)) return null;
return {identity_resolution_status:'matched',identity_verified:true,
  response_source_reason:'account_context_required',provided_identifiers:[]};
