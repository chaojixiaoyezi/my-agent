
from __future__ import annotations

from ...runtime_errors import runtime_error_report
from ...tooling.models import ToolHandlerOutcome
from ..runner.context import current_subagent_attempt_id, current_subagent_run_id


def stale_subagent_attempt_result(agent, payload: object) -> ToolHandlerOutcome | None:
    reason = _stale_attempt_reason(agent)
    if reason is None:
        return None
    return _blocked_result(payload, reason)


def stale_subagent_attempt_message(agent) -> str | None:
    reason = _stale_attempt_reason(agent)
    if reason is None:
        return None
    return (
        f"{reason}。旧 runner attempt 已停止，不再继续调用模型或工具；"
        "请等待父级接管、重试或创建新的 attempt 继续同一任务目录。"
    )


# LLM: The canonical active-attempt pointer is a positive execution grant. A
# missing pointer after result projection means the scoped runner has ended;
# never treat absence as permission to keep calling the model or tools.
# 函数用途: 核对子代理当前执行轮是否仍有权继续，返回可直接用于拦截的结构化原因文本。
def _stale_attempt_reason(agent) -> str | None:
    run_id = current_subagent_run_id(agent)
    attempt_id = current_subagent_attempt_id(agent)
    if not run_id or not attempt_id:
        return None
    try:
        task = agent.subagents.load(run_id)
    except Exception as exc:
        report = runtime_error_report(exc, context="subagent_attempt_guard.subagents.load")
        return (
            f"runner attempt 状态读取失败: run_id={run_id}, attempt_id={attempt_id}, "
            f"category={report.get('category', 'unknown')}, message={report.get('message', '')}"
        )
    if attempt_id in set(getattr(task, "runner_abandoned_attempt_ids", []) or []):
        return f"runner attempt 已被废弃或超时: {attempt_id}"
    active_attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if not active_attempt_id:
        return f"runner attempt 已结束或不再活动: {attempt_id}"
    if active_attempt_id and active_attempt_id != attempt_id:
        return f"runner attempt 已不是当前活动 attempt: {attempt_id}"
    return None


def _blocked_result(payload: object, reason: str) -> ToolHandlerOutcome:
    tool_name = "unknown"
    if isinstance(payload, dict):
        tool_name = str(payload.get("tool") or "unknown").strip() or "unknown"
    return ToolHandlerOutcome(
        tool_name,
        False,
        (
            f"{reason}。系统已阻止本次工具调用，避免超时后的旧 runner 继续读写、"
            "调度或汇报；请等待父级接管/重试新 attempt。"
        ),
        error_code="RUNNER_ATTEMPT_STALE",
    )
