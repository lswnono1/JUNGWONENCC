from __future__ import annotations

import re
import sqlite3
from typing import Any

from .core import (
    Monitor,
    dict_value,
    iter_json_records,
    normalize_date,
    normalize_text,
    official_law_url,
    parse_payload,
)


def _clean_id(value: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z_-]", "", str(value or "").strip())
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def _search_variants(name: str) -> list[str]:
    original = " ".join(str(name or "").split()).strip()
    candidates = [original]
    punctuation_spaced = re.sub(r"[·ㆍ․・∙]", " ", original)
    punctuation_spaced = " ".join(punctuation_spaced.split())
    candidates.append(punctuation_spaced)
    candidates.append(re.sub(r"\([^)]*\)|\[[^]]*\]", " ", punctuation_spaced))
    candidates.append(re.sub(r"\s+", " ", punctuation_spaced.replace(" 등에 관한 ", " ")).strip())

    # 검색 API는 긴 정식 명칭보다 핵심어가 더 잘 맞는 경우가 있어 마지막 보조 검색어를 만든다.
    tokens = [
        token
        for token in re.findall(r"[가-힣A-Za-z0-9]+", punctuation_spaced)
        if token not in {"등에", "관한", "및", "의", "운영", "절차"}
    ]
    if len(tokens) >= 3:
        candidates.append(" ".join(tokens[:5]))

    output: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = " ".join(str(candidate or "").split()).strip()
        key = normalize_text(candidate)
        if candidate and key and key not in seen:
            seen.add(key)
            output.append(candidate)
    return output


def _core_name(value: str) -> str:
    text = normalize_text(value)
    for token in (
        "법률", "시행령", "시행규칙", "규칙", "규정", "기준", "고시", "훈령", "예규",
        "등에관한", "에관한", "운영절차", "운영",
    ):
        text = text.replace(token, "")
    return text


def _score(target: str, candidate: str) -> int:
    left = normalize_text(target)
    right = normalize_text(candidate)
    if not left or not right:
        return 0
    if left == right:
        return 120
    if left in right or right in left:
        return 100
    left_core = _core_name(target)
    right_core = _core_name(candidate)
    if left_core and right_core:
        if left_core == right_core:
            return 95
        if left_core in right_core or right_core in left_core:
            return 85
    left_tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", str(target)))
    right_tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", str(candidate)))
    common = left_tokens.intersection(right_tokens)
    if common:
        coverage = len(common) / max(1, min(len(left_tokens), len(right_tokens)))
        return int(30 + coverage * 50)
    return 0


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, (dict, list)):
        return list(iter_json_records(payload))
    return []


def _record_name(record: dict[str, Any]) -> str:
    return dict_value(
        record,
        "법령명한글", "법령명", "법령명_한글", "행정규칙명", "title", "lsNm",
    )


def _request_records(self: Monitor, *, target: str, query: str = "", lid: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "OC": self.settings.get("law_oc", ""),
        "target": target,
        "type": "JSON",
        "display": 100,
        "page": 1,
        "sort": "ddes",
    }
    if target == "eflaw":
        params["nw"] = "1,2,3"
    if lid and target == "eflaw":
        params["LID"] = _clean_id(lid)
    elif query:
        params["search"] = 1
        params["query"] = query
    raw = self.http.get(str(self.settings["law_search_url"]), params)
    return _records(parse_payload(raw))


def _find_best(self: Monitor, item: sqlite3.Row) -> tuple[dict[str, Any] | None, str, int]:
    configured_kind = str(item["kind"])
    primary_target = "admrul" if configured_kind == "행정규칙" else "eflaw"
    fallback_target = "eflaw" if primary_target == "admrul" else "admrul"
    name = str(item["name"])
    source_id = str(item["source_id"] or "")

    attempts: list[tuple[str, str, str]] = []
    if primary_target == "eflaw" and source_id:
        attempts.append((primary_target, "", source_id))
    for query in _search_variants(name):
        attempts.append((primary_target, query, ""))
    # 기존 자료에서 법령/행정규칙 구분이 잘못 들어온 경우를 자동 보정하기 위한 제한적 교차검색.
    for query in _search_variants(name)[:3]:
        attempts.append((fallback_target, query, ""))

    best: dict[str, Any] | None = None
    best_target = primary_target
    best_score = 0
    seen_attempts: set[tuple[str, str, str]] = set()
    for target, query, lid in attempts:
        key = (target, normalize_text(query), _clean_id(lid))
        if key in seen_attempts:
            continue
        seen_attempts.add(key)
        records = _request_records(self, target=target, query=query, lid=lid)
        for record in records:
            score = _score(name, _record_name(record))
            if score > best_score:
                best = record
                best_target = target
                best_score = score
        if best_score >= 100:
            break
    return best, best_target, best_score


def patched_sync_item(self: Monitor, item: sqlite3.Row) -> bool:
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
    source_id = dict_value(
        best, "법령ID", "행정규칙ID", "법령일련번호", "행정규칙일련번호", "MST", "id",
    ) or str(item["source_id"] or "")
    revision_date = normalize_date(
        dict_value(best, "공포일자", "발령일자", "개정일자", "promulgationDate")
    )
    enforcement_date = normalize_date(dict_value(best, "시행일자", "enforcementDate"))
    revision_type = dict_value(
        best, "제개정구분명", "제개정구분", "개정구분", "revisionType",
    )
    ministry = dict_value(best, "소관부처명", "소관부처", "부처명", "ministry")
    supplied_link = dict_value(
        best, "법령상세링크", "행정규칙상세링크", "상세링크", "link", "url",
    )
    url = official_law_url(found_kind, name, supplied_link or str(item["official_url"] or ""))
    event_key = ":".join(
        [
            found_kind,
            source_id or normalize_text(name),
            revision_date or "날짜미상",
            revision_type or "구분미상",
        ]
    )
    previous_key = str(item["last_event_key"] or "")
    is_baseline = not previous_key
    is_changed = bool(previous_key and previous_key != event_key)
    inserted = self.db.upsert_change(
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
        },
        is_new=is_changed,
    )
    self.db.update_item_check(
        int(item["id"]),
        event_key=event_key,
        revision_type=revision_type,
        revision_date=revision_date,
        enforcement_date=enforcement_date,
        source_id=source_id,
        official_url=url,
        status="정상" if found_kind == str(item["kind"]) else f"정상·{found_kind} 자동판별",
    )
    self.db.add_log(
        "법규",
        "성공",
        f"{name} 기준선 저장" if is_baseline else f"{name} 확인",
    )
    return bool(inserted and is_changed)


def apply_patch() -> None:
    Monitor._sync_item = patched_sync_item  # type: ignore[method-assign]
