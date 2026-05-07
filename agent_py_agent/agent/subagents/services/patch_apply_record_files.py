# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply record file writing helper."""

from __future__ import annotations

import json
from pathlib import Path


# LLM: PatchApplyRecordFiles 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装补丁应用记录文件相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class PatchApplyRecordFiles:
    """Write patch apply record files to task directory."""

    # LLM: write_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入write记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def write_record(record, manager) -> None:
        """Write single patch apply record to task directory."""

        from agent_py_agent.agent.subagents.patch.patch_apply_reports import (
            patch_apply_record_to_dict,
        )
        from agent_py_agent.agent.subagents.patch.patch_renderer import (
            render_patch_apply_record_markdown,
        )

        try:
            task = manager.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "patch_apply.json"
        record_md = Path(task.task_dir) / "PATCH_APPLY.md"
        record_json.write_text(
            json.dumps(patch_apply_record_to_dict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record_md.write_text(render_patch_apply_record_markdown(record), encoding="utf-8")

    # LLM: append_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入appendlog的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def append_log(record, manager) -> None:
        """Append patch apply record to global audit log."""

        from agent_py_agent.agent.file_io import append_jsonl
        from agent_py_agent.agent.subagents.patch.patch_apply_reports import (
            patch_apply_record_to_dict,
        )

        jsonl = manager.workspace / "subagent_patch_apply_log.jsonl"
        append_jsonl(jsonl, patch_apply_record_to_dict(record))

        markdown = manager.workspace / "PATCH_APPLY_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PATCH APPLY LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"rollback={record.rollback_performed} message={record.message}\n"
            )