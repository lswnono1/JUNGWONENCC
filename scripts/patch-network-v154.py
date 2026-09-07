from pathlib import Path
root=Path(__file__).resolve().parents[1]
def replace(s,old,new):
    assert s.count(old)==1, repr(old)
    return s.replace(old,new,1)
p=root/'lawmonitor/app-core-6.txt';s=p.read_text();a=s.index('async function fetchRevisions(');b=s.index('\nasync function fetchAppendices(',a)
s=s[:a]+r'''async function fetchRevisions(item) {
  const oc=state.secrets?.lawOc;
  if(!oc)throw v154Error('API_OC','국가법령정보 API OC가 없습니다.');
  const admin=item.kind==='행정규칙', target=admin?'admrul':'eflaw';
  const lawIdentity=await v154LawIdentity(item),rows=[],seen=new Set();
  let scanned=0;
  for(let page=1;page<=50;page++) {
    const params={OC:oc,target,type:'JSON',query:item.name,display:100,page,sort:'ddes',search:1};
    if(!admin){params.nw='1,2,3';params.LID=lawIdentity;}
    const result=v154SearchResult(parsePayload(await fetchText(apiUrl(LAW_URL,params))),['법령명한글','법령명','행정규칙명']);
    const records=result.rows;scanned+=records.length;
    let added=0;
    for(const record of records) {
      const name=recordName(record);
      if(!v154SameName(item.name,name))continue;
      const lawId=admin?'':v151Number(first(record,'법령ID'));
      if(!admin&&!v152EqualId(lawId,lawIdentity))continue;
      const lawMst=admin?'':v151Number(first(record,'법령일련번호','MST'));
      const adminId=admin?v151Number(first(record,'행정규칙일련번호')):'';
      if(!(admin?adminId:lawMst))continue;
      const sourceId=admin?adminId:lawId;
      const promulgationDate=normalizeDate(first(record,'공포일자','발령일자'));
      const enforcementDate=normalizeDate(first(record,'시행일자','효력일자'));
      const revisionType=first(record,'제개정구분명','제개정구분','개정구분')||'구분미상';
      const eventKey=[item.kind,lawMst||sourceId||name,promulgationDate||'날짜미상',revisionType].join(':');
      if(seen.has(eventKey))continue;
      seen.add(eventKey);added++;
      const row={eventKey,managedId:item.id,kind:item.kind,name,sourceId,lawId,lawMst,adminId,revisionType,promulgationDate,enforcementDate,ministry:first(record,'소관부처명','소관부처','부처명'),appendices:[]};
      row.officialUrl=v14MobileLawUrl(row,{date:promulgationDate});rows.push(row);
    }
    if(scanned>=result.total)break;
    if(!records.length || (page>1&&!added&& !admin) || page===50)throw v154Error('HISTORY_INCOMPLETE','개정이력 전체 페이지를 확인하지 못했습니다. 기존 이력은 유지했습니다.');
  }
  if(!rows.length)throw v154Error('HISTORY_NO_MATCH','선택한 법령과 정확히 일치하는 개정이력을 찾지 못했습니다.');
  rows.sort((a,b)=>(b.promulgationDate||'').localeCompare(a.promulgationDate||'')||(b.enforcementDate||'').localeCompare(a.enforcementDate||''));
  return rows;
}
''' +s[b:]
s=replace(s,"    const records = candidateRecords(payload,['별표명','별표서식명','별표일련번호','별표서식일련번호']);","    const result = v154SearchResult(payload,['별표명','별표서식명','별표일련번호','별표서식일련번호']);\n    const records = result.rows;\n    if (!records.length && result.total > 0) throw v154Error('APPENDIX_FORMAT','별표 목록의 형식을 확인하지 못했습니다.');")
p.write_text(s)
p=root/'lawmonitor/app-core-7.txt';s=p.read_text()
for old,new in [
 ('async function syncNow() {','async function syncNow(options = {}) {'),
 ("  const items = (await managedRows()).filter((row) => row.enabled);", "  const items = (await managedRows()).filter((row) => row.enabled && (!options.failedOnly || /실패|미확인|부분|중계/.test(row.checkStatus || '미확인')));"),
 ("return toast('활성 관리대상이 없습니다.');", "return toast(options.failedOnly ? '재조회할 실패 항목이 없습니다.' : '활성 관리대상이 없습니다.');"),
 ('  let noticeFailures = 0;','  let noticeFailures = 0;\n  let appendixFailures = 0;'),
 ("      let lawStatus = '정상';", "      let lawStatus = '정상';\n      let appendixStatus = '미확인';\n      let appendixOk = false;\n      let revisionsOk = false;"),
 ('        const appendices = await fetchAppendices(item);', "        revisionsOk = true;\n      } catch (error) {\n        lawFailures += 1;\n        lawStatus = `개정이력 실패: ${cleanText(error.message).slice(0,180)}`;\n        await addLog('개정이력 실패',`${item.name}: ${error.message}`);\n      }\n\n      if (revisionsOk) try {\n        renderSync(true,`${item.name} 별표 확인`,base + 5);\n        const appendices = await fetchAppendices({...item,...revisions[0]});\n        appendixStatus = '정상';\n        appendixOk = true;"),
 ("            const eventKey = byDate.get(appendix.revisionDate) || revisions[0].eventKey;", "            const eventKey = byDate.get(appendix.revisionDate);\n            if (!eventKey) continue; // No inferred attachment of an undated appendix to another revision."),
 ("        lawFailures += 1;\n        lawStatus = `법령 실패: ${cleanText(error.message).slice(0,80)}`;\n        await addLog('법령 실패',`${item.name}: ${error.message}`);", "        appendixFailures += 1;\n        appendixStatus = `별표 실패: ${cleanText(error.message).slice(0,180)}`;\n        await addLog('별표 실패',`${item.name}: ${error.message}`);"),
 ("      const latest = revisions[0] || {};", "      const latest = revisionsOk ? revisions[0] || {} : {};"),
 ("      const checkStatus = lawStatus === '정상'\n        ? (noticeStatus === '정상' ? '정상' : `법령 정상 · ${noticeStatus}`)\n        : lawStatus;", "      const checkStatus = [lawStatus === '정상' ? '개정이력 정상' : lawStatus, appendixStatus === '정상' ? '별표 정상' : appendixStatus === '미확인' ? '별표 미확인' : appendixStatus, noticeStatus === '정상' ? '' : noticeStatus].filter(Boolean).join(' · ');"),
 ("        lastCheckedAt:new Date().toISOString(),", "        lastCheckedAt:new Date().toISOString(),\n        lastRevisionSuccessAt:revisionsOk ? new Date().toISOString() : item.lastRevisionSuccessAt || '',\n        lastAppendixSuccessAt:appendixOk ? new Date().toISOString() : item.lastAppendixSuccessAt || '',"),
 ("lastNoticeCheckedAt:relayError ? item.lastNoticeCheckedAt || '' : new Date().toISOString(),", "lastNoticeCheckedAt:noticeStatus === '정상' ? new Date().toISOString() : item.lastNoticeCheckedAt || '',"),
 ("await addLog(lawFailures || noticeFailures || relayError ? '부분완료':'완료'", "await addLog(lawFailures || appendixFailures || noticeFailures || relayError ? '부분완료':'완료'"),
 ('법령 실패 ${lawFailures}건 · 입법예고 실패', '개정이력 실패 ${lawFailures}건 · 별표 실패 ${appendixFailures}건 · 입법예고 실패'),
 ("toast(relayError ? '법령 동기화 완료 · 입법예고 중계 설정 필요' : `동기화 완료 · 신규 개정 ${newChanges}건 · 신규 입법예고 ${newNotices}건`,4200);", "toast(lawFailures || appendixFailures || noticeFailures || relayError ? `부분 완료 · 이력 실패 ${lawFailures}건 · 별표 실패 ${appendixFailures}건 · 예고 ${relayError ? '미확인' : noticeFailures+'건 실패'}` : `동기화 완료 · 신규 개정 ${newChanges}건 · 신규 입법예고 ${newNotices}건`,5000);"),
]:s=replace(s,old,new)
p.write_text(s)
p=root/'lawmonitor/app-core-10.txt';s=p.read_text();s=replace(s,"const V153_VERSION = '1.5.3';","const V153_VERSION = '1.5.4';");p.write_text(s)
p=root/'scripts/build-mobile-release.cjs';s=p.read_text().replace("version='1.5.3'","version='1.5.4'").replace('length:10','length:11')
s=replace(s,"fs.writeFileSync(path.join(app,`recover-v${version}.html`),html);", "fs.writeFileSync(path.join(app,`recover-v${version}.html`),html);\nfs.writeFileSync(path.join(app,'recover-v1.5.3.html'),html);")
s=s[:s.index('// Keep the original functional reader tests;')]+"console.log(JSON.stringify({version,file,sha256:digest.toString('hex'),bytes:Buffer.byteLength(bundle)}));\n";p.write_text(s)
p=root/'tests/mobile-links.test.cjs';s=p.read_text().replace('app-v1\\.5\\.3\\.js','app-v1\\.5\\.4\\.js').replace('app-v1.5.3.js','app-v1.5.4.js');p.write_text(s)
p=root/'tests/mobile-reader-browser.cjs';s=p.read_text().replace('length:10','length:11').replace('/v1\\.5\\.3/','/v1\\.5\\.4/');p.write_text(s)
# Exercise the actual HTTPS loader with isolated fixtures, including nonempty saved DB data.
p=root/'tests/mobile-recovery-browser.py';s=p.read_text().replace('1.5.3','1.5.4')
s=s.replace("BASE=os.environ.get('JLM_TEST_BASE','http://127.0.0.1:8080/lawmonitor/')", "BASE='https://jlm-fixture.test/lawmonitor/'")
s=s.replace("    page.goto(BASE+'recover-v1.5.4.html')", "    def static_file(route):\n        import mimetypes\n        rel=urlparse(route.request.url).path.removeprefix('/lawmonitor/') or 'index.html'\n        file=(Path('lawmonitor')/rel).resolve()\n        assert file.is_relative_to(Path('lawmonitor').resolve())\n        route.fulfill(path=str(file),content_type=mimetypes.guess_type(str(file))[0] or 'text/plain')\n    context.route(BASE+'**',static_file)\n    page.goto(BASE+'recover-v1.5.4.html')",1)
s=s.replace("{'LawSearch':{'law':rows}}", "{'LawSearch':{'totalCnt':str(len(rows)),'law':rows}}")
p.write_text(s)
