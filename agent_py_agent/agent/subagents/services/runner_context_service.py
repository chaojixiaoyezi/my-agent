"""Runner execution-context construction and persistence service."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ...common.audit_activation import (
    audit_worker_tool_scope,
    structured_audit_supervised_worker_attributes,
)
from ...common.value_parsing import text_or_sequence_strings
from ...model_visible_refs import clean_path_contract_refs, is_non_model_visible_locator_root
from ..context_bundle_refs import workspace_refs
from ..controlled_exec_gateway import controlled_exec_grant_refs
from ..manager_collaboration_context import collaboration_context_payload
from ..model_capabilities import capability_request_counts_as_open
from ..models import SubAgentExecutionContext, SubAgentTask
from ..policies import (
    _dedupe_granted_cards,
    _execution_context_instructions,
)
from ..role_templates import is_self_authorized_root_task, role_template_snapshot_for_task
from ..runner_context_bundle_files import execution_context_bundle, write_context_bundle_files
from ..runner_rendering import render_execution_context_markdown
from ..utils import (
    _apply_missing_paths,
    _merge_list,
)
from .output_alignment import declared_output_write_roots, output_write_grant_roots


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
        task_workspace_refs = workspace_refs(task)
        report_roots = _task_report_write_roots(task)
        product_roots = task_product_write_roots(task, report_roots)
        controlled_exec_grants = controlled_exec_grant_refs(list(task.capability_grants or []))
        grant_write_roots = _granted_filesystem_write_roots(task)
        # P3-2:grant 的 path_scope 同时开放读取(中途求读权限的 capreq 闭环)。
        exact_source_read_scope = structured_audit_supervised_worker_attributes(
            getattr(task, "attributes", None)
        )
        read_roots = _merge_list(
            _task_required_read_roots(task),
            _granted_filesystem_read_roots(task),
        )
        if exact_source_read_scope:
            read_roots = _merge_list(read_roots, [_model_task_dir(task)])
        # 任务交付区（tasks/<日期>/<任务>/output）必须可写：子代理直接把声明产物写到
        # 交付区，用户拿走即可（见 output_alignment）。显式并入，不依赖 task_workspace_dir
        # 是否被 locator 过滤，也修 R4 那种"主代理只给 output 子目录、没给交付区根"的形态。
        # R8 接力实锤补强：delivery_root 的环境字段缺失时（创建链未透传 run_workspace
        # 等），声明产物目录过围栏（与 capability grant 同款基准）后直接授权——
        # 声明驱动的对偶，子代理写自己声明的交付位置不再被自家边界拦。
        fence_roots = [
            str(getattr(task, "task_workspace_dir", "") or ""),
            str(getattr(task, "task_dir", "") or ""),
            str(getattr(self.manager, "workspace_root", "") or ""),
            str(getattr(self.manager, "workspace", "") or ""),
        ]
        delivery_grant_roots = [
            *output_write_grant_roots(task),
            *declared_output_write_roots(task, fence_roots),
        ]
        allowed_write_roots = _model_allowed_write_roots(task, [*grant_write_roots, *delivery_grant_roots], report_roots)
        return {
            "task_dir": _model_task_dir(task),
            # 结构化 cwd 事实，不是授权：只读工具可把 workspace/... 稳定解析到
            # 当前 owner 的长期工作区；写权限仍完全由 allowed_write_roots 决定。
            "owner_workspace_dir": task_workspace_refs.get("owner_workspace_dir", ""),
            "role": task.role,
            "allowed_write_roots": allowed_write_roots,
            "allowed_read_roots": read_roots,
            # LLM: Exact modes are generic Tool Gateway controls activated only
            # by a fully typed source-worker identity, never by role/name text.
            # 函数用途: 来源工作者只读显式文件根和自己 run 的工具输出，普通子代理语义不变。
            "read_scope_mode": "exact" if exact_source_read_scope else "owner",
            "artifact_read_scope_mode": (
                "current_run" if exact_source_read_scope else "task"
            ),
            # Large tool results are archived under the stable per-agent run
            # workspace, not under the shared task work directory.  Keep the
            # recovery handle anchored to that same typed workspace so a later
            # runner attempt of this agent can read its own earlier output
            # without exposing sibling-agent artifacts.
            "artifact_read_root": task_workspace_refs.get("agent_work_dir", ""),
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

        task = self.manager.load(run_id)
        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
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
        collaboration = collaboration_context_payload(self.manager, task)
        if collaboration:
            context_bundle["collaboration"] = collaboration
        _attach_runtime_guidance(context_bundle, _runtime_guidance_context(self.manager, task.id))
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
            context_bundle_file=str(Path(_model_task_dir(task)) / "CONTEXT_BUNDLE.md"),
            context_bundle_json=str(Path(_model_task_dir(task)) / "context_bundle.json"),
            role_template=role_template_snapshot_for_task(task),
            write_boundary=self._build_write_boundary(task),
            pending_requests=[
                asdict(item)
                for item in task.capability_requests
                if capability_request_counts_as_open(getattr(item, "status", "OPEN"))
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

        task = self.manager.load(run_id)
        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        self.manager.save(task)
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
        task = self.manager.load(run_id)
        self.manager.actions._append_task_work_log(
            task,
            f"execution_context: 已生成执行上下文，cards={len(context.granted_cards)}。",
        )
        self.manager.indexing.index_execution_context(context)
        return context


def _execution_context_task_fields(task: SubAgentTask) -> dict[str, object]:
    task_dir = _model_task_dir(task)
    return {
        "run_id": task.id,
        "generated_at": time.time(),
        "goal": task.goal,
        "thought": task.thought,
        "plan": task.plan,
        "agent_name": task.agent_name,
        "role": task.role,
        "status": task.status,
        "turn_end_reason": str(getattr(task, "turn_end_reason", "") or ""),
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
        "task_dir": task_dir,
        "execution_context_file": task.execution_context_file,
        "execution_context_json": task.execution_context_json,
    }


def _model_task_dir(task: SubAgentTask) -> str:
    return str(getattr(task, "agent_run_workspace_dir", "") or getattr(task, "task_dir", "") or "").strip()


def _model_allowed_write_roots(
    task: SubAgentTask,
    grant_write_roots: list[str],
    report_roots: list[str],
) -> list[str]:
    canonical_roots = [
        getattr(task, "task_workspace_dir", ""),
        getattr(task, "agent_run_workspace_dir", ""),
    ]
    raw_roots = [*canonical_roots, *list(task.allowed_write_roots or []), *grant_write_roots, *report_roots]
    roots: list[str] = []
    for root in clean_path_contract_refs(raw_roots):
        if is_non_model_visible_locator_root(task, root):
            continue
        if root not in roots:
            roots.append(root)
    return roots


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


# LLM: 任务完成力底座 P3-2(R5a 实锤:子代理读不到分析材料、grant 后读边界也不扩,
#   "扩展 allowed_read_roots"走投无路)。规则:capability grant 的 path_scope 一律
#   并入读根——grant 是父代理显式授权的路径,读取是其中的最低权限(能写必能读),
#   不按工具类型过滤。目录条目天然授权整个子树(执行端 workspace_roots 子树语义)。
# 函数用途: 把父代理 grant 过的路径范围开放给子代理"读",中途求读权限从此有用。
def _granted_filesystem_read_roots(task: object) -> list[str]:
    roots: list[str] = []
    for grant in getattr(task, "capability_grants", []) or []:
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
    if exact_scope := audit_worker_tool_scope(
        getattr(task, "attributes", None)
    ):
        return list(exact_scope)
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


def task_product_write_roots(task: SubAgentTask, report_roots: list[str]) -> list[str]:
    task_dir = _resolved_path_text(task.task_dir)
    report_root_set = {_resolved_path_text(item) for item in report_roots}
    roots: list[str] = []
    for raw in task.allowed_write_roots:
        text = str(raw or "").strip()
        if not text:
            continue
        resolved = _resolved_path_text(text)
        if resolved == task_dir or resolved in report_root_set:
            continue
        if text not in roots:
            roots.append(text)
    if not roots:
        roots = _task_workspace_fallback_roots(task)
    return roots


# LLM: 子代理产物写区兜底(batch3 C3/G4 实锤:子代理 allowed_write_roots 只含
#   自己的 agent 目录〔没声明 output_files,declared_output_write_roots 没生效〕,
#   过滤掉自己目录后 product_write_roots 为空 → tool_preflight 报 missing_allowed_
#   write_roots → 子代理写不了产物 → BLOCKED → 主代理空等未收口)。my-agent 的
#   "必须先声明产物落点才有写区"是对模型的过度约束(对照组子代理直接写工作区)。
#   兜底:product_write_roots 为空时回退到任务工作区的 output/work——子代理总能
#   写产物(交付事实优先),且围栏在本任务工作区内(非任意位置),安全。
# 函数用途: 子代理没有任何声明产物写区时,给它任务工作区的 output 和 work 兜底。
def _task_workspace_fallback_roots(task: SubAgentTask) -> list[str]:
    workspace = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if not workspace:
        return []
    base = Path(workspace)
    return [str(base / "output"), str(base / "work")]


def task_product_write_policy(task: SubAgentTask, product_roots: list[str]) -> str:
    policy = str((getattr(task, "attributes", None) or {}).get("product_write_policy") or "").strip().lower()
    return policy if policy in {"direct", "delegate"} else "direct"


def _resolved_path_text(path: str | Path) -> str:
    return str(Path(str(path)).expanduser().resolve(strict=False))
