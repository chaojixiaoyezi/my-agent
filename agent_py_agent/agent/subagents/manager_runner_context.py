from __future__ import annotations

"""Compatibility facade for runner execution-context APIs."""

from .models import SubAgentExecutionContext
from .services.runner_context import ExecutionContextBuildRequest, SubAgentRunnerContextService


class SubAgentRunnerContextMixin:
    @property
    def _runner_context_service(self) -> SubAgentRunnerContextService:
        service = getattr(self, "runner_context", None)
        if isinstance(service, SubAgentRunnerContextService) and service.manager is self:
            return service
        service = SubAgentRunnerContextService(self)
        self.runner_context = service
        return service

    def _extract_granted_caps(self, task) -> tuple[list[str], list[str], list[dict[str, object]]]:
        return self._runner_context_service._extract_granted_caps(task)

    def _build_write_boundary(self, task) -> dict[str, object]:
        return self._runner_context_service._build_write_boundary(task)

    def build_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        return self._runner_context_service.build_execution_context(run_id, max_cards=max_cards)

    def _make_execution_context(
        self,
        request: ExecutionContextBuildRequest,
    ) -> SubAgentExecutionContext:
        return self._runner_context_service._make_execution_context(request)

    def write_execution_context(
        self,
        run_id: str,
        *,
        max_cards: int = 0,
    ) -> SubAgentExecutionContext:
        return self._runner_context_service.write_execution_context(run_id, max_cards=max_cards)
