from __future__ import annotations

"""LLM: helper functions for building acceptance review findings.

给人看的解释：
这些函数各自负责构建一组验收检查项，避免单个方法过长。
主 mixin 只需要调用这些函数并拼接结果。
"""

from collections.abc import Callable
from pathlib import Path

from .models import SubAgentTask
from .parsing import _dict_list, _string_list
from .reports import AcceptanceReviewFinding


def _build_readiness_findings(
    task: SubAgentTask,
    runner: dict[str, object],
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """LLM: build readiness, channel, and structured-output findings.

    新手说明:
    检查任务是否处于等待验收状态、通道是否正常、runner 结构化输出是否可解析。
    """

    findings: list[AcceptanceReviewFinding] = []

    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    findings.append(
        AcceptanceReviewFinding(
            name="ready_for_acceptance",
            ok=ready,
            severity="P1",
            message=(
                "任务处于等待验收状态。"
                if ready
                else f"任务未处于等待验收状态: status={task.status} verify={task.verification_status}"
            ),
            evidence_path=task.runner_result_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="channel_not_broken",
            ok=task.channel_status != "BROKEN",
            severity="P1",
            message=(
                "通道未标记为 BROKEN。"
                if task.channel_status != "BROKEN"
                else "通道为 BROKEN，不能验收。"
            ),
            evidence_path=task.channel_probe_file,
            created_at=created_at,
        )
    )

    runner_structured_found = bool(runner.get("structured_output_found", False))
    runner_structured_ok = bool(runner.get("structured_output_ok", False))
    findings.append(
        AcceptanceReviewFinding(
            name="structured_output",
            ok=(not runner_structured_found) or runner_structured_ok,
            severity="P1",
            message=(
                "runner 结构化输出可解析。"
                if runner_structured_found and runner_structured_ok
                else "runner 未记录结构化输出，按人工证据验收。"
                if not runner_structured_found
                else f"runner 结构化输出解析失败: {runner.get('structured_parse_error', '')}"
            ),
            evidence_path=task.runner_result_json,
            created_at=created_at,
        )
    )
    return findings


def _build_evidence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """LLM: build evidence presence and tool-requirement findings.

    新手说明:
    检查是否有可用验收证据、是否有失败证据，以及 acceptance_checks 要求的
    read_file / write_file 工具是否已有对应证据。
    """

    findings: list[AcceptanceReviewFinding] = []

    ok_evidence = [item for item in task.evidence if item.ok]
    bad_evidence = [item for item in task.evidence if not item.ok]
    findings.append(
        AcceptanceReviewFinding(
            name="evidence_present",
            ok=bool(ok_evidence),
            severity="P0",
            message=(
                f"已有 {len(ok_evidence)} 条可用验收证据。"
                if ok_evidence
                else "缺少可用验收证据。"
            ),
            evidence_path=task.acceptance_file,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="evidence_not_failed",
            ok=not bad_evidence,
            severity="P1",
            message=(
                "没有失败验收证据。"
                if not bad_evidence
                else f"存在 {len(bad_evidence)} 条失败证据。"
            ),
            evidence_path=task.acceptance_file,
            created_at=created_at,
        )
    )

    acceptance_text = "；".join(task.acceptance_checks).lower()
    if "read_file" in acceptance_text:
        has_read = "read_file" in task.used_tools and any(
            item.ok and (
                item.kind in {"read_file", "file_read", "file_content"}
                or "read_file" in item.command.lower()
                or "read_file" in item.summary.lower()
            )
            for item in task.evidence
        )
        findings.append(
            AcceptanceReviewFinding(
                name="acceptance_requires_read_file",
                ok=has_read,
                severity="P0",
                message=(
                    "acceptance_checks 要求 read_file，且已有对应工具和证据。"
                    if has_read
                    else "acceptance_checks 要求 read_file，但缺少对应工具执行或证据。"
                ),
                evidence_path=task.acceptance_file,
                created_at=created_at,
            )
        )
    if "write_file" in acceptance_text:
        has_write = "write_file" in task.used_tools and any(
            item.ok and (
                item.kind in {"write_file", "file_write", "file_written"}
                or "write_file" in item.command.lower()
                or "write_file" in item.summary.lower()
                or "写入" in item.summary
            )
            for item in task.evidence
        )
        findings.append(
            AcceptanceReviewFinding(
                name="acceptance_requires_write_file",
                ok=has_write,
                severity="P0",
                message=(
                    "acceptance_checks 要求 write_file，且已有对应工具和证据。"
                    if has_write
                    else "acceptance_checks 要求 write_file，但缺少对应工具执行或证据。"
                ),
                evidence_path=task.acceptance_file,
                created_at=created_at,
            )
        )
    return findings


def _build_output_and_capability_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
    artifact_exists_fn: Callable[[str], bool],
) -> list[AcceptanceReviewFinding]:
    """LLM: build capability-gap, blocker, test, artifact, and patch findings.

    新手说明:
    检查是否还有未关闭的能力请求/缺口、output.json 里的 blocker 和测试失败、
    artifact 路径是否真实存在、以及 patch 的状态和审核情况。
    """

    findings: list[AcceptanceReviewFinding] = []

    open_requests = [item for item in task.capability_requests if item.status == "OPEN"]
    open_gaps = [item for item in task.capability_gaps if item.status == "OPEN"]
    findings.append(
        AcceptanceReviewFinding(
            name="no_open_capability_requests",
            ok=not open_requests,
            severity="P1",
            message=(
                "没有待处理 capability request。"
                if not open_requests
                else f"仍有 {len(open_requests)} 条 OPEN capability request。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="no_open_capability_gaps",
            ok=not open_gaps,
            severity="P1",
            message=(
                "没有待处理 capability gap。"
                if not open_gaps
                else f"仍有 {len(open_gaps)} 条 OPEN capability gap。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )

    blockers = [item for item in _string_list(output.get("blockers", [])) if item.strip()]
    findings.append(
        AcceptanceReviewFinding(
            name="no_output_blockers",
            ok=not blockers,
            severity="P1",
            message="output.json 没有 blocker。" if not blockers else f"output.json 仍有 blocker: {blockers[0]}",
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )

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

    patches = _dict_list(output.get("patches", []))
    valid_patch_statuses = {"applied", "planned", "blocked"}
    unresolved_patches = [
        item for item in patches if str(item.get("status", "")).lower() in {"planned", "blocked"}
    ]
    invalid_patches = [
        item
        for item in patches
        if str(item.get("status", "")).lower() not in valid_patch_statuses
    ]
    unreviewed_applied_patches = [
        item
        for item in patches
        if str(item.get("status", "")).lower() == "applied"
        and str(item.get("review_status", "")).upper() != "APPROVED"
    ]
    findings.append(
        AcceptanceReviewFinding(
            name="no_unresolved_patches",
            ok=not unresolved_patches,
            severity="P1",
            message=(
                "没有未处理 patch。"
                if not unresolved_patches
                else f"仍有 {len(unresolved_patches)} 个 patch 处于 planned/blocked。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="patch_status_valid",
            ok=not invalid_patches,
            severity="P1",
            message=(
                "patch 状态均符合协议。"
                if not invalid_patches
                else f"存在 {len(invalid_patches)} 个未知 patch 状态。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="patches_reviewed",
            ok=not unreviewed_applied_patches,
            severity="P1",
            message=(
                "所有 applied patch 已审核。"
                if not unreviewed_applied_patches
                else f"仍有 {len(unreviewed_applied_patches)} 个 applied patch 未通过审核。"
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
