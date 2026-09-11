const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const base=path.resolve(__dirname,'..',process.env.PA_N8N_SOURCE||'candidates');
function run(context){
 const nodes={'Parse response':{question:'Where is my account?'},'Get fields1':{ticketId:'TKT-SIMULATION'},'Participant Search':{identity_context:context}};
 return JSON.parse(vm.runInNewContext('(function(){'+fs.readFileSync(path.join(base,'knowledge-question-body.js'),'utf8')+'\n})()',{$:name=>({first:()=>({json:nodes[name]})})},{timeout:1000}));
}
for(const [status,reason] of [['not_found','account_not_found'],['ambiguous','account_ambiguous'],['access_error','account_lookup_failed']])test('KQ HTTP body preserves identity '+status,()=>{
 const context={identity_resolution_status:status,response_source_reason:reason,identity_verified:false,provided_identifiers:['name','employer']};
 const result=run(context);assert.equal(result.question,'Where is my account?');assert.equal(result.ticket_id,'TKT-SIMULATION');assert.deepEqual(result.identity_context,context);
});
test('Missing context on the verified account-fallback branch cannot become educational by omission',()=>{
 const result=run(undefined);assert.equal(result.identity_context.identity_resolution_status,'ambiguous');assert.equal(result.identity_context.response_source_reason,'account_context_required');
});
