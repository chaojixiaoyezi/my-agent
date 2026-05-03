from __future__ import annotations

"""LLM: workflow planning service for subagent tasks.

给人看的解释：
这里承接工作流规划、模板选择、worker 规格实例化等逻辑。
SubAgentManager 通过 facade 方法委托到这里，不把规划逻辑塞在 mixin 里。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import SubAgentTask
    from ..capability_config import CapabilityConfig


_WORKFLOW_MODES = {"off", "plan", "auto"}
_READ_ONLY_SUBAGENT_TOOLS = ["list_files", "read_file", "search_text"]
_CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "write_file",
    "append_file",
    "replace_in_file",
]


def _normalize_workflow_mode_value(value: object) -> str:
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in _WORKFLOW_MODES:
            return mode
    return "off"


def _workflow_worker_tools(parent_tools: list[str], worker_kind: str) -> list[str]:
    if parent_tools:
        return list(parent_tools)
    if worker_kind in {"review", "verification", "planning"}:
        return list(_READ_ONLY_SUBAGENT_TOOLS)
    return list(_CODING_SUBAGENT_TOOLS)


def _merge_workflow_acceptance_checks(
    acceptance_checks: list[str],
    workflow_plan_dict: dict[str, object] | None,
) -> list[str]:
    """Merge workflow plan acceptance checks into existing checks list."""
    merged = list(acceptance_checks)
    if not workflow_plan_dict or not workflow_plan_dict.get("ok"):
        return merged
    workers = workflow_plan_dict.get("workers") or []
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            for check in worker.get("acceptance_checks") or []:
                if isinstance(check, str) and check not in merged:
                    merged.append(check)
    for check in workflow_plan_dict.get("parent_acceptance_checklist") or []:
        if isinstance(check, str) and check not in merged:
            merged.append(check)
    return merged


def _try_workflow_plan(
    goal: str,
    *,
    quality_contract: object | None = None,
    context_manifest: object | None = None,
    allowed_write_roots: list[str] | None = None,
) -> dict[str, object] | None:
    """Try to run workflow planning, return None on failure."""
    try:
        from ...subagent_workflows.planner import plan_workflow_for_goal

        result = plan_workflow_for_goal(
            goal,
            quality_contract=quality_contract,
            context_manifest=context_manifest,
            allowed_write_roots=allowed_write_roots,
        )
        return result.to_dict()
    except Exception:
        return None


def _workflow_extra_write_roots(task: "SubAgentTask") -> list[str]:
    return [item for item in task.allowed_write_roots if item and item != task.task_dir]


class SubAgentWorkflowService:
    """Workflow planning, template selection, and worker instantiation."""

    def __init__(self, manager: Any):
        self.manager = manager

    def plan_workflow(self, run_id: str, *, workflow_mode: str) -> "SubAgentTask":
        """Refresh and persist a workflow plan onto an existing parent run."""
        task = self.manager.load(run_id)
        normalized = _normalize_workflow_mode_value(workflow_mode)
        if normalized == "off":
            task.workflow_mode = "off"
            self.manager.save(task)
            return task

        plan_dict = _try_workflow_plan(
            task.goal,
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            allowed_write_roots=_workflow_extra_write_roots(task),
        ) or {}
        task.workflow_mode = normalized
        task.workflow_template_id = str(plan_dict.get("selected_template_id") or "")
        task.workflow_plan = plan_dict
        task.acceptance_checks = _merge_workflow_acceptance_checks(task.acceptance_checks, plan_dict)
        self.manager.save(task)
        return task

    def realize_workflow_plan(self, run_id: str) -> tuple["SubAgentTask", list["SubAgentTask"]]:
        """Materialize persisted workflow worker specs into child runs exactly once."""
        parent = self.manager.load(run_id)
        if parent.workflow_child_run_ids:
            children = [self.manager.load(child_id) for child_id in parent.workflow_child_run_ids if child_id]
            return parent, children

        workers = parent.workflow_plan.get("workers") or []
        if not isinstance(workers, list):
            return parent, []

        created: list["SubAgentTask"] = []
        phase_to_child_id: dict[str, str] = {}
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            phase_id = str(worker.get("phase_id") or "").strip()
            role = str(worker.get("role") or worker.get("kind") or "worker").strip() or "worker"
            kind = str(worker.get("kind") or role or "worker").strip() or "worker"
            phase_task = str(worker.get("task") or parent.goal).strip() or parent.goal
            depends_on = [str(item).strip() for item in worker.get("depends_on") or [] if str(item).strip()]
            child_plan = [
                phase_task,
                "保留命令、文件和测试证据。",
                "不要自判最终完成，等待父代理验收。",
            ]
            if depends_on:
                child_plan.append("先确认依赖 phase 已提交结果：" + ", ".join(depends_on))
            child = self.manager.create_run(
                goal=f"{parent.goal}\n\nWorkflow phase {phase_id}: {phase_task}",
                thought=(
                    f"执行 workflow phase {phase_id}（{kind}）。"
                    " 先遵守质量契约和写入边界，再提交待父代理验收的材料。"
                ),
                plan=child_plan,
                agent_name=parent.agent_name,
                role=role,
                parent_id=parent.id,
                root_id=parent.root_id,
                depth=parent.depth + 1,
                allowed_skills=list(parent.allowed_skills),
                allowed_tools=_workflow_worker_tools(parent.allowed_tools, kind),
                owner=parent.owner,
                supervisor=parent.supervisor,
                final_owner=parent.final_owner,
                acceptance_checks=[str(item) for item in worker.get("acceptance_checks") or [] if str(item).strip()],
                quality_contract=parent.quality_contract,
                context_manifest=parent.context_manifest,
                context_packs=parent.context_packs,
                extra_write_roots=_workflow_extra_write_roots(parent),
                workflow_mode="off",
            )
            child.workflow_parent_run_id = parent.id
            child.workflow_phase_id = phase_id
            child.workflow_depends_on = depends_on
            self.manager.save(child)
            created.append(child)
            if phase_id:
                phase_to_child_id[phase_id] = child.id

        for child in created:
            dep_run_ids = [phase_to_child_id[item] for item in child.workflow_depends_on if item in phase_to_child_id]
            Path(child.dependencies_json).write_text(
                json.dumps(
                    {
                        "run_id": child.id,
                        "dependencies": dep_run_ids,
                        "workflow_depends_on": child.workflow_depends_on,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        parent.workflow_child_run_ids = [child.id for child in created]
        self.manager.save(parent)
        return parent, created