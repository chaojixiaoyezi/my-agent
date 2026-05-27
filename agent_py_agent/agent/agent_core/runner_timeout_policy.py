# LLM: Runner timeout policy is isolated so runner execution stays focused on worker orchestration.
# 模块用途: 根据全局配置、角色覆盖和动态估算计算单个子代理 runner 的 wrapper timeout。

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..subagent import SubAgentTask


# LLM: runner_timeout_disabled makes off/none/0 mean no wrapper timeout.
# 函数用途: 判断用户是否显式关闭 runner 超时；true 表示可一直等模型自然返回。
def runner_timeout_disabled(config: Any) -> bool:
    raw_value = getattr(config, "runner_timeout_seconds", "off")
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(raw_value) == 0.0
    except (TypeError, ValueError):
        return False


# LLM: get_task_timeout reads role overrides before falling back to static or dynamic timeout.
# 函数用途: 为一个子代理任务计算执行 wrapper timeout；0 表示不限制。
def get_task_timeout(task: SubAgentTask, runner_timeout_seconds: float, config: Any) -> float:
    from .dynamic_timeout import calculate_dynamic_timeout, estimate_task_tokens
    from .runner_dispatch import _resolve_runner_timeout_seconds

    role_override = _runner_timeout_for_task_role(task, config)
    if role_override is not None:
        if _runner_timeout_value_disabled(role_override):
            return 0.0
        resolved_override = _resolve_runner_timeout_seconds(role_override)
        if resolved_override > 0:
            return resolved_override
        if not _runner_timeout_value_auto(role_override):
            role_override = None

    if role_override is None and runner_timeout_seconds > 0:
        return runner_timeout_seconds
    if role_override is None and runner_timeout_disabled(config):
        return 0.0

    if task.attributes and "dynamic_timeout_seconds" in task.attributes:
        timeout = float(task.attributes["dynamic_timeout_seconds"])
        if timeout > 0:
            return timeout

    estimated_input_tokens, estimated_output_tokens = estimate_task_tokens(task.goal, task.plan)
    return calculate_dynamic_timeout(config, estimated_input_tokens, estimated_output_tokens)


# LLM: resolve_runner_config derives batch runner timeout/concurrency/start-rate in one place.
# 函数用途: 将配置中的 runner 超时、并发和启动速率规整给 dispatch batch 使用。
def resolve_runner_config(config: Any, job_count: int) -> tuple[float, int, int]:
    from .runner_dispatch import (
        _resolve_runner_concurrency,
        _resolve_runner_start_rate,
        _resolve_runner_timeout_seconds,
    )

    runner_start_rate = _resolve_runner_start_rate(config.runner_start_rate, job_count)
    if runner_start_rate and runner_start_rate < job_count:
        job_count = runner_start_rate
    runner_concurrency = _resolve_runner_concurrency(
        config.runner_concurrency,
        job_count,
        auto_limit=getattr(config, "runner_auto_concurrency", job_count),
    )
    runner_timeout_seconds = _resolve_runner_timeout_seconds(config.runner_timeout_seconds)
    return runner_timeout_seconds, runner_concurrency, runner_start_rate


# LLM: _runner_timeout_for_task_role lets tests and production use different budgets per role.
# 函数用途: 先匹配 root，再匹配 task.role，最后匹配 default/*。
def _runner_timeout_for_task_role(task: SubAgentTask, config: Any) -> object | None:
    mapping = getattr(config, "runner_timeout_by_role", {}) or {}
    if not isinstance(mapping, dict):
        return None
    keys = _runner_timeout_role_keys(task)
    normalized = {str(key).strip().lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in normalized:
            return normalized[key]
    return normalized.get("default", normalized.get("*"))


# LLM: _runner_timeout_role_keys keeps root detection independent from a role label typo.
# 函数用途: 为当前任务生成角色匹配顺序；根节点优先 root，再看模板角色。
def _runner_timeout_role_keys(task: SubAgentTask) -> list[str]:
    keys: list[str] = []
    run_id = str(getattr(task, "id", "") or "")
    parent_id = str(getattr(task, "parent_id", "") or "")
    root_id = str(getattr(task, "root_id", "") or "")
    role = str(getattr(task, "role", "") or "").strip().lower()
    if (not parent_id or (run_id and root_id and run_id == root_id)) and _runner_timeout_root_like_role(role):
        keys.append("root")
    if str((getattr(task, "attributes", {}) or {}).get("takeover_source_run_id") or "").strip():
        keys.append("takeover")
    if role:
        keys.append(role)
        keys.extend(_runner_timeout_role_aliases(role))
    return keys


# LLM: _runner_timeout_root_like_role keeps top-level workers from inheriting root's unlimited budget.
# 函数用途: 判断无父节点任务是否真是带队/root 类角色。
def _runner_timeout_root_like_role(role: str) -> bool:
    normalized = str(role or "").strip().lower().replace("-", "_")
    if not normalized:
        return True
    return normalized in {"root", "coordinator", "leader", "manager", "planner", "dispatcher"} or normalized.endswith(
        "_coordinator"
    )


# LLM: _runner_timeout_role_aliases hides internal template names from timeout config.
# 函数用途: 把 leaf_worker/review/critic 等内部角色归到用户能理解的大类。
def _runner_timeout_role_aliases(role: str) -> list[str]:
    aliases: list[str] = []
    normalized = str(role or "").strip().lower().replace("-", "_")
    if "worker" in normalized and normalized != "worker":
        aliases.append("worker")
    if normalized in {"review", "reviewer", "critic", "qa", "tester"}:
        aliases.append("tester")
    if normalized in {"verifier"}:
        aliases.append("tester")
    if (
        ("coordinator" in normalized and normalized != "coordinator")
        or normalized in {"leader", "manager", "planner", "dispatcher"}
        or normalized.endswith("_leader")
    ):
        aliases.append("coordinator")
    return aliases


# LLM: _runner_timeout_value_disabled mirrors runner_timeout_seconds without requiring full config.
# 函数用途: 判断角色级 timeout 值是否表示不限制。
def _runner_timeout_value_disabled(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


# LLM: _runner_timeout_value_auto lets role overrides opt into dynamic timeout.
# 函数用途: 判断角色级 timeout 是否表示动态估算。
def _runner_timeout_value_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == "auto"
