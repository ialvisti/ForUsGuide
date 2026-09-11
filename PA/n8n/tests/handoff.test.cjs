const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const base=path.resolve(__dirname,'..',process.env.PA_N8N_SOURCE||'candidates');
function run(input){return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+fs.readFileSync(path.join(base,'internal-handoff.js'),'utf8')+'\n})()',{$input:{first:()=>({json:input})},$:name=>{assert.equal(name,'Get fields1');return {first:()=>({json:{ticketId:'TKT-SIMULATION'}})}}},{timeout:1000})));}
test('Successful human review is a bounded internal handoff, not a thrown technical error',()=>{
 const result=run({ticket_job_id:'simulation-job',state:'succeeded',next_action:'human_review',metadata:{fallback:false},primary:{inquiry:'Where are the funds?',topic:'balance',generate_response:{response:{data_gaps:['Current custodian'],escalation:{needed:true,reason:'Verify individual disposition'}},metadata:{raw_notes:'PRIVATE_CANARY'}}},related:[]});
 assert.equal(result.json.handoff_only,true);assert.equal(result.json.visibility,'internal');assert.equal(result.json.tag,undefined);
 const p=JSON.parse(result.json.body);assert.equal(p.ticket_job_id,'simulation-job');assert.equal(p.participant_reply_safe,false);
 assert.equal(p.inquiries[0].escalation.reason,'Verify individual disposition');assert.equal(JSON.stringify(result).includes('PRIVATE_CANARY'),false);
});
for(const input of [{state:'failed',next_action:'human_review'},{state:'succeeded',next_action:'send_participant_reply'},{state:'succeeded',next_action:'human_review',metadata:{fallback:true}}])test('Do not disguise a different unsafe terminal result as a verified handoff '+JSON.stringify(input),()=>assert.throws(()=>run(input)));
