from __future__ import annotations

import json
import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from .connection_patch import install_connection_patch
from .migration_patch import install_patch

install_connection_patch()
install_patch()

from .api_search_patch import apply_patch as apply_api_search_patch
from .enhancement_patch import (
    apply_core_enhancements,
    apply_ui_enhancements,
    search_catalog,
    should_run_daily_auto,
)

apply_api_search_patch()
apply_core_enhancements()

from . import enhancement_patch
from .core import Database, Monitor


class FakeCatalogHttp:
    def __init__(self):
        self.calls: list[dict] = []

    def get(self, _url: str, params: dict) -> bytes:
        self.calls.append(dict(params))
        if params.get("target") == "eflaw":
            payload = {
                "LawSearch": {
                    "totalCnt": 1,
                    "law": [
                        {
                            "법령명한글": "소방시설 설치 및 관리에 관한 법률",
                            "법령ID": "140000",
                            "공포일자": "20260701",
                            "시행일자": "20260801",
                            "제개정구분명": "일부개정",
                            "소관부처명": "소방청",
                            "법령상세링크": "/법령/소방시설설치및관리에관한법률",
                        }
                    ],
                }
            }
        else:
            payload = {
                "AdmRulSearch": {
                    "totalCnt": 1,
                    "admrul": [
                        {
                            "행정규칙명": "소방시설 자체점검사항 등에 관한 고시",
                            "행정규칙ID": "99001",
                            "발령일자": "20260620",
                            "시행일자": "20260620",
                            "제개정구분명": "일부개정",
                            "소관부처명": "소방청",
                            "행정규칙상세링크": "/행정규칙/소방시설자체점검사항등에관한고시",
                        }
                    ],
                }
            }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_daily_auto() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "daily.db")
        day = date(2026, 7, 15)
        assert should_run_daily_auto(db, day)
        db.set_meta("last_auto_sync_date", day.isoformat())
        assert not should_run_daily_auto(db, day)
        assert should_run_daily_auto(db, date(2026, 7, 16))


def test_actual_change_only() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "change.db")
        item_id = db.add_item("법령", "시험 법률")
        item = db.item(item_id)
        assert item is not None
        state = {"date": "20260701"}

        def fake_find_best(_monitor, _item):
            return (
                {
                    "법령명한글": "시험 법률",
                    "법령ID": "1000",
                    "공포일자": state["date"],
                    "시행일자": state["date"],
                    "제개정구분명": "일부개정",
                    "소관부처명": "법제처",
                    "법령상세링크": "/법령/시험법률",
                },
                "eflaw",
                120,
            )

        original = enhancement_patch._find_best
        enhancement_patch._find_best = fake_find_best
        try:
            monitor = Monitor(db, {})
            assert monitor._sync_item(item) is False
            assert db.actual_changes() == []
            state["date"] = "20260715"
            refreshed = db.item(item_id)
            assert refreshed is not None
            assert monitor._sync_item(refreshed) is True
            changes = db.actual_changes()
            assert len(changes) == 1
            assert changes[0]["promulgation_date"] == "2026-07-15"
            assert changes[0]["is_actual_change"] == 1
        finally:
            enhancement_patch._find_best = original


def test_official_notice_fields() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "notice.db")
        monitor = Monitor(db, {})
        xml = ET.fromstring(
            """
            <response>
              <item>
                <ogLmPpSeq>77777</ogLmPpSeq>
                <lsNm>소방시설 설치 및 관리에 관한 법률 일부개정법률안</lsNm>
                <asndOfiNm>소방청</asndOfiNm>
                <pntcNo>소방청공고제2026-10호</pntcNo>
                <pntcDt>20260701</pntcDt>
                <stYd>20260701</stYd>
                <edYd>20260731</edYd>
              </item>
            </response>
            """
        )
        rows = monitor._notice_records(xml, "진행 중")
        assert len(rows) == 1
        assert rows[0]["sequence"] == "77777"
        assert rows[0]["title"].startswith("소방시설")
        assert rows[0]["ministry"] == "소방청"
        assert rows[0]["start_date"] == "2026-07-01"
        assert rows[0]["end_date"] == "2026-07-31"


def test_catalog_search() -> None:
    fake = FakeCatalogHttp()
    rows = search_catalog(
        {
            "law_oc": "test",
            "law_search_url": "https://example.invalid",
            "request_timeout": 5,
        },
        "소방시설",
        "전체",
        http=fake,
    )
    assert rows
    assert any(row["kind"] == "법령" for row in rows)
    assert any(row["kind"] == "행정규칙" for row in rows)
    assert any(call.get("target") == "eflaw" for call in fake.calls)
    assert any(call.get("target") == "admrul" for call in fake.calls)


def test_ui() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory() as folder:
        os.environ["JLM_DATA_ROOT"] = folder
        from .core import save_settings

        save_settings(
            {
                "law_oc": "test",
                "notice_oc": "test",
                "startup_check": False,
                "recent_revision_days": 30,
            }
        )
        from . import ui

        apply_ui_enhancements()
        app = ui.create_application(["enhancement-selftest"])
        window = ui.MainWindow()
        assert hasattr(window, "dashboard_range_label")
        assert hasattr(window, "setting_recent_days")
        assert window.setting_recent_days.value() == 30
        assert window.stack.count() == 6
        window.close()
        app.processEvents()


def run_all() -> None:
    test_daily_auto()
    test_actual_change_only()
    test_official_notice_fields()
    test_catalog_search()
    test_ui()


if __name__ == "__main__":
    run_all()
