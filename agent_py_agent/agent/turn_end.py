# LLM: 统一技术结束与续跑事实，不调度任务、不读取自然语言；新增状态须同步 CLI、后台、子代理和收口回归。
# 模块用途: 为执行、持久化和前端提供唯一结束协议与技术续跑判据。
"""模块用途: 统一一轮 Agent 运行为什么结束的结构化协议。

LLM: 本模块只归一化宿主已经掌握的运行事实，不能读取模型正文、产物、测试或
验收结果。主代理、子代理、Gateway 和 TUI 必须共享这里的六种 reason；新增或
修改 reason 时同步检查 AgentRunResult、SubAgentRunnerResult、持久化投影和定向
测试。只读技术提示与归一化共用这里的协议，不成为模型正文或恢复命令。
``turn/end.reason`` 表达宿主轮结束原因；继续或停止循环由工具调用与 pending input 决定。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


# LLM: 枚举值必须与公开 turn/end.reason kind 保持一致；不要加入质量判定状态。
# 类用途: 表示一次模型工具循环结束时可观察到的六种客观原因。
class TurnEndReason(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    MAX_TOKENS = "max-tokens"
    ABORTED = "aborted"
    ERROR = "error"
    INTERRUPTED = "interrupted"


TURN_END_REASONS = frozenset(item.value for item in TurnEndReason)
_MAX_TOKEN_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "max-tokens",
        "model_response_truncated",
        "context_overflow",
    }
)
_ABORT_REASONS = frozenset({"user_stop", "conversation_control", "cancelled"})


# LLM: 仅接受当前协议值；未知字符串不得被猜成 completed。
# 函数用途: 校验外部或持久化的结束原因，未知值返回空串供调用方重新按事实归一化。
def normalize_turn_end_reason(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in TURN_END_REASONS else ""


# LLM: 优先级是显式宿主 reason > provider stop_reason > runtime status/reason；
# 任何分支都不得查看 assistant 正文、verification_status、tests 或 artifact。
# 函数用途: 把现有运行状态转换成唯一的 宿主协议结束原因。
def infer_turn_end_reason(
    *,
    explicit: object = "",
    runtime_status: object = "",
    runtime_reason: object = "",
    stop_reason: object = "",
) -> str:
    selected = normalize_turn_end_reason(explicit)
    if selected:
        return selected

    provider_reason = str(stop_reason or "").strip().lower()
    reason = str(runtime_reason or "").strip().lower()
    status = str(runtime_status or "ok").strip().lower() or "ok"
    if provider_reason in _MAX_TOKEN_REASONS or reason in _MAX_TOKEN_REASONS:
        return TurnEndReason.MAX_TOKENS.value
    if reason in _ABORT_REASONS or status in {"cancelled", "aborted"}:
        return TurnEndReason.ABORTED.value
    if status in {"blocked", "needs_user_input", "approval_required"}:
        return TurnEndReason.BLOCKED.value
    if status in {"failed", "error"}:
        return TurnEndReason.ERROR.value
    if status == "ok":
        return TurnEndReason.COMPLETED.value
    return TurnEndReason.INTERRUPTED.value


# LLM: 只读取宿主结果 DTO 的结构化结束字段；字典和本地结果对象共享优先级，不能解析 response 或 error 正文。
# 函数用途: 给持久化和前端统一取得本轮真正的结束原因；不修改结果、不启动恢复。
def result_turn_end_reason(value: object) -> str:
    field = value.get if isinstance(value, Mapping) else lambda key: getattr(value, key, "")
    return infer_turn_end_reason(
        explicit=field("turn_end_reason"),
        runtime_status=field("runtime_status"),
        runtime_reason=field("runtime_reason"),
    )


# LLM: 这是只读技术提示，不是模型答复、生命周期状态或自动重试命令；live/history/main/child 复用同一文案。
# 函数用途: 当宿主明确记录长度限制时向用户说明回复可能不完整，未知状态不猜测。
def turn_end_notice(reason: object) -> str:
    if normalize_turn_end_reason(reason) == TurnEndReason.MAX_TOKENS.value:
        return "本次模型响应达到长度限制，回复可能不完整。"
    return ""


# LLM: 子代理生命周期只能由 turn_end_reason 映射；模型输出的 status/summary
# 不能覆盖这里的 host-owned 结果。返回值依次为 task status、failure type、ok。
# 函数用途: 将一轮结束原因投影成现有子代理状态，供持久化和父代理通知使用。
def subagent_outcome_for_turn_end(reason: object) -> tuple[str, str, bool]:
    selected = normalize_turn_end_reason(reason) or TurnEndReason.ERROR.value
    if selected == TurnEndReason.COMPLETED.value:
        return "DONE", "", True
    if selected == TurnEndReason.BLOCKED.value:
        return "BLOCKED", "status_blocked", False
    if selected == TurnEndReason.ABORTED.value:
        return "CANCELLED", "cancelled", False
    if selected == TurnEndReason.MAX_TOKENS.value:
        return "PENDING", "model_error", False
    if selected == TurnEndReason.INTERRUPTED.value:
        return "PENDING", "", False
    return "FAILED", "runner_error", False


# LLM: 这些是宿主技术结束原因；普通 final 不据此自动调度，续跑仍要求 Goal 或显式 resume 授权。
# 只承接原有状态判据，不用任务质量、回复正文或工具名称推测续跑。
CONTINUABLE_REASONS = frozenset({
    "TASK_PROGRESS_OPEN", "TOOL_ROUND_LIMIT_REACHED", "REPEATED_TOOL_FAILURE",
    "TOOL_CALL_UNCLOSED", "MODEL_RESPONSE_TRUNCATED", "CONTEXT_OVERFLOW",
})


# LLM: 读取精确 reason/source/status；TOOL_CALL_UNCLOSED、截断与超窗必须匹配各自宿主来源，不能放宽为仅原因匹配。
# 函数用途: 统一 CLI、finalization 与后台的技术续跑判据，只返回事实，不安排下一轮或改任务状态。
def should_continue_task(final_response: object) -> tuple[bool, str]:
    """判断一次收口是否允许由 Goal 或显式 resume 继续(结构化)。

    返回 (should, reason): should=True 只表示技术上可继续，不代表普通任务会
    自动调度。blocked/协议违规/UNKNOWN effect 等一律 False。

    显式 Goal 的 finalization 与 CLI 手动/Goal 驱动共用这条精确条件。
    """
    reason = str(getattr(final_response, "runtime_reason", "") or "").strip().upper()
    source = str(getattr(final_response, "runtime_source", "") or "").strip()
    status = str(getattr(final_response, "runtime_status", "") or "").strip().lower()
    if reason in CONTINUABLE_REASONS:
        if reason == "TOOL_CALL_UNCLOSED" and not (
            source == "tool_protocol_adapter" and status == "unfinished"
        ):
            return False, reason or "not_continuable"
        if reason == "MODEL_RESPONSE_TRUNCATED" and not (
            source == "tool_loop" and status == "unfinished"
        ):
            return False, reason or "not_continuable"
        if reason == "CONTEXT_OVERFLOW" and not (
            source == "preflight" and status == "context_overflow"
        ):
            return False, reason or "not_continuable"
        return True, reason
    return False, reason or "not_continuable"


__all__ = [
    "TURN_END_REASONS",
    "TurnEndReason",
    "should_continue_task",
    "infer_turn_end_reason",
    "normalize_turn_end_reason",
    "result_turn_end_reason",
    "subagent_outcome_for_turn_end",
    "turn_end_notice",
]
