# LLM: TaskRun（一条会话任务的整棵执行总账）收口的唯一判定位置（D3 + O1，2026-09-30）。两个调用方：
#   agent_core/runtime_mixin._settle_terminal_conversation_task_run（根/子代理执行收口边）与 gateway_parts/turn_recovery_control
#   （/recover 处置子代理之后）。放在会话层是因为判定只依赖会话任务关联与运行库，且网关按分层边界不能导入 agent_core。
#   规则：根代理已终态；会话任务关联已终态，或关联文件确实不存在（D3：从未升格成会话任务的请求）；再由
#   RuntimeRepository.settle_task_run_if_agent_tree_terminal 证明整棵树终态或静止（unknown 不算静止）。缺存储、空任务身份、
#   关联读坏或未终态都保持打开；之后 create_attempt 会重新打开已关的 TaskRun。改口径同步
#   test_task_run_close_without_conversation_task.py 与 test_turn_recovery_child_unknown.py。
# 模块用途: 判断一条任务执行总账能不能关，能关就用同一个树终态 CAS 关掉；不能关时什么也不写。
from __future__ import annotations

from typing import Any

from ..runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from .task_state import conversation_task_link_is_terminal


# LLM: A TaskRun spans every foreground/background slice and descendant of one conversation task. The durable link—not a
# turn-local flag or model prose—authorizes closeout, while RuntimeDB proves the exact agent tree terminal. run_id locates the
# run whose edge fired; with an empty run_id the root main run of task_id is used (the /recover child branch). A TaskRun whose
# task id has no link file at all is governed only by its own agent tree (operator agent-runtime, reason no_conversation_task).
# Only a real absence counts: missing store, empty task id, an unreadable link (DataCorruptionError) or a non-terminal link keep
# it open. Unknown attempts still block via the repository's tree rule. Side effect: may write task_run.closed.
# 函数用途: 在代理执行收口或显式恢复之后核对会话任务状态，并幂等关闭整棵任务执行总账；没有会话任务的请求在代理树结束后同样关闭。
def settle_terminal_task_run(repo: Any, store: Any, *, run_id: str = "", task_id: str = "") -> None:
    if repo is None or store is None:
        return
    run_id = str(run_id or "").strip()
    task_id = str(task_id or "").strip()
    try:
        row = repo.agent_run_for_run_id(run_id) if run_id else None
        if row is None and task_id:
            row = repo.main_agent_run_for_task(task_id)
        if row is None or str(row["status"] or "") not in AGENT_RUN_TERMINAL_STATUSES:
            return
        task_run_id = str(row["task_run_id"] or "").strip()
        task_run = repo.get_task_run(task_run_id)
        if task_run is None:
            return
        canonical_task_id = str(task_run["task_id"] or "").strip()
        if not canonical_task_id:
            return
        closeout = _task_run_closeout_reason(store.tasks.load(canonical_task_id))
        if closeout is None:
            return
        operator, reason = closeout
        repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=task_run_id,
            task_id=canonical_task_id,
            operator=operator,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 审计投影失败不反噬已完成的用户任务
        return


# LLM: Pure decision for settle_terminal_task_run. `link` is the result of
# ConversationStore.tasks.load: None means the link file is truly absent (D3: the request never
# promoted a conversation task, so its own agent tree governs closeout); a loaded link must be
# terminal per conversation_task_link_is_terminal. Returns (operator, reason) for the tree CAS or
# None to keep the TaskRun open. Callers must never pass None for an unreadable link — load raises
# DataCorruptionError there and the caller's except keeps the TaskRun open.
# 函数用途: 根据会话任务关联文件决定能否关闭任务执行总账，并给出关闭人和原因；返回 None 表示继续保持打开。
def _task_run_closeout_reason(link: object) -> tuple[str, str] | None:
    if link is None:
        # 没有会话任务关联文件：这条执行总账只归本次请求的代理树管，树结束即可关（D3）。
        return "agent-runtime", "no_conversation_task"
    link_status = str(getattr(link, "status", "") or "").strip().lower()
    if not conversation_task_link_is_terminal(link_status):
        return None
    return "conversation-runtime", f"conversation_task_{link_status}"
