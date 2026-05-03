from __future__ import annotations

"""LLM contract: SubAgentWorkflowMixin - thin facade delegating workflow service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/workflow.py。
"""

from .services.workflow import SubAgentWorkflowService


class SubAgentWorkflowMixin:
    """Thin facade delegating workflow planning and realization to SubAgentWorkflowService."""

    @property
    def _workflow_service(self):
        if not hasattr(self, "__workflow_service"):
            self.__workflow_service = SubAgentWorkflowService(self)
        return self.__workflow_service

    def ensure_workflow_plan(self, run_id: str, *, workflow_mode: str):
        """Ensure a workflow plan is persisted for the given run."""
        return self._workflow_service.plan_workflow(run_id, workflow_mode=workflow_mode)

    def realize_workflow_plan(self, run_id: str):
        """Materialize persisted workflow worker specs into child runs."""
        return self._workflow_service.realize_workflow_plan(run_id)