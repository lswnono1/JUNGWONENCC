from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .api_search_patch import apply_patch
from .connection_patch import install_connection_patch
from .core import Database, Monitor

install_connection_patch()


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params):
        self.calls.append(dict(params))
        payload = self.payloads.pop(0) if self.payloads else {"LawSearch": {"totalCnt": 0, "law": []}}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TargetAwareFakeHttp:
    def __init__(self, admin_payload):
        self.admin_payload = admin_payload
        self.calls = []

    def get(self, url, params):
        self.calls.append(dict(params))
        if params.get("target") == "admrul":
            payload = self.admin_payload
        else:
            payload = {"LawSearch": {"totalCnt": 0, "law": []}}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_eflaw_target() -> None:
    apply_patch()
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "test.db")
        item_id = db.add_item("법령", "건축물의 설비기준 등에 관한 규칙")
        item = db.item(item_id)
        monitor = Monitor(db, {"law_oc": "test", "law_search_url": "https://example.invalid"})
        monitor.http = FakeHttp([
            {
                "LawSearch": {
                    "totalCnt": 1,
                    "law": [
                        {
                            "법령명한글": "건축물의 설비기준 등에 관한 규칙",
                            "법령ID": "001234",
                            "공포일자": "20260701",
                            "시행일자": "20260701",
                            "제개정구분명": "일부개정",
                            "소관부처명": "국토교통부",
                            "법령상세링크": "/법령/건축물의설비기준등에관한규칙",
                        }
                    ],
                }
            }
        ])
        monitor._sync_item(item)
        assert monitor.http.calls[0]["target"] == "eflaw"
        checked = db.item(item_id)
        assert checked["check_status"] == "정상"
        assert checked["source_id"] == "001234"


def test_punctuation_and_kind_fallback() -> None:
    apply_patch()
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "test.db")
        item_id = db.add_item("법령", "화재안전영향평가 운영절차 등에 관한 규정")
        item = db.item(item_id)
        monitor = Monitor(db, {"law_oc": "test", "law_search_url": "https://example.invalid"})
        admin = {
            "AdmRulSearch": {
                "totalCnt": 1,
                "admrul": [
                    {
                        "행정규칙명": "화재안전영향평가 운영절차 등에 관한 규정",
                        "행정규칙ID": "98765",
                        "발령일자": "20260630",
                        "시행일자": "20260701",
                        "제개정구분명": "제정",
                        "소관부처명": "소방청",
                        "행정규칙상세링크": "/행정규칙/화재안전영향평가운영절차등에관한규정",
                    }
                ],
            }
        }
        monitor.http = TargetAwareFakeHttp(admin)
        monitor._sync_item(item)
        checked = db.item(item_id)
        assert "행정규칙 자동판별" in checked["check_status"]
        assert any(call["target"] == "admrul" for call in monitor.http.calls)


def test_no_result_is_not_api_failure() -> None:
    apply_patch()
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "test.db")
        item_id = db.add_item("법령", "존재하지 않는 시험 법령")
        item = db.item(item_id)
        monitor = Monitor(db, {"law_oc": "test", "law_search_url": "https://example.invalid"})
        monitor.http = FakeHttp([])
        result = monitor._sync_item(item)
        assert result is False
        assert db.item(item_id)["check_status"] == "검색 결과 없음"


def main() -> int:
    test_eflaw_target()
    test_punctuation_and_kind_fallback()
    test_no_result_is_not_api_failure()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
