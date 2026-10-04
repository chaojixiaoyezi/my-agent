
"""Patch application with file write boundary enforcement.

Human version:
这个模块处理 patch 的实际文件写入，是安全敏感的边界检查核心。
所有文件操作必须通过 validate_write_boundary 校验后才执行。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from agent_py_agent.agent.common.json_io import (
    append_private_jsonl_records,
    append_private_text,
    read_json_object_report,
    write_private_text_file_atomic,
)
from agent_py_agent.agent.subagents.reports import PatchApplyRecord, PatchApplyReport
from agent_py_agent.agent.subagents.services.indexing.records import (
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
from .patch_renderer import render_patch_apply_record_markdown


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
            summary=_patch_apply_summary(records),
            records=records,
        )

    # LLM: 应用报告 JSON/Markdown 走私有写（0600/0700）；报告内容与旧格式逐字节一致。
    # 函数用途: 写出一份补丁应用报告的 JSON 与 Markdown。
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
        # 补丁应用报告属宿主运行数据：私有原子写（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
        write_private_text_file_atomic(
            self.manager.workspace / "SUBAGENT_PATCH_APPLY.md",
            render_patch_apply_markdown(report),
        )
        self._write_apply_records(report, apply=opts.apply)
        self.manager.indexing.index_report(
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
        for record in report.records:
            _write_patch_apply_record_file(record, self.manager)
            self.manager.indexing._index_dataclass_record(
                DataclassRecordIndexParams(
                    "subagent_patch_apply",
                    record.id,
                    f"Patch apply {record.run_id} {record.decision}",
                    record,
                    "subagent_patch_apply_logged",
                ),
            )
            if apply:
                _append_patch_apply_log(record, self.manager)

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
        """Apply patches for one task."""
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
        """Normalize one patch spec for a task."""
        return normalize_patch_apply_spec(self.manager, task, patch)

    def _resolve_patch_target(self, raw_path: str):
        """Resolve a patch path through the manager write boundary."""
        return resolve_patch_target(self.manager, raw_path)


def _collect_patch_apply_records(service: PatchApplyService, run_ids, opts: PatchApplyOptions):
    records = []
    for task in service.manager.indexing.select_runs(run_ids):
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


def _patch_apply_summary(records: list[PatchApplyRecord]) -> dict[str, int]:
    summary = {"total": len(records)}
    for record in records:
        summary[record.decision] = summary.get(record.decision, 0) + 1
        ok_key = "ok" if record.ok else "failed"
        mode_key = "dry_run" if record.dry_run else "applied"
        summary[ok_key] = summary.get(ok_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
        if record.rollback_performed:
            summary["rolled_back"] = summary.get("rolled_back", 0) + 1
    return summary


# LLM: 报告 JSON 走私有写（0600/0700）；不改变序列化格式。
# 函数用途: 把补丁应用报告写成 JSON 文件。
def _write_patch_apply_report_json(manager, report: PatchApplyReport) -> None:
    payload = {
        "generated_at": report.generated_at,
        "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [patch_apply_record_to_dict(r) for r in report.records],
    }
    # 补丁应用报告属宿主运行数据：私有原子写（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
    write_private_text_file_atomic(
        manager.workspace / "subagent_patch_apply_report.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


# LLM: 记录 Markdown 走私有写（0600/0700）；内容与旧实现逐字节一致。
# 函数用途: 把一条补丁应用记录写成 Markdown 文件。
def _write_patch_apply_record_file(record: PatchApplyRecord, manager) -> None:
    try:
        task = manager.load(record.run_id)
    except FileNotFoundError:
        return
    record_json = task.reports_dir_path / "patch_apply.json"
    record_md = task.task_dir_path / "PATCH_APPLY.md"
    write_private_text_file_atomic(
        record_json,
        json.dumps(patch_apply_record_to_dict(record), ensure_ascii=False, indent=2),
    )
    write_private_text_file_atomic(record_md, render_patch_apply_record_markdown(record))


# LLM: 日志追加走私有写（0600/0700）；JSONL 行格式不变。
# 函数用途: 把一条补丁应用记录追加进 JSONL 日志并补齐 Markdown 头。
def _append_patch_apply_log(record: PatchApplyRecord, manager) -> None:
    jsonl = manager.workspace / "subagent_patch_apply_log.jsonl"
    # 补丁应用账属宿主运行数据：私有追加（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(jsonl, [patch_apply_record_to_dict(record)], sort_keys=False)

    markdown = manager.workspace / "PATCH_APPLY_LOG.md"
    if not markdown.exists():
        write_private_text_file_atomic(markdown, "# PATCH APPLY LOG\n\n")
    status = "OK" if record.ok else "FAIL"
    append_private_text(
        markdown,
        f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
        f"rollback={record.rollback_performed} message={record.message}\n",
    )
