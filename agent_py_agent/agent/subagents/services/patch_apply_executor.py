"""LLM: patch apply task executor with rollback support."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.subagents.patch.patch_apply_helpers import run_patch_apply_tests
from agent_py_agent.agent.subagents.patch.patch_file_ops import (
    PatchFileApplyContext,
    do_apply_patches,
    rollback_patch_apply,
)
from agent_py_agent.agent.subagents.utils import _read_json_object


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
    def execute(
        params: PatchApplyParams,
    ):
        """Execute patch apply with rollback on failure."""
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


def _run_patch_apply_tests(params: PatchApplyParams) -> list:
    if not params.test_commands:
        return []
    test_results = run_patch_apply_tests(params.test_commands, params.manager.workspace_root)
    failed = [item for item in test_results if not item.get("ok")]
    if failed:
        raise RuntimeError(f"{len(failed)} 个 apply 后测试失败。")
    return test_results


def _write_patch_apply_success(params: PatchApplyParams, applied_count: int, test_results: list) -> None:
    output = _read_json_object(Path(params.task.output_json))
    output["patches"] = params.review_status_updates
    Path(params.task.output_json).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    params.manager._append_task_work_log(
        params.task,
        f"patch_apply: applied={applied_count} tests={len(test_results)} applier={params.applier}",
    )
