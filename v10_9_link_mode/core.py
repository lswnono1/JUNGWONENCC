from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

APP_TITLE = "정원이앤씨 법령·입법예고 모니터 v10.9 Link Mode"
APP_VERSION = "10.9 Link Mode"
USER_AGENT = "JungwonLawMonitor/10.9-LinkMode"

DEFAULT_SETTINGS: dict[str, Any] = {
    "law_oc": "jungwonenc",
    "notice_oc": "jungwonenc",
    "law_search_url": "https://www.law.go.kr/DRF/lawSearch.do",
    "notice_url": "https://www.lawmaking.go.kr/rest/ogLmPp.xml",
    "request_timeout": 30,
    "closed_notice_days": 45,
    "startup_check": True,
    "startup_delay_seconds": 5,
    "company_name": "정원이앤씨",
    "reviewer": "정원이앤씨",
}


def app_root() -> Path:
    override = os.environ.get("JLM_DATA_ROOT", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "JungwonLawMonitor"


def database_dir() -> Path:
    return app_root() / "database"


def database_path() -> Path:
    return database_dir() / "law_monitor_v10_9_link_mode.db"


def settings_path() -> Path:
    return app_root() / "v10_9_link_mode_settings.json"


def log_dir() -> Path:
    return app_root() / "logs"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_text(value: Any) -> str:
    text = str(value or "").lower().strip()
    return "".join(ch for ch in text if ch.isalnum() or "가" <= ch <= "힣")


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10]


def parse_date(value: Any) -> date | None:
    normalized = normalize_date(value)
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except Exception:
        return None


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def bool_value(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off", "중지", "미사용"}


def load_settings() -> dict[str, Any]:
    root = app_root()
    root.mkdir(parents=True, exist_ok=True)
    result = DEFAULT_SETTINGS.copy()
    path = settings_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result.update(loaded)
                return result
        except Exception:
            pass

    # 기존 v10.9 설정 중 확인 가능한 값만 읽고, 원본 파일은 수정하지 않는다.
    candidates = [
        root / "settings.json",
        root / "config.json",
        root / "app_settings.json",
        root / "law_monitor_settings.json",
    ]
    aliases = {
        "law_oc": ("law_oc", "law_api_oc", "oc", "law_api_key"),
        "notice_oc": ("notice_oc", "notice_api_oc", "lawmaking_oc"),
        "company_name": ("company_name", "company"),
        "reviewer": ("reviewer", "author", "manager"),
    }
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            lowered = {str(k).lower(): v for k, v in raw.items()}
            for destination, names in aliases.items():
                for name in names:
                    if name.lower() in lowered and str(lowered[name.lower()]).strip():
                        result[destination] = lowered[name.lower()]
                        break
        except Exception:
            continue
    save_settings(result)
    return result


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = DEFAULT_SETTINGS.copy()
    clean.update(settings)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})")}

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, specs: dict[str, str]) -> None:
        columns = self._columns(conn, table)
        for name, sql_type in specs.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE {quote_identifier(table)} "
                    f"ADD COLUMN {quote_identifier(name)} {sql_type}"
                )

    def initialize(self) -> None:
        with self.connect() as conn:
            # 테이블을 먼저 만들고, 열 보완 후 마지막에 인덱스를 만든다.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS managed_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT '법령',
                    name TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    official_url TEXT NOT NULL DEFAULT '',
                    last_event_key TEXT NOT NULL DEFAULT '',
                    last_revision_type TEXT NOT NULL DEFAULT '',
                    last_revision_date TEXT NOT NULL DEFAULT '',
                    last_enforcement_date TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    check_status TEXT NOT NULL DEFAULT '미확인',
                    import_source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS change_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL DEFAULT '',
                    managed_item_id INTEGER,
                    kind TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    revision_type TEXT NOT NULL DEFAULT '',
                    promulgation_date TEXT NOT NULL DEFAULT '',
                    enforcement_date TEXT NOT NULL DEFAULT '',
                    ministry TEXT NOT NULL DEFAULT '',
                    official_url TEXT NOT NULL DEFAULT '',
                    detected_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    is_new INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(managed_item_id) REFERENCES managed_items(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS legislative_notices(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_key TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    ministry TEXT NOT NULL DEFAULT '',
                    notice_no TEXT NOT NULL DEFAULT '',
                    start_date TEXT NOT NULL DEFAULT '',
                    end_date TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '미확인',
                    official_url TEXT NOT NULL DEFAULT '',
                    matched_item TEXT NOT NULL DEFAULT '',
                    detected_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    is_new INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS sync_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    checked_at TEXT NOT NULL DEFAULT ''
                );
                """
            )

            self._ensure_columns(
                conn,
                "managed_items",
                {
                    "kind": "TEXT NOT NULL DEFAULT '법령'",
                    "name": "TEXT NOT NULL DEFAULT ''",
                    "source_id": "TEXT NOT NULL DEFAULT ''",
                    "enabled": "INTEGER NOT NULL DEFAULT 1",
                    "official_url": "TEXT NOT NULL DEFAULT ''",
                    "last_event_key": "TEXT NOT NULL DEFAULT ''",
                    "last_revision_type": "TEXT NOT NULL DEFAULT ''",
                    "last_revision_date": "TEXT NOT NULL DEFAULT ''",
                    "last_enforcement_date": "TEXT NOT NULL DEFAULT ''",
                    "last_checked_at": "TEXT NOT NULL DEFAULT ''",
                    "check_status": "TEXT NOT NULL DEFAULT '미확인'",
                    "import_source": "TEXT NOT NULL DEFAULT ''",
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                    "updated_at": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                conn,
                "change_events",
                {
                    "event_key": "TEXT NOT NULL DEFAULT ''",
                    "managed_item_id": "INTEGER",
                    "kind": "TEXT NOT NULL DEFAULT ''",
                    "name": "TEXT NOT NULL DEFAULT ''",
                    "source_id": "TEXT NOT NULL DEFAULT ''",
                    "revision_type": "TEXT NOT NULL DEFAULT ''",
                    "promulgation_date": "TEXT NOT NULL DEFAULT ''",
                    "enforcement_date": "TEXT NOT NULL DEFAULT ''",
                    "ministry": "TEXT NOT NULL DEFAULT ''",
                    "official_url": "TEXT NOT NULL DEFAULT ''",
                    "detected_at": "TEXT NOT NULL DEFAULT ''",
                    "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                    "is_new": "INTEGER NOT NULL DEFAULT 1",
                },
            )
            self._ensure_columns(
                conn,
                "legislative_notices",
                {
                    "notice_key": "TEXT NOT NULL DEFAULT ''",
                    "title": "TEXT NOT NULL DEFAULT ''",
                    "ministry": "TEXT NOT NULL DEFAULT ''",
                    "notice_no": "TEXT NOT NULL DEFAULT ''",
                    "start_date": "TEXT NOT NULL DEFAULT ''",
                    "end_date": "TEXT NOT NULL DEFAULT ''",
                    "status": "TEXT NOT NULL DEFAULT '미확인'",
                    "official_url": "TEXT NOT NULL DEFAULT ''",
                    "matched_item": "TEXT NOT NULL DEFAULT ''",
                    "detected_at": "TEXT NOT NULL DEFAULT ''",
                    "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                    "is_new": "INTEGER NOT NULL DEFAULT 1",
                },
            )

            # 과거 시험판의 notice_status 열이 있으면 status로 안전하게 옮긴다.
            notice_columns = self._columns(conn, "legislative_notices")
            if "notice_status" in notice_columns:
                conn.execute(
                    "UPDATE legislative_notices SET status=notice_status "
                    "WHERE (status='' OR status='미확인') AND notice_status<>''"
                )

            conn.execute(
                "UPDATE managed_items SET created_at=? WHERE created_at=''",
                (now_text(),),
            )
            conn.execute(
                "UPDATE managed_items SET updated_at=created_at WHERE updated_at=''"
            )
            conn.execute(
                "UPDATE change_events SET event_key='legacy-change:' || id "
                "WHERE event_key='' OR event_key IS NULL"
            )
            conn.execute(
                "UPDATE legislative_notices SET notice_key='legacy-notice:' || id "
                "WHERE notice_key='' OR notice_key IS NULL"
            )

            conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_managed_kind_name
                    ON managed_items(kind, name);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_change_event_key
                    ON change_events(event_key);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_notice_key
                    ON legislative_notices(notice_key);
                CREATE INDEX IF NOT EXISTS ix_change_dates
                    ON change_events(promulgation_date DESC, id DESC);
                CREATE INDEX IF NOT EXISTS ix_notice_dates
                    ON legislative_notices(start_date DESC, end_date DESC, id DESC);
                CREATE INDEX IF NOT EXISTS ix_notice_status
                    ON legislative_notices(status, end_date DESC);
                CREATE INDEX IF NOT EXISTS ix_sync_log_date
                    ON sync_log(checked_at DESC, id DESC);
                """
            )
            self.set_meta("schema_version", "3", conn=conn)

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_meta(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        owns_connection = conn is None
        target = conn or self.connect()
        try:
            target.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            if owns_connection:
                target.commit()
        finally:
            if owns_connection:
                target.close()

    def add_item(
        self,
        kind: str,
        name: str,
        source_id: str = "",
        official_url: str = "",
        enabled: bool = True,
        import_source: str = "",
    ) -> int:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("관리대상명을 입력하세요.")
        clean_kind = "행정규칙" if "행정" in str(kind) else "법령"
        timestamp = now_text()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO managed_items(
                    kind,name,source_id,enabled,official_url,import_source,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(kind,name) DO UPDATE SET
                    source_id=CASE WHEN excluded.source_id<>'' THEN excluded.source_id ELSE managed_items.source_id END,
                    official_url=CASE WHEN excluded.official_url<>'' THEN excluded.official_url ELSE managed_items.official_url END,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    clean_kind,
                    clean_name,
                    str(source_id or "").strip(),
                    1 if enabled else 0,
                    str(official_url or "").strip(),
                    str(import_source or "").strip(),
                    timestamp,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT id FROM managed_items WHERE kind=? AND name=?",
                (clean_kind, clean_name),
            ).fetchone()
            return int(row[0])

    def delete_item(self, item_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM managed_items WHERE id=?", (int(item_id),))

    def set_item_enabled(self, item_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE managed_items SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, now_text(), int(item_id)),
            )

    def update_item_check(
        self,
        item_id: int,
        *,
        event_key: str,
        revision_type: str,
        revision_date: str,
        enforcement_date: str,
        source_id: str,
        official_url: str,
        status: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE managed_items SET
                    last_event_key=?, last_revision_type=?, last_revision_date=?,
                    last_enforcement_date=?, source_id=CASE WHEN ?<>'' THEN ? ELSE source_id END,
                    official_url=CASE WHEN ?<>'' THEN ? ELSE official_url END,
                    last_checked_at=?, check_status=?, updated_at=?
                WHERE id=?
                """,
                (
                    event_key,
                    revision_type,
                    revision_date,
                    enforcement_date,
                    source_id,
                    source_id,
                    official_url,
                    official_url,
                    now_text(),
                    status,
                    now_text(),
                    int(item_id),
                ),
            )

    def update_item_failure(self, item_id: int, message: str) -> None:
        compact = str(message or "").replace("\n", " ")[:250]
        with self.connect() as conn:
            conn.execute(
                "UPDATE managed_items SET last_checked_at=?, check_status=?, updated_at=? WHERE id=?",
                (now_text(), f"실패: {compact}", now_text(), int(item_id)),
            )

    def items(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM managed_items"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY kind, name"
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def item(self, item_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM managed_items WHERE id=?", (int(item_id),)).fetchone()

    def upsert_change(self, data: dict[str, Any], is_new: bool) -> bool:
        timestamp = now_text()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM change_events WHERE event_key=?",
                (data["event_key"],),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE change_events SET
                        managed_item_id=?,kind=?,name=?,source_id=?,revision_type=?,
                        promulgation_date=?,enforcement_date=?,ministry=?,official_url=?,last_seen_at=?
                    WHERE event_key=?
                    """,
                    (
                        data.get("managed_item_id"),
                        data.get("kind", ""),
                        data.get("name", ""),
                        data.get("source_id", ""),
                        data.get("revision_type", ""),
                        data.get("promulgation_date", ""),
                        data.get("enforcement_date", ""),
                        data.get("ministry", ""),
                        data.get("official_url", ""),
                        timestamp,
                        data["event_key"],
                    ),
                )
                return False
            conn.execute(
                """
                INSERT INTO change_events(
                    event_key,managed_item_id,kind,name,source_id,revision_type,
                    promulgation_date,enforcement_date,ministry,official_url,
                    detected_at,last_seen_at,is_new
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data["event_key"],
                    data.get("managed_item_id"),
                    data.get("kind", ""),
                    data.get("name", ""),
                    data.get("source_id", ""),
                    data.get("revision_type", ""),
                    data.get("promulgation_date", ""),
                    data.get("enforcement_date", ""),
                    data.get("ministry", ""),
                    data.get("official_url", ""),
                    timestamp,
                    timestamp,
                    1 if is_new else 0,
                ),
            )
            return True

    def upsert_notice(self, data: dict[str, Any], is_new: bool) -> bool:
        timestamp = now_text()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM legislative_notices WHERE notice_key=?",
                (data["notice_key"],),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE legislative_notices SET
                        title=?,ministry=?,notice_no=?,start_date=?,end_date=?,status=?,
                        official_url=?,matched_item=?,last_seen_at=?
                    WHERE notice_key=?
                    """,
                    (
                        data.get("title", ""),
                        data.get("ministry", ""),
                        data.get("notice_no", ""),
                        data.get("start_date", ""),
                        data.get("end_date", ""),
                        data.get("status", "미확인"),
                        data.get("official_url", ""),
                        data.get("matched_item", ""),
                        timestamp,
                        data["notice_key"],
                    ),
                )
                return False
            conn.execute(
                """
                INSERT INTO legislative_notices(
                    notice_key,title,ministry,notice_no,start_date,end_date,status,
                    official_url,matched_item,detected_at,last_seen_at,is_new
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data["notice_key"],
                    data.get("title", ""),
                    data.get("ministry", ""),
                    data.get("notice_no", ""),
                    data.get("start_date", ""),
                    data.get("end_date", ""),
                    data.get("status", "미확인"),
                    data.get("official_url", ""),
                    data.get("matched_item", ""),
                    timestamp,
                    timestamp,
                    1 if is_new else 0,
                ),
            )
            return True

    def changes(self, limit: int = 1000) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM change_events ORDER BY promulgation_date DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            )

    def notices(self, limit: int = 2000) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM legislative_notices ORDER BY start_date DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            )

    def logs(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM sync_log ORDER BY checked_at DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            )

    def add_log(self, category: str, status: str, message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sync_log(category,status,message,checked_at) VALUES(?,?,?,?)",
                (category, status, str(message or "")[:2000], now_text()),
            )

    def mark_seen(self, table: str) -> None:
        allowed = {"change_events", "legislative_notices"}
        if table not in allowed:
            raise ValueError("잘못된 테이블입니다.")
        with self.connect() as conn:
            conn.execute(f"UPDATE {table} SET is_new=0 WHERE is_new<>0")

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "managed": int(conn.execute("SELECT COUNT(*) FROM managed_items").fetchone()[0]),
                "enabled": int(conn.execute("SELECT COUNT(*) FROM managed_items WHERE enabled=1").fetchone()[0]),
                "changes": int(conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0]),
                "new_changes": int(conn.execute("SELECT COUNT(*) FROM change_events WHERE is_new=1").fetchone()[0]),
                "notices": int(conn.execute("SELECT COUNT(*) FROM legislative_notices").fetchone()[0]),
                "new_notices": int(conn.execute("SELECT COUNT(*) FROM legislative_notices WHERE is_new=1").fetchone()[0]),
            }

    def import_legacy_managed_items(self, force: bool = False) -> tuple[int, list[str]]:
        if not force and self.get_meta("legacy_import_done", "") == "1":
            return 0, []

        root = app_root()
        candidates: list[Path] = []
        for folder in (root, root / "database", root / "data"):
            if not folder.exists():
                continue
            for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
                candidates.extend(folder.glob(pattern))
        unique_candidates = []
        seen_paths: set[str] = set()
        for path in candidates:
            resolved = str(path.resolve()).lower()
            if resolved == str(self.path.resolve()).lower() or resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            unique_candidates.append(path)

        imported = 0
        sources: list[str] = []
        name_candidates = [
            "name", "display_name", "law_name", "rule_name", "material_name",
            "resource_name", "title", "korean_name", "ls_nm", "법령명",
        ]
        kind_candidates = ["kind", "type", "category", "law_type", "resource_type", "material_type"]
        id_candidates = ["source_id", "law_id", "rule_id", "lid", "mst", "api_id", "serial_no", "법령id"]
        enabled_candidates = ["enabled", "is_active", "active", "monitoring", "selected", "use_yn"]
        url_candidates = ["official_url", "url", "link", "detail_url"]

        for legacy_path in unique_candidates:
            try:
                uri = f"file:{legacy_path.as_posix()}?mode=ro"
                legacy = sqlite3.connect(uri, uri=True, timeout=5)
                legacy.row_factory = sqlite3.Row
            except Exception:
                continue
            file_imported = 0
            try:
                tables = [
                    str(row[0])
                    for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    if not str(row[0]).startswith("sqlite_")
                ]
                for table in tables:
                    table_lower = table.lower()
                    if not any(token in table_lower for token in ("managed", "monitor", "target", "material")):
                        continue
                    columns = [
                        str(row[1])
                        for row in legacy.execute(f"PRAGMA table_info({quote_identifier(table)})")
                    ]
                    lowered = {column.lower(): column for column in columns}

                    def pick(candidates_list: Iterable[str]) -> str | None:
                        for candidate in candidates_list:
                            if candidate.lower() in lowered:
                                return lowered[candidate.lower()]
                        return None

                    name_col = pick(name_candidates)
                    if not name_col:
                        continue
                    kind_col = pick(kind_candidates)
                    id_col = pick(id_candidates)
                    enabled_col = pick(enabled_candidates)
                    url_col = pick(url_candidates)
                    selected = [name_col]
                    for optional in (kind_col, id_col, enabled_col, url_col):
                        if optional and optional not in selected:
                            selected.append(optional)
                    sql = "SELECT " + ",".join(quote_identifier(c) for c in selected)
                    sql += f" FROM {quote_identifier(table)} LIMIT 5000"
                    try:
                        rows = legacy.execute(sql).fetchall()
                    except Exception:
                        continue
                    for row in rows:
                        raw_name = str(row[name_col] or "").strip()
                        if not (2 <= len(raw_name) <= 250):
                            continue
                        raw_kind = str(row[kind_col] or "") if kind_col else ""
                        upper_name = raw_name.upper()
                        kind = "행정규칙" if (
                            "행정" in raw_kind
                            or "RULE" in raw_kind.upper()
                            or any(token in upper_name for token in ("NFPC", "NFTC"))
                            or any(token in raw_name for token in ("고시", "훈령", "예규", "기준"))
                        ) else "법령"
                        source_id = str(row[id_col] or "").strip() if id_col else ""
                        enabled = bool_value(row[enabled_col], True) if enabled_col else True
                        official_url = str(row[url_col] or "").strip() if url_col else ""
                        before = len(self.items())
                        self.add_item(
                            kind,
                            raw_name,
                            source_id,
                            official_url,
                            enabled,
                            import_source=f"{legacy_path.name}:{table}",
                        )
                        after = len(self.items())
                        if after > before:
                            imported += 1
                            file_imported += 1
                if file_imported:
                    sources.append(legacy_path.name)
            finally:
                legacy.close()

        self.set_meta("legacy_import_done", "1")
        self.add_log("기존자료", "성공", f"관리대상 {imported}건 가져오기")
        return imported, sources


class HttpClient:
    def __init__(self, timeout: int = 30):
        self.timeout = max(5, min(int(timeout), 120))

    def get(self, url: str, params: dict[str, Any]) -> bytes:
        query = urllib.parse.urlencode(
            {key: value for key, value in params.items() if value not in (None, "")},
            doseq=True,
        )
        full_url = url + ("&" if "?" in url else "?") + query
        request = urllib.request.Request(
            full_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, application/xml, text/xml, */*",
            },
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in {400, 401, 403, 404}:
                    raise
            except Exception as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
        if last_error:
            raise last_error
        raise RuntimeError("API 응답을 받지 못했습니다.")


def parse_payload(data: bytes) -> Any:
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        for encoding in ("utf-8", "euc-kr", "cp949"):
            try:
                return json.loads(data.decode(encoding))
            except Exception:
                continue
        return json.loads(data.decode("utf-8", errors="replace"))
    return ET.fromstring(data)


def dict_value(mapping: dict[str, Any], *names: str) -> str:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            if isinstance(value, (dict, list)):
                continue
            return str(value).strip()
    return ""


def iter_json_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        lowered = {str(key).lower() for key in value}
        markers = {
            "법령명한글", "법령명", "행정규칙명", "입법예고명", "법령안명",
            "lmp pnm", "title", "lsnm", "법령id", "행정규칙id",
        }
        if lowered.intersection(markers) or any(
            key in lowered for key in ("법령명한글", "행정규칙명", "lmp pnm", "lmp pseq")
        ):
            yield value
        for child in value.values():
            yield from iter_json_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_records(child)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def xml_child_value(node: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(node):
        if local_name(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    for child in node.iter():
        if child is node:
            continue
        if local_name(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def iter_xml_records(root: ET.Element) -> Iterable[ET.Element]:
    title_names = {
        "법령안명", "입법예고명", "제목", "title", "lmpppnm", "lmppnm", "lsnm"
    }
    seen: set[int] = set()
    for node in root.iter():
        child_names = {local_name(child.tag) for child in list(node)}
        if child_names.intersection(title_names) and id(node) not in seen:
            seen.add(id(node))
            yield node


def absolute_url(base: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith(("http://", "https://")):
        return text
    return urllib.parse.urljoin(base, text)


def official_law_url(kind: str, name: str, supplied: str = "") -> str:
    resolved = absolute_url("https://www.law.go.kr", supplied)
    if resolved:
        return resolved
    category = "행정규칙" if kind == "행정규칙" else "법령"
    return f"https://www.law.go.kr/{urllib.parse.quote(category)}/{urllib.parse.quote(name)}"


def official_notice_url(sequence: str, supplied: str = "") -> str:
    resolved = absolute_url("https://www.lawmaking.go.kr", supplied)
    if resolved:
        return resolved
    base = "https://www.lawmaking.go.kr/lmSts/ogLmPp"
    if sequence:
        return base + "?lmPpSeq=" + urllib.parse.quote(sequence)
    return base


def match_score(target: str, candidate: str) -> int:
    left = normalize_text(target)
    right = normalize_text(candidate)
    if not left or not right:
        return 0
    if left == right:
        return 100
    if left in right or right in left:
        return 80
    removable = ("법률", "시행령", "시행규칙", "기준", "규정", "에관한")
    left_core = left
    right_core = right
    for token in removable:
        left_core = left_core.replace(token, "")
        right_core = right_core.replace(token, "")
    if left_core and right_core and (left_core in right_core or right_core in left_core):
        return 65
    left_tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", str(target)))
    right_tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", str(candidate)))
    if left_tokens and right_tokens:
        common = left_tokens.intersection(right_tokens)
        if common:
            return min(60, 20 + 15 * len(common))
    return 0


class Monitor:
    def __init__(self, db: Database, settings: dict[str, Any]):
        self.db = db
        self.settings = DEFAULT_SETTINGS.copy()
        self.settings.update(settings)
        self.http = HttpClient(safe_int(self.settings.get("request_timeout"), 30))

    def sync_all(self, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        report = {
            "checked_items": 0,
            "new_changes": 0,
            "new_notices": 0,
            "errors": [],
        }
        progress = progress or (lambda _message: None)
        items = self.db.items(enabled_only=True)
        for index, item in enumerate(items, start=1):
            progress(f"법규 개정 확인 중 {index}/{len(items)}: {item['name']}")
            try:
                inserted = self._sync_item(item)
                report["checked_items"] += 1
                if inserted:
                    report["new_changes"] += 1
            except Exception as exc:
                message = f"{item['name']}: {exc}"
                report["errors"].append(message)
                self.db.update_item_failure(int(item["id"]), str(exc))
                self.db.add_log("법규", "실패", message)

        progress("관련 입법예고 확인 중...")
        try:
            report["new_notices"] = self._sync_notices(items)
            self.db.add_log(
                "입법예고",
                "성공",
                f"신규 {report['new_notices']}건",
            )
        except Exception as exc:
            report["errors"].append(f"입법예고: {exc}")
            self.db.add_log("입법예고", "실패", str(exc))

        self.db.set_meta("last_sync_at", now_text())
        if report["errors"]:
            self.db.add_log("전체점검", "일부 실패", "\n".join(report["errors"]))
        else:
            self.db.add_log(
                "전체점검",
                "성공",
                f"법규 {report['checked_items']}건, 신규 개정 {report['new_changes']}건, 신규 입법예고 {report['new_notices']}건",
            )
        return report

    def _sync_item(self, item: sqlite3.Row) -> bool:
        kind = str(item["kind"])
        target = "admrul" if kind == "행정규칙" else "law"
        params = {
            "OC": self.settings.get("law_oc", ""),
            "target": target,
            "type": "JSON",
            "query": str(item["name"]),
            "display": 20,
            "page": 1,
            "sort": "ddes",
        }
        raw = self.http.get(str(self.settings["law_search_url"]), params)
        payload = parse_payload(raw)
        records = list(iter_json_records(payload)) if isinstance(payload, (dict, list)) else []
        best: dict[str, Any] | None = None
        best_score = 0
        for record in records:
            candidate_name = dict_value(
                record,
                "법령명한글",
                "법령명",
                "법령명_한글",
                "행정규칙명",
                "title",
                "lsNm",
            )
            score = match_score(str(item["name"]), candidate_name)
            if score > best_score:
                best = record
                best_score = score
        if not best or best_score < 20:
            raise RuntimeError("검색 결과에서 일치하는 법규를 찾지 못했습니다.")

        name = dict_value(
            best,
            "법령명한글",
            "법령명",
            "법령명_한글",
            "행정규칙명",
            "title",
            "lsNm",
        ) or str(item["name"])
        source_id = dict_value(
            best,
            "법령ID",
            "행정규칙ID",
            "법령일련번호",
            "행정규칙일련번호",
            "MST",
            "id",
        ) or str(item["source_id"])
        revision_date = normalize_date(
            dict_value(best, "공포일자", "발령일자", "개정일자", "promulgationDate")
        )
        enforcement_date = normalize_date(
            dict_value(best, "시행일자", "enforcementDate")
        )
        revision_type = dict_value(
            best,
            "제개정구분명",
            "제개정구분",
            "개정구분",
            "revisionType",
        )
        ministry = dict_value(
            best,
            "소관부처명",
            "소관부처",
            "부처명",
            "ministry",
        )
        supplied_link = dict_value(
            best,
            "법령상세링크",
            "행정규칙상세링크",
            "상세링크",
            "link",
            "url",
        )
        url = official_law_url(kind, name, supplied_link or str(item["official_url"]))
        event_key = ":".join(
            [
                kind,
                source_id or normalize_text(name),
                revision_date or "날짜미상",
                revision_type or "구분미상",
            ]
        )
        previous_key = str(item["last_event_key"] or "")
        is_baseline = not previous_key
        is_changed = bool(previous_key and previous_key != event_key)
        inserted = self.db.upsert_change(
            {
                "event_key": event_key,
                "managed_item_id": int(item["id"]),
                "kind": kind,
                "name": name,
                "source_id": source_id,
                "revision_type": revision_type,
                "promulgation_date": revision_date,
                "enforcement_date": enforcement_date,
                "ministry": ministry,
                "official_url": url,
            },
            is_new=is_changed,
        )
        self.db.update_item_check(
            int(item["id"]),
            event_key=event_key,
            revision_type=revision_type,
            revision_date=revision_date,
            enforcement_date=enforcement_date,
            source_id=source_id,
            official_url=url,
            status="정상",
        )
        self.db.add_log(
            "법규",
            "성공",
            f"{name} 기준선 저장" if is_baseline else f"{name} 확인",
        )
        return bool(inserted and is_changed)

    def _sync_notices(self, items: list[sqlite3.Row]) -> int:
        if not items:
            return 0
        records: list[dict[str, str]] = []
        seen_record_keys: set[str] = set()
        for diff, forced_status in ((0, "진행 중"), (1, "종료")):
            raw = self.http.get(
                str(self.settings["notice_url"]),
                {"OC": self.settings.get("notice_oc", ""), "diff": diff},
            )
            payload = parse_payload(raw)
            parsed = self._notice_records(payload, forced_status)
            for record in parsed:
                rough_key = "|".join(
                    [record.get("sequence", ""), record.get("notice_no", ""), record.get("title", ""), record.get("start_date", "")]
                )
                if rough_key in seen_record_keys:
                    continue
                seen_record_keys.add(rough_key)
                records.append(record)

        baseline = self.db.get_meta("notice_baseline_done", "") != "1"
        inserted_new = 0
        cutoff = date.today() - timedelta(
            days=max(0, safe_int(self.settings.get("closed_notice_days"), 45))
        )
        for record in records:
            title = record.get("title", "").strip()
            if not title:
                continue
            if record.get("status") == "종료":
                end_value = parse_date(record.get("end_date"))
                if end_value and end_value < cutoff:
                    continue
            scored = [
                (match_score(str(item["name"]), title), item)
                for item in items
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            if not scored or scored[0][0] < 20:
                continue
            matched = scored[0][1]
            sequence = record.get("sequence", "")
            key = "notice:" + ":".join(
                [
                    sequence or record.get("notice_no", "") or normalize_text(title),
                    record.get("start_date", "") or "날짜미상",
                ]
            )
            inserted = self.db.upsert_notice(
                {
                    "notice_key": key,
                    "title": title,
                    "ministry": record.get("ministry", ""),
                    "notice_no": record.get("notice_no", ""),
                    "start_date": record.get("start_date", ""),
                    "end_date": record.get("end_date", ""),
                    "status": record.get("status", "미확인"),
                    "official_url": official_notice_url(sequence, record.get("official_url", "")),
                    "matched_item": str(matched["name"]),
                },
                is_new=not baseline,
            )
            if inserted and not baseline:
                inserted_new += 1
        self.db.set_meta("notice_baseline_done", "1")
        return inserted_new

    def _notice_records(self, payload: Any, forced_status: str) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        if isinstance(payload, (dict, list)):
            for record in iter_json_records(payload):
                title = dict_value(
                    record,
                    "법령안명",
                    "입법예고명",
                    "제목",
                    "title",
                    "lmPpNm",
                    "lmPpPnm",
                )
                if not title:
                    continue
                output.append(
                    {
                        "title": title,
                        "ministry": dict_value(record, "소관부처", "부처명", "deptNm", "소관부처명"),
                        "notice_no": dict_value(record, "공고번호", "announceNo", "noticeNo"),
                        "start_date": normalize_date(dict_value(record, "공고일자", "시작일자", "announceStartDt", "예고시작일")),
                        "end_date": normalize_date(dict_value(record, "마감일자", "종료일자", "announceEndDt", "예고종료일")),
                        "sequence": dict_value(record, "입법예고ID", "lmPpSeq", "id"),
                        "official_url": dict_value(record, "상세링크", "link", "url", "상세페이지"),
                        "status": forced_status,
                    }
                )
            return output

        if isinstance(payload, ET.Element):
            for node in iter_xml_records(payload):
                title = xml_child_value(
                    node,
                    "법령안명",
                    "입법예고명",
                    "제목",
                    "title",
                    "lmPpNm",
                    "lmPpPnm",
                )
                if not title:
                    continue
                output.append(
                    {
                        "title": title,
                        "ministry": xml_child_value(node, "소관부처", "부처명", "deptNm", "소관부처명"),
                        "notice_no": xml_child_value(node, "공고번호", "announceNo", "noticeNo"),
                        "start_date": normalize_date(xml_child_value(node, "공고일자", "시작일자", "announceStartDt", "예고시작일")),
                        "end_date": normalize_date(xml_child_value(node, "마감일자", "종료일자", "announceEndDt", "예고종료일")),
                        "sequence": xml_child_value(node, "입법예고ID", "lmPpSeq", "id"),
                        "official_url": xml_child_value(node, "상세링크", "link", "url", "상세페이지"),
                        "status": forced_status,
                    }
                )
        return output


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db_file = Path(folder) / "test.db"
        # 불완전한 과거 DB를 고의로 만든 뒤 자동 보완되는지 확인한다.
        raw = sqlite3.connect(db_file)
        raw.executescript(
            """
            CREATE TABLE legislative_notices(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                notice_status TEXT
            );
            INSERT INTO legislative_notices(title,notice_status) VALUES('시험 공고','진행 중');
            """
        )
        raw.commit()
        raw.close()

        db = Database(db_file)
        with db.connect() as conn:
            columns = Database._columns(conn, "legislative_notices")
            assert "status" in columns
            assert "official_url" in columns
            migrated = conn.execute(
                "SELECT status FROM legislative_notices WHERE title='시험 공고'"
            ).fetchone()
            assert migrated and migrated[0] == "진행 중"

        item_id = db.add_item("법령", "소방시설 설치 및 관리에 관한 법률")
        items = db.items()
        assert len(items) == 1 and int(items[0]["id"]) == item_id
        db.upsert_change(
            {
                "event_key": "law:test:2026-01-01:일부개정",
                "managed_item_id": item_id,
                "kind": "법령",
                "name": "시험법",
                "revision_type": "일부개정",
                "promulgation_date": "2026-01-01",
                "official_url": "https://www.law.go.kr",
            },
            is_new=True,
        )
        db.upsert_notice(
            {
                "notice_key": "notice:test",
                "title": "시험 입법예고",
                "status": "진행 중",
                "official_url": "https://www.lawmaking.go.kr",
                "matched_item": "시험법",
            },
            is_new=True,
        )
        assert db.counts()["changes"] == 1
        assert db.counts()["notices"] >= 2
        for table in ("managed_items", "change_events", "legislative_notices"):
            with db.connect() as conn:
                columns = Database._columns(conn, table)
                forbidden = {"body", "content", "article_text", "attachment_blob", "xml_raw"}
                assert not columns.intersection(forbidden)
