"""持久化入站队列(Tier 1.2 企业规模化):IM(飞书等)webhook 永不被 LLM 拖死 + 跨实例 exactly-once。

持久入站队列：支持多实例、租约、墓碑与通道公平调度。
**自建**(用 DB 原语,不引 Celery/Kafka——它们绑死重运维栈且要再包飞书重投/lane 公平语义):
- 入站 webhook verify→dedup→enqueue→立即 ack;多步 agent loop 在独立 worker 拉取跑,HTTP 不阻塞。
- claim 用 PG ``FOR UPDATE SKIP LOCKED``(任意多 worker 无竞争分发);SQLite 用 ``BEGIN IMMEDIATE`` 写锁串行。
- **墓碑去重**(任意状态都去重,防平台重投重复处理)、**lease 租约**(超时由 recover_stale 退回,防 worker
  崩后消息卡死)、**lane**(同会话串行 / 跨会话并行)、**max_attempts 毒丸保护**、**两级背压**
  (全局 + per-lane → QueueBackpressure → HTTP 429 让平台重投)。

须 ``scale`` extra(SQLAlchemy)。SQLite=本地/开发,PostgreSQL=10k-100k 规模(同代码)。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import (
        BigInteger,
        Column,
        Integer,
        MetaData,
        String,
        Table,
        Text,
        and_,
        func,
        insert,
        select,
        update,
    )

    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False


class QueueBackpressure(Exception):
    """队列满(全局或 per-lane 超限);上层应转 HTTP 429 让平台重投。"""


@dataclass(frozen=True)
class ClaimedMessage:
    id: int
    lane: str
    payload: dict[str, Any]
    claim_token: str
    attempts: int


def _messages_table(meta: Any) -> Any:
    return Table(
        "ingress_messages",
        meta,
        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
        Column("dedup_key", String(200), nullable=False, unique=True),
        Column("lane", String(200), nullable=False),
        Column("payload", Text, nullable=False),
        Column("status", String(20), nullable=False),  # pending / claimed / completed / failed
        Column("attempts", Integer, nullable=False),
        Column("claim_token", String(64), nullable=True),
        Column("lease_until", BigInteger, nullable=True),  # epoch ms
        Column("created_at", BigInteger, nullable=False),  # epoch ms
    )


@dataclass(frozen=True)
class QueueConfig:
    global_cap: int = 10000
    lane_cap: int = 50
    max_attempts: int = 5


class IngressQueue:
    def __init__(self, backend: StorageBackend, config: QueueConfig | None = None) -> None:
        if not _HAS_SQLALCHEMY:
            raise RuntimeError("入站队列需 SQLAlchemy:pip install 'my-agent[scale]'")
        self._backend = backend
        self._cfg = config or QueueConfig()
        self._meta = MetaData()
        self._t = _messages_table(self._meta)

    def ensure_schema(self) -> None:
        self._meta.create_all(self._backend.engine)

    # --- 入站(webhook 调用,立即返回)---
    def enqueue(self, dedup_key: str, lane: str, payload: dict[str, Any], *, now_ms: int = 0) -> bool:
        """墓碑去重 + 两级背压后入队。返回 True=新入队,False=重复(已去重)。满则抛 QueueBackpressure。"""
        now = now_ms or _now_ms()
        with self._backend.begin() as conn:  # 写事务(SQLite→BEGIN IMMEDIATE),与 claim 互斥保原子
            if conn.execute(select(self._t.c.id).where(self._t.c.dedup_key == dedup_key)).first() is not None:
                return False  # 墓碑去重:任意状态存在即跳过
            self._check_backpressure(conn, lane)
            conn.execute(
                insert(self._t).values(
                    dedup_key=dedup_key, lane=lane, payload=json.dumps(payload, ensure_ascii=False),
                    status="pending", attempts=0, claim_token=None, lease_until=None, created_at=now,
                )
            )
        return True

    def _check_backpressure(self, conn: Any, lane: str) -> None:
        active = and_(self._t.c.status.in_(("pending", "claimed")))
        total = conn.execute(select(func.count()).select_from(self._t).where(active)).scalar() or 0
        if total >= self._cfg.global_cap:
            raise QueueBackpressure(f"全局队列满({total}/{self._cfg.global_cap})")
        in_lane = conn.execute(
            select(func.count()).select_from(self._t).where(and_(active, self._t.c.lane == lane))
        ).scalar() or 0
        if in_lane >= self._cfg.lane_cap:
            raise QueueBackpressure(f"lane '{lane}' 满({in_lane}/{self._cfg.lane_cap})")

    # --- worker 拉取 ---
    def claim(self, lease_seconds: int = 120, *, now_ms: int = 0) -> ClaimedMessage | None:
        """原子领取下一条 pending(其 lane 当前无在飞消息=lane 串行)。PG SKIP LOCKED;SQLite 写锁串行。"""
        now = now_ms or _now_ms()
        with self._backend.begin() as conn:
            row = self._select_claimable(conn)
            if row is None:
                return None
            token = uuid.uuid4().hex
            conn.execute(
                update(self._t)
                .where(self._t.c.id == row.id)
                .values(status="claimed", claim_token=token, lease_until=now + lease_seconds * 1000, attempts=row.attempts + 1)
            )
            return ClaimedMessage(int(row.id), str(row.lane), json.loads(row.payload), token, int(row.attempts) + 1)

    def _select_claimable(self, conn: Any) -> Any:
        busy_lanes = select(self._t.c.lane).where(self._t.c.status == "claimed")
        stmt = (
            select(self._t.c.id, self._t.c.lane, self._t.c.payload, self._t.c.attempts)
            .where(and_(self._t.c.status == "pending", self._t.c.lane.notin_(busy_lanes)))
            .order_by(self._t.c.created_at, self._t.c.id)
            .limit(1)
        )
        if self._backend.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True)  # 多 worker 无竞争分发
        return conn.execute(stmt).first()

    def complete(self, claim_token: str) -> bool:
        return self._finish(claim_token, "completed")

    def fail(self, claim_token: str) -> bool:
        return self._finish(claim_token, "failed")

    def _finish(self, claim_token: str, status: str) -> bool:
        with self._backend.begin() as conn:
            result = conn.execute(
                update(self._t).where(self._t.c.claim_token == claim_token).values(status=status, claim_token=None)
            )
        return result.rowcount > 0

    def heartbeat(self, claim_token: str, lease_seconds: int = 120, *, now_ms: int = 0) -> bool:
        """续租(多步 LLM turn 超 lease 前调用,防被 recover_stale 误回收重复处理)。"""
        now = now_ms or _now_ms()
        with self._backend.begin() as conn:
            result = conn.execute(
                update(self._t)
                .where(and_(self._t.c.claim_token == claim_token, self._t.c.status == "claimed"))
                .values(lease_until=now + lease_seconds * 1000)
            )
        return result.rowcount > 0

    def recover_stale(self, *, now_ms: int = 0) -> int:
        """租约过期的 claimed 退回 pending(worker 崩→消息不卡死);attempts 达上限则 failed(毒丸保护)。"""
        now = now_ms or _now_ms()
        with self._backend.begin() as conn:
            stale = and_(self._t.c.status == "claimed", self._t.c.lease_until < now)
            poison = and_(stale, self._t.c.attempts >= self._cfg.max_attempts)
            failed = conn.execute(update(self._t).where(poison).values(status="failed", claim_token=None)).rowcount
            requeued = conn.execute(
                update(self._t).where(stale).values(status="pending", claim_token=None, lease_until=None)
            ).rowcount
        return int(requeued) + int(failed)

    def stats(self) -> dict[str, int]:
        with self._backend.connect() as conn:
            rows = conn.execute(
                select(self._t.c.status, func.count()).select_from(self._t).group_by(self._t.c.status)
            ).all()
        return {str(s): int(c) for s, c in rows}


def _now_ms() -> int:
    return int(time.time() * 1000)
