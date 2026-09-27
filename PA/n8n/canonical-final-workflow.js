'use strict';
// Local source builder. Workflow nodes contain only the generated closed contract,
// never filesystem access, dynamic code generation, URLs from events, or secrets.
const fs=require('node:fs'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,name),'utf8');
const transportSource=read('final-transport.js');
const normalizeFinalTransport=new Function(transportSource+'\nreturn normalizeFinalTransport;')();
function selectFinalReference(webhook){
 const transport=normalizeFinalTransport(webhook);
 const ticket=webhook?.headers?.['x-pa-ticket-id'];
 const job=webhook?.headers?.['x-pa-ticket-job-id'];
 const selected=typeof ticket==='string' && ticket===transport.ticketId &&
   typeof job==='string' && /^[0-9a-f]{32}$/.test(job);
 return {...transport,ticket_job_id:selected?job:null,read_canonical:selected,
  reference_reason:selected?null:'missing_independent_job_reference'};
}
function validateWorkReference(reference,work){
 if(!reference?.read_canonical || !work || work.type!=='ticket' ||
   work.display_id!==reference.ticketId ||
   work.custom_fields?.tnt__pa_execution_job!==reference.ticket_job_id){
  throw new Error('PA current work execution reference mismatch');
 }
 return work;
}
function decodeCanonicalPoll(response){
 const bad=()=>{throw new Error('invalid_canonical_poll');};
 if(!response || response.statusCode!==200 || typeof response.body!=='string' ||
   Buffer.byteLength(response.body,'utf8')>1048576 ||
   typeof response.headers?.['content-type']!=='string' ||
   !/^application\/json(?:\s*;|$)/i.test(response.headers['content-type']))bad();
 try{return JSON.parse(response.body);}catch(_){return bad();}
}
function referenceCode(){return transportSource+'\n'+selectFinalReference.toString()+
 "\nreturn [{json:selectFinalReference($('Webhook').first().json)}];\n";}
function sourceCode(){return require('./conversation-workflow.js').normalizerCode()+
 validateWorkReference.toString()+`
const selected=$('PA Final Reference').first().json;
const work=validateWorkReference(selected,$('PA Final Work').first().json.work);
const snapshot=snapshotFromDevRev({work,ticketId:selected.ticketId,
 pages:$input.all().map(item=>item.json),capturedAt:new Date().toISOString()});
return [{json:{snapshot,preimage:canonicalConversation(snapshot)}}];
`;}
function artifactInputCode(){return validateWorkReference.toString()+`
const selected=$('PA Final Reference').first().json;
const work=validateWorkReference(selected,$('PA Final Work').first().json.work);
return [{json:{work,ticketId:selected.ticketId,pages:$input.all().map(item=>item.json)}}];
`;}
function hydratedSourceCode(){return require('./conversation-workflow.js').normalizerCode()+
 validateWorkReference.toString()+`
const selected=$('PA Final Reference').first().json;
const source=$input.first().json;
validateWorkReference(selected,source.work);
if(source.ticketId!==selected.ticketId)throw new Error('invalid_devrev_conversation');
const snapshot=snapshotFromDevRev({...source,capturedAt:new Date().toISOString()});
return [{json:{snapshot,preimage:canonicalConversation(snapshot)}}];
`;}
function evidenceCode(){
 const validator=read('canonical-evidence.js').replace('module.exports = {validateCanonicalEvidence};','return {validateCanonicalEvidence};');
 return 'const {validateCanonicalEvidence}=(()=>{'+validator+'\n})();\n'+decodeCanonicalPoll.toString()+`
const selected=$('PA Final Reference').first().json;
let evidence;
if(!selected.read_canonical){
 evidence={evidence_status:'unavailable',reason_codes:[selected.reference_reason],verified_participant_facts:null,
 publication_authorized:false,participant_reply_safe:false,set_stage_solved:false,human_review_required:true};
}else{
 const bound=$('PA Final Conversation Hash').first().json;
 const snapshot=bound.snapshot;
 const conversation_reference={type:snapshot.type,schema_version:snapshot.schema_version,
 hash_algorithm:'sha256',digest:bound.digest,complete:snapshot.complete,partial:snapshot.partial,truncated:snapshot.truncated};
 const reference={ticket_id:selected.ticketId,ticket_job_id:selected.ticket_job_id,conversation_reference};
 evidence=validateCanonicalEvidence({reference,poll:decodeCanonicalPoll($input.first().json),now:new Date().toISOString()});
 evidence.execution_reference=reference;
}
return [{json:{ticketId:selected.ticketId,agentResponse:selected.agentResponse,canonical_evidence:evidence}}];
`;
}
function parserInputCode(){return `const source=$('PA Final Evidence').first().json;
return JSON.stringify({ticketId:source.ticketId,agentResponse:source.agentResponse,canonical_evidence:source.canonical_evidence});\n`;}
// Returned verbatim and pinned by the manifest to the accepted live Data extractor
// hash. The module's own header still reads "unpublished/NOT APPLIED"; that comment is
// stale and cannot be corrected without breaking byte-identity with the served node.
function extractorCode(){return read('final-data-extractor.js');}
module.exports={selectFinalReference,validateWorkReference,decodeCanonicalPoll,
 referenceCode,sourceCode,artifactInputCode,hydratedSourceCode,evidenceCode,parserInputCode,extractorCode};
