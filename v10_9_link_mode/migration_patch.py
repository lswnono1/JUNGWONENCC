from __future__ import annotations

import sqlite3
from typing import Any

from . import core


def _row_value(row: sqlite3.Row, columns: set[str], *names: str) -> str:
    for name in names:
        if name in columns:
            value = row[name]
            if value not in (None, ""):
                return str(value)
    return ""


def robust_initialize(self: core.Database) -> None:
    with self.connect() as conn:
        existing_tables = self._tables(conn)
        recovered_notice_table = ""

        # 시험판이나 중단된 초기화로 핵심 열이 없는 경우에는 원본 테이블을
        # 보존 이름으로 바꾸고 정상 테이블을 새로 만든 뒤 메타정보만 복사한다.
        if "legislative_notices" in existing_tables:
            notice_columns = self._columns(conn, "legislative_notices")
            required = {"notice_key", "title", "status", "official_url", "detected_at"}
            if not required.issubset(notice_columns):
                base = "legislative_notices_recovered_legacy"
                recovered_notice_table = base
                suffix = 1
                while recovered_notice_table in existing_tables:
                    suffix += 1
                    recovered_notice_table = f"{base}_{suffix}"
                conn.execute(
                    f"ALTER TABLE {core.quote_identifier('legislative_notices')} "
                    f"RENAME TO {core.quote_identifier(recovered_notice_table)}"
                )

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

        if recovered_notice_table:
            legacy_columns = self._columns(conn, recovered_notice_table)
            rows = conn.execute(
                f"SELECT * FROM {core.quote_identifier(recovered_notice_table)}"
            ).fetchall()
            for index, row in enumerate(rows, start=1):
                legacy_id = _row_value(row, legacy_columns, "id") or str(index)
                title = _row_value(row, legacy_columns, "title", "입법예고명", "법령안명")
                status = _row_value(row, legacy_columns, "status", "notice_status") or "미확인"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO legislative_notices(
                        notice_key,title,ministry,notice_no,start_date,end_date,status,
                        official_url,matched_item,detected_at,last_seen_at,is_new
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"legacy-notice:{recovered_notice_table}:{legacy_id}",
                        title,
                        _row_value(row, legacy_columns, "ministry", "부처명", "소관부처"),
                        _row_value(row, legacy_columns, "notice_no", "공고번호"),
                        _row_value(row, legacy_columns, "start_date", "시작일자"),
                        _row_value(row, legacy_columns, "end_date", "종료일자"),
                        status,
                        _row_value(row, legacy_columns, "official_url", "url", "link"),
                        _row_value(row, legacy_columns, "matched_item", "관련관리대상"),
                        core.now_text(),
                        core.now_text(),
                        0,
                    ),
                )

        current_notice_columns = self._columns(conn, "legislative_notices")
        if "notice_status" in current_notice_columns:
            conn.execute(
                "UPDATE legislative_notices SET status=notice_status "
                "WHERE (status='' OR status='미확인') AND notice_status<>''"
            )

        conn.execute("UPDATE managed_items SET created_at=? WHERE created_at=''", (core.now_text(),))
        conn.execute("UPDATE managed_items SET updated_at=created_at WHERE updated_at=''")
        conn.execute(
            "UPDATE change_events SET event_key='legacy-change:' || id "
            "WHERE event_key='' OR event_key IS NULL"
        )
        conn.execute(
            "UPDATE legislative_notices SET notice_key='legacy-notice:' || id "
            "WHERE notice_key='' OR notice_key IS NULL"
        )

        # 인덱스를 만들기 전에 중복 메타행을 안전하게 정리한다.
        conn.execute(
            "DELETE FROM managed_items WHERE id NOT IN "
            "(SELECT MIN(id) FROM managed_items GROUP BY kind,name)"
        )
        conn.execute(
            "DELETE FROM change_events WHERE id NOT IN "
            "(SELECT MIN(id) FROM change_events GROUP BY event_key)"
        )
        conn.execute(
            "DELETE FROM legislative_notices WHERE id NOT IN "
            "(SELECT MIN(id) FROM legislative_notices GROUP BY notice_key)"
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
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version','4') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        if recovered_notice_table:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('last_recovered_notice_table',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (recovered_notice_table,),
            )


def install_patch() -> None:
    if getattr(core.Database, "_link_mode_recovery_installed", False):
        return
    core.Database.initialize = robust_initialize  # type: ignore[method-assign]
    core.Database._link_mode_recovery_installed = True  # type: ignore[attr-defined]
