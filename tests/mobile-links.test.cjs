'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'lawmonitor/app-core-8.txt'), 'utf8');
function context(oc = 'test-oc') {
  const calls = [], toasts = [], nodes = [];
  const ctx = vm.createContext({
    URL, console,
    state:{secrets:{lawOc:oc}},
    cleanText:v => String(v ?? '').replace(/\s+/g, ' ').trim(),
    apiUrl:(base, params) => {const u = new URL(base); for (const [k,v] of Object.entries(params)) if (v !== '' && v != null) u.searchParams.set(k, v); return u.href;},
    v14AppendixTypeCode:v => String(v).includes('서식') ? 2 : String(v).includes('별지') ? 3 : String(v).includes('별도') ? 4 : String(v).includes('부록') ? 5 : 1,
    toast:v => toasts.push(v), $:() => null,
    document:{
      createElement:tag => {assert.equal(tag, 'a'); const a = {click(){calls.push({href:this.href,target:this.target,rel:this.rel});},remove(){a.removed = true;}}; nodes.push(a); return a;},
      body:{appendChild:a => {a.connected = true;}}
    },
    window:{open(){throw new Error('window.open must not be called');}},
    location:new Proxy({}, {set(){throw new Error('app navigation must not change');}}),
  });
  vm.runInContext(source, ctx);
  return {ctx, calls, toasts, nodes};
}
const {ctx} = context();
const q = value => new URL(value).searchParams;
const law = {kind:'법령',name:'소방시설 설치 및 관리에 관한 법률 시행령',lawId:'014397',lawMst:'123456',promulgationDate:'2025-01-01'};
for (const [input, expected] of [
  ['http://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123&lsId=12','https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123&lsId=12'],
  ['//www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123','https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123'],
  ['/LSW/lsInfoP.do?lsiSeq=123&amp;lsId=12','https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123&lsId=12'],
  ['LSW/lsInfoP.do?lsiSeq=123','https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123'],
  ['law.go.kr/법령/건축법','https://law.go.kr/%EB%B2%95%EB%A0%B9/%EA%B1%B4%EC%B6%95%EB%B2%95'],
]) test(`normalize ${input}`, () => assert.equal(ctx.v151NormalizeUrl(input), expected));
for (const input of ['', 'javascript:alert(1)', 'data:text/html,hello', 'file:///a', 'intent://a', 'http://law.go.kr.evil.test/a', 'https://user:pass@law.go.kr/', 'https://law.go.kr\\@evil.test/', 'https://law.go.kr/\nfoo', 'relative-page']) {
  test(`reject unsafe ${JSON.stringify(input)}`, () => assert.equal(ctx.v151NormalizeUrl(input), ''));
}
test('external link opens only once without app navigation', () => {
  const {ctx,calls,nodes} = context();
  ctx.openUrl('https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123');
  assert.equal(calls.length, 1); assert.equal(calls[0].target, '_blank');
  assert.equal(calls[0].rel, 'noopener noreferrer'); assert.equal(nodes[0].removed, true);
});
test('invalid link is blocked with feedback', () => {
  const {ctx,calls,toasts} = context(); ctx.openUrl('javascript:alert(1)');
  assert.equal(calls.length,0); assert.equal(toasts.length,1);
});
test('official supplied history URL is not discarded', () => {
  const url = ctx.officialLawUrl('법령', '건축법', '/LSW/lsInfoP.do?lsiSeq=123');
  assert.equal(q(url).get('lsiSeq'), '123');
});
test('law history uses MST, not stable ID', () => {
  const params = q(ctx.v14MobileLawUrl(law));
  assert.equal(params.get('MST'),'123456'); assert.equal(params.get('ID'),null);
  assert.equal(params.get('mobileYn'),'Y'); assert.equal(params.get('LD'),null);
});
test('MST is recovered from legacy official URL', () => {
  assert.equal(q(ctx.v14MobileLawUrl({kind:'법령',officialUrl:'http://law.go.kr/LSW/lsInfoP.do?lsiSeq=778899'})).get('MST'),'778899');
});
test('unknown sourceId is never treated as a stable law ID', () => {
  const params = q(ctx.v14MobileLawUrl({kind:'법령', name:'건축법', sourceId:'999999'}));
  assert.equal(params.get('ID'),null); assert.equal(params.get('LM'),'건축법');
});
test('administrative rule preserves its ID even for appendix', () => {
  const params = q(ctx.v14MobileLawUrl({kind:'행정규칙',name:'고시',adminId:'210000000001',lawId:'999'},{appendix:{appendixNo:'1'}}));
  assert.equal(params.get('target'),'admrul'); assert.equal(params.get('ID'),'210000000001');
  assert.equal(params.get('BD'),null);
});
test('wrong-kind source URL does not inject the wrong ID', () => {
  const params = q(ctx.v14MobileLawUrl({kind:'행정규칙',name:'고시',officialUrl:'https://law.go.kr/DRF/lawService.do?target=law&ID=123'}));
  assert.equal(params.get('ID'),null); assert.equal(params.get('LM'),'고시');
});
test('appendix 1의2 becomes BN=1 BG=2, never BN=12', () => {
  const params = q(ctx.v14MobileLawUrl(law,{appendix:{appendixNo:'1의2',appendixKind:'별표'}}));
  assert.equal(params.get('BD'),'ON'); assert.equal(params.get('BN'),'1'); assert.equal(params.get('BG'),'2');
});
test('appendix number and branch can be read from title', () => {
  const params = q(ctx.v14MobileLawUrl(law,{appendix:{title:'[별표 3의2] 설치기준'}}));
  assert.equal(params.get('BN'),'3'); assert.equal(params.get('BG'),'2');
});
test('selected old MST is not overwritten by current appendix MST', () => {
  const params = q(ctx.v14AppendixUrl(law,{lawMst:'999999',appendixNo:'2',revisionDate:'2026-01-01'},'2026-01-01'));
  assert.equal(params.get('MST'),'123456');
});
test('promulgation date is not replaced by appendix date', () => {
  const params = q(ctx.v14AppendixUrl({...law, lawMst:''},{appendixNo:'2',revisionDate:'2026-01-01'},'2026-01-01'));
  assert.equal(params.get('LD'),'20250101');
});
test('cached API OC is refreshed at click time', () => {
  const params = q(ctx.v151ExternalUrl('http://law.go.kr/DRF/lawService.do?target=law&MST=12&OC=old&type=XML'));
  assert.equal(params.get('OC'),'test-oc'); assert.equal(params.get('type'),'HTML');
  assert.equal(params.get('MST'),'12'); assert.equal(params.get('mobileYn'),'Y');
});
test('notice REST links become public original links', () => {
  const value = ctx.v151ExternalUrl('http://www.lawmaking.go.kr/rest/ogLmPp/12345/0/TYPE1.html?OC=old');
  assert.equal(value,'https://opinion.lawmaking.go.kr/gcom/ogLmPp/12345');
});
test('Korean names are not double encoded', () => {
  const value = ctx.v151ExternalUrl(ctx.v14MobileLawUrl({kind:'법령',name:'건축법 시행령'}));
  assert.equal(q(value).get('LM'),'건축법 시행령'); assert.ok(!value.includes('%25EA'));
});
test('missing OC uses public exact historical page', () => {
  const {ctx} = context(''); assert.equal(q(ctx.v14MobileLawUrl(law)).get('lsiSeq'),'123456');
});
test('missing OC never silently substitutes current law for unknown history', () => {
  const {ctx} = context(''); assert.equal(ctx.v14MobileLawUrl({...law,lawMst:'',eventKey:'old'}),'');
});
test('missing OC still allows a named current law', () => {
  const {ctx} = context(''); assert.match(ctx.v14MobileLawUrl({kind:'법령',name:'건축법'}),/^https:\/\/www\.law\.go\.kr\//);
});
test('new loader includes the repair layer', () => {
  const text = fs.readFileSync(path.join(root,'lawmonitor/app.js'),'utf8');
  assert.match(text,/length:8/); assert.match(text,/1\.5\.1/);
});
test('repair performs no persistent data mutation', () => {
  assert.doesNotMatch(source,/\b(?:dbPut|dbDelete|deleteDatabase|localStorage\.clear|sessionStorage\.clear)\s*\(/);
});
test('service worker caches legacy entry point and limits cache deletion to this app', () => {
  const text = fs.readFileSync(path.join(root,'lawmonitor/sw.js'),'utf8');
  assert.match(text,/app\.js\?v=1\.4\.0/); assert.match(text,/key\.startsWith\(CACHE_PREFIX\)/);
});
