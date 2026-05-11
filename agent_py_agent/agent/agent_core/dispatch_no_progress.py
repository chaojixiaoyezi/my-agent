# LLM: Agent core helper module for detecting dispatch rounds that only repeat audit bookkeeping.
# 模块用途: 给父级调度循环提供 no-progress 判断，避免 root 完成后被重复审计记录拖住。

from __future__ import annotations

from dataclasses import dataclass


# LLM: DispatchNoProgressTracker owns repeated audit-only round detection for dispatch_loop.
# 类用途: 记录上一轮无进展签名，并在连续重复时通知外层调度循环停止；不读写磁盘或任务状态。
@dataclass
class DispatchNoProgressTracker:
    previous_signature: tuple | None = None
    repeated_rounds: int = 0

    # LLM: should_stop updates the signature counter and returns True only after repeated no-progress rounds.
    # 函数用途: 判断当前调度报告是否连续重复且没有实际推进，用于让父级循环自然退出。
    def should_stop(self, dispatch_report) -> bool:
        signature = _dispatch_no_progress_signature(dispatch_report)
        if signature is None:
            self.previous_signature = None
            self.repeated_rounds = 0
            return False
        if signature == self.previous_signature:
            self.repeated_rounds += 1
        else:
            self.previous_signature = signature
            self.repeated_rounds = 1
        return self.repeated_rounds >= 2


# LLM: _dispatch_no_progress_signature protects parent dispatch from repeating identical audit-only work forever.
# 函数用途: 给一轮调度生成“无实际推进”的稳定签名；如果本轮创建孩子、改变状态或执行动作，则返回 None 让循环继续。
def _dispatch_no_progress_signature(dispatch_report) -> tuple | None:
    records = list(getattr(dispatch_report, "records", []) or [])
    if not records:
        return ()

    signature = []
    for record in records:
        if _dispatch_record_made_progress(record):
            return None
        record_signature = _dispatch_record_signature(record)
        if record_signature is None:
            return None
        signature.append(record_signature)
    return tuple(signature)


# LLM: _dispatch_record_signature ignores volatile IDs while preserving the work identity.
# 函数用途: 把调度记录压成可比较的稳定字段，避免 created_at/id 不同导致重复轮次无法识别。
def _dispatch_record_signature(record) -> tuple | None:
    values = (
        _safe_str(record, "step"),
        _safe_str(record, "action"),
        _safe_str(record, "run_id"),
        _safe_str(record, "before_status"),
        _safe_str(record, "after_status"),
        _safe_str(record, "before_verification_status"),
        _safe_str(record, "after_verification_status"),
    )
    if any(value is None for value in values[:2]):
        return None
    return values


# LLM: _dispatch_record_made_progress distinguishes real state movement from audit-only bookkeeping.
# 函数用途: 判断单条调度记录是否真的推进了子代理树，防止 record-only 记录被当成无限循环的进展。
def _dispatch_record_made_progress(record) -> bool:
    if _safe_int(record, "runner_created_child_count") > 0:
        return True
    if _safe_list(record, "runner_created_child_ids"):
        return True
    if _safe_bool(record, "parent_acceptance_policy_executed"):
        return True
    if _safe_bool(record, "parent_acceptance_auto_execution_executed"):
        return True

    before_status = _safe_str(record, "before_status")
    after_status = _safe_str(record, "after_status")
    if before_status and after_status and before_status != after_status:
        return True

    before_verification = _safe_str(record, "before_verification_status")
    after_verification = _safe_str(record, "after_verification_status")
    if before_verification and after_verification and before_verification != after_verification:
        return True

    return _record_action_is_mutating(record)


# LLM: _record_action_is_mutating keeps explicit record-only actions out of progress accounting.
# 函数用途: 对没有状态字段的记录做保守判断，只有明确的非审计动作才算推进。
def _record_action_is_mutating(record) -> bool:
    step = _safe_str(record, "step") or ""
    action = _safe_str(record, "action") or ""
    applied = _safe_bool(record, "applied")
    if not applied:
        return False
    if step == "action_apply" and action in _RECORD_ONLY_ACTIONS:
        return False
    if (step, action) in _AUDIT_ONLY_ACTIONS:
        return False
    return True


_RECORD_ONLY_ACTIONS = {
    "classify_blocker",
    "inspect_failure",
    "recover_child_after_parent_timeout",
    "route_capability_request",
    "triage_capability_gap",
}

_AUDIT_ONLY_ACTIONS = {
    ("due_check", "scan"),
    ("leadership_recovery_plan", "inspect_refs"),
}


# LLM: _safe_str reads only concrete string fields so MagicMock-based legacy tests keep old behavior.
# 函数用途: 安全读取调度记录字符串字段，未知或 mock 值返回 None，避免误判为可比较状态。
def _safe_str(record, field_name: str) -> str | None:
    value = getattr(record, field_name, None)
    if isinstance(value, str):
        return value
    return None


# LLM: _safe_bool reads concrete booleans without letting truthy mock objects affect dispatch progress.
# 函数用途: 安全读取布尔字段，只有真实 bool 才参与推进判断。
def _safe_bool(record, field_name: str) -> bool:
    value = getattr(record, field_name, False)
    return value if isinstance(value, bool) else False


# LLM: _safe_int reads concrete integers for child creation counters.
# 函数用途: 安全读取计数字段，非整数按 0 处理，避免 mock 或空值污染判断。
def _safe_int(record, field_name: str) -> int:
    value = getattr(record, field_name, 0)
    return value if isinstance(value, int) else 0


# LLM: _safe_list reads concrete list refs for child creation evidence.
# 函数用途: 安全读取列表字段，非列表按空处理，避免 mock 或字符串被误当作真实 refs。
def _safe_list(record, field_name: str) -> list:
    value = getattr(record, field_name, [])
    return value if isinstance(value, list) else []


__all__ = ["DispatchNoProgressTracker"]
