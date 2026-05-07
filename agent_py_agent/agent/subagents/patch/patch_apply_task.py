# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""single-task patch apply workflow for PatchApplyService.

给人看的解释：
这里处理单个任务里的 patch 规范化、边界检查、执行和回滚，PatchApplyService 只保留批量门面。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.subagents.patch.patch_renderer import build_unified_diff
from agent_py_agent.agent.subagents.reports import PatchApplyRecord
from agent_py_agent.agent.subagents.utils import _new_id
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

_PATCH_APPLY_WRITE_TYPES = {"write_file"}


# LLM: _ExecuteApplyContext 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 集中保存execute应用上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class _ExecuteApplyContext:
    """Bundle for _execute_apply to reduce parameter count."""
    manager: Any
    task: Any
    patch_specs: list
    patches: list
    applier: str
    note: str
    test_commands: list
    decision: str
    ok: bool
    message: str


# LLM: ApplyPatchTaskParams 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 集中保存应用补丁任务参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class ApplyPatchTaskParams:
    """Bundle for apply_patch_task keyword-only parameters."""
    # LLM: 单任务补丁应用状态以同一参数包穿过服务和兜底路径。
    output: dict
    patches: list
    apply: bool
    applier: str
    note: str


# LLM: BaseAuditParams 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 集中保存基础audit参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class BaseAuditParams:

    patch: dict
    raw_path: str
    status: str
    patch_type: str
    diff_text: str


# LLM: extract_patch_test_info 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理extract补丁testinfo相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def extract_patch_test_info(task, output):
    """Extract test commands and blocked test reasons from task output."""
    from agent_py_agent.agent.subagents.services.patch_apply_test_commands import (
        PatchApplyTestCommands,
    )

    test_commands, blocked_test_reasons = PatchApplyTestCommands.extract(task, output)
    patch_entries = [
        {
            "path": "",
            "status": "test_command",
            "apply_status": "BLOCKED",
            "message": reason,
        }
        for reason in blocked_test_reasons
    ]
    return test_commands, len(blocked_test_reasons), patch_entries


# LLM: apply_patch_task 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 更新补丁任务对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def apply_patch_task(manager, task, *, params: ApplyPatchTaskParams) -> PatchApplyRecord:
    """Execute single task patch apply dry-run or real apply."""
    now = time.time()
    patch_entries, patch_specs, blocked_count = _normalize_all_patches(manager, task, params.patches)
    test_commands, test_blocked_count, test_entries = extract_patch_test_info(task, params.output)
    blocked_count += test_blocked_count
    patch_entries.extend(test_entries)

    from agent_py_agent.agent.subagents.services.patch_apply_decision import PatchApplyDecision

    decision, ok, message = PatchApplyDecision.decide(params.patches, patch_specs, blocked_count, params.apply)
    rollback_performed, test_results, applied_count = False, [], 0

    if params.apply and ok and patch_specs:
        review_status_updates = [dict(item) for item in params.patches]
        applied_count, rollback_performed, test_results, decision, ok, message = _execute_apply(
            _ExecuteApplyContext(
                manager=manager,
                task=task,
                patch_specs=patch_specs,
                patches=review_status_updates,
                applier=params.applier,
                note=params.note,
                test_commands=test_commands,
                decision=decision,
                ok=ok,
                message=message,
            )
        )
        params.output["patches"] = [spec["patch_ref"] for spec in patch_specs]
        Path(task.output_json).write_text(
            json.dumps(params.output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    evidence_paths = [task.output_json, task.work_log_file]
    if params.apply:
        evidence_paths.append(str(manager.workspace / "subagent_patch_apply_log.jsonl"))

    return PatchApplyRecord(
        id=_new_id("patchapply"), run_id=task.id, dry_run=not params.apply, applied=params.apply and ok,
        ok=ok, decision=decision, message=message, patch_count=len(params.patches),
        applied_count=applied_count, blocked_count=blocked_count, rollback_performed=rollback_performed,
        applier=params.applier, note=params.note, evidence_paths=evidence_paths, test_commands=test_commands,
        test_results=test_results, patches=patch_entries, created_at=now,
    )


# LLM: _normalize_all_patches 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 解析并归一化allpatches的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_all_patches(manager, task, patches):
    """Normalize all patches and return entries, specs, and blocked count."""
    patch_entries, patch_specs, blocked_count = [], [], 0
    for item in [dict(p) for p in patches]:
        spec = normalize_patch_apply_spec(manager, task, item)
        patch_entries.append(spec["audit"])
        if spec["ok"]:
            patch_specs.append(spec)
        else:
            blocked_count += 1
    return patch_entries, patch_specs, blocked_count


# LLM: _execute_apply 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 推进execute应用的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响补丁文件、预演结果和应用报告，需保持重试、超时和状态迁移语义。
def _execute_apply(ctx: _ExecuteApplyContext):
    """Execute the patch apply and handle rollback on failure."""
    applied_count, rollback_performed, test_results = 0, False, []
    try:
        from agent_py_agent.agent.subagents.services.patch_apply_executor import (
            PatchApplyExecutor,
            PatchApplyParams,
        )
        applied_count, _, rollback_performed, test_results = PatchApplyExecutor.execute(
            PatchApplyParams(
                patch_specs=ctx.patch_specs,
                review_status_updates=ctx.patches,
                task=ctx.task,
                manager=ctx.manager,
                applier=ctx.applier,
                note=ctx.note,
                test_commands=ctx.test_commands,
            ),
        )
    except Exception as exc:
        rollback_performed = True
        for spec in ctx.patch_specs:
            spec["audit"]["apply_status"] = "ROLLED_BACK"
            spec["audit"]["message"] = f"apply 失败: {exc}"
            spec["patch_ref"]["apply_status"] = spec["audit"]["apply_status"]
        ctx.decision, ctx.ok, ctx.message = "ROLLBACK", False, f"patch apply 失败，已回滚: {exc}"
        ctx.manager._append_task_work_log(ctx.task, f"patch_apply: rollback applier={ctx.applier} error={exc}")
    return applied_count, rollback_performed, test_results, ctx.decision, ctx.ok, ctx.message


# LLM: normalize_patch_apply_spec 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 解析并归一化补丁应用spec的输入形态，让下游只处理稳定结构；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def normalize_patch_apply_spec(manager, task, patch: dict) -> dict:
    """Normalize patch spec with write boundary enforcement."""
    raw_path = str(patch.get("path") or "").strip()
    status = str(patch.get("status") or "").strip().lower()
    patch_type = _patch_type(patch)
    content = _patch_content(patch)
    diff_text = _patch_diff_text(patch)
    audit = _base_audit(BaseAuditParams(patch, raw_path, status, patch_type, diff_text))

    blocked = _preflight_patch_blocker(raw_path, status, patch_type, content)
    if blocked:
        audit["apply_status"] = "BLOCKED"
        audit["message"] = blocked
        return {"ok": False, "audit": audit, "patch_ref": patch}

    boundary_error = validate_write_boundary(
        "write_file",
        {"path": raw_path},
        workspace_root=manager.workspace_root,
        write_boundary={
            "allowed_write_roots": task.allowed_write_roots,
            "forbidden_write_roots": task.forbidden_write_roots,
            "locked_files": task.locked_files,
        },
    )
    if boundary_error:
        audit["apply_status"] = "BLOCKED"
        audit["message"] = boundary_error
        return {"ok": False, "audit": audit, "patch_ref": patch}

    target = resolve_patch_target(manager, raw_path)
    before_text = target.read_text(encoding="utf-8") if target.exists() else ""
    audit["diff_preview"] = diff_text or build_unified_diff(raw_path, before_text, content)
    audit["message"] = "patch 可以进入 apply。"
    return {"ok": True, "audit": audit, "patch_ref": patch, "target": target, "content": content, "path": raw_path}


# LLM: _patch_type 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁type相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def _patch_type(patch: dict) -> str:
    return str(
        patch.get("tool")
        or patch.get("type")
        or ("write_file" if any(key in patch for key in ("content", "new_content", "file_content", "after")) else "")
    ).strip().lower()


# LLM: _patch_content 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁内容相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def _patch_content(patch: dict):
    content = patch.get("content")
    if content is not None:
        return content
    for key in ("new_content", "file_content", "desired_content", "after"):
        if patch.get(key) is not None:
            return patch.get(key)
    return None


# LLM: _patch_diff_text 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁diff文本相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def _patch_diff_text(patch: dict) -> str:
    for key in ("diff", "patch", "patch_diff", "unified_diff"):
        value = patch.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# LLM: _base_audit 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理基础audit相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def _base_audit(params: BaseAuditParams) -> dict:
    return {
        "path": params.raw_path,
        "status": params.status or "unknown",
        "review_status": str(params.patch.get("review_status") or "UNREVIEWED"),
        "summary": str(params.patch.get("summary") or ""),
        "patch_type": params.patch_type or "unknown",
        "apply_status": "PENDING",
        "diff_preview": params.diff_text,
        "actual_diff": "",
        "message": "",
    }


# LLM: _preflight_patch_blocker 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理preflight补丁blocker相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def _preflight_patch_blocker(raw_path: str, status: str, patch_type: str, content) -> str:
    if not raw_path:
        return "patch 缺少 path。"
    if status not in {"planned", "applied"}:
        return f"patch status={status or 'unknown'} 不能进入 apply。"
    if patch_type and patch_type not in _PATCH_APPLY_WRITE_TYPES:
        return f"只支持 write_file patch，当前类型是 {patch_type}。"
    if not isinstance(content, str):
        return "write_file patch 缺少完整 content，不能安全 apply。"
    return ""


# LLM: resolve_patch_target 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 读取或查询补丁target需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def resolve_patch_target(manager, raw_path: str) -> Path:
    """Resolve patch target path relative to workspace root."""
    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        target = manager.workspace_root / target
    return target.resolve(strict=False)
