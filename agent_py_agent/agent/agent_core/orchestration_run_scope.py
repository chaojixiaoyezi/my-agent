# LLM: Runtime orchestration scope tracks subagent ids touched by the current root turn.
# 模块用途: 让看板/收口只按本轮创建或调度的子代理判断，避免旧测试或旧任务污染最终状态。

from __future__ import annotations

from collections.abc import Iterable


# LLM: remember_orchestration_run_ids stores concrete run ids on the live agent facade.
# 函数用途: 记录当前 root 轮次创建或调度过的 run_id；不持久化，只用于本轮状态收口。
def remember_orchestration_run_ids(agent: object, run_ids: Iterable[object]) -> None:
    seen = getattr(agent, "_orchestration_run_ids_seen", None)
    if not isinstance(seen, set):
        seen = set()
        agent._orchestration_run_ids_seen = seen
    for raw in run_ids:
        run_id = str(raw or "").strip()
        if run_id:
            seen.add(run_id)


# LLM: remembered_orchestration_run_ids returns the current root-turn scope without mutating it.
# 函数用途: 给最终收口和工具 payload 读取本轮相关 run_id；没有记录时返回空集合表示兼容旧行为。
def remembered_orchestration_run_ids(agent: object) -> set[str]:
    seen = getattr(agent, "_orchestration_run_ids_seen", None)
    if not isinstance(seen, set):
        return set()
    return {str(item) for item in seen if str(item or "").strip()}
