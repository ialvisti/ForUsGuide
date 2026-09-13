const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const polished = {ticketId:'TKT-SIMULATION',participant_reply:'A bounded answer.',stage_reason:'Review pending.',internal_notes:null,set_stage_solved:false};
function run(file, webhook) {
  const source = fs.readFileSync(path.join(root, 'candidates', file), 'utf8');
  return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+source+'\n})()', {
    $: name => { assert.equal(name,'Webhook'); return {first:()=>({json:webhook})}; },
    $input:{first:()=>({json:{output:JSON.stringify(polished)}})},
  }, {timeout:1000})));
}
const opaque = '{"agentResponse": "```json\n{"participant_reply":"Synthetic text"}\n```", "ticketId":"TKT-UNTRUSTED"}';
test('Legacy opaque body requires a separately mapped ticket header and remains advisory',()=>{
  const out=run('final-data-extractor.js',{headers:{'x-pa-ticket-id':'TKT-SIMULATION'},body:opaque})[0].json;
  assert.equal(out.ticketId,'TKT-SIMULATION');
  assert.equal(out.publication_authorized,false);
  assert.equal(out.set_stage_solved,false);
});
test('Parser receives valid outer JSON while legacy model content stays opaque',()=>{
  const out=JSON.parse(run('final-parser-input.js',{headers:{'x-pa-ticket-id':'TKT-SIMULATION'},body:opaque}));
  assert.equal(out.ticketId,'TKT-SIMULATION');assert.equal(out.agentResponse,opaque);
});
test('A valid JSON string envelope is normalized without guessing',()=>{
  const body=JSON.stringify({ticketId:'TKT-SIMULATION',agentResponse:polished});
  assert.equal(run('final-data-extractor.js',{body})[0].json.ticketId,'TKT-SIMULATION');
});
test('Header and structured body disagreement stops before ticket writes',()=>{
  assert.throws(()=>run('final-data-extractor.js',{headers:{'x-pa-ticket-id':'TKT-OTHER'},body:{ticketId:'TKT-SIMULATION',agentResponse:polished}}),/correlation/i);
});
test('Missing or invalid independent correlation never repairs an opaque body',()=>{
  for(const header of [undefined,'',false,['TKT-SIMULATION'],'TKT-SIMULATION\nX: injected']) {
    assert.throws(()=>run('final-data-extractor.js',{headers:{'x-pa-ticket-id':header},body:opaque}),/correlation/i);
  }
});
test('Empty and oversized raw responses are rejected before parsing',()=>{
  for(const body of ['',null,'x'.repeat(131073)]) {
    assert.throws(()=>run('final-data-extractor.js',{headers:{'x-pa-ticket-id':'TKT-SIMULATION'},body}),/contract|correlation/i);
  }
});
