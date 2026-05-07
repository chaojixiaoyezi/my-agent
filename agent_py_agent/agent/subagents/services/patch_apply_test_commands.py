# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply test command extraction and validation helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import SubAgentTask


# LLM: PatchApplyTestCommands 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装补丁应用testcommands相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class PatchApplyTestCommands:
    """Extract and validate test commands for patch apply."""

    # LLM: extract 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理extract相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    @staticmethod
    def extract(
        task: SubAgentTask,
        output: dict,
    ) -> tuple[list[str], list[str]]:
        """Extract valid test commands from task acceptance checks and output."""

        from agent_py_agent.agent.subagents.parsing import _dict_list
        from agent_py_agent.agent.subagents.patch.patch_apply_helpers import (
            extract_patch_test_command,
            validate_patch_test_command,
        )

        commands: list[str] = []
        blocked: list[str] = []
        for check in task.acceptance_checks:
            command = extract_patch_test_command(check)
            _append_validated_command(command, commands, blocked, validate_patch_test_command)
        for test in _dict_list(output.get("tests", [])):
            command = str(test.get("command") or "").strip()
            _append_validated_command(command, commands, blocked, validate_patch_test_command)
        return commands, blocked


# LLM: _append_validated_command 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 写入validatedcommand的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _append_validated_command(
    command: str,
    commands: list[str],
    blocked: list[str],
    validate_patch_test_command,
) -> None:
    if not command:
        return
    problem = validate_patch_test_command(command)
    if problem:
        blocked.append(problem)
    elif command not in commands:
        commands.append(command)
