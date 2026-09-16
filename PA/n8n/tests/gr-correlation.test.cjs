const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../candidates/format-gr.js'),'utf8');
const ticket='TKT-SYNTHETIC';
const jobId='a'.repeat(32), otherJob='b'.repeat(32);
const response=()=>({ticket_job_id:jobId,ticket_id:ticket,state:'succeeded',next_action:'send_participant_reply',
 created_at:'2026-09-16T01:00:00Z',completed_at:'2026-09-16T01:01:00Z',expires_at:'2026-09-23T01:00:00Z',
 primary:{inquiry:'What does vesting mean?',route:'knowledge_question',knowledge_answer:{answer:'Vesting means ownership.',metadata:{response_source_reason:'general_knowledge'}}},related:[],total_inquiries_in_ticket:1});
function run(input,accepted={ticket_job_id:jobId},fields={ticketId:ticket}){
 const result=vm.runInNewContext('(function(){'+source+'\n})()',{
  $input:{first:()=>({json:input})},
  $:name=>({first:()=>{if(name==='Get fields1')return {json:fields};if(name==='Handle Ticket'){if(accepted===null)throw Error('Unexecuted node');return {json:accepted};}throw Error('Unexpected dependency');}}),
 },{timeout:1000});
 const raw=JSON.parse(JSON.stringify(result.json));
 const escaped=raw.body.slice('```\\n\\n'.length,-'\\n\\n'.length);
 return {raw,payload:JSON.parse(JSON.parse('"'+escaped+'"'))};
}
test('Correlated poll keeps the accepted execution and the exact dated reference',()=>{
 const {payload}=run(response());
 assert.deepEqual(payload.execution_reference,{source:'ticket_job_poll',ticket_id:ticket,ticket_job_id:jobId,
  created_at:'2026-09-16T01:00:00Z',completed_at:'2026-09-16T01:01:00Z',expires_at:'2026-09-23T01:00:00Z'});
 assert.equal(payload.participant_reply_safe,true);
});
for(const reason of ['wrong_ticket','missing_ticket','null_ticket','wrong_job','missing_job','invalid_job','missing_acceptance','different_acceptance','multiple_poll_results','multiple_acceptances','spoofed_metadata','don_display_mismatch']){
 test('Reject before serialization when '+reason,()=>{
  let x=response(),accepted={ticket_job_id:jobId};
  if(reason==='wrong_ticket')x.ticket_id='TKT-OTHER';
  if(reason==='missing_ticket')delete x.ticket_id;
  if(reason==='null_ticket')x.ticket_id=null;
  if(reason==='wrong_job')x.ticket_job_id=otherJob;
  if(reason==='missing_job')delete x.ticket_job_id;
  if(reason==='invalid_job')x.ticket_job_id=accepted.ticket_job_id='https://untrusted.test/job';
  if(reason==='missing_acceptance')accepted=null;
  if(reason==='different_acceptance')accepted.ticket_job_id=otherJob;
  if(reason==='multiple_poll_results')x=[x,{...x,ticket_job_id:otherJob}];
  if(reason==='multiple_acceptances')accepted=[accepted,{ticket_job_id:otherJob}];
  if(reason==='spoofed_metadata'){x.ticket_id='TKT-OTHER';x.metadata={ticket_id:ticket,ticket_job_id:jobId};}
  if(reason==='don_display_mismatch')x.ticket_id='don:core:example:devo/test:ticket/1';
  assert.throws(()=>run(x,accepted),/correlation/i);
 });
}
test('A single array wrapper is accepted without choosing among multiple executions',()=>{
 assert.equal(run([response()],[{ticket_job_id:jobId}]).payload.ticket_job_id,jobId);
});
test('Inline KQ remains compatible when it is the exact response of Handle Ticket',()=>{
 const inline=response();for(const key of ['ticket_job_id','ticket_id','state','next_action','created_at','completed_at','expires_at'])delete inline[key];
 const p=run(inline,structuredClone(inline)).payload;
 assert.deepEqual(p.execution_reference,{source:'handle_ticket_inline',ticket_id:ticket,ticket_job_id:null});
 assert.equal(p.inquiries[0].knowledge_answer.answer,'Vesting means ownership.');
 assert.equal(p.inquiries[0].human_review_required,false);
});
test('An unrelated inline answer cannot impersonate the accepted response',()=>{
 const accepted={primary:response().primary,related:[]}, other=structuredClone(accepted);other.primary.knowledge_answer.answer='Unrelated response.';
 assert.throws(()=>run(other,accepted),/correlation/i);
});
test('An empty or unavailable input cannot supply a reference',()=>{
 for(const x of [null,{},[],[null]])assert.throws(()=>run(x),/correlation/i);
});
test('A poll does not become inline by removing its job identity',()=>{
 const x=response();delete x.ticket_job_id;delete x.ticket_id;
 assert.throws(()=>run(x,structuredClone(x)),/correlation/i);
});
