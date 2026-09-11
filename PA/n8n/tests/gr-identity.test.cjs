const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
function run(search, included) {
 const source=fs.readFileSync(path.join(__dirname,'../candidates/handle-ticket-identity.js'),'utf8');
 return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+source+'\n})()', {
  $: name => name === 'Participant Search' ? {first:()=>({json:search})} : {item:{json:included}},
 },{timeout:1000})));
}
const found=()=>({Result:'Participant was found',userData:{pptId:'111',planId:'222',companyName:'PRIVATE_CANARY'}});
const included=()=>({userData:{pptId:'111',planId:'222'},ticketData:{userEmail:'PRIVATE_CANARY'}});
test('Approved found-account criterion is carried only for the same participant and plan',()=>{
 const out=run(found(),included());
 assert.equal(out.identity_verified,true);assert.equal(out.identity_resolution_status,'matched');
 assert.equal(out.response_source_reason,'account_context_required');
 assert.deepEqual(out.provided_identifiers,[]);assert.equal(JSON.stringify(out).includes('PRIVATE_CANARY'),false);
});
for(const condition of ['not_found','participant_mismatch','plan_mismatch','no_selection','invalid_id']) {
 test('No verified-account assertion when '+condition,()=>{
  const s=found(),i=included();
  if(condition==='not_found')s.Result='Participant was NOT found';
  if(condition==='participant_mismatch')i.userData.pptId='333';
  if(condition==='plan_mismatch')i.userData.planId='444';
  if(condition==='no_selection')delete s.userData;
  if(condition==='invalid_id')s.userData.pptId=i.userData.pptId='null';
  assert.equal(run(s,i),null);
 });
}
