# LLM: Product acceptance findings verify user deliverables, not run-private handoff files.
# 模块用途: 检查 required_files 是否真的写到业务产物目录，避免内部 runner 报告冒充用户交付物。

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..context_bundle_contracts import (
    product_write_roots,
    required_file_contract,
    required_product_file_refs,
)
from ..reports import AcceptanceReviewFinding

if TYPE_CHECKING:
    from ..models import SubAgentTask


# LLM: required_product_files_finding checks required_files against product roots.
# 函数用途: 防止 runner 把 agent-run final_report.md 当成用户要求的 final_report.md 通过验收。
def required_product_files_finding(task: SubAgentTask, created_at: float) -> AcceptanceReviewFinding:
    required = required_file_contract(task)
    if not required:
        return _no_required_product_files_finding(task, created_at)
    refs = required_product_file_refs(task, required, product_write_roots(task))
    missing = [ref for ref in refs if not Path(ref).exists()]
    return AcceptanceReviewFinding(
        name="required_product_files_exist",
        ok=bool(refs) and not missing,
        severity="P1",
        message=_required_product_message(required, refs, missing),
        evidence_path=refs[0] if refs else getattr(task, "output_json", ""),
        created_at=created_at,
    )


# LLM: _no_required_product_files_finding keeps optional deliverables from blocking acceptance.
# 函数用途: 当前任务没有声明必须交付文件时返回通过 finding，保持旧任务兼容。
def _no_required_product_files_finding(task: SubAgentTask, created_at: float) -> AcceptanceReviewFinding:
    return AcceptanceReviewFinding(
        name="required_product_files_exist",
        ok=True,
        severity="P1",
        message="任务未声明必须交付的文件。",
        evidence_path=getattr(task, "output_json", ""),
        created_at=created_at,
    )


# LLM: _required_product_message explains which product contract path failed.
# 函数用途: 生成业务产物验收提示，让父级知道是缺 product root、缺文件，还是全部存在。
def _required_product_message(required: list[str], refs: list[str], missing: list[str]) -> str:
    if not refs:
        return f"任务要求交付 {', '.join(required[:5])}，但没有 product_write_roots。"
    if missing:
        return f"缺少 {len(missing)} 个业务产物文件: {missing[0]}"
    return f"业务产物文件已在 product_write_roots 下存在: {len(refs)} 个。"
