# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: SubAgentActionMixin - thin facade delegating action service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/actions.py。
"""

from .models import SubAgentTask
from .reports import ActionApplyRecord, ActionPlanItem
from .services.action_options import ActionApplyOptions
from .services.action_params import RecordAfterTaskActionParams
from .services.actions import SubAgentActionService
from .services.indexing_params import IndexReportParams


# LLM: SubAgentActionMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent动作混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentActionMixin:
    """Thin facade delegating action apply execution to SubAgentActionService."""

    # LLM: _action_service 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理动作服务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    @property
    def _action_service(self):
        if not hasattr(self, "__action_service"):
            self.__action_service = SubAgentActionService(self)
        return self.__action_service

    # LLM: _apply_action_item 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新动作条目对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def _apply_action_item(
        self,
        action: ActionPlanItem,
        *,
        options: ActionApplyOptions | None = None,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ):
        return self._action_service._apply_action_item(
            action,
            options=options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )

    # LLM: _record_after_task_action 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入after任务动作的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _record_after_task_action(
        self,
        params: RecordAfterTaskActionParams,
    ):
        return self._action_service._record_after_task_action(params)

    # LLM: _append_action_apply_log 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入动作应用log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _append_action_apply_log(self, record: ActionApplyRecord):
        return self._action_service._append_action_apply_log(record)

    # LLM: _append_task_work_log 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入任务worklog的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _append_task_work_log(self, task: SubAgentTask, message: str):
        return self._action_service._append_task_work_log(task, message)

    # LLM: apply_actions 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新动作对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def apply_actions(
        self,
        config=None,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ):
        return self._action_service.apply_actions(
            config,
            ActionApplyOptions.from_values(
                options,
                apply=apply,
                action_filter=action_filter,
                run_id=run_id,
                take_over_by=take_over_by,
                locked_files=locked_files,
                limit=limit,
            ),
        )

    # LLM: write_action_apply_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入动作应用报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def write_action_apply_report(
        self,
        config=None,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ):
        import json
        from dataclasses import asdict
        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )
        report = self.apply_actions(config, opts)
        (self.workspace / "subagent_action_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_action_apply_markdown
        (self.workspace / "SUBAGENT_ACTION_APPLY.md").write_text(
            render_action_apply_markdown(report), encoding="utf-8",
        )
        self._index_report(
            IndexReportParams(
                "subagent_action_apply_report", "latest",
                "Subagent action apply report", report,
                "subagent_action_apply_report_written",
            ),
        )
        return report
