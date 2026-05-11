# LLM: Parent acceptance preflight stays side-effect free and separate from decision assembly.
# 模块用途: 准备父级验收测试项并预检命令安全性，不执行命令、不写任务状态。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .execution_executor import TestExecutor
from .execution_test_items import TestItemPreparationRequest, prepare_test_items
from .models import SubAgentTask
from .parsing import _dict_list
from .static_required_files import required_static_files_for_task


# LLM: unsafe_test_reason preflights command syntax through TestExecutor validation without running it.
# 函数用途: 检查 tests 里的命令是否触发 allowlist 或 shell 字符风险；只做预检，不执行命令。
def unsafe_test_reason(tests: list[dict[str, Any]], workspace_root: str | Path) -> str:
    executor = TestExecutor(workspace_root)
    for item in tests:
        reason = _unsafe_command_reason(item, executor)
        if reason:
            return reason
    return ""


# LLM: prepared_tests_for_parent_acceptance aligns dry-run checks with bounded executor input.
# 函数用途: 在父级验收预检前归一化 tests，并合入任务文本里的静态 required files。
def prepared_tests_for_parent_acceptance(
    output: dict[str, Any],
    workspace_root: str | Path,
    task: SubAgentTask,
) -> list[dict[str, Any]]:
    return prepare_test_items(
        TestItemPreparationRequest(
            tests=_dict_list(output.get("tests", [])),
            output=output,
            workspace_root=workspace_root,
            required_files=required_static_files_for_task(task),
        )
    )


# LLM: _unsafe_command_reason checks one prepared test item and keeps the public loop flat.
# 函数用途: 对单条 command 测试做安全预检；非 command 项直接跳过。
def _unsafe_command_reason(item: dict[str, Any], executor: TestExecutor) -> str:
    method = str(item.get("validation_method") or "command").strip() or "command"
    if method != "command":
        return ""
    command = str(item.get("command") or "").strip()
    error = executor._validate_command(command)
    if not error:
        return ""
    name = str(item.get("name") or command or "unknown").strip()
    return f"test command requires human confirmation: {name}: {error}"
