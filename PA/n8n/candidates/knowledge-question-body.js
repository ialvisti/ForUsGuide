// Body of the JSON.stringify expression in FUG / Knowledge Question.
// This node is specifically on the account-lookup fallback branch.
const found = $('Participant Search').first().json.identity_context;
const allowedStatus = ['not_found', 'ambiguous', 'access_error'];
const reasons = {not_found:'account_not_found', ambiguous:'account_ambiguous', access_error:'account_lookup_failed'};
const identityContext = found && allowedStatus.includes(found.identity_resolution_status) ? {
  identity_resolution_status: found.identity_resolution_status,
  response_source_reason: reasons[found.identity_resolution_status],
  identity_verified: false,
  provided_identifiers: Array.isArray(found.provided_identifiers) ? [...new Set(found.provided_identifiers.filter(item =>
    ['name', 'email', 'employer', 'date_of_birth', 'last_four_ssn'].includes(item)))] : [],
} : {identity_resolution_status:'ambiguous',response_source_reason:'account_context_required',identity_verified:false,provided_identifiers:[]};
return JSON.stringify({
  question: $('Parse response').first().json.question,
  ticket_id: $('Get fields1').first().json.ticketId,
  identity_context: identityContext,
});
