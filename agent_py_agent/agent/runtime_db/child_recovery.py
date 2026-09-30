# LLM: /recover 子代理分支在运行库一侧的两个入口（O1，2026-09-30）：
#   1. unknown_child_attempts_for_thread：只读投影，按 tasks.thread_id 列出本线程未关 TaskRun 里“当前执行轮为 unknown 的非根代理”；
#      只返回编号、角色、TaskRun 这类结构化列，不读 goal/title 正文。
#   2. recover_child_attempt_unknown：在同一写事务里复核目标仍属该线程、TaskRun 未关、仍是非根代理且当前执行轮仍为 unknown，
#      再走与主链同一个 unknown→recovered CAS（repository._recover_unknown_attempt_conn），事件带 recovery_target/thread_id/task_run_id。
#   只有用户显式处置才会调用写入口；这里不自动处置、不按时间过期，也不改静止规则和 TaskRun 树规则。
#   改动同步 test_turn_recovery_child_unknown.py、gateway_parts/turn_recovery_control.py 与 docs/modules/gateway/04-structure.md。
# 模块用途: 让会话里的 /recover 能看到并显式处置本线程子代理（例如被 SIGKILL 的子代理）留下的未知执行轮。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .operations import ATTEMPT_EFFECT_DISPOSITIONS, ATTEMPT_STATUS_UNKNOWN
from .repository import _recover_unknown_attempt_conn

# 事件里标明这次恢复的目标是子代理（结构化来源，不从正文推断）。
CHILD_RECOVERY_TARGET = "child_agent_run"

# 线程范围的唯一口径：读投影和写事务复核共用同一段条件，避免两边漂移。
_THREAD_CHILD_UNKNOWN_SCOPE = (
    "FROM agent_runs ar "
    "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
    "JOIN tasks t ON t.task_id = tr.task_id "
    "JOIN agent_attempts aa ON aa.attempt_id = ar.current_attempt_id "
    "WHERE t.thread_id = ? AND tr.closed_at = 0 "
    "AND ar.parent_agent_run_id != '' AND aa.status = ?"
)


# LLM: 投影行只承载定位和展示需要的结构化身份；attempt_id 是写入口 CAS 的比对键，run_id 用来读子代理记录判断接替。
# 类用途: 描述一条可以由 /recover 处置的子代理未知执行轮。
@dataclass(frozen=True)
class ChildRecoveryTarget:
    agent_run_id: str
    run_id: str
    role: str
    attempt_id: str
    task_run_id: str
    task_id: str
    thread_id: str


# LLM: 空线程身份直接返回空列表（证明不了归属，不能退回全库扫描）；结果按 agent_run 创建时间排序，展示顺序稳定。
# 函数用途: 列出本线程里被中断、结果未确认、正等待人工处置的子代理执行轮；只读，不改任何行。
def unknown_child_attempts_for_thread(repository: Any, thread_id: str) -> list[ChildRecoveryTarget]:
    normalized = str(thread_id or "").strip()
    if not normalized:
        return []
    with repository._runtime_connection() as conn:
        rows = conn.execute(
            "SELECT ar.agent_run_id, ar.run_id, ar.role, ar.current_attempt_id, tr.task_run_id, tr.task_id "
            + _THREAD_CHILD_UNKNOWN_SCOPE
            + " ORDER BY ar.created_at, ar.agent_run_id",
            (normalized, ATTEMPT_STATUS_UNKNOWN),
        ).fetchall()
    return [
        ChildRecoveryTarget(
            agent_run_id=str(row["agent_run_id"] or ""),
            run_id=str(row["run_id"] or ""),
            role=str(row["role"] or ""),
            attempt_id=str(row["current_attempt_id"] or ""),
            task_run_id=str(row["task_run_id"] or ""),
            task_id=str(row["task_id"] or ""),
            thread_id=normalized,
        )
        for row in rows
    ]


# LLM: 写事务内先按同一范围条件复核（线程、TaskRun 未关、非根、当前执行轮仍 unknown），不满足返回 target_changed，
#   不落任何写；满足才调用主链共用的 CAS。副作用：attempt→recovered、run unknown→created、释放该 attempt 的执行锁、
#   写 attempt_recovered 事件。处置值只认 ATTEMPT_EFFECT_DISPOSITIONS。
# 函数用途: 用户在会话里核对后，显式恢复一条子代理未知执行轮。
def recover_child_attempt_unknown(
    repository: Any,
    target: ChildRecoveryTarget,
    disposition: str,
    operator: str,
) -> dict[str, Any]:
    if str(disposition or "") not in ATTEMPT_EFFECT_DISPOSITIONS:
        return {"recovered": False, "reason": "invalid_effect_disposition"}
    with repository.transaction() as conn:
        still_in_scope = conn.execute(
            "SELECT 1 " + _THREAD_CHILD_UNKNOWN_SCOPE + " AND ar.agent_run_id = ? AND ar.current_attempt_id = ?",
            (target.thread_id, ATTEMPT_STATUS_UNKNOWN, target.agent_run_id, target.attempt_id),
        ).fetchone()
        if still_in_scope is None:
            return {"recovered": False, "reason": "target_changed"}
        return _recover_unknown_attempt_conn(
            repository,
            conn,
            attempt_id=target.attempt_id,
            operator=operator,
            effect_disposition=disposition,
            reason="用户在会话中用 /recover 显式确认子代理处置",
            event_facts={
                "recovery_target": CHILD_RECOVERY_TARGET,
                "thread_id": target.thread_id,
                "task_run_id": target.task_run_id,
            },
        )


__all__ = [
    "CHILD_RECOVERY_TARGET",
    "ChildRecoveryTarget",
    "recover_child_attempt_unknown",
    "unknown_child_attempts_for_thread",
]
