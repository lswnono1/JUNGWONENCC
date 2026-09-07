/* Jungwon Law Monitor 1.5.3: immutable full application */
(function(){
'use strict';

const $ = (q) => document.querySelector(q);
const $$ = (q) => [...document.querySelectorAll(q)];
const LAW_URL = 'https://www.law.go.kr/DRF/lawSearch.do';
const NOTICE_URL = 'https://www.lawmaking.go.kr/rest/ogLmPp.xml';
const DB_NAME = 'JungwonLawMonitorPWA';
const DB_VERSION = 1;
const AUTO_LOCK_MS = 15 * 60 * 1000;
const API_TIMEOUT_MS = 30000;

const state = {
  db: null,
  setup: null,
  secrets: null,
  keyBytes: null,
  syncing: false,
  page: 'dashboard',
  catalogRows: [],
  lastTouch: Date.now(),
  deferredInstallPrompt: null,
};

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (m) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[m]));
}

function cleanText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function normalizeName(value) {
  return cleanText(value).replace(/[\s·ㆍ,.'"()\[\]{}<>-]+/g, '').toLowerCase();
}

function normalizeDate(value) {
  const raw = cleanText(value);
  const parts = raw.match(/\d+/g) || [];
  if (parts.length >= 3 && parts[0].length === 4) {
    const y = Number(parts[0]), m = Number(parts[1]), d = Number(parts[2]);
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d) {
      return `${String(y).padStart(4,'0')}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    }
  }
  const digits = raw.replace(/\D/g, '');
  if (digits.length >= 8) {
    const y = Number(digits.slice(0,4)), m = Number(digits.slice(4,6)), d = Number(digits.slice(6,8));
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d) {
      return `${digits.slice(0,4)}-${digits.slice(4,6)}-${digits.slice(6,8)}`;
    }
  }
  return '';
}

function first(record, ...keys) {
  if (!record || typeof record !== 'object') return '';
  const lower = new Map(Object.keys(record).map((k) => [k.toLowerCase(), record[k]]));
  for (const key of keys) {
    const value = Object.prototype.hasOwnProperty.call(record, key) ? record[key] : lower.get(key.toLowerCase());
    if (value !== undefined && value !== null && cleanText(value) !== '') return cleanText(value);
  }
  return '';
}

function matchScore(target, candidate) {
  const t = normalizeName(target), c = normalizeName(candidate);
  if (!t || !c) return 0;
  if (t === c) return 100;
  if (c.startsWith(t) || t.startsWith(c)) return 90;
  if (t.includes(c) || c.includes(t)) return 78;
  const ts = new Set(t), cs = new Set(c);
  let common = 0;
  ts.forEach((ch) => { if (cs.has(ch)) common += 1; });
  return Math.round(60 * common / Math.max(ts.size, 1));
}

function absoluteUrl(base, value) {
  const v = cleanText(value);
  if (!v) return '';
  try { return new URL(v, base).href; } catch { return ''; }
}

function officialLawUrl(kind, name, supplied = '') {
  if (supplied) return absoluteUrl('https://www.law.go.kr', supplied);
  const segment = kind === '행정규칙' ? '행정규칙' : '법령';
  return `https://www.law.go.kr/${encodeURIComponent(segment)}/${encodeURIComponent(name)}`;
}

function noticeUrl(sequence, supplied = '') {
  if (supplied) return absoluteUrl('https://opinion.lawmaking.go.kr', supplied);
  return sequence
    ? `https://opinion.lawmaking.go.kr/gcom/ogLmPp/${encodeURIComponent(sequence)}`
    : 'https://opinion.lawmaking.go.kr/gcom/ogLmPp';
}

function formatDateTime(value) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat('ko-KR', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(d);
}

function toast(message, ms = 2600) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove('show'), ms);
}

function setBusy(el, busy) {
  if (!el) return;
  el.disabled = !!busy;
  el.classList.toggle('loading', !!busy);
}

function bytesToB64(bytes) {
  let binary = '';
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < arr.length; i += 1) binary += String.fromCharCode(arr[i]);
  return btoa(binary);
}

function b64ToBytes(value) {
  const binary = atob(value);
  const arr = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) arr[i] = binary.charCodeAt(i);
  return arr;
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
}

async function deriveKeyBytes(password, salt) {
  const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({name:'PBKDF2', salt, iterations:180000, hash:'SHA-256'}, material, 256);
  return new Uint8Array(bits);
}

async function makeVerifier(keyBytes) {
  const marker = new TextEncoder().encode('JUNGWON-LAW-MONITOR-VERIFY-v1');
  const joined = new Uint8Array(keyBytes.length + marker.length);
  joined.set(keyBytes, 0); joined.set(marker, keyBytes.length);
  return bytesToB64(await sha256(joined));
}

async function encryptJson(value, keyBytes) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await crypto.subtle.importKey('raw', keyBytes, {name:'AES-GCM'}, false, ['encrypt']);
  const plain = new TextEncoder().encode(JSON.stringify(value));
  const cipher = new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM', iv}, key, plain));
  return {iv: bytesToB64(iv), data: bytesToB64(cipher)};
}

async function decryptJson(payload, keyBytes) {
  const key = await crypto.subtle.importKey('raw', keyBytes, {name:'AES-GCM'}, false, ['decrypt']);
  const plain = await crypto.subtle.decrypt({name:'AES-GCM', iv:b64ToBytes(payload.iv)}, key, b64ToBytes(payload.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta', {keyPath:'key'});
      if (!db.objectStoreNames.contains('managed')) {
        const s = db.createObjectStore('managed', {keyPath:'id', autoIncrement:true});
        s.createIndex('kindName', ['kind','name'], {unique:true});
        s.createIndex('enabled', 'enabled', {unique:false});
      }
      if (!db.objectStoreNames.contains('changes')) {
        const s = db.createObjectStore('changes', {keyPath:'eventKey'});
        s.createIndex('name', 'name', {unique:false});
        s.createIndex('date', 'promulgationDate', {unique:false});
        s.createIndex('isNew', 'isNew', {unique:false});
      }
      if (!db.objectStoreNames.contains('notices')) {
        const s = db.createObjectStore('notices', {keyPath:'noticeKey'});
        s.createIndex('status', 'status', {unique:false});
        s.createIndex('isNew', 'isNew', {unique:false});
      }
      if (!db.objectStoreNames.contains('logs')) db.createObjectStore('logs', {keyPath:'id', autoIncrement:true});
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbRequest(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function txStore(name, mode = 'readonly') {
  return state.db.transaction(name, mode).objectStore(name);
}

async function dbGet(store, key) { return idbRequest(txStore(store).get(key)); }
async function dbGetAll(store) { return idbRequest(txStore(store).getAll()); }
async function dbPut(store, value) { return idbRequest(txStore(store, 'readwrite').put(value)); }
async function dbAdd(store, value) { return idbRequest(txStore(store, 'readwrite').add(value)); }
async function dbDelete(store, key) { return idbRequest(txStore(store, 'readwrite').delete(key)); }
async function dbClear(store) { return idbRequest(txStore(store, 'readwrite').clear()); }

async function getMeta(key, fallback = null) {
  const row = await dbGet('meta', key);
  return row ? row.value : fallback;
}

async function setMeta(key, value) {
  await dbPut('meta', {key, value});
}

async function addLog(status, message) {
  await dbAdd('logs', {status, message: cleanText(message).slice(0,1000), at:new Date().toISOString()});
  const rows = await dbGetAll('logs');
  if (rows.length > 120) {
    rows.sort((a,b) => a.id - b.id);
    for (const row of rows.slice(0, rows.length - 120)) await dbDelete('logs', row.id);
  }
}

function xmlNodeToObject(node) {
  const children = [...node.children];
  if (!children.length) return cleanText(node.textContent);
  const out = {};
  for (const child of children) {
    const key = child.localName || child.nodeName;
    const value = xmlNodeToObject(child);
    if (Object.prototype.hasOwnProperty.call(out, key)) {
      if (!Array.isArray(out[key])) out[key] = [out[key]];
      out[key].push(value);
    } else out[key] = value;

  }
  return out;
}

function parsePayload(text) {
  const trimmed = String(text || '').replace(/^\uFEFF/, '').trim();
  if (!trimmed) return {};
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) return JSON.parse(trimmed);
  const doc = new DOMParser().parseFromString(trimmed, 'application/xml');
  if (doc.querySelector('parsererror')) throw new Error('공식 API 응답을 해석하지 못했습니다.');
  return {[doc.documentElement.localName || doc.documentElement.nodeName]: xmlNodeToObject(doc.documentElement)};
}

function walkObjects(value, output = []) {
  if (Array.isArray(value)) value.forEach((v) => walkObjects(v, output));
  else if (value && typeof value === 'object') {
    output.push(value);
    Object.values(value).forEach((v) => walkObjects(v, output));
  }
  return output;
}

function candidateRecords(payload, requiredKeys) {
  const seen = new Set();
  const output = [];
  for (const record of walkObjects(payload)) {
    const keys = Object.keys(record).map((k) => k.toLowerCase());
    if (!requiredKeys.some((r) => keys.includes(r.toLowerCase()))) continue;
    const sig = JSON.stringify(record);
    if (!seen.has(sig)) { seen.add(sig); output.push(record); }
  }
  return output;
}

function recordName(record) {
  return first(record, '법령명한글','법령명','법령명_한글','행정규칙명','법령안명','입법예고명','별표명','별표서식명','별표서식제목','별표제목','title','lsNm');
}

async function fetchText(url, timeoutMs = API_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {method:'GET', cache:'no-store', credentials:'omit', signal:controller.signal});
    if (!response.ok) throw new Error(`공식 API 오류 HTTP ${response.status}`);
    return await response.text();
  } catch (error) {
    if (error && error.name === 'AbortError') throw new Error('공식 API 응답 시간이 초과되었습니다.');
    if (error instanceof TypeError) throw new Error('공식 API에 연결하지 못했습니다. 인터넷 연결 또는 브라우저의 교차 출처 요청 허용 여부를 확인하세요.');
    throw error;
  } finally { clearTimeout(timer); }
}

function apiUrl(base, params) {
  const url = new URL(base);
  Object.entries(params).forEach(([k,v]) => {
    if (v !== undefined && v !== null && String(v) !== '') url.searchParams.set(k, String(v));
  });
  return url.href;
}

function normalizeOc(value) {
  return cleanText(value).split('@')[0].trim();
}

async function searchCatalog(query, kind = '전체') {
  const oc = state.secrets?.lawOc;
  if (!oc) throw new Error('설정에서 국가법령정보 API OC를 입력하세요.');
  const targets = [];
  if (kind === '전체' || kind === '법령') targets.push(['law','법령']);
  if (kind === '전체' || kind === '행정규칙') targets.push(['admrul','행정규칙']);
  const results = [];
  for (const [target, displayKind] of targets) {
    const text = await fetchText(apiUrl(LAW_URL, {OC:oc,target,type:'JSON',query,display:100,page:1,sort:'ddes'}));
    const payload = parsePayload(text);
    for (const record of candidateRecords(payload, ['법령명','행정규칙명','법령명한글','법령ID'])) {
      const name = recordName(record);
      if (!name) continue;
      const sourceId = first(record,'법령ID','행정규칙ID','법령일련번호','행정규칙일련번호','MST','id');
      const link = first(record,'법령상세링크','행정규칙상세링크','상세링크','link','url');
      results.push({
        kind: displayKind,
        name,
        sourceId,
        officialUrl: officialLawUrl(displayKind, name, link),
        revisionType: first(record,'제개정구분명','제개정구분','개정구분'),
        promulgationDate: normalizeDate(first(record,'공포일자','발령일자','개정일자')),
        enforcementDate: normalizeDate(first(record,'시행일자','효력일자')),
        ministry: first(record,'소관부처명','소관부처','부처명'),
        score: matchScore(query,name),
      });
    }
  }
  const unique = new Map();
  results.sort((a,b) => b.score - a.score || a.name.localeCompare(b.name,'ko'));
  results.forEach((r) => { const k = `${r.kind}|${r.name}`; if (!unique.has(k)) unique.set(k,r); });
  return [...unique.values()].slice(0,100);
}

async function fetchRevisions(item) {
  const oc = state.secrets?.lawOc;
  if (!oc) throw new Error('국가법령정보 API OC가 없습니다.');
  const target = item.kind === '행정규칙' ? 'admrul' : 'eflaw';
  const rows = [];
  const seen = new Set();
  for (let page = 1; page <= 50; page += 1) {
    const params = {OC:oc,target,type:'JSON',query:item.name,display:100,page,sort:'ddes'};
    if (item.kind !== '행정규칙') { params.search = 1; params.nw = '1,2,3'; }
    const payload = parsePayload(await fetchText(apiUrl(LAW_URL, params)));
    const records = candidateRecords(payload, ['법령명','행정규칙명','법령ID','공포일자','발령일자']);
    let added = 0;
    for (const record of records) {
      const candidate = recordName(record);
      if (matchScore(item.name,candidate) < 78) continue;
      const sourceId = first(record,'법령ID','행정규칙ID','법령일련번호','행정규칙일련번호','MST','id') || item.sourceId || '';
      const promulgationDate = normalizeDate(first(record,'공포일자','발령일자','개정일자','promulgationDate'));
      const enforcementDate = normalizeDate(first(record,'시행일자','효력일자','enforcementDate'));
      const revisionType = first(record,'제개정구분명','제개정구분','개정구분','revisionType') || '구분미상';
      const link = first(record,'법령상세링크','행정규칙상세링크','상세링크','link','url');
      const name = candidate || item.name;
      const eventKey = [item.kind, sourceId || name, promulgationDate || '날짜미상', revisionType].join(':');
      if (seen.has(eventKey)) continue;
      seen.add(eventKey); added += 1;
      rows.push({eventKey,managedId:item.id,kind:item.kind,name,sourceId,revisionType,promulgationDate,enforcementDate,ministry:first(record,'소관부처명','소관부처','부처명','ministry'),officialUrl:officialLawUrl(item.kind,name,link),appendices:[]});
    }
    if ((page > 1 && added === 0) || records.length < 100) break;
  }
  rows.sort((a,b) => (b.promulgationDate || '').localeCompare(a.promulgationDate || '') || (b.enforcementDate || '').localeCompare(a.enforcementDate || ''));
  return rows;
}

function appendixComparisonUrl(record, itemKind, fallbackDate) {
  const appendixSeq = first(record,'별표일련번호','별표서식일련번호','별표명ID');
  const lawSerial = first(record,'관련법령일련번호','법령일련번호');
  const lawIdRaw = first(record,'관련법령ID','법령ID').replace(/\D/g,'');
  const lawId = lawIdRaw ? lawIdRaw.padStart(6,'0') : '';
  const digits = first(record,'별표번호','별표서식번호').replace(/\D/g,'');
  const appendixNo = digits ? digits.padStart(4,'0') : '';
  const combined = `${first(record,'별표종류','별표서식구분명')} ${first(record,'별표명','별표서식명','별표서식제목','별표제목')}`;
  let classCode = '110201';
  [['서식','110202'],['별지','110203'],['별도','110204'],['부록','110205'],['별표','110201']].some(([needle,code]) => {
    if (combined.includes(needle)) { classCode = code; return true; }
    return false;
  });
  const effective = normalizeDate(first(record,'시행일자','효력일자','공포일자') || fallbackDate).replace(/-/g,'');
  if (itemKind === '행정규칙' || !appendixSeq || !lawSerial || !lawId || !appendixNo || !effective) return '';
  return apiUrl('https://www.law.go.kr/LSW/lsBylDiffHwpP.do', {lsiSeq:lawSerial,lsId:lawId,vSct:'*',bylSeq:appendixSeq,bylNo:appendixNo,bylBrNo:'00',bylClsCd:classCode,bylEfYd:effective});
}

async function fetchAppendices(item) {
  const oc = state.secrets?.lawOc;
  if (!oc) return [];
  const target = item.kind === '행정규칙' ? 'admbyl' : 'licbyl';
  const output = [], seen = new Set();
  for (let page = 1; page <= 30; page += 1) {
    const payload = parsePayload(await fetchText(apiUrl(LAW_URL,{OC:oc,target,type:'JSON',search:2,query:item.name,display:100,page,sort:'lasc'})));
    const records = candidateRecords(payload,['별표명','별표서식명','별표일련번호','별표서식일련번호']);
    let added = 0;
    for (const record of records) {
      const title = first(record,'별표명','별표서식명','별표서식제목','별표제목');
      const seq = first(record,'별표일련번호','별표서식일련번호','별표명ID','id');
      const revisionDate = normalizeDate(first(record,'공포일자','발령일자','개정일자','시행일자','효력일자'));
      const key = [seq,title,revisionDate].join('|');
      if (!title || seen.has(key)) continue;
      seen.add(key); added += 1;
      const rawKind = first(record,'별표종류','별표서식구분명');
      const appendixKind = ['별표','서식','별지','별도','부록'].find((x) => rawKind.includes(x) || title.includes(x)) || '별표';
      const link = first(record,'별표법령상세링크','별표행정규칙상세링크');
      output.push({
        appendixKey:key,title,appendixKind,revisionDate,
        revisionType:first(record,'제개정구분명','제개정구분','개정구분'),
        appendixNo:first(record,'별표번호','별표서식번호'),
        officialUrl:absoluteUrl('https://www.law.go.kr',link),
        comparisonUrl:appendixComparisonUrl(record,item.kind,revisionDate),
      });
    }
    if ((page > 1 && added === 0) || records.length < 100) break;
  }
  return output;
}

async function fetchNotices(lawName) {
  const oc = state.secrets?.noticeOc || state.secrets?.lawOc;
  if (!oc) throw new Error('입법예고 API OC가 없습니다.');
  const output = [], seen = new Set();
  for (const [diff,status] of [[0,'진행 중'],[1,'종료']]) {
    for (let page = 1; page <= 30; page += 1) {
      const payload = parsePayload(await fetchText(apiUrl(NOTICE_URL,{OC:oc,diff,lsNm:lawName,pageIndex:page,pageUnit:100})));
      const records = candidateRecords(payload,['법령안명','입법예고명','입법예고ID','시작일자','종료일자']);
      let added = 0;
      for (const record of records) {
        const title = first(record,'법령안명','입법예고명','title','법령명');
        if (!title || matchScore(lawName,title) < 35) continue;
        const sequence = first(record,'입법예고ID','sequence','seq','id');
        const noticeNo = first(record,'공고번호','입법예고번호','noticeNo','notice_no');
        const startDate = normalizeDate(first(record,'시작일자','입법예고시작일자','startDate','start_date'));
        const endDate = normalizeDate(first(record,'종료일자','입법예고종료일자','endDate','end_date'));
        const noticeKey = `notice:${sequence || noticeNo || title}:${startDate || '날짜미상'}`;
        if (seen.has(noticeKey)) continue;
        seen.add(noticeKey); added += 1;
        output.push({noticeKey,title,ministry:first(record,'소관부처명','소관부처','부처명','ministry'),noticeNo,startDate,endDate,status,officialUrl:noticeUrl(sequence,first(record,'상세링크','입법예고상세링크','link','url')),matchedItem:lawName});
      }
      if ((page > 1 && added === 0) || records.length < 100) break;
    }
  }
  return output;
}

async function managedRows() {
  const rows = await dbGetAll('managed');
  return rows.sort((a,b) => a.kind.localeCompare(b.kind,'ko') || a.name.localeCompare(b.name,'ko'));
}

async function findManaged(kind, name) {
  const rows = await managedRows();
  return rows.find((r) => r.kind === kind && r.name === name) || null;
}

async function addManaged(row) {
  const existing = await findManaged(row.kind,row.name);
  const now = new Date().toISOString();
  if (existing) {
    await dbPut('managed',{...existing,...row,enabled:true,updatedAt:now});
    return existing.id;
  }
  return dbAdd('managed',{kind:row.kind === '행정규칙' ? '행정규칙':'법령',name:cleanText(row.name),sourceId:row.sourceId || '',officialUrl:row.officialUrl || '',enabled:true,lastEventKey:'',lastRevisionType:'',lastRevisionDate:'',lastEnforcementDate:'',lastCheckedAt:'',checkStatus:'미확인',createdAt:now,updatedAt:now});
}

async function seedManaged() {
  if ((await managedRows()).length) return;
  const names = [
    ['법령','소방시설 설치 및 관리에 관한 법률'],
    ['법령','소방시설 설치 및 관리에 관한 법률 시행령'],
    ['법령','소방시설 설치 및 관리에 관한 법률 시행규칙'],
    ['법령','화재의 예방 및 안전관리에 관한 법률'],
    ['법령','화재의 예방 및 안전관리에 관한 법률 시행령'],
    ['법령','건축법'],
    ['법령','건축법 시행령'],
    ['법령','건축물의 피난ㆍ방화구조 등의 기준에 관한 규칙'],
  ];

  for (const [kind,name] of names) await addManaged({kind,name});
}

async function setupApp(event) {
  event.preventDefault();
  const password = $('#setup-password').value;
  if (password !== $('#setup-password2').value) return toast('비밀번호 확인이 일치하지 않습니다.');
  if (password.length < 8) return toast('비밀번호는 8자 이상으로 설정하세요.');
  const lawOc = normalizeOc($('#setup-law-oc').value);
  if (!lawOc) return toast('국가법령정보 API OC를 입력하세요.');
  setBusy(event.submitter,true);
  try {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const keyBytes = await deriveKeyBytes(password,salt);
    const setup = {
      ownerName: cleanText($('#setup-owner').value) || '이상우',
      salt: bytesToB64(salt),
      verifier: await makeVerifier(keyBytes),
      encryptedSecrets: await encryptJson({lawOc,noticeOc:normalizeOc($('#setup-notice-oc').value)},keyBytes),
      autoSync:true,
      syncIntervalHours:12,
      createdAt:new Date().toISOString(),
    };
    await setMeta('setup',setup);
    state.setup = setup; state.keyBytes = keyBytes; state.secrets = await decryptJson(setup.encryptedSecrets,keyBytes);
    await seedManaged();
    await addLog('설정','휴대폰 독립형 초기 설정 완료');
    showApp();
    await refreshAll();
    toast('초기 설정이 완료되었습니다.');
  } catch (error) { toast(error.message || '초기 설정에 실패했습니다.'); }
  finally { setBusy(event.submitter,false); }
}

async function login(event) {
  event.preventDefault();
  setBusy(event.submitter,true);
  try {
    const keyBytes = await deriveKeyBytes($('#login-password').value,b64ToBytes(state.setup.salt));
    if (await makeVerifier(keyBytes) !== state.setup.verifier) throw new Error('비밀번호가 올바르지 않습니다.');
    const secrets = await decryptJson(state.setup.encryptedSecrets,keyBytes);
    state.keyBytes = keyBytes; state.secrets = secrets; state.lastTouch = Date.now();
    $('#login-password').value = '';
    showApp();
    await refreshAll();
    maybeAutoSync();
  } catch (error) { toast(error.message || '잠금을 해제하지 못했습니다.'); }
  finally { setBusy(event.submitter,false); }
}

function showSetup() {
  $('#setup-view').classList.remove('hidden');
  $('#login-view').classList.add('hidden');
  $('#app-view').classList.add('hidden');
}

function showLogin() {
  state.secrets = null; state.keyBytes = null;
  $('#setup-view').classList.add('hidden');
  $('#login-view').classList.remove('hidden');
  $('#app-view').classList.add('hidden');
  setTimeout(() => $('#login-password').focus(),100);
}

function showApp() {
  $('#setup-view').classList.add('hidden');
  $('#login-view').classList.add('hidden');
  $('#app-view').classList.remove('hidden');
  $('#owner-name').textContent = state.setup.ownerName || '이상우';
  go('dashboard');
}

function go(page) {
  state.page = page;
  $$('.page').forEach((p) => p.classList.toggle('active',p.id === `page-${page}`));
  $$('.bottom-nav button').forEach((b) => b.classList.toggle('active',b.dataset.page === page));
  window.scrollTo(0,0);
  if (page === 'managed') loadManaged();
  if (page === 'changes') loadChanges();
  if (page === 'notices') loadNotices();
  if (page === 'settings') loadSettings();
}

async function refreshDashboard() {
  const managed = await managedRows();
  const changes = await dbGetAll('changes');
  const notices = await dbGetAll('notices');
  $('#count-managed').textContent = managed.filter((r) => r.enabled).length;
  $('#count-changes').textContent = changes.length;
  $('#count-new-changes').textContent = changes.filter((r) => r.isNew).length;
  $('#count-active-notices').textContent = notices.filter((r) => r.status === '진행 중').length;
  const last = await getMeta('lastSync','');
  $('#last-sync').textContent = last ? `최근 동기화 ${formatDateTime(last)}` : '아직 동기화하지 않았습니다.';
}

async function refreshAll() {
  await refreshDashboard();
  if (state.page === 'managed') await loadManaged();
  if (state.page === 'changes') await loadChanges();
  if (state.page === 'notices') await loadNotices();
}

function renderSync(running,message,progress) {
  $('#sync-card').classList.toggle('hidden',!running);
  $('#sync-message').textContent = message || '동기화 중';
  $('#sync-progress').value = progress || 0;
  $('#sync-progress-text').textContent = `${Math.round(progress || 0)}%`;
  $('#sync-button').classList.toggle('loading',running);
}

async function syncNow() {
  if (state.syncing) return toast('이미 동기화 중입니다.');
  if (!navigator.onLine) return toast('인터넷 연결을 확인하세요.');
  if (!state.secrets?.lawOc) return toast('설정에서 API OC를 입력하세요.');
  state.syncing = true;
  const items = (await managedRows()).filter((r) => r.enabled);
  if (!items.length) { state.syncing = false; return toast('활성 관리대상이 없습니다.'); }
  let failures = 0, newChanges = 0, newNotices = 0;
  renderSync(true,'동기화 준비',1);
  try {
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      const baseProgress = i / items.length * 100;
      renderSync(true,`${item.name} 개정 이력 확인`,baseProgress + 2);
      try {
        const revisions = await fetchRevisions(item);
        const hadBaseline = !!item.lastEventKey;
        for (const row of revisions) {
          const existing = await dbGet('changes',row.eventKey);
          if (!existing) {
            row.isNew = hadBaseline;
            row.detectedAt = new Date().toISOString();
            row.lastSeenAt = row.detectedAt;
            if (row.isNew) newChanges += 1;
            await dbPut('changes',row);
          } else await dbPut('changes',{...existing,...row,appendices:existing.appendices || [],lastSeenAt:new Date().toISOString()});
        }
        renderSync(true,`${item.name} 별표·서식 확인`,baseProgress + 5);
        const appendices = await fetchAppendices(item);
        if (appendices.length && revisions.length) {
          const byDate = new Map();
          revisions.forEach((r) => {
            const key = r.promulgationDate || r.enforcementDate || '';
            if (!byDate.has(key)) byDate.set(key,r.eventKey);
          });
          for (const appendix of appendices) {
            let eventKey = byDate.get(appendix.revisionDate);
            if (!eventKey) eventKey = revisions[0].eventKey;
            const change = await dbGet('changes',eventKey);
            if (change) {
              const list = Array.isArray(change.appendices) ? change.appendices : [];
              const map = new Map(list.map((x) => [x.appendixKey,x]));
              map.set(appendix.appendixKey,appendix);
              change.appendices = [...map.values()];
              await dbPut('changes',change);
            }
          }
        }
        renderSync(true,`${item.name} 입법예고 확인`,baseProgress + 8);
        const notices = await fetchNotices(item.name);
        const hadNoticeBaseline = !!item.lastNoticeCheckedAt;
        for (const notice of notices) {
          const existing = await dbGet('notices',notice.noticeKey);
          if (!existing) {
            notice.isNew = hadNoticeBaseline;
            notice.detectedAt = new Date().toISOString();
            if (notice.isNew) newNotices += 1;
            await dbPut('notices',notice);
          } else await dbPut('notices',{...existing,...notice});
        }
        const latest = revisions[0] || {};
        await dbPut('managed',{...item,sourceId:latest.sourceId || item.sourceId || '',officialUrl:latest.officialUrl || item.officialUrl || '',lastEventKey:latest.eventKey || item.lastEventKey || '',lastRevisionType:latest.revisionType || '',lastRevisionDate:latest.promulgationDate || '',lastEnforcementDate:latest.enforcementDate || '',lastCheckedAt:new Date().toISOString(),lastNoticeCheckedAt:new Date().toISOString(),checkStatus:'정상',updatedAt:new Date().toISOString()});
      } catch (error) {
        failures += 1;
        await dbPut('managed',{...item,lastCheckedAt:new Date().toISOString(),checkStatus:`실패: ${cleanText(error.message).slice(0,100)}`,updatedAt:new Date().toISOString()});
        await addLog('실패',`${item.name}: ${error.message}`);
      }
      renderSync(true,`${i+1}/${items.length} 완료`,(i+1)/items.length*100);
    }
    const now = new Date().toISOString();
    await setMeta('lastSync',now);
    await addLog(failures ? '부분완료':'완료',`관리대상 ${items.length}건, 신규 개정 ${newChanges}건, 신규 입법예고 ${newNotices}건, 실패 ${failures}건`);
    toast(failures ? `동기화 완료 · 실패 ${failures}건` : `동기화 완료 · 신규 개정 ${newChanges}건`);
  } finally {
    state.syncing = false;
    renderSync(false,'',0);
    await refreshAll();
  }
}

async function maybeAutoSync() {
  if (!state.setup?.autoSync || state.syncing || !navigator.onLine) return;
  const last = await getMeta('lastSync','');
  const interval = Math.max(6,Number(state.setup.syncIntervalHours || 12)) * 3600000;
  if (!last || Date.now() - new Date(last).getTime() >= interval) setTimeout(syncNow,700);
}

async function loadManaged() {
  const q = normalizeName($('#managed-filter').value);
  const rows = (await managedRows()).filter((r) => !q || normalizeName(`${r.kind} ${r.name}`).includes(q));
  $('#managed-list').innerHTML = rows.length ? rows.map((r) => `
    <article class="item">
      <div class="item-head"><div><h3>${esc(r.name)}</h3><p>${esc(r.kind)} · ${esc(r.checkStatus || '미확인')}</p><p>최근 개정 ${esc(r.lastRevisionDate || '-')} · 시행 ${esc(r.lastEnforcementDate || '-')}</p></div>
      <label class="switch"><input type="checkbox" data-toggle="${r.id}" ${r.enabled ? 'checked':''}><span></span></label></div>
      <div class="item-actions">${r.officialUrl ? `<button data-open="${esc(r.officialUrl)}">공식 원문</button>`:''}<button class="delete" data-delete="${r.id}" data-name="${esc(r.name)}">삭제</button></div>
    </article>`).join('') : '<div class="empty">관리대상이 없습니다.</div>';
}

function manualAddCard(query, message = '') {
  const kind = $('#catalog-kind').value === '행정규칙' ? '행정규칙' : '법령';
  return `<article class="item"><div><h3>입력한 이름으로 직접 추가</h3>${message ? `<p>${esc(message)}</p>` : ''}<p><b>${esc(kind)}</b> · ${esc(query)}</p></div><div class="item-actions"><button class="primary" data-manual-add="1">직접 추가</button></div></article>`;
}

async function openCatalog() {
  $('#catalog-results').innerHTML = '<div class="empty">법규명을 검색하세요. API 검색이 안 되면 입력한 이름으로 직접 추가할 수 있습니다.</div>';
  $('#search-dialog').showModal();
  setTimeout(() => $('#catalog-q').focus(),100);
}

async function runCatalogSearch() {
  const q = cleanText($('#catalog-q').value);
  if (q.length < 2) return toast('두 글자 이상 입력하세요.');
  const btn = $('#catalog-search'); setBusy(btn,true);
  $('#catalog-results').innerHTML = '<div class="empty">공식 API 검색 중…</div>';
  try {
    const rows = await searchCatalog(q,$('#catalog-kind').value);
    state.catalogRows = rows;
    const searchResults = rows.map((r,i) => `
      <article class="item"><div class="item-head"><div><h3>${esc(r.name)}</h3><p>${esc(r.kind)} · ${esc(r.ministry || '')}</p><p>${esc(r.promulgationDate || '')} ${esc(r.revisionType || '')}</p></div><span class="tag">일치 ${r.score}</span></div><div class="item-actions"><button class="primary" data-add-index="${i}">관리대상 추가</button></div></article>`).join('');
    $('#catalog-results').innerHTML = `${searchResults}${manualAddCard(q, rows.length ? '찾는 항목이 검색 결과와 다를 때 사용하세요.' : '검색 결과가 없어도 직접 등록할 수 있습니다.')}`;
  } catch (error) {
    state.catalogRows = [];
    $('#catalog-results').innerHTML = manualAddCard(q, `공식 API 검색 실패: ${error.message}`);
  }
  finally { setBusy(btn,false); }
}

async function addCatalog(index) {
  const row = state.catalogRows[index];
  if (!row) return toast('추가할 항목을 다시 선택하세요.');
  await addManaged(row);
  $('#search-dialog').close();
  toast(`${row.name} 추가 완료`);
  await loadManaged(); await refreshDashboard();
}

async function addManualCatalog() {
  const name = cleanText($('#catalog-q').value);
  if (name.length < 2) return toast('법규명을 두 글자 이상 입력하세요.');
  const selected = $('#catalog-kind').value;
  const kind = selected === '행정규칙' ? '행정규칙' : '법령';
  await addManaged({kind,name,officialUrl:officialLawUrl(kind,name)});
  $('#search-dialog').close();
  toast(`${name} 직접 추가 완료`);
  await loadManaged(); await refreshDashboard();
}

async function loadChanges() {
  const q = normalizeName($('#changes-q').value);
  const from = $('#changes-from').value, to = $('#changes-to').value;
  const newOnly = $('#changes-new-only').checked;

  const rows = (await dbGetAll('changes')).filter((r) => {
    const date = r.promulgationDate || r.enforcementDate || '';
    return (!q || normalizeName(`${r.name} ${r.revisionType} ${r.ministry}`).includes(q)) && (!from || date >= from) && (!to || date <= to) && (!newOnly || r.isNew);
  }).sort((a,b) => (b.promulgationDate || '').localeCompare(a.promulgationDate || '') || (b.detectedAt || '').localeCompare(a.detectedAt || ''));
  $('#changes-list').innerHTML = rows.length ? rows.slice(0,1500).map((r) => {
    const count = Array.isArray(r.appendices) ? r.appendices.length : 0;
    return `<article class="item ${r.isNew ? 'new':''}"><div class="item-head"><div><h3>${esc(r.name)}</h3><p>${esc(r.revisionType || '구분미상')} · 공포 ${esc(r.promulgationDate || '-')} · 시행 ${esc(r.enforcementDate || '-')}</p><p>${esc(r.ministry || '')}</p></div>${r.isNew ? '<span class="tag red">NEW</span>':''}</div><div class="tags">${count ? `<span class="tag red">별표·서식 ${count}건</span>`:''}<span class="tag">${esc(r.kind)}</span></div><div class="item-actions">${r.officialUrl ? `<button data-open="${esc(r.officialUrl)}">법령 원문</button>`:''}${count ? `<button data-appendix="${esc(r.eventKey)}" data-title="${esc(`${r.name} ${r.promulgationDate}`)}">별표 비교</button>`:''}</div></article>`;
  }).join('') : '<div class="empty">조건에 맞는 개정사항이 없습니다.</div>';
}

async function openAppendices(eventKey,title) {
  const row = await dbGet('changes',eventKey);
  const items = row?.appendices || [];
  $('#appendix-dialog-subtitle').textContent = title;
  $('#appendix-list').innerHTML = items.length ? items.map((r) => `<article class="item"><h3>${esc(r.title)}</h3><p>${esc(r.appendixKind)} · ${esc(r.revisionDate || '-')} · ${esc(r.revisionType || '')}</p><div class="item-actions">${r.comparisonUrl ? `<button data-open="${esc(r.comparisonUrl)}">법제처 별표·서식 비교</button>` : r.officialUrl ? `<button data-open="${esc(r.officialUrl)}">공식 별표 보기</button>`:''}</div></article>`).join('') : '<div class="empty">연결된 별표·서식이 없습니다.</div>';
  $('#appendix-dialog').showModal();
}

async function loadNotices() {
  const q = normalizeName($('#notices-q').value);
  const status = $('#notices-status').value;
  const from = $('#notices-from').value, to = $('#notices-to').value;
  const rows = (await dbGetAll('notices')).filter((r) => {
    const date = r.startDate || r.endDate || '';
    return (!q || normalizeName(`${r.title} ${r.matchedItem} ${r.ministry}`).includes(q)) && (status === '전체' || r.status === status) && (!from || date >= from) && (!to || date <= to);
  }).sort((a,b) => (b.startDate || '').localeCompare(a.startDate || ''));
  $('#notices-list').innerHTML = rows.length ? rows.slice(0,1500).map((r) => `<article class="item ${r.isNew ? 'new':''}"><div class="item-head"><div><h3>${esc(r.title)}</h3><p>${esc(r.ministry || '')} · ${esc(r.noticeNo || '')}</p><p>${esc(r.startDate || '-')} ~ ${esc(r.endDate || '-')} · ${esc(r.matchedItem || '')}</p></div><span class="tag ${r.status === '진행 중' ? 'green':''}">${esc(r.status)}</span></div><div class="item-actions">${r.officialUrl ? `<button data-open="${esc(r.officialUrl)}">입법예고 상세</button>`:''}</div></article>`).join('') : '<div class="empty">조건에 맞는 입법예고가 없습니다.</div>';
}

async function loadSettings() {
  $('#setting-owner').value = state.setup.ownerName || '이상우';
  $('#setting-law-oc').value = state.secrets?.lawOc || '';
  $('#setting-notice-oc').value = state.secrets?.noticeOc || '';
  $('#setting-auto-sync').checked = !!state.setup.autoSync;
  $('#setting-sync-interval').value = String(state.setup.syncIntervalHours || 12);
  const logs = (await dbGetAll('logs')).sort((a,b) => b.id - a.id).slice(0,30);
  $('#logs-list').innerHTML = logs.length ? logs.map((x) => `<div><b>${esc(x.status)}</b><span>${esc(formatDateTime(x.at))} · ${esc(x.message)}</span></div>`).join('') : '<div>로그가 없습니다.</div>';
}

async function saveSettings(event) {
  event.preventDefault();
  setBusy(event.submitter,true);
  try {
    const ownerName = cleanText($('#setting-owner').value) || '이상우';
    const secrets = {lawOc:normalizeOc($('#setting-law-oc').value),noticeOc:normalizeOc($('#setting-notice-oc').value)};
    if (!secrets.lawOc) throw new Error('국가법령정보 API OC를 입력하세요.');
    const setup = {...state.setup,ownerName,autoSync:$('#setting-auto-sync').checked,syncIntervalHours:Number($('#setting-sync-interval').value),encryptedSecrets:await encryptJson(secrets,state.keyBytes)};
    await setMeta('setup',setup); state.setup = setup; state.secrets = secrets;
    $('#owner-name').textContent = ownerName;
    await addLog('설정','API 및 자동 동기화 설정 변경');
    toast('설정을 저장했습니다.');
  } catch (error) { toast(error.message); }
  finally { setBusy(event.submitter,false); }
}

async function changePassword(event) {
  event.preventDefault();
  const current = $('#current-password').value, next = $('#new-password').value;
  if (next.length < 8) return toast('새 비밀번호는 8자 이상이어야 합니다.');
  setBusy(event.submitter,true);
  try {
    const oldKey = await deriveKeyBytes(current,b64ToBytes(state.setup.salt));
    if (await makeVerifier(oldKey) !== state.setup.verifier) throw new Error('현재 비밀번호가 올바르지 않습니다.');
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const newKey = await deriveKeyBytes(next,salt);
    const setup = {...state.setup,salt:bytesToB64(salt),verifier:await makeVerifier(newKey),encryptedSecrets:await encryptJson(state.secrets,newKey)};
    await setMeta('setup',setup); state.setup = setup; state.keyBytes = newKey;
    $('#current-password').value = ''; $('#new-password').value = '';
    await addLog('보안','잠금 비밀번호 변경');
    toast('비밀번호를 변경했습니다.');
  } catch (error) { toast(error.message); }
  finally { setBusy(event.submitter,false); }
}

async function exportBackup() {
  const data = {format:'JungwonLawMonitorStandaloneBackup',version:1,exportedAt:new Date().toISOString(),managed:await dbGetAll('managed'),changes:await dbGetAll('changes'),notices:await dbGetAll('notices'),logs:(await dbGetAll('logs')).slice(-50),lastSync:await getMeta('lastSync','')};
  const text = bytesToB64(new TextEncoder().encode(JSON.stringify(data)));
  $('#backup-text').value = text;
  $('#backup-text').focus(); $('#backup-text').select();
  toast('백업 문자열을 생성했습니다. 전체 복사해 보관하세요.',3500);
}

async function importBackup() {
  const raw = cleanText($('#backup-text').value);
  if (!raw) return toast('복원할 백업 문자열을 붙여넣으세요.');
  if (!confirm('현재 휴대폰 DB를 백업 내용으로 교체하시겠습니까?')) return;
  try {
    const data = JSON.parse(new TextDecoder().decode(b64ToBytes(raw)));
    if (data.format !== 'JungwonLawMonitorStandaloneBackup' || data.version !== 1) throw new Error('지원하지 않는 백업 형식입니다.');
    for (const store of ['managed','changes','notices','logs']) await dbClear(store);
    for (const r of data.managed || []) await dbPut('managed',r);
    for (const r of data.changes || []) await dbPut('changes',r);
    for (const r of data.notices || []) await dbPut('notices',r);
    for (const r of data.logs || []) { const copy = {...r}; delete copy.id; await dbAdd('logs',copy); }
    await setMeta('lastSync',data.lastSync || '');
    await addLog('복원','휴대폰 DB 백업 복원 완료');
    $('#backup-text').value = '';
    await refreshAll();
    toast('백업 자료를 복원했습니다.');
  } catch (error) { toast(`복원 실패: ${error.message}`); }
}

function openUrl(url) {
  if (!/^https:\/\//i.test(url || '')) return toast('안전하지 않은 주소는 열 수 없습니다.');
  window.location.href = url;
}

async function markChangesSeen() {
  const rows = await dbGetAll('changes');
  for (const row of rows) if (row.isNew) await dbPut('changes',{...row,isNew:false});
  await loadChanges(); await refreshDashboard(); toast('신규 개정을 확인 처리했습니다.');
}

async function markNoticesSeen() {
  const rows = await dbGetAll('notices');
  for (const row of rows) if (row.isNew) await dbPut('notices',{...row,isNew:false});
  await loadNotices(); await refreshDashboard(); toast('입법예고를 확인 처리했습니다.');
}

function setupPwaInstall() {
  const button = $('#install-button');
  const card = $('#install-card');
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  if (standalone && card) card.classList.add('hidden');
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    state.deferredInstallPrompt = event;
    if (button) button.classList.remove('hidden');
  });
  window.addEventListener('appinstalled', () => {
    state.deferredInstallPrompt = null;
    if (button) button.classList.add('hidden');
    if (card) card.classList.add('hidden');
    toast('홈 화면에 설치되었습니다.');
  });
  if (button) button.addEventListener('click', async () => {
    if (!state.deferredInstallPrompt) {
      toast('Chrome 메뉴에서 “홈 화면에 추가”를 선택하세요.', 3500);
      return;
    }
    state.deferredInstallPrompt.prompt();
    await state.deferredInstallPrompt.userChoice;
    state.deferredInstallPrompt = null;
    button.classList.add('hidden');
  });
}

async function testOfficialApis() {
  const button = $('#api-test-button');
  const box = $('#api-test-result');
  if (!button || !box) return;
  setBusy(button, true);
  box.innerHTML = '<p class="muted">연결 확인 중…</p>';
  const lawOc = state.secrets?.lawOc;
  const noticeOc = state.secrets?.noticeOc || lawOc;
  const tests = [
    ['국가법령정보', lawOc ? apiUrl(LAW_URL,{OC:lawOc,target:'law',type:'JSON',query:'소방',display:1,page:1}) : ''],
    ['입법예고', noticeOc ? apiUrl(NOTICE_URL,{OC:noticeOc,diff:0,lsNm:'소방',pageIndex:1,pageUnit:1}) : ''],
  ];
  const results = [];
  for (const [name,url] of tests) {
    if (!url) { results.push({name,ok:false,message:'API OC 미입력'}); continue; }
    try {
      const text = await fetchText(url, 15000);
      const payload = parsePayload(text);
      results.push({name,ok:!!payload,message:'정상 연결'});
    } catch (error) {
      results.push({name,ok:false,message:error.message});
    }
  }
  box.innerHTML = results.map((r) => `<div class="api-test-row ${r.ok?'ok':'fail'}"><b>${esc(r.name)}</b><span>${esc(r.message)}</span></div>`).join('');
  setBusy(button, false);
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('./sw.js', {scope:'./'});
  } catch (error) {
    console.warn('service worker registration failed', error);
  }
}

function bindEvents() {
  $('#setup-form').addEventListener('submit',setupApp);
  $('#login-form').addEventListener('submit',login);
  $('#sync-button').addEventListener('click',syncNow);
  $$('.bottom-nav button').forEach((b) => b.addEventListener('click',() => go(b.dataset.page)));
  $$('[data-go]').forEach((b) => b.addEventListener('click',() => go(b.dataset.go)));
  $('#open-search').addEventListener('click',openCatalog);
  $('#catalog-search').addEventListener('click',(e) => { e.preventDefault(); runCatalogSearch(); });
  $('#catalog-q').addEventListener('keydown',(e) => { if (e.key === 'Enter') { e.preventDefault(); runCatalogSearch(); } });
  $('#catalog-results').addEventListener('click',(e) => {
    const resultButton = e.target.closest('[data-add-index]');
    if (resultButton) return addCatalog(Number(resultButton.dataset.addIndex));
    const manualButton = e.target.closest('[data-manual-add]');
    if (manualButton) return addManualCatalog();
  });
  $('#managed-filter').addEventListener('input',loadManaged);
  $('#managed-refresh').addEventListener('click',loadManaged);
  $('#managed-list').addEventListener('change',async (e) => { const id = Number(e.target.dataset.toggle); if (!id) return; const row = await dbGet('managed',id); await dbPut('managed',{...row,enabled:e.target.checked,updatedAt:new Date().toISOString()}); await refreshDashboard(); });
  $('#managed-list').addEventListener('click',async (e) => {
    const open = e.target.closest('[data-open]'); if (open) return openUrl(open.dataset.open);
    const del = e.target.closest('[data-delete]');
    if (del && confirm(`${del.dataset.name}을(를) 관리대상에서 삭제하시겠습니까? 저장된 개정 이력은 유지됩니다.`)) { await dbDelete('managed',Number(del.dataset.delete)); await loadManaged(); await refreshDashboard(); }
  });
  let changeTimer;
  ['changes-q','changes-from','changes-to','changes-new-only'].forEach((id) => $('#'+id).addEventListener(id === 'changes-q' ? 'input':'change',() => { clearTimeout(changeTimer); changeTimer=setTimeout(loadChanges,220); }));
  $('#changes-list').addEventListener('click',(e) => { const open=e.target.closest('[data-open]'); if (open) return openUrl(open.dataset.open); const b=e.target.closest('[data-appendix]'); if (b) openAppendices(b.dataset.appendix,b.dataset.title); });
  $('#appendix-list').addEventListener('click',(e) => { const open=e.target.closest('[data-open]'); if (open) openUrl(open.dataset.open); });
  $('#changes-seen').addEventListener('click',markChangesSeen);
  let noticeTimer;
  ['notices-q','notices-status','notices-from','notices-to'].forEach((id) => $('#'+id).addEventListener(id === 'notices-q' ? 'input':'change',() => { clearTimeout(noticeTimer); noticeTimer=setTimeout(loadNotices,220); }));
  $('#notices-list').addEventListener('click',(e) => { const open=e.target.closest('[data-open]'); if (open) openUrl(open.dataset.open); });
  $('#notices-seen').addEventListener('click',markNoticesSeen);
  $('#settings-form').addEventListener('submit',saveSettings);
  $('#password-form').addEventListener('submit',changePassword);
  $('#export-button').addEventListener('click',exportBackup);
  $('#import-button').addEventListener('click',importBackup);
  $('#api-test-button').addEventListener('click',testOfficialApis);
  $('#lock-button').addEventListener('click',showLogin);
  ['click','keydown','touchstart','scroll'].forEach((name) => document.addEventListener(name,() => { state.lastTouch=Date.now(); },{passive:true}));
  setInterval(() => { if (state.keyBytes && !state.syncing && Date.now()-state.lastTouch>AUTO_LOCK_MS) { showLogin(); toast('보안을 위해 자동 잠금되었습니다.'); } },30000);
  document.addEventListener('visibilitychange',() => { if (!document.hidden) { state.lastTouch=Date.now(); if (state.keyBytes) maybeAutoSync(); } });
}

async function boot() {
  try {
    if (!window.crypto?.subtle || !window.indexedDB) throw new Error('이 휴대폰의 Android System WebView가 너무 오래되었습니다. WebView를 업데이트하세요.');
    await registerServiceWorker();
    setupPwaInstall();
    state.db = await openDb();
    state.setup = await getMeta('setup',null);
    bindEvents();
    if (!state.setup) showSetup(); else showLogin();
  } catch (error) {
    document.body.innerHTML = `<div class="gate"><div class="gate-card"><h1>앱 시작 실패</h1><p>${esc(error.message)}</p></div></div>`;
  }
}

/* v1.3.0 mobile fixes: remembered login, notice API, admin-rule links, multi-select */
const V13_REMEMBER_KEY = 'JungwonLawMonitorRememberedKeyV1';
const V13_MANUAL_LOCK_KEY = 'JungwonLawMonitorManualLockV1';

function v13DateShiftMonths(base, months) {
  const d = new Date(base);
  const day = d.getDate();
  d.setDate(1);
  d.setMonth(d.getMonth() + months);
  const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  d.setDate(Math.min(day, last));
  return d;
}

function v13Ymd(date) {
  const d = new Date(date);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function v13NoticeApiDate(value) {
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}.+${d.getMonth() + 1}.+${d.getDate()}.`;
}

function v13NoticeRange() {
  const now = new Date();
  return {from:v13Ymd(v13DateShiftMonths(now,-6)), to:v13Ymd(v13DateShiftMonths(now,6)), today:v13Ymd(now)};
}

function v13EnsureNoticeRange() {
  const range = v13NoticeRange();
  if ($('#notices-from') && !$('#notices-from').value) $('#notices-from').value = range.from;
  if ($('#notices-to') && !$('#notices-to').value) $('#notices-to').value = range.to;
  const subtitle = $('#page-notices .page-title p');
  if (subtitle) subtitle.textContent = `오늘 기준 전후 6개월(${range.from} ~ ${range.to}) 입법예고를 비교합니다.`;
}

function v13AdministrativeRuleUrl(name) {
  const oc = state.secrets?.lawOc || '';
  return apiUrl('https://www.law.go.kr/DRF/lawService.do', {
    OC:oc,
    target:'admrul',
    type:'HTML',
    LM:name,
    mobileYn:'Y',
  });
}

function officialLawUrl(kind, name, supplied = '') {
  if (kind === '행정규칙') return v13AdministrativeRuleUrl(name);
  if (supplied) {
    const absolute = absoluteUrl('https://www.law.go.kr', supplied);
    if (absolute) return absolute.replace(/^http:/i,'https:');
  }
  return `https://www.law.go.kr/${encodeURIComponent('법령')}/${encodeURIComponent(name)}`;
}

function v13ItemOfficialUrl(item) {
  if (item.kind === '행정규칙') return v13AdministrativeRuleUrl(item.name);
  return item.officialUrl || officialLawUrl(item.kind || '법령', item.name || '');
}

function v13NoticeDetailUrl(record) {
  const seq = first(record,'ogLmPpSeq','입법예고일련번호','sequence','seq','id');
  const mapping = first(record,'mappingLbicId','법안매핑번호') || '0';
  const type = first(record,'announceType','공고종류') || 'TYPE1';
  const oc = state.secrets?.noticeOc || state.secrets?.lawOc || '';
  if (!seq) return 'https://opinion.lawmaking.go.kr/gcom/ogLmPp';
  return apiUrl(`https://www.lawmaking.go.kr/rest/ogLmPp/${encodeURIComponent(seq)}/${encodeURIComponent(mapping)}/${encodeURIComponent(type)}.html`, {OC:oc});
}

function v13PeriodSide(startDate, endDate, today) {
  if (startDate && startDate > today) return '향후 6개월';
  if (endDate && endDate < today) return '지난 6개월';
  return '진행 구간';
}

async function searchCatalog(query, kind = '전체') {
  const oc = state.secrets?.lawOc;
  if (!oc) throw new Error('설정에서 국가법령정보 API OC를 입력하세요.');
  const targets = [];
  if (kind === '전체' || kind === '법령') targets.push(['law','법령']);
  if (kind === '전체' || kind === '행정규칙') targets.push(['admrul','행정규칙']);
  const results = [];
  for (const [target, displayKind] of targets) {
    const text = await fetchText(apiUrl(LAW_URL, {OC:oc,target,type:'JSON',query,display:100,page:1,sort:'ddes'}));
    const payload = parsePayload(text);
    for (const record of candidateRecords(payload, ['법령명','행정규칙명','법령명한글','법령ID','행정규칙일련번호'])) {
      const name = recordName(record);
      if (!name) continue;
      const sourceId = displayKind === '행정규칙'
        ? first(record,'행정규칙일련번호','행정규칙ID','id')
        : first(record,'법령ID','법령일련번호','MST','id');
      const link = first(record,'법령상세링크','행정규칙상세링크','상세링크','link','url');
      results.push({
        kind:displayKind,
        name,
        sourceId,
        officialUrl:officialLawUrl(displayKind,name,link),
        revisionType:first(record,'제개정구분명','제개정구분','개정구분'),
        promulgationDate:normalizeDate(first(record,'공포일자','발령일자','개정일자')),
        enforcementDate:normalizeDate(first(record,'시행일자','효력일자')),
        ministry:first(record,'소관부처명','소관부처','부처명'),
        score:matchScore(query,name),
      });
    }
  }
  const unique = new Map();
  results.sort((a,b) => b.score - a.score || a.name.localeCompare(b.name,'ko'));
  results.forEach((r) => { const key = `${r.kind}|${r.name}`; if (!unique.has(key)) unique.set(key,r); });
  return [...unique.values()].slice(0,100);
}

async function fetchNotices(lawName) {
  const oc = state.secrets?.noticeOc || state.secrets?.lawOc;
  if (!oc) throw new Error('입법예고 API OC가 없습니다.');
  const range = v13NoticeRange();
  const output = [], seen = new Set();
  for (const diff of [0,1]) {
    for (let page = 1; page <= 30; page += 1) {
      const url = apiUrl(NOTICE_URL, {
        OC:oc,
        diff,
        lsNm:lawName,
        stYdFmt:v13NoticeApiDate(range.from),
        edYdFmt:v13NoticeApiDate(range.to),
        pageIndex:page,
        pageSize:100,
      });
      const payload = parsePayload(await fetchText(url));
      const records = candidateRecords(payload,['ogLmPpSeq','lsNm','stYd','edYd','pntcDt']);
      let added = 0;
      for (const record of records) {
        const title = first(record,'lsNm','법령안명','입법예고명','title','법령명');
        if (!title || matchScore(lawName,title) < 25) continue;
        const sequence = first(record,'ogLmPpSeq','입법예고ID','sequence','seq','id');
        const noticeNo = first(record,'pntcNo','공고번호','입법예고번호','noticeNo','notice_no');
        const announcedDate = normalizeDate(first(record,'pntcDt','공고일자'));
        const startDate = normalizeDate(first(record,'stYd','시작일자','입법예고시작일자','startDate','start_date')) || announcedDate;
        const endDate = normalizeDate(first(record,'edYd','종료일자','입법예고종료일자','endDate','end_date'));
        if (startDate && startDate > range.to) continue;
        if (endDate && endDate < range.from) continue;
        const noticeKey = `notice:${sequence || noticeNo || title}:${startDate || '날짜미상'}`;
        if (seen.has(noticeKey)) continue;
        seen.add(noticeKey); added += 1;
        const status = endDate && endDate < range.today ? '종료' : '진행 중';
        output.push({
          noticeKey,
          title,
          ministry:first(record,'asndOfiNm','cptOfiOrgNm','소관부처명','소관부처','부처명','ministry'),
          noticeNo,
          announcedDate,
          startDate,
          endDate,
          status,
          periodSide:v13PeriodSide(startDate,endDate,range.today),
          officialUrl:v13NoticeDetailUrl(record),
          fileName:first(record,'FileName','fileName'),
          fileUrl:absoluteUrl('https://www.lawmaking.go.kr',first(record,'FileDownLink','fileDownLink')),
          matchedItem:lawName,
        });
      }
      if ((page > 1 && added === 0) || records.length < 100) break;
    }
  }
  const unique = new Map();
  output.forEach((row) => { if (!unique.has(row.noticeKey)) unique.set(row.noticeKey,row); });
  return [...unique.values()];
}

function v13RememberKey(keyBytes) {
  try { localStorage.setItem(V13_REMEMBER_KEY, bytesToB64(keyBytes)); } catch {}
}

function v13ForgetKey() {
  try { localStorage.removeItem(V13_REMEMBER_KEY); } catch {}
}

async function v13TryRememberedLogin() {
  if (!state.setup?.keepLogin || sessionStorage.getItem(V13_MANUAL_LOCK_KEY) === '1') return false;
  let encoded = '';
  try { encoded = localStorage.getItem(V13_REMEMBER_KEY) || ''; } catch { return false; }
  if (!encoded) return false;
  try {
    const keyBytes = b64ToBytes(encoded);
    if (await makeVerifier(keyBytes) !== state.setup.verifier) throw new Error('저장된 로그인 정보 불일치');
    const secrets = await decryptJson(state.setup.encryptedSecrets,keyBytes);
    state.keyBytes = keyBytes;
    state.secrets = secrets;
    state.lastTouch = Date.now();
    return true;
  } catch {
    v13ForgetKey();
    return false;
  }
}

async function setupApp(event) {
  event.preventDefault();
  const password = $('#setup-password').value;
  if (password !== $('#setup-password2').value) return toast('비밀번호 확인이 일치하지 않습니다.');
  if (password.length < 8) return toast('비밀번호는 8자 이상으로 설정하세요.');
  const lawOc = normalizeOc($('#setup-law-oc').value);
  if (!lawOc) return toast('국가법령정보 API OC를 입력하세요.');
  setBusy(event.submitter,true);
  try {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const keyBytes = await deriveKeyBytes(password,salt);
    const keepLogin = !!$('#setup-keep-login')?.checked;
    const setup = {
      ownerName:cleanText($('#setup-owner').value) || '이상우',
      salt:bytesToB64(salt),
      verifier:await makeVerifier(keyBytes),
      encryptedSecrets:await encryptJson({lawOc,noticeOc:normalizeOc($('#setup-notice-oc').value)},keyBytes),
      autoSync:true,
      syncIntervalHours:12,
      keepLogin,
      createdAt:new Date().toISOString(),
    };
    await setMeta('setup',setup);
    state.setup = setup;
    state.keyBytes = keyBytes;
    state.secrets = await decryptJson(setup.encryptedSecrets,keyBytes);
    if (keepLogin) v13RememberKey(keyBytes); else v13ForgetKey();
    sessionStorage.removeItem(V13_MANUAL_LOCK_KEY);
    await seedManaged();
    await addLog('설정','휴대폰 독립형 초기 설정 완료');
    showApp();
    await refreshAll();
    toast('초기 설정이 완료되었습니다.');
  } catch (error) { toast(error.message || '초기 설정에 실패했습니다.'); }
  finally { setBusy(event.submitter,false); }
}

async function login(event) {
  event.preventDefault();
  setBusy(event.submitter,true);
  try {
    const keyBytes = await deriveKeyBytes($('#login-password').value,b64ToBytes(state.setup.salt));
    if (await makeVerifier(keyBytes) !== state.setup.verifier) throw new Error('비밀번호가 올바르지 않습니다.');
    const secrets = await decryptJson(state.setup.encryptedSecrets,keyBytes);
    const keepLogin = !!$('#login-keep')?.checked;
    state.setup = {...state.setup,keepLogin};
    await setMeta('setup',state.setup);
    state.keyBytes = keyBytes;
    state.secrets = secrets;
    state.lastTouch = Date.now();
    if (keepLogin) v13RememberKey(keyBytes); else v13ForgetKey();
    sessionStorage.removeItem(V13_MANUAL_LOCK_KEY);
    $('#login-password').value = '';
    showApp();
    await refreshAll();
    maybeAutoSync();
  } catch (error) { toast(error.message || '잠금을 해제하지 못했습니다.'); }
  finally { setBusy(event.submitter,false); }
}

function showLogin() {
  state.secrets = null;
  state.keyBytes = null;
  sessionStorage.setItem(V13_MANUAL_LOCK_KEY,'1');
  $('#setup-view').classList.add('hidden');
  $('#login-view').classList.remove('hidden');
  $('#app-view').classList.add('hidden');
  if ($('#login-keep')) $('#login-keep').checked = !!state.setup?.keepLogin;
  setTimeout(() => $('#login-password').focus(),100);
}

async function loadSettings() {
  $('#setting-owner').value = state.setup.ownerName || '이상우';
  $('#setting-law-oc').value = state.secrets?.lawOc || '';
  $('#setting-notice-oc').value = state.secrets?.noticeOc || '';
  $('#setting-auto-sync').checked = !!state.setup.autoSync;
  $('#setting-sync-interval').value = String(state.setup.syncIntervalHours || 12);
  if ($('#setting-keep-login')) $('#setting-keep-login').checked = !!state.setup.keepLogin;
  const logs = (await dbGetAll('logs')).sort((a,b) => b.id - a.id).slice(0,30);
  $('#logs-list').innerHTML = logs.length ? logs.map((x) => `<div><b>${esc(x.status)}</b><span>${esc(formatDateTime(x.at))} · ${esc(x.message)}</span></div>`).join('') : '<div>로그가 없습니다.</div>';
}

async function saveSettings(event) {
  event.preventDefault();
  setBusy(event.submitter,true);
  try {
    const ownerName = cleanText($('#setting-owner').value) || '이상우';
    const secrets = {lawOc:normalizeOc($('#setting-law-oc').value),noticeOc:normalizeOc($('#setting-notice-oc').value)};
    if (!secrets.lawOc) throw new Error('국가법령정보 API OC를 입력하세요.');
    const keepLogin = !!$('#setting-keep-login')?.checked;
    const setup = {
      ...state.setup,
      ownerName,
      autoSync:$('#setting-auto-sync').checked,
      syncIntervalHours:Number($('#setting-sync-interval').value),
      keepLogin,
      encryptedSecrets:await encryptJson(secrets,state.keyBytes),
    };
    await setMeta('setup',setup);
    state.setup = setup;
    state.secrets = secrets;
    if (keepLogin) v13RememberKey(state.keyBytes); else v13ForgetKey();
    $('#owner-name').textContent = ownerName;
    await addLog('설정',`API·자동 동기화·로그인 유지 설정 변경(로그인 유지 ${keepLogin ? '사용':'해제'})`);
    toast('설정을 저장했습니다.');
  } catch (error) { toast(error.message); }
  finally { setBusy(event.submitter,false); }
}

async function changePassword(event) {
  event.preventDefault();
  const current = $('#current-password').value, next = $('#new-password').value;
  if (next.length < 8) return toast('새 비밀번호는 8자 이상이어야 합니다.');
  setBusy(event.submitter,true);
  try {
    const oldKey = await deriveKeyBytes(current,b64ToBytes(state.setup.salt));
    if (await makeVerifier(oldKey) !== state.setup.verifier) throw new Error('현재 비밀번호가 올바르지 않습니다.');
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const newKey = await deriveKeyBytes(next,salt);
    const setup = {...state.setup,salt:bytesToB64(salt),verifier:await makeVerifier(newKey),encryptedSecrets:await encryptJson(state.secrets,newKey)};
    await setMeta('setup',setup);
    state.setup = setup;
    state.keyBytes = newKey;
    if (setup.keepLogin) v13RememberKey(newKey); else v13ForgetKey();
    $('#current-password').value = '';
    $('#new-password').value = '';
    await addLog('보안','잠금 비밀번호 변경');
    toast('비밀번호를 변경했습니다.');
  } catch (error) { toast(error.message); }
  finally { setBusy(event.submitter,false); }
}

function v13SelectionBox(group, key) {
  return `<label class="v13-select-box" aria-label="항목 선택"><input type="checkbox" data-select-group="${group}" data-key="${esc(key)}"><span></span></label>`;
}

function v13SelectionBar(group, actions) {
  return `<div class="v13-selection-bar" id="${group}-selection-bar"><label class="check"><input type="checkbox" id="${group}-select-all"> 전체 선택</label><b id="${group}-selected-count">0개 선택</b><div class="v13-bulk-actions">${actions}</div></div>`;
}

function v13InstallUi() {
  if ($('#v13-style')) return;
  const style = document.createElement('style');
  style.id = 'v13-style';
  style.textContent = `
    .v13-selection-bar{display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:14px;padding:10px 12px;margin:0 0 12px}.v13-selection-bar>b{text-align:right;font-size:12px;color:var(--muted)}.v13-bulk-actions{grid-column:1/-1;display:flex;gap:7px;flex-wrap:wrap}.v13-bulk-actions button{flex:1;min-width:80px;padding:9px;border-radius:10px;background:#eef5fa;color:var(--blue);font-weight:700;font-size:12px}.v13-bulk-actions .danger{background:#fff1f0;color:var(--red);border:1px solid #ffd2cf}.v13-select-box{display:flex;align-items:center;justify-content:center;flex:0 0 30px;width:30px;height:30px}.v13-select-box input{width:20px;height:20px;accent-color:var(--blue)}.item-head.v13-with-select{align-items:flex-start}.v13-remember-note{font-size:11px;color:var(--muted);line-height:1.45;margin-top:-4px}.v13-period{background:#f2f4f7;color:#475467}.v13-period.future{background:#e8f5ff;color:#075a9c}.v13-period.past{background:#fff4ed;color:#b54708}`;
  document.head.appendChild(style);

  const setupButton = $('#setup-form button[type="submit"]');
  if (setupButton && !$('#setup-keep-login')) setupButton.insertAdjacentHTML('beforebegin','<label class="check"><input id="setup-keep-login" type="checkbox" checked> 이 휴대폰에서 로그인 유지</label><div class="v13-remember-note">사용 시 앱을 다시 열 때 비밀번호 입력을 생략합니다.</div>');
  const loginButton = $('#login-form button[type="submit"]');
  if (loginButton && !$('#login-keep')) loginButton.insertAdjacentHTML('beforebegin','<label class="check"><input id="login-keep" type="checkbox"> 이 휴대폰에서 로그인 유지</label>');
  const autoSyncLabel = $('#setting-auto-sync')?.closest('label');
  if (autoSyncLabel && !$('#setting-keep-login')) autoSyncLabel.insertAdjacentHTML('afterend','<label class="check"><input id="setting-keep-login" type="checkbox"> 이 휴대폰에서 로그인 유지</label><div class="v13-remember-note">휴대폰을 분실하면 앱을 바로 열 수 있으므로 개인 휴대폰에서만 사용하세요.</div>');

  const managedList = $('#managed-list');
  if (managedList && !$('#managed-selection-bar')) managedList.insertAdjacentHTML('beforebegin',v13SelectionBar('managed','<button id="managed-enable-selected">선택 활성</button><button id="managed-disable-selected">선택 중지</button><button id="managed-delete-selected" class="danger">선택 삭제</button>'));
  const changesList = $('#changes-list');
  if (changesList && !$('#changes-selection-bar')) changesList.insertAdjacentHTML('beforebegin',v13SelectionBar('changes','<button id="changes-seen-selected">선택 확인 처리</button>'));
  const noticesList = $('#notices-list');
  if (noticesList && !$('#notices-selection-bar')) noticesList.insertAdjacentHTML('beforebegin',v13SelectionBar('notices','<button id="notices-seen-selected">선택 확인 처리</button>'));
  const catalogList = $('#catalog-results');
  if (catalogList && !$('#catalog-selection-bar')) catalogList.insertAdjacentHTML('beforebegin',v13SelectionBar('catalog','<button id="catalog-add-selected">선택 항목 추가</button>'));
  v13EnsureNoticeRange();
}

function v13Selected(group) {
  return [...document.querySelectorAll(`input[data-select-group="${group}"]:checked`)].map((el) => el.dataset.key);
}

function v13RefreshSelection(group) {
  const all = [...document.querySelectorAll(`input[data-select-group="${group}"]`)];
  const selected = all.filter((el) => el.checked);
  const count = $(`#${group}-selected-count`);
  if (count) count.textContent = `${selected.length}개 선택`;
  const selectAll = $(`#${group}-select-all`);
  if (selectAll) {
    selectAll.checked = all.length > 0 && selected.length === all.length;
    selectAll.indeterminate = selected.length > 0 && selected.length < all.length;
  }
}

function v13SetAll(group, checked) {
  document.querySelectorAll(`input[data-select-group="${group}"]`).forEach((el) => { el.checked = checked; });
  v13RefreshSelection(group);
}

async function loadManaged() {
  const q = normalizeName($('#managed-filter').value);
  const rows = (await managedRows()).filter((r) => !q || normalizeName(`${r.kind} ${r.name}`).includes(q));
  $('#managed-list').innerHTML = rows.length ? rows.map((r) => `
    <article class="item">
      <div class="item-head v13-with-select">${v13SelectionBox('managed',r.id)}<div><h3>${esc(r.name)}</h3><p>${esc(r.kind)} · ${esc(r.checkStatus || '미확인')}</p><p>최근 개정 ${esc(r.lastRevisionDate || '-')} · 시행 ${esc(r.lastEnforcementDate || '-')}</p></div>
      <label class="switch"><input type="checkbox" data-toggle="${r.id}" ${r.enabled ? 'checked':''}><span></span></label></div>
      <div class="item-actions"><button data-open="${esc(v13ItemOfficialUrl(r))}">${r.kind === '행정규칙' ? '행정규칙 원문':'공식 원문'}</button><button class="delete" data-delete="${r.id}" data-name="${esc(r.name)}">삭제</button></div>
    </article>`).join('') : '<div class="empty">관리대상이 없습니다.</div>';
  v13RefreshSelection('managed');
}

async function loadChanges() {
  const q = normalizeName($('#changes-q').value);
  const from = $('#changes-from').value, to = $('#changes-to').value;
  const newOnly = $('#changes-new-only').checked;
  const rows = (await dbGetAll('changes')).filter((r) => {
    const date = r.promulgationDate || r.enforcementDate || '';
    return (!q || normalizeName(`${r.name} ${r.revisionType} ${r.ministry}`).includes(q)) && (!from || date >= from) && (!to || date <= to) && (!newOnly || r.isNew);
  }).sort((a,b) => (b.promulgationDate || '').localeCompare(a.promulgationDate || '') || (b.detectedAt || '').localeCompare(a.detectedAt || ''));
  $('#changes-list').innerHTML = rows.length ? rows.slice(0,1500).map((r) => {
    const count = Array.isArray(r.appendices) ? r.appendices.length : 0;
    return `<article class="item ${r.isNew ? 'new':''}"><div class="item-head v13-with-select">${v13SelectionBox('changes',r.eventKey)}<div><h3>${esc(r.name)}</h3><p>${esc(r.revisionType || '구분미상')} · 공포 ${esc(r.promulgationDate || '-')} · 시행 ${esc(r.enforcementDate || '-')}</p><p>${esc(r.ministry || '')}</p></div>${r.isNew ? '<span class="tag red">NEW</span>':''}</div><div class="tags">${count ? `<span class="tag red">별표·서식 ${count}건</span>`:''}<span class="tag">${esc(r.kind)}</span></div><div class="item-actions"><button data-open="${esc(v13ItemOfficialUrl(r))}">${r.kind === '행정규칙' ? '행정규칙 원문':'법령 원문'}</button>${count ? `<button data-appendix="${esc(r.eventKey)}" data-title="${esc(`${r.name} ${r.promulgationDate}`)}">별표·서식</button>`:''}</div></article>`;
  }).join('') : '<div class="empty">조건에 맞는 개정사항이 없습니다.</div>';
  v13RefreshSelection('changes');
}

async function loadNotices() {
  v13EnsureNoticeRange();
  const q = normalizeName($('#notices-q').value);
  const status = $('#notices-status').value;
  const from = $('#notices-from').value, to = $('#notices-to').value;
  const range = v13NoticeRange();
  const rows = (await dbGetAll('notices')).filter((r) => {
    const start = r.startDate || r.announcedDate || '';
    const end = r.endDate || start;
    return (!q || normalizeName(`${r.title} ${r.matchedItem} ${r.ministry}`).includes(q)) && (status === '전체' || r.status === status) && (!from || end >= from) && (!to || start <= to);
  }).sort((a,b) => (b.startDate || b.announcedDate || '').localeCompare(a.startDate || a.announcedDate || ''));
  $('#notices-list').innerHTML = rows.length ? rows.slice(0,1500).map((r) => {
    const period = r.periodSide || v13PeriodSide(r.startDate,r.endDate,range.today);
    const periodClass = period.startsWith('향후') ? 'future' : period.startsWith('지난') ? 'past' : '';
    return `<article class="item ${r.isNew ? 'new':''}"><div class="item-head v13-with-select">${v13SelectionBox('notices',r.noticeKey)}<div><h3>${esc(r.title)}</h3><p>${esc(r.ministry || '')} · ${esc(r.noticeNo || '')}</p><p>${esc(r.startDate || r.announcedDate || '-')} ~ ${esc(r.endDate || '-')} · ${esc(r.matchedItem || '')}</p></div><span class="tag ${r.status === '진행 중' ? 'green':''}">${esc(r.status)}</span></div><div class="tags"><span class="tag v13-period ${periodClass}">${esc(period)}</span></div><div class="item-actions">${r.officialUrl ? `<button data-open="${esc(r.officialUrl)}">입법예고 상세</button>`:''}${r.fileUrl ? `<button data-open="${esc(r.fileUrl)}">첨부파일</button>`:''}</div></article>`;
  }).join('') : `<div class="empty">오늘 기준 전후 6개월(${esc(range.from)} ~ ${esc(range.to)})에 해당하는 입법예고가 없습니다.</div>`;
  v13RefreshSelection('notices');
}

async function runCatalogSearch() {
  const q = cleanText($('#catalog-q').value);
  if (q.length < 2) return toast('두 글자 이상 입력하세요.');
  const btn = $('#catalog-search');
  setBusy(btn,true);
  $('#catalog-results').innerHTML = '<div class="empty">공식 API 검색 중…</div>';
  try {
    const rows = await searchCatalog(q,$('#catalog-kind').value);
    state.catalogRows = rows;
    const searchResults = rows.map((r,i) => `
      <article class="item"><div class="item-head v13-with-select">${v13SelectionBox('catalog',i)}<div><h3>${esc(r.name)}</h3><p>${esc(r.kind)} · ${esc(r.ministry || '')}</p><p>${esc(r.promulgationDate || '')} ${esc(r.revisionType || '')}</p></div><span class="tag">일치 ${r.score}</span></div><div class="item-actions"><button class="primary" data-add-index="${i}">관리대상 추가</button></div></article>`).join('');
    $('#catalog-results').innerHTML = `${searchResults}${manualAddCard(q, rows.length ? '찾는 항목이 검색 결과와 다를 때 사용하세요.' : '검색 결과가 없어도 직접 등록할 수 있습니다.')}`;
  } catch (error) {
    state.catalogRows = [];
    $('#catalog-results').innerHTML = manualAddCard(q, `공식 API 검색 실패: ${error.message}`);
  } finally {
    setBusy(btn,false);
    v13RefreshSelection('catalog');
  }
}

async function addCatalog(index) {
  const row = state.catalogRows[index];
  if (!row) return toast('추가할 항목을 다시 선택하세요.');
  await addManaged({...row,officialUrl:v13ItemOfficialUrl(row)});
  $('#search-dialog').close();
  toast(`${row.name} 추가 완료`);
  await loadManaged();
  await refreshDashboard();
}

async function addManualCatalog() {
  const name = cleanText($('#catalog-q').value);
  if (name.length < 2) return toast('법규명을 두 글자 이상 입력하세요.');
  const selected = $('#catalog-kind').value;
  const kind = selected === '행정규칙' ? '행정규칙' : '법령';
  const row = {kind,name};
  await addManaged({...row,officialUrl:v13ItemOfficialUrl(row)});
  $('#search-dialog').close();
  toast(`${name} 직접 추가 완료`);
  await loadManaged();
  await refreshDashboard();
}

function v13ValidComparisonUrl(url) {
  if (!url || !/lsBylDiffHwpP\.do/i.test(url)) return false;
  try {
    const parsed = new URL(url);
    return ['lsiSeq','lsId','bylSeq','bylNo','bylEfYd'].every((key) => parsed.searchParams.get(key));
  } catch { return false; }
}

async function openAppendices(eventKey,title) {
  const row = await dbGet('changes',eventKey);
  const items = row?.appendices || [];
  $('#appendix-dialog-subtitle').textContent = title;
  $('#appendix-list').innerHTML = items.length ? items.map((r) => {
    const compare = v13ValidComparisonUrl(r.comparisonUrl) ? r.comparisonUrl : '';
    return `<article class="item"><h3>${esc(r.title)}</h3><p>${esc(r.appendixKind)} · ${esc(r.revisionDate || '-')} · ${esc(r.revisionType || '')}</p><div class="item-actions">${r.officialUrl ? `<button data-open="${esc(r.officialUrl)}">별표·서식 원문</button>`:''}${compare ? `<button data-open="${esc(compare)}">법제처 전후 비교</button>`:''}</div>${!r.officialUrl && !compare ? '<p>공식 연결정보가 없어 법령 원문에서 확인해야 합니다.</p>':''}</article>`;
  }).join('') : '<div class="empty">연결된 별표·서식이 없습니다.</div>';
  $('#appendix-dialog').showModal();
}

function openUrl(url) {
  if (!/^https:\/\//i.test(url || '')) return toast('안전하지 않은 주소는 열 수 없습니다.');
  const opened = window.open(url,'_blank','noopener,noreferrer');
  if (!opened) window.location.href = url;
}

async function testOfficialApis() {
  const button = $('#api-test-button');
  const box = $('#api-test-result');
  if (!button || !box) return;
  setBusy(button,true);
  box.innerHTML = '<p class="muted">연결 확인 중…</p>';
  const lawOc = state.secrets?.lawOc;
  const noticeOc = state.secrets?.noticeOc || lawOc;
  const range = v13NoticeRange();
  const results = [];
  try {
    if (!lawOc) throw new Error('API OC 미입력');
    const text = await fetchText(apiUrl(LAW_URL,{OC:lawOc,target:'law',type:'JSON',query:'소방',display:3,page:1}),15000);
    const payload = parsePayload(text);
    const count = candidateRecords(payload,['법령명','법령명한글','법령ID']).length;
    results.push({name:'국가법령정보',ok:true,message:`정상 연결 · 확인 가능한 응답 ${count}건`});
  } catch (error) { results.push({name:'국가법령정보',ok:false,message:error.message}); }
  try {
    if (!noticeOc) throw new Error('API OC 미입력');
    const text = await fetchText(apiUrl(NOTICE_URL,{OC:noticeOc,diff:0,stYdFmt:v13NoticeApiDate(range.from),edYdFmt:v13NoticeApiDate(range.to),pageIndex:1,pageSize:5}),15000);
    const payload = parsePayload(text);
    const count = candidateRecords(payload,['ogLmPpSeq','lsNm','stYd','edYd','pntcDt']).length;
    results.push({name:'입법예고',ok:true,message:`정상 연결 · 전후 6개월 응답 ${count}건`});
  } catch (error) { results.push({name:'입법예고',ok:false,message:error.message}); }
  box.innerHTML = results.map((r) => `<div class="api-test-row ${r.ok?'ok':'fail'}"><b>${esc(r.name)}</b><span>${esc(r.message)}</span></div>`).join('');
  setBusy(button,false);
}

async function v13BulkManaged(enabled) {
  const ids = v13Selected('managed').map(Number).filter(Boolean);
  if (!ids.length) return toast('관리대상을 선택하세요.');
  for (const id of ids) {
    const row = await dbGet('managed',id);
    if (row) await dbPut('managed',{...row,enabled,updatedAt:new Date().toISOString()});
  }
  await loadManaged();
  await refreshDashboard();
  toast(`${ids.length}개 항목을 ${enabled ? '활성':'중지'}했습니다.`);
}

async function v13DeleteManaged() {
  const ids = v13Selected('managed').map(Number).filter(Boolean);
  if (!ids.length) return toast('삭제할 관리대상을 선택하세요.');
  if (!confirm(`선택한 ${ids.length}개 관리대상을 삭제하시겠습니까? 저장된 개정 이력은 유지됩니다.`)) return;
  for (const id of ids) await dbDelete('managed',id);
  await loadManaged();
  await refreshDashboard();
  toast(`${ids.length}개 관리대상을 삭제했습니다.`);
}

async function v13MarkSelected(store, group, keyName, loader) {
  const keys = v13Selected(group);
  if (!keys.length) return toast('항목을 선택하세요.');
  for (const key of keys) {
    const row = await dbGet(store,key);
    if (row) await dbPut(store,{...row,isNew:false});
  }
  await loader();
  await refreshDashboard();
  toast(`${keys.length}개 항목을 확인 처리했습니다.`);
}

async function v13AddSelectedCatalog() {
  const indexes = v13Selected('catalog').map(Number).filter((v) => Number.isInteger(v));
  if (!indexes.length) return toast('추가할 검색 결과를 선택하세요.');
  let added = 0;
  for (const index of indexes) {
    const row = state.catalogRows[index];
    if (!row) continue;
    await addManaged({...row,officialUrl:v13ItemOfficialUrl(row)});
    added += 1;
  }
  $('#search-dialog').close();
  await loadManaged();
  await refreshDashboard();
  toast(`${added}개 관리대상을 추가했습니다.`);
}

function v13BindEvents() {
  document.addEventListener('change',(event) => {
    const target = event.target;
    if (target.matches('input[data-select-group]')) v13RefreshSelection(target.dataset.selectGroup);
    for (const group of ['managed','changes','notices','catalog']) {
      if (target.id === `${group}-select-all`) v13SetAll(group,target.checked);
    }
  });
  $('#managed-enable-selected')?.addEventListener('click',() => v13BulkManaged(true));
  $('#managed-disable-selected')?.addEventListener('click',() => v13BulkManaged(false));
  $('#managed-delete-selected')?.addEventListener('click',v13DeleteManaged);
  $('#changes-seen-selected')?.addEventListener('click',() => v13MarkSelected('changes','changes','eventKey',loadChanges));
  $('#notices-seen-selected')?.addEventListener('click',() => v13MarkSelected('notices','notices','noticeKey',loadNotices));
  $('#catalog-add-selected')?.addEventListener('click',v13AddSelectedCatalog);
}

async function boot() {
  try {
    if (!window.crypto?.subtle || !window.indexedDB) throw new Error('이 휴대폰의 Android System WebView가 너무 오래되었습니다. WebView를 업데이트하세요.');
    await registerServiceWorker();
    setupPwaInstall();
    v13InstallUi();
    state.db = await openDb();
    state.setup = await getMeta('setup',null);
    bindEvents();
    v13BindEvents();
    v13EnsureNoticeRange();
    if (!state.setup) {
      showSetup();
    } else if (await v13TryRememberedLogin()) {
      showApp();
      await refreshAll();
      maybeAutoSync();
    } else {
      showLogin();
    }
  } catch (error) {
    document.body.innerHTML = `<div class="gate"><div class="gate-card"><h1>앱 시작 실패</h1><p>${esc(error.message)}</p></div></div>`;
  }
}


/* v1.4.0: official mobile full-text links and mobile appendix before/after viewing */

function v14Digits(value) {
  return cleanText(value).replace(/\D/g, '');
}

function v14AppendixTypeCode(value) {
  const text = cleanText(value);
  if (text.includes('서식')) return 2;
  if (text.includes('별지')) return 3;
  if (text.includes('별도')) return 4;
  if (text.includes('부록')) return 5;
  return 1;
}

function v14MobileLawUrl(item, options = {}) {
  const kind = item?.kind === '행정규칙' ? '행정규칙' : '법령';
  const oc = state.secrets?.lawOc || '';
  const name = cleanText(item?.name || '');
  const id = cleanText(item?.adminId || item?.lawId || item?.mobileId || item?.sourceId || '');
  const mst = cleanText(item?.lawMst || item?.mobileMst || item?.mst || '');

  if (kind === '행정규칙' && options.appendix) {
    return apiUrl('https://www.law.go.kr/DRF/lawSearch.do', {
      OC:oc,
      target:'admbyl',
      type:'HTML',
      mobileYn:'Y',
      search:2,
      query:name,
      display:100,
      page:1,
    });
  }

  const params = {
    OC:oc,
    target:kind === '행정규칙' ? 'admrul' : 'law',
    type:'HTML',
    mobileYn:'Y',
  };

  if (kind === '법령' && mst) params.MST = mst;
  else if (id) params.ID = id;
  else if (name) params.LM = name;

  const date = v14Digits(options.date || '');
  if (kind === '법령' && date.length >= 8) params.LD = date.slice(0,8);

  if (kind === '법령' && (options.allAppendices || options.appendix)) {
    params.BD = 'ON';
    if (options.appendix) {
      const appendix = options.appendix;
      params.BT = v14AppendixTypeCode(appendix.appendixKind || appendix.title);
      const number = v14Digits(appendix.appendixNo || '');
      const branch = v14Digits(appendix.appendixBranch || '');
      if (number) params.BN = Number(number);
      if (branch) params.BG = Number(branch);
    }
  }

  return apiUrl('https://www.law.go.kr/DRF/lawService.do', params);
}

function officialLawUrl(kind, name, supplied = '') {
  return v14MobileLawUrl({kind,name,officialUrl:supplied});
}

function v13AdministrativeRuleUrl(name) {
  return v14MobileLawUrl({kind:'행정규칙',name});
}

function v13ItemOfficialUrl(item) {
  return v14MobileLawUrl(item || {});
}

function openUrl(url) {
  if (!/^https:\/\//i.test(url || '')) return toast('안전하지 않은 주소는 열 수 없습니다.');
  const popup = window.open(url, '_blank', 'noopener,noreferrer');
  if (!popup) window.location.href = url;
}

async function searchCatalog(query, kind = '전체') {
  const oc = state.secrets?.lawOc;
  if (!oc) throw new Error('설정에서 국가법령정보 API OC를 입력하세요.');
  const targets = [];
  if (kind === '전체' || kind === '법령') targets.push(['law','법령']);
  if (kind === '전체' || kind === '행정규칙') targets.push(['admrul','행정규칙']);
  const results = [];

  for (const [target, displayKind] of targets) {
    const text = await fetchText(apiUrl(LAW_URL, {
      OC:oc,target,type:'JSON',mobileYn:'Y',query,display:100,page:1,sort:'ddes'
    }));
    const payload = parsePayload(text);
    for (const record of candidateRecords(payload, ['법령명','행정규칙명','법령명한글','법령ID','행정규칙일련번호'])) {
      const name = recordName(record);
      if (!name) continue;
      const lawId = first(record,'법령ID','법령아이디');
      const lawMst = first(record,'법령일련번호','MST');
      const adminId = first(record,'행정규칙일련번호','행정규칙ID');
      const row = {
        kind:displayKind,
        name,
        lawId,
        lawMst,
        adminId,
        sourceId:displayKind === '행정규칙' ? (adminId || lawId) : (lawId || lawMst),
        revisionType:first(record,'제개정구분명','제개정구분','개정구분'),
        promulgationDate:normalizeDate(first(record,'공포일자','발령일자','개정일자')),
        enforcementDate:normalizeDate(first(record,'시행일자','효력일자')),
        ministry:first(record,'소관부처명','소관부처','부처명'),
        score:matchScore(query,name),
      };
      row.officialUrl = v14MobileLawUrl(row);
      results.push(row);
    }
  }

  const unique = new Map();
  results.sort((a,b) => b.score - a.score || a.name.localeCompare(b.name,'ko'));
  results.forEach((row) => {
    const key = `${row.kind}|${row.name}`;
    if (!unique.has(key)) unique.set(key,row);
  });
  return [...unique.values()].slice(0,100);
}

async function fetchRevisions(item) {
  const oc = state.secrets?.lawOc;
  if (!oc) throw new Error('국가법령정보 API OC가 없습니다.');
  const target = item.kind === '행정규칙' ? 'admrul' : 'eflaw';
  const rows = [];
  const seen = new Set();

  for (let page = 1; page <= 50; page += 1) {
    const params = {OC:oc,target,type:'JSON',query:item.name,display:100,page,sort:'ddes'};
    if (item.kind !== '행정규칙') { params.search = 1; params.nw = '1,2,3'; }
    const payload = parsePayload(await fetchText(apiUrl(LAW_URL, params)));
    const records = candidateRecords(payload, ['법령명','행정규칙명','법령ID','법령일련번호','공포일자','발령일자']);
    let added = 0;

    for (const record of records) {
      const candidate = recordName(record);
      if (matchScore(item.name,candidate) < 78) continue;
      const lawId = first(record,'법령ID','법령아이디') || item.lawId || '';
      const lawMst = first(record,'법령일련번호','MST') || item.lawMst || '';
      const adminId = first(record,'행정규칙일련번호','행정규칙ID') || item.adminId || '';
      const sourceId = item.kind === '행정규칙' ? (adminId || item.sourceId || '') : (lawId || lawMst || item.sourceId || '');
      const promulgationDate = normalizeDate(first(record,'공포일자','발령일자','개정일자','promulgationDate'));
      const enforcementDate = normalizeDate(first(record,'시행일자','효력일자','enforcementDate'));
      const revisionType = first(record,'제개정구분명','제개정구분','개정구분','revisionType') || '구분미상';
      const name = candidate || item.name;
      const eventKey = [item.kind,lawMst || sourceId || name,promulgationDate || '날짜미상',revisionType].join(':');
      if (seen.has(eventKey)) continue;
      seen.add(eventKey); added += 1;
      const row = {
        eventKey,managedId:item.id,kind:item.kind,name,sourceId,lawId,lawMst,adminId,
        revisionType,promulgationDate,enforcementDate,
        ministry:first(record,'소관부처명','소관부처','부처명','ministry'),appendices:[]
      };
      row.officialUrl = v14MobileLawUrl(row,{date:promulgationDate});
      rows.push(row);
    }
    if ((page > 1 && added === 0) || records.length < 100) break;
  }

  rows.sort((a,b) => (b.promulgationDate || '').localeCompare(a.promulgationDate || '') || (b.enforcementDate || '').localeCompare(a.enforcementDate || ''));
  return rows;
}

async function fetchAppendices(item) {
  const oc = state.secrets?.lawOc;
  if (!oc) return [];
  const target = item.kind === '행정규칙' ? 'admbyl' : 'licbyl';
  const output = [];
  const seen = new Set();

  for (let page = 1; page <= 30; page += 1) {
    const payload = parsePayload(await fetchText(apiUrl(LAW_URL, {
      OC:oc,target,type:'JSON',mobileYn:'Y',search:2,query:item.name,display:100,page,sort:'lasc'
    })));
    const records = candidateRecords(payload,['별표명','별표서식명','별표일련번호','별표서식일련번호']);
    let added = 0;

    for (const record of records) {
      const title = first(record,'별표명','별표서식명','별표서식제목','별표제목');
      const seq = first(record,'별표일련번호','별표서식일련번호','별표명ID','id');
      const revisionDate = normalizeDate(first(record,'공포일자','발령일자','개정일자','시행일자','효력일자'));
      const appendixNo = first(record,'별표번호','별표서식번호');
      const appendixBranch = first(record,'별표가지번호','별표서식가지번호');
      const key = [seq,title,revisionDate,appendixNo,appendixBranch].join('|');
      if (!title || seen.has(key)) continue;
      seen.add(key); added += 1;
      const rawKind = first(record,'별표종류','별표서식구분명');
      const appendixKind = ['별표','서식','별지','별도','부록'].find((x) => rawKind.includes(x) || title.includes(x)) || '별표';
      output.push({
        appendixKey:key,
        title,
        appendixKind,
        revisionDate,
        revisionType:first(record,'제개정구분명','제개정구분','개정구분'),
        appendixNo,
        appendixBranch,
        lawId:first(record,'관련법령ID','법령ID') || item.lawId || '',
        lawMst:first(record,'관련법령일련번호','법령일련번호') || item.lawMst || '',
        adminId:first(record,'관련행정규칙일련번호','행정규칙일련번호') || item.adminId || '',
        comparisonUrl:'',
      });
    }
    if ((page > 1 && added === 0) || records.length < 100) break;
  }
  return output;
}

function v14AppendixUrl(change, appendix, date) {
  return v14MobileLawUrl({
    ...change,
    lawId:appendix?.lawId || change?.lawId,
    lawMst:appendix?.lawMst || change?.lawMst,
    adminId:appendix?.adminId || change?.adminId,
  }, {
    date:date || appendix?.revisionDate || change?.promulgationDate,
    appendix,
    allAppendices:true,
  });
}

async function openAppendices(eventKey,title) {
  const current = await dbGet('changes',eventKey);
  if (!current) return toast('별표 개정 정보를 찾지 못했습니다.');
  const allChanges = (await dbGetAll('changes'))
    .filter((row) => row.name === current.name && row.kind === current.kind)
    .sort((a,b) => (a.promulgationDate || '').localeCompare(b.promulgationDate || ''));
  const currentDate = current.promulgationDate || current.enforcementDate || '';
  const currentItems = Array.isArray(current.appendices) ? current.appendices : [];

  $('#appendix-dialog-subtitle').textContent = `${title} · 모바일 전후 원문`;

  if (!currentItems.length) {
    const allUrl = v14MobileLawUrl(current,{date:currentDate,allAppendices:true});
    $('#appendix-list').innerHTML = `<div class="card compact"><b>별표 개정 목록이 연결되지 않았습니다.</b><p>법제처 모바일 전문에서 해당 시점의 전체 별표·서식을 확인하세요.</p><div class="item-actions"><button data-open="${esc(allUrl)}">모바일 전체 별표 보기</button></div></div>`;
    $('#appendix-dialog').showModal();
    return;
  }

  const history = [];
  for (const change of allChanges) {
    for (const appendix of (change.appendices || [])) {
      history.push({change,appendix,date:appendix.revisionDate || change.promulgationDate || change.enforcementDate || ''});
    }
  }

  $('#appendix-list').innerHTML = `<div class="card compact"><b>모바일 별표 전후 보기</b><p>PC용 신구비교창 대신 법제처 모바일 원문을 개정 전·개정 후로 각각 엽니다.</p></div>` + currentItems.map((appendix) => {
    const afterDate = appendix.revisionDate || currentDate;
    const normalizedTitle = normalizeName(appendix.title);
    const previous = history
      .filter((entry) => normalizeName(entry.appendix.title) === normalizedTitle && entry.date && afterDate && entry.date < afterDate)
      .sort((a,b) => b.date.localeCompare(a.date))[0];
    const afterUrl = v14AppendixUrl(current,appendix,afterDate);
    const beforeUrl = previous ? v14AppendixUrl(previous.change,previous.appendix,previous.date) : '';
    const allUrl = v14MobileLawUrl(current,{date:afterDate,allAppendices:true});
    return `<article class="item"><h3>${esc(appendix.title)}</h3><p>${esc(appendix.appendixKind)} · 개정 후 ${esc(afterDate || '-')}</p>${previous ? `<p>개정 전 ${esc(previous.date || '-')}</p>` : '<p>저장된 이전 별표 이력이 없습니다.</p>'}<div class="item-actions">${beforeUrl ? `<button data-open="${esc(beforeUrl)}">개정 전 별표</button>`:''}<button data-open="${esc(afterUrl)}">개정 후 별표</button></div><div class="item-actions"><button data-open="${esc(allUrl)}">해당 시점 전체 별표</button></div></article>`;
  }).join('');
  $('#appendix-dialog').showModal();
}

function v14InstallUi() {
  const managedText = $('#page-managed .page-title p');
  if (managedText) managedText.textContent = '법령·행정규칙 전문은 법제처 모바일 화면으로 엽니다.';
  const changesText = $('#page-changes .page-title p');
  if (changesText) changesText.textContent = '전문과 별표 개정 전·후를 법제처 모바일 화면으로 확인합니다.';
}

async function boot() {
  try {
    if (!window.crypto?.subtle || !window.indexedDB) throw new Error('이 휴대폰의 Android System WebView가 너무 오래되었습니다. WebView를 업데이트하세요.');
    await registerServiceWorker();
    setupPwaInstall();
    v13InstallUi();
    v14InstallUi();
    state.db = await openDb();
    state.setup = await getMeta('setup',null);
    bindEvents();
    v13BindEvents();
    v13EnsureNoticeRange();
    if (!state.setup) {
      showSetup();
    } else if (await v13TryRememberedLogin()) {
      showApp();
      await refreshAll();
      maybeAutoSync();
    } else {
      showLogin();
    }
  } catch (error) {
    document.body.innerHTML = `<div class="gate"><div class="gate-card"><h1>앱 시작 실패</h1><p>${esc(error.message)}</p></div></div>`;
  }
}


/* v1.5.0: mobile legislation-notice relay and isolated sync failures */
const V15_NOTICE_FEED_URL = './data/notices.json?v=1.5.0';
let v15NoticeFeedPromise = null;

async function v15NoticeFeed(force = false) {
  if (force) v15NoticeFeedPromise = null;
  if (!v15NoticeFeedPromise) {
    v15NoticeFeedPromise = fetch(V15_NOTICE_FEED_URL, {cache:'no-store'}).then(async (response) => {
      if (!response.ok) throw new Error(`입법예고 중계 데이터 HTTP ${response.status}`);
      const data = await response.json();
      if (data.status !== 'ready') throw new Error(data.message || '입법예고 중계 데이터가 아직 준비되지 않았습니다.');
      if (!Array.isArray(data.records)) throw new Error('입법예고 중계 데이터 형식이 올바르지 않습니다.');
      return data;
    }).catch((error) => {
      v15NoticeFeedPromise = null;
      throw error;
    });
  }
  return v15NoticeFeedPromise;
}

async function fetchNotices(lawName) {
  const feed = await v15NoticeFeed();
  const range = v13NoticeRange();
  return feed.records.filter((row) => {
    const title = row.title || '';
    const startDate = row.startDate || row.announcedDate || '';
    const endDate = row.endDate || '';
    if (matchScore(lawName,title) < 25) return false;
    if (startDate && startDate > range.to) return false;
    if (endDate && endDate < range.from) return false;
    return true;
  }).map((row) => ({
    ...row,
    matchedItem:lawName,
    periodSide:v13PeriodSide(row.startDate || row.announcedDate || '',row.endDate || '',range.today),
  }));
}

async function testOfficialApis() {
  const button = $('#api-test-button');
  const box = $('#api-test-result');
  if (!button || !box) return;
  setBusy(button,true);
  box.innerHTML = '<p class="muted">연결 확인 중…</p>';
  const results = [];
  try {
    const lawOc = state.secrets?.lawOc;
    if (!lawOc) throw new Error('API OC 미입력');
    const text = await fetchText(apiUrl(LAW_URL,{OC:lawOc,target:'law',type:'JSON',mobileYn:'Y',query:'소방',display:3,page:1}),15000);
    const payload = parsePayload(text);
    const count = candidateRecords(payload,['법령명','법령명한글','법령ID']).length;
    results.push({name:'국가법령정보',ok:true,message:`정상 연결 · 확인 가능한 응답 ${count}건`});
  } catch (error) {
    results.push({name:'국가법령정보',ok:false,message:error.message});
  }
  try {
    const feed = await v15NoticeFeed(true);
    results.push({name:'입법예고',ok:true,message:`모바일 중계 정상 · ${feed.range?.from || '-'} ~ ${feed.range?.to || '-'} · ${feed.records.length}건 · ${formatDateTime(feed.generatedAt)}`});
  } catch (error) {
    results.push({name:'입법예고',ok:false,message:`모바일 중계 미설정 · ${error.message}`});
  }
  box.innerHTML = results.map((r) => `<div class="api-test-row ${r.ok?'ok':'fail'}"><b>${esc(r.name)}</b><span>${esc(r.message)}</span></div>`).join('');
  setBusy(button,false);
}

async function syncNow() {
  if (state.syncing) return toast('이미 동기화 중입니다.');
  if (!navigator.onLine) return toast('인터넷 연결을 확인하세요.');
  if (!state.secrets?.lawOc) return toast('설정에서 API OC를 입력하세요.');
  state.syncing = true;
  const items = (await managedRows()).filter((row) => row.enabled);
  if (!items.length) { state.syncing = false; return toast('활성 관리대상이 없습니다.'); }

  let lawFailures = 0;
  let noticeFailures = 0;
  let newChanges = 0;
  let newNotices = 0;
  let relayError = '';
  try { await v15NoticeFeed(true); }
  catch (error) { relayError = error.message; }

  renderSync(true,'동기화 준비',1);
  try {
    for (let index = 0; index < items.length; index += 1) {
      const item = items[index];
      const base = index / items.length * 100;
      let lawStatus = '정상';
      let noticeStatus = relayError ? '입법예고 중계 미설정' : '정상';
      let revisions = [];

      renderSync(true,`${item.name} 법령·별표 확인`,base + 2);
      try {
        revisions = await fetchRevisions(item);
        const hadBaseline = !!item.lastEventKey;
        for (const row of revisions) {
          const existing = await dbGet('changes',row.eventKey);
          if (!existing) {
            row.isNew = hadBaseline;
            row.detectedAt = new Date().toISOString();
            row.lastSeenAt = row.detectedAt;
            if (row.isNew) newChanges += 1;
            await dbPut('changes',row);
          } else {
            await dbPut('changes',{...existing,...row,appendices:existing.appendices || [],lastSeenAt:new Date().toISOString()});
          }
        }

        const appendices = await fetchAppendices(item);
        if (appendices.length && revisions.length) {
          const byDate = new Map();
          revisions.forEach((row) => {
            const key = row.promulgationDate || row.enforcementDate || '';
            if (!byDate.has(key)) byDate.set(key,row.eventKey);
          });
          for (const appendix of appendices) {
            const eventKey = byDate.get(appendix.revisionDate) || revisions[0].eventKey;
            const change = await dbGet('changes',eventKey);
            if (!change) continue;
            const map = new Map((change.appendices || []).map((entry) => [entry.appendixKey,entry]));
            map.set(appendix.appendixKey,appendix);
            await dbPut('changes',{...change,appendices:[...map.values()]});
          }
        }
      } catch (error) {
        lawFailures += 1;
        lawStatus = `법령 실패: ${cleanText(error.message).slice(0,80)}`;
        await addLog('법령 실패',`${item.name}: ${error.message}`);
      }

      renderSync(true,`${item.name} 입법예고 확인`,base + 8);
      if (!relayError) {
        try {
          const notices = await fetchNotices(item.name);
          const hadBaseline = !!item.lastNoticeCheckedAt;
          for (const notice of notices) {
            const existing = await dbGet('notices',notice.noticeKey);
            if (!existing) {
              notice.isNew = hadBaseline;
              notice.detectedAt = new Date().toISOString();
              if (notice.isNew) newNotices += 1;
              await dbPut('notices',notice);
            } else await dbPut('notices',{...existing,...notice});
          }
        } catch (error) {
          noticeFailures += 1;
          noticeStatus = `입법예고 실패: ${cleanText(error.message).slice(0,80)}`;
          await addLog('입법예고 실패',`${item.name}: ${error.message}`);
        }
      }

      const latest = revisions[0] || {};
      const checkStatus = lawStatus === '정상'
        ? (noticeStatus === '정상' ? '정상' : `법령 정상 · ${noticeStatus}`)
        : lawStatus;
      await dbPut('managed',{
        ...item,
        lawId:latest.lawId || item.lawId || '',
        lawMst:latest.lawMst || item.lawMst || '',
        adminId:latest.adminId || item.adminId || '',
        sourceId:latest.sourceId || item.sourceId || '',
        officialUrl:v14MobileLawUrl({...item,...latest}),
        lastEventKey:latest.eventKey || item.lastEventKey || '',
        lastRevisionType:latest.revisionType || item.lastRevisionType || '',
        lastRevisionDate:latest.promulgationDate || item.lastRevisionDate || '',
        lastEnforcementDate:latest.enforcementDate || item.lastEnforcementDate || '',
        lastCheckedAt:new Date().toISOString(),
        lastNoticeCheckedAt:relayError ? item.lastNoticeCheckedAt || '' : new Date().toISOString(),
        checkStatus,
        updatedAt:new Date().toISOString(),
      });
      renderSync(true,`${index + 1}/${items.length} 완료`,(index + 1) / items.length * 100);
    }

    const now = new Date().toISOString();
    await setMeta('lastSync',now);
    if (relayError) await addLog('입법예고 중계',relayError);
    await addLog(lawFailures || noticeFailures || relayError ? '부분완료':'완료',`관리대상 ${items.length}건 · 신규 개정 ${newChanges}건 · 신규 입법예고 ${newNotices}건 · 법령 실패 ${lawFailures}건 · 입법예고 실패 ${noticeFailures}건`);
    toast(relayError ? '법령 동기화 완료 · 입법예고 중계 설정 필요' : `동기화 완료 · 신규 개정 ${newChanges}건 · 신규 입법예고 ${newNotices}건`,4200);
  } finally {
    state.syncing = false;
    renderSync(false,'',0);
    await refreshAll();
  }
}

function v15InstallUi() {
  const cards = [...document.querySelectorAll('#page-settings .card')];
  const apiCard = cards.find((card) => card.querySelector('#api-test-button'));
  const apiText = apiCard?.querySelector('.muted');
  if (apiText) apiText.textContent = '국가법령정보는 휴대폰에서 직접 연결하고, 입법예고는 브라우저 차단을 피하기 위해 GitHub Actions 중계 데이터로 확인합니다.';
  const noticeTitle = $('#page-notices .page-title p');
  if (noticeTitle) {
    const range = v13NoticeRange();
    noticeTitle.textContent = `오늘 기준 전후 6개월(${range.from} ~ ${range.to}) 중계 데이터를 비교합니다.`;
  }
}

async function boot() {
  try {
    if (!window.crypto?.subtle || !window.indexedDB) throw new Error('이 휴대폰의 Android System WebView가 너무 오래되었습니다. WebView를 업데이트하세요.');
    await registerServiceWorker();
    setupPwaInstall();
    v13InstallUi();
    v14InstallUi();
    v15InstallUi();
    state.db = await openDb();
    state.setup = await getMeta('setup',null);
    bindEvents();
    v13BindEvents();
    v13EnsureNoticeRange();
    if (!state.setup) showSetup();
    else if (await v13TryRememberedLogin()) {
      showApp();
      await refreshAll();
      maybeAutoSync();
    } else showLogin();
  } catch (error) {
    document.body.innerHTML = `<div class="gate"><div class="gate-card"><h1>앱 시작 실패</h1><p>${esc(error.message)}</p></div></div>`;
  }
}


/* v1.5.1 — non-destructive mobile link repair. No DB or credential migration.
 * Law API contract: open.law.go.kr/LSO/openApi/guideResult.do?htmlName=mobLsInfoGuide
 * Administrative rule: ...?htmlName=mobAdmrulInfoGuide
 * window.open(..., 'noopener') may return null on success; never navigate the app as fallback.
 */
function v151NormalizeUrl(value, base = 'https://www.law.go.kr/') {
  let text = String(value ?? '').trim().replace(/&amp;/gi, '&');
  if (!text || /[\u0000-\u001f\u007f\\]/.test(text)) return '';
  if (/^(?:www\.)?(?:law|lawmaking)\.go\.kr(?:[/?#]|$)/i.test(text)) text = `https://${text}`;
  if (!/^(?:https?:\/\/|\/)/i.test(text) && !/^(?:LSW|DRF)\//i.test(text)) return '';
  try {
    const url = new URL(text, base);
    const official = /^(?:[a-z0-9-]+\.)*(?:law|lawmaking)\.go\.kr$/i.test(url.hostname);
    if (url.username || url.password) return '';
    if (url.protocol === 'http:' && official && !url.port) url.protocol = 'https:';
    if (url.protocol !== 'https:') return '';
    return url.href;
  } catch (_) { return ''; }
}

function v151Number(value) {
  const text = String(value ?? '').trim();
  return /^\d+$/.test(text) ? text : '';
}

function v151Param(url, ...names) {
  if (!url) return '';
  for (const name of names) {
    for (const [key, value] of url.searchParams) {
      if (key.toLowerCase() === name.toLowerCase() && value.trim()) return value.trim();
    }
  }
  return '';
}

function v151LawSource(item) {
  const value = v151NormalizeUrl(item?.officialUrl || item?.originalUrl || '');
  if (!value) return null;
  const url = new URL(value);
  return /^(?:www\.)?law\.go\.kr$/i.test(url.hostname) ? url : null;
}

function v151LawIdentity(item = {}) {
  const source = v151LawSource(item);
  const admin = item.kind === '행정규칙';
  const sourceAdmin = v151Param(source, 'target') === 'admrul' || /admRul/i.test(source?.pathname || '') || !!v151Param(source, 'admRulSeq');
  const matching = !source || admin === sourceAdmin;
  return {
    source,
    name:cleanText(item.name || (matching ? v151Param(source, 'LM') : '')),
    lawId:admin ? '' : v151Number(item.lawId || item.mobileId || (matching ? v151Param(source, 'ID', 'lsId') : '')),
    lawMst:admin ? '' : v151Number(item.lawMst || item.mobileMst || item.mst || (matching ? v151Param(source, 'MST', 'lsiSeq') : '')),
    adminId:admin ? v151Number(item.adminId || (matching ? v151Param(source, 'ID', 'admRulSeq') : '')) : '',
  };
}

function v151PublicLawUrl(item = {}) {
  const id = v151LawIdentity(item);
  if (item.kind === '행정규칙' && id.adminId) {
    return apiUrl('https://www.law.go.kr/LSW/admRulInfoP.do', {admRulSeq:id.adminId});
  }
  if (item.kind !== '행정규칙' && id.lawMst) {
    return apiUrl('https://www.law.go.kr/LSW/lsInfoP.do', {lsiSeq:id.lawMst});
  }
  if (id.source && !/\/DRF\//i.test(id.source.pathname)) return id.source.href;
  // A date-only historical link cannot identify a revision reliably. Never silently show current law.
  if (item.eventKey || item.promulgationDate) return '';
  if (!id.name) return '';
  const kind = item.kind === '행정규칙' ? '행정규칙' : '법령';
  return `https://www.law.go.kr/${encodeURIComponent(kind)}/${encodeURIComponent(id.name)}`;
}

function v151AppendixNumbers(appendix = {}) {
  const raw = cleanText(appendix.appendixNo || '');
  const match = raw.match(/^(?:별표|별지|서식)?\s*(\d+)(?:\s*의\s*(\d+))?$/) ||
    cleanText(appendix.title || '').match(/^(?:\[\s*)?(?:별표|별지(?:\s*제)?|서식)\s*(\d+)(?:\s*의\s*(\d+))?/);
  return {
    number:v151Number(match?.[1] || raw),
    branch:v151Number(appendix.appendixBranch || match?.[2] || ''),
  };
}

function v14MobileLawUrl(item = {}, options = {}) {
  const admin = item.kind === '행정규칙';
  const id = v151LawIdentity(item);
  const oc = cleanText(state.secrets?.lawOc || '');
  if (!oc) return v151PublicLawUrl(item);
  const params = {OC:oc, target:admin ? 'admrul' : 'law', type:'HTML', mobileYn:'Y'};
  if (admin && id.adminId) params.ID = id.adminId;
  else if (!admin && id.lawMst) params.MST = id.lawMst;
  else if (!admin && id.lawId) params.ID = id.lawId;
  else if (id.name) params.LM = id.name;
  else return v151PublicLawUrl(item);
  // LD means law promulgation date, not appendix revision or enforcement date.
  if (!admin && !id.lawMst) {
    const date = String(item.promulgationDate || options.date || '').replace(/[-.\s]/g, '');
    if (/^\d{8}$/.test(date)) params.LD = date;
  }
  // The mobile administrative-rule endpoint has no BD/BN parameters.
  // Keep the exact rule version instead of searching today's admbyl list by name.
  if (!admin && (options.allAppendices || options.appendix)) {
    params.BD = 'ON';
    if (options.appendix) {
      params.BT = v14AppendixTypeCode(options.appendix.appendixKind || options.appendix.title);
      const numbers = v151AppendixNumbers(options.appendix);
      if (numbers.number) params.BN = String(Number(numbers.number));
      if (numbers.branch) params.BG = String(Number(numbers.branch));
    }
  }
  return apiUrl('https://www.law.go.kr/DRF/lawService.do', params);
}

function officialLawUrl(kind, name, supplied = '') {
  const normalized = v151NormalizeUrl(supplied);
  if (normalized && /^(?:www\.)?law\.go\.kr$/i.test(new URL(normalized).hostname)) return normalized;
  return v14MobileLawUrl({kind, name});
}

function v14AppendixUrl(change = {}, appendix = {}, date) {
  // A current appendix's parent MST must not replace the selected historical change MST.
  const sameDate = !!appendix.revisionDate && appendix.revisionDate === change.promulgationDate;
  const identity = v151LawIdentity(change);
  return v14MobileLawUrl({
    ...change,
    lawId:identity.lawId || (sameDate ? appendix.lawId : '') || '',
    lawMst:identity.lawMst || (sameDate ? appendix.lawMst : '') || '',
    adminId:identity.adminId || (sameDate ? appendix.adminId : '') || '',
  }, {date:change.promulgationDate || date, appendix, allAppendices:true});
}

function v151ExternalUrl(value) {
  const normalized = v151NormalizeUrl(value);
  if (!normalized) return '';
  const url = new URL(normalized);
  // Repair legacy notice links without exposing OC in a public detail link.
  if (/^(?:www\.)?lawmaking\.go\.kr$/i.test(url.hostname)) {
    const notice = url.pathname.match(/^\/rest\/ogLmPp\/(\d+)\/[^/]+\/[^/]+\.html$/i);
    if (notice) return `https://opinion.lawmaking.go.kr/gcom/ogLmPp/${notice[1]}`;
  }
  if (/^(?:www\.)?law\.go\.kr$/i.test(url.hostname) && /\/DRF\/lawService\.do$/i.test(url.pathname)) {
    const target = v151Param(url, 'target').toLowerCase();
    if (target === 'law' || target === 'admrul') {
      const oc = cleanText(state.secrets?.lawOc || '');
      if (!oc) return v151PublicLawUrl({kind:target === 'admrul' ? '행정규칙' : '법령', name:v151Param(url,'LM'), officialUrl:url.href, promulgationDate:v151Param(url,'LD')});
      for (const key of [...url.searchParams.keys()]) {
        if (/^(?:oc|type|mobileyn)$/i.test(key)) url.searchParams.delete(key);
      }
      url.searchParams.set('OC', oc);
      url.searchParams.set('type', 'HTML');
      url.searchParams.set('mobileYn', 'Y');
    }
  }
  return url.href;
}

function openUrl(value) {
  const url = v151ExternalUrl(value);
  if (!url) return toast('원문 주소를 확인할 수 없습니다. API 설정과 동기화 상태를 확인해 주세요.');
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.referrerPolicy = 'no-referrer';
  link.hidden = true;
  try {
    document.body.appendChild(link);
    link.click();
  } catch (_) {
    toast('브라우저로 연결하지 못했습니다. 휴대폰의 기본 브라우저 설정을 확인해 주세요.');
  } finally { link.remove(); }
}

function v14InstallUi() {
  const managed = $('#page-managed .page-title p');
  if (managed) managed.textContent = '법령·행정규칙 전문을 새 창으로 엽니다. · 링크 수정 v1.5.1';
  const changes = $('#page-changes .page-title p');
  if (changes) changes.textContent = '법령 별표는 개정 전·후로, 행정규칙 별표는 해당 버전 원문에서 확인합니다.';
}


/* v1.5.2: mobile reader for official JSON text; never execute remote HTML.
 * The legacy mobile HTML samples return a blank body or redirect appendices to desktop.
 * This reader uses the user's existing OC and the documented law/admrul body API.
 * No database migration, credential copy, API proxy, or persistent response cache.
 */
const V152_VERSION = '1.5.2';
let v152Generation = 0;
let v152Document = null;
let v152SelectedSpec = null;
let v152FontSize = 17;

function v152Base() { return new URL('./', window.location.href); }
function v152Date(value) { return normalizeDate(value || '').replace(/-/g, ''); }
function v152List(value) { return value == null || value === '' ? [] : Array.isArray(value) ? value : [value]; }
function v152Strings(value) {
  if (value == null) return [];
  if (typeof value === 'string' || typeof value === 'number') return [String(value)];
  if (Array.isArray(value)) return value.flatMap(v152Strings);
  if (typeof value === 'object') return v152Strings(value.content ?? value['#text'] ?? '');
  return [];
}
function v152Text(value) { return v152Strings(value).join('\n'); }
function v152EqualId(a,b) { return !!a && !!b && String(a).replace(/^0+/, '') === String(b).replace(/^0+/, ''); }

function v152SpecFromUrl(value) {
  const normalized = v151NormalizeUrl(value);
  if (!normalized) return null;
  const u = new URL(normalized), base = v152Base();
  const local = u.origin === base.origin && u.pathname === base.pathname && u.searchParams.get('jlmReader') === '1';
  const official = /^(?:www\.)?law\.go\.kr$/i.test(u.hostname);
  if (!local && !official) return null;
  const path = decodeURIComponent(u.pathname), target = v151Param(u,'target').toLowerCase();
  const admin = local ? u.searchParams.get('kind') === 'admrul' : target === 'admrul' || /admRul/i.test(path) || path.startsWith('/행정규칙/');
  const named = path.match(/^\/(법령|행정규칙)\/([^/]+)\/?$/);
  const recognized = local || !!named || /\/(?:lsInfo[PR]|lsBylInfoR|admRulInfo[PR])\.do$/i.test(path) || /\/DRF\/lawService\.do$/i.test(path) && ['law','eflaw','admrul'].includes(target);
  if (!recognized) return null;
  return {
    kind:admin ? '행정규칙' : '법령',
    name:v151Param(u,'LM') || named?.[2] || '',
    lawMst:admin ? '' : v151Number(v151Param(u,'MST','lsiSeq')),
    lawId:admin ? '' : v151Number(v151Param(u,'ID','lsId')),
    adminId:admin ? v151Number(v151Param(u,'ID','admRulSeq')) : '',
    promulgationDate:v152Date(v151Param(u,'PD','LD')),
    enforcementDate:v152Date(v151Param(u,'EF','efYd')),
    history:local && u.searchParams.get('history') === '1',
    view:v151Param(u,'view') === 'appendices' || v151Param(u,'BD') === 'ON' || /lsBylInfoR/i.test(path) ? 'appendices' : 'body',
    appendixNo:v151Number(v151Param(u,'BN')),
    appendixBranch:v151Number(v151Param(u,'BG')),
    appendixType:v151Number(v151Param(u,'BT')),
  };
}

function v14MobileLawUrl(item = {}, options = {}) {
  const stored = v152SpecFromUrl(item.officialUrl || '') || {};
  const ids = v151LawIdentity(item);
  const kind = item.kind || stored.kind || '법령';
  const u = v152Base();
  u.search = ''; u.hash = '';
  const numbers = options.appendix ? v151AppendixNumbers(options.appendix) : {};
  const params = {
    jlmReader:'1', kind:kind === '행정규칙' ? 'admrul' : 'law',
    LM:item.name || stored.name || ids.name,
    MST:kind === '법령' ? ids.lawMst || stored.lawMst : '',
    ID:kind === '행정규칙' ? ids.adminId || stored.adminId : ids.lawId || stored.lawId,
    PD:v152Date(item.promulgationDate || stored.promulgationDate || options.date),
    EF:v152Date(item.enforcementDate || stored.enforcementDate),
    history:item.eventKey || stored.history ? '1' : '',
    view:options.appendix || options.allAppendices ? 'appendices' : 'body',
    BN:numbers.number, BG:numbers.branch,
    BT:options.appendix ? v14AppendixTypeCode(options.appendix.appendixKind || options.appendix.title) : '',
  };
  for (const [k,v] of Object.entries(params)) if (v !== '' && v != null) u.searchParams.set(k,String(v));
  return u.href;
}

function v152BodyUrl(spec) {
  const oc = cleanText(state.secrets?.lawOc || '');
  if (!oc) throw new Error('설정에서 국가법령정보 API OC를 입력한 뒤 다시 열어 주세요.');
  const p = {OC:oc, target:spec.kind === '행정규칙' ? 'admrul' : 'law', type:'JSON'};
  if (spec.kind === '행정규칙' && spec.adminId) p.ID = spec.adminId;
  else if (spec.kind !== '행정규칙' && spec.lawMst) p.MST = spec.lawMst;
  else if (spec.kind !== '행정규칙' && spec.lawId) p.ID = spec.lawId;
  else throw new Error('원문 식별번호를 확인하지 못했습니다. 관리대상을 동기화한 뒤 다시 열어 주세요.');
  if (spec.kind !== '행정규칙' && spec.promulgationDate) p.LD = spec.promulgationDate;
  return apiUrl('https://www.law.go.kr/DRF/lawService.do',p);
}

async function v152ResolveSpec(spec) {
  if (spec.kind === '행정규칙' ? spec.adminId : spec.lawMst || spec.lawId) return spec;
  if (!spec.name) throw new Error('법령명과 원문 식별번호가 없습니다. 관리대상을 다시 동기화해 주세요.');
  const oc = cleanText(state.secrets?.lawOc || '');
  if (!oc) throw new Error('설정에서 국가법령정보 API OC를 입력해 주세요.');
  const target = spec.kind === '행정규칙' ? 'admrul' : 'law';
  for (let page = 1; page <= 3; page += 1) {
    const p = {OC:oc,target,type:'JSON',query:spec.name,search:1,display:100,page};
    if (spec.promulgationDate || spec.history) p.nw = '1,2,3';
    const payload = parsePayload(await fetchText(apiUrl(LAW_URL,p),20000));
    const records = candidateRecords(payload,['법령명한글','법령명','행정규칙명']);
    const match = records.find((r) => cleanText(recordName(r)).replace(/\s/g,'') === cleanText(spec.name).replace(/\s/g,'') &&
      (!spec.promulgationDate || v152Date(first(r,'공포일자','발령일자')) === spec.promulgationDate));
    if (match) {
      if (spec.history && !spec.promulgationDate) throw new Error('과거 이력을 특정할 공포일이 없습니다. 최신 법령으로 임의 연결하지 않았습니다.');
      return {...spec,
        lawMst:target === 'law' ? first(match,'법령일련번호','MST') : '',
        lawId:target === 'law' ? first(match,'법령ID') : '',
        adminId:target === 'admrul' ? first(match,'행정규칙일련번호') : '',
      };
    }
    if (records.length < 100) break;
  }
  throw new Error('해당 이름·공포일과 일치하는 원문을 찾지 못했습니다. 다른 버전으로 대체하지 않았습니다.');
}

function v152DecodeDocument(payload, spec) {
  const root = spec.kind === '행정규칙' ? payload?.AdmRulService || payload?.행정규칙 : payload?.법령;
  const meta = root && (root.기본정보 || root.행정규칙기본정보);
  if (!meta || typeof meta !== 'object') throw new Error('공식 API가 원문 데이터를 반환하지 않았습니다. 설정의 OC 및 법령·행정규칙 본문 이용권한을 확인해 주세요.');
  const name = first(meta,'법령명_한글','법령명한글','행정규칙명');
  if (!name) throw new Error('응답에 법령명이 없어 원문으로 표시하지 않았습니다.');
  const pd = v152Date(first(meta,'공포일자','발령일자'));
  if (spec.promulgationDate && pd !== v152Date(spec.promulgationDate)) throw new Error('선택한 공포일과 API 응답의 공포일이 다릅니다. 다른 이력을 원문으로 표시하지 않았습니다.');
  if (spec.lawId && !v152EqualId(spec.lawId,first(meta,'법령ID'))) throw new Error('법령 ID가 일치하지 않아 표시를 중단했습니다.');
  if (spec.adminId && !v152EqualId(spec.adminId,first(meta,'행정규칙일련번호'))) throw new Error('행정규칙 이력 번호가 일치하지 않아 표시를 중단했습니다.');
  const body = root.조문?.조문단위 ?? root.조문내용;
  const appendices = v152List(root.별표?.별표단위);
  if (!body && !appendices.length && !root.첨부파일) throw new Error('본문과 첨부파일을 확인할 수 없습니다. 빈 응답을 정상으로 처리하지 않았습니다.');
  return {root,meta,name,promulgationDate:pd,enforcementDate:v152Date(first(meta,'시행일자')),body,appendices,spec};
}

function v152Paragraphs(value, level = 0) {
  return v152Strings(value).filter((s) => s.trim()).map((s) => `<p class="jlm-text jlm-level-${Math.min(level,3)}">${esc(s)}</p>`).join('');
}
function v152Clause(value, level = 0) {
  if (typeof value === 'string' || typeof value === 'number') return v152Paragraphs(value,level);
  if (Array.isArray(value)) return value.map((r) => v152Clause(r,level)).join('');
  if (!value || typeof value !== 'object') return '';
  let html = '';
  for (const key of ['조문내용','항내용','호내용','목내용','세목내용']) if (value[key] != null) html += v152Paragraphs(value[key],level);
  for (const key of ['조문단위','조문','항단위','호단위','목단위']) if (value[key] != null) html += v152Clause(value[key],level);
  for (const key of ['항','호','목','세목']) if (value[key] != null) html += v152Clause(value[key],level+1);
  if (value.조문참고자료) html += v152Paragraphs(value.조문참고자료,level);
  return html;
}
function v152FileLink(value, label) {
  const url = v151NormalizeUrl(v152Text(value));
  if (!url || !/^(?:[a-z0-9-]+\.)?law\.go\.kr$/i.test(new URL(url).hostname)) return '';
  return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${esc(label)}</a>`;
}
function v152Attachments(root) {
  return v152List(root.첨부파일).map((r) => `<div class="jlm-file-actions">${v152FileLink(r?.첨부파일링크,r?.첨부파일명 || '공식 첨부파일 열기')}</div>`).join('');
}
function v152AppendixMatches(row, spec) {
  if (!spec.appendixNo) return false;
  return v152EqualId(row.별표번호,spec.appendixNo) && Number(row.별표가지번호 || 0) === Number(spec.appendixBranch || 0) &&
    (!spec.appendixType || v14AppendixTypeCode(row.별표구분 || '') === Number(spec.appendixType));
}
function v152RenderBody(doc) {
  let body = v152List(doc.body).map((row) => v152Clause(row)).filter((html) => html.trim()).map((html) => `<article class="jlm-article">${html}</article>`).join('');
  if (!body.trim()) body = '<p>이 원문의 본문은 첨부파일로 제공될 수 있습니다. 아래 공식 첨부파일을 확인해 주세요.</p>';
  for (const [label,value] of [['제정·개정이유',doc.root.제개정이유?.제개정이유내용],['개정문',doc.root.개정문?.개정문내용]]) {
    if (v152Text(value)) body += `<details><summary>${label}</summary>${v152Paragraphs(value)}</details>`;
  }
  return body + v152Attachments(doc.root);
}
function v152RenderAppendices(doc) {
  const rows = doc.appendices;
  let html = '<p class="jlm-note">표의 열·병합 구조는 공식 PDF 또는 원본 파일에서 확인하세요. 아래 텍스트는 API 제공값이며, 제공되지 않은 내용을 임의로 보완하지 않습니다.</p>';
  if (!rows.length) return html + '<p>이 버전의 API 응답에 별표·서식 목록이 없습니다.</p>' + v152Attachments(doc.root);
  if (doc.spec.appendixNo && !rows.some((r) => v152AppendixMatches(r,doc.spec))) html += '<p class="jlm-warning">선택한 번호의 별표를 이 버전에서 찾지 못했습니다. 다른 별표로 대체하지 않고 전체 목록을 표시합니다.</p>';
  return html + rows.map((r,index) => {
    const selected = v152AppendixMatches(r,doc.spec);
    const number = Number(r.별표번호 || 0), branch = Number(r.별표가지번호 || 0);
    const title = `${r.별표구분 || '별표'} ${number || ''}${branch ? '의'+branch : ''} ${r.별표제목 || ''}`;
    const links = v152FileLink(r.별표서식PDF파일링크,'공식 PDF 열기') + v152FileLink(r.별표서식파일링크,'공식 원본 파일 열기') +
      v152FileLink(r.별표서식이미지파일링크,'공식 이미지 열기');
    return `<article class="jlm-article ${selected?'jlm-selected':''}" ${selected?'data-jlm-selected="true"':''}><h3>${esc(title)}</h3><div class="jlm-file-actions">${links}</div><details ${selected?'open':''}><summary>API 제공 텍스트</summary>${v152Paragraphs(r.별표내용) || '<p>텍스트가 제공되지 않습니다. 공식 파일을 열어 확인해 주세요.</p>'}</details></article>`;
  }).join('');
}

function v152EnsureReader() {
  let dialog = $('#jlm-reader');
  if (dialog) return dialog;
  const style = document.createElement('style');
  style.textContent = '#jlm-reader{width:min(100vw,840px);height:94vh;height:94dvh;max-width:100vw;max-height:100dvh;margin:auto;border:0;border-radius:18px;padding:0;color:var(--ink);background:#fff}#jlm-reader[open]{display:flex;flex-direction:column}#jlm-reader *{min-width:0}#jlm-reader header{display:flex;align-items:flex-start;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line);flex-shrink:0}#jlm-reader header>div{flex:1}#jlm-reader h2{font-size:19px;line-height:1.4;margin:0;overflow-wrap:anywhere}#jlm-reader h3{font-size:18px;line-height:1.5}#jlm-reader .jlm-note{font-size:12px;line-height:1.6;color:var(--muted);margin:5px 0;overflow-wrap:anywhere}#jlm-reader button{min-height:42px;border-radius:10px;padding:9px 12px;color:var(--blue);background:#eef5fa}#jlm-reader [data-jlm-close]{flex:0 0 auto;font-size:17px}#jlm-reader nav{display:flex;gap:5px;flex-wrap:wrap;padding:9px 12px;border-bottom:1px solid var(--line);flex-shrink:0}#jlm-reader nav button[aria-pressed=true]{background:var(--blue);color:white}#jlm-reader-main{overflow:auto;overscroll-behavior:contain;flex:1;padding:16px 18px calc(24px + env(safe-area-inset-bottom));font-size:var(--jlm-font,17px);line-height:1.85;overflow-wrap:anywhere}#jlm-reader .jlm-text{white-space:pre-wrap;margin:0 0 12px}#jlm-reader .jlm-level-1{padding-left:8px}#jlm-reader .jlm-level-2{padding-left:16px}#jlm-reader .jlm-level-3{padding-left:24px}#jlm-reader .jlm-article{margin-bottom:18px;padding:14px 0;border-bottom:1px solid var(--line)}#jlm-reader details{margin:12px 0}#jlm-reader summary{cursor:pointer;font-weight:700;min-height:36px}#jlm-reader .jlm-file-actions{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}#jlm-reader .jlm-file-actions a{font-size:14px;padding:9px 11px;border-radius:10px;background:#eef5fa;color:var(--blue);text-decoration:none;max-width:100%;overflow-wrap:anywhere}#jlm-reader .jlm-warning{border-left:4px solid #b54708;padding:12px;background:#fff8eb;font-size:14px;line-height:1.65}#jlm-reader .jlm-selected{border:2px solid var(--blue);border-radius:12px;padding:12px}#jlm-reader-status{font-size:14px}#jlm-reader [data-jlm-retry]{margin-top:12px}@media(max-width:600px){#jlm-reader{height:100vh;height:100dvh;border-radius:0}#jlm-reader header{padding-top:calc(12px + env(safe-area-inset-top))}}';
  document.head.appendChild(style);
  dialog = document.createElement('dialog'); dialog.id = 'jlm-reader'; dialog.setAttribute('aria-labelledby','jlm-reader-title');
  dialog.innerHTML = '<header><div><h2 id="jlm-reader-title">모바일 원문</h2><p class="jlm-note">국가법령정보센터 API 원문 · 정원이앤씨 모바일 열람창</p><p id="jlm-reader-meta" class="jlm-note"></p></div><button type="button" data-jlm-close autofocus aria-label="원문 열람창 닫기">닫기</button></header><nav aria-label="원문 보기"><button data-jlm-tab="body">본문</button><button data-jlm-tab="supplements">부칙</button><button data-jlm-tab="appendices">별표·서식</button><button data-jlm-font="-1" aria-label="원문 글자 축소">가−</button><button data-jlm-font="1" aria-label="원문 글자 확대">가＋</button></nav><div id="jlm-reader-main"><div id="jlm-reader-status" role="status"></div><div id="jlm-reader-content"></div></div>';
  document.body.appendChild(dialog);
  dialog.addEventListener('close',() => { if (!dialog.open) { v152Generation += 1; v152Document = null; } });
  dialog.addEventListener('click',(e) => {
    if (e.target.closest('[data-jlm-close]')) return dialog.close();
    const tab = e.target.closest('[data-jlm-tab]');
    if (tab) return v152ShowTab(tab.dataset.jlmTab);
    const font = e.target.closest('[data-jlm-font]');
    if (font) {v152FontSize = Math.max(14,Math.min(24,v152FontSize+Number(font.dataset.jlmFont))); dialog.style.setProperty('--jlm-font',v152FontSize+'px');}
    if (e.target.closest('[data-jlm-retry]') && v152SelectedSpec) v152OpenReader(v152SelectedSpec);
  });
  return dialog;
}

function v152ShowTab(tab) {
  if (!v152Document) return;
  const doc = v152Document;
  $('#jlm-reader').querySelectorAll('[data-jlm-tab]').forEach((b) => b.setAttribute('aria-pressed',String(b.dataset.jlmTab === tab)));
  const content = $('#jlm-reader-content');
  if (tab === 'appendices') content.innerHTML = v152RenderAppendices(doc);
  else if (tab === 'supplements') {
    const units = doc.root.부칙?.부칙단위 ?? doc.root.부칙;
    content.innerHTML = v152List(units).map((r) => `<article class="jlm-article">${v152Paragraphs(typeof r === 'string' ? r : r?.부칙내용)}</article>`).join('') || '<p>이 버전의 API 응답에 부칙 텍스트가 없습니다.</p>';
  } else content.innerHTML = v152RenderBody(doc);
  $('#jlm-reader-main').scrollTop = 0;
  const selected = content.querySelector('[data-jlm-selected]');
  if (selected) selected.scrollIntoView({block:'start'});
}

async function v152OpenReader(inputSpec) {
  const dialog = v152EnsureReader(), generation = ++v152Generation;
  v152Document = null; v152SelectedSpec = {...inputSpec};
  $('#jlm-reader-title').textContent = inputSpec.name || '모바일 원문';
  $('#jlm-reader-meta').textContent = '';
  $('#jlm-reader-content').replaceChildren();
  $('#jlm-reader-status').textContent = '공식 원문을 불러오는 중입니다…';
  if (!dialog.open) dialog.showModal();
  try {
    const spec = await v152ResolveSpec(inputSpec);
    const raw = await fetchText(v152BodyUrl(spec),25000);
    let payload;
    try { payload = JSON.parse(raw.replace(/^\uFEFF/,'')); }
    catch (_) { throw new Error('원문 JSON 대신 다른 응답이 반환되었습니다. OC·본문 API 이용권한 또는 공식 서비스 상태를 확인해 주세요.'); }
    const doc = v152DecodeDocument(payload,spec);
    if (generation !== v152Generation || !dialog.open) return;
    v152Document = doc; v152SelectedSpec = spec;
    $('#jlm-reader-title').textContent = doc.name;
    $('#jlm-reader-meta').textContent = `${spec.kind} · 공포/발령 ${normalizeDate(doc.promulgationDate) || '-'} · API 시행일 ${normalizeDate(doc.enforcementDate) || '-'} · ${first(doc.meta,'공포번호','발령번호') || ''}호`;
    const status = $('#jlm-reader-status'); status.replaceChildren();
    if (spec.enforcementDate && spec.enforcementDate !== doc.enforcementDate) {
      const note = document.createElement('p'); note.className = 'jlm-warning';
      note.textContent = `목록의 시행일(${normalizeDate(spec.enforcementDate)})과 공포일 기준 본문 API의 시행일(${normalizeDate(doc.enforcementDate) || '미제공'})이 다릅니다. 일부 조항의 단계 시행 여부는 부칙과 공식 원문을 함께 확인하세요. 이 화면을 선택 시행일의 통합 현행본문으로 단정하지 마세요.`;
      status.appendChild(note);
    }
    v152ShowTab(spec.view === 'appendices' ? 'appendices' : 'body');
  } catch (error) {
    if (generation !== v152Generation || !dialog.open) return;
    $('#jlm-reader-status').innerHTML = `<p class="jlm-warning">${esc(error.message || '원문을 불러오지 못했습니다.')}</p><p>빈 화면이나 다른 법령으로 자동 이동하지 않았습니다.</p><button type="button" data-jlm-retry>다시 연결</button>`;
  }
}

function openUrl(value) {
  let spec;
  try { spec = v152SpecFromUrl(value); }
  catch (_) { return toast('원문 주소 형식을 확인할 수 없습니다.'); }
  if (spec) { void v152OpenReader(spec); return; }
  const url = v151ExternalUrl(value);
  if (!url) return toast('원문 주소를 확인할 수 없습니다.');
  const a = document.createElement('a');
  a.href=url; a.target='_blank'; a.rel='noopener noreferrer'; a.referrerPolicy='no-referrer'; a.hidden=true;
  try {document.body.appendChild(a); a.click();} finally {a.remove();}
}

const v152PriorAppendixDialog = openAppendices;
openAppendices = async function(eventKey,title) {
  await v152PriorAppendixDialog(eventKey,title);
  const subtitle = $('#appendix-dialog-subtitle');
  if (subtitle) subtitle.textContent = `${title} · 앱 내 모바일 원문`;
  const card = $('#appendix-list .card');
  if (card) {
    const heading = card.querySelector('b'), paragraph = card.querySelector('p');
    if (heading) heading.textContent = '모바일 원문에서 별표·서식 확인';
    if (paragraph) paragraph.textContent = '개정 전·후의 해당 법령 본문 API를 각각 조회합니다. 별표가 제공되면 PDF 또는 원본 파일을 열 수 있습니다.';
  }
};

function v14InstallUi() {
  const managed = $('#page-managed .page-title p');
  if (managed) managed.textContent = '공식 원문을 앱 내 모바일 열람창으로 엽니다. · 모바일 원문 v1.5.2';
  const changes = $('#page-changes .page-title p');
  if (changes) changes.textContent = '선택한 공포 이력의 본문·부칙·별표를 모바일에서 확인합니다.';
  const settings = $('#page-settings .page-title p');
  if (settings) settings.textContent = '모바일 원문 v1.5.2 · 기존 API OC와 저장 자료 유지';
}


/* v1.5.3: cache-independent entry, guarded legacy routes and redacted diagnostics.
 * No IndexedDB schema changes, credential migration, database/cache clearing.
 */
const V153_VERSION = '1.5.3';
window.JLM_BUILD = Object.freeze({version:V153_VERSION, mode:'in-app-json', bundled:true});

const v153PriorFetchText = fetchText;
fetchText = async function(url, timeout) {
  const raw = await v153PriorFetchText(url, timeout);
  const u = new URL(url, window.location.href);
  if (/^(?:www\.)?law\.go\.kr$/i.test(u.hostname) && /\/DRF\/lawService\.do$/i.test(u.pathname) &&
      u.searchParams.get('type') === 'JSON' && /^\s*</.test(raw) && /기본정보\s*조회\s*실패/.test(raw)) {
    throw new Error('JLM-153-BASIC: 공식 API가 해당 식별번호의 기본정보를 찾지 못했습니다. 외부 오류 화면은 실행하지 않았습니다.');
  }
  return raw;
};

function v153Name(value) { return cleanText(value || '').replace(/\s/g,''); }
async function v153SameRevision(spec) {
  // Recover only an exact named, dated revision. Never silently fall back to current law.
  const date = v152Date(spec.promulgationDate);
  if (!spec.name || !/^\d{8}$/.test(date)) return null;
  const oc = cleanText(state.secrets?.lawOc || '');
  if (!oc) return null;
  const admin = spec.kind === '행정규칙', found = new Map();
  for (let page=1; page<=3; page+=1) {
    const payload = parsePayload(await fetchText(apiUrl(LAW_URL, {
      OC:oc,target:admin?'admrul':'law',type:'JSON',query:spec.name,search:1,nw:'1,2,3',display:100,page
    }),20000));
    const rows = candidateRecords(payload,['법령명한글','법령명','행정규칙명']);
    for (const r of rows) {
      if (v153Name(recordName(r)) !== v153Name(spec.name) || v152Date(first(r,'공포일자','발령일자')) !== date) continue;
      const lawId = v151Number(first(r,'법령ID'));
      if (!admin && spec.lawId && !v152EqualId(spec.lawId,lawId)) continue;
      const seq = v151Number(first(r,admin?'행정규칙일련번호':'법령일련번호','MST'));
      if (seq) found.set(seq,{...spec,lawMst:admin?'':seq,lawId:admin?'':lawId,adminId:admin?seq:'',promulgationDate:date});
    }
    if (rows.length < 100) break;
    if (page === 3) return null; // Incomplete enumeration is not an unambiguous match.
  }
  if (found.size !== 1) return null;
  const next = [...found.values()][0];
  if (v152EqualId(admin?next.adminId:next.lawMst,admin?spec.adminId:spec.lawMst)) return null;
  return next;
}

const v153PriorOpenReader = v152OpenReader;
v152OpenReader = async function(spec) {
  const started=v152Generation+1;
  await v153PriorOpenReader(spec);
  if (started !== v152Generation) return;
  const dialog = $('#jlm-reader');
  if (!dialog?.open) return;
  let generation = v152Generation;
  let status = $('#jlm-reader-status');
  if (!v152Document && /JLM-153-BASIC/.test(status.textContent)) {
    try {
      const repaired = await v153SameRevision(spec);
      if (generation !== v152Generation || !dialog.open) return;
      if (repaired) {
        generation=v152Generation+1;
        await v153PriorOpenReader(repaired);
        if (generation !== v152Generation || !dialog.open) return;
        if (v152Document) {
          const note=document.createElement('p'); note.className='jlm-note';
          note.textContent='같은 법령명·공포일의 이력 번호를 다시 확인했습니다. 저장 자료는 변경하지 않았습니다.';
          $('#jlm-reader-status').appendChild(note);
        }
      }
    } catch (_) { /* Preserve the initial error; do not substitute a different law. */ }
  }
  if (generation !== v152Generation || !dialog.open) return;
  status = $('#jlm-reader-status');
  const old = dialog.querySelector('[data-jlm-diagnostic]'); if (old) old.remove();
  if (!v152Document) {
    const detail=document.createElement('details'); detail.dataset.jlmDiagnostic='1'; detail.open=true;
    const title=document.createElement('summary'); title.textContent='연결 진단 정보 (인증값 제외)';
    const text=document.createElement('p'); text.className='jlm-note';
    const s=v152SelectedSpec || spec;
    text.textContent=`실행 v${V153_VERSION} / 앱 내부 열람 / ${s.kind || '-'} / ${s.name || '법령명 미확인'} / MST ${s.lawMst || '-'} / ID ${s.adminId || s.lawId || '-'} / 공포일 ${s.promulgationDate || '-'} / 시행일 ${s.enforcementDate || '-'}`;
    detail.append(title,text);status.appendChild(detail);
  }
};

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    const registration=await navigator.serviceWorker.register('./sw.js',{scope:'./',updateViaCache:'none'});
    void registration.update().catch(()=>{});
  } catch (_) { /* The network app remains usable even when offline caching is unavailable. */ }
}

function v153RecoveryUrl() { return new URL('./recover-v1.5.3.html',window.location.href).href; }
function v14InstallUi() {
  const label='모바일 원문 v1.5.3 · 앱 내부 열람';
  const managed=$('#page-managed .page-title p'); if(managed) managed.textContent=label;
  const changes=$('#page-changes .page-title p'); if(changes) changes.textContent='선택한 공포 이력의 본문·부칙·별표를 앱 안에서 확인합니다.';
  const settings=$('#page-settings .page-title p'); if(settings) settings.textContent=label+' · 저장 자료 유지';
  for (const gate of ['#login-view .gate-card','#setup-view .gate-card']) {
    const box=$(gate); if(box && !box.querySelector('[data-jlm-build]')) {
      const p=document.createElement('p');p.dataset.jlmBuild='1';p.className='muted';p.textContent=label;box.appendChild(p);
    }
  }
  if (!$('#jlm-recovery-card')) {
    const card=document.createElement('div');card.id='jlm-recovery-card';card.className='card';
    const title=document.createElement('h3');title.textContent='실행 버전 및 연결 복구';
    const p=document.createElement('p');p.textContent='v1.5.3 통합 실행본입니다. 복구는 실행 화면만 다시 열며 관리대상·개정이력·API 설정을 삭제하지 않습니다.';
    const button=document.createElement('button');button.type='button';button.textContent='데이터 유지하고 최신 화면 열기';
    button.addEventListener('click',()=>window.location.assign(v153RecoveryUrl()));card.append(title,p,button);
    $('#page-settings')?.appendChild(card);
  }
  if (!document.documentElement.dataset.jlmGuard) {
    document.documentElement.dataset.jlmGuard='1';
    document.addEventListener('click',(event)=>{
      const element=event.target?.closest?.('[data-open],a[href]');
      if (!element || element.hasAttribute('data-jlm-external')) return;
      const value=element.dataset.open || element.getAttribute('href');
      let spec;try{spec=v152SpecFromUrl(value);}catch(_){return;}
      if (!spec) return; // Files, notice pages and unrelated links are not rewritten.
      event.preventDefault();event.stopImmediatePropagation();void v152OpenReader(spec);
    },true);
  }
}

boot();
})();
