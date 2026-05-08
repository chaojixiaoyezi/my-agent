# LLM: Parent acceptance apply audit helpers; keep this separate from dry-run decision planning.
# 模块用途: 保存父级验收显式 apply 的结果模型和审计落盘逻辑，避免 controller/manager 继续膨胀。
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .models import SubAgentTask
from .parent_acceptance_controller import ParentAcceptanceDecision


# LLM: ParentAcceptanceApplyResult records the explicit apply boundary without hiding blocked decisions.
# 类用途: 保存父级验收 apply 的结果、审计引用和预留字段；被拦截时也会写入，便于后续自动调度器接着处理。
@dataclass(frozen=True)
class ParentAcceptanceApplyResult:
    """Result for an explicit parent acceptance apply attempt."""

    __test__: ClassVar[bool] = False

    run_id: str
    applied: bool
    parent_decision: str
    message: str
    acceptance_decision: str = ""
    decision_ref: str = ""
    apply_ref: str = ""
    acceptance_review_ref: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps apply-result JSON stable for CLI and future schedulers.
    # 函数用途: 把 apply 结果转换成 JSON 友好字典；不展开引用文件正文。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: write_parent_acceptance_apply_result_file persists the explicit apply audit result.
# 函数用途: 写入父级验收 apply 结果；包含被拦截原因和预留字段，不读取大 artifact 正文。
def write_parent_acceptance_apply_result_file(
    task: SubAgentTask,
    result: ParentAcceptanceApplyResult,
    *,
    generated_at: float | None = None,
) -> Path:
    path = Path(task.reports_dir) / "parent_acceptance_apply.json"
    reserved = {
        "auto_execute_tests": False,
        "auto_rescue": False,
        "requires_inspect_only": True,
        "mutates_task_state": bool(result.applied),
        "refs_only": True,
        **dict(result.reserved),
    }
    payload = {
        "schema": "parent_acceptance_apply.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "run_id": task.id,
        "applied": result.applied,
        "parent_decision": result.parent_decision,
        "message": result.message,
        "result": result.to_dict(),
        "reserved": reserved,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: blocked_parent_acceptance_apply_result records a non-mutating stop for unsafe or incomplete decisions.
# 函数用途: 构造被拦截的 apply 结果；供 manager/CLI 留下审计而不改变任务状态。
def blocked_parent_acceptance_apply_result(
    task: SubAgentTask,
    decision: ParentAcceptanceDecision,
    *,
    decision_ref: str,
) -> ParentAcceptanceApplyResult:
    return ParentAcceptanceApplyResult(
        run_id=task.id,
        applied=False,
        parent_decision=decision.decision,
        message=f"parent decision {decision.decision} requires explicit next action; apply is limited to inspect_only",
        decision_ref=decision_ref,
        apply_ref=str(Path(task.reports_dir) / "parent_acceptance_apply.json"),
        reserved={
            "auto_execute_tests": False,
            "auto_rescue": False,
            "mutates_task_state": False,
        },
    )


# LLM: applied_parent_acceptance_apply_result wraps the normal acceptance record as parent-controller audit.
# 函数用途: 把 inspect_only 进入普通验收后的结果转成父级 apply 审计记录。
def applied_parent_acceptance_apply_result(
    task: SubAgentTask,
    decision: ParentAcceptanceDecision,
    record: Any,
    *,
    decision_ref: str,
) -> ParentAcceptanceApplyResult:
    return ParentAcceptanceApplyResult(
        run_id=task.id,
        applied=bool(getattr(record, "applied", False)),
        parent_decision=decision.decision,
        acceptance_decision=str(getattr(record, "decision", "") or ""),
        message=str(getattr(record, "message", "") or ""),
        decision_ref=decision_ref,
        apply_ref=str(Path(task.reports_dir) / "parent_acceptance_apply.json"),
        acceptance_review_ref=str(Path(task.reports_dir) / "acceptance_review.json"),
        reserved={
            "auto_execute_tests": False,
            "auto_rescue": False,
            "mutates_task_state": bool(getattr(record, "applied", False)),
        },
    )
