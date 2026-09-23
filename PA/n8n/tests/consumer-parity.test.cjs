// Root decisions CD1 (KQ nested-identity veto), CD2 (cross-inquiry participant
// quarantine) and CD3 (plan disclosures fail closed without a job completion
// instant), plus an independent canonical-parity check over realistic job
// wrappers.
//
// Everything here runs the REAL PA/n8n/candidates/format-gr.js and
// PA/n8n/candidates/format-kq.js in a vm, exactly as consumer.test.cjs does,
// and compares against PA/n8n/canonical-evidence.js, which is authoritative.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {validateCanonicalEvidence} = require('../canonical-evidence.js');

const sourceDir = path.resolve(__dirname, '..', process.env.PA_N8N_SOURCE || 'candidates');
const JOB = 'a'.repeat(32);
const TICKET = 'TKT-SIMULATION';
const PLAN = '4821';
const COMPLETED = '2026-09-12T10:05:00Z';
const fields = {ticketId: TICKET, caseData: {ticketData: {firstContact: false}}};
const clone = value => JSON.parse(JSON.stringify(value));

function run(name, input, accepted = {ticket_job_id: JOB}) {
  const source = fs.readFileSync(path.join(sourceDir, name + '.js'), 'utf8');
  const result = vm.runInNewContext('(function(){' + source + '\n})()', {
    $input: {first: () => ({json: input})},
    $: node => {
      if (node === 'Handle Ticket') return {first: () => ({json: accepted})};
      assert.equal(node, 'Get fields1');
      return {first: () => ({json: fields})};
    },
  }, {timeout: 1000});
  const json = JSON.parse(JSON.stringify(result.json));
  const escaped = json.body.slice('```\\n\\n'.length, -'\\n\\n'.length);
  return JSON.parse(JSON.parse('"' + escaped + '"'));
}

// ---------------------------------------------------------------- fixtures
const participantFacts = (balance = 500) => ({
  identity_verified: true, identity_resolution_status: 'matched',
  facts: {
    first_name: {value: 'Jordan', status: 'known',
      source: 'participant.census.First Name', observed_at: null, as_of: '2026-09-01'},
    account_balance: {value: balance, status: 'known',
      source: 'participant.savings_rate.Account Balance',
      observed_at: '2026-09-11T18:00:00Z', as_of: null},
  },
});
const planFacts = (plan = PLAN) => ({
  plan_id: plan, identity_verified: true, identity_resolution_status: 'matched',
  facts: {
    legal_plan_name: {value: 'Acme Industries 401(k) Plan', status: 'known',
      source: 'plan.basic_info.official_plan_name', observed_at: '2026-09-11T18:00:00Z', as_of: null},
    rk_plan_id: {value: 'RK-77821', status: 'known',
      source: 'plan.plan_design.rk_plan_id', observed_at: null, as_of: '2026-09-01'},
  },
});
const inquiry = () => ({inquiry: 'What is my balance?', topic: 'account_balance', route: 'generate_response',
  generate_response: {decision: 'can_proceed', confidence: 0.9, coverage_gaps: [],
    metadata: {human_review_required: false, requested_questions: ['What is my balance?'],
      question_coverage: [{question_index: 0, status: 'answered', answer_reference: 'Balance.'}]},
    response: {outcome: 'can_proceed',
      response_to_participant: {opening: 'Balance.', key_points: [], steps: [], warnings: []},
      questions_to_ask: [], escalation: {needed: false}}}});
const job = () => ({ticket_job_id: JOB, ticket_id: TICKET, state: 'succeeded',
  next_action: 'send_participant_reply', plan_id: PLAN,
  created_at: '2026-09-12T10:00:00Z', completed_at: COMPLETED, expires_at: '2026-09-19T10:05:00Z',
  metadata: {fallback: false}, primary: inquiry(), related: [], total_inquiries_in_ticket: 1});
const grMeta = (payload, index = 0) => payload.inquiries[index].metadata;
const nestedNegative = () => ({identity_resolution_status: 'not_found',
  response_source_reason: 'account_not_found', identity_verified: false,
  provided_identifiers: ['name', 'employer']});

// ------------------------------------------------- CD1: the KQ nested veto
test('CD1 a nested-only negative identity withdraws the stale KQ reference', () => {
  const payload = run('format-kq', {answer: 'Your balance is on file.', key_points: [],
    metadata: {response_source_reason: 'account_context_required', identity_verified: true,
      identity_resolution_status: 'matched', identity_context: nestedNegative(),
      verified_participant_facts: participantFacts()}});
  assert.equal(payload.metadata.verified_participant_facts, undefined,
    'a nested negative identity must withdraw the stale positive participant reference');
  assert.equal(payload.human_review_required, true);
  assert.equal(payload.participant_reply_safe, false);
  assert.equal(payload.next_action, 'human_review');
  assert.equal(JSON.stringify(payload).includes('identity_context'), false,
    'the wrapper itself still never reaches the model');
});

test('CD1 the nested veto also applies one level up, on the response itself', () => {
  const payload = run('format-kq', {answer: 'Your balance is on file.', key_points: [],
    identity_context: nestedNegative(),
    metadata: {response_source_reason: 'account_context_required',
      verified_participant_facts: participantFacts()}});
  assert.equal(payload.metadata.verified_participant_facts, undefined);
});

test('CD1 a nested negative beats a flat positive in the same metadata', () => {
  const payload = run('format-kq', {answer: 'Your balance is on file.', key_points: [],
    metadata: {response_source_reason: 'account_context_required',
      identity_verified: true, identity_resolution_status: 'matched',
      identity_context: {identity_verified: false},
      verified_participant_facts: participantFacts()}});
  assert.equal(payload.metadata.verified_participant_facts, undefined,
    'the contradiction must fail closed, not resolve to the flat positive');
});

test('CD1 a nested POSITIVE identity alone authorizes nothing on the KQ path', () => {
  const positive = {identity_verified: true, identity_resolution_status: 'matched'};
  const payload = run('format-kq', {answer: 'A general explanation.', key_points: [],
    metadata: {response_source_reason: 'account_context_required', identity_context: positive}});
  assert.equal(payload.metadata.identity_verified, undefined,
    'a nested positive is never promoted to a flat verified identity');
  assert.equal(payload.metadata.identity_resolution_status, undefined);
  assert.equal(JSON.stringify(payload).includes('identity_context'), false,
    'the wrapper itself still never reaches the model');
  // And it cannot rehabilitate a result the flat signal already rejected.
  const vetoed = run('format-kq', {answer: 'Your balance is on file.', key_points: [],
    metadata: {response_source_reason: 'account_not_found', identity_resolution_status: 'not_found',
      identity_context: positive, verified_participant_facts: participantFacts()}});
  assert.equal(vetoed.metadata.verified_participant_facts, undefined,
    'a nested positive never overrides a negative identity result');
  assert.equal(vetoed.participant_reply_safe, false);
});

test('CD1 the legitimate flat-positive KQ path still discloses', () => {
  const payload = run('format-kq', {answer: 'Here is the figure.', key_points: [],
    metadata: {response_source_reason: 'general_knowledge', identity_verified: true,
      identity_resolution_status: 'matched', verified_participant_facts: participantFacts()}});
  assert.equal(payload.metadata.verified_participant_facts.facts.account_balance.value, 500);
  assert.equal(payload.participant_reply_safe, true);
});

test('CD1 an educational KQ answer keeps its purpose under a nested negative', () => {
  const payload = run('format-kq', {answer: 'A general explanation.', key_points: [],
    metadata: {response_source_reason: 'general_knowledge',
      identity_context: nestedNegative(), verified_participant_facts: participantFacts()}});
  assert.equal(payload.participant_reply_safe, true,
    'a general explanation must not acquire an account-verification prerequisite');
  assert.equal(payload.next_action, 'send_participant_reply');
  assert.equal(payload.metadata.response_source_reason, 'general_knowledge');
  assert.equal(payload.metadata.verified_participant_facts, undefined,
    'but it still discloses no personal figure under a negative identity result');
});

test('CD1 GR and KQ now agree about the same nested negative', () => {
  const nested = {identity_context: nestedNegative()};
  const kq = run('format-kq', {answer: 'Your balance is on file.', key_points: [],
    metadata: Object.assign({response_source_reason: 'account_context_required',
      verified_participant_facts: participantFacts()}, nested)});
  const x = job();
  x.primary = {inquiry: 'What is my balance?', route: 'knowledge_question',
    knowledge_answer: {answer: 'Your balance is on file.', key_points: [],
      metadata: Object.assign({response_source_reason: 'account_context_required',
        verified_participant_facts: participantFacts()}, nested)}};
  const gr = run('format-gr', x);
  assert.equal(kq.metadata.verified_participant_facts, undefined);
  assert.equal(gr.inquiries[0].knowledge_answer.metadata.verified_participant_facts, undefined);
  assert.equal(kq.human_review_required, gr.human_review_required);
});

// --------------------------- CD2: cross-inquiry participant fact quarantine
test('CD2 two inquiries disagreeing about one participant figure quarantine it everywhere', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const second = inquiry();
  second.generate_response.metadata.verified_participant_facts = participantFacts(999);
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run('format-gr', x);
  for (const index of [0, 1]) {
    const facts = grMeta(payload, index).verified_participant_facts.facts;
    assert.ok(!Object.hasOwn(facts, 'account_balance'),
      'a contested figure is withheld from every inquiry');
    assert.equal(facts.first_name.value, 'Jordan',
      'an unrelated valid field in the same container is preserved');
  }
});

test('CD2 the same figure repeated consistently is NOT quarantined', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const second = inquiry();
  second.generate_response.metadata.verified_participant_facts = participantFacts(500);
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run('format-gr', x);
  for (const index of [0, 1]) {
    assert.equal(grMeta(payload, index).verified_participant_facts.facts.account_balance.value, 500);
  }
});

test('CD2 an invalid occurrence quarantines the field for the whole job', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const second = inquiry();
  const broken = participantFacts(500);
  broken.facts.account_balance.source = 'participant.savings_rate.Other Balance';
  second.generate_response.metadata.verified_participant_facts = broken;
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run('format-gr', x);
  assert.ok(!Object.hasOwn(grMeta(payload, 0).verified_participant_facts.facts, 'account_balance'),
    'the inquiry that carried a valid copy must not disclose a field another inquiry broke');
  assert.equal(grMeta(payload, 0).verified_participant_facts.facts.first_name.value, 'Jordan');
});

test('CD2 a figure observed after job completion is quarantined everywhere', () => {
  const x = job();
  const late = participantFacts(500);
  late.facts.account_balance.observed_at = '2026-09-12T10:06:00Z';
  x.primary.generate_response.metadata.verified_participant_facts = late;
  const second = inquiry();
  second.generate_response.metadata.verified_participant_facts = participantFacts(500);
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run('format-gr', x);
  for (const index of [0, 1]) {
    assert.ok(!Object.hasOwn(grMeta(payload, index).verified_participant_facts.facts, 'account_balance'),
      'an observation that postdates the job cannot survive in any inquiry');
  }
});

test('CD2 quarantining every field drops the reference rather than emitting an empty one', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const second = inquiry();
  const other = participantFacts(999);
  other.facts.first_name.value = 'Alex';
  second.generate_response.metadata.verified_participant_facts = other;
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run('format-gr', x);
  for (const index of [0, 1]) {
    assert.equal(grMeta(payload, index).verified_participant_facts, undefined);
  }
});

test('CD2 a conflicting participant figure does not disturb the plan identifiers', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const second = inquiry();
  second.generate_response.metadata.verified_participant_facts = participantFacts(999);
  second.generate_response.metadata.verified_plan_facts = planFacts();
  x.related = [second];
  x.total_inquiries_in_ticket = 2;
  const payload = run('format-gr', x);
  assert.ok(!Object.hasOwn(grMeta(payload).verified_participant_facts.facts, 'account_balance'));
  assert.equal(grMeta(payload).verified_plan_facts.facts.rk_plan_id.value, 'RK-77821',
    'plan and participant facts are quarantined independently');
});

test('CD2 a single inquiry with one valid container still discloses', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const facts = grMeta(run('format-gr', x)).verified_participant_facts.facts;
  assert.equal(facts.account_balance.value, 500);
  assert.equal(facts.first_name.value, 'Jordan');
});

// --------------------------------------- CD3: plan disclosure fails closed
test('CD3 a job without completed_at discloses no plan identifier', () => {
  const x = job();
  delete x.completed_at;
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  assert.equal(grMeta(run('format-gr', x)).verified_plan_facts, undefined,
    'without an authoritative completion instant the date-bound check cannot run, so it fails closed');
});

for (const [name, value] of [
  ['null', null],
  ['empty', ''],
  ['date only', '2026-09-12'],
  ['unparseable', 'yesterday'],
  ['numeric epoch', 1789200000000],
  ['no offset', '2026-09-12T10:05:00'],
]) {
  test(`CD3 a malformed completion instant is not a completion instant: ${name}`, () => {
    const x = job();
    x.completed_at = value;
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    assert.equal(grMeta(run('format-gr', x)).verified_plan_facts, undefined);
  });
}

test('CD3 the completion instant is never taken from model output', () => {
  const x = job();
  delete x.completed_at;
  // The model controls its own metadata; it must not be able to supply the
  // instant that authorizes a plan disclosure.
  x.metadata.completed_at = COMPLETED;
  x.primary.generate_response.metadata.completed_at = COMPLETED;
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  assert.equal(grMeta(run('format-gr', x)).verified_plan_facts, undefined);
});

test('CD3 the plan binding itself is never taken from model output', () => {
  const x = job();
  delete x.plan_id;
  x.metadata.plan_id = PLAN;
  x.primary.generate_response.metadata.plan_id = PLAN;
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  assert.equal(grMeta(run('format-gr', x)).verified_plan_facts, undefined);
});

test('CD3 the inline (non-poll) path has no job completion instant and discloses no plan', () => {
  const inline = {primary: {inquiry: 'Which plan am I in?', route: 'knowledge_question',
    plan_id: PLAN,
    knowledge_answer: {answer: 'Your plan.', key_points: [],
      metadata: {response_source_reason: 'general_knowledge', verified_plan_facts: planFacts()}}}};
  const payload = run('format-gr', clone(inline), clone(inline));
  assert.equal(payload.inquiries[0].knowledge_answer.metadata.verified_plan_facts, undefined,
    'the inline path carries no completion instant, so a plan identifier fails closed');
});

test('CD3 a completed job still discloses, so the rule is not a blanket refusal', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  assert.equal(grMeta(run('format-gr', x)).verified_plan_facts.facts.rk_plan_id.value, 'RK-77821');
});

test('CD3 a plan fact observed after completion is still refused', () => {
  const x = job();
  const late = planFacts();
  late.facts.legal_plan_name.observed_at = '2026-09-12T10:06:00Z';
  x.primary.generate_response.metadata.verified_plan_facts = late;
  const facts = grMeta(run('format-gr', x)).verified_plan_facts.facts;
  assert.ok(!Object.hasOwn(facts, 'legal_plan_name'));
  assert.equal(facts.rk_plan_id.value, 'RK-77821');
});

// ------------------ CD4: participant disclosure fails closed the same way
// CD4 is the participant-side counterpart of CD3, and it matches
// canonical-evidence.js, which refuses the whole poll when the job carries no
// usable completion instant. The consumer withholds the reference only; it
// adds no review requirement of its own.
test('CD4 a job without completed_at discloses no participant figure', () => {
  const x = job();
  delete x.completed_at;
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  assert.equal(grMeta(run('format-gr', x)).verified_participant_facts, undefined,
    'with no instant to bound observed_at against, a backdated figure is undetectable, so none is disclosed');
});

for (const [name, value] of [
  ['null', null],
  ['empty', ''],
  ['date only', '2026-09-12'],
  ['unparseable', 'yesterday'],
  ['numeric epoch', 1789200000000],
  ['no offset', '2026-09-12T10:05:00'],
]) {
  test(`CD4 a malformed completion instant discloses no participant figure: ${name}`, () => {
    const x = job();
    x.completed_at = value;
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
    assert.equal(grMeta(run('format-gr', x)).verified_participant_facts, undefined);
  });
}

test('CD4 the completion instant is never taken from model output', () => {
  const x = job();
  delete x.completed_at;
  // The model controls its own metadata; it must not be able to supply the
  // instant that authorizes a personal figure.
  x.metadata.completed_at = COMPLETED;
  x.primary.generate_response.metadata.completed_at = COMPLETED;
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  const payload = run('format-gr', x);
  assert.equal(grMeta(payload).verified_participant_facts, undefined);
  assert.equal(JSON.stringify(payload).includes('500'), false);
});

test('CD4 the inline (non-poll) path discloses no participant figure', () => {
  const inline = {primary: {inquiry: 'What is my balance?', route: 'knowledge_question',
    knowledge_answer: {answer: 'Your balance.', key_points: [],
      metadata: {response_source_reason: 'account_context_required',
        verified_participant_facts: participantFacts()}}}};
  const payload = run('format-gr', clone(inline), clone(inline));
  assert.equal(payload.inquiries[0].knowledge_answer.metadata.verified_participant_facts, undefined,
    'an inline response carries no correlated job completion, so it authorizes no personal figure');
});

test('CD4 does not widen the standalone direct-KQ contract', () => {
  // format-kq.js answers a single question with no ticket-job wrapper at all,
  // so there is no correlated job whose completion could be read. Inventing or
  // guessing one there would be a fabricated authorization, so that contract is
  // deliberately left alone: it still discloses under its own identity rules.
  const payload = run('format-kq', {answer: 'Your balance is on file.', key_points: [],
    metadata: {response_source_reason: 'account_context_required', identity_verified: true,
      identity_resolution_status: 'matched', verified_participant_facts: participantFacts()}});
  assert.equal(payload.metadata.verified_participant_facts.facts.account_balance.value, 500);
});

test('CD4 a completed job still discloses, so the rule is not a blanket refusal', () => {
  const x = job();
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts();
  assert.equal(grMeta(run('format-gr', x)).verified_participant_facts.facts.account_balance.value, 500);
});

// ------------------- CD5: the payload's own top level is a fact container
// canonical-evidence.js reads fact containers only from inquiry metadata and
// ignores poll.metadata. This consumer DOES project data.metadata into the
// payload's top-level metadata, so it also scans that level for conflicts.
// The divergence is one-directional: the consumer can only withhold MORE than
// canonical, never disclose something canonical refused.
test('CD5 a conflicting top-level figure cannot leak into the inquiries', () => {
  const x = job();
  x.metadata.verified_participant_facts = participantFacts(999);
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const payload = run('format-gr', x);
  assert.ok(!Object.hasOwn(grMeta(payload).verified_participant_facts.facts, 'account_balance'),
    'a figure the payload top level contradicts is withheld from the inquiries too');
  assert.ok(!Object.hasOwn(payload.metadata.verified_participant_facts.facts, 'account_balance'),
    'and from the top level itself, so the payload never disagrees with itself');
  assert.equal(JSON.stringify(payload).includes('999'), false);
  assert.equal(JSON.stringify(payload).includes('500'), false);
  assert.equal(grMeta(payload).verified_participant_facts.facts.first_name.value, 'Jordan',
    'an unrelated valid field in the same container is untouched');
});

test('CD5 an invalid top-level occurrence quarantines the inquiry copy', () => {
  const x = job();
  const broken = participantFacts(500);
  broken.facts.account_balance.source = 'ticket.message';
  x.metadata.verified_participant_facts = broken;
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  assert.ok(!Object.hasOwn(grMeta(run('format-gr', x)).verified_participant_facts.facts, 'account_balance'),
    'a badly sourced copy at the top level still fails the field closed everywhere');
});

test('CD5 a top-level conflict quarantines plan identifiers the same way', () => {
  const x = job();
  const conflicting = planFacts();
  conflicting.facts.rk_plan_id.value = 'RK-00000';
  x.metadata.verified_plan_facts = conflicting;
  x.primary.generate_response.metadata.verified_plan_facts = planFacts();
  const facts = grMeta(run('format-gr', x)).verified_plan_facts.facts;
  assert.ok(!Object.hasOwn(facts, 'rk_plan_id'));
  assert.equal(facts.legal_plan_name.value, 'Acme Industries 401(k) Plan');
});

test('CD5 the extra top-level scan is stricter than canonical, never looser', () => {
  // canonicalFor is declared below; node:test runs after the module is loaded.
  const x = job();
  x.metadata.verified_participant_facts = participantFacts(999);
  x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
  const canonical = canonicalFor(x);
  assert.equal(canonical.verified_participant_facts.facts.account_balance.value, 500,
    'canonical ignores poll.metadata, so it would still disclose the inquiry copy');
  assert.equal(grMeta(run('format-gr', x)).verified_participant_facts.facts.account_balance, undefined,
    'the consumer withholds it instead: the only divergence is in the safe direction');
});

// -------------------------- independent canonical parity, realistic wrappers
const digest = () => ({type: 'devrev_conversation_snapshot', schema_version: 1,
  hash_algorithm: 'sha256', digest: 'b'.repeat(64), complete: true, partial: false, truncated: false});

function canonicalFor(poll) {
  const reference = digest();
  return validateCanonicalEvidence({
    reference: {ticket_id: TICKET, ticket_job_id: JOB, conversation_reference: reference},
    poll: Object.assign(clone(poll), {conversation_reference: reference}),
    now: '2026-09-12T11:00:00Z',
  });
}

// Each case is a realistic ticket-job wrapper, not a bare fact container.
const parityCases = {
  'one inquiry, participant and plan facts': () => {
    const x = job();
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    return x;
  },
  'two inquiries agreeing about both kinds of fact': () => {
    const x = job();
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    const second = inquiry();
    second.generate_response.metadata.verified_participant_facts = participantFacts(500);
    second.generate_response.metadata.verified_plan_facts = planFacts();
    x.related = [second];
    x.total_inquiries_in_ticket = 2;
    return x;
  },
  'two inquiries disagreeing about one participant figure': () => {
    const x = job();
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
    const second = inquiry();
    second.generate_response.metadata.verified_participant_facts = participantFacts(999);
    x.related = [second];
    x.total_inquiries_in_ticket = 2;
    return x;
  },
  'a participant figure broken in one inquiry only': () => {
    const x = job();
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
    const second = inquiry();
    const broken = participantFacts(500);
    broken.facts.account_balance.status = 'unknown';
    second.generate_response.metadata.verified_participant_facts = broken;
    x.related = [second];
    x.total_inquiries_in_ticket = 2;
    return x;
  },
  'a participant figure observed after completion': () => {
    const x = job();
    const late = participantFacts(500);
    late.facts.account_balance.observed_at = '2026-09-12T10:06:00Z';
    x.primary.generate_response.metadata.verified_participant_facts = late;
    return x;
  },
  'a knowledge answer carrying both kinds of fact': () => {
    const x = job();
    x.primary = {inquiry: 'Who keeps the records?', topic: 'plan_information', route: 'knowledge_question',
      knowledge_answer: {answer: 'Your plan record.', key_points: [], coverage_gaps: [],
        metadata: {response_source_reason: 'account_context_required',
          verified_participant_facts: participantFacts(500), verified_plan_facts: planFacts()}}};
    return x;
  },
  'a cross-plan container beside valid participant facts': () => {
    const x = job();
    x.primary.generate_response.metadata.verified_participant_facts = participantFacts(500);
    x.primary.generate_response.metadata.verified_plan_facts = planFacts('9999');
    return x;
  },
  'two inquiries disagreeing about one plan identifier': () => {
    const x = job();
    x.primary.generate_response.metadata.verified_plan_facts = planFacts();
    const second = inquiry();
    const conflicting = planFacts();
    conflicting.facts.rk_plan_id.value = 'RK-00000';
    second.generate_response.metadata.verified_plan_facts = conflicting;
    x.related = [second];
    x.total_inquiries_in_ticket = 2;
    return x;
  },
};

// A reason code that rejects the WRAPPER outright would make the comparison
// below vacuous, so those are asserted absent. A per-field reason such as
// fact_conflict is exactly what these cases are for and is expected.
const FATAL_REASONS = ['invalid_reference', 'invalid_poll', 'ticket_mismatch', 'job_mismatch',
  'job_not_succeeded', 'job_error', 'invalid_now', 'job_timestamps_invalid', 'job_expired',
  'conversation_binding_missing', 'conversation_binding_invalid', 'conversation_binding_mismatch',
  'conversation_binding_incomplete', 'job_evidence_unavailable'];

for (const [name, build] of Object.entries(parityCases)) {
  test(`canonical parity over a realistic job wrapper: ${name}`, () => {
    const x = build();
    const canonical = canonicalFor(x);
    for (const reason of FATAL_REASONS) {
      assert.ok(!canonical.reason_codes.includes(reason),
        'the wrapper must be a job canonical-evidence.js actually judges, not one it rejects: ' +
        canonical.evidence_status + ' ' + canonical.reason_codes.join(','));
    }
    const payload = run('format-gr', x);
    const projected = payload.inquiries.map(iq => iq.metadata || iq.knowledge_answer.metadata);
    assert.ok(projected.length > 0);
    for (const metadata of projected) {
      assert.deepEqual(metadata.verified_participant_facts,
        canonical.verified_participant_facts ?? undefined,
        'the consumer must transport exactly the canonical participant projection');
      assert.deepEqual(metadata.verified_plan_facts,
        canonical.verified_plan_facts ?? undefined,
        'the consumer must transport exactly the canonical plan projection');
    }
  });
}
