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
    path = Path(text).expanduser()
    candidates = [path] if path.is_absolute() else []
    roots = _artifact_roots(workspace, task_dir)
    if not path.is_absolute():
        candidates.extend(
            [
                root / path
                for root in roots
            ]
        )
    if any(candidate.exists() for candidate in candidates):
        return True
    if not path.is_absolute():
        return _path_suffix_exists(path, roots)
    if _absolute_path_inside_roots(path, roots):
        return _absolute_path_suffix_exists(path, roots)
    return False


# LLM: _artifact_roots searches runtime and user workspace roots for model-relative artifact paths.
# 函数用途: 让 .my_agent_runtime/<case>/subagents 下的验收也能用 leaf_outputs/... 找到项目产物。
def _artifact_roots(workspace: Path, task_dir: str) -> list[Path]:
    roots = [Path(task_dir), workspace, workspace.parent]
    runtime_root = _runtime_project_root(workspace)
    if runtime_root is not None:
        roots.append(runtime_root)
    return list(dict.fromkeys(root.resolve() for root in roots))


# LLM: _runtime_project_root maps hidden agent runtime paths back to the visible project root.
# 函数用途: 从 <project>/.my_agent_runtime/<case>/subagents 或 <project>/.my_agent/subagents 推回 project。
def _runtime_project_root(workspace: Path) -> Path | None:
    parts = workspace.resolve().parts
    index = _runtime_marker_index(parts)
    return Path(*parts[:index]) if index > 0 else None


# LLM: _runtime_marker_index keeps runtime root detection shallow for code-size guards.
# 函数用途: 返回隐藏 runtime 标记所在下标；找不到时返回 -1。
def _runtime_marker_index(parts: tuple[str, ...]) -> int:
    for marker in (".my_agent_runtime", ".my_agent"):
        try:
            return parts.index(marker)
        except ValueError:
            continue
    return -1


# LLM: _path_suffix_exists recovers model-reported relative artifacts from nested task output dirs.
# 函数用途: 当 runner 少写了外层任务目录时，在安全候选根目录内按路径后缀查找真实文件。
def _path_suffix_exists(relative_path: Path, roots: list[Path]) -> bool:
    parts = relative_path.parts
    if not parts:
        return False
    return any(
        _path_has_suffix(item, parts)
        for root in roots
        if root.exists() and root.is_dir()
        for item in root.rglob(parts[-1])
    )


# LLM: _absolute_path_inside_roots avoids repairing arbitrary absolute paths outside the task workspace.
# 函数用途: 只有不存在的绝对路径仍位于 workspace/runtime 根下，才尝试按尾部路径片段恢复。
def _absolute_path_inside_roots(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True
    return False


# LLM: _absolute_path_suffix_exists recovers model typos in run/case directory names.
# 函数用途: 绝对 artifact 路径不存在时，用足够长的尾部路径在安全根目录内查找真实文件。
def _absolute_path_suffix_exists(path: Path, roots: list[Path]) -> bool:
    parts = path.parts
    max_depth = min(6, len(parts))
    return any(
        _path_suffix_exists(Path(*parts[-depth:]), roots)
        for depth in range(max_depth, 2, -1)
    )


# LLM: _path_has_suffix keeps nested artifact recovery shallow enough for strict size guards.
# 函数用途: 判断真实文件路径是否以 runner 报告的相对路径片段结尾。
def _path_has_suffix(path: Path, parts: tuple[str, ...]) -> bool:
    return len(path.parts) >= len(parts) and path.parts[-len(parts):] == parts
