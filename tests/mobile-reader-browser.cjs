'use strict';
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const {chromium,devices}=require('playwright');
const source=Array.from({length:11},(_,i)=>fs.readFileSync(path.join('lawmonitor',`app-core-${i+1}.txt`),'utf8')).join('\n').replace(/^\s*boot\(\);\s*$/gm,'');
fs.mkdirSync('test-output',{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
  const context=await browser.newContext({...devices['Pixel 7'],serviceWorkers:'block'});
  const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/app-v*.js',r=>r.fulfill({contentType:'application/javascript',body:''}));
  await page.route('**/app.js*',r=>r.fulfill({contentType:'application/javascript',body:''}));
  await page.goto('http://127.0.0.1:8080/lawmonitor/',{waitUntil:'domcontentloaded'});
  await page.evaluate(text=>{(0,eval)(text+'\nwindow.jlmTest={state,openUrl,v14MobileLawUrl,v152OpenReader,v152DecodeDocument,v152Clause};');},source);
  await page.evaluate(()=>{window.jlmTest.state.secrets={lawOc:'test'};});
  const results=[];
  async function check(name,spec,expected){
   const before=context.pages().length;
   for(let attempt=1;attempt<=2;attempt++) {
    await page.evaluate(s=>window.jlmTest.v152OpenReader(s),spec);
    const retryStatus=await page.locator('#jlm-reader-status').innerText();
    const retryContent=await page.locator('#jlm-reader-content').innerText();
    if(retryContent.length>80 || !/시간.*초과|연결|Failed to fetch|NetworkError/i.test(retryStatus) || attempt===2) break;
    console.log('NETWORK_RETRY',JSON.stringify({name,attempt,status:retryStatus}));
   }
   const title=await page.locator('#jlm-reader-title').innerText();
   const status=await page.locator('#jlm-reader-status').innerText();
   const content=await page.locator('#jlm-reader-content').innerText();
   const result={name,title,status,characters:content.length,preview:content.slice(0,180),popups:context.pages().length-before};
   results.push(result); console.log('READER',JSON.stringify(result));
   assert.ok(title.includes(expected),`${name}: title ${title}, error ${status}`);
   assert.ok(content.length>80,`${name}: empty reader ${status}`);assert.equal(result.popups,0);
   assert.equal(await page.evaluate(()=>document.querySelector('#jlm-reader').scrollWidth>window.innerWidth+2),false,'reader exceeds mobile width');
   await page.screenshot({path:`test-output/${name}.png`});
   await page.locator('[data-jlm-tab=appendices]').click();
   const annexText=await page.locator('#jlm-reader-content').innerText();
   console.log('ANNEX',name,annexText.slice(0,350));
   if(name==='admin-sample'){
    assert.ok(annexText.includes('서식 1'));assert.ok(await page.locator('#jlm-reader-content a').count()>0);
    const href=await page.locator('#jlm-reader-content a').first().getAttribute('href');assert.ok(href.startsWith('https://www.law.go.kr/LSW/flDownload.do'));
   }
   await page.locator('[data-jlm-close]').click();
  }
  await check('law-sample',{kind:'법령',lawId:'1747',name:'자동차관리법'},'자동차관리법');
  await check('admin-sample',{kind:'행정규칙',adminId:'62505',name:'개성공업지구 폐기물 국내반입 처리 절차 등에 관한 업무처리지침'},'개성공업지구');
  async function search(kind,query,needle){
   return page.evaluate(async({kind,query,needle})=>{
    const u=new URL('https://www.law.go.kr/DRF/lawSearch.do');
    Object.entries({OC:'test',target:kind==='법령'?'law':'admrul',type:'JSON',query,display:100,page:1}).forEach(([k,v])=>u.searchParams.set(k,v));
    let payload;
    for(let attempt=1;attempt<=2;attempt++) {
      try{const response=await fetch(u,{signal:AbortSignal.timeout(20000)});if(!response.ok)throw Error('Search HTTP '+response.status);payload=await response.json();break;}
      catch(error){console.log('SEARCH_NETWORK_RETRY',query,attempt,error.message);if(attempt===2)throw error;}
    }
    const all=[];function walk(v){if(Array.isArray(v))v.forEach(walk);else if(v&&typeof v==='object'){all.push(v);Object.values(v).forEach(walk)}}walk(payload);
    const r=all.find(x=>String(x.법령명한글||x.법령명||x.행정규칙명||'').includes(needle));
    if(!r)throw new Error('No matching official search row: '+query);
    return{kind,name:r.법령명한글||r.법령명||r.행정규칙명,lawMst:r.법령일련번호||'',lawId:r.법령ID||'',adminId:r.행정규칙일련번호||''};
   },{kind,query,needle});
  }
  await check('fire-law',await search('법령','소방시설 설치 및 관리에 관한 법률 시행령','시행령'),'소방시설');
  await check('fire-admin',await search('행정규칙','옥내소화전설비','NFPC'),'옥내소화전');
  await page.evaluate(()=>window.jlmTest.openUrl('http://www.law.go.kr/LSW/admRulInfoP.do?admRulSeq=62505'));
  await page.waitForFunction(()=>document.querySelector('#jlm-reader-content').innerText.length>80,null,{timeout:30000});
  assert.equal(context.pages().length,1,'legacy PC link must not open desktop page');
  await page.locator('[data-jlm-close]').click();
  await page.evaluate(()=>{window.jlmTest.state.secrets={lawOc:''};});
  await page.evaluate(()=>window.jlmTest.v152OpenReader({kind:'법령',lawId:'1747'}));
  assert.match(await page.locator('#jlm-reader-status').innerText(),/OC/);
  assert.equal(await page.locator('#jlm-reader-content').innerText(),'');
  await page.screenshot({path:'test-output/missing-oc.png'});
  await page.locator('[data-jlm-close]').click();
  const fresh=await context.newPage();
  await fresh.goto('http://127.0.0.1:8080/lawmonitor/',{waitUntil:'domcontentloaded'});
  await fresh.waitForSelector('#setup-view:not(.hidden)',{timeout:15000});
  assert.match(await fresh.locator('#page-managed .page-title p').innerText(),/v1\.5\.4/);
  results.push({name:'actual-loader-boot',passed:true});
  fs.writeFileSync('test-output/results.json',JSON.stringify({results,errors},null,2));
  assert.deepEqual(errors,[]);console.log('MOBILE_READER_BROWSER_PASS');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);fs.writeFileSync('test-output/failure.txt',e.stack||String(e));process.exitCode=1});
