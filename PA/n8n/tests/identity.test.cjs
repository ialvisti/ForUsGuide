const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const base=path.resolve(__dirname,'..',process.env.PA_N8N_SOURCE||'candidates');
function run(name,input,nodes={}){
 return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+fs.readFileSync(path.join(base,name+'.js'),'utf8')+'\n})()',{$input:{first:()=>({json:input})},$:name=>({isExecuted:Object.hasOwn(nodes,name),first:()=>{if(!Object.hasOwn(nodes,name))throw Error('Node has not executed');return {json:nodes[name]};}})},{timeout:1000}))).json;
}
const company={'Relevant information found?1':{output:{company_name:'Synthetic Employer'}}};
test('Multiple candidates with an employer reach comparison instead of unreachable branch',()=>{
 const p=run('validate-match',{state:'succeeded',result:{data:{count:2}}},company);
 assert.equal(p.matchStatus,'AI Needed');assert.equal(p.identity_resolution_status,'ambiguous');
});
for(const count of [null,'',false,'error',-1,1.5])test('Invalid count is not a zero-result lookup: '+String(count),()=>{
 const p=run('validate-match',{state:'succeeded',result:{data:{count}}},company);
 assert.equal(p.identity_resolution_status,'access_error');assert.equal(p.matchStatus,'No Match Found');
});
test('Explicit numeric zero is a completed empty lookup',()=>{
 assert.equal(run('validate-match',{state:'succeeded',result:{data:{count:0}}},company).identity_resolution_status,'not_found');
});
test('One candidate preserves route but does not certify identity',()=>{
 const p=run('validate-match',{state:'succeeded',result:{data:{count:'1'}}},company);
 assert.equal(p.matchStatus,'Match Found');assert.equal(p.identity_verified,false);
});
for(const [state,count,expected] of [['succeeded',0,'not_found'],['succeeded',2,'ambiguous'],['failed',null,'access_error']])test('Unresolved result preserves lookup evidence: '+expected,()=>{
 const p=run('identity-unresolved',{}, {'Get Job1':{state,result:{data:{count}}}});
 assert.equal(p.Result,'Participant was NOT found');assert.equal(p.identity_context.identity_resolution_status,expected);
 assert.equal(p.identity_context.identity_verified,false);assert.equal(p.human_review_required,true);
});
test('No executed lookup does not claim the database returned zero',()=>{
 assert.equal(run('identity-unresolved',{}).identity_context.identity_resolution_status,'ambiguous');
});
test('Candidate records from either search preserve ambiguity even if a second search is empty',()=>{
 const p=run('identity-unresolved',{}, {'Get Job':{state:'succeeded',result:{data:{count:2}}},'Get Job1':{state:'succeeded',result:{data:{count:0}}}});
 assert.equal(p.identity_context.identity_resolution_status,'ambiguous');
});
test('Available identity clue types survive without their private values',()=>{
 const p=run('identity-unresolved',{}, {
  'Participant Verifier':{userName:'Synthetic Person',userEmail:'synthetic@example.test',ticket_messages:{message_1:'I worked at Synthetic Employer.'}},
  'Gather Info Specialist1':{output:{company_name:'Synthetic Employer'}},
 });
 assert.deepEqual(p.identity_context.provided_identifiers,['name','email','employer']);
 assert.equal(JSON.stringify(p).includes('synthetic@example.test'),false);
 assert.equal(JSON.stringify(p).includes('Synthetic Person'),false);
});
test('A company inferred only by the model is not an identifier already provided',()=>{
 const p=run('identity-unresolved',{}, {'Participant Verifier':{userName:'not found',userEmail:'unknown',ticket_messages:{message_1:'Help me.'}},'Gather Info Specialist1':{output:{company_name:'Invented Employer'}}});
 assert.deepEqual(p.identity_context.provided_identifiers,[]);
});
