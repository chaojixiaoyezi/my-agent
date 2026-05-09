# LLM: Follow-up control turns post-test recommendations into explicit manual gates.
# 模块用途: 读取父级验收 follow-up，并在人工确认时执行安全 apply 或接管入口。
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any, ClassVar

from .models import SubAgentTask

_INVALID_FOLLOWUP_ERROR = "_invalid_followup_error"


# LLM: ParentAcceptanceFollowUpControlOptions is the explicit bundle for manual follow-up handling.
# 类用途: 保存 follow-up 入口的人工确认选项；新增字段时扩展这个包，避免散参。
@dataclass(frozen=True)
class ParentAcceptanceFollowUpControlOptions:
    """Options for manually handling a parent acceptance follow-up."""

    __test__: ClassVar[bool] = False

    apply: bool = False
    reviewer: str = "parent"
    note: str = ""
    take_over_by: str = ""
    locked_files: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: ParentAcceptanceFollowUpControlResult records the explicit gate outcome.
# 类用途: 保存 follow-up 入口结果；可能只是预览、被拦截，也可能显式 apply 或接管。
@dataclass(frozen=True)
class ParentAcceptanceFollowUpControlResult:
    """Result of a manual follow-up control attempt."""

    __test__: ClassVar[bool] = False

    run_id: str
    status: str
    action: str
    applied: bool
    ok: bool
    message: str
    followup_ref: str = ""
    control_ref: str = ""
    recommended_command: str = ""
    acceptance_apply_ref: str = ""
    takeover_ref: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    mutates_task_state: bool = False
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps CLI JSON and audit payloads stable.
    # 函数用途: 把 follow-up 控制结果转换成 JSON 字典；只包含摘要和引用，不展开正文。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: BlockedFollowUpControlInput keeps blocked-result calls bundle-shaped as reasons grow.
# 类用途: 保存 follow-up 被拦截时的状态、动作、说明和建议命令，避免业务参数继续变长。
@dataclass(frozen=True)
class BlockedFollowUpControlInput:
    """Bundle for a blocked follow-up control result."""

    __test__: ClassVar[bool] = False

    status: str
    action: str
    message: str
    recommended_command: str = ""


# LLM: read_parent_acceptance_followup_payload reads the latest task-local follow-up audit.
# 函数用途: 读取 `parent_acceptance_auto_followup.json`；缺失返回空字典，格式错误返回 invalid 占位。
def read_parent_acceptance_followup_payload(task: SubAgentTask) -> dict[str, Any]:
    path = parent_acceptance_followup_ref(task)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError, TypeError) as exc:
        return {_INVALID_FOLLOWUP_ERROR: str(exc)}
    return payload if isinstance(payload, dict) else {}


# LLM: parent_acceptance_followup_ref centralizes the follow-up audit filename.
# 函数用途: 返回测试执行后 follow-up 审计路径；调用方只保存 ref，不读取正文。
def parent_acceptance_followup_ref(task: SubAgentTask) -> Path:
    return Path(task.reports_dir) / "parent_acceptance_auto_followup.json"


# LLM: parent_acceptance_followup_control_ref centralizes the manual control audit filename.
# 函数用途: 返回 follow-up 人工控制结果路径；显式 apply/rescue 入口会写这里。
def parent_acceptance_followup_control_ref(task: SubAgentTask) -> Path:
    return Path(task.reports_dir) / "parent_acceptance_followup_control.json"


# LLM: preview_parent_acceptance_followup_control makes semi-auto guidance explicit without mutating state.
# 函数用途: 从 follow-up 生成可展示的人工下一步建议；不写文件、不改任务状态。
def preview_parent_acceptance_followup_control(
    task: SubAgentTask,
) -> ParentAcceptanceFollowUpControlResult:
    payload = read_parent_acceptance_followup_payload(task)
    invalid = invalid_followup_error(payload)
    if invalid:
        return invalid_followup_control_result(task, invalid)
    followup = _followup(payload)
    if not followup:
        return _missing_followup_result(task)
    return _preview_result(task, followup)


# LLM: write_parent_acceptance_followup_control_file persists only the explicit control outcome.
# 函数用途: 写入 follow-up 控制审计；记录是否修改状态、证据 refs 和后续推荐命令。
def write_parent_acceptance_followup_control_file(
    task: SubAgentTask,
    result: ParentAcceptanceFollowUpControlResult,
    *,
    generated_at: float | None = None,
) -> Path:
    path = parent_acceptance_followup_control_ref(task)
    payload = {
        "schema": "parent_acceptance_followup_control.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "run_id": task.id,
        "result": result.to_dict(),
        "reserved": {
            "refs_only": True,
            "manual_confirmation_required": True,
            "mutates_task_state": result.mutates_task_state,
            **dict(result.reserved),
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: followup_status returns a normalized status string from a stored follow-up payload.
# 函数用途: 给 manager 和 CLI 提取 follow-up 状态；缺失时返回空字符串。
def followup_status(payload: dict[str, Any]) -> str:
    return str(_followup(payload).get("status") or "")


# LLM: followup_action returns the normalized action string from a stored follow-up payload.
# 函数用途: 给 manager 和 CLI 提取 follow-up 动作；缺失时返回空字符串。
def followup_action(payload: dict[str, Any]) -> str:
    return str(_followup(payload).get("action") or "")


# LLM: invalid_followup_error identifies damaged follow-up audit files without throwing into CLI.
# 函数用途: 判断 follow-up payload 是否是坏 JSON 或读取失败的占位；返回错误摘要给 blocked 结果。
def invalid_followup_error(payload: dict[str, Any]) -> str:
    return str(payload.get(_INVALID_FOLLOWUP_ERROR) or "") if isinstance(payload, dict) else ""


# LLM: followup_command_for_action renders the controlled command for semi-auto prompts.
# 函数用途: 根据 follow-up 动作生成下一步显式命令；命令只是建议，不会被本函数执行。
def followup_command_for_action(run_id: str, action: str) -> str:
    if action == "apply_acceptance":
        return f"subagents-acceptance-plan {run_id} --apply-followup"
    if action == "plan_rescue":
        return f"subagents-acceptance-plan {run_id} --apply-followup --take-over-by <agent>"
    if action == "run_tests":
        return f"subagents-acceptance-plan {run_id} --auto-execution --execute-auto-tests"
    return ""


# LLM: blocked_followup_control_result creates a non-mutating stop with a useful next command.
# 函数用途: 构造被拦截的 follow-up 控制结果；用于缺事实、缺人工确认或危险动作。
def blocked_followup_control_result(
    task: SubAgentTask,
    blocked: BlockedFollowUpControlInput,
) -> ParentAcceptanceFollowUpControlResult:
    return ParentAcceptanceFollowUpControlResult(
        run_id=task.id,
        status=blocked.status or "blocked",
        action=blocked.action or "blocked",
        applied=False,
        ok=False,
        message=blocked.message,
        followup_ref=str(parent_acceptance_followup_ref(task)),
        control_ref=str(parent_acceptance_followup_control_ref(task)),
        recommended_command=blocked.recommended_command,
        mutates_task_state=False,
    )


# LLM: invalid_followup_control_result turns corrupt JSON into a stable blocked result.
# 函数用途: follow-up 文件损坏时阻断 apply/rescue，并提示重新跑显式测试生成新审计。
def invalid_followup_control_result(task: SubAgentTask, error: str) -> ParentAcceptanceFollowUpControlResult:
    return blocked_followup_control_result(
        task,
        BlockedFollowUpControlInput(
            status="invalid_followup",
            action="run_tests",
            message=f"parent acceptance follow-up is invalid; rerun explicit tests first: {error}",
            recommended_command=followup_command_for_action(task.id, "run_tests"),
        ),
    )


# LLM: acceptance_followup_control_result wraps a successful explicit acceptance apply.
# 函数用途: 把显式 follow-up apply 的验收结果转换成控制审计结果；区分已写审计和真正验收通过。
def acceptance_followup_control_result(task: SubAgentTask, apply_result: Any) -> ParentAcceptanceFollowUpControlResult:
    applied = bool(getattr(apply_result, "applied", False))
    accepted = applied and str(getattr(apply_result, "acceptance_decision", "") or "").upper() == "ACCEPT"
    status = "applied_acceptance" if accepted else "acceptance_rejected" if applied else "blocked"
    apply_ref = str(getattr(apply_result, "apply_ref", "") or "")
    return ParentAcceptanceFollowUpControlResult(
        run_id=task.id,
        status=status,
        action="apply_acceptance",
        applied=applied,
        ok=accepted,
        message=str(getattr(apply_result, "message", "") or ""),
        followup_ref=str(parent_acceptance_followup_ref(task)),
        control_ref=str(parent_acceptance_followup_control_ref(task)),
        acceptance_apply_ref=apply_ref,
        evidence_refs=[apply_ref] if apply_ref else [],
        mutates_task_state=applied,
    )


# LLM: rescue_followup_control_result wraps an explicit action-apply rescue record.
# 函数用途: 把 follow-up rescue 接管 action 结果转成控制审计结果；保留 action handler 的安全门结果。
def rescue_followup_control_result(task: SubAgentTask, record: Any) -> ParentAcceptanceFollowUpControlResult:
    applied = bool(getattr(record, "applied", False))
    ok = bool(getattr(record, "ok", False))
    evidence = [str(item) for item in getattr(record, "evidence_paths", []) or [] if item]
    return ParentAcceptanceFollowUpControlResult(
        run_id=task.id,
        status="takeover_recorded" if applied else "blocked",
        action="plan_rescue",
        applied=applied,
        ok=ok,
        message=str(getattr(record, "message", "") or ""),
        followup_ref=str(parent_acceptance_followup_ref(task)),
        control_ref=str(parent_acceptance_followup_control_ref(task)),
        takeover_ref=str(getattr(task, "takeover_file", "") or ""),
        evidence_refs=evidence,
        mutates_task_state=applied,
    )


# LLM: _followup extracts the nested follow-up object without assuming schema version.
# 函数用途: 兼容读取 follow-up 审计 payload；缺失时返回空字典。
def _followup(payload: dict[str, Any]) -> dict[str, Any]:
    followup = payload.get("followup") if isinstance(payload, dict) else {}
    return followup if isinstance(followup, dict) else {}


# LLM: _missing_followup_result gives callers a stable blocked shape when tests have not produced follow-up.
# 函数用途: 缺少 follow-up 文件时生成预览结果；不写文件、不改状态。
def _missing_followup_result(task: SubAgentTask) -> ParentAcceptanceFollowUpControlResult:
    return blocked_followup_control_result(
        task,
        BlockedFollowUpControlInput(
            status="missing_followup",
            action="run_tests",
            message="parent acceptance follow-up is missing; run explicit tests first",
            recommended_command=followup_command_for_action(task.id, "run_tests"),
        ),
    )


# LLM: _preview_result maps stored follow-up into a human/agent readable next-step package.
# 函数用途: 生成半自动调度提示；显示推荐命令，但不执行、不落盘。
def _preview_result(task: SubAgentTask, followup: dict[str, Any]) -> ParentAcceptanceFollowUpControlResult:
    action = str(followup.get("action") or "")
    status = str(followup.get("status") or "")
    command = followup_command_for_action(task.id, action)
    return ParentAcceptanceFollowUpControlResult(
        run_id=task.id,
        status=status,
        action=action,
        applied=False,
        ok=status in {"ready_for_manual_apply", "needs_manual_rescue"},
        message=str(followup.get("reason") or ""),
        followup_ref=str(parent_acceptance_followup_ref(task)),
        control_ref=str(parent_acceptance_followup_control_ref(task)),
        recommended_command=command,
        mutates_task_state=False,
    )
