# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: runner execution-context construction and persistence.

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
from .context_bundle import (
    build_context_bundle,
    render_context_bundle_markdown,
    validate_context_bundle,
)
from .controlled_exec_gateway import controlled_exec_grant_refs
from .models import SubAgentExecutionContext
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
from .runner_rendering import _render_runner_item_line, render_execution_context_markdown
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from .write_boundary_policy import task_product_write_policy, task_product_write_roots

if TYPE_CHECKING:
    from ..local_store import LocalStore

# LLM: SubAgentRunnerContextMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent执行器上下文混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentRunnerContextMixin:
    # LLM: _extract_granted_caps keeps grant scope auditable; controlled exec callers depend on these fields.
    # 函数用途: 汇总父级 grant 的技能、工具和 shell/MCP/path/network/output scope，供 runner context 和审计展示读取。
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

    # LLM: _build_write_boundary carries filesystem plus controlled exec grant refs into tool execution.
    # 函数用途: 构建写入边界和受控 exec 授权边界；工具层只能从这里读取父级授权，不能让模型自填。
    def _build_write_boundary(self, task: SubAgentTask) -> dict[str, object]:
        """Build write boundary configuration dict."""
        report_roots = _task_report_write_roots(task)
        product_roots = task_product_write_roots(task, report_roots)
        controlled_exec_grants = controlled_exec_grant_refs(list(task.capability_grants or []))
        grant_write_roots = _granted_filesystem_write_roots(task)
        return {
            "task_dir": task.task_dir,
            "role": task.role,
            "allowed_write_roots": _merge_list(
                _merge_list(task.allowed_write_roots, grant_write_roots),
                report_roots,
            ),
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
            # LLM: runners may write task-local skill candidates, not global memory.
            "skill_sparks_file": task.skill_sparks_file,
            "handoff_file": task.handoff_file,
            "debrief_file": task.debrief_file,
            "output_json": task.output_json,
            "dependencies_json": task.dependencies_json,
        }

    # LLM: build_execution_context 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建execution上下文所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
        allowed_tools = _merge_list(task.allowed_tools, granted_tools)
        return self._make_execution_context(
            task,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
            grants=grants,
            max_cards=max_cards,
        )

    # LLM: _make_execution_context 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建execution上下文所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _make_execution_context(
        self,
        task: SubAgentTask,
        *,
        params: object | None = None,
        allowed_skills: list[str],
        allowed_tools: list[str],
        grants: list[dict[str, object]],
        max_cards: int,
        ) -> SubAgentExecutionContext:
        controlled_exec_grants = controlled_exec_grant_refs(list(task.capability_grants or []))
        return SubAgentExecutionContext(
            **_execution_context_task_fields(task),
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
            granted_cards=_dedupe_granted_cards(task.capability_grants, max_cards=max_cards),
            grants=grants,
            controlled_exec_grants=controlled_exec_grants,
            acceptance_checks=task.acceptance_checks,
            evidence=[asdict(item) for item in task.evidence],
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            context_packs=task.context_packs,
            context_bundle=_execution_context_bundle(task),
            context_bundle_file=str(Path(task.task_dir) / "CONTEXT_BUNDLE.md"),
            context_bundle_json=str(Path(task.task_dir) / "context_bundle.json"),
            write_boundary=self._build_write_boundary(task),
            pending_requests=[
                asdict(item) for item in task.capability_requests if item.status == "OPEN"
            ],
            open_gaps=[asdict(item) for item in task.capability_gaps if item.status == "OPEN"],
            instructions=_execution_context_instructions(),
        )

    # LLM: write_execution_context 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入execution上下文的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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
        _write_context_bundle_files(context)
        task = self.load(run_id)
        self._append_task_work_log(
            task,
            f"execution_context: 已生成执行上下文，cards={len(context.granted_cards)}。",
        )
        self._index_execution_context(context)
        return context


# LLM: _execution_context_task_fields 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理execution上下文任务字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
        "task_dir": task.task_dir,
        "execution_context_file": task.execution_context_file,
        "execution_context_json": task.execution_context_json,
    }


_FILESYSTEM_WRITE_GRANT_TOOLS = {"write_file", "append_file", "replace_in_file"}


# LLM: grant path_scope becomes an actual filesystem write boundary only for explicit file-write grants.
# 函数用途: 把父级已批准的 write_file/append_file/replace_in_file 路径范围加入 runner 写边界；shell grant 仍走 controlled_exec，不混进普通文件写权限。
def _granted_filesystem_write_roots(task: object) -> list[str]:
    roots: list[str] = []
    for grant in getattr(task, "capability_grants", []) or []:
        tools = {str(item or "").strip() for item in getattr(grant, "tools", []) or []}
        if not tools & _FILESYSTEM_WRITE_GRANT_TOOLS:
            continue
        roots = _merge_list(roots, _string_list(getattr(grant, "path_scope", []) or []))
    return roots


# LLM: _task_report_write_roots grants runners only their internal report workspace, not product roots.
# 函数用途: 允许 coordinator/root 写 agent-run workspace 的 final_report 等内部交接文件，避免误把报告写入当越界产物。
def _task_report_write_roots(task: SubAgentTask) -> list[str]:
    roots: list[str] = []
    run_workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if run_workspace:
        roots.append(run_workspace)
    final_report = str(getattr(task, "agent_run_final_report_md", "") or "").strip()
    if final_report:
        roots.append(str(Path(final_report).parent))
    return roots


# LLM: _execution_context_bundle embeds the gate report beside the handoff facts for runner self-checks.
# 函数用途: 生成执行上下文内的 context_bundle 字典，包含 bundle 正文和 gate 结果，供 prompt 和落盘文件复用。
def _execution_context_bundle(task: SubAgentTask) -> dict[str, object]:
    bundle = build_context_bundle(task)
    gate = validate_context_bundle(bundle)
    payload = asdict(bundle)
    payload["gate"] = asdict(gate)
    payload["context_bundle_json"] = str(Path(task.task_dir) / "context_bundle.json")
    payload["context_bundle_file"] = str(Path(task.task_dir) / "CONTEXT_BUNDLE.md")
    return payload


# LLM: _write_context_bundle_files persists the handoff bundle next to existing execution context files.
# 函数用途: 写出 context_bundle.json 和 CONTEXT_BUNDLE.md；不改变任务状态，只补充可读交接物。
def _write_context_bundle_files(context: SubAgentExecutionContext) -> None:
    payload = dict(context.context_bundle or {})
    bundle_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"gate", "context_bundle_json", "context_bundle_file"}
    }
    gate_payload = payload.get("gate") or {}
    bundle_json = Path(context.context_bundle_json)
    bundle_json.parent.mkdir(parents=True, exist_ok=True)
    bundle_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bundle = build_context_bundle_from_payload(bundle_payload)
    gate = context_gate_report_from_payload(gate_payload)
    bundle_file = Path(context.context_bundle_file)
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_file.write_text(
        render_context_bundle_markdown(bundle, gate),
        encoding="utf-8",
    )
    _mirror_context_bundle_to_run_workspace(context)


# LLM: _mirror_context_bundle_to_run_workspace gives takeover/resume readers a stable task-local ref.
# 函数用途: 把 context bundle 同步到 agent run workspace；旧 task_dir 文件仍保留兼容。
def _mirror_context_bundle_to_run_workspace(context: SubAgentExecutionContext) -> None:
    refs = context.context_bundle.get("workspace_refs") if isinstance(context.context_bundle, dict) else {}
    if not isinstance(refs, dict):
        return
    run_workspace = str(refs.get("agent_run_workspace") or "").strip()
    if not run_workspace:
        return
    target_dir = Path(run_workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    json_target = target_dir / "context_bundle.json"
    md_target = target_dir / "CONTEXT_BUNDLE.md"
    json_target.write_text(Path(context.context_bundle_json).read_text(encoding="utf-8"), encoding="utf-8")
    md_target.write_text(Path(context.context_bundle_file).read_text(encoding="utf-8"), encoding="utf-8")


# LLM: build_context_bundle_from_payload keeps Markdown rendering decoupled from dataclass serialization.
# 函数用途: 从已序列化字典恢复 ContextBundleV1；只用于同进程落盘渲染。
def build_context_bundle_from_payload(payload: object):
    from .context_bundle import ContextBundleV1

    if not isinstance(payload, dict):
        payload = {}
    return ContextBundleV1(**payload)


# LLM: context_gate_report_from_payload keeps gate Markdown rendering tolerant of missing future fields.
# 函数用途: 从 gate 字典恢复 ContextGateReport；只用于 context bundle 文件渲染。
def context_gate_report_from_payload(payload: object):
    from .context_bundle import ContextGateReport

    if not isinstance(payload, dict):
        payload = {}
    return ContextGateReport(**payload)
