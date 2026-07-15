from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "정원이앤씨 법령·입법예고 모니터 Link Lite v11.1"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JungwonLawMonitor"
DB_PATH = APP_DIR / "database" / "law_monitor_link_lite.db"
SETTINGS_PATH = APP_DIR / "link_lite_settings.json"

DEFAULTS = {
    "law_oc": "jungwonenc",
    "notice_oc": "jungwonenc",
    "law_search_url": "https://www.law.go.kr/DRF/lawSearch.do",
    "notice_url": "https://www.lawmaking.go.kr/rest/ogLmPp.xml",
    "days": 45,
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum() or "가" <= ch <= "힣")


def load_settings() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            out = DEFAULTS.copy(); out.update(data); return out
        except Exception:
            pass
    return DEFAULTS.copy()


def save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Database:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        return c

    def _columns(self, conn, table: str) -> set[str]:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}

    def initialize(self):
        with self.connect() as conn:
            # Tables first. This avoids the v11 bug where an index referenced notice_status too early.
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS managed_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT '법령',
                name TEXT NOT NULL,
                source_id TEXT DEFAULT '',
                official_url TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(kind, name)
            );
            CREATE TABLE IF NOT EXISTS changes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                revision_type TEXT DEFAULT '',
                promulgation_date TEXT DEFAULT '',
                enforcement_date TEXT DEFAULT '',
                ministry TEXT DEFAULT '',
                official_url TEXT DEFAULT '',
                detected_at TEXT NOT NULL,
                is_new INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS notices(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                ministry TEXT DEFAULT '',
                notice_no TEXT DEFAULT '',
                start_date TEXT DEFAULT '',
                end_date TEXT DEFAULT '',
                notice_status TEXT NOT NULL DEFAULT '미확인',
                official_url TEXT DEFAULT '',
                matched_item TEXT DEFAULT '',
                detected_at TEXT NOT NULL,
                is_new INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sync_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT DEFAULT '',
                checked_at TEXT NOT NULL
            );
            ''')
            # Automatic repair for incomplete v11 DBs.
            if "notice_status" not in self._columns(conn, "notices"):
                conn.execute("ALTER TABLE notices ADD COLUMN notice_status TEXT NOT NULL DEFAULT '미확인'")
            if "matched_item" not in self._columns(conn, "notices"):
                conn.execute("ALTER TABLE notices ADD COLUMN matched_item TEXT DEFAULT ''")
            if "is_new" not in self._columns(conn, "notices"):
                conn.execute("ALTER TABLE notices ADD COLUMN is_new INTEGER NOT NULL DEFAULT 1")
            conn.executescript('''
            CREATE INDEX IF NOT EXISTS idx_changes_date ON changes(promulgation_date DESC);
            CREATE INDEX IF NOT EXISTS idx_notices_status ON notices(notice_status, end_date DESC);
            CREATE INDEX IF NOT EXISTS idx_notices_dates ON notices(start_date DESC, end_date DESC);
            ''')

    def items(self):
        with self.connect() as c:
            return c.execute("SELECT * FROM managed_items ORDER BY kind, name").fetchall()

    def add_item(self, kind, name, source_id="", official_url=""):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO managed_items(kind,name,source_id,official_url,created_at) VALUES(?,?,?,?,?)",
                      (kind, name.strip(), source_id.strip(), official_url.strip(), now_text()))

    def delete_item(self, item_id):
        with self.connect() as c: c.execute("DELETE FROM managed_items WHERE id=?", (item_id,))

    def upsert_change(self, d):
        with self.connect() as c:
            c.execute('''INSERT INTO changes(source_key,kind,name,revision_type,promulgation_date,enforcement_date,ministry,official_url,detected_at,is_new)
            VALUES(?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(source_key) DO UPDATE SET kind=excluded.kind,name=excluded.name,revision_type=excluded.revision_type,
            promulgation_date=excluded.promulgation_date,enforcement_date=excluded.enforcement_date,ministry=excluded.ministry,
            official_url=excluded.official_url''',
            (d['source_key'],d['kind'],d['name'],d.get('revision_type',''),d.get('promulgation_date',''),d.get('enforcement_date',''),d.get('ministry',''),d.get('official_url',''),now_text()))

    def upsert_notice(self, d):
        with self.connect() as c:
            c.execute('''INSERT INTO notices(source_key,title,ministry,notice_no,start_date,end_date,notice_status,official_url,matched_item,detected_at,is_new)
            VALUES(?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(source_key) DO UPDATE SET title=excluded.title,ministry=excluded.ministry,notice_no=excluded.notice_no,
            start_date=excluded.start_date,end_date=excluded.end_date,notice_status=excluded.notice_status,
            official_url=excluded.official_url,matched_item=excluded.matched_item''',
            (d['source_key'],d['title'],d.get('ministry',''),d.get('notice_no',''),d.get('start_date',''),d.get('end_date',''),d.get('notice_status','미확인'),d.get('official_url',''),d.get('matched_item',''),now_text()))

    def changes(self):
        with self.connect() as c: return c.execute("SELECT * FROM changes ORDER BY promulgation_date DESC, id DESC").fetchall()
    def notices(self):
        with self.connect() as c: return c.execute("SELECT * FROM notices ORDER BY start_date DESC, id DESC").fetchall()
    def mark_seen(self, table):
        with self.connect() as c: c.execute(f"UPDATE {table} SET is_new=0")
    def log(self, category,status,message):
        with self.connect() as c: c.execute("INSERT INTO sync_log(category,status,message,checked_at) VALUES(?,?,?,?)",(category,status,message,now_text()))


def fetch(url: str, params: dict, timeout=20) -> bytes:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + qs, headers={"User-Agent":"JungwonLawMonitor/11.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def text_of(node, *names):
    for name in names:
        v = node.findtext(name)
        if v and v.strip(): return v.strip()
    return ""


def parse_any(data: bytes):
    s = data.lstrip()
    if s.startswith(b"{") or s.startswith(b"["):
        return json.loads(data.decode("utf-8", errors="replace"))
    return ET.fromstring(data)


def flatten_json(obj):
    if isinstance(obj, list):
        for x in obj: yield from flatten_json(x)
    elif isinstance(obj, dict):
        keys = {k.lower() for k in obj}
        if any(k in keys for k in ("법령명한글","법령명","법령명_한글","title","입법예고명")):
            yield obj
        for v in obj.values(): yield from flatten_json(v)


def val(d, *names):
    for n in names:
        for k,v in d.items():
            if k.lower() == n.lower() and v not in (None,""): return str(v).strip()
    return ""


def official_law_url(kind, source_id, fallback=""):
    if fallback.startswith("http"): return fallback
    base = "https://www.law.go.kr/"
    if source_id:
        return base + ("행정규칙/" if kind == "행정규칙" else "법령/") + urllib.parse.quote(source_id)
    return base


class Monitor:
    def __init__(self, db: Database, settings: dict): self.db, self.s = db, settings

    def sync(self, progress=lambda x:None):
        errors=[]
        for kind,target in (("법령","law"),("행정규칙","admrul")):
            try:
                progress(f"{kind} 변경목록 확인 중...")
                self.sync_laws(kind,target)
                self.db.log(kind,"성공","")
            except Exception as e:
                errors.append(f"{kind}: {e}"); self.db.log(kind,"실패",str(e))
        try:
            progress("입법예고 확인 중...")
            self.sync_notices(); self.db.log("입법예고","성공","")
        except Exception as e:
            errors.append(f"입법예고: {e}"); self.db.log("입법예고","실패",str(e))
        return errors

    def sync_laws(self, kind, target):
        items=[r for r in self.db.items() if r['enabled'] and r['kind']==kind]
        if not items: return
        data=fetch(self.s['law_search_url'], {"OC":self.s['law_oc'],"target":target,"type":"JSON","display":100,"page":1,"sort":"ddes"})
        obj=parse_any(data)
        rows=list(flatten_json(obj)) if isinstance(obj,(dict,list)) else []
        for row in rows:
            name=val(row,"법령명한글","법령명","법령명_한글","행정규칙명","title")
            if not name: continue
            matched=next((i for i in items if norm(i['name']) in norm(name) or norm(name) in norm(i['name'])),None)
            if not matched: continue
            sid=val(row,"법령ID","행정규칙ID","id","법령일련번호","행정규칙일련번호") or matched['source_id']
            pdate=val(row,"공포일자","발령일자","개정일자","promulgationDate")
            edate=val(row,"시행일자","enforcementDate")
            rtype=val(row,"제개정구분명","제개정구분","개정구분")
            ministry=val(row,"소관부처명","소관부처","부처명")
            link=val(row,"법령상세링크","행정규칙상세링크","link","url")
            key=f"{kind}:{sid or name}:{pdate}:{rtype}"
            self.db.upsert_change({"source_key":key,"kind":kind,"name":name,"revision_type":rtype,"promulgation_date":pdate,"enforcement_date":edate,"ministry":ministry,"official_url":official_law_url(kind,sid,link)})

    def sync_notices(self):
        items=[r for r in self.db.items() if r['enabled']]
        data=fetch(self.s['notice_url'], {"OC":self.s['notice_oc'],"diff":0})
        root=parse_any(data)
        nodes=[]
        if isinstance(root,ET.Element):
            for n in root.iter():
                if len(list(n)) >= 3: nodes.append(n)
        elif isinstance(root,(dict,list)):
            nodes=list(flatten_json(root))
        today=datetime.now().date()
        for n in nodes:
            if isinstance(n,ET.Element):
                title=text_of(n,"법령안명","입법예고명","제목","title","lmPpNm")
                ministry=text_of(n,"소관부처","부처명","deptNm","소관부처명")
                no=text_of(n,"공고번호","announceNo","공고번호명")
                start=text_of(n,"공고일자","시작일자","announceStartDt","예고시작일")
                end=text_of(n,"마감일자","종료일자","announceEndDt","예고종료일")
                link=text_of(n,"상세링크","link","url","상세페이지")
                sid=text_of(n,"입법예고ID","lmPpSeq","id")
            else:
                title=val(n,"법령안명","입법예고명","제목","title","lmPpNm")
                ministry=val(n,"소관부처","부처명","deptNm","소관부처명")
                no=val(n,"공고번호","announceNo")
                start=val(n,"공고일자","시작일자","announceStartDt","예고시작일")
                end=val(n,"마감일자","종료일자","announceEndDt","예고종료일")
                link=val(n,"상세링크","link","url")
                sid=val(n,"입법예고ID","lmPpSeq","id")
            if not title: continue
            matched=next((i for i in items if norm(i['name']) in norm(title) or norm(title) in norm(i['name'])),None)
            if not matched: continue
            status="진행 중"
            try:
                if end and datetime.strptime(end[:10].replace('.','-'),"%Y-%m-%d").date() < today: status="종료"
            except Exception: pass
            if not link.startswith("http"):
                link="https://www.lawmaking.go.kr/lmSts/ogLmPp" + (("?lmPpSeq="+urllib.parse.quote(sid)) if sid else "")
            key=f"notice:{sid or no or title}:{start}"
            self.db.upsert_notice({"source_key":key,"title":title,"ministry":ministry,"notice_no":no,"start_date":start,"end_date":end,"notice_status":status,"official_url":link,"matched_item":matched['name']})


class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(APP_NAME); self.geometry("1180x720"); self.minsize(980,620)
        self.db=Database(); self.settings=load_settings(); self.monitor=Monitor(self.db,self.settings)
        self.status=tk.StringVar(value="준비")
        self._build(); self.refresh_all(); self.after(500,self.sync_async)

    def _build(self):
        bar=ttk.Frame(self,padding=8); bar.pack(fill='x')
        ttk.Label(bar,text=APP_NAME,font=("맑은 고딕",15,"bold")).pack(side='left')
        ttk.Button(bar,text="지금 확인",command=self.sync_async).pack(side='right')
        self.nb=ttk.Notebook(self); self.nb.pack(fill='both',expand=True,padx=8,pady=4)
        self.tabs={}
        for name in ("대시보드","관리대상","개정사항","입법예고","설정"):
            f=ttk.Frame(self.nb,padding=8); self.nb.add(f,text=name); self.tabs[name]=f
        self._dashboard(); self._managed(); self._changes(); self._notices(); self._settings()
        ttk.Label(self,textvariable=self.status,anchor='w',relief='sunken').pack(fill='x',side='bottom')

    def _tree(self,parent,cols):
        t=ttk.Treeview(parent,columns=[c[0] for c in cols],show='headings')
        for key,title,width in cols: t.heading(key,text=title); t.column(key,width=width,anchor='w')
        y=ttk.Scrollbar(parent,orient='vertical',command=t.yview); t.configure(yscrollcommand=y.set)
        t.pack(side='left',fill='both',expand=True); y.pack(side='right',fill='y'); return t

    def _dashboard(self):
        f=self.tabs['대시보드']; self.dash=tk.StringVar(); ttk.Label(f,textvariable=self.dash,font=("맑은 고딕",14)).pack(anchor='w',pady=20)
        ttk.Label(f,text="전체 본문·별표·첨부파일은 저장하지 않습니다. 목록을 더블클릭하면 공식 사이트를 엽니다.",font=("맑은 고딕",11)).pack(anchor='w')

    def _managed(self):
        f=self.tabs['관리대상']; top=ttk.Frame(f); top.pack(fill='x',pady=(0,8))
        self.kind=tk.StringVar(value='법령'); self.item_name=tk.StringVar(); self.item_id=tk.StringVar()
        ttk.Combobox(top,textvariable=self.kind,values=['법령','행정규칙'],width=10,state='readonly').pack(side='left')
        ttk.Entry(top,textvariable=self.item_name,width=45).pack(side='left',padx=4)
        ttk.Entry(top,textvariable=self.item_id,width=18).pack(side='left',padx=4)
        ttk.Button(top,text='추가',command=self.add_item).pack(side='left'); ttk.Button(top,text='삭제',command=self.del_item).pack(side='left',padx=4)
        holder=ttk.Frame(f); holder.pack(fill='both',expand=True)
        self.item_tree=self._tree(holder,[('id','번호',60),('kind','구분',100),('name','관리대상명',520),('source','ID',180)])

    def _changes(self):
        f=self.tabs['개정사항']; ttk.Label(f,text='더블클릭: 국가법령정보센터 원문 열기').pack(anchor='w')
        holder=ttk.Frame(f); holder.pack(fill='both',expand=True,pady=6)
        self.change_tree=self._tree(holder,[('new','신규',55),('kind','구분',90),('name','명칭',380),('rtype','개정유형',120),('pdate','공포·발령일',110),('edate','시행일',110),('ministry','소관부처',160)])
        self.change_tree.bind('<Double-1>',lambda e:self.open_selected(self.change_tree,'changes'))

    def _notices(self):
        f=self.tabs['입법예고']; ttk.Label(f,text='더블클릭: 국민참여입법센터 공식 상세페이지 열기').pack(anchor='w')
        holder=ttk.Frame(f); holder.pack(fill='both',expand=True,pady=6)
        self.notice_tree=self._tree(holder,[('new','신규',55),('status','상태',85),('title','입법예고명',410),('matched','관련 관리대상',250),('start','시작일',100),('end','종료일',100),('ministry','소관부처',140)])
        self.notice_tree.bind('<Double-1>',lambda e:self.open_selected(self.notice_tree,'notices'))

    def _settings(self):
        f=self.tabs['설정']; self.vars={}
        rows=[('law_oc','국가법령정보 API OC'),('notice_oc','입법예고 API OC'),('law_search_url','법령·행정규칙 검색 API'),('notice_url','입법예고 목록 API'),('days','조회 기준일수')]
        for i,(k,label) in enumerate(rows):
            ttk.Label(f,text=label).grid(row=i,column=0,sticky='w',pady=6); v=tk.StringVar(value=str(self.settings.get(k,''))); self.vars[k]=v; ttk.Entry(f,textvariable=v,width=85).grid(row=i,column=1,sticky='ew',padx=8)
        f.columnconfigure(1,weight=1); ttk.Button(f,text='설정 저장',command=self.save_settings_ui).grid(row=len(rows),column=1,sticky='e',pady=12)

    def add_item(self):
        if not self.item_name.get().strip(): return
        self.db.add_item(self.kind.get(),self.item_name.get(),self.item_id.get()); self.item_name.set(''); self.item_id.set(''); self.refresh_items()
    def del_item(self):
        sel=self.item_tree.selection();
        if sel: self.db.delete_item(int(self.item_tree.item(sel[0],'values')[0])); self.refresh_items()
    def save_settings_ui(self):
        for k,v in self.vars.items(): self.settings[k]=int(v.get()) if k=='days' and v.get().isdigit() else v.get().strip()
        save_settings(self.settings); self.monitor=Monitor(self.db,self.settings); messagebox.showinfo('저장','설정을 저장했습니다.')
    def sync_async(self):
        self.status.set('API 확인 시작...')
        def run():
            errs=self.monitor.sync(lambda s:self.after(0,lambda:self.status.set(s)))
            self.after(0,lambda:self.sync_done(errs))
        threading.Thread(target=run,daemon=True).start()
    def sync_done(self,errs):
        self.refresh_all(); self.status.set('점검 완료: '+now_text())
        if errs: messagebox.showwarning('일부 조회 실패','\n'.join(errs))
    def refresh_all(self):
        self.refresh_items(); self.refresh_changes(); self.refresh_notices()
        self.dash.set(f"관리대상 {len(self.db.items())}건   |   개정사항 {len(self.db.changes())}건   |   입법예고 {len(self.db.notices())}건")
    def refresh_items(self):
        for x in self.item_tree.get_children(): self.item_tree.delete(x)
        for r in self.db.items(): self.item_tree.insert('', 'end', values=(r['id'],r['kind'],r['name'],r['source_id']))
    def refresh_changes(self):
        self.change_rows={};
        for x in self.change_tree.get_children(): self.change_tree.delete(x)
        for r in self.db.changes():
            iid=self.change_tree.insert('', 'end', values=('●' if r['is_new'] else '',r['kind'],r['name'],r['revision_type'],r['promulgation_date'],r['enforcement_date'],r['ministry'])); self.change_rows[iid]=r['official_url']
        self.db.mark_seen('changes')
    def refresh_notices(self):
        self.notice_rows={};
        for x in self.notice_tree.get_children(): self.notice_tree.delete(x)
        for r in self.db.notices():
            iid=self.notice_tree.insert('', 'end', values=('●' if r['is_new'] else '',r['notice_status'],r['title'],r['matched_item'],r['start_date'],r['end_date'],r['ministry'])); self.notice_rows[iid]=r['official_url']
        self.db.mark_seen('notices')
    def open_selected(self,tree,which):
        sel=tree.selection();
        if not sel: return
        url=(self.change_rows if which=='changes' else self.notice_rows).get(sel[0],'')
        if url: webbrowser.open(url)

if __name__ == '__main__':
    App().mainloop()
