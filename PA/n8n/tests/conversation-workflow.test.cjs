const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const {producerSnapshotCode,includeTicketCode,typedTicketBody,updateJobBody}=require('../conversation-workflow.js');
const fixture=JSON.parse(fs.readFileSync(require('node:path').join(__dirname,'../fixtures/conversation-snapshot.fixture')));
const s=fixture;
function source(){return {work:{id:s.work_id,display_id:s.ticket_id,type:'ticket',visibility:{label:'external'},title:s.subject,body:s.initial_message.body,created_date:s.initial_message.created_at,created_by:{id:s.initial_message.author_id,type:'rev_user'}},pages:[{timeline_entries:s.messages.map(m=>({id:m.id,object:s.work_id,type:'timeline_comment',visibility:'external',created_date:m.created_at,modified_date:m.updated_at,created_by:{id:m.author_id,type:'dev_user'},body:m.body}))}]};}
function run(code,nodes,input){
 const setup=`const nodes=JSON.parse(${JSON.stringify(JSON.stringify(nodes))});const input=JSON.parse(${JSON.stringify(JSON.stringify(input))});
 const $=name=>({first:()=>({json:nodes[name]})});const $input={all:()=>input.map(json=>({json})),first:()=>({json:input[0]})};`;
 return JSON.parse(JSON.stringify(vm.runInNewContext(setup+'(function(){'+code+'})()',{Date,Buffer},{timeout:1000})));
}
test('Producer retains authenticated roles and original bodies from every page without model rewriting',()=>{
 const {work,pages}=source();pages[0].next_cursor='next';pages.push({timeline_entries:[]});
 const out=run(producerSnapshotCode(),{'Get tickets from DevRev1':{work},'Get relevant tag1':{ticketId:s.ticket_id,emailBody:'stale',userName:'Synthetic'}},pages)[0].json;
 assert.equal(out.conversation_snapshot.initial_message.author_role,'participant');assert.equal(out.conversation_snapshot.messages[0].author_role,'advisor');
 assert.equal(out.emailBody,work.body);assert.equal(out.ticket_messages,undefined);assert.equal(out.conversation_snapshot.complete,true);
});
test('Producer rejects lost pages and preserves partial source diagnostics',()=>{
 const {work,pages}=source();pages[0].next_cursor='pending';
 const out=run(producerSnapshotCode(),{'Get tickets from DevRev1':{work},'Get relevant tag1':{ticketId:s.ticket_id}},pages)[0].json;
 assert.equal(out.conversation_snapshot.partial,true);
 assert.throws(()=>run(producerSnapshotCode(),{'Get tickets from DevRev1':{work},'Get relevant tag1':{ticketId:'TKT-OTHER'}},pages),/invalid/);
});
test('Include node binds original conversation and stable retry key, not mutable model contact classification',()=>{
 const base={ticketId:s.ticket_id,emailSubject:'rewritten',emailBody:'rewritten',conversation_snapshot:s};
 const nodes={'Code':base,'Obtain the real message from the PPT':base,'Get fields1':{ticketId:s.ticket_id}};
 const input=[{userData:{pptId:'123',planId:'456'},fubData:{recordKeeper:'synthetic'}}];
 const a=run(includeTicketCode(),nodes,input);base.conversation_snapshot={...s,captured_at:'2026-09-17T00:00:00Z'};
 const b=run(includeTicketCode(),nodes,input);
 assert.equal(a.ticketData.emailSubject,s.subject);assert.equal(a.ticketData.emailBody,s.initial_message.body);
 assert.equal(a.idempotencyKey,b.idempotencyKey);
 base.conversation_snapshot={...s,initial_message:{...s.initial_message,body:'Changed participant request'}};
 assert.notEqual(a.idempotencyKey,run(includeTicketCode(),nodes,input).idempotencyKey);
});
test('Handle Ticket body carries typed input and does not submit untyped history',()=>{
 const old='={"ticket":{"ticket_messages": {{ legacy() }},"email_body":"original"}}';
 const out=typedTicketBody(old);assert.match(out,/conversation_snapshot/);assert.doesNotMatch(out,/ticket_messages|legacy/);
 assert.throws(()=>typedTicketBody('={"ticket":{}}'),/unexpected/);
});
test('Update carries exact accepted job; unrelated legacy branches clear stale reference',()=>{
 const old='={"custom_fields":{"tnt__workflow_status": "Context Enriched"}}';
 const out=updateJobBody(old);assert.match(out,/tnt__pa_execution_job/);assert.match(out,/Handle Ticket/);assert.doesNotMatch(out,/latest|internal_notes/);
});
test('n8n host Object binding accepts ordinary JSON literals from the VM realm',()=>{
 const {work,pages}=source();
 const setup=`const w=JSON.parse(${JSON.stringify(JSON.stringify(work))});const pages=JSON.parse(${JSON.stringify(JSON.stringify(pages))});
 const $=name=>({first:()=>({json:name==='Get tickets from DevRev1'?{work:w}:{ticketId:w.display_id}})});
 const $input={all:()=>pages.map(json=>({json}))};`;
 const out=vm.runInNewContext(setup+'(function(){'+producerSnapshotCode()+'})()',{Object,Date,Buffer},{timeout:1000});
 assert.equal(out[0].json.conversation_snapshot.complete,true);
});
test('n8n hardened Object.getPrototypeOf placeholders do not reject JSON records',()=>{
 const {work,pages}=source(),hardenedObject=Object.create(Object);hardenedObject.getPrototypeOf=()=>({});
 const setup=`const w=JSON.parse(${JSON.stringify(JSON.stringify(work))});const pages=JSON.parse(${JSON.stringify(JSON.stringify(pages))});
 const $=name=>({first:()=>({json:name==='Get tickets from DevRev1'?{work:w}:{ticketId:w.display_id}})});
 const $input={all:()=>pages.map(json=>({json}))};`;
 const out=vm.runInNewContext(setup+'(function(){'+producerSnapshotCode()+'})()',{Object:hardenedObject,Date,Buffer},{timeout:1000});
 assert.equal(out[0].json.conversation_snapshot.complete,true);
});
test('Status update uses the accepted job and refuses malformed executed responses',()=>{
 const out=updateJobBody('={"custom_fields":{"tnt__workflow_status": "Context Enriched"}}');
 const expression=out.slice(out.indexOf('{{')+2,out.indexOf(' }}',out.indexOf('{{')));
 const evaluate=lookup=>JSON.parse(new Function('$','return '+expression)(lookup));
 const job='1'.repeat(32);
 assert.equal(evaluate(name=>{assert.equal(name,'Handle Ticket');return {first:()=>({json:{ticket_job_id:job}})};}),job);
 assert.equal(evaluate(()=>{throw new Error('node_not_executed');}),null);
 for(const bad of [{},{ticket_job_id:'model-guess'},{ticket_job_id:[job]}]){
  assert.throws(()=>evaluate(()=>({first:()=>({json:bad})})),/reference missing/);
 }
});
