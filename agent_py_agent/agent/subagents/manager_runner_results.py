from __future__ import annotations

"""Compatibility facade for runner result APIs."""

from .manager_runner_result_payload import RecordRunnerResultParams
from .models import SubAgentRunnerResult
from .services.runner_result import SubAgentRunnerResultService


class SubAgentRunnerResultMixin:
    @property
    def _runner_result_service(self) -> SubAgentRunnerResultService:
        service = getattr(self, "runner_result", None)
        if isinstance(service, SubAgentRunnerResultService) and service.manager is self:
            return service
        service = SubAgentRunnerResultService(self)
        self.runner_result = service
        return service

    def record_runner_result(self, params: RecordRunnerResultParams) -> SubAgentRunnerResult:
        return self._runner_result_service.record_runner_result(params)

    def _build_and_persist_result(self, ctx) -> SubAgentRunnerResult:
        return self._runner_result_service._build_and_persist_result(ctx)

    def _extract_parsed_output(self, task, structured_output, now: float, actual_tools):
        return self._runner_result_service._extract_parsed_output(task, structured_output, now, actual_tools)

    def _apply_status_and_build_payload(self, params, extracted, now: float):
        return self._runner_result_service._apply_status_and_build_payload(params, extracted, now)

    def _post_result_side_effects(self, task, result, params) -> int:
        return self._runner_result_service._post_result_side_effects(task, result, params)

    def _runner_result_build_context(self, build_params, task):
        return self._runner_result_service._runner_result_build_context(build_params, task)

    def _check_stale_runner_result(self, task, attempt_id, dry_run):
        return self._runner_result_service._check_stale_runner_result(task, attempt_id, dry_run)

    def _make_quick_result(self, task, dry_run, ok, message):
        return self._runner_result_service._make_quick_result(task, dry_run, ok, message)


__all__ = ["RecordRunnerResultParams", "SubAgentRunnerResultMixin"]
