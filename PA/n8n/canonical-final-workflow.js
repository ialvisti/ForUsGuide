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
function extractorCode(){return `
const source=$('PA Final Evidence').first().json;
const parsed=JSON.parse($input.first().json.output);
const keys=['ticketId','participant_reply','set_stage_solved','stage_reason','internal_notes'];
if(!parsed || typeof parsed!=='object' || Array.isArray(parsed) ||
 Object.keys(parsed).length!==keys.length || keys.some(k=>!Object.hasOwn(parsed,k)) ||
 parsed.ticketId!==source.ticketId || typeof parsed.participant_reply!=='string' ||
 typeof parsed.stage_reason!=='string' || typeof parsed.set_stage_solved!=='boolean' ||
 !(parsed.internal_notes===null || typeof parsed.internal_notes==='string'))
 throw new Error('Invalid PA final draft contract; no ticket write permitted');
const evidence=source.canonical_evidence;
if(!evidence || evidence.publication_authorized!==false || evidence.human_review_required!==true)
 throw new Error('Missing independent canonical evidence decision');
const provenance=JSON.stringify({evidence_status:evidence.evidence_status,reason_codes:evidence.reason_codes,
 execution_reference:evidence.execution_reference??null,verified_participant_facts:evidence.verified_participant_facts});
const internal=[parsed.internal_notes,'Independent backend evidence: '+provenance].filter(Boolean).join('\\n\\n');
const doubleBreaks=str=>str.replace(/\\n/g,'\\n\\u3164\\n\\n');
return [{json:{ticketId:source.ticketId,participant_reply:doubleBreaks(parsed.participant_reply),
 set_stage_solved:false,stage_reason:'Advisor review is required. '+parsed.stage_reason,
 internal_notes:doubleBreaks(internal),model_recommended_solved:parsed.set_stage_solved,
 canonical_evidence:evidence,verified_participant_facts:evidence.verified_participant_facts,
 human_review_required:true,participant_reply_safe:false,publication_authorized:false,
 verification_reason:evidence.evidence_status==='matched'?'Exact job and conversation matched; generated prose still requires advisor review.':
 'Canonical evidence is incomplete or unavailable; generated prose is not factual authority.'}}];
`;}
module.exports={selectFinalReference,validateWorkReference,decodeCanonicalPoll,
 referenceCode,sourceCode,evidenceCode,parserInputCode,extractorCode};
