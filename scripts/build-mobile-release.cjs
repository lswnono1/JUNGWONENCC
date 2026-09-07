'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const root=path.resolve(__dirname,'..'),app=path.join(root,'lawmonitor'),version='1.5.3';
const source=Array.from({length:10},(_,i)=>fs.readFileSync(path.join(app,`app-core-${i+1}.txt`),'utf8')).join('\n').replace(/^\s*boot\(\);\s*$/gm,'');
const bundle=`/* Jungwon Law Monitor ${version}: immutable full application */\n(function(){\n${source}\nboot();\n})();\n`;
const file=`app-v${version}.js`,digest=crypto.createHash('sha256').update(bundle).digest(),sri='sha256-'+digest.toString('base64');
fs.writeFileSync(path.join(app,file),bundle);
let html=fs.readFileSync(path.join(app,'index.html'),'utf8');
html=html.replace(/<script\s+src="\.\/app(?:-v[\d.]+)?\.js(?:\?[^" ]*)?"[^>]*><\/script>/,`<script src="./${file}" integrity="${sri}" crossorigin="anonymous" defer></script>`);
if(!html.includes(sri))throw new Error('Entry script was not found');
fs.writeFileSync(path.join(app,'index.html'),html);
fs.writeFileSync(path.join(app,`recover-v${version}.html`),html);
fs.writeFileSync(path.join(app,'release.json'),JSON.stringify({version,bundle:file,sha256:digest.toString('hex'),mode:'in-app-json'},null,2)+'\n');
// Existing home-screen installs may still request app.js?v=1.4.0. Bridge all of them to the same immutable source.
fs.writeFileSync(path.join(app,'app.js'),`'use strict';\n(()=>{const s=document.createElement('script');s.src='./${file}';s.integrity='${sri}';s.crossOrigin='anonymous';s.onerror=()=>{const p=document.createElement('p');p.textContent='앱 구성 파일 연결 실패. 인터넷 연결 후 복구 화면을 열어 주세요.';const a=document.createElement('a');a.href='./recover-v${version}.html';a.textContent='데이터 유지하고 복구 화면 열기';document.body.append(p,a);};document.head.appendChild(s);})();\n`);
fs.writeFileSync(path.join(app,'sw.js'),`const CACHE_PREFIX='jungwon-lawmonitor-pwa-';
const CACHE_NAME=CACHE_PREFIX+'v${version}';
const APP_SHELL=['./','./index.html','./recover-v${version}.html','./${file}','./app.js','./app.js?v=1.4.0','./app.js?v=1.5.0','./app.js?v=1.5.1','./app.js?v=1.5.2','./styles.css','./manifest.webmanifest','./icon.svg','./maskable.svg','./release.json'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE_NAME).then(c=>c.addAll(APP_SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith(CACHE_PREFIX)&&key!==CACHE_NAME).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
 const request=event.request,u=new URL(request.url),scope=new URL(self.registration.scope);
 if(request.method!=='GET'||u.origin!==scope.origin||!u.pathname.startsWith(scope.pathname))return;
 event.respondWith((async()=>{
  const c=await caches.open(CACHE_NAME);
  try{const r=await fetch(request);if(r.ok){const copy=r.clone();event.waitUntil(c.put(request,copy).catch(()=>{}));}return r;}
  catch(error){const stored=await c.match(request);if(stored)return stored;if(request.mode==='navigate'){const page=await c.match('./index.html');if(page)return page;}throw error;}
 })());
});
`);
// Keep the original functional reader tests; update the two build-layout checks to the new release contract.
const testFile=path.join(root,'tests/mobile-links.test.cjs');
let tests=fs.readFileSync(testFile,'utf8');
tests=tests.replace(/test\('new loader includes nine layers and current version'[^\n]*/,`test('legacy loader bridges to immutable current bundle',()=>{const text=fs.readFileSync(path.join(root,'lawmonitor/app.js'),'utf8');assert.match(text,/app-v1\\.5\\.3\\.js/);assert.match(text,/integrity/)});`);
tests=tests.replace(/test\('service worker caches new layer and old entry point'[^\n]*/,`test('worker caches immutable bundle and preserves old entry aliases',()=>{const text=fs.readFileSync(path.join(root,'lawmonitor/sw.js'),'utf8');assert.match(text,/app-v1\\.5\\.3\\.js/);assert.match(text,/app\\.js\\?v=1\\.4\\.0/);assert.match(text,/key\\.startsWith\\(CACHE_PREFIX\\)/)});`);
fs.writeFileSync(testFile,tests);
console.log(JSON.stringify({version,file,sha256:digest.toString('hex'),bytes:Buffer.byteLength(bundle)}));
const browserFile=path.join(root,'tests/mobile-reader-browser.cjs');
let browserTest=fs.readFileSync(browserFile,'utf8').replace('length:9','length:10').replace('/v1\\.5\\.2/','/v1\\.5\\.3/');
if(!browserTest.includes("page.route('**/app-v*.js'"))browserTest=browserTest.replace("  await page.route('**/app.js*'", "  await page.route('**/app-v*.js',r=>r.fulfill({contentType:'application/javascript',body:''}));\n  await page.route('**/app.js*'");
fs.writeFileSync(browserFile,browserTest);
