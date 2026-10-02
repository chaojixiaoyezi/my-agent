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

from ..common.opaque_id import is_opaque_id
from .operations import ATTEMPT_EFFECT_DISPOSITIONS, ATTEMPT_STATUS_UNKNOWN
from .repository import _recover_unknown_attempt_conn

# 事件里标明这次恢复的目标是子代理（结构化来源，不从正文推断）。
CHILD_RECOVERY_TARGET = "child_agent_run"
ROOT_RECOVERY_TARGET = "root_agent_run"

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


# LLM: 按编号写入口把解析后的线程、编号、处置和审计人冻结在一个值对象里，避免函数参数扩张和调用时错位。
# 类用途: 描述一次用户明确指定编号的线程恢复请求。
@dataclass(frozen=True)
class ThreadRecoveryRequest:
    thread_id: str
    target_id: str
    disposition: str
    operator: str


# LLM: 编号恢复必须让仓储、现有写事务和不可变请求同行，避免拆散后误在事务外重查；仅供本模块内部传递。
# 类用途: 打包一条线程恢复写操作所需的事务上下文。
@dataclass(frozen=True)
class _ThreadRecoveryWrite:
    repository: Any
    conn: Any
    request: ThreadRecoveryRequest


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


# LLM: 带编号的写入口必须在同一个 BEGIN IMMEDIATE 事务里定位编号并复核 thread/open TaskRun/current unknown；
#   三类失败分别返回 target_not_found、target_out_of_scope、target_not_unknown，不能退回“恰好一条”或按正文猜目标。
#   成功仍调用主链唯一 _recover_unknown_attempt_conn，并在事件里保留目标类型、线程和 TaskRun。
# 函数用途: 按查看结果里的 opaque 编号恢复本线程一条根或子代理未知执行轮。
def recover_thread_attempt_unknown(repository: Any, request: ThreadRecoveryRequest) -> dict[str, Any]:
    if request.disposition not in ATTEMPT_EFFECT_DISPOSITIONS:
        return {"recovered": False, "reason": "invalid_effect_disposition"}
    if not is_opaque_id(request.target_id, kind="run_id"):
        return {"recovered": False, "reason": "target_not_found"}
    with repository.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = _target_rows(conn, request.target_id)
        if not rows:
            return {"recovered": False, "reason": "target_not_found"}
        scoped = [row for row in rows if str(row["thread_id"] or "") == request.thread_id
                  and float(row["closed_at"] or 0) == 0]
        if not scoped:
            return {"recovered": False, "reason": "target_out_of_scope"}
        row = scoped[0]
        if str(row["attempt_status"] or "") != ATTEMPT_STATUS_UNKNOWN:
            return {"recovered": False, "reason": "target_not_unknown"}
        return _recover_target_row(_ThreadRecoveryWrite(repository, conn, request), row)


# LLM: 编号只匹配规范 run_id；极旧空 run_id 记录才允许用 agent_run_id 回退。查询不按角色或正文筛选。
# 函数用途: 在写事务内读取一个编号对应的当前执行轮和范围事实。
def _target_rows(conn: Any, target_id: str) -> list[Any]:
    return conn.execute(
        "SELECT ar.agent_run_id, ar.run_id, ar.role, ar.parent_agent_run_id, ar.current_attempt_id, "
        "aa.status AS attempt_status, tr.task_run_id, tr.task_id, tr.closed_at, t.thread_id "
        "FROM agent_runs ar JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id "
        "JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id "
        "WHERE ar.run_id=? OR (ar.run_id='' AND ar.agent_run_id=?) ORDER BY ar.created_at",
        (target_id, target_id),
    ).fetchall()


# LLM: 调用共享 CAS 后补回调用方收口所需的结构化目标字段；这些字段都来自同一事务查询，不读子代理正文。
# 函数用途: 对已复核的目标行执行共享恢复 CAS，并返回目标身份。
def _recover_target_row(write: _ThreadRecoveryWrite, row: Any) -> dict[str, Any]:
    request = write.request
    recovery_target = CHILD_RECOVERY_TARGET if str(row["parent_agent_run_id"] or "") else ROOT_RECOVERY_TARGET
    result = _recover_unknown_attempt_conn(
        write.repository, write.conn, attempt_id=str(row["current_attempt_id"] or ""), operator=request.operator,
        effect_disposition=request.disposition, reason="用户在会话中用 /recover 编号显式确认处置",
        event_facts={"recovery_target": recovery_target, "thread_id": request.thread_id,
                     "task_run_id": str(row["task_run_id"] or "")},
    )
    if result.get("recovered"):
        result.update(
            target_id=request.target_id, recovery_target=recovery_target,
            agent_run_id=str(row["agent_run_id"] or ""), run_id=str(row["run_id"] or ""),
            role=str(row["role"] or ""),
            attempt_id=str(row["current_attempt_id"] or ""), task_run_id=str(row["task_run_id"] or ""),
            task_id=str(row["task_id"] or ""), thread_id=request.thread_id,
        )
    return result


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
    "ROOT_RECOVERY_TARGET",
    "ChildRecoveryTarget",
    "ThreadRecoveryRequest",
    "recover_child_attempt_unknown",
    "recover_thread_attempt_unknown",
    "unknown_child_attempts_for_thread",
]
