const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('Internal handoff serializes nested JSON, quotes, and newlines without enriching or sending', () => {
  const input = {ticketId:'TKT-SIMULATION',body:JSON.stringify({reason:'Verify "account" internally\nNext step',human_review_required:true}),tag:'must-not-copy',stage:'must-not-copy'};
  // The old template quotes an already serialized body without escaping it.
  assert.throws(() => JSON.parse('{"body":"'+input.body+'"}'), SyntaxError);
  const source = fs.readFileSync(path.join(__dirname,'../candidates/internal-handoff-body.js'),'utf8');
  const output = JSON.parse(new Function('$json',source)(input));
  assert.deepEqual(output,{object:input.ticketId,body:input.body,visibility:'internal',type:'timeline_comment'});
  assert.equal(JSON.parse(output.body).human_review_required,true);
});
