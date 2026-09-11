// Correct the observed unreachable multi-candidate branch; retain legacy labels.
const input = $input.first().json;
const rawCount = input.result?.data?.count;
const count = typeof rawCount === 'number' ? rawCount :
  (typeof rawCount === 'string' && /^\d+$/.test(rawCount.trim()) ? Number(rawCount) : NaN);
const validCount = Number.isSafeInteger(count) && count >= 0;
const company = $('Relevant information found?1').first().json.output?.company_name;
const hasCompany = typeof company === 'string' && company.trim() !== '' && !/^(not found|null|unknown|n\/a)$/i.test(company.trim());
let matchStatus = 'No Match Found';
let status = 'access_error';
if (validCount) {
  status = count === 0 ? 'not_found' : count === 1 ? 'matched' : 'ambiguous';
  if (count === 1) matchStatus = 'Match Found';
  else if (count > 1 && hasCompany) matchStatus = 'AI Needed';
}
return {json: {matchStatus, identity_resolution_status: status, identity_verified: false,
  lookup_count: validCount ? count : null, lookup_count_status: validCount ? 'known' : 'parse_error'}};
