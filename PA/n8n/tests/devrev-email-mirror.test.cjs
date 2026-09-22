const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const file=path.join(__dirname,'../devrev-email-mirror.js');
const api=fs.existsSync(file)?require(file):{};
const text='What is the plan ID?\n\nThank you.';
function message({plain=text,html='<div dir="ltr">What is the plan ID?</div><div><br></div><div class="gmail_signature" data-smartmail="gmail_signature">Thank you.</div>',encoding='quoted-printable',extra='',ending='--b--',type='multipart/alternative'}={}){
 return `From: synthetic@example.invalid\r\nReceived: one\r\nReceived: two\r\nMIME-Version: 1.0\r\nContent-Type: ${type}; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain; charset="UTF-8"\r\nContent-Transfer-Encoding: ${encoding}\r\n\r\n${plain}\r\n--b\r\nContent-Type: text/html; charset=UTF-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n${html}\r\n${extra}${ending}\r\n`;
}
function matches(raw,body=text){assert.equal(typeof api.isRedundantEmailMirror,'function');return api.isRedundantEmailMirror(raw,body);}
test('Archived email is redundant only when both alternatives preserve the timeline text',()=>{
 assert.equal(matches(message()),true);
 assert.equal(matches(message({plain:'What is the plan=20ID?\r\n\r\nThank you.'})),true);
 assert.equal(matches(message({plain:Buffer.from(text).toString('base64'),encoding:'base64'})),true);
 assert.equal(matches(message({plain:'What is the pl=\r\nan ID?\n\nThank you.'})),true);
});
for(const [name,raw,body] of [
 ['missing content',null,text],['missing timeline',message(),null],['changed plain',message({plain:'Different request'}),text],
 ['changed HTML',message({html:'<div>Different request</div>'}),text],
 ['HTML link target',message({html:'<a href="https://example.invalid">'+text+'</a>'}),text],
 ['HTML hidden content',message({html:'<div style="display:none">'+text+'</div>'}),text],
 ['HTML image',message({html:'<div>'+text+'<img src="cid:file"></div>'}),text],
 ['HTML comments',message({html:'<div>'+text+'</div><!-- omitted content -->'}),text],
 ['unknown HTML entity',message({html:'<div>'+text+'&unknown;</div>'}),text],
 ['unbalanced HTML',message({html:'<div>'+text+'</span>'}),text],
 ['mixed attachments',message({type:'multipart/mixed'}),text],
 ['extra MIME part',message({extra:'--b\nContent-Type: application/pdf\nContent-Disposition: attachment; filename="form.pdf"\n\nPDF\n'}),text],
 ['missing closing boundary',message({ending:''}),text],
 ['unsupported encoding',message({encoding:'x-unknown'}),text],
 ['invalid quoted printable',message({plain:'What is the plan=XXID?\n\nThank you.'}),text],
 ['invalid UTF8',message({plain:'=FF'}),'�'],
 ['invalid base64',message({plain:'!!!!',encoding:'base64'}),text],
 ['oversized email','x'.repeat(262145),text],
 ['nonempty epilogue',message()+'Extra request',text],
 ['duplicate MIME header',message().replace('MIME-Version: 1.0','Content-Type: text/plain\r\nMIME-Version: 1.0'),text],
 ['part filename',message().replace('text/plain; charset="UTF-8"','text/plain; charset="UTF-8"; name="request.txt"'),text],
 ['part attachment disposition',message().replace('Content-Transfer-Encoding: quoted-printable','Content-Disposition: attachment\r\nContent-Transfer-Encoding: quoted-printable'),text],
])test('Unread or ambiguous email stays partial: '+name,()=>assert.equal(matches(raw,body),false));
test('Signed artifact URL is accepted only for the observed HTTPS storage host without embedded credentials',()=>{
 assert.equal(typeof api.validateArtifactDownloadUrl,'function');
 const url='https://devrev-prod-artifacts.s3-accelerate.dualstack.amazonaws.com/synthetic?signature=example';
 assert.equal(api.validateArtifactDownloadUrl(url),url);
 for(const bad of [undefined,'http://devrev-prod-artifacts.s3-accelerate.dualstack.amazonaws.com/a',url.replace('https://','https://user:pass@'),url.replace('.amazonaws.com','.amazonaws.com.attacker.invalid'),url+'#fragment','https://localhost/a','file:///tmp/a'])
  assert.throws(()=>api.validateArtifactDownloadUrl(bad),/invalid_artifact_download/);
});
module.exports={message,text};
