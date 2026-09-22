const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm');
const fs=require('node:fs');
const file=require('node:path').join(__dirname,'../email-artifact-workflow.js');
const api=fs.existsSync(file)?require(file):{};
const workId='don:core:dvrv-us-1:devo/synthetic:ticket/100001';
const aid='don:core:dvrv-us-1:devo/synthetic:artifact/1';
function source(){return {ticketId:'TKT-100001',work:{id:workId,type:'ticket',display_id:'TKT-100001',visibility:{label:'external'},title:'Question',body:'Question',created_date:'2026-09-16T00:00:00Z',created_by:{id:'don:synthetic:service_account:1',type:'service_account'}},pages:[{timeline_entries:[{id:'don:synthetic:timeline_entry:1',type:'timeline_comment',object:workId,visibility:'external',body:'Question',created_date:'2026-09-16T00:00:00Z',created_by:{id:'don:synthetic:rev_user:1',type:'rev_user'},artifacts:[{id:aid,file:{type:'message/rfc822',size:123}}]}]}]};}
function tasks(s){assert.equal(typeof api.selectArtifactTasks,'function');return api.selectArtifactTasks(s);}
test('Collector selects only bounded original-email archives from the authenticated conversation',()=>{
 assert.deepEqual(tasks(source()),[{id:aid,size:123}]);
 const s=source();s.pages[0].timeline_entries[0].artifacts.push({id:aid,file:{type:'message/rfc822',size:123}});assert.equal(tasks(s).length,1);
 for(const bad of [{id:aid,file:{type:'application/pdf',size:123}},{id:aid,file:{type:'message/rfc822',size:262145}},{id:'https://untrusted.invalid/a',file:{type:'message/rfc822',size:123}}]){
  const s=source();s.pages[0].timeline_entries[0].artifacts=[bad];assert.deepEqual(tasks(s),[]);
 }
});
test('Collector refuses invalid correlation before any artifact request',()=>{
 for(const mutate of [s=>s.work.display_id='TKT-OTHER',s=>s.pages[0].timeline_entries[0].object='don:other:ticket/1',s=>s.pages[0].timeline_entries[0].visibility='internal']){const s=source();mutate(s);assert.throws(()=>tasks(s));}
});
test('Collector bounds cumulative fetches and retains unsupported attachments for partial normalization',()=>{
 const s=source();s.pages[0].timeline_entries[0].artifacts=Array.from({length:12},(_,i)=>({id:aid+i,file:{type:'message/rfc822',size:262144}}));
 const result=tasks(s);assert.equal(result.length,4);assert.equal(s.pages[0].timeline_entries[0].artifacts.length,12);
 s.pages[0].timeline_entries[0].artifacts=s.pages[0].timeline_entries[0].artifacts.map(a=>({...a,file:{...a.file,size:10}}));assert.equal(tasks(s).length,10);
});
test('Repeated artifact ID with conflicting size is not trusted',()=>{
 const s=source();s.pages[0].timeline_entries[0].artifacts.push({id:aid,file:{type:'message/rfc822',size:124}});assert.throws(()=>tasks(s));
});
test('Collector preserves unmodified source and admits only correlated successful exact-size UTF8 downloads',()=>{
 assert.equal(typeof api.collectArtifactContents,'function');const s=source(),t=tasks(s);
 const body='x'.repeat(123),r={statusCode:200,body};
 const out=api.collectArtifactContents(s,t,[{id:aid,response:r}]);assert.deepEqual(out.artifactContents,[{id:aid,raw:body}]);assert.deepEqual(out.work,s.work);assert.deepEqual(out.pages,s.pages);
 for(const response of [{...r,statusCode:404},{...r,body:'short'},{...r,body:{}},{...r,body:'\ud800'.repeat(41)}]) assert.throws(()=>api.collectArtifactContents(s,t,[{id:aid,response}]));
 assert.throws(()=>api.collectArtifactContents(s,t,[{id:aid+'other',response:r}]));
 assert.throws(()=>api.collectArtifactContents(s,t,[{id:aid,response:r},{id:aid,response:r}]));
 assert.throws(()=>api.collectArtifactContents(s,t,[]));
 assert.deepEqual(api.collectArtifactContents(s,[],[]).artifactContents,[]);
});
test('Inline collector has only read operations, fixed locate origin and no credentials on storage download',()=>{
 assert.equal(typeof api.collectorWorkflow,'function');const flow=api.collectorWorkflow({id:'synthetic',name:'Existing DevRev'});
 const http=flow.nodes.filter(n=>n.type==='n8n-nodes-base.httpRequest');assert.equal(http.length,2);
 const locate=http.find(n=>n.name==='PA Email Locate'),download=http.find(n=>n.name==='PA Email Download');
 assert.equal(locate.parameters.url,'https://api.devrev.ai/artifacts.locate');assert.equal(locate.parameters.method,'GET');assert.deepEqual(locate.credentials,{httpHeaderAuth:{id:'synthetic',name:'Existing DevRev'}});
 assert.equal(download.parameters.authentication,'none');assert.equal(download.credentials,undefined);assert.equal(download.parameters.sendHeaders,undefined);
 for(const n of http){assert.equal(n.parameters.options.redirect.redirect.followRedirects,false);assert.equal(n.parameters.options.timeout,30000);}
 assert.equal(flow.nodes.filter(n=>n.type==='n8n-nodes-base.executeWorkflowTrigger').length,1);
 assert.ok(!flow.nodes.some(n=>/webhook|schedule|trigger/i.test(n.type)&&n.type!=='n8n-nodes-base.executeWorkflowTrigger'));
});
function run(code,input,nodes={},paired){return JSON.parse(JSON.stringify(vm.runInNewContext('(function(){'+code+'})()',{$input:{all:()=>input.map(json=>({json})),first:()=>({json:input[0]})},$:name=>({first:()=>({json:nodes[name]}),all:()=>nodes[name].map(json=>({json})),itemMatching:i=>({json:paired[i]})}),Buffer,Date},{timeout:1000})));}
test('Generated tasks, per-item URL pairing and aggregate run in hardened Code-node-compatible JavaScript',()=>{
 const s=source(),rows=run(api.tasksCode(),[s]);assert.equal(rows[0].json.id,aid);
 const urls=run(api.urlCode(),[{url:'https://devrev-prod-artifacts.s3-accelerate.dualstack.amazonaws.com/fixture?signature=synthetic'}],{},rows.map(x=>x.json));assert.equal(urls[0].json.id,aid);
 assert.throws(()=>run(api.urlCode(),[{url:'https://evil.invalid/file'}],{},rows.map(x=>x.json)));
 const out=run(api.aggregateCode(),[{statusCode:200,body:'x'.repeat(123)}],{'PA Email Input':s,'PA Email Tasks':rows.map(x=>x.json)},urls.map(x=>x.json));assert.equal(out[0].json.artifactContents[0].id,aid);assert.equal(JSON.stringify(out).includes('signature'),false);
 const empty=source();empty.pages[0].timeline_entries[0].artifacts=[];assert.equal(run(api.tasksCode(),[empty])[0].json.skip,true);
});
test('Producer and final generated adapters both carry hydrated bytes into the same canonical snapshot',()=>{
 const p=require('../conversation-workflow.js'),f=require('../canonical-final-workflow.js');
 assert.equal(typeof p.producerArtifactInputCode,'function');assert.equal(typeof p.producerHydratedSnapshotCode,'function');assert.equal(typeof f.artifactInputCode,'function');assert.equal(typeof f.hydratedSourceCode,'function');
 const s=source(),e=s.pages[0].timeline_entries[0];
 const raw='Content-Type: multipart/alternative; boundary=b\n\n--b\nContent-Type: text/plain; charset=utf-8\n\nQuestion\n--b\nContent-Type: text/html; charset=utf-8\n\n<div>Question</div>\n--b--\n';e.artifacts[0].file.size=Buffer.byteLength(raw);
 const base={'Get tickets from DevRev1':{work:s.work},'Get relevant tag1':{ticketId:s.ticketId}};
 const prepared=run(p.producerArtifactInputCode(),s.pages,base)[0].json;assert.deepEqual(prepared,s);
 const hydrated={...prepared,artifactContents:[{id:aid,raw}]};
 const producer=run(p.producerHydratedSnapshotCode(),[hydrated],base)[0].json.conversation_snapshot;
 assert.equal(producer.complete,true);assert.equal(JSON.stringify(producer).includes('Content-Type'),false);
 const selected={ticketId:s.ticketId,ticket_job_id:'a'.repeat(32),read_canonical:true};s.work.custom_fields={tnt__pa_execution_job:selected.ticket_job_id};
 const finalNodes={'PA Final Reference':selected,'PA Final Work':{work:s.work}};
 const finalPrepared=run(f.artifactInputCode(),s.pages,finalNodes)[0].json;assert.deepEqual(finalPrepared,s);
 const final=run(f.hydratedSourceCode(),[{...finalPrepared,artifactContents:hydrated.artifactContents}],finalNodes)[0].json;
 assert.equal(final.snapshot.complete,true);
 assert.equal(final.preimage,require('../conversation-snapshot.js').canonicalConversation(producer));
 selected.ticket_job_id='b'.repeat(32);assert.throws(()=>run(f.artifactInputCode(),s.pages,finalNodes),/reference mismatch/);
});
test('Generated aggregator follows native paired-item links when HTTP outputs are reordered and rejects dropped items',()=>{
 const s=source(),tasks=[{id:aid,size:3},{id:aid+'2',size:4}];
 const nodes={'PA Email Input':s,'PA Email Tasks':tasks};
 const out=run(api.aggregateCode(),[{statusCode:200,body:'two2'},{statusCode:200,body:'one'}],nodes,[tasks[1],tasks[0]])[0].json;
 assert.deepEqual(out.artifactContents,[{id:aid+'2',raw:'two2'},{id:aid,raw:'one'}]);
 assert.throws(()=>run(api.aggregateCode(),[{statusCode:200,body:'one'}],nodes,[tasks[0]]),/incomplete_email_download/);
});
test('A failed independent artifact read cannot inherit the producer completeness decision',()=>{
 const s=source(),tasks=api.selectArtifactTasks(s);
 assert.throws(()=>api.collectArtifactContents(s,tasks,[{id:aid,response:{statusCode:404,body:''}}]),/invalid_email_download/);
 const incomplete=require('../devrev-conversation.js').snapshotFromDevRev({...s,capturedAt:new Date().toISOString()});
 assert.equal(incomplete.complete,false);assert.equal(incomplete.partial,true);
});
