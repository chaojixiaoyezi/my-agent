from __future__ import annotations

"""LLM contract: SubAgentDispatchMixin - thin facade delegating dispatch service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/dispatch.py。
"""

from .services.dispatch import SubAgentDispatchService
from .services.dispatch_params import (
    DispatchRecordParams,
    DispatchWatchHeartbeatParams,
    DispatchWatchRecordParams,
    ParentPlannerRecordParams,
)


class _SubAgentDispatchFacade:
    """Thin facade delegating dispatch record and reporting to SubAgentDispatchService."""

    @property
    def _dispatch_service(self):
        if not hasattr(self, "__dispatch_service"):
            self.__dispatch_service = SubAgentDispatchService(self)
        return self.__dispatch_service

    def make_dispatch_record(
        self,
        *,
        params: DispatchRecordParams | None = None,
        step: str = "",
        action: str = "",
        run_id: str = "",
        dry_run: bool = True,
        applied: bool = False,
        ok: bool = True,
        message: str = "",
        before_status: str = "",
        after_status: str = "",
        before_verification_status: str = "",
        after_verification_status: str = "",
        evidence_paths: list[str] | None = None,
    ):
        params = params or DispatchRecordParams(
            step=step,
            action=action,
            run_id=run_id,
            dry_run=dry_run,
            applied=applied,
            ok=ok,
            message=message,
            before_status=before_status,
            after_status=after_status,
            before_verification_status=before_verification_status,
            after_verification_status=after_verification_status,
            evidence_paths=evidence_paths,
        )
        return self._dispatch_service.make_dispatch_record(params=params)

    def build_dispatch_report(self, records, *, dry_run):
        return self._dispatch_service.build_dispatch_report(records, dry_run=dry_run)

    def write_dispatch_report(self, report, *, append_log=False):
        return self._dispatch_service.write_dispatch_report(report, append_log=append_log)

    def _append_dispatch_log(self, record):
        self._dispatch_service._append_dispatch_log(record)

    def make_dispatch_watch_record(
        self,
        *,
        params: DispatchWatchRecordParams | None = None,
        cycle: int = 0,
        dry_run: bool = True,
        ok: bool = True,
        message: str = "",
        dispatch_record_count: int = 0,
        dispatch_summary: dict[str, int] | None = None,
        started_at: float = 0.0,
        ended_at: float = 0.0,
        evidence_paths: list[str] | None = None,
    ):
        params = params or DispatchWatchRecordParams(
            cycle=cycle,
            dry_run=dry_run,
            ok=ok,
            message=message,
            dispatch_record_count=dispatch_record_count,
            dispatch_summary=dispatch_summary,
            started_at=started_at,
            ended_at=ended_at,
            evidence_paths=evidence_paths,
        )
        return self._dispatch_service.make_dispatch_watch_record(params=params)

    def build_dispatch_watch_report(self, records, *, dry_run):
        return self._dispatch_service.build_dispatch_watch_report(records, dry_run=dry_run)

    def write_dispatch_watch_report(self, report):
        return self._dispatch_service.write_dispatch_watch_report(report)

    def write_dispatch_watch_heartbeat(
        self,
        *,
        params: DispatchWatchHeartbeatParams | None = None,
        cycle: int = 0,
        status: str = "",
        lock_path: str = "",
        pid: int = 0,
        message: str = "",
    ):
        params = params or DispatchWatchHeartbeatParams(
            cycle=cycle,
            status=status,
            lock_path=lock_path,
            pid=pid,
            message=message,
        )
        return self._dispatch_service.write_dispatch_watch_heartbeat(params=params)

    def write_parent_planner_exchange(self, prompt, response=""):
        return self._dispatch_service.write_parent_planner_exchange(prompt, response)

    def make_parent_planner_record(
        self,
        *,
        params: ParentPlannerRecordParams | None = None,
        dry_run: bool = True,
        triggered: bool = False,
        ok: bool = True,
        decision: str = "",
        message: str = "",
        gate_summary: dict[str, int] | None = None,
        backend: str = "",
        tool_rounds: int = 0,
        parse_error: str = "",
        summary: str = "",
        actions: list[dict[str, object]] | None = None,
        blockers: list[str] | None = None,
        risks: list[str] | None = None,
        notes: list[str] | None = None,
        runner_instruction: str = "",
        suggested_max_runners: int = 0,
        prompt_path: str = "",
        response_path: str = "",
        evidence_paths: list[str] | None = None,
    ):
        params = params or ParentPlannerRecordParams(
            dry_run=dry_run,
            triggered=triggered,
            ok=ok,
            decision=decision,
            message=message,
            gate_summary=gate_summary,
            backend=backend,
            tool_rounds=tool_rounds,
            parse_error=parse_error,
            summary=summary,
            actions=actions,
            blockers=blockers,
            risks=risks,
            notes=notes,
            runner_instruction=runner_instruction,
            suggested_max_runners=suggested_max_runners,
            prompt_path=prompt_path,
            response_path=response_path,
            evidence_paths=evidence_paths,
        )
        return self._dispatch_service.make_parent_planner_record(params=params)

    def build_parent_planner_report(self, records, *, dry_run):
        return self._dispatch_service.build_parent_planner_report(records, dry_run=dry_run)

    def write_parent_planner_report(self, report, *, append_log=False):
        return self._dispatch_service.write_parent_planner_report(report, append_log=append_log)

    def append_dispatch_watch_log(self, record):
        self._dispatch_service.append_dispatch_watch_log(record)

    def append_parent_planner_log(self, record):
        self._dispatch_service.append_parent_planner_log(record)

def _render_dispatch_markdown(report):
    from .rendering import render_dispatch_markdown
    return render_dispatch_markdown(report)


def _render_dispatch_watch_markdown(report):
    from .rendering import render_dispatch_watch_markdown
    return render_dispatch_watch_markdown(report)


def _render_parent_planner_markdown(report):
    from .rendering import render_parent_planner_markdown
    return render_parent_planner_markdown(report)


_SubAgentDispatchFacade._render_dispatch_markdown = staticmethod(_render_dispatch_markdown)
_SubAgentDispatchFacade._render_dispatch_watch_markdown = staticmethod(_render_dispatch_watch_markdown)
_SubAgentDispatchFacade._render_parent_planner_markdown = staticmethod(_render_parent_planner_markdown)


class SubAgentDispatchMixin(_SubAgentDispatchFacade):
    """Public compatibility mixin; dispatch behavior stays in the facade class."""
