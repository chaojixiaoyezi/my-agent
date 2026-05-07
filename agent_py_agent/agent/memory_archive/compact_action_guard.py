# LLM: Compact action guard is the safety lock before any automated compact resume continues work.
# 模块用途: 根据 compact resume 的一致性报告和 work state，判断手动/自动恢复后是否允许继续动作。

from __future__ import annotations

"""action guard for compact resume safety."""

from dataclasses import dataclass
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_ACTION_GUARD_SCHEMA = RuntimeMemorySchemaOptions("compact_action_guard")


# LLM: CompactActionGuardOptions keeps manual and future unattended modes explicit.
# 类用途: 描述 action guard 的运行模式和 owner，防止自动恢复逻辑隐藏在普通 resume 中。
@dataclass(frozen=True)
class CompactActionGuardOptions:
    mode: str = "manual"
    owner_type: str = "main_agent"
    owner_id: str = ""


# LLM: CompactActionGuardRequest bundles compact resume state for deterministic gating.
# 类用途: 汇总 consistency report、work_state 和 refs，用于生成是否允许继续动作的状态锁报告。
@dataclass(frozen=True)
class CompactActionGuardRequest:
    consistency_report: dict[str, Any]
    work_state: dict[str, Any]
    refs: dict[str, Any]
    options: CompactActionGuardOptions


# LLM: build_compact_action_guard never runs tools; it only emits a machine-readable go/no-go report.
# 函数用途: 生成 compact resume 后的动作守门报告，自动模式缺字段或 refs 异常时必须阻断。
def build_compact_action_guard(request: CompactActionGuardRequest) -> dict[str, Any]:
    mode = _mode(request.options.mode)
    checks = _guard_checks(request, mode)
    hard_ok = all(item["ok"] for item in checks if item["severity"] == "hard")
    status = _guard_status(mode, hard_ok, _missing_fields(request.work_state))
    return {
        "version": COMPACT_ACTION_GUARD_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_ACTION_GUARD_SCHEMA),
        "ok": hard_ok,
        "mode": mode,
        "status": status,
        "allowed_to_continue": status == "allow_automated_continue",
        "allowed_next_action": _allowed_next_action(status),
        "automatic_tool_execution": "none",
        "owner": _owner_payload(request.options),
        "apply_id": request.consistency_report.get("apply_id", ""),
        "plan_id": request.consistency_report.get("plan_id", ""),
        "missing_fields": _missing_fields(request.work_state),
        "checks": checks,
        "reserved": runtime_memory_reserved_fields(COMPACT_ACTION_GUARD_SCHEMA),
    }


# LLM: _guard_checks separates manual confirmation from automated continuation requirements.
# 函数用途: 生成 action guard 检查项；自动模式把缺失工作状态字段作为 hard 阻断。
def _guard_checks(request: CompactActionGuardRequest, mode: str) -> list[dict[str, Any]]:
    work_state = request.work_state
    consistency = request.consistency_report
    missing = _missing_fields(work_state)
    return [
        {"name": "consistency_ok", "ok": bool(consistency.get("ok")), "severity": "hard"},
        {"name": "goal_present", "ok": bool(work_state.get("goal")), "severity": "hard"},
        {"name": "next_step_present", "ok": bool(work_state.get("next_step")), "severity": "hard"},
        {"name": "restore_refs_present", "ok": bool(request.refs.get("restore_refs")), "severity": "hard"},
        {"name": "self_check_present", "ok": bool(request.refs.get("post_compact_self_check")), "severity": "hard"},
        {"name": "auto_missing_fields_clear", "ok": mode != "auto" or not missing, "severity": "hard"},
        {"name": "manual_confirmation_required", "ok": mode == "manual", "severity": "soft"},
    ]


# LLM: _guard_status is intentionally conservative for unattended compact/resume.
# 函数用途: 将检查结果映射为明确状态；自动模式缺字段时进入 blocked。
def _guard_status(mode: str, hard_ok: bool, missing_fields: list[str]) -> str:
    if not hard_ok and mode == "auto" and missing_fields:
        return "blocked_missing_work_state_fields"
    if not hard_ok:
        return "blocked_needs_human_review"
    if mode == "auto":
        return "allow_automated_continue"
    return "requires_user_confirmation"


# LLM: _allowed_next_action makes downstream automation handle the guard without interpreting prose.
# 函数用途: 返回机器可读的下一步动作策略，避免自动流程误把人工模式当成可继续。
def _allowed_next_action(status: str) -> str:
    if status == "allow_automated_continue":
        return "continue_after_guard"
    if status == "requires_user_confirmation":
        return "manual_review_then_continue"
    return "stop_and_request_review"


# LLM: _missing_fields normalizes work-state gaps for action guard comparisons.
# 函数用途: 从 work_state_snapshot 中读取缺失字段列表，异常形态按缺失处理。
def _missing_fields(work_state: dict[str, Any]) -> list[str]:
    value = work_state.get("missing_fields", [])
    return [str(item) for item in value] if isinstance(value, list) else ["missing_fields"]


# LLM: _mode clamps unknown modes to manual to keep new automation opt-in.
# 函数用途: 归一化 action guard 模式；未知值按 manual 处理，不静默开启自动继续。
def _mode(value: str) -> str:
    return value if value in {"manual", "auto"} else "manual"


# LLM: _owner_payload records whether the guard is for main-agent or future subagent session compaction.
# 函数用途: 生成 owner 字段，供未来子代理自动会话压缩复用同一守门报告。
def _owner_payload(options: CompactActionGuardOptions) -> dict[str, str]:
    return {"owner_type": options.owner_type, "owner_id": options.owner_id}


__all__ = [
    "COMPACT_ACTION_GUARD_SCHEMA",
    "CompactActionGuardOptions",
    "CompactActionGuardRequest",
    "build_compact_action_guard",
]
