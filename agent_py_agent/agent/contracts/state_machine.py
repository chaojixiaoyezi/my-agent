# LLM: Shared state-machine facts separate lifecycle truth from prompt workflow preferences.
# 模块用途: 定义主代理/子代理都能复用的状态事实和调度判断，不把流程写死成 guard。

from __future__ import annotations

from dataclasses import dataclass

DISPATCHABLE_STATES = {"PLANNING", "PENDING"}
ACTIVE_STATES = {"RUNNING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "WAITING_FOR_USER", "REPAIRING", "TAKING_OVER"}
TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED", "ABANDONED"}
REPAIRABLE_STATES = {"BLOCKED", "FAILED"}
VERIFIED_STATES = {"VERIFIED"}


# LLM: RunStateFacts is a small typed snapshot for dispatch/recovery decisions.
# 类用途: 保存一个 run 的状态、验收状态、失败类型和尝试次数，让状态判断不依赖自然语言。
@dataclass(frozen=True)
class RunStateFacts:
    status: str
    verification_status: str = ""
    failure_type: str = ""
    attempts: int = 0
    max_attempts: int = 0
    has_progress: bool = True


# LLM: RecoveryDecision tells orchestration whether to repair, takeover, wait, or stop.
# 类用途: 表达状态机给调度层的下一步建议；它不直接执行工具或创建新 run。
@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    allow_new_run: bool
    reason: str


# LLM: normalize_status keeps legacy status strings compatible with the shared state machine.
# 函数用途: 把空值、大小写和旧 WAIT_CHILD 写法规整为统一状态名。
def normalize_status(value: object) -> str:
    text = str(value or "").strip().upper()
    if text == "WAIT_CHILD":
        return "WAITING_FOR_CHILD"
    return text or "PLANNING"


# LLM: normalize_verification keeps closeout checks independent from spelling variants.
# 函数用途: 规整验收状态；空值保持 UNVERIFIED，避免 DONE 被误当 VERIFIED。
def normalize_verification(value: object) -> str:
    return str(value or "UNVERIFIED").strip().upper() or "UNVERIFIED"


# LLM: can_dispatch answers whether a run is eligible to start now.
# 函数用途: 判断 run 是否能 dispatch；RUNNING/DONE/BLOCKED 不会被重复启动。
def can_dispatch(facts: RunStateFacts) -> bool:
    return normalize_status(facts.status) in DISPATCHABLE_STATES


# LLM: can_closeout answers whether parent/root can report the run as actually complete.
# 函数用途: 只有 DONE 且 VERIFIED 才允许 closeout，避免完成和验收混淆。
def can_closeout(facts: RunStateFacts) -> bool:
    return normalize_status(facts.status) == "DONE" and normalize_verification(facts.verification_status) in VERIFIED_STATES


# LLM: can_repair answers whether a run should be repaired before creating unrelated new work.
# 函数用途: 判断失败/阻塞 run 是否适合进入 repair，而不是被父级误当完成或无限扩容。
def can_repair(facts: RunStateFacts) -> bool:
    status = normalize_status(facts.status)
    if status == "BLOCKED":
        return True
    return status == "FAILED" and (facts.max_attempts <= 0 or facts.attempts < facts.max_attempts)


# LLM: recovery_decision is deliberately advisory; workflow choice remains with the caller/LLM.
# 函数用途: 根据状态事实返回修复、接管、等待或停止建议，不直接改变任务状态。
def recovery_decision(facts: RunStateFacts) -> RecoveryDecision:
    status = normalize_status(facts.status)
    failure = str(facts.failure_type or "").upper()
    if can_closeout(facts):
        return RecoveryDecision("closeout", False, "done_verified")
    if status in DISPATCHABLE_STATES:
        return RecoveryDecision("dispatch", False, "not_started")
    if status in ACTIVE_STATES:
        return RecoveryDecision("wait_or_observe", False, "already_active")
    if status == "BLOCKED" and failure in {"TOOL_UNAVAILABLE", "WRITE_FORBIDDEN", "PATH_OUTSIDE_WORKSPACE"}:
        return RecoveryDecision("repair_or_request_capability", False, f"blocked_{failure.lower()}")
    if can_repair(facts):
        return RecoveryDecision("repair", False, "repairable_failure")
    if status == "FAILED":
        return RecoveryDecision("takeover_or_stop", True, "attempts_exhausted")
    return RecoveryDecision("manual_review", False, f"unhandled_state_{status.lower()}")


__all__ = [
    "RunStateFacts",
    "RecoveryDecision",
    "can_closeout",
    "can_dispatch",
    "can_repair",
    "normalize_status",
    "normalize_verification",
    "recovery_decision",
]
