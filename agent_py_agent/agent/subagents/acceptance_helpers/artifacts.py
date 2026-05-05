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


def _build_test_and_artifact_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
    artifact_exists_fn: Callable[[str], bool],
) -> list[AcceptanceReviewFinding]:
    """Build findings for tests and artifacts."""
    findings: list[AcceptanceReviewFinding] = []

    tests = _dict_list(output.get("tests", []))
    failed_tests = [item for item in tests if not bool(item.get("ok", False))]
    findings.append(
        AcceptanceReviewFinding(
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
    )

    artifacts = _dict_list(output.get("artifacts", []))
    missing_artifacts = [
        str(item.get("path", "") or "")
        for item in artifacts
        if str(item.get("path", "") or "").strip()
        and not artifact_exists_fn(str(item.get("path", "") or ""))
    ]
    findings.append(
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
    return findings


def _check_artifact_exists(
    workspace: Path,
    task_dir: str,
    raw_path: str,
) -> bool:
    """LLM: check whether an artifact path declared by runner actually exists on disk.

    新手说明:
    runner 可能声称写了一个文件，但实际没写。这个函数去多个可能的位置找一下，
    包括任务目录、工作区根目录、以及 .my_agent 上级目录。
    """

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
