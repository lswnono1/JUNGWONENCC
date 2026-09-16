from __future__ import annotations

from typing import Any

from . import core


_CLEANUP_META_KEY = "actual_change_false_positive_cleanup_v1"
_PREVIOUS_INITIALIZE = core.Database.initialize


def _cleanup_initialize(self: core.Database) -> None:
    """Hide legacy baseline rows that were incorrectly promoted to actual changes.

    The affected rows remain in the database for traceability, but they are no
    longer counted or displayed as new/actual amendments. The current managed
    item baseline (last_event_key and related fields) is preserved, so only a
    later API change is recorded as an actual amendment.
    """
    _PREVIOUS_INITIALIZE(self)
    with self.connect() as conn:
        done = conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (_CLEANUP_META_KEY,),
        ).fetchone()
        if done and str(done[0]) == "1":
            return

        columns = core.Database._columns(conn, "change_events")
        affected = 0
        if "is_actual_change" in columns and "is_new" in columns:
            affected = int(
                conn.execute(
                    "SELECT COUNT(*) FROM change_events "
                    "WHERE is_actual_change=1 OR is_new=1"
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE change_events SET is_actual_change=0, is_new=0"
            )

        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_CLEANUP_META_KEY, "1"),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("actual_change_tracking_reset_at", core.now_text()),
        )
        conn.execute(
            "INSERT INTO sync_log(category,status,message,checked_at) "
            "VALUES(?,?,?,?)",
            (
                "데이터정리",
                "성공",
                f"기존 오분류 개정기록 {affected}건을 신규 개정 표시에서 제외했습니다.",
                core.now_text(),
            ),
        )
        self.set_meta("schema_version", "5", conn=conn)


def apply_patch() -> None:
    if getattr(core.Database, "_actual_change_cleanup_applied", False):
        return
    core.Database.initialize = _cleanup_initialize  # type: ignore[method-assign]
    core.Database._actual_change_cleanup_applied = True  # type: ignore[attr-defined]
