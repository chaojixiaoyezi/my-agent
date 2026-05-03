from __future__ import annotations

"""LLM contract: acceptance finding rules and artifact existence checks.

Human version:
这个 mixin 只放一类 SubAgentManager 能力。它不单独实例化，
由 public SubAgentManager 组合使用，避免单个文件重新长成大杂烩。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from .models import SubAgentTask
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _runner_next_action,
    _select_capability_hits,
    _severity_weight,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .reports import AcceptanceReviewFinding
from .runner_rendering import _render_runner_item_line
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentAcceptanceFindingMixin:
    def acceptance_findings(
        self,
        task: SubAgentTask,
        output: dict[str, object],
        runner: dict[str, object],
        created_at: float,
    ) -> list[AcceptanceReviewFinding]:
        """生成验收检查项。"""

        findings: list[AcceptanceReviewFinding] = []
        validation = self.validate_work_order(task.id)
        findings.append(
            AcceptanceReviewFinding(
                name="work_order",
                ok=validation.ok,
                severity="P0",
                message="工单现场完整。" if validation.ok else f"工单缺少 {len(validation.missing)} 个关键路径。",
                evidence_path=task.task_dir,
                created_at=created_at,
            )
        )
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
            and not self._artifact_exists(task, str(item.get("path", "") or ""))
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

    def _artifact_exists(self, task: SubAgentTask, raw_path: str) -> bool:
        """核对 runner 声称的本地 artifact 是否真实存在。

        artifact path 可能是相对任务目录，也可能是相对工作区根目录。当前
        `workspace_root` 场景下 subagent workspace 通常是
        `<root>/.my_agent/subagents`，所以也会尝试回到 `<root>` 查找。
        """

        text = raw_path.strip()
        if not text or "://" in text:
            return True
        path = Path(text)
        candidates = [path] if path.is_absolute() else []
        if not path.is_absolute():
            candidates.extend(
                [
                    Path(task.task_dir) / path,
                    self.workspace / path,
                    self.workspace.parent / path,
                ]
            )
            if self.workspace.name == "subagents" and self.workspace.parent.name == ".my_agent":
                candidates.append(self.workspace.parent.parent / path)
        return any(candidate.exists() for candidate in candidates)

    # Backward-compatible alias for internal callers
    def _acceptance_findings(self, task, output, runner, created_at):
        return self.acceptance_findings(task, output, runner, created_at)
