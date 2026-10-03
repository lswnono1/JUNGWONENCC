from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import (
    APP_TITLE,
    APP_VERSION,
    DEFAULT_SETTINGS,
    Database,
    Monitor,
    app_root,
    load_settings,
    now_text,
    official_law_url,
    save_settings,
)

ROLE_URL = int(Qt.ItemDataRole.UserRole)
ROLE_ID = ROLE_URL + 1


class SyncWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)

    def __init__(self, db: Database, settings: dict[str, Any]):
        super().__init__()
        self.db = db
        self.settings = settings.copy()

    @Slot()
    def run(self) -> None:
        try:
            result = Monitor(self.db, self.settings).sync_all(self.progress.emit)
        except Exception as exc:
            result = {
                "checked_items": 0,
                "new_changes": 0,
                "new_notices": 0,
                "errors": [f"전체점검: {exc}"],
            }
        self.finished.emit(result)


class MetricCard(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        title_label = QLabel(title)
        title_label.setObjectName("metricTitle")
        self.value_label = QLabel("0")
        self.value_label.setObjectName("metricValue")
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: int | str) -> None:
        self.value_label.setText(str(value))


class ManagedItemDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("관리대상 추가")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItems(["법령", "행정규칙"])
        self.name = QLineEdit()
        self.name.setPlaceholderText("예: 소방시설 설치 및 관리에 관한 법률")
        self.source_id = QLineEdit()
        self.source_id.setPlaceholderText("알고 있는 경우에만 입력")
        self.official_url = QLineEdit()
        self.official_url.setPlaceholderText("비워두면 법규명으로 공식 사이트 연결")
        form.addRow("구분", self.kind)
        form.addRow("관리대상명", self.name)
        form.addRow("법령·행정규칙 ID", self.source_id)
        form.addRow("공식 URL", self.official_url)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "확인", "관리대상명을 입력하세요.")
            return
        self.accept()

    def values(self) -> dict[str, str]:
        return {
            "kind": self.kind.currentText(),
            "name": self.name.text().strip(),
            "source_id": self.source_id.text().strip(),
            "official_url": self.official_url.text().strip(),
        }


class MainWindow(QMainWindow):
    PAGE_NAMES = ["대시보드", "관리대상", "개정사항", "입법예고", "설정", "점검기록"]

    def __init__(self):
        super().__init__()
        self.db = Database()
        self.settings = load_settings()
        self.sync_thread: QThread | None = None
        self.sync_worker: SyncWorker | None = None
        self.imported_count, self.imported_sources = self.db.import_legacy_managed_items(force=False)

        self.setWindowTitle(APP_TITLE)
        self.resize(1360, 820)
        self.setMinimumSize(1080, 680)
        self._build_ui()
        self._apply_style()
        self.refresh_all()

        if self.imported_count:
            self.statusBar().showMessage(
                f"기존 v10.9 관리대상 {self.imported_count}건을 가져왔습니다."
            )
        if bool(self.settings.get("startup_check", True)):
            delay = max(1, int(self.settings.get("startup_delay_seconds", 5))) * 1000
            QTimer.singleShot(delay, self.start_sync)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 14, 22, 14)
        title_box = QVBoxLayout()
        title = QLabel("정원이앤씨 법령·입법예고 모니터")
        title.setObjectName("appTitle")
        subtitle = QLabel("v10.9 기본틀 · 공식 사이트 연결형")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        self.sync_button = QPushButton("지금 개정 확인")
        self.sync_button.setObjectName("primaryButton")
        self.sync_button.setMinimumHeight(38)
        self.sync_button.clicked.connect(self.start_sync)
        header_layout.addWidget(self.sync_button)
        root_layout.addWidget(header)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(190)
        self.sidebar.setSpacing(2)
        for name in self.PAGE_NAMES:
            item = QListWidgetItem(name)
            item.setSizeHint(item.sizeHint().expandedTo(self.sidebar.sizeHint()))
            self.sidebar.addItem(item)
        self.sidebar.currentRowChanged.connect(self._change_page)
        body_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")
        body_layout.addWidget(self.stack, 1)

        self.dashboard_page = self._build_dashboard_page()
        self.managed_page = self._build_managed_page()
        self.changes_page = self._build_changes_page()
        self.notices_page = self._build_notices_page()
        self.settings_page = self._build_settings_page()
        self.logs_page = self._build_logs_page()
        for page in (
            self.dashboard_page,
            self.managed_page,
            self.changes_page,
            self.notices_page,
            self.settings_page,
            self.logs_page,
        ):
            self.stack.addWidget(page)

        root_layout.addWidget(body, 1)
        status = QStatusBar()
        status.setObjectName("statusBar")
        self.setStatusBar(status)
        self.sidebar.setCurrentRow(0)

    def _page(self, title: str, description: str = "") -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        if description:
            text = QLabel(description)
            text.setObjectName("pageDescription")
            text.setWordWrap(True)
            layout.addWidget(text)
        return page, layout

    def _build_dashboard_page(self) -> QWidget:
        page, layout = self._page(
            "대시보드",
            "개정 여부와 입법예고 목록만 API로 확인합니다. 법규 본문·별표·첨부파일은 저장하지 않으며, 확인할 때만 공식 홈페이지를 엽니다.",
        )
        banner = QFrame()
        banner.setObjectName("infoBanner")
        banner_layout = QVBoxLayout(banner)
        banner_title = QLabel("LINK MODE")
        banner_title.setObjectName("bannerTitle")
        banner_text = QLabel(
            "자동점검은 가볍게, 원문 확인은 공식 사이트에서 처리하여 시작 지연과 사용자 DB 증가를 줄였습니다."
        )
        banner_text.setWordWrap(True)
        banner_layout.addWidget(banner_title)
        banner_layout.addWidget(banner_text)
        layout.addWidget(banner)

        card_layout = QHBoxLayout()
        self.card_managed = MetricCard("관리대상")
        self.card_enabled = MetricCard("점검 사용")
        self.card_changes = MetricCard("신규 개정")
        self.card_notices = MetricCard("신규 입법예고")
        for card in (
            self.card_managed,
            self.card_enabled,
            self.card_changes,
            self.card_notices,
        ):
            card_layout.addWidget(card)
        layout.addLayout(card_layout)

        lower = QHBoxLayout()
        recent_change_box = QFrame()
        recent_change_box.setObjectName("panel")
        recent_change_layout = QVBoxLayout(recent_change_box)
        recent_change_layout.addWidget(self._section_label("최근 개정사항"))
        self.dashboard_change_table = self._create_table(
            ["구분", "법규명", "공포·발령일", "개정유형"],
            [80, 330, 110, 110],
        )
        self.dashboard_change_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.dashboard_change_table)
        )
        recent_change_layout.addWidget(self.dashboard_change_table)
        lower.addWidget(recent_change_box, 1)

        recent_notice_box = QFrame()
        recent_notice_box.setObjectName("panel")
        recent_notice_layout = QVBoxLayout(recent_notice_box)
        recent_notice_layout.addWidget(self._section_label("최근 입법예고"))
        self.dashboard_notice_table = self._create_table(
            ["상태", "입법예고명", "종료일"],
            [80, 380, 105],
        )
        self.dashboard_notice_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.dashboard_notice_table)
        )
        recent_notice_layout.addWidget(self.dashboard_notice_table)
        lower.addWidget(recent_notice_box, 1)
        layout.addLayout(lower, 1)
        return page

    def _build_managed_page(self) -> QWidget:
        page, layout = self._page(
            "관리대상",
            "기존 v10.9의 관리대상 틀을 유지합니다. 목록을 더블클릭하면 국가법령정보센터의 현재 원문을 엽니다.",
        )
        toolbar = QHBoxLayout()
        self.managed_search = QLineEdit()
        self.managed_search.setPlaceholderText("관리대상명 검색")
        self.managed_search.textChanged.connect(self.refresh_managed)
        toolbar.addWidget(self.managed_search, 1)
        add_button = QPushButton("추가")
        add_button.clicked.connect(self.add_managed_item)
        toolbar.addWidget(add_button)
        toggle_button = QPushButton("사용/중지 전환")
        toggle_button.clicked.connect(self.toggle_managed_item)
        toolbar.addWidget(toggle_button)
        open_button = QPushButton("공식 원문 열기")
        open_button.clicked.connect(lambda: self.open_table_url(self.managed_table))
        toolbar.addWidget(open_button)
        import_button = QPushButton("기존 v10.9 목록 다시 가져오기")
        import_button.clicked.connect(self.force_import)
        toolbar.addWidget(import_button)
        delete_button = QPushButton("삭제")
        delete_button.setObjectName("dangerButton")
        delete_button.clicked.connect(self.delete_managed_item)
        toolbar.addWidget(delete_button)
        layout.addLayout(toolbar)

        self.managed_table = self._create_table(
            ["사용", "구분", "관리대상명", "ID", "최근 개정", "시행일", "상태", "마지막 확인"],
            [70, 90, 380, 130, 105, 105, 220, 145],
        )
        self.managed_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.managed_table)
        )
        layout.addWidget(self.managed_table, 1)
        return page

    def _build_changes_page(self) -> QWidget:
        page, layout = self._page(
            "개정사항",
            "API에서 확인한 개정 메타정보만 저장합니다. 행을 더블클릭하거나 버튼을 누르면 국가법령정보센터 공식 페이지를 엽니다.",
        )
        toolbar = QHBoxLayout()
        self.change_search = QLineEdit()
        self.change_search.setPlaceholderText("법규명·소관부처 검색")
        self.change_search.textChanged.connect(self.refresh_changes)
        toolbar.addWidget(self.change_search, 1)
        open_button = QPushButton("공식 사이트에서 확인")
        open_button.clicked.connect(lambda: self.open_table_url(self.changes_table))
        toolbar.addWidget(open_button)
        seen_button = QPushButton("신규 표시 모두 확인")
        seen_button.clicked.connect(self.mark_changes_seen)
        toolbar.addWidget(seen_button)
        layout.addLayout(toolbar)
        self.changes_table = self._create_table(
            ["신규", "구분", "법규명", "개정유형", "공포·발령일", "시행일", "소관부처", "발견일시"],
            [55, 80, 360, 110, 110, 110, 150, 145],
        )
        self.changes_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.changes_table)
        )
        layout.addWidget(self.changes_table, 1)
        return page

    def _build_notices_page(self) -> QWidget:
        page, layout = self._page(
            "입법예고",
            "관리대상과 관련된 입법예고의 제목·기간·상태만 저장합니다. 상세 내용은 국민참여입법센터 공식 페이지에서 확인합니다.",
        )
        toolbar = QHBoxLayout()
        self.notice_search = QLineEdit()
        self.notice_search.setPlaceholderText("입법예고명·관련 관리대상 검색")
        self.notice_search.textChanged.connect(self.refresh_notices)
        toolbar.addWidget(self.notice_search, 1)
        self.notice_status_filter = QComboBox()
        self.notice_status_filter.addItems(["전체 상태", "진행 중", "종료", "미확인"])
        self.notice_status_filter.currentTextChanged.connect(self.refresh_notices)
        toolbar.addWidget(self.notice_status_filter)
        self.notice_year_filter = QComboBox()
        self.notice_year_filter.addItem("전체 연도")
        self.notice_year_filter.currentTextChanged.connect(self.refresh_notices)
        toolbar.addWidget(self.notice_year_filter)
        open_button = QPushButton("입법예고 상세 열기")
        open_button.clicked.connect(lambda: self.open_table_url(self.notices_table))
        toolbar.addWidget(open_button)
        seen_button = QPushButton("신규 표시 모두 확인")
        seen_button.clicked.connect(self.mark_notices_seen)
        toolbar.addWidget(seen_button)
        layout.addLayout(toolbar)
        self.notices_table = self._create_table(
            ["신규", "상태", "입법예고명", "관련 관리대상", "소관부처", "공고번호", "시작일", "종료일"],
            [55, 80, 400, 260, 145, 120, 100, 100],
        )
        self.notices_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.notices_table)
        )
        layout.addWidget(self.notices_table, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page, layout = self._page(
            "설정",
            "OC와 API 주소를 저장합니다. 이메일 형식으로 입력해도 OC에는 @ 앞부분만 저장됩니다.",
        )
        panel = QFrame()
        panel.setObjectName("panel")
        form = QFormLayout(panel)
        form.setContentsMargins(22, 20, 22, 20)
        self.setting_law_oc = QLineEdit(str(self.settings.get("law_oc", "")))
        self.setting_notice_oc = QLineEdit(str(self.settings.get("notice_oc", "")))
        self.setting_law_url = QLineEdit(str(self.settings.get("law_search_url", "")))
        self.setting_notice_url = QLineEdit(str(self.settings.get("notice_url", "")))
        self.setting_company = QLineEdit(str(self.settings.get("company_name", "정원이앤씨")))
        self.setting_reviewer = QLineEdit(str(self.settings.get("reviewer", "정원이앤씨")))
        self.setting_timeout = QSpinBox()
        self.setting_timeout.setRange(5, 120)
        self.setting_timeout.setValue(int(self.settings.get("request_timeout", 30)))
        self.setting_closed_days = QSpinBox()
        self.setting_closed_days.setRange(0, 3650)
        self.setting_closed_days.setValue(int(self.settings.get("closed_notice_days", 45)))
        self.setting_startup_check = QCheckBox("프로그램 실행 후 백그라운드 자동점검")
        self.setting_startup_check.setChecked(bool(self.settings.get("startup_check", True)))
        self.setting_delay = QSpinBox()
        self.setting_delay.setRange(1, 60)
        self.setting_delay.setValue(int(self.settings.get("startup_delay_seconds", 5)))
        self.setting_delay.setSuffix("초")
        form.addRow("국가법령정보 API OC", self.setting_law_oc)
        form.addRow("입법예고 API OC", self.setting_notice_oc)
        form.addRow("법령·행정규칙 검색 API", self.setting_law_url)
        form.addRow("입법예고 목록 API", self.setting_notice_url)
        form.addRow("회사명", self.setting_company)
        form.addRow("검토자", self.setting_reviewer)
        form.addRow("API 시간제한", self.setting_timeout)
        form.addRow("종료 입법예고 보관 기준", self.setting_closed_days)
        form.addRow("자동점검", self.setting_startup_check)
        form.addRow("실행 후 점검 지연", self.setting_delay)
        data_label = QLabel(str(app_root()))
        data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("사용자 데이터 폴더", data_label)
        layout.addWidget(panel)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        folder_button = QPushButton("데이터 폴더 열기")
        folder_button.clicked.connect(self.open_data_folder)
        buttons.addWidget(folder_button)
        save_button = QPushButton("설정 저장")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self.save_settings_ui)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)

        note = QLabel(
            "저장하지 않는 항목: 법령·행정규칙 전체 조문, 별표·서식 파일, 입법예고 본문, 첨부파일, XML·JSON 원문"
        )
        note.setObjectName("noteLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_logs_page(self) -> QWidget:
        page, layout = self._page(
            "점검기록",
            "API 점검 성공·실패와 관리대상별 확인 결과를 기록합니다. 본문 데이터는 기록하지 않습니다.",
        )
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        refresh_button = QPushButton("새로고침")
        refresh_button.clicked.connect(self.refresh_logs)
        toolbar.addWidget(refresh_button)
        layout.addLayout(toolbar)
        self.logs_table = self._create_table(
            ["확인일시", "구분", "상태", "메시지"],
            [150, 100, 100, 760],
        )
        layout.addWidget(self.logs_table, 1)
        return page

    def _create_table(self, headers: list[str], widths: list[int]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        for index, width in enumerate(widths):
            table.setColumnWidth(index, width)
            if index < len(headers) - 1:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
        return table

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _change_page(self, index: int) -> None:
        if 0 <= index < self.stack.count():
            self.stack.setCurrentIndex(index)
            if index == 0:
                self.refresh_dashboard()
            elif index == 1:
                self.refresh_managed()
            elif index == 2:
                self.refresh_changes()
            elif index == 3:
                self.refresh_notices()
            elif index == 5:
                self.refresh_logs()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: '맑은 고딕';
                font-size: 10pt;
                color: #243447;
            }
            QMainWindow, #stack { background: #f4f6f9; }
            #header {
                background: white;
                border-bottom: 1px solid #dce3ea;
            }
            #appTitle { font-size: 16pt; font-weight: 700; color: #172b4d; }
            #appSubtitle { color: #6b778c; }
            #sidebar {
                background: #172b4d;
                border: none;
                color: white;
                padding: 12px 8px;
                outline: none;
            }
            #sidebar::item {
                height: 44px;
                border-radius: 6px;
                padding-left: 16px;
                margin: 2px 0;
            }
            #sidebar::item:selected { background: #2f6fed; color: white; }
            #sidebar::item:hover:!selected { background: #243b64; }
            #pageTitle { font-size: 18pt; font-weight: 700; color: #172b4d; }
            #pageDescription { color: #607086; padding-bottom: 4px; }
            #infoBanner {
                background: #eaf2ff;
                border: 1px solid #b8d2ff;
                border-radius: 8px;
                padding: 8px;
            }
            #bannerTitle { color: #1f5fd1; font-weight: 800; }
            #metricCard, #panel {
                background: white;
                border: 1px solid #dfe6ee;
                border-radius: 8px;
            }
            #metricTitle { color: #6b778c; }
            #metricValue { font-size: 22pt; font-weight: 800; color: #172b4d; }
            #sectionTitle { font-size: 12pt; font-weight: 700; color: #172b4d; }
            QPushButton {
                background: white;
                border: 1px solid #c9d3df;
                border-radius: 5px;
                padding: 7px 12px;
            }
            QPushButton:hover { background: #f0f4f9; }
            #primaryButton {
                background: #2f6fed;
                border-color: #2f6fed;
                color: white;
                font-weight: 700;
            }
            #primaryButton:hover { background: #245dcc; }
            #dangerButton { color: #bd2c2c; border-color: #e2aaaa; }
            QLineEdit, QComboBox, QSpinBox {
                background: white;
                border: 1px solid #c9d3df;
                border-radius: 5px;
                padding: 6px;
                min-height: 24px;
            }
            QTableWidget {
                background: white;
                alternate-background-color: #f8fafc;
                border: 1px solid #dfe6ee;
                border-radius: 6px;
                gridline-color: #e8edf3;
                selection-background-color: #dce9ff;
                selection-color: #172b4d;
            }
            QHeaderView::section {
                background: #eef2f7;
                border: none;
                border-right: 1px solid #d8e0e8;
                border-bottom: 1px solid #d8e0e8;
                padding: 8px;
                font-weight: 700;
            }
            #noteLabel {
                background: #fff7e6;
                border: 1px solid #f2d49b;
                border-radius: 6px;
                padding: 10px;
                color: #7c5c19;
            }
            #statusBar { background: white; border-top: 1px solid #dce3ea; }
            """
        )

    def refresh_all(self) -> None:
        self.refresh_dashboard()
        self.refresh_managed()
        self.refresh_changes()
        self.refresh_notices()
        self.refresh_logs()

    def refresh_dashboard(self) -> None:
        counts = self.db.counts()
        self.card_managed.set_value(counts["managed"])
        self.card_enabled.set_value(counts["enabled"])
        self.card_changes.set_value(counts["new_changes"])
        self.card_notices.set_value(counts["new_notices"])

        changes = self.db.changes(limit=10)
        self.dashboard_change_table.setRowCount(0)
        for record in changes:
            row = self.dashboard_change_table.rowCount()
            self.dashboard_change_table.insertRow(row)
            self._fill_row(
                self.dashboard_change_table,
                row,
                [record["kind"], record["name"], record["promulgation_date"], record["revision_type"]],
                str(record["official_url"]),
                int(record["id"]),
            )

        notices = self.db.notices(limit=10)
        self.dashboard_notice_table.setRowCount(0)
        for record in notices:
            row = self.dashboard_notice_table.rowCount()
            self.dashboard_notice_table.insertRow(row)
            self._fill_row(
                self.dashboard_notice_table,
                row,
                [record["status"], record["title"], record["end_date"]],
                str(record["official_url"]),
                int(record["id"]),
            )

    def refresh_managed(self) -> None:
        if not hasattr(self, "managed_table"):
            return
        query = self.managed_search.text().strip().lower()
        self.managed_table.setRowCount(0)
        for record in self.db.items():
            searchable = " ".join(
                [str(record["kind"]), str(record["name"]), str(record["source_id"]), str(record["check_status"])]
            ).lower()
            if query and query not in searchable:
                continue
            url = str(record["official_url"] or "") or official_law_url(
                str(record["kind"]), str(record["name"])
            )
            row = self.managed_table.rowCount()
            self.managed_table.insertRow(row)
            values = [
                "사용" if record["enabled"] else "중지",
                record["kind"],
                record["name"],
                record["source_id"],
                record["last_revision_date"],
                record["last_enforcement_date"],
                record["check_status"],
                record["last_checked_at"],
            ]
            self._fill_row(self.managed_table, row, values, url, int(record["id"]))
            if not record["enabled"]:
                for column in range(self.managed_table.columnCount()):
                    self.managed_table.item(row, column).setForeground(QColor("#8a97a6"))
            if str(record["check_status"]).startswith("실패"):
                self.managed_table.item(row, 6).setForeground(QColor("#bd2c2c"))

    def refresh_changes(self) -> None:
        if not hasattr(self, "changes_table"):
            return
        query = self.change_search.text().strip().lower()
        self.changes_table.setRowCount(0)
        for record in self.db.changes():
            searchable = " ".join(
                [str(record["name"]), str(record["ministry"]), str(record["revision_type"])]
            ).lower()
            if query and query not in searchable:
                continue
            row = self.changes_table.rowCount()
            self.changes_table.insertRow(row)
            self._fill_row(
                self.changes_table,
                row,
                [
                    "●" if record["is_new"] else "",
                    record["kind"],
                    record["name"],
                    record["revision_type"],
                    record["promulgation_date"],
                    record["enforcement_date"],
                    record["ministry"],
                    record["detected_at"],
                ],
                str(record["official_url"]),
                int(record["id"]),
            )
            if record["is_new"]:
                self.changes_table.item(row, 0).setForeground(QColor("#d43f3a"))

    def refresh_notices(self) -> None:
        if not hasattr(self, "notices_table"):
            return
        records = self.db.notices()
        years = sorted(
            {str(record["start_date"])[:4] for record in records if len(str(record["start_date"])) >= 4},
            reverse=True,
        )
        current_year = self.notice_year_filter.currentText()
        self.notice_year_filter.blockSignals(True)
        self.notice_year_filter.clear()
        self.notice_year_filter.addItem("전체 연도")
        self.notice_year_filter.addItems(years)
        if current_year in ["전체 연도", *years]:
            self.notice_year_filter.setCurrentText(current_year)
        self.notice_year_filter.blockSignals(False)

        query = self.notice_search.text().strip().lower()
        status_filter = self.notice_status_filter.currentText()
        year_filter = self.notice_year_filter.currentText()
        self.notices_table.setRowCount(0)
        for record in records:
            searchable = " ".join(
                [str(record["title"]), str(record["matched_item"]), str(record["ministry"]), str(record["notice_no"])]
            ).lower()
            if query and query not in searchable:
                continue
            if status_filter != "전체 상태" and str(record["status"]) != status_filter:
                continue
            if year_filter != "전체 연도" and not str(record["start_date"]).startswith(year_filter):
                continue
            row = self.notices_table.rowCount()
            self.notices_table.insertRow(row)
            self._fill_row(
                self.notices_table,
                row,
                [
                    "●" if record["is_new"] else "",
                    record["status"],
                    record["title"],
                    record["matched_item"],
                    record["ministry"],
                    record["notice_no"],
                    record["start_date"],
                    record["end_date"],
                ],
                str(record["official_url"]),
                int(record["id"]),
            )
            if record["is_new"]:
                self.notices_table.item(row, 0).setForeground(QColor("#d43f3a"))
            if str(record["status"]) == "진행 중":
                self.notices_table.item(row, 1).setForeground(QColor("#1d7a46"))

    def refresh_logs(self) -> None:
        if not hasattr(self, "logs_table"):
            return
        self.logs_table.setRowCount(0)
        for record in self.db.logs():
            row = self.logs_table.rowCount()
            self.logs_table.insertRow(row)
            self._fill_row(
                self.logs_table,
                row,
                [record["checked_at"], record["category"], record["status"], record["message"]],
            )
            if "실패" in str(record["status"]):
                self.logs_table.item(row, 2).setForeground(QColor("#bd2c2c"))

    @staticmethod
    def _fill_row(
        table: QTableWidget,
        row: int,
        values: list[Any],
        url: str = "",
        record_id: int | None = None,
    ) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value or ""))
            if column == 0:
                item.setData(ROLE_URL, url)
                item.setData(ROLE_ID, record_id)
            table.setItem(row, column, item)

    def selected_record_id(self, table: QTableWidget) -> int | None:
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        if not item:
            return None
        value = item.data(ROLE_ID)
        try:
            return int(value)
        except Exception:
            return None

    def open_table_url(self, table: QTableWidget) -> None:
        row = table.currentRow()
        if row < 0:
            QMessageBox.information(self, "확인", "열 항목을 선택하세요.")
            return
        item = table.item(row, 0)
        url = str(item.data(ROLE_URL) or "") if item else ""
        if not url:
            QMessageBox.warning(self, "링크 없음", "공식 사이트 링크를 만들 수 없습니다.")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "열기 실패", "기본 브라우저에서 링크를 열지 못했습니다.")

    def add_managed_item(self) -> None:
        dialog = ManagedItemDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self.db.add_item(**values)
        except Exception as exc:
            QMessageBox.warning(self, "추가 실패", str(exc))
            return
        self.refresh_all()

    def toggle_managed_item(self) -> None:
        item_id = self.selected_record_id(self.managed_table)
        if item_id is None:
            QMessageBox.information(self, "확인", "관리대상을 선택하세요.")
            return
        record = self.db.item(item_id)
        if not record:
            return
        self.db.set_item_enabled(item_id, not bool(record["enabled"]))
        self.refresh_managed()
        self.refresh_dashboard()

    def delete_managed_item(self) -> None:
        item_id = self.selected_record_id(self.managed_table)
        if item_id is None:
            QMessageBox.information(self, "확인", "삭제할 관리대상을 선택하세요.")
            return
        record = self.db.item(item_id)
        if not record:
            return
        answer = QMessageBox.question(
            self,
            "관리대상 삭제",
            f"'{record['name']}'을 관리대상에서 삭제하시겠습니까?\n기존 개정·입법예고 기록은 보존됩니다.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_item(item_id)
        self.refresh_all()

    def force_import(self) -> None:
        imported, sources = self.db.import_legacy_managed_items(force=True)
        self.refresh_all()
        source_text = ", ".join(sources) if sources else "확인 가능한 기존 DB 없음"
        QMessageBox.information(
            self,
            "가져오기 완료",
            f"새로 가져온 관리대상: {imported}건\n확인 DB: {source_text}",
        )

    def mark_changes_seen(self) -> None:
        self.db.mark_seen("change_events")
        self.refresh_changes()
        self.refresh_dashboard()

    def mark_notices_seen(self) -> None:
        self.db.mark_seen("legislative_notices")
        self.refresh_notices()
        self.refresh_dashboard()

    @staticmethod
    def _oc_part(value: str) -> str:
        return value.strip().split("@", 1)[0]

    def save_settings_ui(self) -> None:
        updated = DEFAULT_SETTINGS.copy()
        updated.update(self.settings)
        updated.update(
            {
                "law_oc": self._oc_part(self.setting_law_oc.text()),
                "notice_oc": self._oc_part(self.setting_notice_oc.text()),
                "law_search_url": self.setting_law_url.text().strip(),
                "notice_url": self.setting_notice_url.text().strip(),
                "company_name": self.setting_company.text().strip(),
                "reviewer": self.setting_reviewer.text().strip(),
                "request_timeout": self.setting_timeout.value(),
                "closed_notice_days": self.setting_closed_days.value(),
                "startup_check": self.setting_startup_check.isChecked(),
                "startup_delay_seconds": self.setting_delay.value(),
            }
        )
        if not updated["law_oc"] or not updated["notice_oc"]:
            QMessageBox.warning(self, "설정 확인", "두 API OC를 모두 입력하세요.")
            return
        save_settings(updated)
        self.settings = updated
        self.setting_law_oc.setText(str(updated["law_oc"]))
        self.setting_notice_oc.setText(str(updated["notice_oc"]))
        self.statusBar().showMessage("설정을 저장했습니다.", 5000)

    def open_data_folder(self) -> None:
        root = app_root()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    @Slot()
    def start_sync(self) -> None:
        if self.sync_thread and self.sync_thread.isRunning():
            self.statusBar().showMessage("이미 점검 중입니다.")
            return
        self.sync_button.setEnabled(False)
        self.sync_button.setText("확인 중...")
        self.statusBar().showMessage("API 개정 확인을 시작합니다...")
        self.sync_thread = QThread(self)
        self.sync_worker = SyncWorker(self.db, self.settings)
        self.sync_worker.moveToThread(self.sync_thread)
        self.sync_thread.started.connect(self.sync_worker.run)
        self.sync_worker.progress.connect(self.statusBar().showMessage)
        self.sync_worker.finished.connect(self._sync_finished)
        self.sync_worker.finished.connect(self.sync_thread.quit)
        self.sync_thread.finished.connect(self._sync_cleanup)
        self.sync_thread.start()

    @Slot(object)
    def _sync_finished(self, report: dict[str, Any]) -> None:
        self.refresh_all()
        self.sync_button.setEnabled(True)
        self.sync_button.setText("지금 개정 확인")
        errors = list(report.get("errors", []))
        summary = (
            f"점검 완료 {now_text()} · 법규 {report.get('checked_items', 0)}건 · "
            f"신규 개정 {report.get('new_changes', 0)}건 · 신규 입법예고 {report.get('new_notices', 0)}건"
        )
        self.statusBar().showMessage(summary)
        if errors:
            QMessageBox.warning(
                self,
                "일부 API 조회 실패",
                "\n".join(str(error) for error in errors[:20]),
            )

    @Slot()
    def _sync_cleanup(self) -> None:
        if self.sync_worker:
            self.sync_worker.deleteLater()
        if self.sync_thread:
            self.sync_thread.deleteLater()
        self.sync_worker = None
        self.sync_thread = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.sync_thread and self.sync_thread.isRunning():
            self.sync_thread.quit()
            self.sync_thread.wait(3000)
        super().closeEvent(event)


def create_application(argv: list[str]) -> QApplication:
    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(APP_VERSION)
    app.setFont(QFont("맑은 고딕", 10))
    return app
