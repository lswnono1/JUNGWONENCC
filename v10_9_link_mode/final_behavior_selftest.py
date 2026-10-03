from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from .connection_patch import install_connection_patch
from .migration_patch import install_patch

install_connection_patch()
install_patch()

from .api_search_patch import apply_patch as apply_api_search_patch

apply_api_search_patch()

from .enhancement_patch import apply_core_enhancements

apply_core_enhancements()

from .actual_change_cleanup_patch import apply_patch as apply_actual_change_cleanup_patch

apply_actual_change_cleanup_patch()

from .period_notice_patch import apply_core_patch as apply_period_notice_core_patch

apply_period_notice_core_patch()

from .final_behavior_patch import (
    apply_core_patch,
    apply_ui_patch,
    direct_notice_match,
)

apply_core_patch()

from . import core, enhancement_patch


def _notice_xml(title: str = "", sequence: str = "", start: str = "", end: str = "") -> bytes:
    if not title:
        return b"<response><items /></response>"
    root = ET.Element("response")
    item = ET.SubElement(root, "item")
    ET.SubElement(item, "ogLmPpSeq").text = sequence
    ET.SubElement(item, "lsNm").text = title
    ET.SubElement(item, "asndOfiNm").text = "국토교통부"
    ET.SubElement(item, "pntcNo").text = "국토교통부공고제2026-100호"
    ET.SubElement(item, "stYd").text = start
    ET.SubElement(item, "edYd").text = end
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class FakeNoticeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, _url: str, params: dict) -> bytes:
        self.calls.append(dict(params))
        page = int(params.get("pageIndex") or params.get("page") or 1)
        today = date.today()
        start = (today - timedelta(days=5)).strftime("%Y%m%d")
        end = (today + timedelta(days=25)).strftime("%Y%m%d")
        if page == 1:
            return _notice_xml(
                "도로교통법 시행령 일부개정령안 입법예고",
                "100",
                start,
                end,
            )
        if page == 2:
            return _notice_xml(
                "건축법 시행령 일부개정령(안) 입법예고",
                "200",
                start,
                end,
            )
        return _notice_xml()


def test_direct_notice_matching() -> None:
    assert direct_notice_match(
        "건축법 시행령",
        "건축법 시행령 일부개정령(안) 입법예고",
    )
    assert direct_notice_match(
        "소방시설 설치 및 관리에 관한 법률",
        "소방시설 설치 및 관리에 관한 법률 일부개정법률안 입법예고",
    )
    assert not direct_notice_match(
        "건축법",
        "건축법 시행령 일부개정령안 입법예고",
    )


def test_future_enforcement_is_added_from_baseline() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = core.Database(Path(folder) / "future.db")
        item_id = db.add_item("법령", "시험법")
        item = db.item(item_id)
        assert item is not None
        future = date.today() + timedelta(days=120)

        def fake_find_best(_monitor, _item):
            return (
                {
                    "법령명한글": "시험법",
                    "법령ID": "FUTURE-1",
                    "공포일자": date.today().strftime("%Y%m%d"),
                    "시행일자": future.strftime("%Y%m%d"),
                    "제개정구분명": "일부개정",
                    "소관부처명": "법제처",
                    "법령상세링크": "/법령/시험법",
                },
                "eflaw",
                120,
            )

        original = enhancement_patch._find_best
        enhancement_patch._find_best = fake_find_best
        try:
            monitor = core.Monitor(db, core.DEFAULT_SETTINGS.copy())
            assert monitor._sync_item(item) is True
            rows = db.actual_changes(limit=20)
            assert len(rows) == 1
            assert "시행예정" in str(rows[0]["revision_type"])
            assert rows[0]["enforcement_date"] == future.isoformat()
            # 같은 자료를 다시 점검해도 신규 행을 중복 생성하지 않는다.
            refreshed = db.item(item_id)
            assert refreshed is not None
            assert monitor._sync_item(refreshed) is False
            assert len(db.actual_changes(limit=20)) == 1
        finally:
            enhancement_patch._find_best = original


def test_notice_pagination_finds_building_act_decree() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = core.Database(Path(folder) / "notice.db")
        db.add_item("법령", "건축법 시행령")
        db.set_meta("notice_baseline_done", "1")
        settings = core.DEFAULT_SETTINGS.copy()
        settings.update(
            {
                "notice_oc": "test",
                "notice_url": "https://example.invalid/rest/ogLmPp.xml",
            }
        )
        monitor = core.Monitor(db, settings)
        fake = FakeNoticeHttp()
        monitor.http = fake
        inserted = monitor._sync_notices(db.items(enabled_only=True))
        assert inserted == 1
        rows = db.active_notices(limit=20)
        assert len(rows) == 1
        assert rows[0]["title"].startswith("건축법 시행령")
        assert rows[0]["matched_item"] == "건축법 시행령"
        assert any(int(call.get("pageIndex", 0)) == 2 for call in fake.calls)


def test_enter_runs_search_without_accepting_dialog() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory() as folder:
        os.environ["JLM_DATA_ROOT"] = folder
        settings = core.DEFAULT_SETTINGS.copy()
        settings["startup_check"] = False
        core.save_settings(settings)

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from . import ui
        from .enhancement_patch import apply_ui_enhancements
        from .period_notice_patch import apply_ui_patch as apply_period_notice_ui_patch

        apply_ui_enhancements()
        apply_period_notice_ui_patch()
        apply_ui_patch()
        app = ui.create_application(["final-behavior-selftest"])
        window = ui.MainWindow()
        dialog = ui.ManagedItemDialog(window)
        calls: list[str] = []
        dialog.start_search = lambda: calls.append(dialog.name.text())
        dialog.show()
        dialog.name.setText("건축법 시행령")
        dialog.name.setFocus()
        QTest.keyClick(dialog.name, Qt.Key.Key_Return)
        app.processEvents()
        assert calls == ["건축법 시행령"]
        assert dialog.isVisible()
        assert dialog.result() == 0
        QTest.keyClick(dialog.name, Qt.Key.Key_Escape)
        app.processEvents()
        assert not dialog.isVisible()
        window.close()
        app.processEvents()
        app.quit()


def run_all() -> None:
    test_direct_notice_matching()
    test_future_enforcement_is_added_from_baseline()
    test_notice_pagination_finds_building_act_decree()
    test_enter_runs_search_without_accepting_dialog()


if __name__ == "__main__":
    run_all()
