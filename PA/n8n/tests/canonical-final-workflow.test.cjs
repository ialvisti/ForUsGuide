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
 assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),/PA certainty guard rejected parser output; no ticket write permitted/);
 const mismatched={ticketId:'TKT-100002',participant_reply:'Synthetic answer',set_stage_solved:true,stage_reason:'Synthetic reason',internal_notes:null};
 assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(mismatched)}),/Invalid PA final draft/);
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
test('Data extractor source matches the release manifest and is not the served node',()=>{
 const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../canonical-final-consumer.manifest')));
 const hash=require('node:crypto').createHash('sha256').update(builders.extractorCode()).digest('hex');
 const pin=manifest.generated_sources.find(node=>node.node==='Data extractor').sha256;
 assert.equal(hash,pin);
 assert.notEqual(pin,'c48ea5854cff546b2bb8803c9820bfd071c4d5f1f40f0d76495906ff56123151');
});
test('Extractor rejects an unsupported personal age claim in a penalty statement',()=>{
 const s=state(),source=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
 const parsed={ticketId:ticket,participant_reply:'Because you are under 59½, a 10% early withdrawal penalty will apply.',
  set_stage_solved:true,stage_reason:'Synthetic reason',internal_notes:null};
 assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),
  /PA certainty guard rejected parser output; no ticket write permitted/);
});
function missingJobEvidence(){
 return {ticketId:ticket,agentResponse:'Synthetic',canonical_evidence:{
  evidence_status:'unavailable',reason_codes:['missing_independent_job_reference'],
  verified_participant_facts:null,publication_authorized:false,participant_reply_safe:false,
  set_stage_solved:false,human_review_required:true}};
}
const ROUTE_1='**If your employer updates their payroll record** to show a terminated status and adds your termination date, that update typically takes about one week after employment ends, and sometimes up to one payroll cycle.';
const ROUTE_2="**If our team already has access to your employer's payroll record and that record already has a termination date on file,** we can update the necessary systems on our end. After both updates are made, the ability to submit a termination distribution request may become available within the next 24–48 business hours. That is the availability of the request, not receipt of funds.";
const ROUTE_3='**If neither of the above applies** and the termination date remains unconfirmed, we will need to reach out through our internal process to confirm your termination date with the plan sponsor before distribution eligibility can be verified.';
const FEE_TEXT='The $75 base fee applies to every delivery method. A wire transfer adds a $35 non-refundable fee. ACH and regular mail check have no additional delivery charge.';
const TIMING='That timing is an estimate, not a guaranteed SLA.';
const LAST_DAY='To help us reconcile the employment record, could you share your last day of employment?';
const HOLD='The current employment record still needs employer verification before a termination cash distribution can move forward.';
const SUPPORTED=[HOLD,ROUTE_1,TIMING,ROUTE_2,ROUTE_3,FEE_TEXT,LAST_DAY].join(' ');
const STATUS_CLAIM='However, our records currently show your employment status as active with no termination date on file.';
test('Null employment evidence cannot publish an exact current status',()=>{
 const source=missingJobEvidence();
 const parsed={ticketId:ticket,participant_reply:SUPPORTED+' '+STATUS_CLAIM,
  set_stage_solved:false,stage_reason:'The record shows Active.',
  internal_notes:'System status is active.'};
 assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),
  /Unsupported employment-status claim; no ticket write permitted/);
});
test('Account facts do not authorize an exact employment status or date',()=>{
 const s=state(),source=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
 assert.ok(source.canonical_evidence.verified_participant_facts);
 for(const claim of ['Our employment record is Active.','Status: Active.','Our payroll shows you remain employed.',
  'Your termination date is July 15, 2026.','Your termination date is 7/15/2026.','Your termination date is 07-15-2026.','Your termination date is 2026-07-15.']){
  const parsed={ticketId:ticket,participant_reply:SUPPORTED+' '+claim,
   set_stage_solved:false,stage_reason:'Employer verification is still required.',internal_notes:null};
  assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),
   /Unsupported employment-status claim/);
 }
});
test('Exact conditional routes survive when only the current status claim is generalized',()=>{
 const source=missingJobEvidence();
 const notes='Supplied employment status active is unverified and does not establish eligibility.';
 const parsed={ticketId:ticket,participant_reply:SUPPORTED,set_stage_solved:true,
  stage_reason:'Employer verification is still required.',internal_notes:notes};
 const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)})[0].json;
 for(const phrase of [ROUTE_1,ROUTE_2,ROUTE_3,FEE_TEXT,TIMING,LAST_DAY,'$75','$35','24–48']){
  assert.ok(out.participant_reply.includes(phrase),phrase);
 }
 assert.equal(out.participant_reply.includes(STATUS_CLAIM),false);
 assert.doesNotMatch(out.participant_reply,/we are investigating|we have escalated|investigation has already/i);
 assert.equal(out.stage_reason,'Advisor review is required. Employer verification is still required.');
 assert.match(out.internal_notes,/unverified/);
 assert.match(out.internal_notes,/does not establish eligibility/);
 for(const key of ['publication_authorized','participant_reply_safe','set_stage_solved'])assert.equal(out[key],false);
 assert.equal(out.human_review_required,true);
 assert.equal(out.verified_participant_facts,null);
});
test('A route condition survives but a leading if does not license a main-clause assertion',()=>{
 const source=missingJobEvidence();
 // The served draft varies its route wording between samples. A status term inside
 // the condition itself is a procedure, not this participant's current fact.
 const routeVariant='**If the record shows your status as terminated** and a termination date is added, that update typically takes about one week after employment ends.';
 const ok={ticketId:ticket,participant_reply:[HOLD,routeVariant,FEE_TEXT,LAST_DAY].join(' '),
  set_stage_solved:false,stage_reason:'Employer verification is still required.',internal_notes:null};
 const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(ok)})[0].json;
 assert.ok(out.participant_reply.includes(routeVariant));
 // An assertion after the condition resolves is still an assertion: no blanket if exemption.
 for(const claim of ['If you ask, our records show Active.',
  'If you are wondering, your employment status is active.',
  'If it helps, our payroll shows you remain employed.']){
  const bad={ticketId:ticket,participant_reply:[HOLD,claim].join(' '),
   set_stage_solved:false,stage_reason:'Employer verification is still required.',internal_notes:null};
  assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(bad)}),
   /Unsupported employment-status claim/,claim);
 }
});
test('Unsegmented conditional, list-verb and last-day date forms are rejected',()=>{
 const source=missingJobEvidence();
 // (1) no comma, so there is no apodosis boundary to trust; (2) "list ... as";
 // (3) the date arrives under "last day of employment" rather than a date label.
 for(const claim of ['If you ask we can confirm our records show Active.',
  'Our records list your status as Active.',
  'Your last day of employment was 7/15/2026.']){
  const bad={ticketId:ticket,participant_reply:[HOLD,claim].join(' '),
   set_stage_solved:false,stage_reason:'Employer verification is still required.',internal_notes:null};
  assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(bad)}),
   /Unsupported employment-status claim/,claim);
 }
});
test('Notes that turn a supplied status into eligibility are rejected',()=>{
 const source=missingJobEvidence();
 const parsed={ticketId:ticket,participant_reply:SUPPORTED,set_stage_solved:false,
  stage_reason:'Employer verification is still required.',
  internal_notes:'The system confirms employment status as active, so the participant is eligible.'};
 assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),
  /Unsupported employment-status claim/);
});
const GENERATED_STATUS_NOTE='Employment-status conflict: participant reports separation; system record shows employment_status Active with no termination date on file.';
const PARAPHRASED_STATUS_NOTE='Employment-status conflict: participant reports separation; system record shows Active with no termination date on file.';
test('A copied generated employment claim is retained only as an unverified internal note',()=>{
 const generated={participant_reply:STATUS_CLAIM,internal_notes:GENERATED_STATUS_NOTE};
 for(const agentResponse of [generated,JSON.stringify(generated)]){
  for(const note of [GENERATED_STATUS_NOTE,PARAPHRASED_STATUS_NOTE]){
   const source=missingJobEvidence();source.agentResponse=agentResponse;
   const parsed={ticketId:ticket,participant_reply:SUPPORTED,set_stage_solved:true,
    stage_reason:'Employer verification is still required.',internal_notes:note};
   const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)})[0].json;
   assert.equal(out.participant_reply,SUPPORTED);
   assert.equal(out.stage_reason,'Advisor review is required. Employer verification is still required.');
   assert.match(out.internal_notes,/^Unverified claim from supplied generated response, not canonical evidence:/);
   assert.ok(out.internal_notes.includes(note));
   assert.equal(out.verified_participant_facts,null);
   for(const key of ['publication_authorized','participant_reply_safe','set_stage_solved'])assert.equal(out[key],false);
   assert.equal(out.human_review_required,true);
  }
 }
});
test('A note cannot gain generated-response provenance without the same source claim',()=>{
 const source=missingJobEvidence();
 for(const note of [GENERATED_STATUS_NOTE,PARAPHRASED_STATUS_NOTE]){
  const parsed={ticketId:ticket,participant_reply:SUPPORTED,set_stage_solved:false,
   stage_reason:'Employer verification is still required.',internal_notes:note};
  assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),
   /Unsupported employment-status claim/);
 }
});
test('A sourced note does not license a separate status or eligibility authority claim',()=>{
 const source=missingJobEvidence();
 source.agentResponse={participant_reply:STATUS_CLAIM,internal_notes:GENERATED_STATUS_NOTE};
 for(const extra of ['Our records show employment status Terminated.',
  'The system confirms employment status as active, so the participant is eligible.']){
  const parsed={ticketId:ticket,participant_reply:SUPPORTED,set_stage_solved:false,
   stage_reason:'Employer verification is still required.',internal_notes:GENERATED_STATUS_NOTE+'\n'+extra};
  assert.throws(()=>run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)}),
   /Unsupported employment-status claim/,extra);
 }
});
test('Extractor preserves conditional age wording and human-review gates',()=>{
 const s=state(),source=run(builders.evidenceCode(),s.nodes,response(s.poll))[0].json;
 const reply='If you are under 59½, a 10% early withdrawal penalty may apply.';
 const parsed={ticketId:ticket,participant_reply:reply,set_stage_solved:true,stage_reason:'Synthetic reason',internal_notes:null};
 const out=run(builders.extractorCode(),{'PA Final Evidence':source},{output:JSON.stringify(parsed)})[0].json;
 assert.equal(out.participant_reply,reply);
 for(const key of ['publication_authorized','participant_reply_safe','set_stage_solved'])assert.equal(out[key],false);
 assert.equal(out.human_review_required,true);
 assert.equal(out.model_recommended_solved,true);
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
test('Parser prompt uses verified savings Account Balance as total vested',()=>{
 // User authority 2026-09-28 supersedes "never stands in for a vested balance",
 // the preserved total-versus-vested rule, and both worked-example denials.
 const prompt=fs.readFileSync(path.join(__dirname,'..','candidates','canonical-final-parser-system.md'),'utf8');
 assert.doesNotMatch(prompt,/never stands in for a vested balance/);
 assert.doesNotMatch(prompt,/not a verified vested balance/);
 assert.doesNotMatch(prompt,/total-versus-vested/);
 assert.match(prompt,/participant\.savings_rate\.Account Balance is the participant total vested balance/);
 assert.match(prompt,/not a contribution-source breakdown/);
 assert.match(prompt,/not a loan account balance/);
 assert.match(prompt,/only the employer vested portion/);
 assert.match(prompt,/does not establish eligibility, availability for withdrawal/);
 assert.match(prompt,/generic model-described total without this verified account_balance fact remains unverified/);
 assert.match(prompt,/\*\*1\. What is my total balance\?\*\*/);
 assert.match(prompt,/\*\*2\. What are my contribution sources\?\*\*/);
 assert.match(prompt,/Non-Roth after-tax source status still needs confirmation/);
 assert.match(prompt,/treating the missing non-Roth after-tax source as zero/);
 assert.doesNotMatch(prompt,/^\| vested_balance \|/m);
 // The rule paragraphs may name internal fact keys; the worked example is
 // participant draft prose and must obey "use natural category descriptions
 // and dates in the participant draft".
 for(const label of ['Input: ','Correct edited text: ']){
  const line=prompt.split('\n').find(l=>l.startsWith(label));
  assert.ok(line,`missing worked-example line: ${label}`);
  assert.doesNotMatch(line,/account_balance|savings_rate|canonical_evidence/,`worked example must not emit internal identifiers: ${label}`);
 }
});
test('Final 4.1 release history and current pins carry no GPT-5.5 publish plan',()=>{
 const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
 const dir=path.join(__dirname,'..','releases','final41-employment-proof');
 const digest=(f)=>crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
 const release=JSON.parse(fs.readFileSync(path.join(dir,'release.json'),'utf8'));
 const notes=JSON.parse(fs.readFileSync(path.join(__dirname,'..','releases','final41-notes-provenance','release.json'),'utf8'));
 const current=JSON.parse(fs.readFileSync(path.join(__dirname,'..','releases','final41-notes-paraphrase','release.json'),'utf8'));
 // Historical receipts remain immutable; each new baseline is the previous
 // published candidate, while the latest candidate binds the current source.
 assert.equal(release.candidate_parser_sha256,digest(path.join(dir,'parser-system.md')));
 assert.equal(notes.baseline_extractor_sha256,release.candidate_extractor_sha256);
 assert.equal(current.baseline_extractor_sha256,notes.candidate_extractor_sha256);
 assert.equal(current.candidate_extractor_sha256,digest(path.join(__dirname,'..','final-data-extractor.js')));
 for(const plan of [release,notes,current]){
  assert.equal(plan.candidate_parser_sha256,digest(path.join(dir,'parser-system.md')));
  assert.equal(plan.model,'gpt-4.1');
  assert.equal(plan.auto_publication,false);
 }
 const prompt=fs.readFileSync(path.join(dir,'parser-system.md'),'utf8');
 assert.doesNotMatch(prompt,/gpt-5\.5/i);
 // "Generalize" alone left 219355 free to restate the status; the replacement must stay actionable.
 assert.match(prompt,/Generated prose is not independent evidence of that status/);
 assert.match(prompt,/still needs employer verification before a termination cash distribution can move forward/);
 assert.match(prompt,/every conditional employer-verification route/);
});
