// BD1 (plan-identifier transport) and BD4 (nested identity veto) for the GR
// consumer. Everything here runs the real PA/n8n/candidates/format-gr.js in a
// vm, exactly as consumer.test.cjs does, and compares its projection against
// PA/n8n/canonical-evidence.js, which is the authoritative shape.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {validateCanonicalEvidence} = require('../canonical-evidence.js');

const SOURCE = path.join(__dirname, '..', process.env.PA_N8N_SOURCE || 'candidates', 'format-gr.js');
const JOB = 'a'.repeat(32);
const TICKET = 'TKT-SIMULATION';
const fields = {ticketId: TICKET, caseData: {ticketData: {firstContact: false}}};
const clone = value => JSON.parse(JSON.stringify(value));

function run(input, accepted = {ticket_job_id: JOB}) {
  const source = fs.readFileSync(SOURCE, 'utf8');
  const result = vm.runInNewContext('(function(){' + source + '\n})()', {
    $input: {first: () => ({json: input})},
    $: name => {
      if (name === 'Handle Ticket') return {first: () => ({json: accepted})};
      assert.equal(name, 'Get fields1');
      return {first: () => ({json: fields})};
    },
  }, {timeout: 1000});
  const json = JSON.parse(JSON.stringify(result.json));
  const escaped = json.body.slice('```\\n\\n'.length, -'\\n\\n'.length);
  return JSON.parse(JSON.parse('"' + escaped + '"'));
}

// The shared projection prefix, loaded as it exists on disk (same technique the
// v10 contract pack uses), so the unit checks below cannot drift from the node.
function projections() {
  const text = fs.readFileSync(SOURCE, 'utf8');
  const cut = text.indexOf('// ---- inputs ---');
  assert.ok(cut > 0, 'format-gr.js no longer has its shared projection prefix');
  // eslint-disable-next-line no-new-func
  return new Function(`${text.slice(0, cut)}\nreturn {projectVerifiedPlanFacts, withPlanFacts,` +
    ` rejectsNestedAccountIdentity, rejectsAnyAccountIdentity, isPlanId};`)();
}
const consumer = projections();

const PLAN = '4821';
const planFacts = (plan = PLAN) => ({
  plan_id: plan, identity_verified: true, identity_resolution_status: 'matched',
  facts: {
    legal_plan_name: {value: 'Acme Industries 401(k) Plan', status: 'known',
      source: 'plan.basic_info.official_plan_name', observed_at: '2026-09-11T18:00:00Z', as_of: null},
    rk_plan_id: {value: 'RK-77821', status: 'known',
      source: 'plan.plan_design.rk_plan_id', observed_at: null, as_of: '2026-09-01'},
  },
});
const participantFacts = () => ({
  identity_verified: true, identity_resolution_status: 'matched',
  facts: {first_name: {value: 'Jordan', status: 'known',
    source: 'participant.census.First Name', observed_at: null, as_of: '2026-09-01'}},
});
const inquiry = () => ({inquiry: 'Which plan am I in?', topic: 'plan_information', route: 'generate_response',
  generate_response: {decision: 'can_proceed', confidence: 0.9, coverage_gaps: [],
    metadata: {human_review_required: false, requested_questions: ['Which plan am I in?'],
      question_coverage: [{question_index: 0, status: 'answered', answer_reference: 'Plan name.'}]},
    response: {outcome: 'can_proceed', response_to_participant: {opening: 'Plan name.', key_points: [], steps: [], warnings: []},
      questions_to_ask: [], escalation: {needed: false}}}});
const job = () => ({ticket_job_id: JOB, ticket_id: TICKET, state: 'succeeded',
  next_action: 'send_participant_reply', plan_id: PLAN,
  created_at: '2026-09-12T10:00:00Z', completed_at: '2026-09-12T10:05:00Z', expires_at: '2026-09-19T10:05:00Z',
  metadata: {fallback: false}, primary: inquiry(), related: [], total_inquiries_in_ticket: 1});
const grMeta = payload => payload.inquiries[0].metadata;

// --- BD1: the transport exists at all ---------------------------------------
test('BD1 the consumer transports validated plan facts with the bound plan_id', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const projected = grMeta(run(x)).verified_plan_facts;
  assert.equal(projected.plan_id, PLAN, 'the reference must carry the plan it is bound to');
  assert.equal(projected.identity_verified, true);
  assert.equal(projected.identity_resolution_status, 'matched');
  assert.deepEqual(Object.keys(projected.facts).sort(), ['legal_plan_name', 'rk_plan_id']);
  assert.deepEqual(projected.facts.rk_plan_id,
    {value: 'RK-77821', status: 'known', source: 'plan.plan_design.rk_plan_id',
      observed_at: null, as_of: '2026-09-01'});
});

test('BD1 the projection equals what canonical-evidence.js projects for the same job', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  const digest = {type: 'devrev_conversation_snapshot', schema_version: 1, hash_algorithm: 'sha256',
    digest: 'b'.repeat(64), complete: true, partial: false, truncated: false};
  const canonical = validateCanonicalEvidence({
    reference: {ticket_id: TICKET, ticket_job_id: JOB, conversation_reference: digest},
    poll: Object.assign(clone(x), {conversation_reference: digest}),
    now: '2026-09-12T11:00:00Z',
  });
  assert.equal(canonical.evidence_status, 'matched', canonical.reason_codes.join(','));
  const projected = grMeta(run(x));
  assert.deepEqual(projected.verified_plan_facts, canonical.verified_plan_facts,
    'the consumer must transport exactly the canonical plan projection');
  assert.deepEqual(projected.verified_participant_facts, canonical.verified_participant_facts);
});

test('BD1 a knowledge answer in the same job gets the same bound transport', () => {
  const x = job();
  x.primary = {inquiry: 'Who keeps the records?', topic: 'plan_information', route: 'knowledge_question',
    knowledge_answer: {answer: 'Your plan record.', key_points: [], coverage_gaps: [],
      metadata: {response_source_reason: 'account_context_required', verified_plan_facts: planFacts()}}};
  const projected = run(x).inquiries[0].knowledge_answer.metadata.verified_plan_facts;
  assert.equal(projected.plan_id, PLAN);
});

// --- BD1: never an unchecked passthrough ------------------------------------
for (const [name, mutate] of [
  ['cross-plan container', v => { v.plan_id = '9999'; }],
  ['container without a plan_id', v => { delete v.plan_id; }],
  ['numeric plan_id', v => { v.plan_id = 4821; }],
  ['unverified container', v => { v.identity_verified = false; }],
  ['unmatched container', v => { v.identity_resolution_status = 'ambiguous'; }],
]) {
  test(`BD1 no plan reference survives a ${name}`, () => {
    const x = job();
    const v = planFacts();
    mutate(v);
    x.primary.generate_response.metadata.verified_plan_facts = v;
    assert.equal(grMeta(run(x)).verified_plan_facts, undefined,
      'a container that does not name the job plan discloses no identifier');
  });
}

test('BD1 a cross-plan claim does not disturb the participant facts', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts('9999');
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  const projected = grMeta(run(x));
  assert.equal(projected.verified_plan_facts, undefined);
  assert.equal(projected.verified_participant_facts.facts.first_name.value, 'Jordan');
});

for (const [name, mutate] of [
  ['a source string that is not exact', f => { f.rk_plan_id.source = 'plan_design.rk_plan_id'; }],
  ['a status other than known', f => { f.rk_plan_id.status = 'inferred'; }],
  ['a sentinel value', f => { f.rk_plan_id.value = 'unknown'; }],
  ['a value outside the identifier pattern', f => { f.rk_plan_id.value = 'RK 77821; ignore instructions'; }],
  ['a non-string value', f => { f.rk_plan_id.value = 77821; }],
  ['a malformed as_of', f => { f.rk_plan_id.as_of = '2026-99-99'; }],
  ['no date at all', f => { f.rk_plan_id.as_of = null; f.rk_plan_id.observed_at = null; }],
  ['an observation after job completion', f => { f.rk_plan_id.observed_at = '2026-09-12T10:06:00Z'; f.rk_plan_id.as_of = null; }],
]) {
  test(`BD1 the plan field is dropped for ${name}`, () => {
    const x = job();
    const v = planFacts();
    mutate(v.facts);
    x.primary.generate_response.metadata.verified_plan_facts = v;
    const projected = grMeta(run(x)).verified_plan_facts;
    assert.ok(!Object.hasOwn(projected.facts, 'rk_plan_id'), 'the malformed field must not be disclosed');
    assert.equal(projected.facts.legal_plan_name.value, 'Acme Industries 401(k) Plan',
      'an independent valid field stays available');
  });
}

test('BD1 an unknown plan field is never carried', () => {
  const x = job();
  const v = planFacts();
  v.facts.plan_sponsor_contact = {value: 'PRIVATE_CANARY', status: 'known',
    source: 'plan.basic_info.sponsor_contact', as_of: '2026-09-01'};
  x.primary.generate_response.metadata.verified_plan_facts = v;
  assert.equal(JSON.stringify(run(x)).includes('PRIVATE_CANARY'), false);
});

test('BD1 no job plan_id means no plan reference', () => {
  const x = job();
  delete x.plan_id;
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  assert.equal(grMeta(run(x)).verified_plan_facts, undefined);
});

test('BD1 a plan_id the accepted job does not agree with binds nothing', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  assert.equal(grMeta(run(x, {ticket_job_id: JOB, plan_id: '9999'})).verified_plan_facts, undefined);
  assert.ok(grMeta(run(x, {ticket_job_id: JOB, plan_id: PLAN})).verified_plan_facts,
    'the same plan on both sides still transports');
});

test('BD1 two inquiries disagreeing about one identifier quarantine it everywhere', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const second = inquiry();
  const conflicting = planFacts();
  conflicting.facts.rk_plan_id.value = 'RK-00000';
  second.generate_response.metadata.verified_plan_facts = conflicting;
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run(x);
  for (const index of [0, 1]) {
    const facts = payload.inquiries[index].metadata.verified_plan_facts.facts;
    assert.ok(!Object.hasOwn(facts, 'rk_plan_id'), 'a contested identifier is withheld from every inquiry');
    assert.equal(facts.legal_plan_name.value, 'Acme Industries 401(k) Plan');
  }
});

// --- BD4: a nested-only negative identity result fails closed ---------------
const nestedNegative = () => ({identity_resolution_status: 'not_found',
  response_source_reason: 'account_not_found', identity_verified: false,
  provided_identifiers: ['name', 'employer']});

for (const level of ['job', 'job_metadata', 'inquiry', 'gr_metadata']) {
  test(`BD4 a nested-only negative identity vetoes stale positive facts: ${level}`, () => {
    const x = job();
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    const wrapper = {identity_context: nestedNegative()};
    if (level === 'job') Object.assign(x, wrapper);
    if (level === 'job_metadata') Object.assign(x.metadata, wrapper);
    if (level === 'inquiry') Object.assign(x.primary, wrapper);
    if (level === 'gr_metadata') Object.assign(x.primary.generate_response.metadata, wrapper);
    const payload = run(x);
    assert.equal(grMeta(payload).verified_participant_facts, undefined,
      'a nested negative identity must withdraw the stale positive participant reference');
    assert.equal(grMeta(payload).verified_plan_facts, undefined,
      'a nested negative identity must withdraw the plan disclosure too');
    assert.equal(payload.human_review_required, true);
    assert.equal(payload.participant_reply_safe, false);
    assert.equal(payload.next_action, 'human_review');
    assert.equal(JSON.stringify(payload).includes('identity_context'), false,
      'the wrapper itself still never reaches the model');
  });
}

test('BD4 a nested-only negative on a related inquiry vetoes the primary reference', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  const second = inquiry();
  second.generate_response.metadata.identity_context = nestedNegative();
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  assert.equal(grMeta(run(x)).verified_participant_facts, undefined);
});

test('BD4 a nested POSITIVE identity alone authorizes nothing', () => {
  const x = job();
  x.primary.generate_response.metadata.identity_context = {
    identity_verified: true, identity_resolution_status: 'matched'};
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  // The nested positive is not promoted, and it is not a veto either: the facts
  // stand or fall on their own source-bound validation, never on the wrapper.
  const projected = grMeta(run(x));
  assert.equal(projected.identity_verified, undefined, 'a nested positive is never promoted to a flat flag');
  assert.equal(projected.identity_resolution_status, undefined);
  assert.equal(consumer.rejectsNestedAccountIdentity(
    {identity_context: {identity_verified: true, identity_resolution_status: 'matched'}}), false);
});

test('BD4 the legitimate flat-positive path still discloses', () => {
  const x = job();
  x.primary.generate_response.metadata.identity_verified = true;
  x.primary.generate_response.metadata.identity_resolution_status = 'matched';
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const payload = run(x);
  assert.equal(grMeta(payload).verified_participant_facts.facts.first_name.value, 'Jordan');
  assert.equal(grMeta(payload).verified_plan_facts.facts.rk_plan_id.value, 'RK-77821');
  assert.equal(payload.participant_reply_safe, true);
});

test('BD4 a flat negative still vetoes the plan disclosure', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  x.primary.generate_response.metadata.identity_resolution_status = 'not_found';
  const payload = run(x);
  assert.equal(grMeta(payload).verified_plan_facts, undefined);
  assert.equal(payload.human_review_required, true);
});

test('BD4 an educational answer keeps its purpose despite an unresolved account', () => {
  const x = job();
  x.metadata.identity_context = nestedNegative();
  x.primary = {inquiry: 'What is a rollover?', route: 'knowledge_question',
    knowledge_answer: {answer: 'A general explanation.', key_points: [],
      metadata: {response_source_reason: 'general_knowledge', verified_plan_facts: planFacts()}}};
  const payload = run(x);
  assert.equal(payload.participant_reply_safe, true,
    'a general explanation must not acquire an account-verification prerequisite');
  assert.equal(payload.inquiries[0].knowledge_answer.metadata.verified_plan_facts, undefined,
    'but it still discloses no plan identifier under a negative identity result');
});

// --- direct unit checks on the projection ----------------------------------
test('projectVerifiedPlanFacts refuses to bind without a caller-supplied plan', () => {
  const completed = Date.parse('2026-09-12T10:05:00Z');
  assert.equal(consumer.projectVerifiedPlanFacts(planFacts(), null, completed), null);
  // CD3: the completion instant is now part of binding, so the old two-argument
  // call fails closed. It is not an optional comparison any more.
  assert.equal(consumer.projectVerifiedPlanFacts(planFacts(), PLAN), null);
  assert.equal(consumer.projectVerifiedPlanFacts(planFacts(), PLAN, completed).plan_id, PLAN);
  assert.equal(consumer.isPlanId('0123'), false);
  assert.equal(consumer.isPlanId(PLAN), true);
});

test('withPlanFacts attaches nothing while an identity veto is in force', () => {
  const completed = Date.parse('2026-09-12T10:05:00Z');
  const metadata = {verified_plan_facts: planFacts(), verified_participant_facts: participantFacts()};
  // A real completion instant is supplied, so the identity veto is the ONLY
  // reason nothing is attached; CD4 below covers the missing-instant reason.
  const vetoed = consumer.withPlanFacts(clone(metadata), true, PLAN, completed, new Set());
  assert.equal(vetoed.verified_plan_facts, undefined);
  assert.equal(vetoed.verified_participant_facts, undefined);
  const allowed = consumer.withPlanFacts(clone(metadata), false, PLAN, completed, new Set());
  assert.equal(allowed.verified_plan_facts.plan_id, PLAN);
  assert.equal(allowed.verified_participant_facts.facts.first_name.value, 'Jordan');
});

test('CD4 withPlanFacts attaches no participant facts without a completion instant', () => {
  const metadata = {verified_plan_facts: planFacts(), verified_participant_facts: participantFacts()};
  for (const completed of [null, undefined]) {
    const out = consumer.withPlanFacts(clone(metadata), false, PLAN, completed, new Set());
    assert.equal(out.verified_participant_facts, undefined,
      'a participant figure is no longer disclosable without a bound completion instant');
    assert.equal(out.verified_plan_facts, undefined, 'and neither is a plan identifier (CD3)');
  }
});

// --- CD6: the TOP-LEVEL plan binding the AGENT-3 contract reads -------------
// v11 refuses every plan identifier unless `D.plan_id` exists and equals the
// reference's own plan_id. These checks pin WHERE that top-level value may come
// from: the job/poll correlation this run performed, and nothing else.
test('CD6 the top-level binding equals the plan the source job reports', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const payload = run(x);
  assert.equal(payload.plan_id, PLAN, 'the payload must carry the bound plan identifier');
  assert.equal(payload.plan_id, x.plan_id, 'and it must be the source job plan, not a derived value');
  assert.equal(grMeta(payload).verified_plan_facts.plan_id, payload.plan_id,
    'the reference the draft may cite must be bound to the same top-level value');
});

test('CD6 the binding is absent, never null or empty, when it is not eligible', () => {
  const x = job();
  delete x.plan_id;
  const payload = run(x);
  assert.equal(Object.hasOwn(payload, 'plan_id'), false,
    'an ineligible binding is omitted so the contract reads it as missing');
});

test('CD6 no forged metadata fallback: a reference plan_id never binds itself', () => {
  // The ONLY plan_id in this job lives inside the model-controlled reference.
  const x = job();
  delete x.plan_id;
  x.metadata.verified_plan_facts = planFacts();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  x.primary.generate_response.metadata.plan_id = PLAN;
  const payload = run(x);
  assert.equal(Object.hasOwn(payload, 'plan_id'), false,
    'metadata.verified_plan_facts.plan_id must never be promoted to the top-level binding');
  assert.equal(grMeta(payload).verified_plan_facts, undefined,
    'and the unbound reference itself is still withheld');
});

test('CD6 an accepted response naming another plan binds nothing at the top level', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const conflicting = run(x, {ticket_job_id: JOB, plan_id: '9999'});
  assert.equal(Object.hasOwn(conflicting, 'plan_id'), false,
    'a cross-plan disagreement discloses no binding at all');
  const agreeing = run(x, {ticket_job_id: JOB, plan_id: PLAN});
  assert.equal(agreeing.plan_id, PLAN);
});

test('CD6 no completion instant means no top-level binding', () => {
  for (const completed of [null, '', 'yesterday', '2026-99-99T10:05:00Z']) {
    const x = job();
    x.completed_at = completed;
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    const payload = run(x);
    assert.equal(Object.hasOwn(payload, 'plan_id'), false,
      `completed_at ${JSON.stringify(completed)} must not authorize a binding`);
    assert.equal(grMeta(payload).verified_plan_facts, undefined,
      'and the reference stays withheld for the same reason (CD3)');
  }
});

test('CD6 a job plan_id outside the identifier shape binds nothing', () => {
  for (const bad of ['0123', 4821, '', ' 4821', 'RK-77821', null]) {
    const x = job();
    x.plan_id = bad;
    const payload = run(x);
    assert.equal(Object.hasOwn(payload, 'plan_id'), false,
      `plan_id ${JSON.stringify(bad)} is not a bindable identifier`);
  }
});

test('CD6 an identity veto withholds the top-level binding too', () => {
  const vetoes = [
    ['job', x => { x.identity_verified = false; }],
    ['job_metadata', x => { x.metadata.identity_resolution_status = 'not_found'; }],
    ['inquiry_metadata', x => { x.primary.generate_response.metadata.identity_verified = false; }],
    ['nested_wrapper', x => {
      x.primary.generate_response.metadata.identity_context = {identity_verified: false,
        identity_resolution_status: 'not_found', response_source_reason: 'account_not_found'};
    }],
  ];
  for (const [where, veto] of vetoes) {
    const x = job();
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    veto(x);
    const payload = run(x);
    assert.equal(Object.hasOwn(payload, 'plan_id'), false,
      `a negative identity signal on ${where} must withhold the binding`);
    assert.equal(grMeta(payload).verified_plan_facts, undefined,
      'the reference is vetoed for the same reason');
  }
});

test('CD6 an inline KQ response carries no job binding', () => {
  const inline = {primary: {inquiry: 'What is vesting?', topic: 'vesting', route: 'knowledge_question',
    knowledge_answer: {answer: 'A general explanation.', key_points: [], coverage_gaps: [],
      metadata: {response_source_reason: 'general_knowledge'}}}, plan_id: PLAN};
  const payload = run(clone(inline), clone(inline));
  assert.equal(payload.execution_reference.source, 'handle_ticket_inline');
  assert.equal(Object.hasOwn(payload, 'plan_id'), false,
    'the inline path has no correlated poll body and therefore no completion instant');
});
