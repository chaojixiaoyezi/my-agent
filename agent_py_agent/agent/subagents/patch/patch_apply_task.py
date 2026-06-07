
from __future__ import annotations

"""single-task patch apply workflow for PatchApplyService.

这里处理单个任务里的 patch 规范化、边界检查、执行和回滚，PatchApplyService 只保留批量门面。
审计字段由 patch_apply_audit 统一构造，避免执行流程和报告证据互相分叉。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.subagents.patch.patch_apply_audit import (
    PatchApplyRecordPayload,
    build_patch_apply_record,
)
from agent_py_agent.agent.subagents.patch.patch_apply_helpers import (
    run_patch_apply_tests,
    validate_patch_test_command,
)
from agent_py_agent.agent.subagents.patch.patch_file_ops import (
    PatchFileApplyContext,
    do_apply_patches,
    rollback_patch_apply,
)
from agent_py_agent.agent.subagents.patch.patch_renderer import build_unified_diff
from agent_py_agent.agent.subagents.parsing import _dict_list
from agent_py_agent.agent.subagents.reports import PatchApplyRecord
from agent_py_agent.agent.subagents.utils import _new_id
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

_PATCH_APPLY_WRITE_TYPES = {"write_file"}


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


@dataclass
class ApplyPatchTaskParams:
    """Bundle for apply_patch_task keyword-only parameters."""
    output: dict
    patches: list
    apply: bool
    applier: str
    note: str


@dataclass
class PatchApplyParams:
    """Bundle for PatchApplyExecutor.execute parameters."""

    patch_specs: list
    review_status_updates: list
    task: Any
    manager: Any
    applier: str
    note: str
    test_commands: list


class PatchApplyExecutor:
    """Execute patch apply with rollback support."""

    @staticmethod
    def execute(params: PatchApplyParams):
        touched_files = {}
        applied_count = 0
        rollback_performed = False
        test_results = []

        try:
            applied_count, touched_files = do_apply_patches(
                PatchFileApplyContext(params.patch_specs, params.task, params.applier, params.note)
            )
            test_results = _run_patch_apply_tests(params)
            _write_patch_apply_success(params, applied_count, test_results)
        except Exception as exc:
            rollback_performed = bool(touched_files)
            rollback_patch_apply(touched_files)
            for spec in params.patch_specs:
                spec["audit"]["apply_status"] = "ROLLED_BACK" if rollback_performed else "FAILED"
                spec["audit"]["message"] = f"apply 失败: {exc}"
                spec["patch_ref"]["apply_status"] = spec["audit"]["apply_status"]
            raise RuntimeError(str(exc)) from exc

        return applied_count, touched_files, rollback_performed, test_results


@dataclass
class _PreparedPatchApply:
    patch_entries: list
    patch_specs: list
    blocked_count: int
    test_commands: list


@dataclass
class _ApplyIfReadyContext:
    manager: Any
    task: Any
    params: ApplyPatchTaskParams
    prepared: _PreparedPatchApply
    decision: str
    ok: bool
    message: str


@dataclass(frozen=True)
class BaseAuditParams:

    patch: dict
    raw_path: str
    status: str
    patch_type: str
    diff_text: str


def extract_patch_test_info(task, output):
    commands, blocked_reasons = _extract_patch_test_commands(task, output)
    entries = [
        {
            "path": "",
            "status": "test_command",
            "apply_status": "BLOCKED",
            "message": reason,
        }
        for reason in blocked_reasons
    ]
    return commands, len(blocked_reasons), entries


def apply_patch_task(manager, task, *, params: ApplyPatchTaskParams) -> PatchApplyRecord:
    """Execute single task patch apply dry-run or real apply."""
    now = time.time()
    prepared = _prepare_patch_apply(manager, task, params)
    decision, ok, message = _decide_patch_apply(
        params.patches, prepared.patch_specs, prepared.blocked_count, params.apply
    )
    applied_count, rollback_performed, test_results, decision, ok, message = _execute_apply_if_ready(
        _ApplyIfReadyContext(manager, task, params, prepared, decision, ok, message)
    )

    evidence_paths = [task.output_json, task.work_log_file]
    if params.apply:
        evidence_paths.append(str(manager.workspace / "subagent_patch_apply_log.jsonl"))

    return build_patch_apply_record(
        PatchApplyRecordPayload(
            manager=manager,
            task=task,
            params=params,
            now=now,
            ok=ok,
            decision=decision,
            message=message,
            patch_specs=prepared.patch_specs,
            blocked_count=prepared.blocked_count,
            rollback_performed=rollback_performed,
            test_commands=prepared.test_commands,
            test_results=test_results,
            patch_entries=prepared.patch_entries,
            evidence_paths=evidence_paths,
            applied_count=applied_count,
        )
    )


def _decide_patch_apply(patches, patch_specs, blocked_count, apply):
    if not patches:
        return ("NO_PATCHES", False, "没有 patch 可以 apply。")
    if blocked_count:
        return ("REJECT", False, f"{blocked_count} 项 patch/test 不满足 apply 条件。")
    if not apply:
        return ("WOULD_APPLY", True, f"dry-run: 将 apply {len(patch_specs)} 个 patch。")
    return ("APPLIED", True, f"已 apply {len(patch_specs)} 个 patch。")


def _prepare_patch_apply(manager, task, params: ApplyPatchTaskParams) -> _PreparedPatchApply:
    patch_entries, patch_specs, blocked_count = _normalize_all_patches(manager, task, params.patches)
    test_commands, test_blocked_count, test_entries = extract_patch_test_info(task, params.output)
    return _PreparedPatchApply(
        patch_entries=[*patch_entries, *test_entries],
        patch_specs=patch_specs,
        blocked_count=blocked_count + test_blocked_count,
        test_commands=test_commands,
    )


def _execute_apply_if_ready(ctx: _ApplyIfReadyContext):
    params = ctx.params
    prepared = ctx.prepared
    if not (params.apply and ctx.ok and prepared.patch_specs):
        return 0, False, [], ctx.decision, ctx.ok, ctx.message
    applied_count, rollback_performed, test_results, decision, ok, message = _execute_apply(
        _ExecuteApplyContext(
            manager=ctx.manager,
            task=ctx.task,
            patch_specs=prepared.patch_specs,
            patches=[dict(item) for item in params.patches],
            applier=params.applier,
            note=params.note,
            test_commands=prepared.test_commands,
            decision=ctx.decision,
            ok=ctx.ok,
            message=ctx.message,
        )
    )
    _write_successful_apply_output(ctx.task, params, prepared)
    return applied_count, rollback_performed, test_results, decision, ok, message


def _write_successful_apply_output(task, params: ApplyPatchTaskParams, prepared) -> None:
    _carry_existing_load_errors(params.output, task.output_json)
    params.output["patches"] = [spec["patch_ref"] for spec in prepared.patch_specs]
    Path(task.output_json).write_text(
        json.dumps(params.output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _carry_existing_load_errors(output: dict, output_json: str) -> None:
    read_report = read_json_object_report(
        Path(output_json),
        parse_nested_string=True,
        context="patch_apply.outer_success_output_json",
    )
    errors = read_report.payload.get("load_errors") if isinstance(read_report.payload, dict) else None
    items = list(errors) if isinstance(errors, list) else []
    if read_report.load_error is not None:
        items.append(read_report.load_error)
    if items:
        output["load_errors"] = items


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


def _execute_apply(ctx: _ExecuteApplyContext):
    """Execute the patch apply and handle rollback on failure."""
    applied_count, rollback_performed, test_results = 0, False, []
    try:
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
        ctx.manager.actions._append_task_work_log(ctx.task, f"patch_apply: rollback applier={ctx.applier} error={exc}")
    return applied_count, rollback_performed, test_results, ctx.decision, ctx.ok, ctx.message


def _extract_patch_test_commands(task, output: dict) -> tuple[list[str], list[str]]:
    commands: list[str] = []
    blocked: list[str] = []
    for command in _task_patch_test_commands(task):
        _append_validated_test_command(command, commands, blocked)
    for test in _dict_list(output.get("tests", [])):
        command = str(test.get("command") or "").strip()
        _append_validated_test_command(command, commands, blocked)
    return commands, blocked


def _append_validated_test_command(command: str, commands: list[str], blocked: list[str]) -> None:
    if not command:
        return
    problem = validate_patch_test_command(command)
    if problem:
        blocked.append(problem)
    elif command not in commands:
        commands.append(command)


def _task_patch_test_commands(task) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    commands: list[str] = []
    for field_name in ("patch_test_commands", "test_commands"):
        commands.extend(_string_items(attrs.get(field_name)))
    return commands


def _string_items(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [text for item in value if (text := str(item or "").strip())]
    return []


def _run_patch_apply_tests(params: PatchApplyParams) -> list:
    if not params.test_commands:
        return []
    test_results = run_patch_apply_tests(params.test_commands, params.manager.workspace_root)
    failed = [item for item in test_results if not item.get("ok")]
    if failed:
        raise RuntimeError(f"{len(failed)} 个 apply 后测试失败。")
    return test_results


def _write_patch_apply_success(params: PatchApplyParams, applied_count: int, test_results: list) -> None:
    read_report = read_json_object_report(
        Path(params.task.output_json),
        parse_nested_string=True,
        context="patch_apply.success_output_json",
    )
    output = dict(read_report.payload)
    if read_report.load_error is not None:
        _append_load_error(output, read_report.load_error)
    output["patches"] = params.review_status_updates
    Path(params.task.output_json).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    params.manager.actions._append_task_work_log(
        params.task,
        f"patch_apply: applied={applied_count} tests={len(test_results)} applier={params.applier}",
    )


def _append_load_error(output: dict[str, object], load_error: dict[str, object]) -> None:
    existing = output.get("load_errors")
    items = list(existing) if isinstance(existing, list) else []
    items.append(load_error)
    output["load_errors"] = items


def normalize_patch_apply_spec(manager, task, patch: dict) -> dict:
    """Normalize patch spec with write boundary enforcement."""
    raw_path = str(patch.get("path") or "").strip()
    status = str(patch.get("status") or "").strip()
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


def _patch_type(patch: dict) -> str:
    return str(
        patch.get("tool")
        or patch.get("type")
        or ("write_file" if any(key in patch for key in ("content", "new_content", "file_content", "after")) else "")
    ).strip().lower()


def _patch_content(patch: dict):
    content = patch.get("content")
    if content is not None:
        return content
    for key in ("new_content", "file_content", "desired_content", "after"):
        if patch.get(key) is not None:
            return patch.get(key)
    return None


def _patch_diff_text(patch: dict) -> str:
    for key in ("diff", "patch", "patch_diff", "unified_diff"):
        value = patch.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


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


def resolve_patch_target(manager, raw_path: str) -> Path:
    """Resolve patch target path relative to workspace root."""
    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        target = manager.workspace_root / target
    return target.resolve(strict=False)
