# LLM: Shared progress panel composes runtime query projections without becoming a fact source.
# 模块用途: 将 LocalStore 控制面查询整理成上级代理/接管代理可读的共享进度状态包。

from __future__ import annotations

"""Shared progress panel read model for LocalStore control-plane projections."""

from .control_plane_models import AgentRunRecord, AgentRuntimeQueryContext, SharedProgressPanel

_BLOCKED_STATUSES = {"BLOCKED"}


# LLM: LocalStoreSharedProgressPanelMixin builds panel read models from existing control-plane queries.
# 类用途: 为 LocalStore 增加共享进度面板查询，不新增事实源。
class LocalStoreSharedProgressPanelMixin:

    # LLM: query_shared_progress_panel joins runtime query, blocked runs, and recovery refs.
    # 函数用途: 查询共享进度面板所需的最小状态包。
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


# LLM: _blocked_runs keeps panel risk highlights scoped to visible runs.
# 函数用途: 从面板可见 run 中筛选阻塞项。
def _blocked_runs(runs: list[AgentRunRecord]) -> list[AgentRunRecord]:
    return [run for run in runs if run.status in _BLOCKED_STATUSES]


# LLM: _inheritance_manifest_refs exposes audit refs without loading file contents.
# 函数用途: 收集可见 run 的 inheritance manifest 引用并去重。
def _inheritance_manifest_refs(runs: list[AgentRunRecord]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("inheritance_manifest_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# LLM: _failure_handoff_refs exposes recovery refs without loading failure handoff files.
# 函数用途: 收集可见 run 的 failure handoff 引用并去重。
def _failure_handoff_refs(runs: list[AgentRunRecord]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("failure_handoff_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# LLM: _takeover_readiness_refs exposes recovery packet refs without loading packet files.
# 函数用途: 收集可见 run 的 takeover readiness 引用并去重。
def _takeover_readiness_refs(runs: list[AgentRunRecord]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs
