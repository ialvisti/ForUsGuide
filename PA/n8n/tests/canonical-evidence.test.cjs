'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {validateCanonicalEvidence} = require('../canonical-evidence.js');

// Synthetic contract fixtures, not historical participant data or live replays.
const JOB = 'a'.repeat(32);
const NOW = '2026-09-16T12:00:00Z';
const source = {
  first_name: 'participant.census.First Name',
  account_balance: 'participant.savings_rate.Account Balance',
  employee_deferral_balance: 'participant.savings_rate.Employee Deferral Balance',
  roth_deferral_balance: 'participant.savings_rate.Roth Deferral Balance',
  rollover_balance: 'participant.savings_rate.Rollover Balance',
  employer_match_balance: 'participant.savings_rate.Employer Match Balance',
  employer_match_vested_balance: 'participant.savings_rate.Employer Match Vested Balance',
};
const binding = () => ({type: 'devrev_conversation_snapshot', schema_version: 1,
  hash_algorithm: 'sha256', digest: 'b'.repeat(64), complete:true, partial:false, truncated:false});
const fact = (key, value) => ({status: 'known', value, source: source[key],
  observed_at: '2026-09-16T10:59:00Z', as_of: '2026-09-15'});
const disclosure = () => ({identity_verified: true, identity_resolution_status: 'matched',
  facts: {first_name: fact('first_name', 'Synthetic'), account_balance: fact('account_balance', 123.45)}});
const inquiry = () => ({route: 'generate_response', generate_response: {
  metadata: {verified_participant_facts: disclosure()},
  response: {response_to_participant: {opening: 'Synthetic response'}}}});
const fixture = () => ({now: NOW,
  reference: {ticket_id: 'TKT-SYNTHETIC', ticket_job_id: JOB, conversation_reference: binding()},
  poll: {ticket_id: 'TKT-SYNTHETIC', ticket_job_id: JOB, state: 'succeeded',
    created_at: '2026-09-16T10:00:00Z', completed_at: '2026-09-16T11:00:00Z',
    expires_at: '2026-09-17T10:00:00Z', conversation_reference: binding(),
    primary: inquiry(), related: [], metadata: {}, error: null}});
const factsOf = data => data.poll.primary.generate_response.metadata.verified_participant_facts.facts;
function assertGuarded(result) {
  assert.equal(result.publication_authorized, false);
  assert.equal(result.participant_reply_safe, false);
  assert.equal(result.set_stage_solved, false);
  assert.equal(result.human_review_required, true);
}
function denied(input, reason) {
  const result = validateCanonicalEvidence(input);
  assertGuarded(result);
  assert.equal(result.evidence_status, 'unavailable');
  assert.equal(result.verified_participant_facts, null);
  assert.ok(result.reason_codes.includes(reason), JSON.stringify(result.reason_codes));
  return result;
}

test('Matches a canonical v1 execution and conversation, with only bounded facts', () => {
  const input = fixture();
  factsOf(input).ssn = {status: 'known', value: 'SYNTHETIC-PRIVATE'};
  factsOf(input).first_name.author = 'SYNTHETIC-STAFF';
  input.poll.primary.generate_response.metadata.raw_notes = 'SYNTHETIC-NOTE';
  const result = validateCanonicalEvidence(input);
  assertGuarded(result);
  assert.equal(result.evidence_status, 'matched');
  assert.deepEqual(result.reason_codes, []);
  assert.deepEqual(result.verified_participant_facts, disclosure());
  assert.doesNotMatch(JSON.stringify(result), /SYNTHETIC-PRIVATE|SYNTHETIC-STAFF|SYNTHETIC-NOTE/);
});

for (const key of Object.keys(source)) {
  test('Preserves approved field with exact source: ' + key, () => {
    const input = fixture();
    factsOf(input)[key] = fact(key, key === 'first_name' ? 'Synthetic' : 0);
    const result = validateCanonicalEvidence(input);
    assert.equal(result.verified_participant_facts.facts[key].value, key === 'first_name' ? 'Synthetic' : 0);
  });
}

for (const [name, mutate, reason] of [
  ['missing reference', x => {x.reference = null;}, 'invalid_reference'],
  ['URL in reference', x => {x.reference.ticket_job_id = 'https://example.invalid/job';}, 'invalid_reference'],
  ['reference supplied as model prose', x => {x.reference = JSON.stringify(x.reference);}, 'invalid_reference'],
  ['reference with uncontracted URL', x => {x.reference.url = 'https://example.invalid/';}, 'invalid_reference'],
  ['different job of same ticket', x => {x.poll.ticket_job_id = 'c'.repeat(32);}, 'job_mismatch'],
  ['different ticket', x => {x.poll.ticket_id = 'TKT-OTHER';}, 'ticket_mismatch'],
  ['DON is not a display identifier alias', x => {x.poll.ticket_id = 'don:core:example:devo/test:ticket/1';}, 'ticket_mismatch'],
  ['missing canonical job', x => {delete x.poll.ticket_job_id;}, 'invalid_poll'],
  ['missing canonical ticket', x => {delete x.poll.ticket_id;}, 'invalid_poll'],
  ['multiple poll responses', x => {x.poll = [x.poll, x.poll];}, 'invalid_poll'],
  ['missing primary result', x => {delete x.poll.primary;}, 'invalid_poll'],
  ['malformed related result', x => {x.poll.related = [null];}, 'invalid_poll'],
  ['non-succeeded state', x => {x.poll.state = 'partial';}, 'job_not_succeeded'],
  ['canonical error', x => {x.poll.error = 'SYNTHETIC-PRIVATE';}, 'job_error'],
  ['missing now', x => {delete x.now;}, 'invalid_now'],
  ['invalid now', x => {x.now = '2026-02-30T12:00:00Z';}, 'invalid_now'],
]) {
  test('Fail closed for ' + name, () => {
    const input = fixture(); mutate(input);
    const result = denied(input, reason);
    assert.doesNotMatch(JSON.stringify(result), /SYNTHETIC-PRIVATE|https:\/\//);
  });
}

test('Accepts an exact DON without converting it to a display ID', () => {
  const input = fixture();
  input.poll.ticket_id = input.reference.ticket_id = 'don:core:example:devo/test:ticket/1';
  assert.equal(validateCanonicalEvidence(input).evidence_status, 'matched');
});

for (const [name, mutate, reason] of [
  ['missing timestamp', x => {delete x.poll.completed_at;}, 'job_timestamps_invalid'],
  ['null timestamp', x => {x.poll.created_at = null;}, 'job_timestamps_invalid'],
  ['impossible date', x => {x.poll.created_at = '2026-02-30T10:00:00Z';}, 'job_timestamps_invalid'],
  ['unknown timezone', x => {x.poll.created_at = '2026-09-16T10:00:00';}, 'job_timestamps_invalid'],
  ['24:00 rollover', x => {x.poll.created_at = '2026-09-15T24:00:00Z';}, 'job_timestamps_invalid'],
  ['completed before created', x => {x.poll.completed_at = '2026-09-16T09:59:59Z';}, 'job_timestamps_invalid'],
  ['completion in future', x => {x.poll.completed_at = '2026-09-16T12:00:01Z';}, 'job_timestamps_invalid'],
  ['expired result', x => {x.poll.expires_at = '2026-09-16T11:59:59Z';}, 'job_expired'],
  ['expiry boundary', x => {x.poll.expires_at = NOW;}, 'job_expired'],
  ['expiry before completion', x => {x.poll.expires_at = '2026-09-16T10:30:00Z';}, 'job_timestamps_invalid'],
]) {
  test('Rejects ' + name, () => {const input = fixture(); mutate(input); denied(input, reason);});
}

test('Compares sub-millisecond job chronology without rounding away a reversal', () => {
  const input = fixture();
  input.poll.created_at = '2026-09-16T10:00:00.000002Z';
  input.poll.completed_at = '2026-09-16T10:00:00.000001Z';
  denied(input, 'job_timestamps_invalid');
});

test('Offsets represent instants and no arbitrary data freshness window is imposed', () => {
  const input = fixture();
  input.poll.created_at = '2026-09-16T05:00:00-05:00';
  factsOf(input).account_balance.observed_at = '2020-01-01T00:00:00Z';
  factsOf(input).account_balance.as_of = '2019-12-31';
  assert.equal(validateCanonicalEvidence(input).evidence_status, 'matched');
});

for (const field of ['reference', 'poll']) {
  test('Missing independent conversation binding in ' + field + ' hides all facts', () => {
    const input = fixture(); delete input[field].conversation_reference;
    denied(input, 'conversation_binding_missing');
  });
  test('A boolean cannot certify conversation binding in ' + field, () => {
    const input = fixture(); input[field].conversation_reference = true;
    denied(input, 'conversation_binding_invalid');
  });
}
test('A different conversation snapshot from the same ticket is not interchangeable', () => {
  const input = fixture(); input.poll.conversation_reference.digest = 'c'.repeat(64);
  denied(input, 'conversation_binding_mismatch');
});
test('An unknown conversation schema or extra URL cannot qualify as an independent binding', () => {
  for (const change of [{schema_version: 2}, {digest: 'not-a-digest'}, {url: 'https://example.invalid/'}]) {
    const input = fixture(); Object.assign(input.poll.conversation_reference, change);
    denied(input, 'conversation_binding_invalid');
  }
});
test('Model text and metadata cannot repair missing canonical correlation fields', () => {
  for (const field of ['ticket_id', 'ticket_job_id', 'created_at', 'conversation_reference']) {
    const input = fixture();
    input.poll.metadata[field] = input.poll[field];
    input.poll.agentResponse = JSON.stringify(input.poll);
    delete input.poll[field];
    const reason = field === 'created_at' ? 'job_timestamps_invalid' :
      field === 'conversation_reference' ? 'conversation_binding_missing' : 'invalid_poll';
    denied(input, reason);
  }
});

for (const location of ['poll', 'poll_metadata', 'inquiry', 'inquiry_metadata', 'gr_metadata', 'kq_metadata', 'facts_context']) {
  test('Identity veto overrides all positive facts at ' + location, () => {
    const input = fixture(); const other = inquiry(); input.poll.related.push(other);
    const signals = {poll: input.poll, poll_metadata: input.poll.metadata, inquiry: other,
      inquiry_metadata: other.metadata = {}, gr_metadata: other.generate_response.metadata,
      kq_metadata: (other.knowledge_answer = {metadata: {}}).metadata,
      facts_context: other.generate_response.metadata.verified_participant_facts};
    signals[location].identity_verified = false;
    denied(input, 'identity_veto');
  });
}
test('Unmatched statuses and string booleans never provide identity evidence', () => {
  for (const status of ['ambiguous', 'not_found', 'access_error']) {
    const input = fixture(); input.poll.metadata.identity_resolution_status = status;
    denied(input, 'identity_veto');
  }
  const input = fixture(); input.poll.metadata.identity_verified = 'true';
  denied(input, 'identity_context_invalid');
});
test('A model response cannot introduce facts even when it contains a well-formed reference', () => {
  const input = fixture();
  input.poll.primary.generate_response.response.verified_participant_facts = disclosure();
  delete input.poll.primary.generate_response.metadata.verified_participant_facts;
  input.poll.metadata.verified_participant_facts = disclosure();
  denied(input, 'no_verified_facts');
});

for (const [name, edit] of [
  ['null is not zero', f => {f.value = null;}],
  ['false is not zero', f => {f.value = false;}],
  ['empty is not zero', f => {f.value = '';}],
  ['numeric string is not a number', f => {f.value = '0';}],
  ['non-finite number', f => {f.value = Infinity;}],
  ['wrong source', f => {f.source = 'participant.census.First Name';}],
  ['unknown fact', f => {f.status = 'unknown';}],
  ['missing dates', f => {delete f.observed_at; delete f.as_of;}],
  ['impossible date despite valid observation', f => {f.as_of = '2026-02-30';}],
  ['malformed observation despite valid as-of', f => {f.observed_at = 'yesterday';}],
]) {
  test('Omits affected fact and preserves independently valid fields: ' + name, () => {
    const input = fixture(); edit(factsOf(input).account_balance);
    const result = validateCanonicalEvidence(input);
    assertGuarded(result);
    assert.equal(result.evidence_status, 'partial');
    assert.ok(result.reason_codes.includes('invalid_fact'));
    assert.deepEqual(Object.keys(result.verified_participant_facts.facts), ['first_name']);
  });
}
test('Either valid as-of or observation date is sufficient when the other is absent', () => {
  for (const missing of ['as_of', 'observed_at']) {
    const input = fixture(); delete factsOf(input).account_balance[missing];
    const result = validateCanonicalEvidence(input);
    assert.equal(result.verified_participant_facts.facts.account_balance.value, 123.45);
    assert.equal(result.verified_participant_facts.facts.account_balance[missing], null);
  }
});
test('Duplicate equal facts merge; different values or dates quarantine only that field', () => {
  for (const conflict of ['none', 'value', 'date', 'source']) {
    const input = fixture(); const other = inquiry(); input.poll.related.push(other);
    const f = other.generate_response.metadata.verified_participant_facts.facts.account_balance;
    if (conflict === 'value') f.value = 456.78;
    if (conflict === 'date') f.as_of = '2026-09-14';
    if (conflict === 'source') f.source = 'WRONG-SOURCE';
    const result = validateCanonicalEvidence(input);
    if (conflict === 'none') assert.equal(result.evidence_status, 'matched');
    else {
      assert.equal(result.evidence_status, 'partial');
      assert.equal(Object.hasOwn(result.verified_participant_facts.facts, 'account_balance'), false);
      assert.equal(result.verified_participant_facts.facts.first_name.value, 'Synthetic');
      assert.ok(result.reason_codes.includes(conflict === 'source' ? 'invalid_fact' : 'fact_conflict'));
    }
  }
});
test('Equivalent observed timestamps do not create a false conflict', () => {
  const input = fixture(); const other = inquiry(); input.poll.related.push(other);
  other.generate_response.metadata.verified_participant_facts.facts.account_balance.observed_at = '2026-09-16T05:59:00-05:00';
  assert.equal(validateCanonicalEvidence(input).evidence_status, 'matched');
});
test('Does not mutate input or alias fact objects into its output', () => {
  const input = fixture(); const before = structuredClone(input);
  const result = validateCanonicalEvidence(input);
  assert.deepEqual(input, before);
  result.verified_participant_facts.facts.account_balance.value = 999;
  assert.deepEqual(input, before);
});

test('Equal digests do not certify incomplete conversation hydration', () => {
  for (const side of ['reference', 'poll']) {
    for (const change of [{complete:false}, {partial:true}, {truncated:true}]) {
      const input=fixture();
      for (const owner of [input.reference,input.poll]) Object.assign(owner.conversation_reference,
        {complete:true,partial:false,truncated:false});
      Object.assign(input[side].conversation_reference,change);
      denied(input,'conversation_binding_incomplete');
    }
  }
});
test('A source observed after job completion is not evidence available to that job', () => {
  const input=fixture();factsOf(input).account_balance.observed_at='2026-09-16T11:00:00.000001Z';
  const result=validateCanonicalEvidence(input);
  assert.equal(result.evidence_status,'partial');
  assert.equal(Object.hasOwn(result.verified_participant_facts.facts,'account_balance'),false);
  assert.ok(result.reason_codes.includes('fact_observed_after_completion'));
});
test('A failed inquiry cannot supply figures but independent successful facts survive', () => {
  for (const failure of ['error','fallback','scrape_failed','scrape_timeout']) {
    const input=fixture(), other=inquiry();
    delete factsOf(input).account_balance;
    delete other.generate_response.metadata.verified_participant_facts.facts.first_name;
    if(failure==='error')other.generate_response.metadata.error='SYNTHETIC-PRIVATE';
    if(failure==='fallback')other.generate_response.metadata.fallback=true;
    if(failure==='scrape_failed')other.scrape_status='failed';
    if(failure==='scrape_timeout')other.scrape_status='timeout';
    input.poll.related.push(other);
    const result=validateCanonicalEvidence(input);
    assert.equal(result.evidence_status,'partial');
    assert.deepEqual(Object.keys(result.verified_participant_facts.facts),['first_name']);
    assert.ok(result.reason_codes.includes('inquiry_evidence_unavailable'));
    assert.doesNotMatch(JSON.stringify(result),/SYNTHETIC-PRIVATE/);
  }
});
test('Review required and a partial scrape do not invalidate independently known fields', () => {
  const input=fixture();input.poll.primary.scrape_status='partial';
  input.poll.primary.generate_response.metadata.human_review_required=true;
  assert.equal(validateCanonicalEvidence(input).evidence_status,'matched');
});

test('Empty optional disclosure from an educational inquiry is absence, not identity conflict', () => {
  const input=fixture();input.poll.related.push({route:'knowledge_question',knowledge_answer:{metadata:{verified_participant_facts:{}}}});
  assert.equal(validateCanonicalEvidence(input).evidence_status,'matched');
});
test('Canonical envelope fallback cannot certify otherwise valid facts', () => {
  const input=fixture();input.poll.metadata.fallback=true;
  denied(input,'job_evidence_unavailable');
});
test('Missing hydration diagnostics cannot certify matching snapshot hashes', () => {
  const input=fixture();delete input.reference.conversation_reference.complete;
  denied(input,'conversation_binding_invalid');
});
test('Identity conflict still vetoes facts if the conflicting inquiry failed', () => {
  const input=fixture(),other=inquiry();other.scrape_status='failed';
  other.generate_response.metadata.identity_verified=false;input.poll.related.push(other);
  denied(input,'identity_veto');
});

test('n8n hardened object introspection still validates exact JSON evidence',()=>{
  const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
  const hardenedObject=Object.create(Object);hardenedObject.getPrototypeOf=()=>({});
  const module={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../canonical-evidence.js'),'utf8'),{module,Object:hardenedObject});
  const result=module.exports.validateCanonicalEvidence(fixture());
  assert.equal(result.evidence_status,'matched');assertGuarded(result);
});

// ---------------------------------------------------------------------------
// Plan identifier evidence: server-owned, bound to the independently polled plan
// ---------------------------------------------------------------------------

const planSource = {
  legal_plan_name: 'plan.basic_info.official_plan_name',
  rk_plan_id: 'plan.plan_design.rk_plan_id',
  record_keeper: 'plan.plan_design.record_keeper_id',
};
const planFact = (key, value) => ({status: 'known', value, source: planSource[key],
  observed_at: '2026-09-16T10:59:00Z', as_of: '2026-09-15'});
const planDisclosure = (planIdValue = '580') => ({plan_id: planIdValue,
  identity_verified: true, identity_resolution_status: 'matched',
  facts: {legal_plan_name: planFact('legal_plan_name', 'Synthetic Fixture 401(k) Plan'),
    rk_plan_id: planFact('rk_plan_id', 'RK-0000123'),
    record_keeper: planFact('record_keeper', 'LT Trust')}});
function planFixture(planIdValue = '580') {
  const input = fixture();
  input.poll.plan_id = planIdValue;
  input.poll.primary.generate_response.metadata.verified_plan_facts = planDisclosure(planIdValue);
  return input;
}
const planFactsOf = data => data.poll.primary.generate_response.metadata.verified_plan_facts.facts;

test('Projects the three bound plan identifier fields alongside participant facts', () => {
  const input = planFixture();
  planFactsOf(input).ein = {status: 'known', value: 'SYNTHETIC-PRIVATE'};
  const result = validateCanonicalEvidence(input);
  assertGuarded(result);
  assert.equal(result.evidence_status, 'matched');
  assert.deepEqual(result.reason_codes, []);
  assert.deepEqual(result.verified_plan_facts, planDisclosure());
  assert.deepEqual(result.verified_participant_facts, disclosure());
  assert.doesNotMatch(JSON.stringify(result), /SYNTHETIC-PRIVATE/);
});

test('Plan facts alone are sufficient evidence without participant figures', () => {
  const input = planFixture();
  delete input.poll.primary.generate_response.metadata.verified_participant_facts;
  const result = validateCanonicalEvidence(input);
  assert.equal(result.evidence_status, 'matched');
  assert.equal(result.verified_participant_facts, null);
  assert.deepEqual(result.verified_plan_facts, planDisclosure());
});

test('Legacy polls without plan metadata keep the seven-field contract unchanged', () => {
  const result = validateCanonicalEvidence(fixture());
  assert.equal(result.evidence_status, 'matched');
  assert.deepEqual(result.verified_participant_facts, disclosure());
  assert.equal(result.verified_plan_facts, null);
  assert.deepEqual(Object.keys(result.verified_participant_facts.facts), ['first_name', 'account_balance']);
});

test('A plan container is not evidence without a valid independently polled plan_id', () => {
  for (const bad of [undefined, null, '', '0', 'PLAN-580', 580, ' 580']) {
    const input = planFixture();
    if (bad === undefined) delete input.poll.plan_id; else input.poll.plan_id = bad;
    const result = validateCanonicalEvidence(input);
    assert.equal(result.verified_plan_facts, null);
    assert.ok(result.reason_codes.includes('plan_binding_missing'), JSON.stringify(result.reason_codes));
    assert.equal(result.verified_participant_facts.facts.first_name.value, 'Synthetic');
  }
});

test('Model metadata cannot supply the polled plan binding', () => {
  const input = planFixture();
  delete input.poll.plan_id;
  input.poll.metadata.plan_id = '580';
  input.poll.primary.generate_response.response = {plan_id: '580'};
  const result = validateCanonicalEvidence(input);
  assert.equal(result.verified_plan_facts, null);
  assert.ok(result.reason_codes.includes('plan_binding_missing'));
});

test('A cross-plan container exposes no plan facts but keeps participant facts', () => {
  const input = planFixture('580');
  input.poll.primary.generate_response.metadata.verified_plan_facts = planDisclosure('999');
  const result = validateCanonicalEvidence(input);
  assert.equal(result.evidence_status, 'partial');
  assert.equal(result.verified_plan_facts, null);
  assert.ok(result.reason_codes.includes('plan_binding_mismatch'));
  assert.deepEqual(result.verified_participant_facts, disclosure());
});

test('plan_id is not part of the closed independent reference', () => {
  const input = planFixture();
  input.reference.plan_id = '580';
  denied(input, 'invalid_reference');
});

for (const [name, edit] of [
  ['missing value', f => {delete f.value;}],
  ['numeric identifier object', f => {f.value = 123;}],
  ['blank identifier', f => {f.value = '   ';}],
  ['sentinel identifier', f => {f.value = 'unknown';}],
  ['unsafe identifier syntax', f => {f.value = 'RK <script>';}],
  ['wrong source', f => {f.source = 'plan.basic_info.official_plan_name';}],
  ['participant source', f => {f.source = 'participant.census.First Name';}],
  ['unknown status', f => {f.status = 'unknown';}],
  ['missing dates', f => {delete f.observed_at; delete f.as_of;}],
  ['impossible as-of', f => {f.as_of = '2026-02-30';}],
  ['malformed observation', f => {f.observed_at = 'yesterday';}],
]) {
  test('Quarantines only the affected plan field: ' + name, () => {
    const input = planFixture(); edit(planFactsOf(input).rk_plan_id);
    const result = validateCanonicalEvidence(input);
    assertGuarded(result);
    assert.equal(result.evidence_status, 'partial');
    assert.ok(result.reason_codes.includes('invalid_plan_fact'));
    assert.deepEqual(Object.keys(result.verified_plan_facts.facts), ['legal_plan_name', 'record_keeper']);
  });
}

test('A plan fact observed after job completion is not available to that job', () => {
  const input = planFixture();
  planFactsOf(input).rk_plan_id.observed_at = '2026-09-16T11:00:00.000001Z';
  const result = validateCanonicalEvidence(input);
  assert.equal(result.evidence_status, 'partial');
  assert.equal(Object.hasOwn(result.verified_plan_facts.facts, 'rk_plan_id'), false);
  assert.ok(result.reason_codes.includes('plan_fact_observed_after_completion'));
});

test('A plan fact as-of date starting after job completion is quarantined', () => {
  const input = planFixture();
  planFactsOf(input).rk_plan_id.observed_at = null;
  planFactsOf(input).rk_plan_id.as_of = '2099-01-01';
  const result = validateCanonicalEvidence(input);
  assert.equal(result.evidence_status, 'partial');
  assert.equal(Object.hasOwn(result.verified_plan_facts.facts, 'rk_plan_id'), false);
  assert.equal(result.verified_plan_facts.facts.legal_plan_name.value,
    'Synthetic Fixture 401(k) Plan');
  assert.equal(result.verified_participant_facts.facts.account_balance.value, 123.45);
  assert.ok(result.reason_codes.includes('plan_fact_as_of_after_completion'));
});

test('A future plan as-of date is checked even with a valid observation', () => {
  const input = planFixture();
  planFactsOf(input).rk_plan_id.as_of = '2099-01-01';
  const result = validateCanonicalEvidence(input);
  assert.equal(Object.hasOwn(result.verified_plan_facts.facts, 'rk_plan_id'), false);
  assert.ok(result.reason_codes.includes('plan_fact_as_of_after_completion'));
});

test('A plan as-of date on the UTC completion day remains allowed', () => {
  const input = planFixture();
  planFactsOf(input).rk_plan_id.observed_at = null;
  planFactsOf(input).rk_plan_id.as_of = '2026-09-16';
  const result = validateCanonicalEvidence(input);
  assert.equal(result.evidence_status, 'matched');
  assert.equal(result.verified_plan_facts.facts.rk_plan_id.value, 'RK-0000123');
});

test('An invalid leap-day plan as-of date is rejected as invalid', () => {
  const input = planFixture();
  planFactsOf(input).rk_plan_id.as_of = '2025-02-29';
  const result = validateCanonicalEvidence(input);
  assert.equal(Object.hasOwn(result.verified_plan_facts.facts, 'rk_plan_id'), false);
  assert.ok(result.reason_codes.includes('invalid_plan_fact'));
  assert.equal(result.reason_codes.includes('plan_fact_as_of_after_completion'), false);
});

test('Conflicting plan identifiers across inquiries quarantine that field only', () => {
  for (const conflict of ['none', 'value', 'date']) {
    const input = planFixture();
    const other = inquiry();
    other.generate_response.metadata.verified_plan_facts = planDisclosure();
    const f = other.generate_response.metadata.verified_plan_facts.facts.rk_plan_id;
    if (conflict === 'value') f.value = 'RK-0009999';
    if (conflict === 'date') f.as_of = '2026-09-14';
    input.poll.related.push(other);
    const result = validateCanonicalEvidence(input);
    if (conflict === 'none') assert.equal(result.evidence_status, 'matched');
    else {
      assert.equal(result.evidence_status, 'partial');
      assert.equal(Object.hasOwn(result.verified_plan_facts.facts, 'rk_plan_id'), false);
      assert.equal(result.verified_plan_facts.facts.record_keeper.value, 'LT Trust');
      assert.ok(result.reason_codes.includes('plan_fact_conflict'));
    }
  }
});

test('Identity veto in a plan container removes every class of fact', () => {
  const input = planFixture();
  input.poll.primary.generate_response.metadata.verified_plan_facts.identity_verified = false;
  const result = denied(input, 'identity_veto');
  assert.equal(result.verified_plan_facts, null);
});

test('An unmatched plan container is rejected rather than projected', () => {
  const input = planFixture();
  input.poll.primary.generate_response.metadata.verified_plan_facts.identity_resolution_status = 'pending';
  const result = denied(input, 'identity_context_invalid');
  assert.equal(result.verified_plan_facts, null);
});

test('A failed inquiry cannot supply plan identifiers', () => {
  const input = planFixture();
  input.poll.primary.scrape_status = 'failed';
  const result = validateCanonicalEvidence(input);
  assert.equal(result.evidence_status, 'unavailable');
  assert.equal(result.verified_plan_facts, null);
  assert.ok(result.reason_codes.includes('inquiry_evidence_unavailable'));
  assert.ok(result.reason_codes.includes('no_verified_facts'));
});

test('Empty optional plan disclosure is absence, not a conflicting claim', () => {
  const input = planFixture();
  input.poll.related.push({route: 'knowledge_question',
    knowledge_answer: {metadata: {verified_plan_facts: {}}}});
  assert.equal(validateCanonicalEvidence(input).evidence_status, 'matched');
});

test('A model-generated plan fact object outside metadata is never evidence', () => {
  const input = planFixture();
  input.poll.primary.generate_response.response.verified_plan_facts = planDisclosure();
  delete input.poll.primary.generate_response.metadata.verified_plan_facts;
  const result = validateCanonicalEvidence(input);
  assert.equal(result.verified_plan_facts, null);
  assert.equal(result.evidence_status, 'matched');
});

test('Neither class of facts yields no_verified_facts exactly once', () => {
  const input = planFixture();
  delete input.poll.primary.generate_response.metadata.verified_participant_facts;
  delete input.poll.primary.generate_response.metadata.verified_plan_facts;
  denied(input, 'no_verified_facts');
});
