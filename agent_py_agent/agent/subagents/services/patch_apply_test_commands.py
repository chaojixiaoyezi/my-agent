"""Patch apply test command extraction and validation helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import SubAgentTask


class PatchApplyTestCommands:
    """Extract and validate test commands for patch apply."""

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

        commands = []
        blocked = []
        for check in task.acceptance_checks:
            command = extract_patch_test_command(check)
            if not command:
                continue
            problem = validate_patch_test_command(command)
            if problem:
                blocked.append(problem)
            elif command not in commands:
                commands.append(command)
        for test in _dict_list(output.get("tests", [])):
            command = str(test.get("command") or "").strip()
            if not command:
                continue
            problem = validate_patch_test_command(command)
            if problem:
                blocked.append(problem)
            elif command not in commands:
                commands.append(command)
        return commands, blocked