"""Runner execution-context construction and persistence service."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ....common.value_parsing import text_or_sequence_strings
from ...controlled_exec_gateway import controlled_exec_grant_refs
from ...manager_collaboration_context import collaboration_context_payload
from ...models import SubAgentExecutionContext, SubAgentTask
from ...policies import (
    _dedupe_granted_cards,
    _execution_context_instructions,
)
from ...role_templates import is_self_authorized_root_task, role_template_snapshot_for_task
from ...runner_context_bundle_files import execution_context_bundle, write_context_bundle_files
from ...runner_rendering import render_execution_context_markdown
from ...utils import (
    _apply_missing_paths,
    _merge_list,
)
from ...write_boundary_policy import task_product_write_policy, task_product_write_roots


@dataclass(frozen=True)
class ExecutionContextBuildRequest:
    task: object
    allowed_skills: list[str]
    allowed_tools: list[str]
    grants: list[dict[str, object]]
    max_cards: int


class SubAgentRunnerContextService:
    """Build and persist the execution context for one subagent runner."""

    def __init__(self, manager):
        self.manager = manager

    def __getattr__(self, name: str):
        return getattr(self.manager, name)

    def _extract_granted_caps(self, task: SubAgentTask) -> tuple[list[str], list[str], list[dict[str, object]]]:
        """Extract skills, tools, and grants from capability grants."""
        granted_skills: list[str] = []
        granted_tools: list[str] = []
        grants: list[dict[str, object]] = []
        for grant in task.capability_grants:
            granted_skills = _merge_list(granted_skills, grant.skills)
            granted_tools = _merge_list(granted_tools, grant.tools)
            grants.append(
                {
                    "id": grant.id,
                    "request_id": grant.request_id,
                    "grant_type": grant.grant_type,
                    "skills": grant.skills,
                    "tools": grant.tools,
                    "mcp_tools": grant.mcp_tools,
                    "command_allowlist": grant.command_allowlist,
                    "reason": grant.reason,
                    "constraints": grant.constraints,
                    "path_scope": grant.path_scope,
                    "network_scope": grant.network_scope,
                    "output_budget": grant.output_budget,
                    "risk_level": grant.risk_level,
                    "expires_after_task": grant.expires_after_task,
                    "expires_at": grant.expires_at,
                    "created_at": grant.created_at,
                }
            )
        return granted_skills, granted_tools, grants

    def _build_write_boundary(self, task: SubAgentTask) -> dict[str, object]:
        """Build write boundary configuration dict."""
        report_roots = _task_report_write_roots(task)
        product_roots = task_product_write_roots(task, report_roots)
        controlled_exec_grants = controlled_exec_grant_refs(list(task.capability_grants or []))
        grant_write_roots = _granted_filesystem_write_roots(task)
        read_roots = _task_required_read_roots(task)
        return {
            "task_dir": task.task_dir,
            "role": task.role,
            "allowed_write_roots": _merge_list(
                _merge_list(task.allowed_write_roots, grant_write_roots),
                report_roots,
            ),
            "allowed_read_roots": read_roots,
            "product_write_roots": product_roots,
            "product_write_policy": task_product_write_policy(task, product_roots),
            "forbidden_write_roots": task.forbidden_write_roots,
            "locked_files": task.locked_files,
            "status_file": task.status_file,
            "work_log_file": task.work_log_file,
            "action_receipts_file": task.action_receipts_file,
            "acceptance_file": task.acceptance_file,
            "test_checklist_file": task.test_checklist_file,
            "bugs_file": task.bugs_file,
            "skill_usage_file": task.skill_usage_file,
            "controlled_exec_grants": controlled_exec_grants,
            "effective_permissions": dict(getattr(task, "effective_permissions", {}) or {}),
            "shell_access_mode": str(
                (getattr(task, "effective_permissions", {}) or {}).get("shell_access_mode")
                or "workspace-write"
            ),
            "skill_sparks_file": task.skill_sparks_file,
            "handoff_file": task.handoff_file,
            "debrief_file": task.debrief_file,
            "output_json": task.output_json,
            "dependencies_json": task.dependencies_json,
        }

    def build_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        """生成单个子代理执行器可读取的最小上下文。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        granted_skills, granted_tools, grants = self._extract_granted_caps(task)
        allowed_skills = _merge_list(task.allowed_skills, granted_skills)
        allowed_tools = _runner_allowed_tools(task, _merge_list(task.allowed_tools, granted_tools))
        return self._make_execution_context(
            ExecutionContextBuildRequest(
                task=task,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                grants=grants,
                max_cards=max_cards,
            )
        )

    def _make_execution_context(self, request: ExecutionContextBuildRequest) -> SubAgentExecutionContext:
        task = request.task
        controlled_exec_grants = controlled_exec_grant_refs(list(task.capability_grants or []))
        context_bundle = execution_context_bundle(task)
        collaboration = collaboration_context_payload(self, task)
        if collaboration:
            context_bundle["collaboration"] = collaboration
        _attach_runtime_guidance(context_bundle, _runtime_guidance_context(self, task.id))
        return SubAgentExecutionContext(
            **_execution_context_task_fields(task),
            allowed_skills=request.allowed_skills,
            allowed_tools=request.allowed_tools,
            effective_permissions=dict(getattr(task, "effective_permissions", {}) or {}),
            granted_cards=_dedupe_granted_cards(task.capability_grants, max_cards=request.max_cards),
            grants=request.grants,
            controlled_exec_grants=controlled_exec_grants,
            acceptance_checks=task.acceptance_checks,
            evidence=[asdict(item) for item in task.evidence],
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            context_packs=task.context_packs,
            context_bundle=context_bundle,
            context_bundle_file=str(Path(task.task_dir) / "CONTEXT_BUNDLE.md"),
            context_bundle_json=str(Path(task.task_dir) / "context_bundle.json"),
            role_template=role_template_snapshot_for_task(task),
            write_boundary=self._build_write_boundary(task),
            pending_requests=[
                asdict(item) for item in task.capability_requests if item.status == "OPEN"
            ],
            open_gaps=[asdict(item) for item in task.capability_gaps if item.status == "OPEN"],
            instructions=_execution_context_instructions(),
        )

    def write_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        """写出子代理执行上下文 JSON 和 Markdown。"""

        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        context = self.build_execution_context(run_id, max_cards=max_cards)
        Path(context.execution_context_json).write_text(
            json.dumps(asdict(context), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(context.execution_context_file).write_text(
            render_execution_context_markdown(context),
            encoding="utf-8",
        )
        write_context_bundle_files(context)
        task = self.load(run_id)
        self._append_task_work_log(
            task,
            f"execution_context: 已生成执行上下文，cards={len(context.granted_cards)}。",
        )
        self._index_execution_context(context)
        return context


def _execution_context_task_fields(task: SubAgentTask) -> dict[str, object]:
    return {
        "run_id": task.id,
        "generated_at": time.time(),
        "goal": task.goal,
        "thought": task.thought,
        "plan": task.plan,
        "agent_name": task.agent_name,
        "role": task.role,
        "status": task.status,
        "verification_status": task.verification_status,
        "channel_status": task.channel_status,
        "runner_attempts": task.runner_attempts,
        "runner_last_error": task.runner_last_error,
        "owner": task.owner,
        "supervisor": task.supervisor,
        "final_owner": task.final_owner,
        "parent_id": task.parent_id,
        "root_id": task.root_id,
        "depth": task.depth,
        "subagent_session_id": _task_text_field(task, "subagent_session_id"),
        "agent_thread_id": _task_text_field(task, "agent_thread_id"),
        "parent_subagent_session_id": _task_text_field(task, "parent_subagent_session_id"),
        "root_subagent_session_id": _task_text_field(task, "root_subagent_session_id"),
        "task_dir": task.task_dir,
        "execution_context_file": task.execution_context_file,
        "execution_context_json": task.execution_context_json,
    }


def _task_text_field(task: object, name: str) -> str:
    value = getattr(task, name, "")
    return value if isinstance(value, str) else ""


_FILESYSTEM_WRITE_GRANT_TOOLS = {"write_file", "apply_patch"}


def _granted_filesystem_write_roots(task: object) -> list[str]:
    roots: list[str] = []
    for grant in getattr(task, "capability_grants", []) or []:
        tools = {str(item or "").strip() for item in getattr(grant, "tools", []) or []}
        if not tools & _FILESYSTEM_WRITE_GRANT_TOOLS:
            continue
        roots = _merge_list(roots, text_or_sequence_strings(getattr(grant, "path_scope", []) or []))
    return roots


def _runtime_guidance_context(manager: object, run_id: str) -> list[dict[str, object]]:
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return []
    entries = store.pending_guidance("agent_run", run_id, limit=20)
    if not entries:
        return []
    store.mark_guidance_delivered([entry.guidance_id for entry in entries])
    return [entry.to_dict() for entry in entries]


def _attach_runtime_guidance(bundle: dict[str, object], guidance: list[dict[str, object]]) -> None:
    if guidance:
        bundle["runtime_guidance"] = guidance


def _runner_allowed_tools(task: SubAgentTask, tools: list[str]) -> list[str]:
    disabled = {
        str(item or "").strip()
        for item in (getattr(task, "effective_permissions", {}) or {}).get("disabled_tools", [])
        if str(item or "").strip()
    }
    filtered = [item for item in tools if str(item or "").strip() not in disabled]
    if not is_self_authorized_root_task(task):
        return filtered
    return [item for item in filtered if item != "capability_request"]


# 让子代理能读自己的输入文件，同时不扩大写入权限。
def _task_required_read_roots(task: object) -> list[str]:
    manifest = getattr(task, "context_manifest", None)
    if isinstance(manifest, dict):
        raw = manifest.get("required_read_paths")
        hint_raw = manifest.get("hint_read_paths")
    else:
        raw = getattr(manifest, "required_read_paths", None)
        hint_raw = getattr(manifest, "hint_read_paths", None)
    roots: list[str] = []
    for item in [*text_or_sequence_strings(raw), *text_or_sequence_strings(hint_raw)]:
        text = str(item or "").strip()
        if not text or "://" in text:
            continue
        path = Path(text).expanduser()
        roots.append(str(path if path.is_absolute() else path))
    return _merge_list([], roots)


def _task_report_write_roots(task: SubAgentTask) -> list[str]:
    roots: list[str] = []
    run_workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if run_workspace:
        roots.append(run_workspace)
    final_report = str(getattr(task, "agent_run_final_report_md", "") or "").strip()
    if final_report:
        roots.append(str(Path(final_report).parent))
    return roots
