from __future__ import annotations

import ctypes
import multiprocessing
import os
import sys
import traceback
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

from .core import APP_TITLE, app_root, load_settings, log_dir, save_settings


def write_startup_error(text: str) -> list[Path]:
    targets: list[Path] = []
    for folder in (log_dir(), Path.cwd()):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "startup_error.txt"
            path.write_text(text, encoding="utf-8")
            targets.append(path)
        except Exception:
            continue
    return targets


def native_error_message(message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, APP_TITLE, 0x10)
            return
        except Exception:
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def run_startup_smoke_test() -> int:
    """Verify the packaged program can construct and close its real main window."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    settings = load_settings()
    settings["startup_check"] = False
    save_settings(settings)

    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QListWidget

    QListWidget.sizeHint = lambda self: QSize(170, 44)  # type: ignore[method-assign]

    from .ui import MainWindow, create_application
    from .enhancement_patch import apply_ui_enhancements

    apply_ui_enhancements()
    application = create_application(["JungwonLawMonitor-self-test"])
    window = MainWindow()
    assert window.stack.count() == 6
    assert hasattr(window, "dashboard_range_label")
    assert hasattr(window, "setting_recent_days")
    window.show()
    application.processEvents()
    window.close()
    application.processEvents()
    application.quit()
    del window
    del application
    return 0


def main() -> int:
    multiprocessing.freeze_support()
    if "--self-test" in sys.argv:
        return run_startup_smoke_test()

    try:
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QListWidget

        # v10.9형 좌측 메뉴의 행 높이를 일정하게 유지한다.
        QListWidget.sizeHint = lambda self: QSize(170, 44)  # type: ignore[method-assign]

        from .ui import MainWindow, create_application
        from .enhancement_patch import apply_ui_enhancements

        apply_ui_enhancements()
        application = create_application(sys.argv)
        window = MainWindow()
        window.show()
        return int(application.exec())
    except Exception:
        details = traceback.format_exc()
        paths = write_startup_error(details)
        location = str(paths[0]) if paths else str(app_root())
        native_error_message(
            "프로그램 시작 중 오류가 발생했습니다.\n\n"
            f"오류 기록: {location}\n\n"
            f"{details[-1200:]}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
