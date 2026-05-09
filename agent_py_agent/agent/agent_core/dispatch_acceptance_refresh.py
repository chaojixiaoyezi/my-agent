# LLM: Dispatch acceptance refresh keeps post-test records aligned with newly written test evidence.
# 模块用途: 显式执行父级验收 tests 后，重新计算 acceptance dry-run 记录，避免 dispatch 展示旧结论。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..subagents.acceptance_review_service import AcceptanceReviewOptions


# LLM: DispatchAcceptanceRefreshRequest bundles post-test refresh inputs for the dispatch layer.
# 类用途: 保存刷新验收记录所需对象，避免 dispatch/core 接口继续增加散参。
@dataclass(frozen=True)
class DispatchAcceptanceRefreshRequest:
    agent: Any
    record: Any
    params: Any
    policy_summary: dict[str, object]


# LLM: refresh_acceptance_after_parent_tests recomputes dry-run acceptance after guarded tests execute.
# 函数用途: 当 dispatch 显式写出新的 test_execution.json 后，刷新本轮 dispatch record 的验收结论。
def refresh_acceptance_after_parent_tests(request: DispatchAcceptanceRefreshRequest) -> Any:
    agent = request.agent
    record = request.record
    params = request.params
    policy_summary = request.policy_summary
    if not _should_refresh_after_tests(params, policy_summary):
        return record
    refreshed = agent.subagents.review_acceptance(
        record.run_id,
        options=AcceptanceReviewOptions(
            apply=False,
            reviewer=params.reviewer,
            note=params.note,
            limit=params.limit,
            execute_tests=False,
        ),
    )
    if params.apply:
        _write_refreshed_acceptance_artifacts(agent, refreshed)
    return refreshed


# LLM: _should_refresh_after_tests is the small guard for dispatch-only post-test recomputation.
# 函数用途: 只在本轮确实执行了父级 tests 时刷新 acceptance，普通 dry-run 和未执行场景保持原行为。
def _should_refresh_after_tests(params: Any, policy_summary: dict[str, object]) -> bool:
    return bool(
        getattr(params, "execute_acceptance_tests", False)
        and policy_summary.get("parent_acceptance_auto_execution_executed") is True
    )


# LLM: _write_refreshed_acceptance_artifacts updates single-run audit files without applying task state.
# 函数用途: dispatch --apply 写过旧验收审计时，用测试后的 dry-run 结论覆盖同一 run 的展示文件和索引。
def _write_refreshed_acceptance_artifacts(agent: Any, record: Any) -> None:
    writer = getattr(agent.subagents, "_write_acceptance_record_files", None)
    indexer = getattr(agent.subagents, "_index_acceptance_review", None)
    if callable(writer):
        writer(record)
    if callable(indexer):
        indexer(record)
