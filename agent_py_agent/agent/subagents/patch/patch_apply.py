"""Patch application with file write boundary enforcement.

Human version:
这个模块处理 patch 的实际文件写入，是安全敏感的边界检查核心。
所有文件操作必须通过 validate_write_boundary 校验后才执行。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agent_py_agent.agent.subagents.patch.patch_renderer import build_unified_diff
from agent_py_agent.agent.subagents.reports import PatchApplyRecord, PatchApplyReport
from agent_py_agent.agent.subagents.utils import _new_id, _read_json_object
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

from .patch_apply_helpers import (
    extract_patch_test_command,
    validate_patch_test_command,
)
from .patch_apply_reports import patch_apply_record_to_dict

if TYPE_CHECKING:
    from ..models import SubAgentTask


_PATCH_APPLY_WRITE_TYPES = {"write_file"}


class PatchApplyService:
    """Execute patch apply with write boundary enforcement and rollback support."""

    def __init__(self, manager):
        self.manager = manager

    def apply_patches(
        self,
        run_ids=None,
        *,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ) -> PatchApplyReport:
        """Execute the independent patch-apply audit chain for runner-declared file writes."""

        from agent_py_agent.agent.subagents.parsing import _dict_list

        selected = self.manager._select_runs(run_ids)
        records = []
        for task in selected:
            output = _read_json_object(Path(task.output_json))
            patches = _dict_list(output.get("patches", []))
            if run_ids is None and not patches:
                continue
            records.append(
                self._apply_patch_task(
                    task,
                    output=output,
                    patches=patches,
                    apply=apply,
                    applier=applier,
                    note=note,
                )
            )
            if limit > 0 and len(records) >= limit:
                break

        from agent_py_agent.agent.subagents.services.patch_apply_summary import PatchApplySummary

        summary = PatchApplySummary.build(records)
        return PatchApplyReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_apply_report(
        self,
        run_ids=None,
        *,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ) -> PatchApplyReport:
        """Write patch apply report to disk."""

        from agent_py_agent.agent.file_io import append_jsonl
        from agent_py_agent.agent.subagents.patch.patch_renderer import render_patch_apply_markdown

        report = self.apply_patches(
            run_ids,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )
        (self.manager.workspace / "subagent_patch_apply_report.json").write_text(
            json.dumps(
                {
                    "generated_at": report.generated_at,
                    "dry_run": report.dry_run,
                    "summary": report.summary,
                    "records": [patch_apply_record_to_dict(r) for r in report.records],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_PATCH_APPLY.md").write_text(
            render_patch_apply_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            from agent_py_agent.agent.subagents.services.patch_apply_record_files import (
                PatchApplyRecordFiles,
            )

            PatchApplyRecordFiles.write_record(record, self.manager)
            self.manager._index_dataclass_record(
                "subagent_patch_apply",
                record.id,
                f"Patch apply {record.run_id} {record.decision}",
                record,
                "subagent_patch_apply_logged",
            )
            if apply:
                PatchApplyRecordFiles.append_log(record, self.manager)
        self.manager._index_report(
            "subagent_patch_apply_report",
            "latest",
            "Subagent patch apply report",
            report,
            event_type="subagent_patch_apply_report_written",
        )
        return report

    def _apply_patch_task(
        self,
        task: SubAgentTask,
        *,
        output: dict,
        patches: list[dict],
        apply: bool,
        applier: str,
        note: str,
    ) -> PatchApplyRecord:
        """Execute single task patch apply dry-run or real apply."""

        now = time.time()
        patch_entries = []
        blocked_count = 0
        patch_specs = []
        review_status_updates = [dict(item) for item in patches]

        for index, item in enumerate(review_status_updates):
            spec = self._normalize_patch_apply_spec(task, item)
            patch_entries.append(spec["audit"])
            if spec["ok"]:
                patch_specs.append(spec)
            else:
                blocked_count += 1

        from agent_py_agent.agent.subagents.services.patch_apply_test_commands import (
            PatchApplyTestCommands,
        )

        test_commands, blocked_test_reasons = PatchApplyTestCommands.extract(task, output)
        if blocked_test_reasons:
            blocked_count += len(blocked_test_reasons)
            patch_entries.extend(
                {
                    "path": "",
                    "status": "test_command",
                    "apply_status": "BLOCKED",
                    "message": reason,
                }
                for reason in blocked_test_reasons
            )

        patch_count = len(patches)
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

                applied_count, touched_files, rollback_performed, test_results = PatchApplyExecutor.execute(
                    patch_specs,
                    review_status_updates,
                    task,
                    self.manager,
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
                self.manager._append_task_work_log(
                    task,
                    f"patch_apply: rollback applier={applier} error={exc}",
                )

        evidence_paths = [task.output_json, task.work_log_file]
        if apply:
            evidence_paths.append(str(self.manager.workspace / "subagent_patch_apply_log.jsonl"))

        return PatchApplyRecord(
            id=_new_id("patchapply"),
            run_id=task.id,
            dry_run=not apply,
            applied=apply and ok,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=patch_count,
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

    
    def _normalize_patch_apply_spec(self, task: SubAgentTask, patch: dict) -> dict:
        """Normalize patch spec with write boundary enforcement.

        SECURITY: This is the critical boundary check for file writes.
        """

        raw_path = str(patch.get("path") or "").strip()
        status = str(patch.get("status") or "").strip().lower()
        patch_type = str(
            patch.get("tool")
            or patch.get("type")
            or (
                "write_file"
                if any(key in patch for key in ("content", "new_content", "file_content", "after"))
                else ""
            )
        ).strip().lower()
        content = patch.get("content")
        if content is None:
            for key in ("new_content", "file_content", "desired_content", "after"):
                if patch.get(key) is not None:
                    content = patch.get(key)
                    break
        diff_text = ""
        for key in ("diff", "patch", "patch_diff", "unified_diff"):
            value = patch.get(key)
            if isinstance(value, str) and value.strip():
                diff_text = value
                break

        audit = {
            "path": raw_path,
            "status": status or "unknown",
            "review_status": str(patch.get("review_status") or "UNREVIEWED"),
            "summary": str(patch.get("summary") or ""),
            "patch_type": patch_type or "unknown",
            "apply_status": "PENDING",
            "diff_preview": "",
            "actual_diff": "",
            "message": "",
        }
        if not raw_path:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = "patch 缺少 path。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if status not in {"planned", "applied"}:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = f"patch status={status or 'unknown'} 不能进入 apply。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if patch_type and patch_type not in _PATCH_APPLY_WRITE_TYPES:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = f"只支持 write_file patch，当前类型是 {patch_type}。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if not isinstance(content, str):
            audit["apply_status"] = "BLOCKED"
            audit["message"] = "write_file patch 缺少完整 content，不能安全 apply。"
            audit["diff_preview"] = diff_text
            return {"ok": False, "audit": audit, "patch_ref": patch}

        boundary_error = validate_write_boundary(
            "write_file",
            {"path": raw_path},
            workspace_root=self.manager.workspace_root,
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

        target = self._resolve_patch_target(raw_path)
        before_text = target.read_text(encoding="utf-8") if target.exists() else ""
        audit["diff_preview"] = diff_text or build_unified_diff(raw_path, before_text, content)
        audit["message"] = "patch 可以进入 apply。"
        return {
            "ok": True,
            "audit": audit,
            "patch_ref": patch,
            "target": target,
            "content": content,
            "path": raw_path,
        }

    def _resolve_patch_target(self, raw_path: str) -> Path:
        """Resolve patch target path relative to workspace root."""

        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = self.manager.workspace_root / target
        return target.resolve(strict=False)