# LLM: Runner workflow dependency gate module; keep phase ordering data-driven and refs-only.
# 模块用途: 在 runner 候选排序前检查 workflow phase 依赖，避免 repair/critic 抢跑。

from __future__ import annotations

from ..subagent import SubAgentTask


# LLM: workflow_dependency_ready_candidates prevents later workflow phases from racing their refs.
# 函数用途: workflow 子任务带 depends_on 时，只有依赖 phase 已提交结果，当前 phase 才能进入 runner 候选。
def workflow_dependency_ready_candidates(
    candidates: list[SubAgentTask],
    all_tasks: list[SubAgentTask],
) -> list[SubAgentTask]:
    phase_index = _workflow_phase_index(all_tasks)
    return [
        task
        for task in candidates
        if _workflow_dependencies_satisfied(task, phase_index)
    ]


# LLM: _workflow_phase_index maps sibling workflow phase ids to persisted task snapshots.
# 函数用途: 构建 workflow_parent_run_id + phase_id 索引，供 depends_on 判定使用。
def _workflow_phase_index(tasks: list[SubAgentTask]) -> dict[tuple[str, str], SubAgentTask]:
    index: dict[tuple[str, str], SubAgentTask] = {}
    for task in tasks:
        parent = _string_task_attr(task, "workflow_parent_run_id")
        phase = _string_task_attr(task, "workflow_phase_id")
        if parent and phase:
            index[(parent, phase)] = task
    return index


# LLM: _workflow_dependencies_satisfied keeps dependency gating data-driven and refs-only.
# 函数用途: 检查当前 workflow phase 依赖的 sibling phase 是否已经到可供下游读取的状态。
def _workflow_dependencies_satisfied(
    task: SubAgentTask,
    phase_index: dict[tuple[str, str], SubAgentTask],
) -> bool:
    depends_on = _list_task_attr(task, "workflow_depends_on")
    if not depends_on:
        return True
    parent = _string_task_attr(task, "workflow_parent_run_id")
    if not parent:
        return True
    return all(_workflow_phase_completed(phase_index.get((parent, phase_id))) for phase_id in depends_on)


# LLM: _workflow_phase_completed defines when a workflow phase has evidence for downstream phases.
# 函数用途: produce/critic 等 phase 到等待验收或已验收时，后续 phase 才能启动。
def _workflow_phase_completed(task: SubAgentTask | None) -> bool:
    if task is None:
        return False
    status = _string_task_attr(task, "status").upper()
    verification = _string_task_attr(task, "verification_status").upper()
    return status in {"AWAITING_ACCEPTANCE", "DONE"} or verification in {
        "NEEDS_ACCEPTANCE",
        "VERIFIED",
    }


# LLM: _string_task_attr avoids MagicMock/default truthiness leaking into dispatch decisions.
# 函数用途: 安全读取任务字符串字段，测试 mock 或缺字段时统一当成空字符串。
def _string_task_attr(task: SubAgentTask, name: str) -> str:
    value = getattr(task, name, "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _list_task_attr avoids treating mock attributes as real workflow dependencies.
# 函数用途: 安全读取任务列表字段，缺字段或非列表时统一当成空列表。
def _list_task_attr(task: SubAgentTask, name: str) -> list[str]:
    value = getattr(task, name, None)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
