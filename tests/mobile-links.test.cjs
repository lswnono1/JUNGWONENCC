'use strict';
// v1.5.2 tests the actual combined application, not an isolated obsolete override.
const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const root=path.join(__dirname,'..');
const source=Array.from({length:9},(_,i)=>fs.readFileSync(path.join(root,`lawmonitor/app-core-${i+1}.txt`),'utf8')).join('\n').replace(/^\s*boot\(\);\s*$/gm,'');
function context(oc='fixture-oc'){
 const calls=[];
 const c=vm.createContext({URL,console,TextEncoder,TextDecoder,setTimeout,clearTimeout,AbortController,
  window:{location:{href:'https://lswnono1.github.io/JUNGWONENCC/lawmonitor/'}},
  document:{querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>({click(){calls.push(this.href)},remove(){}}),body:{appendChild(){}}},
  navigator:{onLine:true}});
 vm.runInContext(source,c);vm.runInContext(`state.secrets={lawOc:${JSON.stringify(oc)}};`,c);
 return c;
}
const c=context(), law={kind:'법령',name:'건축법',lawId:'000001',lawMst:'123456',promulgationDate:'2025-01-01',enforcementDate:'2025-02-01',eventKey:'old'};
const parse=v=>c.v152SpecFromUrl(v), q=v=>new URL(v).searchParams;
for(const input of ['javascript:alert(1)','data:text/html,a','file:///a','http://law.go.kr.evil.test/a','https://user:pw@law.go.kr/','https://law.go.kr\\@evil.test/','https://law.go.kr/\nx','relative-name'])test('reject '+JSON.stringify(input),()=>assert.equal(c.v151NormalizeUrl(input),''));
for(const input of ['http://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123','/LSW/lsInfoP.do?lsiSeq=123','//law.go.kr/LSW/lsInfoP.do?lsiSeq=123','LSW/lsInfoP.do?lsiSeq=123'])test('legacy PC URL routed to reader '+input,()=>assert.equal(parse(input).lawMst,'123'));
test('app URL contains no OC and roundtrips version identity',()=>{const url=c.v14MobileLawUrl(law);assert.equal(q(url).get('OC'),null);const s=parse(url);assert.equal(s.lawMst,'123456');assert.equal(s.lawId,'000001');assert.equal(s.promulgationDate,'20250101');assert.equal(s.enforcementDate,'20250201');assert.equal(s.history,true)});
test('DB rows with only saved internal URL retain their identity',()=>{const url=c.v14MobileLawUrl(law);const s=parse(c.v14MobileLawUrl({kind:'법령',name:law.name,officialUrl:url}));assert.equal(s.lawMst,'123456')});
test('ambiguous sourceId is not a law ID',()=>{const s=parse(c.v14MobileLawUrl({kind:'법령',name:'건축법',sourceId:'123'}));assert.equal(s.lawId,'');assert.equal(s.lawMst,'')});
test('external unrelated URL is not intercepted',()=>assert.equal(parse('https://opinion.lawmaking.go.kr/gcom/ogLmPp/123'),null));
test('law download file remains a file',()=>assert.equal(parse('https://law.go.kr/LSW/flDownload.do?flSeq=123'),null));
test('external site cannot impersonate internal reader route',()=>assert.equal(parse('https://evil.test/?jlmReader=1&MST=123'),null));
test('legacy mobile API ID recognized',()=>assert.equal(parse('https://law.go.kr/DRF/lawService.do?target=law&ID=001747&mobileYn=Y').lawId,'001747'));
test('legacy admin PC URL recognized',()=>assert.equal(parse('https://law.go.kr/LSW/admRulInfoP.do?admRulSeq=62505').adminId,'62505'));
test('Korean named URL decoded once',()=>assert.equal(parse('https://law.go.kr/법령/건축법').name,'건축법'));
test('appendix 1의2 keeps number and branch',()=>{const s=parse(c.v14MobileLawUrl(law,{appendix:{appendixNo:'1의2',appendixKind:'별표'}}));assert.equal(s.appendixNo,'1');assert.equal(s.appendixBranch,'2');assert.equal(s.view,'appendices')});
test('selected change MST wins over appendix MST',()=>assert.equal(parse(c.v14AppendixUrl(law,{lawMst:'999999',appendixNo:'2',revisionDate:'2026-01-01'})).lawMst,'123456'));
test('admin appendix uses same admin version',()=>{const s=parse(c.v14MobileLawUrl({kind:'행정규칙',adminId:'62505',name:'고시'},{allAppendices:true}));assert.equal(s.adminId,'62505');assert.equal(s.view,'appendices')});
test('body request uses JSON without mobile HTML redirect flags',()=>{const p=q(c.v152BodyUrl(parse(c.v14MobileLawUrl(law,{allAppendices:true}))));assert.equal(p.get('type'),'JSON');assert.equal(p.get('MST'),'123456');assert.equal(p.get('OC'),'fixture-oc');assert.equal(p.get('mobileYn'),null);assert.equal(p.get('BD'),null)});
test('missing OC is not replaced by sample credential',()=>assert.throws(()=>context('').v152BodyUrl(law),/OC/));
test('unknown identity is not replaced with current law',()=>assert.throws(()=>c.v152BodyUrl({kind:'법령',name:'건축법'}),/식별번호/));
test('law reader includes nested article paragraph item subitem and notes',()=>{const html=c.v152Clause({항:{항내용:'항내용A',호:{호내용:'호내용B',목:{목내용:'목내용C'}}},조문내용:'조문내용D',조문참고자료:'참고E'});for(const s of ['항내용A','호내용B','목내용C','조문내용D','참고E'])assert.ok(html.includes(s));assert.ok(html.indexOf('조문내용D')<html.indexOf('항내용A'))});
test('legal text containing brackets or tags is escaped, not executed',()=>{const h=c.v152Paragraphs('<script>alert(1)</script><개정 2025.1.1>');assert.ok(!h.includes('<script>'));assert.ok(h.includes('&lt;개정'));assert.ok(h.includes('&lt;script&gt;'))});
test('nested line arrays retain every line and whitespace',()=>assert.equal(c.v152Text([['  first','second'],['third']]),'  first\nsecond\nthird'));
const payload={법령:{기본정보:{법령명_한글:'건축법',법령ID:'000001',공포일자:'20250101',시행일자:'20250201'},조문:{조문단위:[{조문내용:'제1조 목적'}]}}};
test('real-shaped body object parsed',()=>assert.equal(c.v152DecodeDocument(payload,law).name,'건축법'));
test('wrong promulgation date is blocked',()=>assert.throws(()=>c.v152DecodeDocument(payload,{...law,promulgationDate:'20240101'}),/공포일/));
test('wrong law ID is blocked',()=>assert.throws(()=>c.v152DecodeDocument(payload,{...law,promulgationDate:'20250101',lawId:'999999'}),/ID/));
test('HTML/error JSON is not treated as a document',()=>{assert.throws(()=>c.v152DecodeDocument({error:'permission'},law),/원문/)});
test('empty body is not successful',()=>{const p={법령:{기본정보:payload.법령.기본정보}};assert.throws(()=>c.v152DecodeDocument(p,{kind:'법령'}),/본문/)});
test('appendix matching checks branch and type',()=>{const r={별표번호:'0001',별표가지번호:'02',별표구분:'별표'};assert.equal(c.v152AppendixMatches(r,{appendixNo:'1',appendixBranch:'2',appendixType:'1'}),true);assert.equal(c.v152AppendixMatches(r,{appendixNo:'1',appendixBranch:'0'}),false)});
test('official PDF link is HTTPS and no executable HTML',()=>{const a=c.v152FileLink('/LSW/flDownload.do?flSeq=12','공식 PDF');assert.ok(a.includes('https://www.law.go.kr/LSW/flDownload.do'));assert.ok(a.includes('noopener'));assert.equal(c.v152FileLink('https://evil.test/a.pdf','PDF'),'');assert.equal(c.v152FileLink('javascript:alert(1)','PDF'),'')});
test('admin JSON schema supported',()=>{const p={AdmRulService:{행정규칙기본정보:{행정규칙명:'고시',행정규칙일련번호:'62505',발령일자:'20090410'},조문내용:'제1장 총칙'}};assert.equal(c.v152DecodeDocument(p,{kind:'행정규칙',adminId:'62505'}).name,'고시');assert.throws(()=>c.v152DecodeDocument(p,{kind:'행정규칙',adminId:'62506'}),/이력/)});
test('legacy loader bridges to immutable current bundle',()=>{const text=fs.readFileSync(path.join(root,'lawmonitor/app.js'),'utf8');assert.match(text,/app-v1\.5\.4\.js/);assert.match(text,/integrity/)});
test('worker caches immutable bundle and preserves old entry aliases',()=>{const text=fs.readFileSync(path.join(root,'lawmonitor/sw.js'),'utf8');assert.match(text,/app-v1\.5\.4\.js/);assert.match(text,/app\.js\?v=1\.4\.0/);assert.match(text,/key\.startsWith\(CACHE_PREFIX\)/)});
test('reader does not write or clear persistent databases',()=>{const text=fs.readFileSync(path.join(root,'lawmonitor/app-core-9.txt'),'utf8');assert.doesNotMatch(text,/\b(?:dbPut|dbDelete|deleteDatabase|localStorage\.clear|sessionStorage\.clear)\s*\(/)});
