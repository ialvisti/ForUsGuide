'use strict';

// Shared wire contract with data_pipeline/ticket_conversation.py. This accepts
// messages obtained by an authenticated producer, never a digest from an LLM.
const fail = () => { throw new Error('invalid_conversation_snapshot'); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value) &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value));
const text = (value, min, max) => typeof value === 'string' && value.isWellFormed() &&
  Array.from(value).length >= min && Array.from(value).length <= max;
const devrevId = value => text(value,1,256) && /^don:[A-Za-z0-9_.:/+-]+$/.test(value);
function keys(value, required, optional=[]) {
  if (!object(value) || required.some(k=>!Object.hasOwn(value,k)) ||
      Object.keys(value).some(k=>![...required,...optional].includes(k))) fail();
}
function instant(value) {
  if (typeof value !== 'string') fail();
  const m=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/.exec(value);
  if (!m) fail();
  const [year,month,day,hour,minute,second]=m.slice(1,7).map(Number);
  const leap=year%4===0 && (year%100!==0 || year%400===0);
  if (year<1 || month<1 || month>12 || day<1 ||
      day>[31,leap?29:28,31,30,31,30,31,31,30,31,30,31][month-1] ||
      hour>23 || minute>59 || second>59) fail();
  return BigInt(Date.parse(value.slice(0,19)+'Z'))*1000n+BigInt((m[7]||'').padEnd(6,'0'));
}
function row(m, captured) {
  keys(m,['id','author_role','visibility','created_at','updated_at','body'],['author_id']);
  if (!devrevId(m.id) || !text(m.body,0,100000) ||
      !['participant','advisor','automation','unknown'].includes(m.author_role) ||
      m.visibility!=='external' ||
      (m.author_id != null && !devrevId(m.author_id)) ||
      (m.author_role!=='unknown' && m.author_id == null)) fail();
  if (instant(m.created_at)>captured || (m.updated_at!==null &&
      (instant(m.created_at)>instant(m.updated_at) || instant(m.updated_at)>captured))) fail();
  return [m.id,m.author_id??null,m.author_role,m.visibility,m.created_at,m.updated_at,m.body];
}
function canonicalConversation(snapshot) {
  keys(snapshot,['type','schema_version','ticket_id','work_id','subject','captured_at',
    'complete','partial','truncated','initial_message','messages']);
  const s=snapshot;
  if (s.type!=='devrev_conversation_snapshot' || s.schema_version!==1 ||
      !text(s.ticket_id,1,64) || !/^TKT-[A-Z0-9-]+$/.test(s.ticket_id) ||
      !text(s.work_id,1,256) || !/^don:[A-Za-z0-9_.:/+-]+$/.test(s.work_id) ||
      !text(s.subject,0,1000) || !['complete','partial','truncated'].every(k=>typeof s[k]==='boolean') ||
      s.complete===(s.partial||s.truncated) || !Array.isArray(s.messages) || s.messages.length>250) fail();
  const captured=instant(s.captured_at), initial=row(s.initial_message,captured);
  if (initial[0]!==s.work_id) fail();
  const rows=s.messages.map(m=>row(m,captured));
  if (new Set([initial[0],...rows.map(r=>r[0])]).size!==rows.length+1 ||
      [initial,...rows].reduce((n,r)=>n+Buffer.byteLength(r[6],'utf8'),0)>200000) fail();
  rows.sort((a,b)=>instant(a[4])<instant(b[4])?-1:instant(a[4])>instant(b[4])?1:
    a[0]<b[0]?-1:a[0]>b[0]?1:0);
  const preimage=[s.type,s.schema_version,s.ticket_id,s.work_id,s.subject,
    s.complete,s.partial,s.truncated,initial,rows];
  return JSON.stringify(preimage);
}
function conversationReference(s) {
  // Local verifier only. The n8n adapter must use canonicalConversation plus its built-in
  // Crypto node (SHA256/HEX), without widening the Code module allowlist.
  const {createHash}=require('node:crypto');
  return {type:s.type,schema_version:s.schema_version,hash_algorithm:'sha256',
    digest:createHash('sha256').update(canonicalConversation(s),'utf8').digest('hex'),
    complete:s.complete,partial:s.partial,truncated:s.truncated};
}
module.exports={conversationReference,canonicalConversation};
