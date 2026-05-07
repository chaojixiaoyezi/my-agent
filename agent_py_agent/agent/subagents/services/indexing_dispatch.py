from __future__ import annotations

"""LLM: dispatch record indexing helpers for SubAgentIndexingService."""

from typing import TYPE_CHECKING, Any

from ..reports import DispatchRecord, DispatchWatchRecord, ParentPlannerRecord
from .indexing_params import DataclassRecordIndexParams

if TYPE_CHECKING:
    from ..models import SubAgentExecutionContext


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
