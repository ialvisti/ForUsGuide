const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const file=path.join(__dirname,'../devrev-conversation.js');
const api=fs.existsSync(file)?require(file):{};
function source(){return {
  ticketId:'TKT-100001',capturedAt:'2026-09-17T01:00:00.000Z',
  work:{id:'don:synthetic:ticket:1',display_id:'TKT-100001',type:'ticket',visibility:{label:'external'},
    title:' Synthetic question ',body:' Original question ',
    created_date:'2026-09-16T01:00:00.000Z',modified_date:'2026-09-17T00:00:00.000Z',
    created_by:{id:'don:synthetic:service_account:1',type:'service_account'},
    reported_by:[{id:'don:synthetic:rev_user:1',type:'rev_user'}]},
  pages:[{timeline_entries:[{type:'timeline_comment',id:'don:synthetic:timeline_entry:1',
    object:'don:synthetic:ticket:1',visibility:'external',body:'Later question?',
    created_date:'2026-09-16T02:00:00.000Z',
    created_by:{id:'don:synthetic:rev_user:1',type:'rev_user'}}]}]
};}
function build(value){assert.equal(typeof api.snapshotFromDevRev,'function');return api.snapshotFromDevRev(value);}
test('Typed hydration preserves original author rather than substituting reporter',()=>{
  const result=build(source());
  assert.equal(result.initial_message.author_role,'automation');
  assert.equal(result.initial_message.updated_at,null);
  assert.equal(result.messages[0].author_role,'participant');
  assert.equal(result.subject,'Synthetic question');
  assert.equal(result.initial_message.body,'Original question');
  assert.equal(result.complete,true);
});
test('All pages are combined and sorted with complete cursor termination',()=>{
  const value=source(),first=value.pages[0].timeline_entries[0];
  value.pages=[{timeline_entries:[{...first,id:'don:synthetic:timeline_entry:2',
    created_date:'2026-09-16T03:00:00.000Z'}],next_cursor:'opaque-page-two'},value.pages[0]];
  const result=build(value);assert.equal(result.messages.length,2);
  assert.equal(result.messages[0].id,first.id);assert.equal(result.complete,true);
});
test('Unfinished pagination remains explicitly incomplete',()=>{
  const value=source();value.pages[0].next_cursor='unread-page';
  const result=build(value);assert.equal(result.complete,false);assert.equal(result.partial,true);
});
test('Attachments are not silently treated as consumed text',()=>{
  const value=source();value.pages[0].timeline_entries[0].artifacts=[{id:'don:synthetic:artifact:1'}];
  assert.equal(build(value).complete,false);
});
test('Unknown authors remain unknown',()=>{
  const value=source();value.pages[0].timeline_entries[0].created_by={type:'unrecognized',id:'don:synthetic:user:1'};
  assert.equal(build(value).messages[0].author_role,'unknown');
});
for(const [name,mutate] of [
  ['mismatched ticket',s=>s.ticketId='TKT-100002'],
  ['non-external work body',s=>s.work.visibility={label:'internal'}],
  ['cross-ticket entry',s=>s.pages[0].timeline_entries[0].object='don:synthetic:ticket:2'],
  ['internal note',s=>s.pages[0].timeline_entries[0].visibility='internal'],
  ['missing body',s=>delete s.pages[0].timeline_entries[0].body],
  ['duplicate message',s=>s.pages[0].timeline_entries.push(s.pages[0].timeline_entries[0])],
  ['missing pages',s=>s.pages=[]],
  ['unlinked pages',s=>s.pages.push({timeline_entries:[]})],
  ['repeated cursor',s=>{s.pages[0].next_cursor='same';s.pages.push({timeline_entries:[],next_cursor:'same'});}],
  ['unexpected source type',s=>s.pages[0].timeline_entries[0].type='unknown'],
])test('Fails closed for '+name,()=>{
  const value=source();mutate(value);assert.equal(typeof api.snapshotFromDevRev,'function');
  assert.throws(()=>api.snapshotFromDevRev(value),/invalid_devrev_conversation|invalid_conversation_snapshot/);
});
