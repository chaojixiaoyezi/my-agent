from __future__ import annotations

"""LLM contract: SubAgentDispatchMixin - thin facade delegating dispatch service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/dispatch.py。
"""

from .services.dispatch import SubAgentDispatchService


class SubAgentDispatchMixin:
    """Thin facade delegating dispatch record and reporting to SubAgentDispatchService."""

    @property
    def _dispatch_service(self):
        if not hasattr(self, "__dispatch_service"):
            self.__dispatch_service = SubAgentDispatchService(self)
        return self.__dispatch_service

    def make_dispatch_record(self, **kwargs):
        return self._dispatch_service.make_dispatch_record(**kwargs)

    def build_dispatch_report(self, records, *, dry_run):
        return self._dispatch_service.build_dispatch_report(records, dry_run=dry_run)

    def write_dispatch_report(self, report, *, append_log=False):
        return self._dispatch_service.write_dispatch_report(report, append_log=append_log)

    def _append_dispatch_log(self, record):
        self._dispatch_service._append_dispatch_log(record)

    def make_dispatch_watch_record(self, **kwargs):
        return self._dispatch_service.make_dispatch_watch_record(**kwargs)

    def build_dispatch_watch_report(self, records, *, dry_run):
        return self._dispatch_service.build_dispatch_watch_report(records, dry_run=dry_run)

    def write_dispatch_watch_report(self, report):
        return self._dispatch_service.write_dispatch_watch_report(report)

    def write_dispatch_watch_heartbeat(self, **kwargs):
        return self._dispatch_service.write_dispatch_watch_heartbeat(**kwargs)

    def write_parent_planner_exchange(self, prompt, response=""):
        return self._dispatch_service.write_parent_planner_exchange(prompt, response)

    def make_parent_planner_record(self, **kwargs):
        return self._dispatch_service.make_parent_planner_record(**kwargs)

    def build_parent_planner_report(self, records, *, dry_run):
        return self._dispatch_service.build_parent_planner_report(records, dry_run=dry_run)

    def write_parent_planner_report(self, report, *, append_log=False):
        return self._dispatch_service.write_parent_planner_report(report, append_log=append_log)

    def append_dispatch_watch_log(self, record):
        self._dispatch_service.append_dispatch_watch_log(record)

    def append_parent_planner_log(self, record):
        self._dispatch_service.append_parent_planner_log(record)

    # Rendering methods needed by dispatch service
    def _render_dispatch_markdown(self, report):
        from .rendering import render_dispatch_markdown
        return render_dispatch_markdown(report)

    def _render_dispatch_watch_markdown(self, report):
        from .rendering import render_dispatch_watch_markdown
        return render_dispatch_watch_markdown(report)

    def _render_parent_planner_markdown(self, report):
        from .rendering import render_parent_planner_markdown
        return render_parent_planner_markdown(report)