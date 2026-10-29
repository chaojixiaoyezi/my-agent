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


# LLM: Python标准库sqlite3.Connection.__exit__仅提交或回滚事务而不关闭连接；重写__exit__在with块结束时立即close，防止遗留连接由GC在任意线程异步触发WAL独占锁竞争。
# 类用途: 在with上下文退出时完成事务结算并立即关闭底层SQLite连接。
class _ClosingConnection(sqlite3.Connection):
    # LLM: 先执行父类事务提交/回滚再无条件调用close；使用显式三参数签名满足架构守卫无*args约束，sqlite3重复close幂等。
    # 函数用途: 退出with块时同步关闭SQLite连接并释放文件锁与WAL句柄。
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool | None:
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()


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

    # LLM: 只读URI不创建缺库且锁忙立即拒绝；Count0仍原10秒/同一表，with块退出时由_ClosingConnection立即关闭连接。
    # 函数用途: 为原状态方法打开对应模式且退出即关闭的SQLite连接。
    def _connect(self) -> sqlite3.Connection:
        address = Path(self._path).resolve().as_uri() + '?mode=ro' if self._read_only else self._path
        conn = sqlite3.connect(
            address,
            timeout=0.0 if self._read_only else 10.0,
            uri=self._read_only,
            factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # -- 私聊活跃时间 ------------------------------------------------------------

    # LLM: 只读查询用户最近私聊活跃时间戳，无记录或为0时返回None，不修改数据库。
    # 函数用途: 获取指定用户的最近私聊活跃时间。
    def last_activity(self, user_id: str) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_activity_at FROM private_chat_locks WHERE user_id = ?", (user_id,)
            ).fetchone()
        return float(row["last_activity_at"]) if row and row["last_activity_at"] else None

    # LLM: 写入或更新用户最近活跃时间与更新时间并立即提交关闭连接，会写SQLite数据库。
    # 函数用途: 记录指定用户的最新私聊活跃时间戳。
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

    # LLM: 只读查询指定用户的scrypt密码散列，无记录或空串时返回None，查询后立即关闭连接。
    # 函数用途: 读取用户在会话锁库中保存的密码散列字符串。
    def password_hash(self, user_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM private_chat_locks WHERE user_id = ?", (user_id,)
            ).fetchone()
        return str(row["password_hash"]) if row and row["password_hash"] else None

    # LLM: 基于password_hash判断用户是否已设置非空密码散列，不修改数据库状态。
    # 函数用途: 检查指定用户是否已配置私聊解锁密码。
    def has_password(self, user_id: str) -> bool:
        return bool(self.password_hash(user_id))

    # LLM: 写入或更新用户的scrypt密码散列与活跃时间戳并立即提交关闭连接，会写SQLite数据库。
    # 函数用途: 保存指定用户的新密码散列并刷新活跃时间。
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
