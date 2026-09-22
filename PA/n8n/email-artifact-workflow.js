'use strict';
// Pure selection/aggregation plus a native read-only n8n collector definition.
// DevRev credentials are references supplied from the existing authenticated
// source node. They are never forwarded to signed object storage.
const {snapshotFromDevRev}=require('./devrev-conversation.js');
const {validateArtifactDownloadUrl}=require('./devrev-email-mirror.js');
function selectArtifactTasks(source){
 snapshotFromDevRev({...source,artifactContents:[],capturedAt:new Date().toISOString()});
 const rows=[source.work,...source.pages.flatMap(p=>p.timeline_entries)],seen=new Map();
 for(const row of rows)for(const a of row.artifacts||[]){
  if(!a || typeof a.id!=='string' || !/^don:core:[A-Za-z0-9-]+:devo\/[A-Za-z0-9]+:artifact\/[0-9]+$/.test(a.id) ||
    a.file?.type!=='message/rfc822' || !Number.isSafeInteger(a.file?.size) || a.file.size<1 || a.file.size>262144)continue;
  if(seen.has(a.id)&&seen.get(a.id)!==a.file.size)throw new Error('inconsistent_email_artifact');
  seen.set(a.id,a.file.size);
 }
 const tasks=[];let bytes=0;
 for(const [id,size] of seen){
  if(tasks.length===10 || bytes+size>1048576)continue;
  tasks.push({id,size});bytes+=size;
 }
 return tasks;
}
function collectArtifactContents(source,tasks,downloads){
 if(!Array.isArray(tasks)||tasks.length>10||!Array.isArray(downloads)||downloads.length!==tasks.length)
  throw new Error('incomplete_email_download');
 const seen=new Set(),artifactContents=[];let bytes=0;
 for(const {id,response} of downloads){
  const task=tasks.find(t=>t.id===id);
  if(!task||seen.has(id)||response?.statusCode!==200||typeof response.body!=='string'||
    !response.body.isWellFormed()||Buffer.byteLength(response.body,'utf8')!==task.size)
   throw new Error('invalid_email_download');
  seen.add(id);bytes+=task.size;if(bytes>1048576)throw new Error('oversized_email_download');
  artifactContents.push({id,raw:response.body});
 }
 return {work:source.work,pages:source.pages,ticketId:source.ticketId,artifactContents};
}
function tasksCode(){return require('./conversation-workflow.js').normalizerCode()+selectArtifactTasks.toString()+`
if($input.all().length!==1)throw new Error('invalid_email_source_count');
const tasks=selectArtifactTasks($input.first().json);
return tasks.length?tasks.map(json=>({json})):[{json:{skip:true}}];
`;}
function urlCode(){return validateArtifactDownloadUrl.toString()+`
return $input.all().map((item,index)=>{
 const task=$('PA Email Tasks').itemMatching(index).json;
 return {json:{id:task.id,size:task.size,url:validateArtifactDownloadUrl(item.json.url)},pairedItem:{item:index}};
});
`;}
function aggregateCode(){return collectArtifactContents.toString()+`
const source=$('PA Email Input').first().json;
const tasks=$('PA Email Tasks').all().map(i=>i.json).filter(t=>!t.skip);
const downloads=tasks.length?$input.all().map((item,index)=>({
 id:$('PA Email URL').itemMatching(index).json.id,response:item.json})):[];
return [{json:collectArtifactContents(source,tasks,downloads)}];
`;}
function collectorWorkflow(credential){
 if(!credential||typeof credential.id!=='string'||typeof credential.name!=='string')throw new Error('missing_existing_devrev_credential');
 const nodes=[],connections={};
 const add=(name,type,typeVersion,parameters,position,extras={})=>nodes.push({id:name.toLowerCase().replaceAll(' ','-'),name,type,typeVersion,parameters,position,...extras});
 const connect=(from,to,index=0)=>{connections[from]??={main:[]};connections[from].main[index]??=[];connections[from].main[index].push({node:to,type:'main',index:0});};
 add('PA Email Input','n8n-nodes-base.executeWorkflowTrigger',1.1,{inputSource:'passthrough'},[0,0]);
 add('PA Email Tasks','n8n-nodes-base.code',2,{jsCode:tasksCode()},[220,0]);
 add('PA Email Required?','n8n-nodes-base.if',2.3,{conditions:{options:{caseSensitive:true,leftValue:'',typeValidation:'strict',version:3},conditions:[{id:'pa-email-needed',leftValue:'={{ !$json.skip }}',rightValue:'',operator:{type:'boolean',operation:'true',singleValue:true}}],combinator:'and'},options:{}},[440,0]);
 const options={redirect:{redirect:{followRedirects:false}},timeout:30000};
 add('PA Email Locate','n8n-nodes-base.httpRequest',4.2,{method:'GET',url:'https://api.devrev.ai/artifacts.locate',authentication:'genericCredentialType',genericAuthType:'httpHeaderAuth',sendQuery:true,queryParameters:{parameters:[{name:'id',value:'={{ $json.id }}'}]},options},[660,-100],{credentials:{httpHeaderAuth:{id:credential.id,name:credential.name}}});
 add('PA Email URL','n8n-nodes-base.code',2,{jsCode:urlCode()},[880,-100]);
 add('PA Email Download','n8n-nodes-base.httpRequest',4.2,{method:'GET',url:'={{ $json.url }}',authentication:'none',options:{...options,response:{response:{fullResponse:true,responseFormat:'text',outputPropertyName:'body'}}}},[1100,-100]);
 add('PA Email Collected','n8n-nodes-base.code',2,{jsCode:aggregateCode()},[1320,0]);
 connect('PA Email Input','PA Email Tasks');connect('PA Email Tasks','PA Email Required?');
 connect('PA Email Required?','PA Email Locate');connect('PA Email Required?','PA Email Collected',1);
 connect('PA Email Locate','PA Email URL');connect('PA Email URL','PA Email Download');connect('PA Email Download','PA Email Collected');
 return {name:'PA bounded email archive reads',nodes,connections,settings:{executionOrder:'v1'}};
}
function collectorParameters(credential){return {source:'parameter',workflowJson:JSON.stringify(collectorWorkflow(credential)),mode:'once',options:{waitForSubWorkflow:true}};}
module.exports={selectArtifactTasks,collectArtifactContents,tasksCode,urlCode,aggregateCode,collectorWorkflow,collectorParameters};
