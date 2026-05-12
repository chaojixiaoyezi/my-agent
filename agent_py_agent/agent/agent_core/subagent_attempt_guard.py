# LLM: Stale subagent attempt guard blocks timed-out worker threads from mutating state.
# 模块用途: runner timeout 后旧线程可能还活着，本模块在工具入口阻断已废弃 attempt 的后续工具调用。

from __future__ import annotations

from ..tools import ToolExecutionResult


# LLM: stale_subagent_attempt_result checks persisted attempt state before any runner tool executes.
# 函数用途: 如果当前 runner attempt 已被 timeout/abandon 标记，返回阻断结果，避免旧线程继续写文件或发消息。
def stale_subagent_attempt_result(agent, payload: object) -> ToolExecutionResult | None:
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "").strip()
    attempt_id = str(getattr(agent, "_current_subagent_attempt_id", "") or "").strip()
    if not run_id or not attempt_id:
        return None
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return None
    if attempt_id in set(getattr(task, "runner_abandoned_attempt_ids", []) or []):
        return _blocked_result(payload, f"runner attempt 已被废弃或超时: {attempt_id}")
    active_attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if active_attempt_id and active_attempt_id != attempt_id:
        return _blocked_result(payload, f"runner attempt 已不是当前活动 attempt: {attempt_id}")
    return None


# LLM: _blocked_result preserves the model-requested tool name while refusing execution.
# 函数用途: 构造工具层阻断响应，让上层日志清楚看到哪个工具被 stale attempt guard 拦截。
def _blocked_result(payload: object, reason: str) -> ToolExecutionResult:
    tool_name = "unknown"
    if isinstance(payload, dict):
        tool_name = str(payload.get("tool") or "unknown").strip() or "unknown"
    return ToolExecutionResult(
        tool_name,
        False,
        (
            f"{reason}。系统已阻止本次工具调用，避免超时后的旧 runner 继续读写、"
            "调度或汇报；请等待父级接管/重试新 attempt。"
        ),
    )
