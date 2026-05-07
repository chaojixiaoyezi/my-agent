# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: SubAgentWorkflowMixin - thin facade delegating workflow service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/workflow.py。
"""

from .services.workflow import SubAgentWorkflowService


# LLM: SubAgentWorkflowMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent工作流混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentWorkflowMixin:
    """Thin facade delegating workflow planning and realization to SubAgentWorkflowService."""

    # LLM: _workflow_service 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理工作流服务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    @property
    def _workflow_service(self):
        if not hasattr(self, "__workflow_service"):
            self.__workflow_service = SubAgentWorkflowService(self)
        return self.__workflow_service

    # LLM: ensure_workflow_plan 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 校验工作流计划需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def ensure_workflow_plan(self, run_id: str, *, workflow_mode: str):
        """Ensure a workflow plan is persisted for the given run."""
        return self._workflow_service.plan_workflow(run_id, workflow_mode=workflow_mode)

    # LLM: realize_workflow_plan 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理realize工作流计划相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def realize_workflow_plan(self, run_id: str):
        """Materialize persisted workflow worker specs into child runs."""
        return self._workflow_service.realize_workflow_plan(run_id)
