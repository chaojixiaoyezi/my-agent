# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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
from agent_py_agent.agent.subagents.services.indexing_params import (
    DataclassRecordIndexParams,
    IndexReportParams,
)
from agent_py_agent.agent.subagents.utils import _read_json_object

from .patch_apply_reports import patch_apply_record_to_dict
from .patch_apply_task import (
    ApplyPatchTaskParams,
    apply_patch_task,
    normalize_patch_apply_spec,
    resolve_patch_target,
)


# LLM: PatchApplyOptions 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 集中保存补丁应用选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class PatchApplyOptions:
    """Options bundle for patch apply report entrypoints."""

    # LLM: apply policy knobs travel together so future gates do not widen public signatures.
    apply: bool = False
    applier: str = "parent"
    note: str = ""
    limit: int = 0

    # LLM: from_values 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 转换values的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
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


# LLM: _patch_apply_options 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁应用选项相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
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


# LLM: PatchApplyService 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 封装补丁应用服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发补丁文件、预演结果和应用报告相关副作用，需保持公开契约稳定。
class PatchApplyService:
    """Execute patch apply with write boundary enforcement and rollback support."""

    # LLM: __init__ 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
    def __init__(self, manager):
        self.manager = manager

    # LLM: apply_patches 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 更新patches对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
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
        from agent_py_agent.agent.subagents.services.patch_apply_summary import PatchApplySummary

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

    # LLM: write_apply_report 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 写入报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
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

    # LLM: _write_apply_records 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 写入记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
    def _write_apply_records(self, report: PatchApplyReport, *, apply: bool) -> None:
        """Persist per-run patch apply records and append apply logs when requested."""
        from agent_py_agent.agent.subagents.services.patch_apply_record_files import (
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

    # LLM: _apply_patch_task 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 更新补丁任务对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
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

    # LLM: _normalize_patch_apply_spec 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 解析并归一化补丁应用spec的输入形态，让下游只处理稳定结构；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
    def _normalize_patch_apply_spec(self, task, patch):
        """Backward-compatible wrapper for patch spec normalization."""
        return normalize_patch_apply_spec(self.manager, task, patch)

    # LLM: _resolve_patch_target 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
    # 函数用途: 读取或查询补丁target需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _resolve_patch_target(self, raw_path: str):
        """Backward-compatible wrapper for patch path resolution."""
        return resolve_patch_target(self.manager, raw_path)


# LLM: _collect_patch_apply_records 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 读取或查询补丁应用记录需要的状态，返回调用方可继续处理的快照；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def _collect_patch_apply_records(service: PatchApplyService, run_ids, opts: PatchApplyOptions):
    from pathlib import Path

    from agent_py_agent.agent.subagents.parsing import _dict_list

    records = []
    for task in service.manager._select_runs(run_ids):
        output = _read_json_object(Path(task.output_json))
        patches = _dict_list(output.get("patches", []))
        if run_ids is None and not patches:
            continue
        records.append(_apply_patch_record(_PatchApplyRecordParams(service, task, output, patches, opts)))
        if opts.limit > 0 and len(records) >= opts.limit:
            break
    return records


# LLM: _PatchApplyRecordParams 属于子代理补丁应用的类边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 类用途: 集中保存补丁应用记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _PatchApplyRecordParams:

    service: PatchApplyService
    task: object
    output: dict
    patches: list[dict]
    opts: PatchApplyOptions


# LLM: _apply_patch_record 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 更新补丁记录对应的任务或运行状态，并保留既有字段语义；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
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


# LLM: _write_patch_apply_report_json 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 写入补丁应用报告JSON的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
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
