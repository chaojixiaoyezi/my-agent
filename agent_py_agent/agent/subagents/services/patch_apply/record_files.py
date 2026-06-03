
"""Patch apply record file writing helper."""

from __future__ import annotations

import json
from pathlib import Path


class PatchApplyRecordFiles:
    """Write patch apply record files to task directory."""

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