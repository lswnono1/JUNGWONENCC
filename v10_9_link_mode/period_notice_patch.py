from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QWidget,
)

from . import core
from . import enhancement_patch


_SUFFIXES = tuple(
    sorted(
        {
            "일부개정법률안",
            "전부개정법률안",
            "제정법률안",
            "폐지법률안",
            "일부개정령안",
            "전부개정령안",
            "제정령안",
            "폐지령안",
            "일부개정규칙안",
            "전부개정규칙안",
            "일부개정고시안",
            "전부개정고시안",
            "일부개정안",
            "전부개정안",
            "행정예고",
            "입법예고",
            "개정법률안",
            "개정령안",
            "개정규칙안",
            "개정고시안",
            "제정안",
            "개정안",
            "폐지안",
            "법률안",
            "령안",
            "규칙안",
            "고시안",
        },
        key=len,
        reverse=True,
    )
)


def _normalized_base(value: Any) -> str:
    text = core.normalize_text(str(value or ""))
    changed = True
    while changed and text:
        changed = False
        for suffix in _SUFFIXES:
            normalized_suffix = core.normalize_text(suffix)
            if text.endswith(normalized_suffix):
                text = text[: -len(normalized_suffix)]
                changed = True
                break
    return text


def direct_notice_match(managed_name: str, notice_title: str) -> bool:
    managed = _normalized_base(managed_name)
    title = _normalized_base(notice_title)
    if len(managed) < 4 or len(title) < 4:
        return False
    if managed == title:
        return True
    if managed in title or title in managed:
        shorter = min(len(managed), len(title))
        longer = max(len(managed), len(title))
        return shorter >= 8 and (shorter / longer) >= 0.72
    return False


def _best_direct_item(items: list[sqlite3.Row], title: str) -> sqlite3.Row | None:
    candidates: list[tuple[int, int, sqlite3.Row]] = []
    title_base = _normalized_base(title)
    for item in items:
        name = str(item["name"] or "")
        if not direct_notice_match(name, title):
            continue
        managed_base = _normalized_base(name)
        exact = 1 if managed_base == title_base else 0
        candidates.append((exact, len(managed_base), item))
    if not candidates:
        return None
    candidates.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return candidates[0][2]


def _notice_is_active(status: str, end_date: str, today: date | None = None) -> bool:
    current = today or date.today()
    status_text = str(status or "").replace(" ", "")
    if "종료" in status_text or "마감" in status_text:
        return False
    parsed_end = core.parse_date(end_date)
    if parsed_end and parsed_end < current:
        return False
    return True


def apply_core_patch() -> None:
    if getattr(core.Database, "_period_notice_patch_applied", False):
        return

    original_initialize = core.Database.initialize
    original_counts = core.Database.counts

    def reclassify_notice_rows(self: core.Database) -> None:
        items = self.items(enabled_only=True)
        today_text = date.today().isoformat()
        with self.connect() as conn:
            columns = core.Database._columns(conn, "legislative_notices")
            if "is_direct_match" not in columns:
                conn.execute(
                    "ALTER TABLE legislative_notices ADD COLUMN "
                    "is_direct_match INTEGER NOT NULL DEFAULT 0"
                )
            rows = list(conn.execute("SELECT * FROM legislative_notices").fetchall())
            for row in rows:
                matched = _best_direct_item(items, str(row["title"] or ""))
                active = _notice_is_active(
                    str(row["status"] or ""),
                    str(row["end_date"] or ""),
                )
                direct = 1 if matched is not None else 0
                matched_name = str(matched["name"]) if matched is not None else ""
                conn.execute(
                    "UPDATE legislative_notices SET is_direct_match=?, matched_item=?, "
                    "is_new=CASE WHEN ?=1 AND ?=1 THEN is_new ELSE 0 END "
                    "WHERE id=?",
                    (direct, matched_name, direct, 1 if active else 0, int(row["id"])),
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_notice_direct_active "
                "ON legislative_notices(is_direct_match,status,end_date,start_date,id)"
            )
            self.set_meta("notice_direct_filter_version", "1", conn=conn)
            self.set_meta("notice_active_reclassified_at", today_text, conn=conn)

    def patched_initialize(self: core.Database) -> None:
        original_initialize(self)
        reclassify_notice_rows(self)

    def active_notices(
        self: core.Database,
        limit: int = 2000,
        *,
        start_date: str = "",
        end_date: str = "",
        only_new: bool = False,
    ) -> list[sqlite3.Row]:
        clauses = [
            "is_direct_match=1",
            "REPLACE(status,' ','') NOT LIKE '%종료%'",
            "REPLACE(status,' ','') NOT LIKE '%마감%'",
            "(end_date='' OR end_date>=?)",
        ]
        params: list[Any] = [date.today().isoformat()]
        # 선택기간과 입법예고 기간이 서로 겹치는 항목을 표시한다.
        if start_date:
            clauses.append("(end_date='' OR end_date>=?)")
            params.append(start_date)
        if end_date:
            clauses.append("(start_date='' OR start_date<=?)")
            params.append(end_date)
        if only_new:
            clauses.append("is_new=1")
        params.append(int(limit))
        sql = (
            "SELECT * FROM legislative_notices WHERE "
            + " AND ".join(clauses)
            + " ORDER BY start_date DESC, id DESC LIMIT ?"
        )
        with self.connect() as conn:
            return list(conn.execute(sql, tuple(params)).fetchall())

    def patched_notices(self: core.Database, limit: int = 2000) -> list[sqlite3.Row]:
        return active_notices(self, limit=limit)

    def patched_counts(self: core.Database) -> dict[str, int]:
        result = original_counts(self)
        active = active_notices(self, limit=100000)
        result["notices"] = len(active)
        result["new_notices"] = sum(1 for row in active if int(row["is_new"] or 0) == 1)
        return result

    def sync_active_direct_notices(
        self: core.Monitor,
        items: list[sqlite3.Row],
    ) -> int:
        if not items:
            return 0
        parsed: list[dict[str, str]] = []
        endpoint_errors: list[str] = []
        successful_calls = 0
        for endpoint in enhancement_patch._notice_endpoint_candidates(
            str(self.settings.get("notice_url", ""))
        ):
            try:
                raw = self.http.get(
                    endpoint,
                    {"OC": self.settings.get("notice_oc", ""), "diff": 0},
                )
                payload = core.parse_payload(raw)
                parsed = self._notice_records(payload, "진행 중")
                successful_calls += 1
                if parsed:
                    break
            except Exception as exc:
                endpoint_errors.append(str(exc))
        if successful_calls == 0:
            raise RuntimeError("입법예고 API 연결 실패: " + "; ".join(endpoint_errors))

        baseline = self.db.get_meta("notice_baseline_done", "") != "1"
        inserted_new = 0
        seen: set[str] = set()
        for record in parsed:
            title = str(record.get("title", "")).strip()
            if not title:
                continue
            if not _notice_is_active("진행 중", str(record.get("end_date", ""))):
                continue
            matched = _best_direct_item(items, title)
            if matched is None:
                continue
            sequence = str(record.get("sequence", ""))
            key = "notice:" + ":".join(
                [
                    sequence
                    or str(record.get("notice_no", ""))
                    or core.normalize_text(title),
                    str(record.get("start_date", "")) or "날짜미상",
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            inserted = self.db.upsert_notice(
                {
                    "notice_key": key,
                    "title": title,
                    "ministry": record.get("ministry", ""),
                    "notice_no": record.get("notice_no", ""),
                    "start_date": record.get("start_date", ""),
                    "end_date": record.get("end_date", ""),
                    "status": "진행 중",
                    "official_url": enhancement_patch._notice_detail_url(
                        sequence,
                        str(record.get("official_url", "")),
                    ),
                    "matched_item": str(matched["name"]),
                },
                is_new=not baseline,
            )
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE legislative_notices SET is_direct_match=1, status='진행 중', "
                    "matched_item=? WHERE notice_key=?",
                    (str(matched["name"]), key),
                )
            if inserted and not baseline:
                inserted_new += 1

        self.db.set_meta("notice_baseline_done", "1")
        reclassify_notice_rows(self.db)
        self.db.add_log(
            "입법예고",
            "성공",
            f"진행 중 API {successful_calls}회, 수신 {len(parsed)}건, 직접 연관 신규 {inserted_new}건",
        )
        return inserted_new

    core.Database.initialize = patched_initialize  # type: ignore[method-assign]
    core.Database.reclassify_notice_rows = reclassify_notice_rows  # type: ignore[attr-defined]
    core.Database.active_notices = active_notices  # type: ignore[attr-defined]
    core.Database.notices = patched_notices  # type: ignore[method-assign]
    core.Database.counts = patched_counts  # type: ignore[method-assign]
    core.Monitor._sync_notices = sync_active_direct_notices  # type: ignore[method-assign]
    core.Database._period_notice_patch_applied = True  # type: ignore[attr-defined]


def _date_edit(value: QDate) -> QDateEdit:
    widget = QDateEdit(value)
    widget.setCalendarPopup(True)
    widget.setDisplayFormat("yyyy-MM-dd")
    widget.setMinimumWidth(112)
    return widget


def apply_ui_patch() -> None:
    from . import ui

    if getattr(ui.MainWindow, "_period_notice_ui_applied", False):
        return

    dialog_class = ui.ManagedItemDialog
    original_dialog_init = dialog_class.__init__

    def dialog_init(self, *args, **kwargs) -> None:
        original_dialog_init(self, *args, **kwargs)
        self.name.returnPressed.connect(self.start_search)
        self.search_button.setAutoDefault(False)
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        shortcut.activated.connect(self.reject)
        self._escape_shortcut = shortcut

    dialog_class.__init__ = dialog_init  # type: ignore[method-assign]

    def build_changes_page(self) -> QWidget:
        page, layout = self._page(
            "개정사항",
            "실제 변경으로 확인된 법규만 표시합니다. 조회기간을 지정하면 해당 공포·발령일의 개정사항을 확인할 수 있습니다.",
        )
        toolbar = QHBoxLayout()
        self.change_search = QLineEdit()
        self.change_search.setPlaceholderText("법규명·소관부처 검색")
        self.change_search.textChanged.connect(self.refresh_changes)
        toolbar.addWidget(self.change_search, 1)
        days = max(1, int(self.settings.get("recent_revision_days", 30)))
        today = QDate.currentDate()
        toolbar.addWidget(QLabel("기간"))
        self.change_start_date = _date_edit(today.addDays(-days))
        self.change_end_date = _date_edit(today.addDays(days))
        self.change_start_date.dateChanged.connect(self.refresh_changes)
        self.change_end_date.dateChanged.connect(self.refresh_changes)
        toolbar.addWidget(self.change_start_date)
        toolbar.addWidget(QLabel("~"))
        toolbar.addWidget(self.change_end_date)
        period_button = QPushButton("기간 조회")
        period_button.clicked.connect(self.refresh_changes)
        toolbar.addWidget(period_button)
        all_button = QPushButton("전체 기간")
        all_button.clicked.connect(self.show_all_change_period)
        toolbar.addWidget(all_button)
        open_button = QPushButton("공식 사이트에서 확인")
        open_button.clicked.connect(lambda: self.open_table_url(self.changes_table))
        toolbar.addWidget(open_button)
        seen_button = QPushButton("신규 표시 모두 확인")
        seen_button.clicked.connect(self.mark_changes_seen)
        toolbar.addWidget(seen_button)
        layout.addLayout(toolbar)
        self.change_period_label = QLabel("")
        layout.addWidget(self.change_period_label)
        self.changes_table = self._create_table(
            ["신규", "구분", "법규명", "개정유형", "공포·발령일", "시행일", "소관부처", "발견일시"],
            [55, 80, 360, 110, 110, 110, 150, 145],
        )
        self.changes_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.changes_table)
        )
        layout.addWidget(self.changes_table, 1)
        return page

    def show_all_change_period(self) -> None:
        self.change_start_date.setDate(QDate(2000, 1, 1))
        self.change_end_date.setDate(QDate(2100, 12, 31))
        self.refresh_changes()

    def refresh_changes(self) -> None:
        if not hasattr(self, "changes_table"):
            return
        start = self.change_start_date.date().toString("yyyy-MM-dd")
        end = self.change_end_date.date().toString("yyyy-MM-dd")
        if start > end:
            start, end = end, start
        self.change_period_label.setText(f"조회기간: {start} ~ {end}")
        query = self.change_search.text().strip().lower()
        self.changes_table.setRowCount(0)
        for record in self.db.actual_changes(start_date=start, end_date=end):
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

    def build_notices_page(self) -> QWidget:
        page, layout = self._page(
            "입법예고",
            "종료된 입법예고는 제외하고, 관리대상 법규명과 직접 연관된 진행 중 항목만 표시합니다. 기간은 입법예고 시작일과 종료일이 조회기간에 겹치는 항목을 찾습니다.",
        )
        toolbar = QHBoxLayout()
        self.notice_search = QLineEdit()
        self.notice_search.setPlaceholderText("입법예고명·관련 관리대상 검색")
        self.notice_search.textChanged.connect(self.refresh_notices)
        toolbar.addWidget(self.notice_search, 1)
        today = QDate.currentDate()
        toolbar.addWidget(QLabel("기간"))
        self.notice_start_date = _date_edit(today.addDays(-30))
        self.notice_end_date = _date_edit(today.addDays(90))
        self.notice_start_date.dateChanged.connect(self.refresh_notices)
        self.notice_end_date.dateChanged.connect(self.refresh_notices)
        toolbar.addWidget(self.notice_start_date)
        toolbar.addWidget(QLabel("~"))
        toolbar.addWidget(self.notice_end_date)
        period_button = QPushButton("기간 조회")
        period_button.clicked.connect(self.refresh_notices)
        toolbar.addWidget(period_button)
        all_button = QPushButton("전체 기간")
        all_button.clicked.connect(self.show_all_notice_period)
        toolbar.addWidget(all_button)
        open_button = QPushButton("입법예고 상세 열기")
        open_button.clicked.connect(lambda: self.open_table_url(self.notices_table))
        toolbar.addWidget(open_button)
        seen_button = QPushButton("신규 표시 모두 확인")
        seen_button.clicked.connect(self.mark_notices_seen)
        toolbar.addWidget(seen_button)
        layout.addLayout(toolbar)
        self.notice_period_label = QLabel("")
        layout.addWidget(self.notice_period_label)
        self.notices_table = self._create_table(
            ["신규", "상태", "입법예고명", "직접 연관 관리대상", "소관부처", "공고번호", "시작일", "종료일"],
            [55, 80, 400, 280, 145, 120, 100, 100],
        )
        self.notices_table.cellDoubleClicked.connect(
            lambda _row, _column: self.open_table_url(self.notices_table)
        )
        layout.addWidget(self.notices_table, 1)
        return page

    def show_all_notice_period(self) -> None:
        self.notice_start_date.setDate(QDate(2000, 1, 1))
        self.notice_end_date.setDate(QDate(2100, 12, 31))
        self.refresh_notices()

    def refresh_notices(self) -> None:
        if not hasattr(self, "notices_table"):
            return
        start = self.notice_start_date.date().toString("yyyy-MM-dd")
        end = self.notice_end_date.date().toString("yyyy-MM-dd")
        if start > end:
            start, end = end, start
        self.notice_period_label.setText(
            f"진행 중·직접 연관 입법예고 조회기간: {start} ~ {end}"
        )
        query = self.notice_search.text().strip().lower()
        records = self.db.active_notices(start_date=start, end_date=end)
        self.notices_table.setRowCount(0)
        for record in records:
            searchable = " ".join(
                [
                    str(record["title"]),
                    str(record["matched_item"]),
                    str(record["ministry"]),
                    str(record["notice_no"]),
                ]
            ).lower()
            if query and query not in searchable:
                continue
            row = self.notices_table.rowCount()
            self.notices_table.insertRow(row)
            self._fill_row(
                self.notices_table,
                row,
                [
                    "●" if record["is_new"] else "",
                    "진행 중",
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

    ui.MainWindow._build_changes_page = build_changes_page  # type: ignore[method-assign]
    ui.MainWindow.show_all_change_period = show_all_change_period  # type: ignore[attr-defined]
    ui.MainWindow.refresh_changes = refresh_changes  # type: ignore[method-assign]
    ui.MainWindow._build_notices_page = build_notices_page  # type: ignore[method-assign]
    ui.MainWindow.show_all_notice_period = show_all_notice_period  # type: ignore[attr-defined]
    ui.MainWindow.refresh_notices = refresh_notices  # type: ignore[method-assign]
    ui.MainWindow._period_notice_ui_applied = True  # type: ignore[attr-defined]
