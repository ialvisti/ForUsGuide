const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const base=path.resolve(__dirname,'..',process.env.PA_N8N_SOURCE || 'candidates');
function run(original,polished,ticket='TKT-SIMULATION'){
 const source=fs.readFileSync(path.join(base,'final-data-extractor.js'),'utf8');
 return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+source+'\n})()',{
  $input:{first:()=>({json:{output:JSON.stringify(polished)}})},
  $:name=>{assert.equal(name,'Webhook');return {first:()=>({json:{body:{ticketId:ticket,agentResponse:JSON.stringify(original)}}})};}
 },{timeout:1000})))[0].json;
}
const original=()=>({participant_reply:'First answer.\nSecond answer.',set_stage_solved:false,stage_reason:'Internal verification is pending.',internal_notes:'Verify plan rule.'});
test('Privacy formatter cannot promote a review into a solved recommendation',()=>{
 const src=original();const p={...src,ticketId:'TKT-SIMULATION',set_stage_solved:true};
 const out=run(src,p);
 assert.equal(out.set_stage_solved,false);assert.equal(out.human_review_required,true);
 assert.match(out.stage_reason,/review/i);
});
test('Ticket identity is taken from the transport and a model mismatch stops before writes',()=>{
 assert.throws(()=>run(original(),{...original(),ticketId:'TKT-DIFFERENT'}),/correlation/i);
});
test('Strings cannot authorize a solved recommendation',()=>{
 assert.equal(run({...original(),set_stage_solved:'true'},{...original(),ticketId:'TKT-SIMULATION',set_stage_solved:'true'}).set_stage_solved,false);
});
test('Preserve internal draft and paragraph rendering without treating it as a sent reply',()=>{
 const out=run(original(),{...original(),ticketId:'TKT-SIMULATION'});
 assert.equal(out.ticketId,'TKT-SIMULATION');assert.equal(out.participant_reply,'First answer.\n\u3164\n\nSecond answer.');
 assert.equal(out.participant_reply_safe,false);assert.equal(out.publication_authorized,false);
});
test('A model-only solved recommendation is advisory until independently verified',()=>{
 const src={...original(),set_stage_solved:true,stage_reason:'Answered',internal_notes:null};
 const out=run(src,{...src,ticketId:'TKT-SIMULATION'});
 assert.equal(out.model_recommended_solved,true);assert.equal(out.set_stage_solved,false);
 assert.equal(out.publication_authorized,false);
});
