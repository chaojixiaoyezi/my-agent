from __future__ import annotations

"""LLM contract: SubAgentAcceptanceFindingMixin - thin facade delegating acceptance findings service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/acceptance_findings.py。
"""

from .services.acceptance_findings import SubAgentAcceptanceFindingService


class SubAgentAcceptanceFindingMixin:
    """Thin facade delegating acceptance finding rules to SubAgentAcceptanceFindingService."""

    @property
    def _acceptance_finding_service(self):
        if not hasattr(self, "__acceptance_finding_service"):
            self.__acceptance_finding_service = SubAgentAcceptanceFindingService(self)
        return self.__acceptance_finding_service

    def acceptance_findings(self, task, output, runner, created_at):
        return self._acceptance_finding_service.acceptance_findings(task, output, runner, created_at)

    # Backward-compatible alias for internal callers
    def _acceptance_findings(self, task, output, runner, created_at):
        return self.acceptance_findings(task, output, runner, created_at)

    # Expose _artifact_exists for backward compatibility by directly calling
    # the module-level function, bypassing the manager to avoid recursion.
    def _artifact_exists(self, task, raw_path: str) -> bool:
        from .services.acceptance_findings import _artifact_exists as _check
        return _check(self, task, raw_path)