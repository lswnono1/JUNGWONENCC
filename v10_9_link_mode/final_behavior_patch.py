from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialogButtonBox, QPushButton

from . import core, enhancement_patch, period_notice_patch


_AMENDMENT_MARKERS = (
    "일부개정",
    "전부개정",
    "개정",
    "제정",
    "폐지",
    "입법예고",
    "행정예고",
    "법률안",
    "령안",
    "규칙안",
    "고시안",
)
_SUBORDINATE_PREFIXES = ("시행령", "시행규칙")


def direct_notice_match(managed_name: str, notice_title: str) -> bool:
    """Match the exact managed enactment, not merely a parent/child law name."""
    managed = core.normalize_text(managed_name)
    title = core.normalize_text(notice_title)
    if len(managed) < 3 or len(title) < 3:
        return False
    if managed == title:
        return True

    position = title.find(managed)
    if position < 0:
        return False
    remainder = title[position + len(managed) :]
    if not remainder:
        return True

    # '건축법' 관리대상에 '건축법 시행령' 예고가 잘못 연결되는 것을 막는다.
    if any(remainder.startswith(prefix) for prefix in _SUBORDINATE_PREFIXES):
        return False

    # 법규명 뒤에 개정안·입법예고 등의 문구가 붙은 경우만 직접 연관으로 본다.
    head = remainder[:40]
    return any(marker in head for marker in _AMENDMENT_MARKERS)


def _best_direct_item(items: list[sqlite3.Row], title: str) -> sqlite3.Row | None:
    matches: list[tuple[int, int, sqlite3.Row]] = []
    normalized_title = core.normalize_text(title)
    for item in items:
        name = str(item["name"] or "")
        if not direct_notice_match(name, title):
            continue
        normalized_name = core.normalize_text(name)
        exact_prefix = 1 if normalized_title.startswith(normalized_name) else 0
        matches.append((exact_prefix, len(normalized_name), item))
    if not matches:
        return None
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return matches[0][2]


def _notice_key(record: dict[str, str]) -> str:
    sequence = str(record.get("sequence", "")).strip()
    title = str(record.get("title", "")).strip()
    start = str(record.get("start_date", "")).strip()
    notice_no = str(record.get("notice_no", "")).strip()
    return "|".join((sequence, notice_no, core.normalize_text(title), start))


def _fetch_notice_records(
    monitor: core.Monitor,
    items: list[sqlite3.Row],
) -> tuple[list[dict[str, str]], int, list[str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    successful_calls = 0
    errors: list[str] = []
    page_size = 200

    def add_payload(raw: bytes) -> int:
        nonlocal successful_calls
        successful_calls += 1
        payload = core.parse_payload(raw)
        parsed = monitor._notice_records(payload, "진행 중")
        added = 0
        for record in parsed:
            key = _notice_key(record)
            if not key or key in seen:
                continue
            seen.add(key)
            records.append(record)
            added += 1
        return added

    endpoints = enhancement_patch._notice_endpoint_candidates(
        str(monitor.settings.get("notice_url", ""))
    )
    oc = str(monitor.settings.get("notice_oc", ""))

    # 기본 응답의 첫 페이지만 읽지 않고, 여러 공공 API의 페이지 파라미터를 함께 전달한다.
    for endpoint in endpoints:
        endpoint_had_response = False
        for page in range(1, 51):
            params: dict[str, Any] = {
                "OC": oc,
                "diff": 0,
                "pageIndex": page,
                "recordCountPerPage": page_size,
                "page": page,
                "display": page_size,
                "pageNo": page,
                "numOfRows": page_size,
            }
            try:
                raw = monitor.http.get(endpoint, params)
                endpoint_had_response = True
                added = add_payload(raw)
                # 페이지 파라미터가 무시되어 같은 첫 페이지가 반복되면 즉시 중단한다.
                if page > 1 and added == 0:
                    break
                payload = core.parse_payload(raw)
                current = monitor._notice_records(payload, "진행 중")
                if not current:
                    break
                if len(current) < page_size and page == 1:
                    # 기본 한도가 page_size보다 작은 API일 수 있어 2페이지까지 확인한다.
                    continue
            except Exception as exc:
                errors.append(f"{endpoint} page {page}: {exc}")
                break
        if endpoint_had_response and records:
            # 두 공식 도메인이 동일 자료를 제공하므로 정상 응답을 얻으면 중복 호출을 줄인다.
            break

    # 전체 목록에서 누락된 관리대상은 법규명 검색 파라미터로 한 번 더 조회한다.
    unmatched = [
        item
        for item in items
        if _best_direct_item([item], " ".join(str(r.get("title", "")) for r in records)) is None
    ]
    for item in unmatched[:50]:
        name = str(item["name"] or "").strip()
        if not name:
            continue
        for endpoint in endpoints:
            params = {
                "OC": oc,
                "diff": 0,
                "lsNm": name,
                "query": name,
                "searchKeyword": name,
                "pageIndex": 1,
                "recordCountPerPage": page_size,
                "page": 1,
                "display": page_size,
            }
            try:
                raw = monitor.http.get(endpoint, params)
                add_payload(raw)
                break
            except Exception as exc:
                errors.append(f"{endpoint} query {name}: {exc}")

    return records, successful_calls, errors


def apply_core_patch() -> None:
    if getattr(core.Monitor, "_final_behavior_patch_applied", False):
        return

    # period_notice_patch의 기존 재분류 로직도 강화된 직접 연관 판정 함수를 사용한다.
    period_notice_patch.direct_notice_match = direct_notice_match
    period_notice_patch._best_direct_item = _best_direct_item

    original_sync_item = core.Monitor._sync_item

    def sync_item_with_future_enforcement(
        self: core.Monitor,
        item: sqlite3.Row,
    ) -> bool:
        changed = bool(original_sync_item(self, item))
        if changed:
            return True

        refreshed = self.db.item(int(item["id"]))
        if refreshed is None:
            return False
        enforcement_text = str(refreshed["last_enforcement_date"] or "")
        enforcement = core.parse_date(enforcement_text)
        if enforcement is None or enforcement <= date.today():
            return False

        source_id = str(refreshed["source_id"] or "")
        name = str(refreshed["name"] or "")
        revision_date = str(refreshed["last_revision_date"] or "")
        revision_type = str(refreshed["last_revision_type"] or "")
        event_key = ":".join(
            [
                "future-effective",
                str(refreshed["kind"] or "법령"),
                source_id or core.normalize_text(name),
                revision_date or "날짜미상",
                enforcement_text,
            ]
        )
        with self.db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM change_events WHERE event_key=? LIMIT 1",
                (event_key,),
            ).fetchone()
        if exists:
            return False

        label = "시행예정"
        if revision_type:
            label += f"·{revision_type}"
        inserted = self.db.upsert_change(
            {
                "event_key": event_key,
                "managed_item_id": int(refreshed["id"]),
                "kind": str(refreshed["kind"] or "법령"),
                "name": name,
                "source_id": source_id,
                "revision_type": label,
                "promulgation_date": revision_date,
                "enforcement_date": enforcement_text,
                "ministry": "",
                "official_url": str(refreshed["official_url"] or ""),
                "is_actual_change": True,
            },
            is_new=True,
        )
        if inserted:
            self.db.add_log(
                "법규",
                "시행예정 확인",
                f"{name}: {enforcement_text} 시행 예정",
            )
        return bool(inserted)

    def sync_complete_active_notices(
        self: core.Monitor,
        items: list[sqlite3.Row],
    ) -> int:
        if not items:
            return 0
        records, successful_calls, errors = _fetch_notice_records(self, items)
        if successful_calls == 0:
            raise RuntimeError("입법예고 API 연결 실패: " + "; ".join(errors))

        baseline = self.db.get_meta("notice_baseline_done", "") != "1"
        inserted_new = 0
        direct_count = 0
        for record in records:
            title = str(record.get("title", "")).strip()
            if not title:
                continue
            if not period_notice_patch._notice_is_active(
                "진행 중",
                str(record.get("end_date", "")),
            ):
                continue
            matched = _best_direct_item(items, title)
            if matched is None:
                continue
            direct_count += 1
            sequence = str(record.get("sequence", ""))
            key = "notice:" + ":".join(
                [
                    sequence
                    or str(record.get("notice_no", ""))
                    or core.normalize_text(title),
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
        self.db.reclassify_notice_rows()
        self.db.add_log(
            "입법예고",
            "성공",
            f"API {successful_calls}회, 수신 {len(records)}건, 직접 연관 {direct_count}건, 신규 {inserted_new}건",
        )
        return inserted_new

    core.Monitor._sync_item = sync_item_with_future_enforcement  # type: ignore[method-assign]
    core.Monitor._sync_notices = sync_complete_active_notices  # type: ignore[method-assign]
    core.Monitor._final_behavior_patch_applied = True  # type: ignore[attr-defined]


def apply_ui_patch() -> None:
    from . import ui

    if getattr(ui.MainWindow, "_final_behavior_ui_applied", False):
        return

    dialog_class = ui.ManagedItemDialog
    original_dialog_init = dialog_class.__init__
    original_event_filter = getattr(dialog_class, "eventFilter", None)

    def dialog_init(self, *args, **kwargs) -> None:
        original_dialog_init(self, *args, **kwargs)
        # QDialog의 기본 확인 버튼이 Enter를 가로채 관리대상이 즉시 추가되지 않도록 한다.
        try:
            self.name.returnPressed.disconnect()
        except Exception:
            pass
        self.name.installEventFilter(self)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        for box in self.findChildren(QDialogButtonBox):
            ok_button = box.button(QDialogButtonBox.StandardButton.Ok)
            if ok_button is not None:
                ok_button.setAutoDefault(False)
                ok_button.setDefault(False)

    def event_filter(self, watched, event) -> bool:
        if watched is getattr(self, "name", None) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.start_search()
                event.accept()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.reject()
                event.accept()
                return True
        if original_event_filter is not None:
            return bool(original_event_filter(self, watched, event))
        return False

    original_refresh_changes = ui.MainWindow.refresh_changes

    def refresh_changes_with_future(self) -> None:
        if not hasattr(self, "changes_table"):
            return
        start = self.change_start_date.date().toString("yyyy-MM-dd")
        end = self.change_end_date.date().toString("yyyy-MM-dd")
        if start > end:
            start, end = end, start
        self.change_period_label.setText(
            f"조회기간: {start} ~ {end} · 시행일이 오늘 이후인 법령은 기간과 관계없이 포함"
        )
        query = self.change_search.text().strip().lower()
        today = date.today()
        self.changes_table.setRowCount(0)
        for record in self.db.actual_changes(limit=10000):
            promulgation = core.parse_date(record["promulgation_date"])
            enforcement = core.parse_date(record["enforcement_date"])
            in_period = bool(
                (promulgation and start <= promulgation.isoformat() <= end)
                or (enforcement and start <= enforcement.isoformat() <= end)
            )
            future = bool(enforcement and enforcement > today)
            if not in_period and not future:
                continue
            searchable = " ".join(
                [
                    str(record["name"]),
                    str(record["ministry"]),
                    str(record["revision_type"]),
                ]
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

    dialog_class.__init__ = dialog_init  # type: ignore[method-assign]
    dialog_class.eventFilter = event_filter  # type: ignore[method-assign]
    ui.MainWindow.refresh_changes = refresh_changes_with_future  # type: ignore[method-assign]
    ui.MainWindow._final_behavior_ui_applied = True  # type: ignore[attr-defined]
