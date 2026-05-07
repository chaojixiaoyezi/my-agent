# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Build findings for tests and artifacts, and check artifact paths.

新手说明:
检查 runner 记录的测试是否通过、artifact 路径是否真实存在。
"""

from collections.abc import Callable
from pathlib import Path

from ..models import SubAgentTask
from ..parsing import _dict_list
from ..reports import AcceptanceReviewFinding


# LLM: _build_test_and_artifact_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建test产物findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_test_and_artifact_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
    artifact_exists_fn: Callable[[str], bool],
) -> list[AcceptanceReviewFinding]:
    """Build findings for tests and artifacts."""
    tests = _dict_list(output.get("tests", []))
    failed_tests = [item for item in tests if not bool(item.get("ok", False))]
    artifacts = _dict_list(output.get("artifacts", []))
    return [
        _tests_finding(task, tests, failed_tests, created_at),
        _artifacts_finding(task, artifacts, artifact_exists_fn, created_at),
    ]


# LLM: _tests_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理testsfinding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _tests_finding(task, tests, failed_tests, created_at):
    return AcceptanceReviewFinding(
        name="tests_passed",
        ok=not failed_tests,
        severity="P1",
        message=(
            f"runner 记录的 {len(tests)} 条测试均通过。"
            if tests and not failed_tests
            else "runner 未记录测试，允许仅凭证据进入人工验收。"
            if not tests
            else f"存在 {len(failed_tests)} 条失败测试。"
        ),
        evidence_path=task.output_json,
        created_at=created_at,
    )


# LLM: _artifacts_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理产物finding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _artifacts_finding(task, artifacts, artifact_exists_fn, created_at):
    missing_artifacts = _missing_artifacts(artifacts, artifact_exists_fn)
    return (
        AcceptanceReviewFinding(
            name="artifact_paths_exist",
            ok=not missing_artifacts,
            severity="P1",
            message=(
                f"runner 记录的 {len(artifacts)} 个 artifact 路径可核对。"
                if not missing_artifacts
                else f"存在 {len(missing_artifacts)} 个 artifact 路径不存在: {missing_artifacts[0]}"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )


# LLM: _missing_artifacts 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理missing产物相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持验收证据、补丁摘要和就绪判断上的返回值和副作用边界稳定。
def _missing_artifacts(artifacts, artifact_exists_fn):
    return [
        str(item.get("path", "") or "")
        for item in artifacts
        if str(item.get("path", "") or "").strip()
        and not artifact_exists_fn(str(item.get("path", "") or ""))
    ]


# LLM: _check_artifact_exists 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 校验产物exists需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_artifact_exists(
    workspace: Path,
    task_dir: str,
    raw_path: str,
) -> bool:

    text = raw_path.strip()
    if not text or "://" in text:
        return True
    path = Path(text)
    candidates = [path] if path.is_absolute() else []
    if not path.is_absolute():
        candidates.extend(
            [
                Path(task_dir) / path,
                workspace / path,
                workspace.parent / path,
            ]
        )
        if workspace.name == "subagents" and workspace.parent.name == ".my_agent":
            candidates.append(workspace.parent.parent / path)
    return any(candidate.exists() for candidate in candidates)
