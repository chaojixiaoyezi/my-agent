"""Patch apply task executor with rollback support."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_py_agent.agent.subagents.patch.patch_apply_helpers import run_patch_apply_tests
from agent_py_agent.agent.subagents.patch.patch_file_ops import (
    do_apply_patches,
    rollback_patch_apply,
)
from agent_py_agent.agent.subagents.utils import _read_json_object


class PatchApplyExecutor:
    """Execute patch apply with rollback support."""

    @staticmethod
    def execute(
        patch_specs,
        review_status_updates,
        task,
        manager,
        applier,
        note,
        test_commands,
    ):
        """Execute patch apply with rollback on failure."""
        touched_files = {}
        applied_count = 0
        rollback_performed = False
        test_results = []

        try:
            applied_count, touched_files = do_apply_patches(
                patch_specs, review_status_updates, task, manager, applier, note
            )
            if test_commands:
                test_results = run_patch_apply_tests(test_commands, manager.workspace_root)
                failed = [item for item in test_results if not item.get("ok")]
                if failed:
                    raise RuntimeError(f"{len(failed)} 个 apply 后测试失败。")

            output = _read_json_object(Path(task.output_json))
            output["patches"] = review_status_updates
            Path(task.output_json).write_text(
                json.dumps(output, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manager._append_task_work_log(
                task,
                f"patch_apply: applied={applied_count} tests={len(test_results)} applier={applier}",
            )
        except Exception as exc:
            rollback_performed = bool(touched_files)
            rollback_patch_apply(touched_files)
            for spec in patch_specs:
                spec["audit"]["apply_status"] = "ROLLED_BACK" if rollback_performed else "FAILED"
                spec["audit"]["message"] = f"apply 失败: {exc}"
                spec["patch_ref"]["apply_status"] = spec["audit"]["apply_status"]
            raise RuntimeError(str(exc)) from exc

        return applied_count, touched_files, rollback_performed, test_results