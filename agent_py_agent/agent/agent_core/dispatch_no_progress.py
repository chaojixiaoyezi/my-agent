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


# LLM: dispatch_made_progress gives watch/daemon loops the same progress definition as dispatch_loop.
# 函数用途: 判断 dispatch 报告是否真的推进了任务树；watch 循环用它区分活跃工作和重复审计。
def dispatch_made_progress(dispatch_report) -> bool:
    return _dispatch_no_progress_signature(dispatch_report) is None


# LLM: dispatch_no_progress_payload exposes no-progress diagnosis to model-facing dispatch tools.
# 函数用途: 当 dispatch_subagents 只做重复审计/分类而没有真实推进时，返回机器可读提示，避免父模型继续空转调用。
def dispatch_no_progress_payload(dispatch_report) -> dict[str, object]:
    signature = _dispatch_no_progress_signature(dispatch_report)
    if signature is None or not signature:
        return {}
    records = list(getattr(dispatch_report, "records", []) or [])
    payload = {
        "no_progress_actions_only": True,
        "recommended_next_action": "summarize_blockers_or_change_strategy",
        "reason": (
            "本轮 dispatch 只有 due-check、inspect 或 classify 等记录类动作；"
            "没有创建子代理、状态变化或验收执行。请不要原样重复 dispatch，改为查看 refs、换策略或汇报 blockers。"
        ),
        "record_count": len(records),
        "blocked_run_ids": _record_run_ids(records),
    }
    if tool_call := _dry_run_recovery_tool_call(records):
        payload["recommended_next_action"] = "rerun_dispatch_with_apply_for_recovery"
        payload["reason"] = (
            "本轮只做了 dry-run，已经发现可写回的恢复动作。"
            "如果当前父级确实要接管/重分配，请按 suggested_tool_call 重新调用；"
            "runner 内部会默认用当前父级 run_id 作为 take_over_by。"
        )
        payload["suggested_tool_call"] = tool_call
    return payload


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
    "stop_no_progress_and_escalate",
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


# LLM: _record_run_ids keeps terminal hints concrete without copying bulky record bodies.
# 函数用途: 从无进展调度记录中提取涉及的 run_id，供父模型最终汇报而不是继续重复调度。
def _record_run_ids(records: list[object]) -> list[str]:
    run_ids: list[str] = []
    for record in records:
        run_id = _safe_str(record, "run_id") or ""
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
    return run_ids[:20]


# LLM: _dry_run_recovery_tool_call turns a non-mutating recovery preview into a safe exact next call.
# 函数用途: 当父模型误把恢复接管跑成 dry-run 时，返回可复制的真实推进参数，避免继续空转或误新建 repair。
def _dry_run_recovery_tool_call(records: list[object]) -> dict[str, object]:
    if not any(_is_dry_run_recovery_apply(record) for record in records):
        return {}
    return {
        "tool": "dispatch_subagents",
        "dry_run": False,
        "workflow_mode": "off",
        "max_runners": 0,
        "limit": max(len(records), 1),
    }


# LLM: _is_dry_run_recovery_apply recognizes safe recovery actions that need a real dispatch pass to mutate state.
# 函数用途: 只对接管/领导权恢复这类恢复写回给建议，普通 classify dry-run 仍按阻塞项汇报。
def _is_dry_run_recovery_apply(record: object) -> bool:
    return (
        _safe_str(record, "step") == "action_apply"
        and _safe_str(record, "action") in {"takeover_or_reassign", "recover_coordinator_leadership"}
        and _safe_bool(record, "dry_run")
        and not _safe_bool(record, "applied")
    )


__all__ = ["DispatchNoProgressTracker", "dispatch_made_progress", "dispatch_no_progress_payload"]
