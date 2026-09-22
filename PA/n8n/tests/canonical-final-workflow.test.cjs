const {test}=require('node:test');
const assert=require('node:assert/strict');
const {selectFinalReference,validateWorkReference,decodeCanonicalPoll}=require('../canonical-final-workflow.js');
const job='a'.repeat(32),ticket='TKT-100001';
const event=()=>({headers:{'x-pa-ticket-id':ticket,'x-pa-ticket-job-id':job},body:{ticketId:ticket,agentResponse:{participant_reply:'Synthetic draft',ticket_job_id:'f'.repeat(32)}}});
test('Exact job comes only from the emitter header, never model output',()=>{
 const r=selectFinalReference(event());assert.equal(r.ticket_job_id,job);assert.equal(r.ticketId,ticket);assert.equal(r.read_canonical,true);
});
test('Missing or malformed independent job cannot select any poll URL',()=>{
 for(const bad of [undefined,null,'',false,[job],'https://untrusted.invalid/job','f'.repeat(31)]){
  const input=event();input.headers['x-pa-ticket-job-id']=bad;
  const out=selectFinalReference(input);assert.equal(out.read_canonical,false);assert.equal(out.ticket_job_id,null);
 }
 const input=event();delete input.headers['x-pa-ticket-id'];assert.equal(selectFinalReference(input).read_canonical,false);
});
test('Body metadata cannot fill a missing header and ticket mismatch is rejected',()=>{
 const input=event();delete input.headers['x-pa-ticket-job-id'];input.body.ticket_job_id=job;
 assert.equal(selectFinalReference(input).read_canonical,false);
 input.headers['x-pa-ticket-id']='TKT-100002';assert.throws(()=>selectFinalReference(input),/correlation/);
});
test('Authenticated work validates the selected job instead of selecting its current value',()=>{
 const ref=selectFinalReference(event());const work={type:'ticket',display_id:ticket,custom_fields:{tnt__pa_execution_job:job}};
 assert.equal(validateWorkReference(ref,work),work);
 for(const bad of [{...work,display_id:'TKT-OTHER'},{...work,custom_fields:{tnt__pa_execution_job:'b'.repeat(32)}},{...work,custom_fields:{}}]){
  assert.throws(()=>validateWorkReference(ref,bad),/reference/);
 }
});
test('Poll adapter rejects errors, non-JSON and oversized text before JSON.parse',()=>{
 const response={statusCode:200,headers:{'content-type':'application/json; charset=utf-8'},body:JSON.stringify({ticket_job_id:job})};
 assert.deepEqual(decodeCanonicalPoll(response),{ticket_job_id:job});
 for(const bad of [{...response,statusCode:302},{...response,body:{ticket_job_id:job}},{...response,body:'x'.repeat(1048577)},{...response,headers:{'content-type':'text/html'}},{...response,body:'not JSON'}]){
  assert.throws(()=>decodeCanonicalPoll(bad),/canonical_poll/);
 }
});
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const builders=require('../canonical-final-workflow.js');
const {conversationReference}=require('../conversation-snapshot.js');
function state(){
 const snapshot=JSON.parse(fs.readFileSync(path.join(__dirname,'../fixtures/conversation-snapshot.fixture')));
 const reference=conversationReference(snapshot),now=Date.now();
 const facts={identity_verified:true,identity_resolution_status:'matched',facts:{account_balance:{status:'known',value:123.45,source:'participant.savings_rate.Account Balance',observed_at:new Date(now-30000).toISOString(),as_of:'2026-09-16'}}};
 const poll={ticket_id:ticket,ticket_job_id:job,state:'succeeded',created_at:new Date(now-60000).toISOString(),completed_at:new Date(now-10000).toISOString(),expires_at:new Date(now+3600000).toISOString(),conversation_reference:reference,primary:{generate_response:{metadata:{verified_participant_facts:facts},response:{}}},related:[],error:null};
 const nodes={'PA Final Reference':selectFinalReference(event()),'PA Final Conversation Hash':{snapshot,digest:reference.digest}};
 return {nodes,poll,facts};
}
function run(code,nodes,input){
 const hostObject=Object.create(Object);hostObject.getPrototypeOf=()=>({});
 return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+code+'})()',{
  $:name=>{assert.ok(nodes[name],name+' must have executed');return {first:()=>({json:nodes[name]})};},
  $input:{first:()=>({json:input}),all:()=>(Array.isArray(input)?input:[input]).map(json=>({json}))},Object:hostObject,Buffer,Date,
 },{timeout:1000})));
}
function response(poll){return {statusCode:200,headers:{'content-type':'application/json'},body:JSON.stringify(poll)};}
test('Generated n8n evidence node binds HTTP response and retains facts outside model prose',()=>{
 const {nodes,poll,facts}=state();nodes['PA Final Reference'].agentResponse={verified_participant_facts:{facts:{account_balance:{value:999}}}};
 const out=run(builders.evidenceCode(),nodes,response(poll))[0].json;
 assert.equal(out.canonical_evidence.evidence_status,'matched');assert.deepEqual(out.canonical_evidence.verified_participant_facts,facts);
 assert.equal(out.canonical_evidence.publication_authorized,false);
});
test('Changed or incomplete conversation hides every fact from the final consumer',()=>{
 for(const change of [s=>{s.nodes['PA Final Conversation Hash'].digest='f'.repeat(64);},s=>{s.nodes['PA Final Conversation Hash'].snapshot.partial=true;s.nodes['PA Final Conversation Hash'].snapshot.complete=false;}]){
  const s=state();change(s);const out=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
  assert.equal(out.canonical_evidence.verified_participant_facts,null);
  assert.equal(out.canonical_evidence.human_review_required,true);
 }
});
test('Missing header takes the unavailable branch without reading an unexecuted HTTP node',()=>{
 const nodes={'PA Final Reference':{ticketId:ticket,agentResponse:'Synthetic',read_canonical:false,reference_reason:'missing_independent_job_reference'}};
 const out=run(builders.evidenceCode(),nodes,{});
 assert.equal(out[0].json.canonical_evidence.evidence_status,'unavailable');
});
test('Generated adapters compose event, authenticated work, paginated source, hash, poll and parser boundary',()=>{
 const s=state(),snapshot=s.nodes['PA Final Conversation Hash'].snapshot;
 const selected=run(builders.referenceCode(),{Webhook:event()},{} )[0].json;
 const work={id:snapshot.work_id,display_id:ticket,type:'ticket',title:snapshot.subject,
  body:snapshot.initial_message.body,visibility:{label:'external'},created_date:snapshot.initial_message.created_at,
  created_by:{id:snapshot.initial_message.author_id,type:'rev_user'},custom_fields:{tnt__pa_execution_job:job}};
 const pages=[{timeline_entries:[],next_cursor:'second'},
  {timeline_entries:snapshot.messages.map(m=>({id:m.id,object:work.id,type:'timeline_comment',
   visibility:'external',body:m.body,created_date:m.created_at,modified_date:m.updated_at,
   created_by:{id:m.author_id,type:'dev_user'}}))}];
 const nodes={'PA Final Reference':selected,'PA Final Work':{work}};
 const bound=run(builders.sourceCode(),nodes,pages)[0].json;
 assert.equal(bound.snapshot.complete,true);assert.equal(bound.snapshot.messages.length,1);
 assert.equal(bound.snapshot.initial_message.author_role,'participant');
 bound.digest=require('node:crypto').createHash('sha256').update(bound.preimage).digest('hex');
 s.poll.conversation_reference=conversationReference(bound.snapshot);
 const source=run(builders.evidenceCode(),{...nodes,'PA Final Conversation Hash':bound},response(s.poll))[0].json;
 assert.equal(source.canonical_evidence.evidence_status,'matched');
 const parserInput=JSON.parse(run(builders.parserInputCode(),{'PA Final Evidence':source},{}));
 assert.deepEqual(parserInput.canonical_evidence.verified_participant_facts,s.facts);
 assert.equal(parserInput.ticketId,ticket);
 // A newer work job is a rejection, never a replacement for the event job.
 work.custom_fields.tnt__pa_execution_job='b'.repeat(32);
 assert.throws(()=>run(builders.sourceCode(),nodes,pages),/reference mismatch/);
});
test('Final extractor exposes only independently verified facts and cannot authorize sending or closure',()=>{
 const s=state(),source=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
 const parsed={ticketId:ticket,participant_reply:'Synthetic answer',set_stage_solved:true,stage_reason:'Synthetic reason',internal_notes:null};
 const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)})[0].json;
 assert.deepEqual(out.verified_participant_facts,s.facts);
 for(const key of ['publication_authorized','participant_reply_safe','set_stage_solved'])assert.equal(out[key],false);
 assert.equal(out.human_review_required,true);assert.match(out.internal_notes,/Independent backend evidence/);
 parsed.canonical_evidence={evidence_status:'matched'};
 assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),/Invalid PA final draft/);
});
test('Consumer release manifest binds generated source, prompt and immutable transport endpoints',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../canonical-final-consumer.manifest')));
 const hash=value=>require('node:crypto').createHash('sha256').update(value).digest('hex');
 for(const node of manifest.generated_sources)assert.equal(hash(builders[node.builder]()),node.sha256,node.node);
 assert.equal(hash(fs.readFileSync(path.join(__dirname,'..',manifest.parser_prompt.path))),manifest.parser_prompt.sha256);
 const http=manifest.native_nodes.filter(n=>n.type==='n8n-nodes-base.httpRequest');
 for(const node of http){
  assert.equal(node.parameters.options.redirect.redirect.followRedirects,false,node.name);
  assert.ok(['genericCredentialType','predefinedCredentialType'].includes(node.parameters.authentication));
 }
 const poll=http.find(n=>n.name==='PA Final Poll');
 assert.equal(poll.parameters.url,"=https://kb-rag-system-900340137010.us-central1.run.app/api/v1/tickets/{{ $('PA Final Reference').first().json.ticket_job_id }}");
 assert.deepEqual(poll.parameters.options.response.response,{fullResponse:true,responseFormat:'text',outputPropertyName:'body'});
 assert.equal(manifest.pins_change,false);
 assert.equal(manifest.credentials_change,false);
 assert.equal(manifest.publication_authorizes_participant_messages,false);
});
test('Plan identifier evidence reaches provenance and output without changing the model contract',()=>{
 const s=state();
 const planFacts={plan_id:'580',identity_verified:true,identity_resolution_status:'matched',
  facts:{rk_plan_id:{status:'known',value:'RK-0000123',source:'plan.plan_design.rk_plan_id',
   observed_at:new Date(Date.parse(s.poll.completed_at)-1000).toISOString(),as_of:'2026-09-16'}}};
 s.poll.plan_id='580';
 s.poll.primary.generate_response.metadata.verified_plan_facts=planFacts;
 const source=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
 assert.deepEqual(source.canonical_evidence.verified_plan_facts,planFacts);
 const parsed={ticketId:ticket,participant_reply:'Synthetic answer',set_stage_solved:true,stage_reason:'Synthetic reason',internal_notes:null};
 const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)})[0].json;
 assert.deepEqual(out.verified_plan_facts,planFacts);
 assert.match(out.internal_notes,/verified_plan_facts/);
 assert.match(out.internal_notes,/RK-0000123/);
 for(const key of ['publication_authorized','participant_reply_safe','set_stage_solved'])assert.equal(out[key],false);
 assert.equal(out.human_review_required,true);
 const parserInput=JSON.parse(run(builders.parserInputCode(),{'PA Final Evidence':source},{}));
 assert.deepEqual(parserInput.canonical_evidence.verified_plan_facts,planFacts);
 assert.deepEqual(Object.keys(parserInput),['ticketId','agentResponse','canonical_evidence']);
});
test('A poll without plan metadata leaves plan provenance explicitly null',()=>{
 const s=state();
 const source=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
 assert.equal(source.canonical_evidence.verified_plan_facts,null);
 const parsed={ticketId:ticket,participant_reply:'Synthetic answer',set_stage_solved:false,stage_reason:'Synthetic reason',internal_notes:null};
 const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)})[0].json;
 assert.equal(out.verified_plan_facts,null);
 assert.match(out.internal_notes,/"verified_plan_facts":null/);
});
test('Parser system prompt grounds plan-specific fees to canonical evidence, not agent prose',()=>{
 const prompt=fs.readFileSync(path.join(__dirname,'..','candidates','canonical-final-parser-system.md'),'utf8');
 assert.match(prompt,/canonical_evidence` never carries a verified fee/);
 assert.match(prompt,/is a plan-specific figure with no separate canonical evidence, exactly like an unverified plan identifier/);
 assert.match(prompt,/Do not salvage a withheld plan-specific figure by recasting it as a generic rule/);
 assert.match(prompt,/that no plan-specific fee or amount without separate canonical evidence was stated as a fact in any field/);
 // The carve-out for genuinely generic, plan-independent fees/procedures must remain, not a blanket ban on numbers.
 assert.match(prompt,/remains a preservable generic value under the paragraph below/);
 assert.match(prompt,/stated as general rules, not as this participant's plan-specific figure/);
});
