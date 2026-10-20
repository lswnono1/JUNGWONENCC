from __future__ import annotations

import sqlite3

from . import core


class ClosingConnection(sqlite3.Connection):
    """sqlite3 context manager that also releases the Windows file handle."""

    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def closing_connect(self: core.Database) -> sqlite3.Connection:
    conn = sqlite3.connect(
        self.path,
        timeout=30,
        factory=ClosingConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def install_connection_patch() -> None:
    if getattr(core.Database, "_closing_connection_installed", False):
        return
    core.Database.connect = closing_connect  # type: ignore[method-assign]
    core.Database._closing_connection_installed = True  # type: ignore[attr-defined]
