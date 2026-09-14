// Check captured, synthetic JSON parser Execute-step output; never call a workflow.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const [file, mode = 'verified'] = process.argv.slice(2);
assert.ok(file, 'Usage: node verify-parser-replay.cjs <capture.json> verified|unverified|conflicting');
assert.ok(['verified', 'unverified', 'conflicting'].includes(mode));
const r = JSON.parse(fs.readFileSync(file, 'utf8')).result;
assert.deepEqual(Object.keys(r), ['ticketId', 'participant_reply', 'set_stage_solved', 'stage_reason', 'internal_notes']);
assert.equal(r.ticketId, 'TKT-SIMULATION');
assert.equal(r.set_stage_solved, false);
assert.equal(typeof r.participant_reply, 'string');
assert.equal(typeof r.stage_reason, 'string');
assert.ok(typeof r.internal_notes === 'string' || r.internal_notes === null);
assert.doesNotMatch(JSON.stringify(r), /TKT-UNTRUSTED|REDACTED|MASKED/);
assert.match(r.participant_reply, /1\..*total balance/i);
assert.match(r.participant_reply, /2\..*contribution sources/i);
assert.match(r.participant_reply, /not a verified vested balance/i);
assert.match(r.participant_reply, /non-Roth after-tax/i);
assert.match(r.participant_reply, /(?:need|still|remain|confirm)/i);
if (mode === 'verified') {
  assert.match(r.participant_reply, /Hi Alex,/);
  for (const value of ['$500', '$300', '$200']) assert.ok(r.participant_reply.includes(value), `Lost ${value}`);
  assert.match(r.participant_reply, /September 1, 2026|2026-09-01/);
  assert.match(r.internal_notes, /participant\.census\.First Name/);
  assert.match(r.internal_notes, /participant\.savings_rate\.Account Balance/);
  assert.match(r.internal_notes, /2026-09-11/);
} else {
  assert.doesNotMatch(JSON.stringify(r), /Alex|\$500|\$300|\$200/);
  assert.match(r.stage_reason + r.internal_notes, /review|verif|conflict/i);
  assert.doesNotMatch(r.participant_reply, /provide.*(?:name|SSN|statement)|send.*(?:name|SSN|statement)/i);
}
console.log(`Actual parser replay acceptance PASS: ${mode}`);
