"""会话锁状态存储(自洽 SQLite:密码 hash + 私聊最近活跃时间)。

按飞书用户 open_id 建表。连接按操作即用即建(gateway 多线程安全),WAL 提并发。
密码只存 scrypt 散列(见 passwords.py),明文永不落库。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS private_chat_locks (
    user_id          TEXT PRIMARY KEY,
    password_hash    TEXT,
    last_activity_at REAL NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0
);
"""


class SessionLockStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with self._init_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- 私聊活跃时间 ------------------------------------------------------------

    def last_activity(self, user_id: str) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_activity_at FROM private_chat_locks WHERE user_id = ?", (user_id,)
            ).fetchone()
        return float(row["last_activity_at"]) if row and row["last_activity_at"] else None

    def record_activity(self, user_id: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO private_chat_locks (user_id, last_activity_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET last_activity_at = excluded.last_activity_at,
                                                      updated_at = excluded.updated_at""",
                (user_id, moment, moment),
            )
            conn.commit()

    # -- 密码 hash --------------------------------------------------------------

    def password_hash(self, user_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM private_chat_locks WHERE user_id = ?", (user_id,)
            ).fetchone()
        return str(row["password_hash"]) if row and row["password_hash"] else None

    def has_password(self, user_id: str) -> bool:
        return bool(self.password_hash(user_id))

    def set_password_hash(self, user_id: str, password_hash: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO private_chat_locks (user_id, password_hash, last_activity_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET password_hash = excluded.password_hash,
                                                      updated_at = excluded.updated_at""",
                (user_id, password_hash, moment, moment),
            )
            conn.commit()


__all__ = ["SessionLockStore"]
