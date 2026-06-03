
"""Patch application with file write boundary enforcement.

Human version:
这个模块处理 patch 的实际文件写入，是安全敏感的边界检查核心。
所有文件操作必须通过 validate_write_boundary 校验后才执行。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.subagents.reports import PatchApplyRecord, PatchApplyReport
from agent_py_agent.agent.subagents.services.indexing.params import (
    DataclassRecordIndexParams,
    IndexReportParams,
)
from agent_py_agent.agent.subagents.utils import _new_id

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

    apply: bool = False
    applier: str = "parent"
    note: str = ""
    limit: int = 0

    @classmethod
    def from_values(
        cls,
        options: PatchApplyOptions | None = None,
        *,
        apply: bool | None = None,
        applier: str | None = None,
        note: str | None = None,
        limit: int | None = None,
    ):
        base = options or cls()
        updates = {"apply": apply, "applier": applier, "note": note, "limit": limit}
        clean = {key: value for key, value in updates.items() if value is not None}
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
        from agent_py_agent.agent.subagents.services.patch_apply.summary import PatchApplySummary

        opts = _patch_apply_options(
            options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )
        records = _collect_patch_apply_records(self, run_ids, opts)
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

        opts = _patch_apply_options(
            options,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )
        report = self.apply_patches(run_ids, options=opts)
        _write_patch_apply_report_json(self.manager, report)
        (self.manager.workspace / "SUBAGENT_PATCH_APPLY.md").write_text(
            render_patch_apply_markdown(report),
            encoding="utf-8",
        )
        self._write_apply_records(report, apply=opts.apply)
        self.manager._index_report(
            IndexReportParams(
                "subagent_patch_apply_report",
                "latest",
                "Subagent patch apply report",
                report,
                "subagent_patch_apply_report_written",
            ),
        )
        return report

    def _write_apply_records(self, report: PatchApplyReport, *, apply: bool) -> None:
        """Persist per-run patch apply records and append apply logs when requested."""
        from agent_py_agent.agent.subagents.services.patch_apply.record_files import (
            PatchApplyRecordFiles,
        )

        for record in report.records:
            PatchApplyRecordFiles.write_record(record, self.manager)
            self.manager._index_dataclass_record(
                DataclassRecordIndexParams(
                    "subagent_patch_apply",
                    record.id,
                    f"Patch apply {record.run_id} {record.decision}",
                    record,
                    "subagent_patch_apply_logged",
                ),
            )
            if apply:
                PatchApplyRecordFiles.append_log(record, self.manager)

    def _apply_patch_task(
        self,
        task,
        *,
        params: ApplyPatchTaskParams | None = None,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        applier: str = "parent",
        note: str = "",
    ):
        """Backward-compatible wrapper for single-task patch application."""
        params = params or ApplyPatchTaskParams(
            output=output or {},
            patches=patches or [],
            apply=bool(apply),
            applier=applier,
            note=note,
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


def _collect_patch_apply_records(service: PatchApplyService, run_ids, opts: PatchApplyOptions):
    records = []
    for task in service.manager._select_runs(run_ids):
        record = _patch_apply_record_for_task(service, task, run_ids, opts)
        if record is None:
            continue
        records.append(record)
        if opts.limit > 0 and len(records) >= opts.limit:
            break
    return records


def _patch_apply_record_for_task(service: PatchApplyService, task, run_ids, opts: PatchApplyOptions):
    from pathlib import Path

    from agent_py_agent.agent.subagents.parsing import _dict_list

    read_report = read_json_object_report(
        Path(task.output_json),
        parse_nested_string=True,
        context="patch_apply.output_json",
    )
    if read_report.load_error is not None:
        return _patch_apply_output_load_error_record(task, opts, read_report.load_error)
    output = read_report.payload
    patches = _dict_list(output.get("patches", []))
    if run_ids is None and not patches:
        return None
    return _apply_patch_record(_PatchApplyRecordParams(service, task, output, patches, opts))


def _patch_apply_output_load_error_record(task, opts: PatchApplyOptions, load_error: dict[str, object]):
    return PatchApplyRecord(
        id=_new_id("patchapply"),
        run_id=task.id,
        dry_run=not opts.apply,
        applied=False,
        ok=False,
        decision="OUTPUT_LOAD_ERROR",
        message="output.json 读取失败；这不是没有 patch，请先修复或重建该子代理输出账本。",
        patch_count=0,
        blocked_count=1,
        applier=opts.applier,
        note=opts.note,
        evidence_paths=[task.output_json, task.work_log_file],
        load_errors=[load_error],
        created_at=time.time(),
    )


@dataclass(frozen=True)
class _PatchApplyRecordParams:

    service: PatchApplyService
    task: object
    output: dict
    patches: list[dict]
    opts: PatchApplyOptions


def _apply_patch_record(params: _PatchApplyRecordParams):
    opts = params.opts
    service = params.service
    return service._apply_patch_task(
        params.task,
        params=ApplyPatchTaskParams(
            output=params.output,
            patches=params.patches,
            apply=opts.apply,
            applier=opts.applier,
            note=opts.note,
        ),
    )


def _write_patch_apply_report_json(manager, report: PatchApplyReport) -> None:
    payload = {
        "generated_at": report.generated_at,
        "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [patch_apply_record_to_dict(r) for r in report.records],
    }
    (manager.workspace / "subagent_patch_apply_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
