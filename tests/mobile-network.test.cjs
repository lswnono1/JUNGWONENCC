'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm'),path=require('path');
const src=Array.from({length:11},(_,i)=>fs.readFileSync(path.join(__dirname,`../lawmonitor/app-core-${i+1}.txt`),'utf8')).join('\n').replace(/^\s*boot\(\);\s*$/gm,'');
function ctx(fn){
 const calls=[];const c=vm.createContext({URL,console,TextEncoder,TextDecoder,AbortController,TypeError,
  setTimeout:(fn,ms)=>setTimeout(fn,ms===700?0:ms),clearTimeout,
  window:{location:{href:'https://lswnono1.github.io/JUNGWONENCC/lawmonitor/'}},
  navigator:{onLine:true},document:{querySelector:()=>null,querySelectorAll:()=>[]},
  fetch:async(u,o)=>{calls.push({u,o});return fn(u,o,calls.length)}});
 vm.runInContext(src+'\nwindow.testState=state;window.trace=v154Requests;',c);c.window.testState.secrets={lawOc:'private-fixture-key'};
 return {c,calls,state:c.window.testState};
}
const res=(obj,status=200)=>({ok:status>=200&&status<300,status,text:async()=>typeof obj==='string'?obj:JSON.stringify(obj)});
const url='https://www.law.go.kr/DRF/lawSearch.do?OC=private-fixture-key&target=law&type=JSON&mobileYn=Y';
const row=(name='건축법',id='001',mst='100')=>({법령명한글:name,법령ID:id,법령일련번호:mst,공포일자:'20250101',시행일자:'20250201',제개정구분명:'일부개정'});
const list=rows=>({LawSearch:{totalCnt:String(rows.length),law:rows}});
test('network error retries once at official HTTPS alias and removes HTML mobile flag',async()=>{const{c,calls}=ctx((u,o,n)=>{if(n===1)throw new TypeError('Failed to fetch');return res(list([row()]));});await c.fetchText(url);assert.equal(calls.length,2);assert.equal(new URL(calls[1].u).hostname,'law.go.kr');assert.equal(new URL(calls[0].u).searchParams.get('mobileYn'),null);assert.equal(calls[1].o.mode,'cors');assert.equal(calls[1].o.credentials,'omit');assert.equal(calls[1].o.headers,undefined)});
test('network error stops after exactly two attempts; no false CORS certainty',async()=>{const{c,calls}=ctx(()=>{throw new TypeError('failed')});await assert.rejects(c.fetchText(url),e=>e.code==='NET_FETCH'&&!e.message.includes('허용 여부'));assert.equal(calls.length,2)});
for(const status of [401,403,429])test(`HTTP ${status} not bypassed or retried`,async()=>{const{c,calls}=ctx(()=>res('',status));await assert.rejects(c.fetchText(url),e=>e.code===`HTTP_${status}`);assert.equal(calls.length,1)});
test('temporary server response retried once',async()=>{const{c,calls}=ctx((u,o,n)=>n===1?res('',503):res(list([])));await c.fetchText(url);assert.equal(calls.length,2)});
test('diagnostics contain no OC or full request URL',async()=>{const{c}=ctx(()=>res(list([])));await c.fetchText(url);const d=JSON.stringify(c.window.trace);assert.ok(!d.includes('private-fixture-key'));assert.ok(!d.includes('OC='));assert.ok(d.includes('target'))});
test('offline makes no API requests',async()=>{const{c,calls}=ctx(()=>res(''));c.navigator.onLine=false;await assert.rejects(c.fetchText(url),e=>e.code==='NET_OFFLINE');assert.equal(calls.length,0)});
test('empty response never treated as valid empty list',async()=>{const{c}=ctx(()=>res(''));await assert.rejects(c.fetchText(url),e=>e.code==='API_EMPTY')});
test('HTML basic error recognized but not executed',async()=>{const{c}=ctx(()=>res('<html><script>alert("기본정보 조회 실패")</script></html>'));await assert.rejects(c.fetchText(url),e=>e.code==='JLM-153-BASIC')});
test('other HTML rejected',async()=>{const{c}=ctx(()=>res('<html>access denied</html>'));await assert.rejects(c.fetchText(url),e=>e.code==='API_FORMAT')});
test('missing catalogue envelope not reported as successful empty result',()=>{const{c}=ctx(()=>res(''));assert.throws(()=>c.v154SearchResult({error:'denied'},[]),e=>e.code==='API_SEARCH_FORMAT')});
test('legacy contaminated ID repaired by exact law title; decree is never accepted',async()=>{const{c,calls}=ctx(u=>{const q=new URL(u).searchParams;return res(q.get('target')==='law'?list([row('건축법 시행령','002'),row()]):list([row(),row('건축법 시행령','002','101'),row('건축법','bad','102')]));});const rows=await c.fetchRevisions({kind:'법령',name:'건축법',id:'managed1',lawId:'002'});assert.equal(rows.length,1);assert.equal(rows[0].lawId,'001');assert.equal(new URL(calls[1].u).searchParams.get('LID'),'001')});
test('ambiguous exact IDs rejected instead of using the first match',async()=>{const{c}=ctx(()=>res(list([row(),row('건축법','002')])));await assert.rejects(c.fetchRevisions({kind:'법령',name:'건축법'}),e=>e.code==='LAW_IDENTITY')});
test('different title cannot be rendered even with a matching stored ID',()=>{const{c}=ctx(()=>res(''));assert.throws(()=>c.v152DecodeDocument({법령:{기본정보:{법령명_한글:'건축법 시행령',법령ID:'001'},조문:{조문단위:[{조문내용:'시험'}]}}},{kind:'법령',lawId:'001',name:'건축법'}),e=>e.code==='LAW_NAME_MISMATCH')});
function syncContext(){const{c,state}=ctx(()=>res(''));const data={managed:[{id:'a',name:'건축법',kind:'법령',enabled:true,lastEventKey:'old',lastRevisionDate:'2024-01-01',lastRevisionSuccessAt:'prior-success',lastNoticeCheckedAt:'old-notice'}],changes:[{eventKey:'old',name:'건축법',extra:'keep'}],notices:[]};
 c.managedRows=async()=>data.managed;c.dbGet=async(s,k)=>data[s].find(x=>(x.id||x.eventKey||x.noticeKey)===k);c.dbPut=async(s,r)=>{const k=r.id||r.eventKey||r.noticeKey,i=data[s].findIndex(x=>(x.id||x.eventKey||x.noticeKey)===k);if(i<0)data[s].push({...r});else data[s][i]={...r};};
 c.renderSync=()=>{};c.addLog=async()=>{};c.refreshAll=async()=>{};c.setMeta=async()=>{};c.toast=()=>{};c.v15NoticeFeed=async()=>({});c.fetchNotices=async()=>[];
 c.fetchRevisions=async()=>[{eventKey:'new',managedId:'a',name:'건축법',kind:'법령',lawId:'001',lawMst:'100',promulgationDate:'2025-01-01',enforcementDate:'2025-02-01'}];c.fetchAppendices=async()=>[];return{c,state,data};}
test('appendix error does not mark successful history as failed',async()=>{const{c,data}=syncContext();c.fetchAppendices=async()=>{throw c.v154Error('NET_FETCH','fixture')};await c.syncNow();assert.match(data.managed[0].checkStatus,/개정이력 정상.*별표 실패/);assert.equal(data.managed[0].lastRevisionDate,'2025-01-01');assert.ok(data.changes.some(x=>x.eventKey==='old'&&x.extra==='keep'));});
test('failed history preserves last successful date and history records',async()=>{const{c,data}=syncContext();c.fetchRevisions=async()=>{throw c.v154Error('NET_FETCH','fixture')};await c.syncNow();assert.match(data.managed[0].checkStatus,/개정이력 실패.*별표 미확인/);assert.equal(data.managed[0].lastRevisionDate,'2024-01-01');assert.equal(data.managed[0].lastRevisionSuccessAt,'prior-success');assert.equal(data.changes.length,1)});
test('unknown appendix date never assigned to the latest law revision',async()=>{const{c,data}=syncContext();c.fetchAppendices=async()=>[{appendixKey:'unknown',revisionDate:'',title:'시험'}];await c.syncNow();assert.equal(data.changes.find(x=>x.eventKey==='new').appendices?.length||0,0)});
test('failed notice query does not advance last successful notice timestamp',async()=>{const{c,data}=syncContext();c.fetchNotices=async()=>{throw new Error('fixture')};await c.syncNow();assert.equal(data.managed[0].lastNoticeCheckedAt,'old-notice')});
test('failed-only sync does not re-query healthy entries',async()=>{const{c,data}=syncContext();data.managed[0].checkStatus='개정이력 정상 · 별표 정상';let count=0;c.fetchRevisions=async()=>{count++;return[]};await c.syncNow({failedOnly:true});assert.equal(count,0)});
