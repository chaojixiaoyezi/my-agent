# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""workflow planning service for subagent tasks.

给人看的解释：
这里承接工作流规划、模板选择、worker 规格实例化等逻辑。
SubAgentManager 通过 facade 方法委托到这里，不把规划逻辑塞在 mixin 里。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import CreateRunParams
from .hierarchy_agent_names import scheduled_child_agent_name

if TYPE_CHECKING:
    from ..capability_config import CapabilityConfig
    from ..models import SubAgentTask


_WORKFLOW_MODES = {"off", "plan", "auto"}
_CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "write_file",
    "apply_patch",
    # LLM: Workflow children inherit generic write tools, not retired session/builder tools.
    # LLM: workflow children can report durable observations without being forced into dispatch.
    "raise_observation",
    "raise_main_event",
    # LLM: workflow children can join generic collaboration cases without task-specific templates.
    "raise_collaboration_event",
    "open_case",
    "request_collaboration",
    "list_collaboration_requests",
    "submit_evidence",
    "update_collaboration_request",
    "reroute_collaboration_request",
    "update_case_status",
    "case_status",
]
_READ_ONLY_SUBAGENT_TOOLS = list(_CODING_SUBAGENT_TOOLS)


# LLM: _WorkflowAgentNameSpec adapts workflow phases to the hierarchy naming helper.
# 类用途: 给 workflow 自动生成的 phase child 提供 agent_name/role 字段，避免重复写命名规则。
@dataclass(frozen=True)
class _WorkflowAgentNameSpec:
    agent_name: str
    role: str


# LLM: _WorkflowPlanAttempt bundles route facts and parent context for one planning try.
# 类用途: 给 _try_workflow_plan 传递一个参数包，避免 workflow 规划入口继续膨胀。
@dataclass(frozen=True)
class _WorkflowPlanAttempt:
    goal: str
    explicit_template_id: str = ""
    workflow_task_type: str = ""
    workflow_risk_tags: object = None
    quality_contract: object | None = None
    context_manifest: object | None = None
    allowed_write_roots: list[str] | None = None


# LLM: _normalize_workflow_mode_value 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化工作流modevalue的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_workflow_mode_value(value: object) -> str:
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in _WORKFLOW_MODES:
            return mode
    return "off"


# LLM: _workflow_worker_tools keeps role specialization in prompts, not by removing basic tools.
# 函数用途: 为 workflow child 生成基础工具包；review/planning 也保留读写工具，避免角色变成无法产出报告或修复的空壳。
def _workflow_worker_tools(parent_tools: list[str], worker_kind: str) -> list[str]:
    if parent_tools:
        return list(parent_tools)
    return list(_CODING_SUBAGENT_TOOLS)


# LLM: _add_worker_checks 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理add工作器检查相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _add_worker_checks(merged: list[str], worker: dict[str, object]) -> None:
    """Add acceptance checks from one worker into the merged list."""
    for check in worker.get("acceptance_checks") or []:
        if isinstance(check, str) and check not in merged:
            merged.append(check)


# LLM: _merge_workflow_acceptance_checks 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 合并 workflow 的 worker 检查项；不再注入单独最终收口清单。
def _merge_workflow_acceptance_checks(
    acceptance_checks: list[str],
    workflow_plan_dict: dict[str, object] | None,
) -> list[str]:
    """Merge workflow plan acceptance checks into existing checks list."""
    merged = list(acceptance_checks)
    if not workflow_plan_dict or not workflow_plan_dict.get("ok"):
        return merged
    workers = workflow_plan_dict.get("workers") or []
    for worker in _iter_workflow_workers(workers):
        _add_worker_checks(merged, worker)
    return merged


# LLM: _iter_workflow_workers 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理迭代工作流workers相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _iter_workflow_workers(workers: object):
    if not isinstance(workers, list):
        return ()
    return (worker for worker in workers if isinstance(worker, dict))


# LLM: _try_workflow_plan 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理try工作流计划相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _try_workflow_plan(request: _WorkflowPlanAttempt) -> dict[str, object] | None:
    """Try to run workflow planning, return None on failure."""
    try:
        from ...subagent_workflows.planner import WorkflowPlanConstraints, plan_workflow_for_goal

        result = plan_workflow_for_goal(
            request.goal,
            constraints=WorkflowPlanConstraints(
                explicit_template_id=request.explicit_template_id,
                workflow_task_type=request.workflow_task_type,
                workflow_risk_tags=request.workflow_risk_tags,
                quality_contract=request.quality_contract,
                context_manifest=request.context_manifest,
                allowed_write_roots=request.allowed_write_roots,
            ),
        )
        return result.to_dict()
    except Exception:
        return None


# LLM: _workflow_extra_write_roots 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理工作流extrawriteroots相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _workflow_extra_write_roots(task: SubAgentTask) -> list[str]:
    return [item for item in task.allowed_write_roots if item and item != task.task_dir]


# LLM: _workflow_child_agent_name keeps workflow-created children in the same lineage naming system.
# 函数用途: workflow phase 自动派生的 child 也必须按“小傻妞/小小傻妞”层级命名，而不是复制 parent 名字。
def _workflow_child_agent_name(parent: SubAgentTask, phase_id: str, role: str) -> str:
    return scheduled_child_agent_name(
        parent,
        _WorkflowAgentNameSpec(
            agent_name=phase_id or role or "worker",
            role=role or "worker",
        ),
    )


# LLM: _workflow_attr reads workflow route facts from task attributes only.
# 函数用途: workflow 模板选择由 attributes 承载；不从 goal 或 prompt 里解析机器事实。
def _workflow_attr(task: SubAgentTask, key: str) -> object:
    attrs = getattr(task, "attributes", {}) or {}
    return attrs.get(key) if isinstance(attrs, dict) else None


# LLM: _workflow_attr_text returns one string workflow attribute.
# 函数用途: 读取 workflow_template_id/workflow_task_type 这类标量机器字段。
def _workflow_attr_text(task: SubAgentTask, key: str) -> str:
    return str(_workflow_attr(task, key) or "").strip()


# LLM: SubAgentWorkflowService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent工作流服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentWorkflowService:
    """Workflow planning, template selection, and worker instantiation."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: plan_workflow 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理计划工作流相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def plan_workflow(self, run_id: str, *, workflow_mode: str) -> SubAgentTask:
        """Refresh and persist a workflow plan onto an existing parent run."""
        task = self.manager.load(run_id)
        normalized = _normalize_workflow_mode_value(workflow_mode)
        if normalized == "off":
            task.workflow_mode = "off"
            self.manager.save(task)
            return task

        plan_dict = _try_workflow_plan(_WorkflowPlanAttempt(
            goal=task.goal,
            explicit_template_id=_workflow_attr_text(task, "workflow_template_id"),
            workflow_task_type=_workflow_attr_text(task, "workflow_task_type"),
            workflow_risk_tags=_workflow_attr(task, "workflow_risk_tags"),
            quality_contract=task.quality_contract,
            context_manifest=task.context_manifest,
            allowed_write_roots=_workflow_extra_write_roots(task),
        )) or {}
        task.workflow_mode = normalized
        task.workflow_template_id = str(plan_dict.get("selected_template_id") or "")
        task.workflow_plan = plan_dict
        task.acceptance_checks = _merge_workflow_acceptance_checks(task.acceptance_checks, plan_dict)
        self.manager.save(task)
        return task

    # LLM: realize_workflow_plan 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理realize工作流计划相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def realize_workflow_plan(self, run_id: str) -> tuple[SubAgentTask, list[SubAgentTask]]:
        """Materialize persisted workflow worker specs into child runs exactly once."""
        parent = self.manager.load(run_id)
        if parent.workflow_child_run_ids:
            children = [self.manager.load(child_id) for child_id in parent.workflow_child_run_ids if child_id]
            return parent, children

        workers = parent.workflow_plan.get("workers") or []
        if not isinstance(workers, list):
            return parent, []

        created, phase_to_child_id = self._create_workers(parent, workers)
        self._update_dependencies(created, phase_to_child_id)

        parent.workflow_child_run_ids = [child.id for child in created]
        self.manager.save(parent)
        return parent, created

    # LLM: _create_workers 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建workers所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def _create_workers(
        self, parent: SubAgentTask, workers: list[object]
    ) -> tuple[list[SubAgentTask], dict[str, str]]:
        """Create child tasks from worker specs and return created tasks with phase mapping."""
        created: list[SubAgentTask] = []
        phase_to_child_id: dict[str, str] = {}
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            child = self._create_single_worker(parent, worker)
            if child.workflow_phase_id:
                phase_to_child_id[child.workflow_phase_id] = child.id
            created.append(child)
        return created, phase_to_child_id

    # LLM: _create_single_worker 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建单个工作器所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def _create_single_worker(self, parent: SubAgentTask, worker: dict[str, object]) -> SubAgentTask:
        """Create a single child task from worker spec."""
        phase_id = str(worker.get("phase_id") or "").strip()
        role = str(worker.get("role") or worker.get("kind") or "worker").strip() or "worker"
        kind = str(worker.get("kind") or role or "worker").strip() or "worker"
        phase_task = str(worker.get("task") or parent.goal).strip() or parent.goal

        child_plan = [
            phase_task,
            "保留命令、文件和测试证据。",
            "完成后写清楚真实产物、证据和阻塞项，交回调用方继续推进。",
        ]

        agent_name = _workflow_child_agent_name(parent, phase_id, role)
        child = self.manager.create_run(
            params=CreateRunParams(
                goal=f"{parent.goal}\n\nWorkflow phase {phase_id}: {phase_task}",
                thought=(
                    f"执行 workflow phase {phase_id}（{kind}）。"
                    " 先遵守质量契约和写入边界，再交回真实材料和证据。"
                ),
                plan=child_plan,
                agent_name=agent_name,
                role=role,
                parent_id=parent.id,
                root_id=parent.root_id,
                depth=parent.depth + 1,
                allowed_skills=list(parent.allowed_skills),
                allowed_tools=_workflow_worker_tools(parent.allowed_tools, kind),
                owner=agent_name,
                supervisor=parent.supervisor,
                final_owner=parent.final_owner,
                acceptance_checks=[str(item) for item in worker.get("acceptance_checks") or [] if str(item).strip()],
                quality_contract=parent.quality_contract,
                context_manifest=parent.context_manifest,
                context_packs=parent.context_packs,
                extra_write_roots=_workflow_extra_write_roots(parent),
                workflow_mode="off",
            )
        )
        child.workflow_parent_run_id = parent.id
        child.workflow_phase_id = phase_id
        self.manager.save(child)
        return child

    # LLM: _update_dependencies 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 更新dependencies对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
    def _update_dependencies(
        self, created: list[SubAgentTask], phase_to_child_id: dict[str, str]
    ) -> None:
        """Write dependencies JSON for each created child task."""
        _ = phase_to_child_id
        for child in created:
            Path(child.dependencies_json).write_text(
                json.dumps(
                    {
                        "run_id": child.id,
                        "dependencies": [],
                        "note": "workflow dependencies are no longer enforced here; parent agents should dispatch dependent work explicitly.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
