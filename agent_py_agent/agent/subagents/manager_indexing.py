from __future__ import annotations

"""LLM contract: SubAgentIndexingMixin - thin facade delegating indexing service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/indexing.py。
"""

from .services.indexing import SubAgentIndexingService


class SubAgentIndexingMixin:
    """Thin facade delegating indexing and event logging to SubAgentIndexingService."""

    @property
    def _indexing_service(self):
        if not hasattr(self, "__indexing_service"):
            self.__indexing_service = SubAgentIndexingService(self)
        return self.__indexing_service

    def _log_local_record(self, **kwargs):
        return self._indexing_service.log_local_record(**kwargs)

    def _index_task(self, task):
        return self._indexing_service.index_task(task)

    def _index_report(self, source_type, source_id, title, report, *, event_type):
        return self._indexing_service.index_report(source_type, source_id, title, report, event_type=event_type)

    def _index_action_apply(self, record):
        return self._indexing_service.index_action_apply(record)

    def _index_capability_route(self, record):
        return self._indexing_service.index_capability_route(record)

    def _index_acceptance_review(self, record):
        return self._indexing_service.index_acceptance_review(record)

    def _index_patch_review(self, record):
        return self._indexing_service.index_patch_review(record)

    def _index_dispatch_record(self, record):
        return self._indexing_service.index_dispatch_record(record)

    def _index_dispatch_watch_record(self, record):
        return self._indexing_service.index_dispatch_watch_record(record)

    def _index_parent_planner_record(self, record):
        return self._indexing_service.index_parent_planner_record(record)

    def _index_execution_context(self, context):
        return self._indexing_service.index_execution_context(context)

    def _index_runner_result(self, result, output_payload):
        return self._indexing_service.index_runner_result(result, output_payload)

    def _index_channel_probe(self, result):
        return self._indexing_service.index_channel_probe(result)

    def _index_dataclass_record(self, source_type, source_id, title, record, event_type):
        return self._indexing_service._index_dataclass_record(source_type, source_id, title, record, event_type)

    def _select_runs(self, run_ids):
        return self._indexing_service.select_runs(run_ids)

    # Public aliases
    def select_runs(self, run_ids):
        return self._indexing_service.select_runs(run_ids)

    def index_task(self, task):
        return self._indexing_service.index_task(task)

    def index_dispatch_record(self, record):
        return self._indexing_service.index_dispatch_record(record)

    def index_dispatch_watch_record(self, record):
        return self._indexing_service.index_dispatch_watch_record(record)

    def index_parent_planner_record(self, record):
        return self._indexing_service.index_parent_planner_record(record)

    def index_execution_context(self, context):
        return self._indexing_service.index_execution_context(context)

    def index_report(self, source_type, source_id, title, report, *, event_type):
        return self._indexing_service.index_report(source_type, source_id, title, report, event_type=event_type)

    def log_local_record(self, **kwargs):
        return self._indexing_service.log_local_record(**kwargs)