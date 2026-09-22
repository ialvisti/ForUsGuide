'use strict';

// Conservative recognizer for an authenticated DevRev RFC822 archive which
// adds no content to its owning external timeline body. This is deliberately
// not a general MIME renderer: unsupported/ambiguous input stays incomplete.
// Neither a filename nor a model assertion can prove redundancy.
const unavailable=()=>{throw new Error('unread_email_mirror');};
function headersAndBody(raw){
  const at=raw.indexOf('\n\n');
  if(at<0 || at>65536)unavailable();
  const lines=raw.slice(0,at).split('\n'),headers={};
  let name=null;
  for(const line of lines){
    if(/^[ \t]/.test(line)){
      if(!name)unavailable();
      if(Object.hasOwn(headers,name))headers[name]+=' '+line.trim();
      continue;
    }
    const m=/^([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(.*)$/.exec(line);
    if(!m)unavailable();
    name=m[1].toLowerCase();
    if(['content-type','content-transfer-encoding','content-disposition','mime-version'].includes(name)){
      if(Object.hasOwn(headers,name))unavailable();
      headers[name]=m[2].trim();
    }
  }
  return {headers,body:raw.slice(at+2)};
}
function contentType(value){
  if(typeof value!=='string')unavailable();
  const tokens=value.split(';'),type=tokens.shift().trim().toLowerCase(),params={};
  if(!/^[a-z0-9-]+\/[a-z0-9-]+$/.test(type))unavailable();
  for(const token of tokens){
    const m=/^\s*([A-Za-z0-9-]+)\s*=\s*(?:"([^"\r\n]*)"|([^\s";]+))\s*$/.exec(token);
    if(!m || Object.hasOwn(params,m[1].toLowerCase()))unavailable();
    params[m[1].toLowerCase()]=m[2]??m[3];
  }
  return {type,params};
}
function decodeText(part){
  const {headers,body}=headersAndBody(part),type=contentType(headers['content-type']);
  if(!['text/plain','text/html'].includes(type.type) || headers['content-disposition'] ||
    Object.keys(type.params).some(k=>k!=='charset') ||
    !['utf-8','us-ascii'].includes((type.params.charset||'us-ascii').toLowerCase()))unavailable();
  const encoding=(headers['content-transfer-encoding']||'7bit').toLowerCase();
  let bytes;
  if(encoding==='base64'){
    const compact=body.replace(/[\n\t ]/g,'');
    if(!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(compact))unavailable();
    bytes=Buffer.from(compact,'base64');
    if(bytes.toString('base64')!==compact)unavailable();
  }else if(encoding==='quoted-printable'){
    const source=body.replace(/=\n/g,''),array=[];
    for(let i=0;i<source.length;i++){
      const code=source.charCodeAt(i);
      if(source[i]==='='){
        const hex=source.slice(i+1,i+3);
        if(!/^[0-9a-f]{2}$/i.test(hex))unavailable();
        array.push(parseInt(hex,16));i+=2;
      }else{
        if(code>127 || (code<32 && ![9,10].includes(code)))unavailable();
        array.push(code);
      }
    }
    bytes=Buffer.from(array);
  }else if(['7bit','8bit'].includes(encoding)){
    if(encoding==='7bit' && /[^\x00-\x7f]/.test(body))unavailable();
    bytes=Buffer.from(body,'utf8');
  }else unavailable();
  if(bytes.length>100000 || bytes.includes(0))unavailable();
  const text=bytes.toString('utf8');
  if(!Buffer.from(text,'utf8').equals(bytes) ||
    ((type.params.charset||'us-ascii').toLowerCase()==='us-ascii' && /[^\x00-\x7f]/.test(text)))unavailable();
  return {type:type.type,text};
}
function simpleHtmlText(html){
  const stack=[],chunks=[];
  let pos=0;
  const tags=/<[^>]*>/g;
  for(const token of html.matchAll(tags)){
    chunks.push(html.slice(pos,token.index));pos=token.index+token[0].length;
    const m=/^<(\/?)(div|p|span|br|b|strong|i|em|u)(\s[^<>]*?)?\s*(\/?)>$/i.exec(token[0]);
    if(!m)unavailable();
    const tag=m[2].toLowerCase(),attrs=m[3]||'';
    if(m[1]){
      if(attrs.trim() || m[4] || stack.pop()!==tag)unavailable();
    }else{
      // Formatting-only attributes used by the observed Gmail archive.
      const used=new Set();let remaining=attrs;
      while(remaining.trim()){
        const a=/^\s+(dir|class|data-smartmail|clear)="([A-Za-z0-9 _-]{1,100})"/i.exec(remaining);
        if(!a || used.has(a[1].toLowerCase()))unavailable();
        used.add(a[1].toLowerCase());remaining=remaining.slice(a[0].length);
      }
      if(tag!=='br'){
        if(m[4])unavailable();
        stack.push(tag);
      }
    }
    if(['div','p','br'].includes(tag))chunks.push('\n');
  }
  chunks.push(html.slice(pos));
  if(stack.length)unavailable();
  const raw=chunks.join('');
  if(/[<>]/.test(raw))unavailable();
  const entities={amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:' '};
  return raw.replace(/&([^;\s]+);|&/g,(all,key)=>{
    if(!key)unavailable();
    if(Object.hasOwn(entities,key))return entities[key];
    if(/^#(?:[0-9]+|x[0-9a-f]+)$/i.test(key)){
      const n=key[1].toLowerCase()==='x'?parseInt(key.slice(2),16):Number(key.slice(1));
      if(n<1 || n>0x10ffff || (n>=0xd800&&n<=0xdfff))unavailable();
      return String.fromCodePoint(n);
    }
    return unavailable();
  });
}
function isRedundantEmailMirror(raw,timelineBody){
  try{
    if(typeof raw!=='string' || typeof timelineBody!=='string' || !timelineBody.trim() ||
      !raw.isWellFormed() || raw.includes('\0') || Buffer.byteLength(raw,'utf8')>262144)unavailable();
    const normalized=raw.replace(/\r\n/g,'\n');
    if(normalized.includes('\r'))unavailable();
    const root=headersAndBody(normalized),type=contentType(root.headers['content-type']);
    if(type.type!=='multipart/alternative' || Object.keys(type.params).length!==1 ||
      !/^[A-Za-z0-9'()+_,./:=?-]{1,70}$/.test(type.params.boundary||'') ||
      root.headers['content-disposition'] || root.headers['content-transfer-encoding'])unavailable();
    const delimiter='--'+type.params.boundary,lines=root.body.split('\n'),parts=[];
    let current=null,closed=false;
    for(const line of lines){
      if(line===delimiter || line===delimiter+'--'){
        if(closed)unavailable();
        if(current!==null)parts.push(current.join('\n'));
        current=[];closed=line===delimiter+'--';
      }else if(closed || current===null){
        if(line.trim())unavailable();
      }else current.push(line);
    }
    if(!closed || parts.length!==2)unavailable();
    const decoded=parts.map(decodeText);
    if(decoded[0].type!=='text/plain' || decoded[1].type!=='text/html')unavailable();
    const plain=decoded[0].text.replace(/\r\n/g,'\n').trim();
    if(plain!==timelineBody.replace(/\r\n/g,'\n').trim())unavailable();
    const collapse=s=>s.replace(/\s+/g,' ').trim();
    return collapse(simpleHtmlText(decoded[1].text))===collapse(plain);
  }catch(_){return false;}
}
function validateArtifactDownloadUrl(value){
  // The origin was observed in an authenticated artifacts.locate response.
  // No auth headers/cookies are forwarded here. Never follow redirects.
  if(typeof value!=='string' || value.length>16384 ||
    !/^https:\/\/devrev-prod-artifacts\.s3-accelerate\.dualstack\.amazonaws\.com\/[^\s#\\]*$/.test(value))
    throw new Error('invalid_artifact_download');
  return value;
}
module.exports={isRedundantEmailMirror,validateArtifactDownloadUrl};
