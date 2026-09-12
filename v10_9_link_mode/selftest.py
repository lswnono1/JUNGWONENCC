from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from .connection_patch import install_connection_patch
from .migration_patch import install_patch

install_connection_patch()
install_patch()

from .core import Database


def make_incomplete_db(folder: str) -> Path:
    db_file = Path(folder) / "repair.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE legislative_notices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            notice_status TEXT
        );
        INSERT INTO legislative_notices(title,notice_status)
        VALUES('시험 공고','진행 중');
        """
    )
    conn.commit()
    conn.close()
    return db_file


def test_repair_initialize() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db_file = make_incomplete_db(folder)
        Database(db_file)


def test_repair_columns() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db_file = make_incomplete_db(folder)
        db = Database(db_file)
        with db.connect() as checked:
            columns = Database._columns(checked, "legislative_notices")
            assert "status" in columns
            assert "official_url" in columns
            assert "notice_key" in columns


def test_repair_values() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db_file = make_incomplete_db(folder)
        db = Database(db_file)
        with db.connect() as checked:
            row = checked.execute(
                "SELECT status,notice_key FROM legislative_notices WHERE title='시험 공고'"
            ).fetchone()
            assert row is not None
            assert row[0] == "진행 중"
            assert str(row[1]).startswith("legacy-notice:")


def test_records() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "records.db")
        item_id = db.add_item("법령", "소방시설 설치 및 관리에 관한 법률")
        assert item_id > 0
        inserted = db.upsert_change(
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
        assert inserted
        inserted_notice = db.upsert_notice(
            {
                "notice_key": "notice:test",
                "title": "시험 입법예고",
                "status": "진행 중",
                "official_url": "https://www.lawmaking.go.kr",
                "matched_item": "시험법",
            },
            is_new=True,
        )
        assert inserted_notice
        counts = db.counts()
        assert counts["managed"] == 1
        assert counts["changes"] == 1
        assert counts["notices"] == 1
        for table in ("managed_items", "change_events", "legislative_notices"):
            with db.connect() as conn:
                columns = Database._columns(conn, table)
                assert not columns.intersection(
                    {"body", "content", "article_text", "attachment_blob", "xml_raw"}
                )


def test_ui() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory() as folder:
        os.environ["JLM_DATA_ROOT"] = folder
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QListWidget

        QListWidget.sizeHint = lambda self: QSize(170, 44)  # type: ignore[method-assign]
        from .ui import MainWindow, create_application

        app = create_application(["selftest"])
        window = MainWindow()
        assert window.stack.count() == 6
        assert window.managed_table.columnCount() == 8
        assert window.changes_table.columnCount() == 8
        assert window.notices_table.columnCount() == 8
        window.close()
        app.processEvents()


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    stages = {
        "repair-init": test_repair_initialize,
        "repair-columns": test_repair_columns,
        "repair-values": test_repair_values,
        "records": test_records,
        "ui": test_ui,
    }
    if stage == "all":
        for function in stages.values():
            function()
        return 0
    if stage not in stages:
        raise SystemExit(f"unknown stage: {stage}")
    stages[stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
