# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""patch apply task executor with rollback support."""

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


# LLM: PatchApplyParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存补丁应用参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: PatchApplyExecutor 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装补丁应用executor相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class PatchApplyExecutor:
    """Execute patch apply with rollback support."""

    # LLM: execute 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
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


# LLM: _run_patch_apply_tests 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 推进补丁应用tests的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
def _run_patch_apply_tests(params: PatchApplyParams) -> list:
    if not params.test_commands:
        return []
    test_results = run_patch_apply_tests(params.test_commands, params.manager.workspace_root)
    failed = [item for item in test_results if not item.get("ok")]
    if failed:
        raise RuntimeError(f"{len(failed)} 个 apply 后测试失败。")
    return test_results


# LLM: _write_patch_apply_success 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 写入补丁应用success的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
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
