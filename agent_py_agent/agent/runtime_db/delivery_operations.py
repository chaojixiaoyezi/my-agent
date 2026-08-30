"""R4 Delivery 权威操作（3.txt K 节）。

同库状态用本地事务（K.1）；跨进程/外部副作用不宣称 exactly-once
（K.2），走 at-least-once outbox/inbox + effect_key 去重（K.4）：
- enqueue 幂等：effect_key UNIQUE，重复入队返回既有条目（K.4）。
- claim 原子租约：CAS 只认 PENDING/FAILED → IN_FLIGHT（并发安全，
  两 worker 不会同投一条）。
- settle：ACKED（provider 确认）| FAILED（退避重试，attempts 超限进
  DEAD_LETTER，K.6 完整证据保留）。
- reconcile（K.3）：provider query 确认实际副作用 —— confirmed 落
  ACKED；absent/unknown 回 PENDING 退避重试（宁可重发不可丢）。

K.7：最终用户消息内容由模型按结构化事实生成，本模块只保证投递一次；
确定性文本仍只用于 /status、/stop 等显式控制协议（工具层既有约束）。
"""

# LLM: Delivery 只管理客观投递状态和 provider 回执；不得把业务质量、
# 验收清单或模型完成文案重新变成发送最终回复的机器硬门。
# 模块用途: 提供 owner runtime.db 的 outbox/inbox 幂等投递与查询能力。

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from ..common.id_generator import new_id
from .operations import (
    INBOX_PROCESSED,
    INBOX_RECEIVED,
    MAX_DELIVERY_ATTEMPTS,
    OUTBOX_ACKED,
    OUTBOX_DEAD_LETTER,
    OUTBOX_FAILED,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    RuntimeConflictError,
)


# LLM: 该 mixin 只保存消息投递事实；TaskRun 终态由 RuntimeRepository 的
# 执行生命周期入口负责，避免投递与任务收口互相卡死。
# 类用途: 为 RuntimeRepository 增加 outbox、inbox 和投递审计查询。
class RuntimeDeliveryMixin:
    """R4 outbox/inbox（挂到 RuntimeRepository）。"""

    # ------------------------------------------------------------ outbox
    def enqueue_outbox(
        self,
        *,
        effect_key: str,
        scope: str = "",
        payload: dict[str, Any] | None = None,
        owner_id: str = "",
        task_run_id: str = "",
    ) -> dict[str, Any]:
        """入队一条外部副作用（K.4 at-least-once 起点）。

        effect_key UNIQUE：重复入队幂等返回既有条目（不产生第二份投递）。
        """
        key = str(effect_key or "").strip()
        if not key:
            raise RuntimeConflictError("outbox effect_key 不能为空")
        now = time.time()
        outbox_id = new_id("outbox_id")
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox_entries(
                    outbox_id, effect_key, owner_id, task_run_id, scope,
                    payload_json, status, attempts, next_retry_at,
                    created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 'PENDING', 0, 0, ?, ?)
                """,
                (
                    outbox_id,
                    key,
                    owner_id,
                    task_run_id,
                    scope,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT outbox_id FROM outbox_entries WHERE effect_key = ?", (key,)
            ).fetchone()
        return {
            "outbox_id": str(row["outbox_id"]),
            "effect_key": key,
            "created": str(row["outbox_id"]) == outbox_id,
        }

    def claim_outbox(
        self,
        *,
        claimed_by: str,
        batch_size: int = 10,
        owner_id: str = "",
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """原子领取到期条目（PENDING/FAILED 且 next_retry_at<=now）。

        逐条 CAS：只把 claim 成功的变 IN_FLIGHT（attempts 已 +1）。
        并发 worker 不会重复领取同一条。
        """
        now = now if now is not None else time.time()
        claimed: list[dict[str, Any]] = []
        with self._runtime_connection() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, effect_key, owner_id, task_run_id, scope,
                       payload_json, attempts, claimed_by
                FROM outbox_entries
                WHERE status IN ('PENDING', 'FAILED') AND next_retry_at <= ?
                  AND (? = '' OR owner_id = ?)
                ORDER BY next_retry_at, created_at
                LIMIT ?
                """,
                (now, owner_id, owner_id, batch_size),
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE outbox_entries
                    SET status = 'IN_FLIGHT', claimed_at = ?, claimed_by = ?,
                        attempts = attempts + 1, updated_at = ?
                    WHERE outbox_id = ? AND status IN ('PENDING', 'FAILED')
                    """,
                    (now, claimed_by, now, row["outbox_id"]),
                )
                if cursor.rowcount != 1:
                    continue  # 已被另一 worker 领取
                claimed.append(
                    {
                        "outbox_id": str(row["outbox_id"]),
                        "effect_key": str(row["effect_key"]),
                        "owner_id": str(row["owner_id"]),
                        "task_run_id": str(row["task_run_id"]),
                        "scope": str(row["scope"]),
                        "payload": json.loads(row["payload_json"] or "{}"),
                        "attempts": int(row["attempts"]) + 1,
                        "claimed_by": claimed_by,
                    }
                )
            conn.commit()
        return claimed

    def settle_outbox(
        self,
        *,
        outbox_id: str,
        status: str,
        provider_evidence: dict[str, Any] | None = None,
        next_retry_at: float = 0.0,
    ) -> dict[str, Any]:
        """落投递结果（K.3/K.6）：ACKED | FAILED | DEAD_LETTER。

        - ACKED：终态，settled_at 落时间。
        - FAILED：退避重试；attempts 超限 → 自动 DEAD_LETTER（证据完整保留）。
        只认 IN_FLIGHT（claim 后）CAS：非 IN_FLIGHT 视为重复 settle，幂等。
        """
        if status not in (OUTBOX_ACKED, OUTBOX_FAILED, OUTBOX_DEAD_LETTER):
            raise RuntimeConflictError(f"settle_outbox 非法终态: {status!r}")
        now = time.time()
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT status, attempts FROM outbox_entries WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise RuntimeConflictError(f"outbox_entries 不存在: {outbox_id}")
            if row["status"] != OUTBOX_IN_FLIGHT:
                # 重复 settle（ACK 重放/竞争 worker）→ 幂等，不改写既有终态。
                return self.outbox_entry(outbox_id)
            final_status = status
            if status == OUTBOX_FAILED and int(row["attempts"]) >= MAX_DELIVERY_ATTEMPTS:
                final_status = OUTBOX_DEAD_LETTER
            evidence = dict(provider_evidence or {})
            existing = conn.execute(
                "SELECT provider_evidence_json FROM outbox_entries WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            merged = dict(json.loads(existing["provider_evidence_json"] or "{}"))
            merged.update({f"attempt_{int(row['attempts'])}": evidence})
            conn.execute(
                """
                UPDATE outbox_entries
                SET status = ?, provider_evidence_json = ?, next_retry_at = ?,
                    settled_at = ?, updated_at = ?
                WHERE outbox_id = ?
                """,
                (
                    final_status,
                    json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    next_retry_at,
                    now if final_status == OUTBOX_ACKED else 0,
                    now,
                    outbox_id,
                ),
            )
            conn.commit()
        return self.outbox_entry(outbox_id)

    def reconcile_outbox(
        self,
        *,
        outbox_id: str,
        provider_found: bool,
        provider_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """K.3 provider query/reconcile：确认实际副作用。

        - provider_found=True：副作用已生效 → ACKED（查重后不会重发）。
        - provider_found=False（absent/unknown）：回 PENDING 退避重试
          —— at-least-once 宁可重发不可丢；attempts 已在 claim 时累计。
        """
        now = time.time()
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT status FROM outbox_entries WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise RuntimeConflictError(f"outbox_entries 不存在: {outbox_id}")
            if row["status"] != OUTBOX_IN_FLIGHT:
                return self.outbox_entry(outbox_id)
            evidence = json.loads(
                conn.execute(
                    "SELECT provider_evidence_json FROM outbox_entries WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()["provider_evidence_json"]
                or "{}"
            )
            evidence[f"reconcile_{now:.0f}"] = dict(provider_evidence or {})
            if provider_found:
                conn.execute(
                    """
                    UPDATE outbox_entries
                    SET status = 'ACKED', provider_evidence_json = ?,
                        settled_at = ?, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                        outbox_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE outbox_entries
                    SET status = 'PENDING', provider_evidence_json = ?,
                        next_retry_at = ?, claimed_by = '', updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                        now + 30.0,  # 固定退避窗口：重试由 claim 循环驱动
                        now,
                        outbox_id,
                    ),
                )
            conn.commit()
        return self.outbox_entry(outbox_id)

    def dead_letter_outbox(self, *, outbox_id: str, reason: str) -> dict[str, Any]:
        """K.6：人工/策略进 dead-letter（保留完整证据，可重放）。"""
        return self.settle_outbox(
            outbox_id=outbox_id,
            status=OUTBOX_DEAD_LETTER,
            provider_evidence={"reason": str(reason or "")},
        )

    def replay_dead_letter(self, *, outbox_id: str) -> dict[str, Any]:
        """K.6：人工重放 dead-letter → PENDING 重新投递（attempts 清零）。"""
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM outbox_entries WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise RuntimeConflictError(f"outbox_entries 不存在: {outbox_id}")
            if row["status"] != OUTBOX_DEAD_LETTER:
                raise RuntimeConflictError(
                    f"只有 DEAD_LETTER 可重放（当前 {row['status']}）: {outbox_id}"
                )
            conn.execute(
                """
                UPDATE outbox_entries
                SET status = 'PENDING', attempts = 0, next_retry_at = 0,
                    claimed_by = '', updated_at = ?
                WHERE outbox_id = ?
                """,
                (now, outbox_id),
            )
        return self.outbox_entry(outbox_id)

    def outbox_entry(self, outbox_id: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM outbox_entries WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        if row is None:
            return None
        return self._outbox_row_to_dict(row)

    def outbox_entries_for_task_run(self, task_run_id: str) -> list[dict[str, Any]]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_entries WHERE task_run_id = ? ORDER BY created_at",
                (task_run_id,),
            ).fetchall()
        return [self._outbox_row_to_dict(row) for row in rows]

    def list_outbox(
        self,
        *,
        status: str = "",
        owner_id: str = "",
        task_run_id: str = "",
        scope: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """K.6：dead-letter/outbox 可查询（过滤状态/owner/task_run/scope）。"""
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if owner_id:
            clauses.append("owner_id = ?")
            args.append(owner_id)
        if task_run_id:
            clauses.append("task_run_id = ?")
            args.append(task_run_id)
        if scope:
            clauses.append("scope = ?")
            args.append(scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._runtime_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM outbox_entries {where} ORDER BY created_at LIMIT ?",
                (*args, max(1, int(limit))),
            ).fetchall()
        return [self._outbox_row_to_dict(row) for row in rows]

    @staticmethod
    def _outbox_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "outbox_id": str(row["outbox_id"]),
            "effect_key": str(row["effect_key"]),
            "owner_id": str(row["owner_id"]),
            "task_run_id": str(row["task_run_id"]),
            "scope": str(row["scope"]),
            "payload": json.loads(row["payload_json"] or "{}"),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "claimed_at": row["claimed_at"],
            "claimed_by": str(row["claimed_by"]),
            "next_retry_at": row["next_retry_at"],
            "provider_evidence": json.loads(row["provider_evidence_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "settled_at": row["settled_at"],
        }

    # ------------------------------------------------------------ inbox
    def receive_inbox(
        self,
        *,
        effect_key: str,
        payload: dict[str, Any] | None = None,
        sender: str = "",
        task_run_id: str = "",
        scope: str = "",
    ) -> dict[str, Any]:
        """接收跨边界消息（K.4）：effect_key UNIQUE 去重，重复投递只存一份。"""
        key = str(effect_key or "").strip()
        if not key:
            raise RuntimeConflictError("inbox effect_key 不能为空")
        now = time.time()
        inbox_id = new_id("inbox_id")
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO inbox_entries(
                    inbox_id, effect_key, sender, task_run_id, scope,
                    payload_json, status, received_at)
                VALUES(?, ?, ?, ?, ?, ?, 'RECEIVED', ?)
                """,
                (
                    inbox_id,
                    key,
                    sender,
                    task_run_id,
                    scope,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT inbox_id, status FROM inbox_entries WHERE effect_key = ?", (key,)
            ).fetchone()
        return {
            "inbox_id": str(row["inbox_id"]),
            "effect_key": key,
            "status": str(row["status"]),
            "created": str(row["inbox_id"]) == inbox_id,
        }

    def mark_inbox_processed(self, *, effect_key: str) -> dict[str, Any]:
        """inbox 条目处理完成（RECEIVED → PROCESSED，幂等）。"""
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT inbox_id, status FROM inbox_entries WHERE effect_key = ?",
                (effect_key,),
            ).fetchone()
            if row is None:
                raise RuntimeConflictError(f"inbox 不存在: {effect_key}")
            if row["status"] != INBOX_RECEIVED:
                return {
                    "inbox_id": str(row["inbox_id"]),
                    "effect_key": effect_key,
                    "status": str(row["status"]),
                }
            conn.execute(
                "UPDATE inbox_entries SET status = 'PROCESSED', processed_at = ? "
                "WHERE inbox_id = ?",
                (now, row["inbox_id"]),
            )
        return {
            "inbox_id": str(row["inbox_id"]),
            "effect_key": effect_key,
            "status": INBOX_PROCESSED,
        }

    def inbox_entry(self, effect_key: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM inbox_entries WHERE effect_key = ?", (effect_key,)
            ).fetchone()
        if row is None:
            return None
        return {
            "inbox_id": str(row["inbox_id"]),
            "effect_key": str(row["effect_key"]),
            "sender": str(row["sender"]),
            "task_run_id": str(row["task_run_id"]),
            "scope": str(row["scope"]),
            "payload": json.loads(row["payload_json"] or "{}"),
            "status": str(row["status"]),
            "received_at": row["received_at"],
            "processed_at": row["processed_at"],
        }

    # LLM: 只按 canonical task_run_id 读取 append-only 事件；不得从展示文案
    # 或旧 closeout 事件名反推任务状态。
    # 函数用途: 按顺序列出一次 TaskRun 的权威审计事件。
    def events_for_task_run(
        self, task_run_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """A.8 审计：某 task_run 的权威事件流。

        按 task_run_id 追索（repository.events_for_attempt 只按 attempt_id，
        task_run.closed 事件挂在 task_run 级、attempt_id 为空）。字段形态与
        conn_row_to_event 一致，避免从 delivery 反向 import repository
        造成循环导入。
        """
        with self._runtime_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_events WHERE task_run_id = ?
                ORDER BY seq ASC LIMIT ?
                """,
                (task_run_id, int(limit)),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "attempt_id": row["attempt_id"],
                "agent_run_id": row["agent_run_id"],
                "task_run_id": row["task_run_id"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
