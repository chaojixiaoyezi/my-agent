# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""dispatch record indexing helpers for SubAgentIndexingService."""

from typing import TYPE_CHECKING, Any

from ..reports import DispatchRecord, DispatchWatchRecord, ParentPlannerRecord
from .indexing_params import DataclassRecordIndexParams

if TYPE_CHECKING:
    from ..models import SubAgentExecutionContext


# LLM: _index_dispatch_record_via 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理index调度记录via相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _index_dispatch_record_via(
    manager_or_service: Any,
    record: DispatchRecord,
) -> None:
    """Index a dispatch record via manager or service."""
    title = f"Dispatch {record.step}/{record.action} {record.run_id or 'global'}"
    manager_or_service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_dispatch", record.id, title, record, "subagent_dispatch_logged",
        ),
    )


# LLM: _index_dispatch_watch_record_via 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理index调度监控记录via相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _index_dispatch_watch_record_via(
    manager_or_service: Any,
    record: DispatchWatchRecord,
) -> None:
    """Index a dispatch watch record via manager or service."""
    title = f"Dispatch watch cycle {record.cycle}"
    manager_or_service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_dispatch_watch", record.id, title, record, "subagent_dispatch_watch_logged",
        ),
    )


# LLM: _index_parent_planner_record_via 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理index父级规划器记录via相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _index_parent_planner_record_via(
    manager_or_service: Any,
    record: ParentPlannerRecord,
) -> None:
    """Index a parent planner record via manager or service."""
    title = f"Parent planner {record.decision}"
    manager_or_service._index_dataclass_record(
        DataclassRecordIndexParams(
            "parent_planner", record.id, title, record, "parent_planner_logged",
        ),
    )


# LLM: _index_execution_context_via 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理indexexecution上下文via相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _index_execution_context_via(
    manager_or_service: Any,
    context: SubAgentExecutionContext,
) -> None:
    """Index an execution context via manager or service."""
    title = f"Execution context {context.run_id}"
    manager_or_service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_execution_context", context.run_id, title, context, "subagent_execution_context_written",
        ),
    )
