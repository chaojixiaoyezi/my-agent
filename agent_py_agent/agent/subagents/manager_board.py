# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: SubAgentBoardMixin - thin facade delegating board service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/board.py。
"""

from .models import SubAgentBoardOptions, SubAgentDueCheckOptions, SubAgentPlanActionsOptions
from .services.board import SubAgentBoardService
from .services.board_items import build_risk_flags, to_board_item


# LLM: SubAgentBoardMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent看板混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentBoardMixin:
    """Thin facade delegating board, due-check, and action planning to SubAgentBoardService."""

    # LLM: _board_service 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理看板服务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    @property
    def _board_service(self):
        if not hasattr(self, "__board_service"):
            self.__board_service = SubAgentBoardService(self)
        return self.__board_service

    # LLM: _to_board_item 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 转换看板条目的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _to_board_item(self, task):
        return to_board_item(self, task)

    # LLM: _risk_flags 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理riskflags相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return build_risk_flags(task, open_request_count, open_gap_count)

    # LLM: build_board 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 构建看板所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ):
        board_options = _board_options(options, recent_limit=recent_limit)
        return self._board_service.build_board(options=board_options)

    # LLM: write_board 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入看板的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def write_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ):
        board_options = _board_options(options, recent_limit=recent_limit)
        board = self.build_board(options=board_options)
        import json
        from dataclasses import asdict
        (self.workspace / "subagent_board.json").write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_board_markdown
        (self.workspace / "SUBAGENT_BOARD.md").write_text(
            render_board_markdown(board), encoding="utf-8",
        )
        return board

    # LLM: due_check must preserve optional root_id scoping for noisy shared subagent workspaces.
    # 函数用途: 生成到期检查报告；可按 root_id 限定一棵任务树，避免其他测试树的问题混进当前汇报。
    def due_check(self, config=None, *, params: SubAgentDueCheckOptions | None = None):
        options = _due_check_options(config=config, params=params, write_report=False)
        return self._board_service.due_check(
            options.config,
            root_id=options.root_id,
            include_run_ids=options.include_run_ids,
            exclude_run_ids=options.exclude_run_ids,
        )

    # LLM: write_due_check writes the same scoped due-check result that the user requested.
    # 函数用途: 写入到期检查报告；如果传了 root_id，只落盘当前任务树的问题视图。
    def write_due_check(self, config=None, *, params: SubAgentDueCheckOptions | None = None):
        options = _due_check_options(config=config, params=params, write_report=True)
        report = self.due_check(params=options)
        import json
        from dataclasses import asdict
        (self.workspace / "subagent_due_check.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_due_check_markdown
        (self.workspace / "SUBAGENT_DUE_CHECK.md").write_text(
            render_due_check_markdown(report), encoding="utf-8",
        )
        return report

    # LLM: plan_actions keeps root-scoped dry-run planning aligned with due-check reports.
    # 函数用途: 生成子代理 dry-run 动作计划；可按 root_id 只处理一棵任务树的问题。
    def plan_actions(self, config=None, *, params: SubAgentPlanActionsOptions | None = None):
        options = _plan_actions_options(config=config, params=params, write_report=False)
        return self._board_service.plan_actions(
            options.config,
            root_id=options.root_id,
            include_run_ids=options.include_run_ids,
            exclude_run_ids=options.exclude_run_ids,
        )

    # LLM: write_action_plan persists the same scoped action plan requested by CLI or parent controller.
    # 函数用途: 写入动作计划报告；如果传了 root_id，只落盘当前任务树的问题动作视图。
    def write_action_plan(self, config=None, *, params: SubAgentPlanActionsOptions | None = None):
        options = _plan_actions_options(config=config, params=params, write_report=True)
        report = self.plan_actions(params=options)
        import json
        from dataclasses import asdict
        (self.workspace / "subagent_action_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_action_plan_markdown
        (self.workspace / "SUBAGENT_ACTION_PLAN.md").write_text(
            render_action_plan_markdown(report), encoding="utf-8",
        )
        return report

    # LLM: _filter_action_plan_items 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理filter动作计划条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _filter_action_plan_items(self, actions, action_filter="", run_id="", limit=0):
        from .policies import _filter_action_plan_items as _filter_items
        return _filter_items(actions, action_filter=action_filter, run_id=run_id, limit=limit)


# LLM: _due_check_options 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理到期检查选项相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _due_check_options(
    *,
    config,
    params: SubAgentDueCheckOptions | None,
    write_report: bool,
) -> SubAgentDueCheckOptions:
    if params is not None:
        if not isinstance(params, SubAgentDueCheckOptions):
            raise TypeError("due check requires params: SubAgentDueCheckOptions")
        return params
    # LLM: 到期检查保留配置位置参数兼容性，内部使用选项参数包。
    return SubAgentDueCheckOptions(config=config, write_report=write_report)


# LLM: _plan_actions_options normalizes legacy config calls into the scoped options bundle.
# 函数用途: 统一解析 action-plan 参数；保留旧 config 入口，同时让 root_id 通过 bundle 传递。
def _plan_actions_options(
    *,
    config,
    params: SubAgentPlanActionsOptions | None,
    write_report: bool,
) -> SubAgentPlanActionsOptions:
    if params is not None:
        if not isinstance(params, SubAgentPlanActionsOptions):
            raise TypeError("plan actions requires params: SubAgentPlanActionsOptions")
        return params
    # LLM: 动作计划保留配置位置参数兼容性，内部使用选项参数包。
    return SubAgentPlanActionsOptions(config=config, write_report=write_report)


# LLM: _board_options 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理看板选项相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _board_options(
    options: SubAgentBoardOptions | None,
    *,
    recent_limit: int,
) -> SubAgentBoardOptions:
    if options is not None:
        if not isinstance(options, SubAgentBoardOptions):
            raise TypeError("board requires options: SubAgentBoardOptions")
        return options
    # LLM: 看板管理器保留 recent_limit 兼容入口，内部改用选项对象。
    return SubAgentBoardOptions(recent_limit=recent_limit)
