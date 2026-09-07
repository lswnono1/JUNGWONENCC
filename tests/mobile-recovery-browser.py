"""UI regression: fixture responses only; no user's account or database is accessed."""
import json, os, re
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from playwright.sync_api import sync_playwright
BASE=os.environ.get('JLM_TEST_BASE','http://127.0.0.1:8080/lawmonitor/')
OUT=Path('test-output'); OUT.mkdir(exist_ok=True)
results=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('CHROME_BIN','/usr/bin/chromium'),args=['--no-sandbox'])
    context=browser.new_context(**p.devices['Pixel 7'],service_workers='block')
    page=context.new_page(); errors=[]; dialogs=[]; requests=[]; mode={'ambiguous':False}
    page.on('pageerror',lambda e: errors.append(str(e)))
    page.on('dialog',lambda d:(dialogs.append(d.message),d.dismiss()))
    def route_api(route):
        u=urlparse(route.request.url); q=parse_qs(u.query); requests.append(q)
        if 'lawSearch.do' in u.path:
            rows=[{'법령명한글':'테스트 법령','법령일련번호':'123','법령ID':'1747','공포일자':'20250101'}]
            if mode['ambiguous']: rows.append({**rows[0],'법령일련번호':'124'})
            route.fulfill(headers={'access-control-allow-origin':'*'},json={'LawSearch':{'law':rows}})
        elif q.get('MST')==['999'] or q.get('MST')==['998']:
            route.fulfill(headers={'access-control-allow-origin':'*'},content_type='text/html',body='<html><script>alert("기본정보 조회 실패");history.back();</script></html>')
        else:
            route.fulfill(headers={'access-control-allow-origin':'*'},json={'법령':{'기본정보':{'법령명_한글':'테스트 법령','법령ID':'1747','공포일자':'20250101','시행일자':'20250101'},'조문':{'조문단위':[{'조문내용':'제1조(목적) 이 문서는 연결 오류 재현을 위한 시험 응답입니다. 실제 법령이 아닙니다.'}]}}})
    context.route('https://www.law.go.kr/**',route_api)
    context.route('https://law.go.kr/**',route_api)
    page.goto(BASE+'recover-v1.5.3.html')
    page.wait_for_selector('#setup-view:not(.hidden)')
    assert page.evaluate('window.JLM_BUILD.version')=='1.5.3'
    page.fill('#setup-password','FixturePassword123!');page.fill('#setup-password2','FixturePassword123!');page.fill('#setup-law-oc','fixture-only')
    page.click('#setup-form button[type=submit]');page.wait_for_selector('#app-view:not(.hidden)')
    page.click('[data-page=settings]');page.uncheck('#setting-auto-sync');page.click('#settings-form button[type=submit]');page.wait_for_timeout(300)
    def snapshot():
        return page.evaluate("""async()=>{const db=await new Promise((ok,bad)=>{const r=indexedDB.open('JungwonLawMonitorPWA');r.onsuccess=()=>ok(r.result);r.onerror=()=>bad(r.error)});const out={};for(const name of ['managed','changes','notices','meta']){out[name]=await new Promise((ok,bad)=>{const r=db.transaction(name).objectStore(name).getAll();r.onsuccess=()=>ok(r.result);r.onerror=()=>bad(r.error)});}db.close();return out;}""")
    before=snapshot()
    page.goto(BASE+'recover-v1.5.3.html?fresh=1')
    page.wait_for_selector('#app-view:not(.hidden)')
    after=snapshot()
    assert before['managed']==after['managed'] and before['changes']==after['changes'] and before['notices']==after['notices']
    assert before['meta']==after['meta'],'setup and credentials must not change'
    results.append('Recovery entry preserved managed, history, notices and credential metadata')
    def open_link(url, anchor=False):
        page.evaluate("""({url,anchor})=>{document.querySelector('#fixture-open')?.remove();const b=document.createElement(anchor?'a':'button');b.id='fixture-open';b.textContent='시험 원문';if(anchor)b.href=url;else b.dataset.open=url;document.body.appendChild(b);}""",{'url':url,'anchor':anchor})
        page.click('#fixture-open');page.wait_for_selector('#jlm-reader[open]')
    def close():
        page.click('[data-jlm-close]');page.wait_for_timeout(80)
    open_link('https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=123')
    page.wait_for_selector('#jlm-reader-content .jlm-article')
    assert '시험 응답' in page.inner_text('#jlm-reader-content')
    assert len(context.pages)==1 and not dialogs
    results.append('Old lsInfoR button stayed inside reader; no external basic-info alert')
    close()
    open_link('https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123',True)
    page.wait_for_selector('#jlm-reader-content .jlm-article');assert len(context.pages)==1
    results.append('Native old hyperlink was intercepted, not just data-open buttons');close()
    args='?jlmReader=1&kind=law&MST=999&ID=1747&LM='+quote('테스트 법령')+'&PD=20250101&history=1'
    open_link(BASE+args)
    page.wait_for_selector('#jlm-reader-content .jlm-article')
    assert '다시 확인' in page.inner_text('#jlm-reader-status')
    assert any(q.get('MST')==['123'] for q in requests)
    results.append('BASIC failure re-resolved only exact name, ID and promulgation date');close()
    mode['ambiguous']=True
    open_link(BASE+args.replace('MST=999','MST=998'))
    page.wait_for_selector('[data-jlm-diagnostic]')
    assert page.inner_text('#jlm-reader-content')==''
    assert 'fixture-only' not in page.inner_text('[data-jlm-diagnostic]')
    results.append('Ambiguous same-date history blocked; diagnostic excludes OC')
    page.screenshot(path=str(OUT/'v153-safe-error.png'));close()
    context.route('**/legacy-test.html',lambda r:r.fulfill(content_type='text/html',body=Path('lawmonitor/index.html').read_text().replace(re.search(r'<script src="./app-v.*?</script>',Path('lawmonitor/index.html').read_text()).group(0),'<script src="./app.js?v=1.4.0" defer></script>')))
    page.goto(BASE+'legacy-test.html');page.wait_for_function('window.JLM_BUILD?.version==="1.5.3"')
    results.append('Legacy v1.4.0 entry loaded v1.5.3 bundle')
    assert not errors, errors
    assert not dialogs, dialogs
    assert all(q.get('type')!=['HTML'] for q in requests)
    assert before['managed']==snapshot()['managed']
    browser.close()
Path('test-output/recovery-results.json').write_text(json.dumps({'passed':True,'cases':results},ensure_ascii=False,indent=2))
print(json.dumps({'passed':True,'cases':results},ensure_ascii=False))
