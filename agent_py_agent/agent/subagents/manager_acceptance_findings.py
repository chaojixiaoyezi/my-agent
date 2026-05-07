# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: SubAgentAcceptanceFindingMixin - thin facade delegating acceptance findings service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/acceptance_findings.py。
"""

from .services.acceptance_findings import SubAgentAcceptanceFindingService


# LLM: SubAgentAcceptanceFindingMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent验收finding混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentAcceptanceFindingMixin:
    """Thin facade delegating acceptance finding rules to SubAgentAcceptanceFindingService."""

    # LLM: _acceptance_finding_service 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理验收finding服务相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    @property
    def _acceptance_finding_service(self):
        if not hasattr(self, "__acceptance_finding_service"):
            self.__acceptance_finding_service = SubAgentAcceptanceFindingService(self)
        return self.__acceptance_finding_service

    # LLM: acceptance_findings 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理验收findings相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def acceptance_findings(self, task, output, runner, created_at):
        return self._acceptance_finding_service.acceptance_findings(task, output, runner, created_at)

    # LLM: _acceptance_findings 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理验收findings相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _acceptance_findings(self, task, output, runner, created_at):
        return self.acceptance_findings(task, output, runner, created_at)

    # LLM: _artifact_exists 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理产物exists相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _artifact_exists(self, task, raw_path: str) -> bool:
        from .services.acceptance_findings import _artifact_exists as _check
        return _check(self, task, raw_path)
