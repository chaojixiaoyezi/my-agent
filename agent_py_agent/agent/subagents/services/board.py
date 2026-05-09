# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""board and action planning service for subagent tasks.

给人看的解释：
这里承接看板构建、due-check 巡检、动作计划生成等逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from ..models import SubAgentBoardOptions, SubAgentTask
from ..policies import (
    _action_for_issue,
    _commands_for_action,
    _issue_weight,
    _risk_weight,
    _severity_weight,
)
from ..reports import (
    ActionPlanItem,
    ActionPlanReport,
    DueCheckIssue,
    DueCheckReport,
    SubAgentBoard,
    SubAgentBoardItem,
)
from ..utils import _merge_list
from .board_due_checks import inspect_single_task_due
from .board_due_models import DueCheckSettings, InspectTaskDueRequest
from .rescue_policy import merge_rescue_fields, rescue_fields_for_issue

if TYPE_CHECKING:
    from ..capability_config import CapabilityConfig


# LLM: _build_risk_flags 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 构建riskflags所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_risk_flags(
    task: SubAgentTask,
    open_request_count: int,
    open_gap_count: int,
) -> list[str]:
    """Calculate risk flags for a task."""
    flags: list[str] = []
    if task.status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        flags.append(task.status.lower())
    if task.status == "DONE" and not task.evidence:
        flags.append("done_without_evidence")
    if task.status == "DONE" and task.verification_status != "VERIFIED":
        flags.append("done_without_verification")
    if open_request_count:
        flags.append("open_capability_request")
    if open_gap_count:
        flags.append("open_capability_gap")
    if task.takeover_by:
        flags.append("taken_over")
    if task.channel_status == "BROKEN":
        flags.append("channel_broken")
    if task.channel_status == "DEGRADED":
        flags.append("channel_degraded")
    return flags


# LLM: _scoped_due_check_tasks keeps root-scoped due-check filtering in one auditable helper.
# 函数用途: 过滤到期检查任务列表；未传 root_id 时保持全部任务，传入时只保留该 root 树。
def _scoped_due_check_tasks(tasks: list[SubAgentTask], root_id: str) -> list[SubAgentTask]:
    normalized = str(root_id or "").strip()
    if not normalized:
        return tasks
    return [task for task in tasks if (task.root_id or task.id) == normalized]


# LLM: _to_board_item 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 转换看板条目的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _to_board_item(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
    include_child_status_counts: bool = True,
) -> SubAgentBoardItem:
    """Convert a task to a board item."""
    open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    flags = _build_risk_flags(task, open_request_count, open_gap_count)
    # LLM: child status counts let parents inspect the task tree without reading every work log.
    child_status_counts = (
        _child_status_counts(manager, task, task_index=task_index)
        if include_child_status_counts
        else {}
    )
    return SubAgentBoardItem(
        id=task.id,
        root_id=task.root_id,
        parent_id=task.parent_id,
        depth=task.depth,
        status=task.status,
        verification_status=task.verification_status,
        channel_status=task.channel_status,
        owner=task.owner,
        supervisor=task.supervisor,
        final_owner=task.final_owner,
        goal=task.goal,
        updated_at=task.updated_at,
        heartbeat_at=task.heartbeat_at,
        evidence_count=len(task.evidence),
        evidence_packet_count=len(task.evidence_packets),
        finding_count=len(task.findings),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        child_count=len(task.child_ids),
        child_status_counts=child_status_counts,
        progress=max(0.0, min(1.0, float(task.progress or 0.0))),
        current_step=task.current_step,
        latest_summary=task.latest_summary,
        blocker_count=len(task.blockers),
        takeover_by=task.takeover_by,
        locked_file_count=len(task.locked_files),
        risk_flags=flags,
        task_dir=task.task_dir,
        output_json=task.output_json,
    )


# LLM: _child_status_counts 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理子级状态counts相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _child_status_counts(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child_id in task.child_ids:
        try:
            child = task_index[child_id] if task_index is not None else manager.load(child_id)
        except (FileNotFoundError, TypeError):
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        except KeyError:
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        counts[child.status] = counts.get(child.status, 0) + 1
    return counts


# LLM: SubAgentBoardService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent看板服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentBoardService:
    """Board, due-check, and action planning service."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: build_board 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建看板所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ) -> SubAgentBoard:
        """Build the subagent traffic light board."""
        board_options = _board_options(options, recent_limit=recent_limit)
        tasks = self.manager.list_runs()
        # LLM: Full boards reuse the loaded task set for child counts; lightweight paths stay metadata-only.
        task_index = {task.id: task for task in tasks} if board_options.include_child_status_counts else None
        items = [
            _to_board_item(
                self.manager,
                task,
                task_index=task_index,
                include_child_status_counts=board_options.include_child_status_counts,
            )
            for task in tasks
        ]
        summary: dict[str, int] = {"total": len(items)}
        for item in items:
            summary[item.status] = summary.get(item.status, 0) + 1
            summary[item.verification_status] = summary.get(item.verification_status, 0) + 1
            summary[f"channel_{item.channel_status}"] = summary.get(f"channel_{item.channel_status}", 0) + 1
        hot_list = [item for item in items if item.risk_flags]
        hot_list.sort(key=lambda item: (-_risk_weight(item.risk_flags), -(item.updated_at or 0)))
        recent = items[: board_options.recent_limit]
        return SubAgentBoard(
            generated_at=time.time(),
            summary=summary,
            hot_list=hot_list,
            recent=recent,
            items=items,
        )

    # LLM: due_check can scope inspection to one root task while preserving default all-runs behavior.
    # 函数用途: 巡检子代理是否超时、阻塞或缺证据；传 root_id 时只看该任务树。
    def due_check(self, config: CapabilityConfig | None = None, *, root_id: str = "") -> DueCheckReport:
        """Inspect all subagent runs to find issues needing parent intervention."""
        if config is None and hasattr(self.manager, "_make_default_capability_config"):
            config = self.manager._make_default_capability_config()
        cfg = config
        now = time.time()
        heartbeat_timeout = cfg.subagent_heartbeat_timeout if cfg else 0
        run_timeout = cfg.subagent_run_timeout if cfg else 0
        min_evidence = cfg.subagent_min_evidence_for_done if cfg else 0
        settings = DueCheckSettings(now, heartbeat_timeout, run_timeout, min_evidence)
        issues: list[DueCheckIssue] = []
        # LLM: due-check uses one loaded task index so parent/child rules stay refs-only and cheap.
        tasks = _scoped_due_check_tasks(self.manager.list_runs(), root_id)
        task_index = {task.id: task for task in tasks}

        for task in tasks:
            issues.extend(
                inspect_single_task_due(
                    InspectTaskDueRequest(
                        manager=self.manager,
                        task=task,
                        settings=settings,
                        risk_flags_builder=_build_risk_flags,
                        task_index=task_index,
                    )
                )
            )

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        summary: dict[str, int] = {"total": len(issues)}
        for issue in issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1
            summary[issue.kind] = summary.get(issue.kind, 0) + 1
        return DueCheckReport(generated_at=now, summary=summary, issues=issues)

    # LLM: plan_actions reuses due-check scoping so dry-run actions stay tied to the active task tree.
    # 函数用途: 根据 due-check 问题生成 dry-run 动作计划；传 root_id 时只为这棵任务树生成建议。
    def plan_actions(
        self,
        config: CapabilityConfig | None = None,
        *,
        root_id: str = "",
    ) -> ActionPlanReport:
        """Convert due-check issues into a dry-run action plan."""
        due_report = self.due_check(config, root_id=root_id)
        merged: dict[tuple[str, str], ActionPlanItem] = {}
        for issue in due_report.issues:
            action, priority, would_change_status_to = _action_for_issue(issue)
            key = (issue.run_id, action)
            if key not in merged:
                # LLM: rescue metadata keeps action plans auditable before apply mutates state.
                rescue_fields = rescue_fields_for_issue(issue, action)
                merged[key] = ActionPlanItem(
                    id=self.manager._new_id("action"),
                    run_id=issue.run_id,
                    severity=issue.severity,
                    priority=priority,
                    action=action,
                    reason=issue.message,
                    source_issue_kinds=[issue.kind],
                    suggested_commands=_commands_for_action(action, issue.run_id),
                    would_change_status_to=would_change_status_to,
                    **rescue_fields,
                    owner=issue.owner,
                    final_owner=issue.final_owner,
                    task_dir=issue.task_dir,
                    created_at=time.time(),
                )
                continue
            item = merged[key]
            item.source_issue_kinds = _merge_list(item.source_issue_kinds, [issue.kind])
            merge_rescue_fields(item, issue, action)
            item.reason = f"{item.reason} / {issue.message}"
            if _severity_weight(issue.severity) > _severity_weight(item.severity):
                item.severity = issue.severity
            item.priority = max(item.priority, priority)

        actions = list(merged.values())
        actions.sort(key=lambda item: (-item.priority, item.run_id, item.action))
        summary: dict[str, int] = {"total": len(actions)}
        for action in actions:
            summary[action.severity] = summary.get(action.severity, 0) + 1
            summary[action.action] = summary.get(action.action, 0) + 1
        return ActionPlanReport(generated_at=time.time(), summary=summary, actions=actions)


# LLM: _board_options 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理看板选项相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _board_options(
    options: SubAgentBoardOptions | None,
    *,
    recent_limit: int,
) -> SubAgentBoardOptions:
    if options is not None:
        if not isinstance(options, SubAgentBoardOptions):
            raise TypeError("build_board requires options: SubAgentBoardOptions")
        return options
    # LLM: 看板服务把旧 recent_limit 入口归一到选项参数包。
    return SubAgentBoardOptions(recent_limit=recent_limit)
