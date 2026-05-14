# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""action apply service for subagent tasks.

给人看的解释：
这里承接动作执行逻辑（apply_actions, _apply_action_item 等）。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from ..models import SubAgentPlanActionsOptions
from .action_options import ActionApplyOptions
from .action_params import RecordAfterTaskActionParams
from .action_records import (
    ACTION_DISPATCH,
    ActionRecordContext,
    action_apply_summary,
    action_handler_context,
    append_action_apply_log,
    append_task_work_log,
    dry_run_action_record,
    missing_task_action_record,
    unsupported_action_record,
)
from .rescue_policy import action_rescue_record_fields

if TYPE_CHECKING:
    from ..models import ActionApplyRecord, ActionPlanItem
    from ..reports import ActionApplyRecord


# LLM: SubAgentActionService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent动作服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentActionService:
    """Action apply execution service."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: apply_actions 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 更新动作对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
    def apply_actions(
        self,
        config: Any = None,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
        include_run_ids: list[str] | None = None,
    ) -> ActionApplyReport:
        """Execute or dry-run an action plan."""
        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
            include_run_ids=include_run_ids,
        )
        records = self._apply_action_plan_records(config, opts)
        return _action_apply_report_from_records(opts, records)

    # LLM: _apply_action_plan_records executes filtered plan items and handles optional apply logging.
    # 函数用途: 根据 ActionApplyOptions 过滤 action plan，并逐条生成 apply record。
    def _apply_action_plan_records(self, config: Any, opts: ActionApplyOptions) -> list[ActionApplyRecord]:
        plan = self.manager.plan_actions(
            config,
            params=_plan_options_from_action_options(config, opts),
        )
        actions = self.manager._filter_action_plan_items(
            plan.actions,
            action_filter=opts.action_filter,
            run_id=opts.run_id,
            limit=opts.limit,
        )
        records: list[ActionApplyRecord] = []
        for action in actions:
            record = self._apply_action_item(action, options=opts)
            records.append(record)
            if opts.apply:
                self._append_action_apply_log(record)
        return records

    # LLM: _apply_action_item 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 更新动作条目对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
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
    ) -> ActionApplyRecord:
        """Execute a single action plan item."""
        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )
        now = time.time()
        try:
            task = self.manager.load(action.run_id)
        except FileNotFoundError as exc:
            return missing_task_action_record(ActionRecordContext(self.manager, action, now, opts=opts, exc=exc))

        before_status = task.status
        before_channel_status = task.channel_status
        if not opts.apply:
            return dry_run_action_record(
                ActionRecordContext(self.manager, action, now, before_status, before_channel_status, task)
            )

        handler = self._action_dispatch().get(action.action)
        if handler:
            return handler(
                self,
                action,
                task,
                action_handler_context(opts, now, before_status, before_channel_status),
            )

        return unsupported_action_record(
            ActionRecordContext(self.manager, action, now, before_status, before_channel_status, task)
        )

    # LLM: _action_dispatch 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理动作调度相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
    def _action_dispatch(self) -> dict[str, callable]:
        return ACTION_DISPATCH

    # LLM: _record_after_task_action 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入after任务动作的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def _record_after_task_action(
        self,
        params: RecordAfterTaskActionParams,
    ) -> ActionApplyRecord:
        """Create an apply record after task modification."""
        from ..reports import ActionApplyRecord

        return ActionApplyRecord(
            id=self.manager._new_id("apply"),
            action_id=params.action.id,
            run_id=params.action.run_id,
            action=params.action.action,
            dry_run=False,
            applied=True,
            ok=True,
            message=params.message,
            before_status=params.before_status,
            after_status=params.task.status,
            before_channel_status=params.before_channel_status,
            after_channel_status=params.task.channel_status,
            # LLM: apply logs preserve the rescue/escalation decision that led here.
            **action_rescue_record_fields(params.action),
            evidence_paths=params.evidence_paths or [params.task.work_log_file],
            created_at=time.time(),
        )


    # LLM: _append_action_apply_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入动作应用log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def _append_action_apply_log(self, record: ActionApplyRecord) -> None:
        """Write global action apply audit log."""
        append_action_apply_log(self.manager, record)

    # LLM: _append_task_work_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入任务worklog的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def _append_task_work_log(self, task: SubAgentTask, message: str) -> None:
        """Write apply progress to task's own WORK_LOG."""
        append_task_work_log(self.manager, task, message)


# LLM: _plan_options_from_action_options carries dispatch scoping into action planning.
# 函数用途: 让 action_apply 复用 due-check 的 root/exclude 边界，防止父级接管自己或别的任务树。
def _plan_options_from_action_options(config: Any, opts: ActionApplyOptions) -> SubAgentPlanActionsOptions:
    return SubAgentPlanActionsOptions(
        config=config,
        root_id=opts.root_id,
        exclude_run_ids=list(opts.exclude_run_ids or []),
        include_run_ids=list(opts.include_run_ids or []),
    )


# LLM: _action_apply_report_from_records keeps report assembly out of the public apply method.
# 函数用途: 根据 apply records 生成 ActionApplyReport，并统一维护 summary 字段。
def _action_apply_report_from_records(
    opts: ActionApplyOptions,
    records: list[ActionApplyRecord],
) -> ActionApplyReport:
    from ..reports import ActionApplyReport

    return ActionApplyReport(
        generated_at=time.time(),
        dry_run=not opts.apply,
        summary=action_apply_summary(records),
        records=records,
    )
