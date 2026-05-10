# LLM: Empty test reports need a narrow decision helper so artifact-only work is not misrouted.
# 模块用途: 判断空 `test_execution.json` 是否可进入父级检查；只看 refs 元数据，不读取 artifact 正文。

from __future__ import annotations

from typing import Any

from .models import SubAgentTask
from .parsing import _dict_list


# LLM: executable_tests drops empty generated command placeholders before parent planning.
# 函数用途: 把 tests 中真正可执行的检查筛出来；空 command 只计数，不触发人工确认或真实执行。
def executable_tests(tests: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    executable: list[dict[str, Any]] = []
    ignored_empty_command_count = 0
    for item in tests:
        method = str(item.get("validation_method") or "command").strip() or "command"
        command = str(item.get("command") or "").strip()
        if method == "command" and not command:
            ignored_empty_command_count += 1
            continue
        executable.append(item)
    return executable, ignored_empty_command_count


# LLM: empty_report_is_inspectable distinguishes "no executable tests" from failed tests.
# 函数用途: 当 total=0/failed=0 且没有可执行测试时，如果已有可追踪证据 refs，则允许进入 inspect_only。
def empty_report_is_inspectable(
    *,
    task: SubAgentTask,
    output: dict[str, Any],
    report: Any,
    has_executable_tests: bool,
) -> bool:
    return (
        report.total_tests == 0
        and report.failed == 0
        and not has_executable_tests
        and has_traceable_acceptance_evidence(task, output)
    )


# LLM: has_traceable_acceptance_evidence checks metadata refs only and avoids body reads.
# 函数用途: 判断任务或 output 是否已有 evidence/artifact 引用，可供父级验收继续检查。
def has_traceable_acceptance_evidence(task: SubAgentTask, output: dict[str, Any]) -> bool:
    if any(item.evidence_refs or item.artifact_refs for item in task.evidence_packets):
        return True
    return any(
        str(item.get("path") or item.get("uri") or item.get("artifact_id") or "").strip()
        for item in _dict_list(output.get("artifacts", []))
    )
