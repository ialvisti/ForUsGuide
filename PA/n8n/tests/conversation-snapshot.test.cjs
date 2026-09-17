const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const file = path.join(__dirname, '../conversation-snapshot.js');
const api = fs.existsSync(file) ? require(file) : {};
const fixture = () => JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/conversation-snapshot.fixture')));
function reference(value) {
  assert.equal(typeof api.conversationReference, 'function', 'shared snapshot binding is implemented');
  return api.conversationReference(value);
}
test('Content and authorship, rather than read time, bind the conversation', () => {
  const value = fixture(), first = reference(value);
  value.captured_at = '2026-09-17T02:00:00.000Z';
  assert.deepEqual(reference(value), first);
  value.messages[0].body = 'A distinct condition';
  assert.notEqual(reference(value).digest, first.digest);
});
test('Message order from pages does not change the reference', () => {
  const value = fixture();
  value.messages.push({...value.messages[0], id:'don:synthetic:timeline_entry:2',created_at:'2026-09-16T01:00:00.000Z'});
  const expected=reference(value); value.messages.reverse();
  assert.deepEqual(reference(value),expected);
});
for (const [label, mutate] of [
  ['model digest', s=>s.digest='a'.repeat(64)],
  ['coerced completeness', s=>s.complete='true'],
  ['contradictory completeness', s=>s.partial=true],
  ['duplicate entry', s=>s.messages.push(s.messages[0])],
  ['hidden note', s=>s.messages[0].visibility='internal'],
  ['missing author', s=>s.messages[0].author_id=null],
  ['future message', s=>s.messages[0].updated_at='2026-09-18T01:00:00.000Z'],
  ['invalid calendar date', s=>s.messages[0].created_at='2026-02-30T01:00:00.000Z'],
  ['different work', s=>s.work_id='don:synthetic:ticket:2'],
  ['non-DevRev identifier', s=>s.messages[0].id='don:synthetic:\u{10000}'],
  ['non-DevRev author', s=>s.messages[0].author_id='don:synthetic:\u{10000}'],
]) test('Rejects '+label,()=>{
  const value=fixture(); mutate(value);
  assert.equal(typeof api.conversationReference,'function');
  assert.throws(()=>api.conversationReference(value),/invalid_conversation_snapshot/);
});
test('Partial reads keep explicit incomplete flags',()=>{
  const value=fixture(); Object.assign(value,{complete:false,partial:true,truncated:true});
  assert.equal(reference(value).complete,false);
});
test('Shared golden digest agrees with the backend',()=>{
  assert.equal(reference(fixture()).digest,'814d1eb1b58a7e448bb538bdbd7b7ce1568c28237062c45eff8742579cee42e6');
});
test('Absent body edit timestamps stay explicitly unknown',()=>{
  const value=fixture(); value.initial_message.updated_at=null; value.messages[0].updated_at=null;
  assert.match(reference(value).digest,/^[a-f0-9]{64}$/);
});
