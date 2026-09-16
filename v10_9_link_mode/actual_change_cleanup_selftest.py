from __future__ import annotations

import tempfile
from pathlib import Path

from .connection_patch import install_connection_patch
from .migration_patch import install_patch

install_connection_patch()
install_patch()

from .api_search_patch import apply_patch as apply_api_search_patch
from .enhancement_patch import apply_core_enhancements

apply_api_search_patch()
apply_core_enhancements()

from .actual_change_cleanup_patch import (
    _CLEANUP_META_KEY,
    apply_patch as apply_actual_change_cleanup_patch,
)

apply_actual_change_cleanup_patch()

from .core import Database


def _change(item_id: int, event_key: str, date_value: str) -> dict:
    return {
        "event_key": event_key,
        "managed_item_id": item_id,
        "kind": "법령",
        "name": "시험 법률",
        "source_id": "1000",
        "revision_type": "일부개정",
        "promulgation_date": date_value,
        "enforcement_date": date_value,
        "ministry": "법제처",
        "official_url": "https://www.law.go.kr/법령/시험법률",
        "is_actual_change": True,
    }


def run_all() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cleanup.db"
        db = Database(path)
        item_id = db.add_item("법령", "시험 법률")

        # 이미 패치된 DB에 과거 오분류 행이 있다고 가정하고 정리 마커를 되돌린다.
        db.upsert_change(
            _change(item_id, "법령:1000:2023-01-03:일부개정", "2023-01-03"),
            is_new=True,
        )
        db.set_meta(_CLEANUP_META_KEY, "0")

        repaired = Database(path)
        assert repaired.actual_changes() == []
        assert repaired.counts()["new_changes"] == 0
        with repaired.connect() as conn:
            row = conn.execute(
                "SELECT is_actual_change,is_new FROM change_events "
                "WHERE event_key=?",
                ("법령:1000:2023-01-03:일부개정",),
            ).fetchone()
            assert row is not None
            assert int(row[0]) == 0
            assert int(row[1]) == 0
            assert repaired.get_meta(_CLEANUP_META_KEY, "",) == "1"

        # 정리 이후 새로 감지된 실제 변경은 다시 표시되어야 한다.
        repaired.upsert_change(
            _change(item_id, "법령:1000:2026-07-16:일부개정", "2026-07-16"),
            is_new=True,
        )
        reopened = Database(path)
        rows = reopened.actual_changes()
        assert len(rows) == 1
        assert rows[0]["promulgation_date"] == "2026-07-16"
        assert reopened.counts()["new_changes"] == 1


if __name__ == "__main__":
    run_all()
