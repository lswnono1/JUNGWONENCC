from __future__ import annotations

import re
import sqlite3
import urllib.error
from datetime import date, datetime, timedelta
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import core
from .api_search_patch import _find_best, _record_name, _score, _search_variants


_ORIGINAL_INITIALIZE = core.Database.initialize
_ORIGINAL_UPSERT_CHANGE = core.Database.upsert_change
_ORIGINAL_COUNTS = core.Database.counts


def _iso_day(value: Any) -> date | None:
    text = core.normalize_date(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _enhanced_initialize(self: core.Database) -> None:
    _ORIGINAL_INITIALIZE(self)
    with self.connect() as conn:
        columns = core.Database._columns(conn, "change_events")
        if "is_actual_change" not in columns:
            conn.execute(
                "ALTER TABLE change_events ADD COLUMN is_actual_change "
                "INTEGER NOT NULL DEFAULT 0"
            )
            # 기존 자료 중 아직 신규 표시가 남아 있는 항목만 실제 변경으로 보존한다.
            conn.execute(
                "UPDATE change_events SET is_actual_change=1 WHERE is_new=1"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_actual_change_dates "
            "ON change_events(is_actual_change, promulgation_date DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_actual_change_new "
            "ON change_events(is_actual_change, is_new, id DESC)"
        )
        self.set_meta("schema_version", "4", conn=conn)


def _enhanced_upsert_change(
    self: core.Database,
    data: dict[str, Any],
    is_new: bool,
) -> bool:
    inserted = _ORIGINAL_UPSERT_CHANGE(self, data, is_new)
    actual = 1 if bool(data.get("is_actual_change", False)) else 0
    with self.connect() as conn:
        conn.execute(
            "UPDATE change_events SET is_actual_change=?, "
            "is_new=CASE WHEN ?=1 THEN 1 ELSE is_new END WHERE event_key=?",
            (actual, 1 if is_new else 0, str(data.get("event_key", ""))),
        )
    return inserted


def _actual_changes(
    self: core.Database,
    limit: int = 1000,
    *,
    start_date: str = "",
    end_date: str = "",
    only_new: bool = False,
) -> list[sqlite3.Row]:
    clauses = ["is_actual_change=1"]
    params: list[Any] = []
    if start_date:
        clauses.append("promulgation_date>=?")
        params.append(start_date)
    if end_date:
        clauses.append("promulgation_date<=?")
        params.append(end_date)
    if only_new:
        clauses.append("is_new=1")
    params.append(int(limit))
    sql = (
        "SELECT * FROM change_events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY promulgation_date DESC, id DESC LIMIT ?"
    )
    with self.connect() as conn:
        return list(conn.execute(sql, tuple(params)).fetchall())


def _enhanced_counts(self: core.Database) -> dict[str, int]:
    result = _ORIGINAL_COUNTS(self)
    with self.connect() as conn:
        result["changes"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM change_events WHERE is_actual_change=1"
            ).fetchone()[0]
        )
        result["new_changes"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM change_events "
                "WHERE is_actual_change=1 AND is_new=1"
            ).fetchone()[0]
        )
    return result


def _enhanced_sync_item(self: core.Monitor, item: sqlite3.Row) -> bool:
    best, result_target, best_score = _find_best(self, item)
    if not best or best_score < 45:
        self.db.update_item_check(
            int(item["id"]),
            event_key=str(item["last_event_key"] or ""),
            revision_type=str(item["last_revision_type"] or ""),
            revision_date=str(item["last_revision_date"] or ""),
            enforcement_date=str(item["last_enforcement_date"] or ""),
            source_id=str(item["source_id"] or ""),
            official_url=str(item["official_url"] or ""),
            status="검색 결과 없음",
        )
        self.db.add_log("법규", "주의", f"{item['name']}: 검색 결과 없음")
        return False

    found_kind = "행정규칙" if result_target == "admrul" else "법령"
    name = _record_name(best) or str(item["name"])
    source_id = core.dict_value(
        best,
        "법령ID",
        "행정규칙ID",
        "법령일련번호",
        "행정규칙일련번호",
        "MST",
        "id",
    ) or str(item["source_id"] or "")
    revision_date = core.normalize_date(
        core.dict_value(best, "공포일자", "발령일자", "개정일자", "promulgationDate")
    )
    enforcement_date = core.normalize_date(
        core.dict_value(best, "시행일자", "enforcementDate")
    )
    revision_type = core.dict_value(
        best,
        "제개정구분명",
        "제개정구분",
        "개정구분",
        "revisionType",
    )
    ministry = core.dict_value(
        best,
        "소관부처명",
        "소관부처",
        "부처명",
        "ministry",
    )
    supplied_link = core.dict_value(
        best,
        "법령상세링크",
        "행정규칙상세링크",
        "상세링크",
        "link",
        "url",
    )
    url = core.official_law_url(
        found_kind,
        name,
        supplied_link or str(item["official_url"] or ""),
    )
    event_key = ":".join(
        [
            found_kind,
            source_id or core.normalize_text(name),
            revision_date or "날짜미상",
            revision_type or "구분미상",
        ]
    )
    previous_key = str(item["last_event_key"] or "")
    status = "정상" if found_kind == str(item["kind"]) else f"정상·{found_kind} 자동판별"

    # 최초 확인은 기준선만 저장하고 개정사항 메뉴에는 표시하지 않는다.
    if not previous_key:
        self.db.update_item_check(
            int(item["id"]),
            event_key=event_key,
            revision_type=revision_type,
            revision_date=revision_date,
            enforcement_date=enforcement_date,
            source_id=source_id,
            official_url=url,
            status=status,
        )
        self.db.add_log("법규", "성공", f"{name} 기준선 저장")
        return False

    if previous_key == event_key:
        self.db.update_item_check(
            int(item["id"]),
            event_key=event_key,
            revision_type=revision_type,
            revision_date=revision_date,
            enforcement_date=enforcement_date,
            source_id=source_id,
            official_url=url,
            status=status,
        )
        self.db.add_log("법규", "성공", f"{name} 변경 없음")
        return False

    self.db.upsert_change(
        {
            "event_key": event_key,
            "managed_item_id": int(item["id"]),
            "kind": found_kind,
            "name": name,
            "source_id": source_id,
            "revision_type": revision_type,
            "promulgation_date": revision_date,
            "enforcement_date": enforcement_date,
            "ministry": ministry,
            "official_url": url,
            "is_actual_change": True,
        },
        is_new=True,
    )
    self.db.update_item_check(
        int(item["id"]),
        event_key=event_key,
        revision_type=revision_type,
        revision_date=revision_date,
        enforcement_date=enforcement_date,
        source_id=source_id,
        official_url=url,
        status=status,
    )
    self.db.add_log("법규", "개정 확인", f"{name}: {revision_type} {revision_date}")
    return True


def _dict_notice_records(value: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if isinstance(value, dict):
        lowered = {str(key).lower() for key in value}
        if "lsnm" in lowered or "oglmppseq" in lowered:
            output.append(value)
        for child in value.values():
            output.extend(_dict_notice_records(child))
    elif isinstance(value, list):
        for child in value:
            output.extend(_dict_notice_records(child))
    return output


def _enhanced_notice_records(
    self: core.Monitor,
    payload: Any,
    forced_status: str,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    if isinstance(payload, (dict, list)):
        records = _dict_notice_records(payload)
        for record in records:
            title = core.dict_value(
                record,
                "lsNm",
                "법령안명",
                "입법예고명",
                "제목",
                "title",
            )
            if not title:
                continue
            output.append(
                {
                    "title": title,
                    "ministry": core.dict_value(
                        record,
                        "asndOfiNm",
                        "cptOfiOrgNm",
                        "소관부처",
                        "부처명",
                        "소관부처명",
                    ),
                    "notice_no": core.dict_value(
                        record,
                        "pntcNo",
                        "공고번호",
                        "announceNo",
                        "noticeNo",
                    ),
                    "start_date": core.normalize_date(
                        core.dict_value(
                            record,
                            "stYd",
                            "pntcDt",
                            "공고일자",
                            "시작일자",
                            "예고시작일",
                        )
                    ),
                    "end_date": core.normalize_date(
                        core.dict_value(
                            record,
                            "edYd",
                            "마감일자",
                            "종료일자",
                            "예고종료일",
                        )
                    ),
                    "sequence": core.dict_value(
                        record,
                        "ogLmPpSeq",
                        "입법예고ID",
                        "lmPpSeq",
                        "id",
                    ),
                    "official_url": core.dict_value(
                        record,
                        "상세링크",
                        "link",
                        "url",
                        "상세페이지",
                    ),
                    "status": forced_status,
                }
            )
        return output

    if hasattr(payload, "iter"):
        for node in payload.iter():
            child_names = {core.local_name(child.tag) for child in list(node)}
            if not child_names.intersection({"lsnm", "법령안명", "입법예고명"}):
                continue
            title = core.xml_child_value(
                node,
                "lsNm",
                "법령안명",
                "입법예고명",
                "제목",
                "title",
            )
            if not title:
                continue
            output.append(
                {
                    "title": title,
                    "ministry": core.xml_child_value(
                        node,
                        "asndOfiNm",
                        "cptOfiOrgNm",
                        "소관부처",
                        "부처명",
                        "소관부처명",
                    ),
                    "notice_no": core.xml_child_value(
                        node,
                        "pntcNo",
                        "공고번호",
                        "announceNo",
                        "noticeNo",
                    ),
                    "start_date": core.normalize_date(
                        core.xml_child_value(
                            node,
                            "stYd",
                            "pntcDt",
                            "공고일자",
                            "시작일자",
                            "예고시작일",
                        )
                    ),
                    "end_date": core.normalize_date(
                        core.xml_child_value(
                            node,
                            "edYd",
                            "마감일자",
                            "종료일자",
                            "예고종료일",
                        )
                    ),
                    "sequence": core.xml_child_value(
                        node,
                        "ogLmPpSeq",
                        "입법예고ID",
                        "lmPpSeq",
                        "id",
                    ),
                    "official_url": core.xml_child_value(
                        node,
                        "상세링크",
                        "link",
                        "url",
                        "상세페이지",
                    ),
                    "status": forced_status,
                }
            )
    return output


def _notice_detail_url(sequence: str, supplied: str = "") -> str:
    resolved = core.absolute_url("https://opinion.lawmaking.go.kr", supplied)
    if resolved:
        return resolved
    if sequence:
        # 현재 국민참여입법센터 상세 경로. 구형 도메인도 이 주소로 연결된다.
        return "https://opinion.lawmaking.go.kr/gcom/ogLmPp/" + str(sequence)
    return "https://opinion.lawmaking.go.kr/gcom/ogLmPp"


def _notice_endpoint_candidates(configured: str) -> list[str]:
    values = [
        str(configured or "").strip(),
        "https://opinion.lawmaking.go.kr/rest/ogLmPp.xml",
        "https://www.lawmaking.go.kr/rest/ogLmPp.xml",
    ]
    output: list[str] = []
    for value in values:
        if not value:
            continue
        if value.endswith("/ogLmPp"):
            value += ".xml"
        if value not in output:
            output.append(value)
    return output


def _enhanced_sync_notices(
    self: core.Monitor,
    items: list[sqlite3.Row],
) -> int:
    if not items:
        return 0
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    endpoint_errors: list[str] = []
    successful_calls = 0

    for diff, forced_status in ((0, "진행 중"), (1, "종료")):
        parsed: list[dict[str, str]] = []
        last_error: Exception | None = None
        for endpoint in _notice_endpoint_candidates(str(self.settings.get("notice_url", ""))):
            try:
                raw = self.http.get(
                    endpoint,
                    {"OC": self.settings.get("notice_oc", ""), "diff": diff},
                )
                payload = core.parse_payload(raw)
                parsed = self._notice_records(payload, forced_status)
                successful_calls += 1
                if parsed:
                    break
            except Exception as exc:
                last_error = exc
                continue
        if last_error and not parsed:
            endpoint_errors.append(f"{forced_status}: {last_error}")
        for record in parsed:
            rough = "|".join(
                [
                    record.get("sequence", ""),
                    record.get("notice_no", ""),
                    record.get("title", ""),
                    record.get("start_date", ""),
                ]
            )
            if rough in seen:
                continue
            seen.add(rough)
            records.append(record)

    if successful_calls == 0:
        raise RuntimeError("입법예고 API 연결 실패: " + "; ".join(endpoint_errors))

    baseline = self.db.get_meta("notice_baseline_done", "") != "1"
    inserted_new = 0
    cutoff = date.today() - timedelta(
        days=max(0, core.safe_int(self.settings.get("closed_notice_days"), 45))
    )
    for record in records:
        title = str(record.get("title", "")).strip()
        if not title:
            continue
        if record.get("status") == "종료":
            end_value = core.parse_date(record.get("end_date"))
            if end_value and end_value < cutoff:
                continue
        scored = [
            (core.match_score(str(item["name"]), title), item)
            for item in items
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored or scored[0][0] < 20:
            continue
        matched = scored[0][1]
        sequence = str(record.get("sequence", ""))
        key = "notice:" + ":".join(
            [
                sequence or str(record.get("notice_no", "")) or core.normalize_text(title),
                str(record.get("start_date", "")) or "날짜미상",
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
                "official_url": _notice_detail_url(
                    sequence,
                    str(record.get("official_url", "")),
                ),
                "matched_item": str(matched["name"]),
            },
            is_new=not baseline,
        )
        if inserted and not baseline:
            inserted_new += 1
    self.db.set_meta("notice_baseline_done", "1")
    self.db.add_log(
        "입법예고",
        "성공",
        f"API {successful_calls}회, 수신 {len(records)}건, 관련 신규 {inserted_new}건",
    )
    return inserted_new


def search_catalog(
    settings: dict[str, Any],
    query: str,
    kind_filter: str = "전체",
    http: core.HttpClient | None = None,
) -> list[dict[str, str]]:
    clean_query = " ".join(str(query or "").split()).strip()
    if len(core.normalize_text(clean_query)) < 2:
        return []
    client = http or core.HttpClient(core.safe_int(settings.get("request_timeout"), 30))
    targets: list[tuple[str, str]] = []
    if kind_filter in {"전체", "법령"}:
        targets.append(("eflaw", "법령"))
    if kind_filter in {"전체", "행정규칙"}:
        targets.append(("admrul", "행정규칙"))
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for target, kind in targets:
        for variant in _search_variants(clean_query)[:3]:
            params: dict[str, Any] = {
                "OC": settings.get("law_oc", ""),
                "target": target,
                "type": "JSON",
                "search": 1,
                "query": variant,
                "display": 100,
                "page": 1,
                "sort": "ldes",
            }
            if target == "eflaw":
                params["nw"] = "1,2,3"
            raw = client.get(str(settings.get("law_search_url", "")), params)
            payload = core.parse_payload(raw)
            records = list(core.iter_json_records(payload)) if isinstance(payload, (dict, list)) else []
            for record in records:
                name = _record_name(record)
                if not name:
                    continue
                score = _score(clean_query, name)
                if score < 20:
                    continue
                source_id = core.dict_value(
                    record,
                    "법령ID",
                    "행정규칙ID",
                    "법령일련번호",
                    "행정규칙일련번호",
                    "MST",
                    "id",
                )
                key = f"{kind}:{source_id or core.normalize_text(name)}"
                if key in seen:
                    continue
                seen.add(key)
                ministry = core.dict_value(
                    record,
                    "소관부처명",
                    "소관부처",
                    "부처명",
                )
                revision_date = core.normalize_date(
                    core.dict_value(record, "공포일자", "발령일자", "개정일자")
                )
                enforcement_date = core.normalize_date(
                    core.dict_value(record, "시행일자")
                )
                supplied = core.dict_value(
                    record,
                    "법령상세링크",
                    "행정규칙상세링크",
                    "상세링크",
                    "link",
                    "url",
                )
                output.append(
                    {
                        "kind": kind,
                        "name": name,
                        "source_id": source_id,
                        "ministry": ministry,
                        "revision_date": revision_date,
                        "enforcement_date": enforcement_date,
                        "official_url": core.official_law_url(kind, name, supplied),
                        "score": str(score),
                    }
                )
            if any(int(row["score"]) >= 100 for row in output if row["kind"] == kind):
                break
    output.sort(key=lambda row: (-int(row["score"]), row["kind"], row["name"]))
    return output[:100]


class CatalogSearchWorker(QObject):
    finished = Signal(object, str)

    def __init__(self, settings: dict[str, Any], query: str, kind_filter: str):
        super().__init__()
        self.settings = settings.copy()
        self.query = query
        self.kind_filter = kind_filter

    @Slot()
    def run(self) -> None:
        try:
            rows = search_catalog(self.settings, self.query, self.kind_filter)
            self.finished.emit(rows, "")
        except Exception as exc:
            self.finished.emit([], str(exc))


class ManagedCatalogDialog(QDialog):
    ROLE_RECORD = int(Qt.ItemDataRole.UserRole) + 31

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = getattr(parent, "settings", core.load_settings()).copy()
        self._thread: QThread | None = None
        self._worker: CatalogSearchWorker | None = None
        self._selected: dict[str, str] | None = None
        self.setWindowTitle("관리대상 검색·추가")
        self.resize(980, 620)
        layout = QVBoxLayout(self)

        note = QLabel(
            "법규명을 정확히 몰라도 핵심어를 입력하면 국가법령정보센터의 현행법령과 행정규칙을 함께 검색합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        search_row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItems(["전체", "법령", "행정규칙"])
        self.name = QLineEdit()
        self.name.setPlaceholderText("예: 화재안전, 건축물 설비, 소방시설")
        self.search_button = QPushButton("법제처 검색")
        self.search_button.clicked.connect(self.start_search)
        search_row.addWidget(self.kind)
        search_row.addWidget(self.name, 1)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        self.status = QLabel("두 글자 이상 입력하세요.")
        layout.addWidget(self.status)

        self.results = QTableWidget(0, 7)
        self.results.setHorizontalHeaderLabels(
            ["구분", "법규명", "소관부처", "공포·발령일", "시행일", "ID", "일치도"]
        )
        self.results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results.verticalHeader().setVisible(False)
        self.results.horizontalHeader().setStretchLastSection(False)
        widths = [90, 390, 150, 110, 110, 120, 70]
        for index, width in enumerate(widths):
            self.results.setColumnWidth(index, width)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.results.cellDoubleClicked.connect(lambda _r, _c: self._accept())
        layout.addWidget(self.results, 1)

        self.manual_name = QLineEdit()
        self.manual_name.setPlaceholderText("검색 결과가 없을 때 직접 입력할 관리대상명")
        layout.addWidget(self.manual_name)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("선택 항목 추가")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(700)
        self._debounce.timeout.connect(self.start_search)
        self.name.textEdited.connect(lambda _text: self._debounce.start())
        self.kind.currentTextChanged.connect(lambda _text: self._debounce.start())

    @Slot()
    def start_search(self) -> None:
        query = self.name.text().strip()
        if len(core.normalize_text(query)) < 2:
            self.status.setText("두 글자 이상 입력하세요.")
            return
        if self._thread and self._thread.isRunning():
            self.status.setText("이전 검색이 끝난 후 다시 검색합니다.")
            self._debounce.start(800)
            return
        self.search_button.setEnabled(False)
        self.status.setText("국가법령정보센터에서 유사 법규를 검색 중입니다...")
        self._thread = QThread(self)
        self._worker = CatalogSearchWorker(self.settings, query, self.kind.currentText())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._search_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._search_cleanup)
        self._thread.start()

    @Slot(object, str)
    def _search_finished(self, rows: list[dict[str, str]], error: str) -> None:
        self.results.setRowCount(0)
        if error:
            self.status.setText(f"검색 실패: {error}")
            self.search_button.setEnabled(True)
            return
        for record in rows:
            row = self.results.rowCount()
            self.results.insertRow(row)
            values = [
                record["kind"],
                record["name"],
                record["ministry"],
                record["revision_date"],
                record["enforcement_date"],
                record["source_id"],
                record["score"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if column == 0:
                    item.setData(self.ROLE_RECORD, record)
                self.results.setItem(row, column, item)
        self.status.setText(f"유사 법규 {len(rows)}건을 찾았습니다. 항목을 선택하세요.")
        self.search_button.setEnabled(True)
        if rows:
            self.results.selectRow(0)

    @Slot()
    def _search_cleanup(self) -> None:
        if self._worker:
            self._worker.deleteLater()
        if self._thread:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    def _accept(self) -> None:
        row = self.results.currentRow()
        if row >= 0:
            first = self.results.item(row, 0)
            record = first.data(self.ROLE_RECORD) if first else None
            if isinstance(record, dict):
                self._selected = dict(record)
                self.accept()
                return
        manual = self.manual_name.text().strip() or self.name.text().strip()
        if not manual:
            QMessageBox.warning(self, "확인", "검색 결과를 선택하거나 관리대상명을 입력하세요.")
            return
        kind = self.kind.currentText()
        if kind == "전체":
            kind = "법령"
        self._selected = {
            "kind": kind,
            "name": manual,
            "source_id": "",
            "official_url": "",
        }
        self.accept()

    def values(self) -> dict[str, str]:
        record = self._selected or {}
        return {
            "kind": str(record.get("kind", "법령")),
            "name": str(record.get("name", "")),
            "source_id": str(record.get("source_id", "")),
            "official_url": str(record.get("official_url", "")),
        }

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)


def should_run_daily_auto(db: core.Database, current_day: date | None = None) -> bool:
    today = (current_day or date.today()).isoformat()
    return db.get_meta("last_auto_sync_date", "") != today


def apply_core_enhancements() -> None:
    if getattr(core.Database, "_daily_dashboard_enhanced", False):
        return
    core.DEFAULT_SETTINGS.setdefault("recent_revision_days", 30)
    core.DEFAULT_SETTINGS.setdefault("auto_check_once_daily", True)
    core.Database.initialize = _enhanced_initialize  # type: ignore[method-assign]
    core.Database.upsert_change = _enhanced_upsert_change  # type: ignore[method-assign]
    core.Database.actual_changes = _actual_changes  # type: ignore[attr-defined]
    core.Database.counts = _enhanced_counts  # type: ignore[method-assign]
    core.Monitor._sync_item = _enhanced_sync_item  # type: ignore[method-assign]
    core.Monitor._notice_records = _enhanced_notice_records  # type: ignore[method-assign]
    core.Monitor._sync_notices = _enhanced_sync_notices  # type: ignore[method-assign]
    core.Database._daily_dashboard_enhanced = True  # type: ignore[attr-defined]


def apply_ui_enhancements() -> None:
    from . import ui

    if getattr(ui.MainWindow, "_daily_dashboard_enhanced", False):
        return

    original_card = ui.MetricCard
    original_settings_page = ui.MainWindow._build_settings_page
    original_save_settings = ui.MainWindow.save_settings_ui
    original_start_sync = ui.MainWindow.start_sync

    class ClickableMetricCard(original_card):
        clicked = Signal()

        def __init__(self, title: str, parent: QWidget | None = None):
            super().__init__(title, parent)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("클릭하면 해당 항목을 팝업으로 표시합니다.")

        def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
            super().mouseReleaseEvent(event)

    class RecordListDialog(QDialog):
        def __init__(
            self,
            parent: QWidget,
            title: str,
            headers: list[str],
            rows: list[tuple[list[Any], str]],
        ):
            super().__init__(parent)
            self.setWindowTitle(title)
            self.resize(980, 560)
            layout = QVBoxLayout(self)
            summary = QLabel(f"{title}: {len(rows)}건")
            summary.setObjectName("sectionTitle")
            layout.addWidget(summary)
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setStretchLastSection(True)
            for values, url in rows:
                row = table.rowCount()
                table.insertRow(row)
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value or ""))
                    if column == 0:
                        item.setData(ui.ROLE_URL, url)
                    table.setItem(row, column, item)
            table.cellDoubleClicked.connect(
                lambda _r, _c: self._open_selected(table)
            )
            layout.addWidget(table, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            buttons.clicked.connect(self.accept)
            layout.addWidget(buttons)

        def _open_selected(self, table: QTableWidget) -> None:
            row = table.currentRow()
            if row < 0:
                return
            item = table.item(row, 0)
            url = str(item.data(ui.ROLE_URL) or "") if item else ""
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def patched_init(self) -> None:
        ui.QMainWindow.__init__(self)
        self.db = ui.Database()
        self.settings = ui.load_settings()
        self.sync_thread = None
        self.sync_worker = None
        self._sync_origin = "manual"
        self.imported_count, self.imported_sources = self.db.import_legacy_managed_items(force=False)
        self.setWindowTitle(ui.APP_TITLE)
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
            if should_run_daily_auto(self.db):
                delay = max(1, int(self.settings.get("startup_delay_seconds", 5))) * 1000
                QTimer.singleShot(delay, lambda: self.start_sync(automatic=True))
            else:
                self.statusBar().showMessage(
                    "오늘 자동점검은 이미 수행했습니다. 수동 점검은 '지금 개정 확인'으로 가능합니다."
                )

    def build_dashboard(self) -> QWidget:
        page, layout = self._page(
            "대시보드",
            "개정 여부와 입법예고 목록만 API로 확인합니다. 수치를 클릭하면 해당 항목이 팝업으로 표시됩니다.",
        )
        banner = QFrame()
        banner.setObjectName("infoBanner")
        banner_layout = QVBoxLayout(banner)
        banner_title = QLabel("DAILY LINK MODE")
        banner_title.setObjectName("bannerTitle")
        banner_text = QLabel(
            "프로그램 실행 자동점검은 하루 한 번만 수행하며, 원문은 공식 사이트에서 확인합니다."
        )
        banner_text.setWordWrap(True)
        banner_layout.addWidget(banner_title)
        banner_layout.addWidget(banner_text)
        layout.addWidget(banner)

        card_layout = QHBoxLayout()
        self.card_managed = ui.MetricCard("관리대상")
        self.card_enabled = ui.MetricCard("점검 사용")
        self.card_changes = ui.MetricCard("신규 개정")
        self.card_notices = ui.MetricCard("신규 입법예고")
        self.card_managed.clicked.connect(lambda: self.show_metric_popup("managed"))
        self.card_enabled.clicked.connect(lambda: self.show_metric_popup("enabled"))
        self.card_changes.clicked.connect(lambda: self.show_metric_popup("changes"))
        self.card_notices.clicked.connect(lambda: self.show_metric_popup("notices"))
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
        change_header = QHBoxLayout()
        change_header.addWidget(self._section_label("최근 개정사항"))
        change_header.addStretch(1)
        self.dashboard_range_label = QLabel("")
        change_header.addWidget(self.dashboard_range_label)
        recent_change_layout.addLayout(change_header)
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

    def show_metric_popup(self, metric: str) -> None:
        rows: list[tuple[list[Any], str]] = []
        if metric in {"managed", "enabled"}:
            records = self.db.items(enabled_only=(metric == "enabled"))
            for record in records:
                url = str(record["official_url"] or "") or ui.official_law_url(
                    str(record["kind"]), str(record["name"])
                )
                rows.append(
                    (
                        [
                            "사용" if record["enabled"] else "중지",
                            record["kind"],
                            record["name"],
                            record["last_revision_date"],
                            record["check_status"],
                        ],
                        url,
                    )
                )
            title = "관리대상" if metric == "managed" else "점검 사용 관리대상"
            headers = ["사용", "구분", "관리대상명", "최근 개정", "상태"]
        elif metric == "changes":
            records = self.db.actual_changes(only_new=True)
            for record in records:
                rows.append(
                    (
                        [
                            record["kind"],
                            record["name"],
                            record["revision_type"],
                            record["promulgation_date"],
                            record["enforcement_date"],
                            record["ministry"],
                        ],
                        str(record["official_url"]),
                    )
                )
            title = "신규 개정"
            headers = ["구분", "법규명", "개정유형", "공포·발령일", "시행일", "소관부처"]
        else:
            with self.db.connect() as conn:
                records = list(
                    conn.execute(
                        "SELECT * FROM legislative_notices WHERE is_new=1 "
                        "ORDER BY start_date DESC, id DESC"
                    ).fetchall()
                )
            for record in records:
                rows.append(
                    (
                        [
                            record["status"],
                            record["title"],
                            record["matched_item"],
                            record["ministry"],
                            record["start_date"],
                            record["end_date"],
                        ],
                        str(record["official_url"]),
                    )
                )
            title = "신규 입법예고"
            headers = ["상태", "입법예고명", "관련 관리대상", "소관부처", "시작일", "종료일"]
        RecordListDialog(self, title, headers, rows).exec()

    def refresh_dashboard(self) -> None:
        counts = self.db.counts()
        self.card_managed.set_value(counts["managed"])
        self.card_enabled.set_value(counts["enabled"])
        self.card_changes.set_value(counts["new_changes"])
        self.card_notices.set_value(counts["new_notices"])

        days = max(1, int(self.settings.get("recent_revision_days", 30)))
        today = date.today()
        start = today - timedelta(days=days)
        end = today + timedelta(days=days)
        self.dashboard_range_label.setText(
            f"{start.isoformat()} ~ {end.isoformat()} (±{days}일)"
        )
        changes = self.db.actual_changes(
            limit=50,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )[:10]
        self.dashboard_change_table.setRowCount(0)
        for record in changes:
            row = self.dashboard_change_table.rowCount()
            self.dashboard_change_table.insertRow(row)
            self._fill_row(
                self.dashboard_change_table,
                row,
                [
                    record["kind"],
                    record["name"],
                    record["promulgation_date"],
                    record["revision_type"],
                ],
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

    def refresh_changes(self) -> None:
        if not hasattr(self, "changes_table"):
            return
        query = self.change_search.text().strip().lower()
        self.changes_table.setRowCount(0)
        for record in self.db.actual_changes():
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

    def build_settings(self) -> QWidget:
        page = original_settings_page(self)
        panels = [child for child in page.findChildren(QFrame) if child.objectName() == "panel"]
        if panels and isinstance(panels[0].layout(), QFormLayout):
            form = panels[0].layout()
            self.setting_recent_days = QSpinBox()
            self.setting_recent_days.setRange(1, 365)
            self.setting_recent_days.setValue(
                int(self.settings.get("recent_revision_days", 30))
            )
            self.setting_recent_days.setSuffix("일")
            form.addRow("최근 개정 표시 범위(전후)", self.setting_recent_days)
            self.setting_startup_check.setText("프로그램 실행 후 하루 1회 자동점검")
        return page

    def save_settings_ui(self) -> None:
        updated = ui.DEFAULT_SETTINGS.copy()
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
                "recent_revision_days": self.setting_recent_days.value(),
                "auto_check_once_daily": True,
            }
        )
        if not updated["law_oc"] or not updated["notice_oc"]:
            QMessageBox.warning(self, "설정 확인", "두 API OC를 모두 입력하세요.")
            return
        ui.save_settings(updated)
        self.settings = updated
        self.setting_law_oc.setText(str(updated["law_oc"]))
        self.setting_notice_oc.setText(str(updated["notice_oc"]))
        self.statusBar().showMessage("설정을 저장했습니다.", 5000)
        self.refresh_dashboard()

    @Slot()
    def start_sync(self, checked: bool = False, *, automatic: bool = False) -> None:
        if self.sync_thread and self.sync_thread.isRunning():
            self.statusBar().showMessage("이미 점검 중입니다.")
            return
        self._sync_origin = "auto" if automatic else "manual"
        if automatic:
            self.db.set_meta("last_auto_sync_date", date.today().isoformat())
        original_start_sync(self)

    ui.MetricCard = ClickableMetricCard
    ui.ManagedItemDialog = ManagedCatalogDialog
    ui.MainWindow.__init__ = patched_init  # type: ignore[method-assign]
    ui.MainWindow._build_dashboard_page = build_dashboard  # type: ignore[method-assign]
    ui.MainWindow.show_metric_popup = show_metric_popup  # type: ignore[attr-defined]
    ui.MainWindow.refresh_dashboard = refresh_dashboard  # type: ignore[method-assign]
    ui.MainWindow.refresh_changes = refresh_changes  # type: ignore[method-assign]
    ui.MainWindow._build_settings_page = build_settings  # type: ignore[method-assign]
    ui.MainWindow.save_settings_ui = save_settings_ui  # type: ignore[method-assign]
    ui.MainWindow.start_sync = start_sync  # type: ignore[method-assign]
    ui.MainWindow._daily_dashboard_enhanced = True  # type: ignore[attr-defined]
