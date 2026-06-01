# LLM: Shared state-machine facts separate lifecycle truth from prompt workflow preferences.
# 模块用途: 定义主代理/子代理都能复用的状态事实和调度判断，不把流程写死成 guard。

from __future__ import annotations

import logging
from dataclasses import dataclass

from .error_taxonomy import classify_error, error_contract
from .recovery_actions import (
    ACTION_CHANGE_STRATEGY_OR_STOP,
    ACTION_CLOSEOUT,
    ACTION_DISPATCH,
    ACTION_MANUAL_REVIEW,
    ACTION_REPAIR,
    ACTION_REPAIR_OR_PROBE_CHANNEL,
    ACTION_REPAIR_OR_REQUEST_CAPABILITY,
    ACTION_REQUEST_APPROVAL_OR_STOP,
    ACTION_TAKEOVER_OR_STOP,
    ACTION_WAIT_FOR_ACCEPTANCE,
    ACTION_WAIT_FOR_LOCAL_PROGRESS,
    ACTION_WAIT_OR_OBSERVE,
    RecoveryAction,
    recovery_action_value,
)

SCHEMA_VERSION = "state_machine.v1"
DISPATCHABLE_STATES = {"PLANNING", "PENDING"}
ACTIVE_STATES = {"RUNNING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "WAITING_FOR_USER", "REPAIRING", "TAKING_OVER"}
TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED", "ABANDONED", "TIMEOUT", "CHANNEL_ERROR"}
REPAIRABLE_STATES = {"BLOCKED", "FAILED"}
VERIFIED_STATES = {"VERIFIED"}
HEALTHY_CHANNEL_STATES = {"", "OK", "UNKNOWN"}
LOGGER = logging.getLogger(__name__)


# LLM: RunStateFacts is a small typed snapshot for dispatch/recovery decisions.
# 类用途: 保存一个 run 的状态、验收状态、失败类型和尝试次数，让状态判断不依赖自然语言。
@dataclass(frozen=True)
class RunStateFacts:
    status: str
    verification_status: str = ""
    channel_status: str = ""
    failure_type: str = ""
    attempts: int = 0
    max_attempts: int = 0
    has_progress: bool = True


# LLM: RecoveryDecision tells orchestration whether to repair, takeover, wait, or stop.
# 类用途: 表达状态机给调度层的下一步建议；它不直接执行工具或创建新 run。
@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction | str
    allow_new_run: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", recovery_action_value(self.action))


# LLM: normalize_status keeps legacy status strings compatible with the shared state machine.
# 函数用途: 把空值、大小写和旧 WAIT_CHILD 写法规整为统一状态名。
def normalize_status(value: object) -> str:
    text = str(value or "").strip().upper()
    if text == "WAIT_CHILD":
        return "WAITING_FOR_CHILD"
    if text == "PLANNED":
        return "PLANNING"
    if text == "QUEUED":
        return "PENDING"
    if text == "COMPLETED":
        return "DONE"
    if text == "ERROR":
        return "FAILED"
    return text or "PLANNING"


# LLM: normalize_verification keeps closeout checks independent from spelling variants.
# 函数用途: 规整验收状态；空值保持 UNVERIFIED，避免 DONE 被误当 VERIFIED。
def normalize_verification(value: object) -> str:
    return str(value or "UNVERIFIED").strip().upper() or "UNVERIFIED"


# LLM: normalize_channel keeps closeout and repair decisions independent from spelling variants.
# 函数用途: 规整通道状态；缺失值按 UNKNOWN 处理，只有明确 BROKEN 才会阻止 closeout。
def normalize_channel(value: object) -> str:
    return str(value or "UNKNOWN").strip().upper() or "UNKNOWN"


# LLM: can_dispatch answers whether a run is eligible to start now.
# 函数用途: 判断 run 是否能 dispatch；RUNNING/DONE/BLOCKED 不会被重复启动。
def can_dispatch(facts: RunStateFacts, *, force: bool = False) -> bool:
    status = normalize_status(facts.status)
    if status in DISPATCHABLE_STATES:
        return True
    if not force:
        return False
    return status in {"FAILED", "ABANDONED"} and _attempts_available(facts)


# LLM: can_closeout answers whether parent/root can report the run as actually complete.
# 函数用途: 只有 DONE 且 VERIFIED 才允许 closeout，避免完成和验收混淆。
def can_closeout(facts: RunStateFacts) -> bool:
    return (
        normalize_status(facts.status) == "DONE"
        and normalize_verification(facts.verification_status) in VERIFIED_STATES
        and normalize_channel(facts.channel_status) in HEALTHY_CHANNEL_STATES
    )


# LLM: can_repair answers whether a run should be repaired before creating unrelated new work.
# 函数用途: 判断失败/阻塞 run 是否适合进入 repair，而不是被父级误当完成或无限扩容。
def can_repair(facts: RunStateFacts) -> bool:
    status = normalize_status(facts.status)
    if normalize_channel(facts.channel_status) == "BROKEN":
        return _attempts_available(facts)
    if status == "BLOCKED":
        return _attempts_available(facts)
    if status in {"TIMEOUT", "CHANNEL_ERROR"}:
        return _attempts_available(facts)
    return status == "FAILED" and _attempts_available(facts)


# LLM: waiting_reason exposes why execution is paused without asking callers to parse status prose.
# 函数用途: 把等待用户、等待工具和等待本地进展整理成稳定原因字段，供控制面和 UI 统一使用。
def waiting_reason(facts: RunStateFacts) -> str:
    status = normalize_status(facts.status)
    verification = normalize_verification(facts.verification_status)
    failure = str(facts.failure_type or "").upper()
    if failure == "APPROVAL_REQUIRED":
        return "approval"
    if status == "WAITING_FOR_USER":
        return "user"
    if status == "WAITING_FOR_TOOL":
        return "tool"
    if status == "WAITING_FOR_CHILD":
        return "child"
    if status == "VERIFYING":
        return "verification"
    if status == "DONE" and verification not in VERIFIED_STATES:
        return "verification"
    if status == "RUNNING" and not facts.has_progress:
        return "local_progress"
    return "none"


# LLM: terminal_outcome separates active/blocked/completed/timed_out meanings from raw status spelling.
# 函数用途: 给终态和等待态补一个统一结果标签，避免上层只看 status 文本就误判 run 是否真的完成。
def terminal_outcome(facts: RunStateFacts) -> str:
    status = normalize_status(facts.status)
    if can_closeout(facts):
        return "completed"
    if status == "TIMEOUT":
        return "timed_out"
    if status in {"CANCELLED", "ABANDONED"}:
        return "cancelled"
    if status in {"BLOCKED", "DONE"}:
        return "blocked"
    if status in {"FAILED", "CHANNEL_ERROR"} or normalize_channel(facts.channel_status) == "BROKEN":
        return "failed"
    return "active"


# LLM: lifecycle_phase projects shared status facts into a stable orchestration-facing phase.
# 函数用途: 把状态、验收、通道和进展规整成 WAITING/VERIFYING/BLOCKED/DONE 这类统一生命周期阶段。
def lifecycle_phase(facts: RunStateFacts) -> str:
    status = normalize_status(facts.status)
    channel = normalize_channel(facts.channel_status)
    reason = waiting_reason(facts)
    if channel == "BROKEN":
        return "BLOCKED"
    if status in {"TIMEOUT", "CHANNEL_ERROR"}:
        return "BLOCKED"
    if reason in {"approval", "user"}:
        return "WAITING_FOR_USER"
    if reason == "acceptance":
        return "VERIFYING"
    if reason == "local_progress":
        return "WAITING_FOR_LOCAL_PROGRESS"
    if reason == "tool":
        return "WAITING_FOR_TOOL"
    if reason == "child":
        return "WAITING_FOR_CHILD"
    if status in {"BLOCKED", "FAILED", "CANCELLED", "ABANDONED"}:
        return "BLOCKED"
    if status in {"PLANNING", "PENDING", "RUNNING"}:
        return status
    return "DONE" if can_closeout(facts) else status


# LLM: recovery_decision is deliberately advisory; workflow choice remains with the caller/LLM.
# 函数用途: 根据状态事实返回修复、接管、等待或停止建议，不直接改变任务状态。
def recovery_decision(facts: RunStateFacts) -> RecoveryDecision:
    status = normalize_status(facts.status)
    channel = normalize_channel(facts.channel_status)
    failure = str(facts.failure_type or "").upper()
    if can_closeout(facts):
        return RecoveryDecision(ACTION_CLOSEOUT, False, "done_verified")
    if channel == "BROKEN":
        return RecoveryDecision(ACTION_REPAIR_OR_PROBE_CHANNEL, False, "channel_broken")
    if status in {"TIMEOUT", "CHANNEL_ERROR"} and can_repair(facts):
        return RecoveryDecision(ACTION_REPAIR, False, "repairable_failure")
    if waiting_reason(facts) == "acceptance":
        return RecoveryDecision(ACTION_WAIT_FOR_ACCEPTANCE, False, "done_unverified")
    if failure == "NO_PROGRESS":
        return RecoveryDecision(ACTION_CHANGE_STRATEGY_OR_STOP, False, "no_progress")
    if failure == "APPROVAL_REQUIRED":
        return RecoveryDecision(ACTION_REQUEST_APPROVAL_OR_STOP, False, "approval_required")
    if status in DISPATCHABLE_STATES:
        return RecoveryDecision(ACTION_DISPATCH, False, "not_started")
    if status == "RUNNING" and not facts.has_progress:
        return RecoveryDecision(ACTION_WAIT_FOR_LOCAL_PROGRESS, False, "running_without_local_progress")
    if status in ACTIVE_STATES:
        return RecoveryDecision(ACTION_WAIT_OR_OBSERVE, False, "already_active")
    if status == "BLOCKED" and failure in {"TOOL_UNAVAILABLE", "WRITE_FORBIDDEN", "PATH_OUTSIDE_WORKSPACE"}:
        if can_repair(facts):
            return RecoveryDecision(ACTION_REPAIR_OR_REQUEST_CAPABILITY, False, f"blocked_{failure.lower()}")
        return RecoveryDecision(ACTION_TAKEOVER_OR_STOP, True, "attempts_exhausted")
    if status == "BLOCKED" and _structured_repair_action(failure):
        if can_repair(facts):
            return RecoveryDecision(_structured_repair_action(failure), False, f"blocked_{failure.lower()}")
        return RecoveryDecision(ACTION_TAKEOVER_OR_STOP, True, "attempts_exhausted")
    if can_repair(facts):
        return RecoveryDecision(ACTION_REPAIR, False, "repairable_failure")
    if status in {"BLOCKED", "FAILED"}:
        return RecoveryDecision(ACTION_TAKEOVER_OR_STOP, True, "attempts_exhausted")
    LOGGER.warning("unhandled recovery state: status=%s failure=%s", status, failure)
    return RecoveryDecision(ACTION_MANUAL_REVIEW, False, f"unhandled_state_{status.lower()}")


# LLM: run_state_snapshot_from_task adapts legacy task objects into the shared state-machine contract.
# 函数用途: 从任意 task-like 对象读取状态、验收、错误和尝试次数，输出机器可读调度/恢复事实。
def run_state_snapshot_from_task(task: object) -> dict[str, object]:
    facts = RunStateFacts(
        status=normalize_status(getattr(task, "status", "")),
        verification_status=normalize_verification(getattr(task, "verification_status", "")),
        channel_status=normalize_channel(getattr(task, "channel_status", "")),
        failure_type=_failure_type_from_task(task),
        attempts=_int_attr(task, "runner_attempts"),
        max_attempts=_int_attr(task, "runner_max_attempts"),
        has_progress=bool(getattr(task, "has_progress", True)),
    )
    decision = recovery_decision(facts)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(getattr(task, "id", "") or ""),
        "status": normalize_status(facts.status),
        "verification_status": normalize_verification(facts.verification_status),
        "channel_status": normalize_channel(facts.channel_status),
        "lifecycle_phase": lifecycle_phase(facts),
        "waiting_reason": waiting_reason(facts),
        "terminal_outcome": terminal_outcome(facts),
        "failure_type": error_contract(facts.failure_type or "UNKNOWN_ERROR").code,
        "attempts": facts.attempts,
        "max_attempts": facts.max_attempts,
        "can_dispatch": can_dispatch(facts),
        "can_closeout": can_closeout(facts),
        "can_repair": can_repair(facts),
        "recovery_decision": {
            "action": decision.action,
            "allow_new_run": decision.allow_new_run,
            "reason": decision.reason,
        },
    }


# LLM: _failure_type_from_task prefers structured failure_type and falls back to taxonomy classification.
# 函数用途: 从任务对象中提取稳定错误类型，避免状态合同只看到自然语言错误。
def _failure_type_from_task(task: object) -> str:
    raw = str(getattr(task, "failure_type", "") or "").strip()
    if raw:
        return error_contract(raw).code
    message = str(getattr(task, "runner_last_error", "") or getattr(task, "error", "") or "").strip()
    return classify_error(message).code if message else "UNKNOWN_ERROR"


# LLM: _int_attr keeps snapshots tolerant of legacy string counters.
# 函数用途: 读取 task 上的整数字段；缺失或坏值按 0 处理。
def _int_attr(task: object, name: str) -> int:
    try:
        return int(getattr(task, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


# LLM: _attempts_available applies the same retry ceiling across failed and blocked states.
# 函数用途: 判断是否还有修复尝试次数，避免 BLOCKED 任务无限 repair。
def _attempts_available(facts: RunStateFacts) -> bool:
    return facts.max_attempts <= 0 or facts.attempts < facts.max_attempts


# LLM: _structured_repair_action maps known failure contracts into repair-loop action names.
# 函数用途: 从 error_contract 读取推荐动作，让 evidence/staged-json 等错误不落到泛化 repair。
def _structured_repair_action(failure: str) -> str:
    contract = error_contract(failure)
    if contract.code == "UNKNOWN_ERROR":
        return ""
    if contract.category in {"artifact", "evidence", "tool", "path", "acceptance", "compact", "model", "orchestration"}:
        return contract.recommended_action
    return ""


__all__ = [
    "SCHEMA_VERSION",
    "RunStateFacts",
    "RecoveryDecision",
    "can_closeout",
    "can_dispatch",
    "can_repair",
    "lifecycle_phase",
    "normalize_channel",
    "normalize_status",
    "normalize_verification",
    "recovery_decision",
    "run_state_snapshot_from_task",
    "terminal_outcome",
    "waiting_reason",
]
