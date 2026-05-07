"""Patch application with file write boundary enforcement.

Human version:
这个模块处理 patch 的实际文件写入，是安全敏感的边界检查核心。
所有文件操作必须通过 validate_write_boundary 校验后才执行。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from agent_py_agent.agent.subagents.reports import PatchApplyReport
from agent_py_agent.agent.subagents.utils import _read_json_object

from .patch_apply_reports import patch_apply_record_to_dict
from .patch_apply_task import (
    ApplyPatchTaskParams,
    apply_patch_task,
    normalize_patch_apply_spec,
    resolve_patch_target,
)


@dataclass(frozen=True)
class PatchApplyOptions:
    """Options bundle for patch apply report entrypoints."""

    # LLM: apply policy knobs travel together so future gates do not widen public signatures.
    apply: bool = False
    applier: str = "parent"
    note: str = ""
    limit: int = 0

    @classmethod
    def from_values(cls, options: PatchApplyOptions | None = None, **overrides):
        base = options or cls()
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(base, **clean)


def _patch_apply_options(
    options: PatchApplyOptions | None,
    *,
    apply: bool,
    applier: str,
    note: str,
    limit: int,
) -> PatchApplyOptions:
    if options is not None and (apply, applier, note, limit) == (False, "parent", "", 0):
        return options
    return PatchApplyOptions.from_values(
        options,
        apply=apply,
        applier=applier,
        note=note,
        limit=limit,
    )


class PatchApplyService:
    """Execute patch apply with write boundary enforcement and rollback support."""

    def __init__(self, manager):
        self.manager = manager

    def apply_patches(
        self,
        run_ids=None,
        *,
        options: PatchApplyOptions | None = None,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ) -> PatchApplyReport:
        """Execute the independent patch-apply audit chain for runner-declared file writes."""
        from pathlib import Path

        from agent_py_agent.agent.subagents.parsing import _dict_list
        from agent_py_agent.agent.subagents.services.patch_apply_summary import PatchApplySummary

        opts = _patch_apply_options(
            options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )
        records = []
        for task in self.manager._select_runs(run_ids):
            output = _read_json_object(Path(task.output_json))
            patches = _dict_list(output.get("patches", []))
            if run_ids is None and not patches:
                continue
            records.append(
                self._apply_patch_task(
                    task,
                    params=ApplyPatchTaskParams(
                        output=output,
                        patches=patches,
                        apply=opts.apply,
                        applier=opts.applier,
                        note=opts.note,
                    ),
                )
            )
            if opts.limit > 0 and len(records) >= opts.limit:
                break

        return PatchApplyReport(
            generated_at=time.time(),
            dry_run=not opts.apply,
            summary=PatchApplySummary.build(records),
            records=records,
        )

    def write_apply_report(
        self,
        run_ids=None,
        *,
        options: PatchApplyOptions | None = None,
        apply=False,
        applier="parent",
        note="",
        limit=0,
    ) -> PatchApplyReport:
        """Write patch apply report to disk."""
        from agent_py_agent.agent.subagents.patch.patch_renderer import render_patch_apply_markdown
        from agent_py_agent.agent.subagents.services.patch_apply_record_files import (
            PatchApplyRecordFiles,
        )

        opts = _patch_apply_options(
            options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )
        report = self.apply_patches(run_ids, options=opts)
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
        self._write_apply_records(report, apply=opts.apply)
        self.manager._index_report(
            "subagent_patch_apply_report",
            "latest",
            "Subagent patch apply report",
            report,
            event_type="subagent_patch_apply_report_written",
        )
        return report

    def _write_apply_records(self, report: PatchApplyReport, *, apply: bool) -> None:
        """Persist per-run patch apply records and append apply logs when requested."""
        from agent_py_agent.agent.subagents.services.patch_apply_record_files import (
            PatchApplyRecordFiles,
        )

        for record in report.records:
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

    def _apply_patch_task(
        self,
        task,
        *,
        params: ApplyPatchTaskParams | None = None,
        **legacy,
    ):
        """Backward-compatible wrapper for single-task patch application."""
        # LLM: legacy kwargs are accepted only to build the task params bundle.
        params = params or ApplyPatchTaskParams(
            output=legacy.get("output") or {},
            patches=legacy.get("patches") or [],
            apply=bool(legacy.get("apply", False)),
            applier=legacy.get("applier", "parent"),
            note=legacy.get("note", ""),
        )
        return apply_patch_task(
            self.manager, task,
            params=params,
        )

    def _normalize_patch_apply_spec(self, task, patch):
        """Backward-compatible wrapper for patch spec normalization."""
        return normalize_patch_apply_spec(self.manager, task, patch)

    def _resolve_patch_target(self, raw_path: str):
        """Backward-compatible wrapper for patch path resolution."""
        return resolve_patch_target(self.manager, raw_path)
