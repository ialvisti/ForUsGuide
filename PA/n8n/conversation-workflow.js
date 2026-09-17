'use strict';
// Build self-contained n8n Code node sources from the shared, tested contract.
// This file performs no workflow or network writes.
const fs=require('node:fs');
const path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,name),'utf8');
function codecCode(){return read('conversation-snapshot.js').split('function conversationReference(s)')[0];}
function normalizerCode(){
  const source=read('devrev-conversation.js').replace("const {canonicalConversation}=require('./conversation-snapshot.js');",'');
  return `const {canonicalConversation}=(()=>{${codecCode()}\nreturn {canonicalConversation};})();\n`+
    `const {snapshotFromDevRev}=(()=>{${source.replace('module.exports={snapshotFromDevRev};','return {snapshotFromDevRev};')}\n})();\n`;
}
function producerSnapshotCode(){return normalizerCode()+`
const base=$('Get relevant tag1').first().json;
const snapshot=snapshotFromDevRev({work:$('Get tickets from DevRev1').first().json.work,
  ticketId:base.ticketId,pages:$input.all().map(item=>item.json),capturedAt:new Date().toISOString()});
return [{json:{...base,emailSubject:snapshot.subject,emailBody:snapshot.initial_message.body,
  conversation_snapshot:snapshot}}];
`;}
function includeTicketCode(){return `
function fnv1a64(value){
 let hash=0xcbf29ce484222325n;
 for(let i=0;i<value.length;i++){
  const unit=value.charCodeAt(i);
  hash=BigInt.asUintN(64,(hash^BigInt(unit&255))*0x100000001b3n);
  hash=BigInt.asUintN(64,(hash^BigInt(unit>>>8))*0x100000001b3n);
 }
 return hash.toString(16).padStart(16,'0');
}
const {userData,fubData:forusbots}=$input.first().json;
const original=$('Obtain the real message from the PPT').first().json;
const snapshot=original.conversation_snapshot;
const ticketId=$('Get fields1').first().json.ticketId;
if(!snapshot || snapshot.ticket_id!==ticketId)throw new Error('PA conversation correlation failed');
const ticketData={...$('Code').first().json,ticketId,emailSubject:snapshot.subject,
 emailBody:snapshot.initial_message.body,conversation_snapshot:snapshot};
delete ticketData.ticket_messages;
const envelope={participant_id:userData.pptId??'',plan_id:userData.planId??'',
 company_name:userData.companyName??'',company_status:userData.companyStatus??'',
 company_status_detail:userData.companyStatusDetail??'',record_keeper:forusbots.recordKeeper??'',
 ticket_handler_mode:'full',max_response_tokens:5500,ticket:{
 username:ticketData.userName??'',user_email:ticketData.userEmail??'',email_subject:ticketData.emailSubject,
 email_body:ticketData.emailBody,conversation_snapshot:{...snapshot,captured_at:undefined},
 tag:null,ticket_id:ticketId,first_contact:null}};
return {userData,ticketData,forusbots,idempotencyKey:'n8n-handle-v2-'+fnv1a64(JSON.stringify(envelope))};
`;}
function typedTicketBody(source){
 const expression=/"ticket_messages": \{\{[^\n]*\}\},/g;
 if((source.match(expression)||[]).length!==1)throw new Error('unexpected_ticket_body');
 return source.replace(expression,'"conversation_snapshot": {{ JSON.stringify($(\'Include Ticket data\').item.json.ticketData.conversation_snapshot) }},');
}
function updateJobBody(source){
 const anchor='"tnt__workflow_status": "Context Enriched"';
 if(source.split(anchor).length!==2)throw new Error('unexpected_update_body');
 return source.replace(anchor,anchor+`,
    "tnt__pa_execution_job": {{ JSON.stringify((() => {
      let accepted;
      try { accepted=$('Handle Ticket').first().json; } catch (_) { return null; }
      if(!accepted || typeof accepted.ticket_job_id!=='string' || !/^[0-9a-f]{32}$/.test(accepted.ticket_job_id))
        throw new Error('PA execution reference missing');
      return accepted.ticket_job_id;
    })()) }}`);
}
module.exports={codecCode,normalizerCode,producerSnapshotCode,includeTicketCode,typedTicketBody,updateJobBody};
