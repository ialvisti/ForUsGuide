'use strict';

// The adapter must supply authenticated works.get and fixed-origin external
// timeline responses. Its cursor paginator must retain every response in order.
// This normalizer does not authenticate arbitrary JSON or fetch network URLs.
const {canonicalConversation}=require('./conversation-snapshot.js');
const invalid=()=>{throw new Error('invalid_devrev_conversation');};
const object=v=>v!==null && typeof v==='object' && !Array.isArray(v);
function author(value){
  if(!object(value) || typeof value.id!=='string')return {author_id:null,author_role:'unknown'};
  return {author_id:value.id,author_role:{rev_user:'participant',dev_user:'advisor',
    service_account:'automation',sys_user:'automation',bot:'automation'}[value.type]||'unknown'};
}
function date(value){
  if(typeof value!=='string')invalid();
  // DevRev uses UTC; preserve microseconds rather than rounding via JS Date.
  return value.replace(/\+00:00$/,'Z');
}
function snapshotFromDevRev({work,ticketId,pages,capturedAt}){
  if(!object(work) || work.type!=='ticket' || work.display_id!==ticketId ||
    work.visibility?.label!=='external' ||
    typeof work.title!=='string' || !(work.body==null || typeof work.body==='string') ||
    !Array.isArray(pages) || pages.length<1 || pages.length>10)invalid();
  const messages=[],cursors=new Set();
  let partial=false,truncated=false;
  // Attachments have not been hydrated into this text contract.
  const hasAttachments=item=>Array.isArray(item.artifacts) && item.artifacts.length>0;
  if(hasAttachments(work))partial=true;
  for(let index=0;index<pages.length;index++){
    const page=pages[index];
    if(!object(page) || !Array.isArray(page.timeline_entries))invalid();
    const cursor=page.next_cursor;
    if(cursor!=null && (typeof cursor!=='string' || !cursor))invalid();
    if(cursor){
      if(cursors.has(cursor))invalid();
      cursors.add(cursor);
      if(index===pages.length-1)partial=true;
    }else if(index!==pages.length-1)invalid();
    for(const entry of page.timeline_entries){
      if(!object(entry) || entry.type!=='timeline_comment' || entry.object!==work.id ||
        entry.visibility!=='external' || typeof entry.body!=='string')invalid();
      if(hasAttachments(entry))partial=true;
      messages.push({id:entry.id,...author(entry.created_by),visibility:'external',
        created_at:date(entry.created_date),updated_at:entry.modified_date==null?null:date(entry.modified_date),
        body:entry.body});
    }
  }
  // Refuse oversized history instead of silently dropping a participant turn.
  if(messages.length>250)invalid();
  const snapshot={type:'devrev_conversation_snapshot',schema_version:1,ticket_id:ticketId,
    work_id:work.id,subject:work.title.trim(),captured_at:date(capturedAt),
    complete:!partial,partial,truncated,
    initial_message:{id:work.id,...author(work.created_by),visibility:'external',
      created_at:date(work.created_date),updated_at:null,body:(work.body||'').trim()},messages};
  // Validates dates, attribution, bounds, duplicate IDs and canonical encoding.
  canonicalConversation(snapshot);
  snapshot.messages.sort((a,b)=>{
    // Normalize fractional width for exact UTC chronology; IDs are ASCII DONs.
    const key=v=>v.replace(/(?:\.(\d+))?Z$/,(_,f)=>'.'+(f||'').padEnd(6,'0')+'Z');
    const left=key(a.created_at),right=key(b.created_at);
    return left<right?-1:left>right?1:a.id<b.id?-1:a.id>b.id?1:0;
  });
  return snapshot;
}
module.exports={snapshotFromDevRev};
