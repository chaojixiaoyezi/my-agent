# LLM: Shared state-transition contracts keep lifecycle legality machine-readable across orchestration and replay.
# 模块用途: 定义主代理运行状态允许流向、迁移原因和序列校验，避免上层根据自然语言猜“这步能不能跳过去”。

from __future__ import annotations

from dataclasses import dataclass

from .state_machine import normalize_status

_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PLANNING": ("PENDING", "RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED"),
    "PENDING": ("RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED"),
    "RUNNING": (
        "WAITING_FOR_TOOL",
        "WAITING_FOR_USER",
        "WAITING_FOR_CHILD",
        "VERIFYING",
        "BLOCKED",
        "FAILED",
        "DONE",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "CANCELLED",
    ),
    "WAITING_FOR_TOOL": ("RUNNING", "BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "CANCELLED"),
    "WAITING_FOR_USER": ("RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED"),
    "WAITING_FOR_CHILD": ("RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED"),
    "VERIFYING": ("DONE", "RUNNING", "BLOCKED", "FAILED", "CANCELLED"),
    "BLOCKED": ("REPAIRING", "TAKING_OVER", "RUNNING", "FAILED", "CANCELLED", "ABANDONED"),
    "REPAIRING": ("RUNNING", "VERIFYING", "BLOCKED", "FAILED", "CANCELLED"),
    "TAKING_OVER": ("RUNNING", "VERIFYING", "BLOCKED", "FAILED", "CANCELLED"),
    "FAILED": ("REPAIRING", "TAKING_OVER", "CANCELLED", "ABANDONED"),
    "TIMEOUT": ("REPAIRING", "TAKING_OVER", "FAILED", "CANCELLED", "ABANDONED"),
    "CHANNEL_ERROR": ("REPAIRING", "TAKING_OVER", "FAILED", "CANCELLED", "ABANDONED"),
    "DONE": (),
    "CANCELLED": (),
    "ABANDONED": (),
}


# LLM: TransitionContract exposes whether one status hop is legal and why.
# 类用途: 保存状态迁移是否允许、失败原因和调用方可展示的稳定条件描述。
@dataclass(frozen=True)
class TransitionContract:
    allowed: bool
    from_status: str
    to_status: str
    reason: str
    required_condition: str


# LLM: allowed_next_statuses keeps callers from re-encoding the transition table in scattered guards.
# 函数用途: 返回某个状态允许流向的下一批状态；未知状态按空列表处理。
def allowed_next_statuses(status: object) -> tuple[str, ...]:
    return _TRANSITIONS.get(normalize_status(status), ())


# LLM: transition_contract answers whether one lifecycle hop is legal without needing prompt-derived workflow hints.
# 函数用途: 校验 from/to 状态是否符合共享状态机，供 replay、控制面和调度测试统一使用。
def transition_contract(from_status: object, to_status: object) -> TransitionContract:
    source = normalize_status(from_status)
    target = normalize_status(to_status)
    if source == target:
        return TransitionContract(True, source, target, "same_state", "idempotent_snapshot_repeat")
    if target in allowed_next_statuses(source):
        return TransitionContract(True, source, target, "allowed_transition", _required_condition(source, target))
    if source not in _TRANSITIONS:
        return TransitionContract(False, source, target, "unknown_source_state", "source_state_must_be_registered")
    return TransitionContract(False, source, target, "disallowed_transition", _required_condition(source, target))


# LLM: first_invalid_transition reports the earliest broken hop in a state sequence.
# 函数用途: 校验 replay/state trace 序列是否合法；全部合法时返回 None。
def first_invalid_transition(statuses: list[object]) -> TransitionContract | None:
    normalized = [normalize_status(item) for item in statuses if str(item or "").strip()]
    for current, nxt in zip(normalized, normalized[1:], strict=False):
        contract = transition_contract(current, nxt)
        if not contract.allowed:
            return contract
    return None


# LLM: _required_condition keeps reasons short and generic for UI/replay diagnostics.
# 函数用途: 给每类迁移返回稳定条件名，避免诊断文本又退回自由描述。
def _required_condition(source: str, target: str) -> str:
    if target == "VERIFYING":
        return "artifact_written_and_pending_acceptance"
    if source == "VERIFYING" and target == "DONE":
        return "acceptance_passed"
    if target == "RUNNING" and source in {"BLOCKED", "REPAIRING", "TAKING_OVER"}:
        return "repair_or_takeover_resolved"
    if target in {"REPAIRING", "TAKING_OVER"}:
        return "terminal_or_blocked_failure_detected"
    if target == "WAITING_FOR_TOOL":
        return "tool_call_in_flight"
    if target == "WAITING_FOR_USER":
        return "approval_or_user_input_required"
    if target == "WAITING_FOR_CHILD":
        return "child_run_in_flight"
    if target in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return "runtime_or_contract_failure_detected"
    if target == "DONE":
        return "final_artifact_ready"
    if target in {"CANCELLED", "ABANDONED"}:
        return "stop_requested_or_run_retired"
    return "registered_lifecycle_transition"


__all__ = [
    "TransitionContract",
    "allowed_next_statuses",
    "first_invalid_transition",
    "transition_contract",
]
