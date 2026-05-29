# LLM: Runner candidate policy keeps dispatch retry and background-start facts out of runner_dispatch.
# 模块用途: 汇总 runner 候选判断需要的配置，避免调度函数继续长参数和重复启动判断。

from __future__ import annotations

from dataclasses import dataclass


# LLM: RunnerCandidatePolicy carries retry limits and background launch ownership as one value.
# 类用途: 给 runner 候选筛选传递调度策略；background_launch_id 让后台启动进程能执行自己的 run。
@dataclass(frozen=True)
class RunnerCandidatePolicy:
    runner_max_attempts: int = 1
    same_run_redispatch_limit: int | None = None
    background_launch_id: str = ""


# LLM: candidate_policy keeps optional policy handling explicit without var-keyword service interfaces.
# 函数用途: 调用方未传策略时返回默认 RunnerCandidatePolicy。
def candidate_policy(policy: RunnerCandidatePolicy | None = None) -> RunnerCandidatePolicy:
    return policy or RunnerCandidatePolicy()


# LLM: runner_launch_in_progress prevents duplicate manual dispatch while create_subagents auto-starts.
# 函数用途: 有 active attempt 或其它后台 launch 正在跑时，普通调度不重复启动同一个 run。
def runner_launch_in_progress(task: object, policy: RunnerCandidatePolicy) -> bool:
    if _runner_active_attempt_id(task):
        return True
    return _background_start_active(task, background_launch_id=policy.background_launch_id)


# LLM: _runner_active_attempt_id is a concrete task-state check, not a prompt heuristic.
# 函数用途: 读取任务当前 active attempt；存在时说明该 run 已有执行器在跑。
def _runner_active_attempt_id(task: object) -> str:
    value = getattr(task, "runner_active_attempt_id", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


# LLM: _background_start_active distinguishes owner launch from a competing dispatch.
# 函数用途: 当前 background_launch_id 匹配时允许后台进程自己执行；其它调度看到 launching/running 就跳过。
def _background_start_active(task: object, *, background_launch_id: str = "") -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return False
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return False
    status = str(background.get("status") or "").strip().lower()
    if background_launch_id and str(background.get("launch_id") or "").strip() == str(background_launch_id).strip():
        return False
    return status in {"launching", "running"}


__all__ = ["RunnerCandidatePolicy", "candidate_policy", "runner_launch_in_progress"]
