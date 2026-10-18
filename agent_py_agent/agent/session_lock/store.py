# LLM: 密码/hash、活跃与输入卡nonce共用原用户行；卡片取消不得改认证事实或另造权限账。
# 模块用途: 持久化个人会话锁及准确输入卡生命周期，SQLite迁移保留既有密码。
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


# LLM: 原密码及活跃行是唯一状态；只读模式禁止建库/迁移并拒绝显式锁等待，写方法仍由SQLite原模式拒绝。
# 类用途: 保存或只读查询同一份个人锁，不将只读或缺库解释成解锁。
class SessionLockStore:
    # LLM: read_only只供原状态查询，不建目录/迁移；默认初始化原样，SQLite ro继续拒任何误用的写方法。
    # 函数用途: 打开原密码库，冷C查询不因构造额外修改共享库。
    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self._path = str(db_path)
        self._read_only = read_only
        self._init_lock = threading.Lock()
        if not read_only:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._init()

    # LLM: 密码与输入卡状态共用原用户行；迁移只加UI nonce字段，不改旧hash/活跃事实。
    # 函数用途: 初始化原数据库并迁移卡片生命周期字段，会写数据库。
    def _init(self) -> None:
        with self._init_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            from .card_control import initialize_card_columns
            initialize_card_columns(conn)

    # LLM: 只读URI不创建缺库且锁忙立即拒绝；Count0仍原10秒/同一表，模式只缩小权限，不产生密码认证。
    # 函数用途: 为原状态方法打开对应模式的连接。
    def _connect(self) -> sqlite3.Connection:
        address = Path(self._path).resolve().as_uri() + '?mode=ro' if self._read_only else self._path
        conn = sqlite3.connect(address, timeout=0.0 if self._read_only else 10.0, uri=self._read_only)
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
