# LLM: 产物类型/数量对账门(REFACTORING_BACKLOG"产物类型/数量对账门",实锤
#   R6b/R6c:prompt 要求 24 周/每篇一个 PDF,实交 1 个 md/0 个 PDF,closeout 只查
#   "有没有产物"照样 ok=true)。契约:①纯声明驱动——只对账 task_progress 账本里
#   模型自己声明的 expected_outputs(pattern/min_count),零声明零影响,绝不解析
#   自然语言;②开放世界——pattern 是 glob,扩展名天然携带类型(声明 *.pdf 而只交
#   .md → 命中数 0 → 缺失),不写任何格式专项分支;③不匹配走 repair(non-terminal
#   返工),不硬卡死;④文件存在性是客观事实,glob 只读文件系统。挂 closeout 两条
#   路径(gates.attach_closeout_gates + uncontracted decisions)。改动时同步检查
#   task_progress.normalize_expected_outputs、tests/test_expected_outputs_gate.py。
# 模块用途: 模型声明了"这个任务最终该交几个什么文件"之后,验收时数一数交付区
#   是不是真有——声明 24 个只交 1 个、声明 PDF 只交 md,都会被打回去补。
from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery import RecoveryAction
from ...task_progress import progress_path, read_task_progress
from .task_progress_gate import _progress_root, _run_id

_GATE_NAME = "expected_outputs_reconciliation"


# LLM: 对账唯一入口。流程:读当前 run 的 progress 账本 → 无 expected_outputs 声明
#   → allow(unchecked,声明驱动零影响);有声明 → 逐条 glob 任务交付目录
#   (相对 pattern)或绝对路径,实存文件数 < min_count 即缺口 → repair finding
#   EXPECTED_OUTPUTS_MISSING(列 pattern/expected/actual,非终态)。无交付目录
#   可解析时按 unchecked 放行(没有对账基准,不瞎拦)。
# 函数用途: closeout 时核对"声明要交的文件"是否真的在交付区里、数量够不够。
def evaluate_expected_outputs_gate(closeout: object) -> GateDecision:
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return _unchecked_decision("progress_scope_missing")
    progress = read_task_progress(root, run_id)
    declared = [entry for entry in progress.get("expected_outputs") or [] if isinstance(entry, dict)]
    if not declared:
        return _unchecked_decision("no_expected_outputs_declared", run_id=run_id)
    output_dir = _task_output_dir(closeout)
    results = [_reconcile_entry(entry, output_dir) for entry in declared]
    missing = [item for item in results if item["satisfied"] is not True]
    evidence = {
        "checked": True,
        "run_id": run_id,
        "progress_ref": str(progress_path(root, run_id)),
        "output_dir": str(output_dir or ""),
        "declared_count": len(results),
        "missing_count": len(missing),
        "reconciliation": results,
    }
    if not missing:
        return GateDecision.allow(_GATE_NAME, evidence=evidence)
    return GateDecision.repair(
        _GATE_NAME,
        (_missing_finding(missing, output_dir),),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            **evidence,
            "required_actions": [
                "produce_missing_declared_outputs_into_task_output",
                "update_expected_outputs_declaration_if_requirement_changed",
                "submit_for_acceptance_after_declared_outputs_exist",
            ],
        },
    )


# 函数用途: 单条声明的对账:数 glob 命中的实存文件,够不够 min_count。
def _reconcile_entry(entry: dict[str, Any], output_dir: Path | None) -> dict[str, Any]:
    pattern = str(entry.get("pattern") or "")
    min_count = max(1, int(entry.get("min_count") or 1))
    matched = _matched_files(pattern, output_dir)
    result: dict[str, Any] = {
        "pattern": pattern,
        "min_count": min_count,
        "actual_count": len(matched),
        "satisfied": len(matched) >= min_count,
        "matched_files": [str(path) for path in matched[:12]],
    }
    note = str(entry.get("note") or "").strip()
    if note:
        result["note"] = note
    if output_dir is None and not Path(pattern).is_absolute():
        result["unresolved_reason"] = "task_output_dir_missing"
    return result


# LLM: glob 解析:绝对 pattern 直接交标准库 glob(用户 prompt 指定绝对交付路径的
#   形态);相对 pattern 锚定任务交付目录。只统计真实文件(目录不算交付物);
#   交付目录缺失时相对 pattern 记 0 命中(unresolved_reason 标注)。
# 函数用途: 把一条声明 pattern 变成"现在交付区里真实存在的文件列表"。
def _matched_files(pattern: str, output_dir: Path | None) -> list[Path]:
    if not pattern:
        return []
    candidate = Path(pattern).expanduser()
    if candidate.is_absolute():
        try:
            return sorted(path for raw in glob.glob(str(candidate)) if (path := Path(raw)).is_file())
        except (OSError, ValueError):
            return []
    if output_dir is None:
        return []
    try:
        return sorted(path for path in output_dir.glob(pattern) if path.is_file())
    except (OSError, ValueError):
        return []


# 函数用途: 从 run_workspace 属性解析任务交付目录(与 uncontracted 同一权威字段)。
def _task_output_dir(closeout: object) -> Path | None:
    params = getattr(closeout, "params", None)
    attrs = getattr(params, "task_attributes", None)
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("output_dir") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


# 函数用途: 缺口 finding:列出每条没满足的声明(要几个/有几个),给修复方向。
def _missing_finding(missing: list[dict[str, Any]], output_dir: Path | None) -> GateFinding:
    parts = [
        f"{item['pattern']}（要求≥{item['min_count']}，实有 {item['actual_count']}）"
        for item in missing[:6]
    ]
    return GateFinding(
        "EXPECTED_OUTPUTS_MISSING",
        "medium",
        message=(
            f"进度账本声明的交付产物有 {len(missing)} 项在交付区不满足：{'；'.join(parts)}。"
            "请补齐缺失产物后重新提交；若交付要求确实已变化，先更新 expected_outputs 声明再提交。"
        ),
        evidence={
            "missing": missing[:12],
            "output_dir": str(output_dir or ""),
        },
    )


# 函数用途: 声明为空或无对账基准时的放行决定(带原因,可审计)。
def _unchecked_decision(reason: str, run_id: str = "") -> GateDecision:
    evidence: dict[str, object] = {"checked": False, "reason": reason}
    if run_id:
        evidence["run_id"] = run_id
    return GateDecision.allow(_GATE_NAME, evidence=evidence)


__all__ = ["evaluate_expected_outputs_gate"]
