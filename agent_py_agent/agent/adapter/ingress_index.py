"""入站查找投影；规范 JSON 才是执行、租约与保留期的权威。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..gateway_parts.recovery_pending_index import private_projection_connection


# LLM: 每个 spool 独立建库，不缓存正文或长期持有 SQLite 连接。
# 类用途: 管理有界候选查询、写前责任标记和可重放的维护位置。
class IngressIndex:
    # LLM: 初始化只建立表与索引，旧文件由维护批次逐步发现。
    # 函数用途: 创建私有 SQLite 投影及部分索引。
    def __init__(self, directory: Path, *, legacy: bool) -> None:
        self.path = directory / "index.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ingress (
                    ingress_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    created_at REAL NOT NULL, due_at REAL NOT NULL, expires_at REAL);
                CREATE INDEX IF NOT EXISTS ingress_pending ON ingress(created_at, ingress_id)
                    WHERE state != 'completed';
                CREATE INDEX IF NOT EXISTS ingress_due ON ingress(due_at, created_at, ingress_id)
                    WHERE state != 'completed';
                CREATE INDEX IF NOT EXISTS ingress_expires ON ingress(expires_at, ingress_id)
                    WHERE state = 'completed';
                CREATE TABLE IF NOT EXISTS intents (ingress_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quarantine (name TEXT PRIMARY KEY, expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS quarantine_expires ON quarantine(expires_at, name);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('legacy', ?)", ("1" if legacy else "0",))

    # LLM: 显式 close 覆盖成功、提交失败和调用者异常，不能依赖连接析构释放 FD。
    # 函数用途: 以耐久事务打开一个短生命周期连接。
    @contextmanager
    def connect(self):
        try:
            with private_projection_connection(self.path) as db:
                yield db
        except sqlite3.Error as exc:
            raise OSError(f"ingress index unavailable: {exc}") from exc

    # LLM: 先单独提交责任标记，再允许规范写；异常返回也不表示规范文件未发布。
    # 函数用途: 保存可按规范文件重放的写前 intent。
    def begin(self, ingress_id: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO intents VALUES (?)", (ingress_id,))

    # LLM: 只能在持有同一规范路径锁并核读当前文件后调用，不允许旧投影覆盖新 CAS。
    # 函数用途: 发布规范行的查找字段，并在同一事务确认责任标记。
    def settle(self, ingress_id: str, record, *, unreadable: bool = False) -> None:
        with self.connect() as db:
            if record is None:
                db.execute("DELETE FROM ingress WHERE ingress_id = ?", (ingress_id,))
            else:
                due = max(record.created_at, record.next_attempt_at, record.claim_expires_at if record.claim_owner else 0.0)
                expires = record.updated_at + 7 * 24 * 60 * 60 if record.state == "completed" else None
                db.execute("INSERT INTO ingress VALUES (?, ?, ?, ?, ?) ON CONFLICT(ingress_id) "
                    "DO UPDATE SET state=excluded.state, created_at=excluded.created_at, "
                    "due_at=excluded.due_at, expires_at=excluded.expires_at",
                    (ingress_id, record.state, record.created_at, due, expires))
            if unreadable:
                db.execute("INSERT OR IGNORE INTO intents VALUES (?)", (ingress_id,))
            else:
                db.execute("DELETE FROM intents WHERE ingress_id = ?", (ingress_id,))

    # LLM: 三个查询均使用部分索引，不能在 Python 中筛选全部完成历史。
    # 函数用途: 取至多一批活跃、到期或终态过期候选。
    def candidates(self, kind: str, *, now: float = 0.0, limit: int = 128) -> list[str]:
        queries = {
            "pending": ("SELECT ingress_id FROM ingress WHERE state != 'completed' "
                        "ORDER BY created_at, ingress_id LIMIT ?", (limit,)),
            "due": ("SELECT ingress_id FROM ingress WHERE state != 'completed' AND due_at <= ? "
                    "ORDER BY due_at, created_at, ingress_id LIMIT ?", (now, limit)),
            "expires": ("SELECT ingress_id FROM ingress WHERE state = 'completed' AND expires_at < ? "
                        "ORDER BY expires_at, ingress_id LIMIT ?", (now, limit)),
        }
        sql, args = queries[kind]
        with self.connect() as db:
            return [row[0] for row in db.execute(sql, args)]

    # LLM: keyset 位置保存在库内；坏记录不会永久挡住字典序靠后的责任。
    # 函数用途: 轮转领取固定数量的审计行或未决 intent。
    def audit_ids(self, *, intents: bool = False, limit: int = 128) -> list[str]:
        table = "intents" if intents else "ingress"
        key = "intent_cursor" if intents else "audit_cursor"
        with self.connect() as db:
            found = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            cursor = found[0] if found else ""
            rows = db.execute(f"SELECT ingress_id FROM {table} WHERE ingress_id > ? "
                              "ORDER BY ingress_id LIMIT ?", (cursor, limit)).fetchall()
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, rows[-1][0] if rows else ""))
            return [row[0] for row in rows]

    # LLM: 元数据仅保存维护位置，不作为消息执行许可。
    # 函数用途: 读取一个持久维护标记。
    def get(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    # LLM: 不批量重写旧表，单个标记独立提交。
    # 函数用途: 更新一个持久维护标记。
    def set(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # LLM: 隔离证据没有执行责任，但仍按真实写入时间保留七天。
    # 函数用途: 登记或刷新一条隔离证据的过期位置。
    def quarantine_put(self, name: str, expires_at: float) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO quarantine VALUES (?, ?)", (name, expires_at))

    # LLM: 隔离清理同样只领取固定批次。
    # 函数用途: 取得已过保留期的隔离文件名。
    def quarantine_expired(self, now: float) -> list[str]:
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT name FROM quarantine WHERE expires_at < ? "
                "ORDER BY expires_at, name LIMIT 128", (now,))]

    # LLM: 删除后只确认对应投影，不影响其它证据。
    # 函数用途: 移除已清理的隔离文件索引。
    def quarantine_drop(self, name: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM quarantine WHERE name = ?", (name,))
