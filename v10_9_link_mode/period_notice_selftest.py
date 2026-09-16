from __future__ import annotations

import os
import tempfile
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

# 실제 main.py와 동일하게 enhancement 적용 뒤 cleanup 모듈을 가져와야
# 이전 initialize 함수가 올바르게 연결된다.
from .actual_change_cleanup_patch import apply_patch as apply_actual_change_cleanup_patch

apply_actual_change_cleanup_patch()

from .period_notice_patch import (
    apply_core_patch,
    apply_ui_patch,
    direct_notice_match,
)

apply_core_patch()

from . import core


def test_direct_matching() -> None:
    assert direct_notice_match(
        "소방시설 설치 및 관리에 관한 법률",
        "소방시설 설치 및 관리에 관한 법률 일부개정법률안",
    )
    assert direct_notice_match(
        "건축법 시행령",
        "건축법 시행령 일부개정령안 입법예고",
    )
    assert not direct_notice_match(
        "소방시설 설치 및 관리에 관한 법률",
        "도로교통법 시행령 일부개정령안",
    )


def test_active_direct_database_filter() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = core.Database(Path(folder) / "period-notice.db")
        db.add_item("법령", "소방시설 설치 및 관리에 관한 법률")
        today = date.today()
        active_end = (today + timedelta(days=20)).isoformat()
        active_start = (today - timedelta(days=5)).isoformat()
        ended_end = (today - timedelta(days=1)).isoformat()
        for key, title, end_date, status in (
            (
                "notice:direct-active",
                "소방시설 설치 및 관리에 관한 법률 일부개정법률안",
                active_end,
                "진행 중",
            ),
            (
                "notice:unrelated-active",
                "도로교통법 시행령 일부개정령안",
                active_end,
                "진행 중",
            ),
            (
                "notice:direct-ended",
                "소방시설 설치 및 관리에 관한 법률 일부개정법률안",
                ended_end,
                "종료",
            ),
        ):
            db.upsert_notice(
                {
                    "notice_key": key,
                    "title": title,
                    "ministry": "소방청",
                    "notice_no": key,
                    "start_date": active_start,
                    "end_date": end_date,
                    "status": status,
                    "official_url": "https://opinion.lawmaking.go.kr/",
                    "matched_item": "",
                },
                is_new=True,
            )
        db.reclassify_notice_rows()
        rows = db.active_notices(
            start_date=(today - timedelta(days=30)).isoformat(),
            end_date=(today + timedelta(days=30)).isoformat(),
        )
        assert len(rows) == 1
        assert rows[0]["notice_key"] == "notice:direct-active"
        assert rows[0]["matched_item"] == "소방시설 설치 및 관리에 관한 법률"
        counts = db.counts()
        assert counts["notices"] == 1
        assert counts["new_notices"] == 1


def test_user_interface_shortcuts_and_period_widgets() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory() as folder:
        os.environ["JLM_DATA_ROOT"] = folder
        settings = core.DEFAULT_SETTINGS.copy()
        settings.update(
            {
                "law_oc": "test",
                "notice_oc": "test",
                "startup_check": False,
                "recent_revision_days": 30,
            }
        )
        core.save_settings(settings)

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from . import ui
        from .enhancement_patch import apply_ui_enhancements

        apply_ui_enhancements()
        apply_ui_patch()
        application = ui.create_application(["period-notice-selftest"])
        window = ui.MainWindow()
        assert hasattr(window, "change_start_date")
        assert hasattr(window, "change_end_date")
        assert hasattr(window, "notice_start_date")
        assert hasattr(window, "notice_end_date")
        assert window.change_start_date.date() <= window.change_end_date.date()
        assert window.notice_start_date.date() <= window.notice_end_date.date()

        dialog = ui.ManagedItemDialog(window)
        dialog.show()
        dialog.name.setText("가")
        dialog.status.setText("before")
        dialog.name.setFocus()
        QTest.keyClick(dialog.name, Qt.Key.Key_Return)
        application.processEvents()
        assert "두 글자" in dialog.status.text()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        application.processEvents()
        assert not dialog.isVisible()

        window.close()
        application.processEvents()
        application.quit()


def run_all() -> None:
    test_direct_matching()
    test_active_direct_database_filter()
    test_user_interface_shortcuts_and_period_widgets()


if __name__ == "__main__":
    run_all()
