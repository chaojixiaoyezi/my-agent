
from __future__ import annotations

"""dispatch record indexing helpers for SubAgentIndexingService."""

from typing import TYPE_CHECKING, Any

from ...reports import DispatchRecord, DispatchWatchRecord, ParentPlannerRecord
from .params import DataclassRecordIndexParams

if TYPE_CHECKING:
    from ...models import SubAgentExecutionContext


def _index_dispatch_record_via(
    service: Any,
    record: DispatchRecord,
) -> None:
    """Index a dispatch record via SubAgentIndexingService."""
    title = f"Dispatch {record.step}/{record.action} {record.run_id or 'global'}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_dispatch", record.id, title, record, "subagent_dispatch_logged",
        ),
    )


def _index_dispatch_watch_record_via(
    service: Any,
    record: DispatchWatchRecord,
) -> None:
    """Index a dispatch watch record via SubAgentIndexingService."""
    title = f"Dispatch watch cycle {record.cycle}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_dispatch_watch", record.id, title, record, "subagent_dispatch_watch_logged",
        ),
    )


def _index_parent_planner_record_via(
    service: Any,
    record: ParentPlannerRecord,
) -> None:
    """Index a parent planner record via SubAgentIndexingService."""
    title = f"Parent planner {record.decision}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "parent_planner", record.id, title, record, "parent_planner_logged",
        ),
    )


def _index_execution_context_via(
    service: Any,
    context: SubAgentExecutionContext,
) -> None:
    """Index an execution context via SubAgentIndexingService."""
    title = f"Execution context {context.run_id}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_execution_context", context.run_id, title, context, "subagent_execution_context_written",
        ),
    )
