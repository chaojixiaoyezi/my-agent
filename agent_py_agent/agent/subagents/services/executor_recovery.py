# LLM: 本模块只处理已证实退出的 exact 子代理执行器；无结果是执行失败，不是业务完成或自动重跑授权。
# 结果/WAL/父级通知复用正式 runner_result 服务，未决工具保留 UNKNOWN 与执行锁。
# 模块用途: 让已经消失的子代理从假运行变成可见失败或待核实状态，并可靠通知父级。
from __future__ import annotations

from typing import Any

from ...contracts.subagent_completion import subagent_takeover_hint
from ...runtime_db.executor_liveness import exited_attempt_facts, mark_exited_attempt_unknown
from ..manager_runner_result_payload import RecordRunnerResultParams
from ..models import FailureType
from .runtime_closeout import pending_closeout

# 子代理 attributes 里保存“执行器退出后的接替提示”的键；只属于最近一次写回的结果。
TAKEOVER_HINT_ATTR = "takeover_hint"


# LLM: 调用方已核对父会话允许管理此 child；只消费 exact attempt 退出事实，不能凭缺 session、无输出或时长触发。
#   takeover_hint 由调用方按 capability 开关 subagent_takeover_hint_enabled 传入（缺省关）：开着时随这一份结果附结构化接替提示
#   （哪个 run、退出原因码、怎么用 replacement_for_run_ids 声明接替），只是提示，不派工、不改状态。改动同步 test_subagent_takeover_hint.py。
#   失败类型只用 FailureType 枚举值（EXECUTOR_EFFECTS_UNKNOWN / HOST_SHUTDOWN_INTERRUPTED / RUNNER_ERROR）：不在枚举里的值会被
#   结果状态改写成通用 runner_error。执行器在宿主优雅停机时被打过记号（facts["host_shutdown"]，见
#   runtime_db.executor_liveness.mark_in_process_executors_host_shutdown）就按“宿主停机中断”收口，同样不自动重跑；
#   有未知工具时仍以“效果未知、先核对”优先。改动同步 test_executor_exit_recovery。
# 函数用途: 为没有结论的已退出工作片补写结构化失败；有未知工具时显示阻塞并保留安全封存，宿主停机带走的说清是停机。
def recover_exited_runner(manager: Any, task: Any, *, takeover_hint: bool = False) -> dict[str, Any] | None:
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
            failure_type=_exit_failure_type(uncertain, bool(facts.get("host_shutdown"))),
            message=_exit_message(uncertain, bool(facts.get("host_shutdown"))),
            takeover_hint=(subagent_takeover_hint(str(task.id), str(facts["reason"]), bool(uncertain))
                           if takeover_hint else None),
        )
    )
    if result.status not in {"FAILED", "BLOCKED"}:
        return None
    return {**facts, "recovery_action": "executor_exit_projected", "status": result.status}


# LLM: 优先级固定：有未确认的工具效果 > 宿主停机带走 > 其它无结果退出。只读结构化布尔，不读文本。
# 函数用途: 选执行器退出收口的失败类型。
def _exit_failure_type(uncertain: bool, host_shutdown: bool) -> str:
    if uncertain:
        return FailureType.EXECUTOR_EFFECTS_UNKNOWN.value
    if host_shutdown:
        return FailureType.HOST_SHUTDOWN_INTERRUPTED.value
    return FailureType.RUNNER_ERROR.value


# 函数用途: 按同一优先级给执行器退出收口写给人看的一句说明。
def _exit_message(uncertain: bool, host_shutdown: bool) -> str:
    if uncertain:
        return "执行器已退出，部分工具是否生效尚未确认；已停止自动重跑，等待核对。"
    if host_shutdown:
        return "宿主停机中断：执行器随网关停机退出，没有返回结果；未自动重跑，需要时由父级续派。"
    return "执行器已退出但没有返回结果；本次执行失败，未作业务完成判定。"


# LLM: 写回结果时调用：这一份结果带接替提示就写入，不带就移除旧提示，所以完成信封不会带出上一份结果的提示。
#   只改内存里的 task.attributes，由结果服务随同一次保存落盘；没有提示时只动已有的 attributes 字典
#   （与 record_tool_failure_ledger 无事可做时不碰 attributes 同口径），真实 SubAgentTask 总有这个字典。
# 函数用途: 按本次结果设置或清除子代理的接替提示。
def record_takeover_hint(task: Any, hint: dict[str, object] | None) -> None:
    if hint:
        task.attributes[TAKEOVER_HINT_ATTR] = dict(hint)
        return
    attrs = getattr(task, "attributes", None)
    if isinstance(attrs, dict):
        attrs.pop(TAKEOVER_HINT_ATTR, None)
