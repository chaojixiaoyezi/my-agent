from __future__ import annotations

"""LLM: single-task patch apply workflow for PatchApplyService.

给人看的解释：
这里处理单个任务里的 patch 规范化、边界检查、执行和回滚，PatchApplyService 只保留批量门面。
"""

import time
from pathlib import Path

from agent_py_agent.agent.subagents.patch.patch_renderer import build_unified_diff
from agent_py_agent.agent.subagents.reports import PatchApplyRecord
from agent_py_agent.agent.subagents.utils import _new_id
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

_PATCH_APPLY_WRITE_TYPES = {"write_file"}


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


def apply_patch_task(manager, task, *, output, patches, apply, applier, note) -> PatchApplyRecord:
    """Execute single task patch apply dry-run or real apply."""
    now = time.time()
    patch_entries = []
    blocked_count = 0
    patch_specs = []
    review_status_updates = [dict(item) for item in patches]

    for item in review_status_updates:
        spec = normalize_patch_apply_spec(manager, task, item)
        patch_entries.append(spec["audit"])
        if spec["ok"]:
            patch_specs.append(spec)
        else:
            blocked_count += 1

    test_commands, test_blocked_count, test_entries = extract_patch_test_info(task, output)
    blocked_count += test_blocked_count
    patch_entries.extend(test_entries)

    from agent_py_agent.agent.subagents.services.patch_apply_decision import PatchApplyDecision

    decision, ok, message = PatchApplyDecision.decide(patches, patch_specs, blocked_count, apply)
    rollback_performed = False
    test_results = []
    applied_count = 0

    if apply and ok and patch_specs:
        try:
            from agent_py_agent.agent.subagents.services.patch_apply_executor import (
                PatchApplyExecutor,
            )

            applied_count, _touched_files, rollback_performed, test_results = PatchApplyExecutor.execute(
                patch_specs,
                review_status_updates,
                task,
                manager,
                applier,
                note,
                test_commands,
            )
        except Exception as exc:
            rollback_performed = True
            for spec in patch_specs:
                spec["audit"]["apply_status"] = "ROLLED_BACK"
                spec["audit"]["message"] = f"apply 失败: {exc}"
                spec["patch_ref"]["apply_status"] = spec["audit"]["apply_status"]
            decision = "ROLLBACK"
            ok = False
            message = f"patch apply 失败，已回滚: {exc}"
            manager._append_task_work_log(task, f"patch_apply: rollback applier={applier} error={exc}")

    evidence_paths = [task.output_json, task.work_log_file]
    if apply:
        evidence_paths.append(str(manager.workspace / "subagent_patch_apply_log.jsonl"))

    return PatchApplyRecord(
        id=_new_id("patchapply"),
        run_id=task.id,
        dry_run=not apply,
        applied=apply and ok,
        ok=ok,
        decision=decision,
        message=message,
        patch_count=len(patches),
        applied_count=applied_count,
        blocked_count=blocked_count,
        rollback_performed=rollback_performed,
        applier=applier,
        note=note,
        evidence_paths=evidence_paths,
        test_commands=test_commands,
        test_results=test_results,
        patches=patch_entries,
        created_at=now,
    )


def normalize_patch_apply_spec(manager, task, patch: dict) -> dict:
    """Normalize patch spec with write boundary enforcement."""
    raw_path = str(patch.get("path") or "").strip()
    status = str(patch.get("status") or "").strip().lower()
    patch_type = _patch_type(patch)
    content = _patch_content(patch)
    diff_text = _patch_diff_text(patch)
    audit = _base_audit(patch, raw_path, status, patch_type, diff_text)

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


def _base_audit(patch: dict, raw_path: str, status: str, patch_type: str, diff_text: str) -> dict:
    return {
        "path": raw_path,
        "status": status or "unknown",
        "review_status": str(patch.get("review_status") or "UNREVIEWED"),
        "summary": str(patch.get("summary") or ""),
        "patch_type": patch_type or "unknown",
        "apply_status": "PENDING",
        "diff_preview": diff_text,
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
