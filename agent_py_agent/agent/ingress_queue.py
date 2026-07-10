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
        delete,
        func,
        insert,
        inspect,
        select,
        text,
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
        Column("release_channel", String(20), nullable=False, default="stable"),
        Column("payload", Text, nullable=False),
        Column("status", String(20), nullable=False),  # pending / claimed / completed / failed(=死信)
        Column("attempts", Integer, nullable=False),
        Column("claim_token", String(64), nullable=True),
        Column("lease_until", BigInteger, nullable=True),  # epoch ms
        Column("created_at", BigInteger, nullable=False),  # epoch ms
        Column("next_visible_at", BigInteger, nullable=False, default=0),  # 退避延迟可见:到点才可被领(审计 #17)
        Column("last_error", Text, nullable=True),  # 末次失败原因(死信排查用)
    )


_BASE_BACKOFF_MS = 1000
_MAX_BACKOFF_MS = 300_000  # 退避上限 5 分钟
# lane advisory 锁的命名空间(int4 classid):与共享 DB 里别的产品的 advisory 锁隔开,绝不串号互锁。
_LANE_LOCK_NS = 0x6D796167  # "myag"


def _backoff_ms(attempts: int) -> int:
    """指数退避(确定性可测):attempts=1→1s, 2→2s, 3→4s…封顶 5min。瞬时失败退避后重投而非永久丢。"""
    return min(_MAX_BACKOFF_MS, _BASE_BACKOFF_MS * (2 ** max(0, attempts - 1)))


def _ensure_queue_columns(backend: StorageBackend) -> None:
    existing = {c["name"] for c in inspect(backend.engine).get_columns("ingress_messages")}
    definitions = {
        "next_visible_at": "BIGINT NOT NULL DEFAULT 0",
        "last_error": "TEXT",
        "release_channel": "VARCHAR(20) NOT NULL DEFAULT 'stable'",
    }
    missing = [(name, definition) for name, definition in definitions.items() if name not in existing]
    if not missing:
        return
    with backend.begin() as conn:
        for name, definition in missing:
            conn.execute(text(f"ALTER TABLE ingress_messages ADD COLUMN {name} {definition}"))


@dataclass(frozen=True)
class QueueConfig:
    global_cap: int = 10000
    lane_cap: int = 50
    max_attempts: int = 5


class IngressQueue:
    def __init__(
        self,
        backend: StorageBackend,
        config: QueueConfig | None = None,
        *,
        release_channel: str = "stable",
    ) -> None:
        if not _HAS_SQLALCHEMY:
            raise RuntimeError("入站队列需 SQLAlchemy:pip install 'my-agent[scale]'")
        self._backend = backend
        self._cfg = config or QueueConfig()
        self._release_channel = str(release_channel or "stable").strip().lower()
        if self._release_channel not in {"stable", "canary"}:
            raise ValueError("release_channel 只允许 stable 或 canary")
        self._meta = MetaData()
        self._t = _messages_table(self._meta)

    def ensure_schema(self) -> None:
        """本地/测试便利入口；正式 scale Pod 只验版本，DDL 由 migrate_entry 独立执行。"""
        from agent_py_agent.agent.runtime_schema import apply_runtime_migrations

        # local/test 仍允许自修复临时库；生产 scale 路径不会调用本方法。
        self._meta.create_all(self._backend.engine)
        _ensure_queue_columns(self._backend)
        self._ensure_indexes()
        apply_runtime_migrations(self._backend)

    def require_schema_current(self) -> None:
        from agent_py_agent.agent.runtime_schema import require_runtime_schema_current

        require_runtime_schema_current(self._backend)

    def _ensure_indexes(self) -> None:
        """幂等建 claim 复合索引(status, next_visible_at, created_at)。

        claim 热路径按 status='pending' + next_visible_at<=now 过滤再按 created_at 排序;原表除
        dedup_key 外零索引,扫描随表线性退化、PG 上 autovacuum 膨胀(审计 #16)。CREATE INDEX
        IF NOT EXISTS 两端(SQLite/PG)都支持,老客户库再 ensure_schema 也会补上(create_all
        不给已存在表补索引,#10 教训)。"""
        sql = (
            "CREATE INDEX IF NOT EXISTS ix_ingress_status_visible "
            "ON ingress_messages (status, next_visible_at, created_at)"
        )
        with self._backend.begin() as conn:
            conn.execute(text(sql))

    def purge_old(self, *, retention_ms: int, now_ms: int = 0, limit: int = 10_000) -> int:
        """清理已终结(completed/failed)且 created_at 超保留期的旧行,防 ingress 表无界增长(审计 #16)。

        保留近 retention_ms 的终结行做墓碑去重(平台重投窗口内仍去重),更老的删;pending/claimed
        在途行永不删(哪怕 created_at 很老)。分批 limit 上限防大事务长锁;返回本次删除行数,
        可循环调至 0 清完积压。后台由 StaleReaper 按慢节拍调用。"""
        now = now_ms or _now_ms()
        cutoff = now - max(0, retention_ms)
        with self._backend.begin() as conn:
            ids = [
                r.id for r in conn.execute(
                    select(self._t.c.id).where(and_(
                        self._t.c.status.in_(("completed", "failed")),
                        self._t.c.created_at < cutoff,
                    )).limit(limit)
                ).all()
            ]
            if ids:
                conn.execute(delete(self._t).where(self._t.c.id.in_(ids)))
        return len(ids)

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
                    dedup_key=dedup_key, lane=lane, release_channel=self._release_channel,
                    payload=json.dumps(payload, ensure_ascii=False),
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
        """原子领取下一条 pending(其 lane 当前无在飞消息=lane 串行)。PG SKIP LOCKED+lane advisory 锁;SQLite 写锁串行。"""
        now = now_ms or _now_ms()
        with self._backend.begin() as conn:
            row = self._select_claimable(conn, now)
            if row is None:
                return None
            if not self._lock_lane(conn, str(row.lane)):
                return None  # 该 lane 正被别的 worker 认领 → 本轮退避(防 SKIP LOCKED 下两 worker 各领同 lane 不同行)
            if self._lane_busy(conn, str(row.lane)):
                return None  # 拿锁后复查:select 与拿锁之间的 MVCC 窗口里,别的 worker 可能刚认领了本 lane
            token = uuid.uuid4().hex
            conn.execute(
                update(self._t)
                .where(self._t.c.id == row.id)
                .values(status="claimed", claim_token=token, lease_until=now + lease_seconds * 1000, attempts=row.attempts + 1)
            )
            return ClaimedMessage(int(row.id), str(row.lane), json.loads(row.payload), token, int(row.attempts) + 1)

    def _lock_lane(self, conn: Any, lane: str) -> bool:
        """PG:非阻塞 advisory xact 锁,序列化"同 lane 并发认领"——SKIP LOCKED 会让两 worker 跳过彼此锁住的行
        各领同 lane 不同行,破坏 lane 串行;此锁堵上这个竞态窗口。锁随事务提交释放,而 claimed 状态同事务
        落库 → 释放即 lane busy,无缝衔接 busy_lanes 排他。命名空间键(_LANE_LOCK_NS)把锁与共享 DB 里别的
        产品的 advisory 锁隔开,绝不互相干扰。SQLite:BEGIN IMMEDIATE 已全序列化 claim,无需。"""
        if not self._backend.is_postgres:
            return True
        locked = conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:lane))"),
            {"ns": _LANE_LOCK_NS, "lane": lane},
        ).scalar()
        return bool(locked)

    def _lane_busy(self, conn: Any, lane: str) -> bool:
        """该 lane 当前是否有在飞(claimed)消息。拿到 lane advisory 锁后做新鲜读,看见已提交的认领,
        堵上"select 时没看见、拿锁后别人已提交"的 MVCC 竞态窗口(与 _lock_lane 合起来才保证 lane 串行)。"""
        return conn.execute(
            select(self._t.c.id).where(and_(self._t.c.lane == lane, self._t.c.status == "claimed")).limit(1)
        ).first() is not None

    def _select_claimable(self, conn: Any, now: int) -> Any:
        busy_lanes = select(self._t.c.lane).where(self._t.c.status == "claimed")
        stmt = (
            select(self._t.c.id, self._t.c.lane, self._t.c.payload, self._t.c.attempts)
            .where(and_(
                self._t.c.status == "pending",
                self._t.c.release_channel == self._release_channel,
                self._t.c.lane.notin_(busy_lanes),
                self._t.c.next_visible_at <= now,  # 退避中的消息(next_visible_at 在未来)暂不可领
            ))
            .order_by(self._t.c.created_at, self._t.c.id)
            .limit(1)
        )
        if self._backend.is_postgres:
            stmt = stmt.with_for_update(skip_locked=True)  # 多 worker 无竞争分发
        return conn.execute(stmt).first()

    def complete(self, claim_token: str) -> bool:
        return self._finish(claim_token, "completed")

    def fail(self, claim_token: str, *, retryable: bool = True, error: str = "", now_ms: int = 0) -> bool:
        """handler 失败处理(审计 #17):可重试且未达上限 → 退避重投(回 pending,next_visible_at 延迟可见);
        否则 → 死信(status='failed',留 last_error)。瞬时 provider 故障不再一次失败就永久丢。"""
        now = now_ms or _now_ms()
        with self._backend.begin() as conn:
            row = conn.execute(
                select(self._t.c.id, self._t.c.attempts).where(self._t.c.claim_token == claim_token)
            ).first()
            if row is None:
                return False
            if retryable and int(row.attempts) < self._cfg.max_attempts:
                values = {"status": "pending", "claim_token": None, "lease_until": None,
                          "next_visible_at": now + _backoff_ms(int(row.attempts)), "last_error": error[:500]}
            else:
                values = {"status": "failed", "claim_token": None, "last_error": error[:500]}  # 死信
            conn.execute(update(self._t).where(self._t.c.id == row.id).values(**values))
        return True

    def requeue_dead(self, *, limit: int = 100) -> int:
        """死信重放:把 failed 改回 pending 重新可领(人工补偿/批量重放,运维入口)。返回重放数。"""
        with self._backend.begin() as conn:
            ids = [r.id for r in conn.execute(
                select(self._t.c.id).where(self._t.c.status == "failed").limit(limit)
            ).all()]
            if ids:
                conn.execute(update(self._t).where(self._t.c.id.in_(ids)).values(
                    status="pending", attempts=0, claim_token=None, lease_until=None, next_visible_at=0, last_error=None,
                ))
        return len(ids)

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
