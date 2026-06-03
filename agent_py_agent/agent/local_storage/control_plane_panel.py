
from __future__ import annotations

"""Shared progress panel read model for LocalStore control-plane projections."""

from .control_plane_models import AgentRunRecord, AgentRuntimeQueryContext, SharedProgressPanel

_BLOCKED_STATUSES = {"BLOCKED"}


class LocalStoreSharedProgressPanelMixin:

    def query_shared_progress_panel(self, context: AgentRuntimeQueryContext) -> SharedProgressPanel:
        runtime = self.query_agent_runtime(context)
        runs = list(runtime.report.runs)
        return SharedProgressPanel(
            context=runtime.context,
            rollup=runtime.report.rollup,
            runs=runs,
            blocked_runs=_blocked_runs(runs),
            inheritance_manifest_refs=_inheritance_manifest_refs(runs),
            failure_handoff_refs=_failure_handoff_refs(runs),
            takeover_readiness_refs=_takeover_readiness_refs(runs),
            warnings=list(runtime.warnings),
            reserved={
                "source": "local_store_control_plane",
                "fact_source": "task_run_workspace",
                "view": "shared_progress_panel",
            },
        )


def _blocked_runs(runs: list[AgentRunRecord]) -> list[AgentRunRecord]:
    return [run for run in runs if run.status in _BLOCKED_STATUSES]


def _inheritance_manifest_refs(runs: list[AgentRunRecord]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("inheritance_manifest_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _failure_handoff_refs(runs: list[AgentRunRecord]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("failure_handoff_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _takeover_readiness_refs(runs: list[AgentRunRecord]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs
