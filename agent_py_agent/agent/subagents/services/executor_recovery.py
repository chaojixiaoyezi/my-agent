# LLM: 本模块只处理已证实退出的 exact 子代理执行器；无结果是执行失败，不是业务完成或自动重跑授权。
# 结果/WAL/父级通知复用正式 runner_result 服务，未决工具保留 UNKNOWN 与执行锁。
# 模块用途: 让已经消失的子代理从假运行变成可见失败或待核实状态，并可靠通知父级。
from __future__ import annotations

from typing import Any

from ...runtime_db.executor_liveness import exited_attempt_facts, mark_exited_attempt_unknown
from ..manager_runner_result_payload import RecordRunnerResultParams
from .runtime_closeout import pending_closeout


# LLM: 调用方已核对父会话允许管理此 child；只消费 exact attempt 退出事实，不能凭缺 session、无输出或时长触发。
# 函数用途: 为没有结论的已退出工作片补写结构化失败；有未知工具时显示阻塞并保留安全封存。
def recover_exited_runner(manager: Any, task: Any) -> dict[str, Any] | None:
    if str(task.status) != "RUNNING" or pending_closeout(task) is not None:
        return None
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "")
    facts = exited_attempt_facts(getattr(manager, "runtime_db", None), str(task.id), attempt_id)
    if facts is None:
        return None
    uncertain = facts["uncertain_effects"]
    if uncertain:
        mark_exited_attempt_unknown(manager.runtime_db, facts)
    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id, attempt_id=attempt_id, dry_run=False, ok=False,
            status="BLOCKED" if uncertain else "FAILED",
            turn_end_reason="blocked" if uncertain else "error",
            failure_type="executor_effects_unknown" if uncertain else "runner_error",
            message=("执行器已退出，部分工具是否生效尚未确认；已停止自动重跑，等待核对。"
                     if uncertain else "执行器已退出但没有返回结果；本次执行失败，未作业务完成判定。"),
        )
    )
    if result.status not in {"FAILED", "BLOCKED"}:
        return None
    return {**facts, "recovery_action": "executor_exit_projected", "status": result.status}
