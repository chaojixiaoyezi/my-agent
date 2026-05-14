# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagent import RecordRunnerResultParams, SubAgentRunnerResult, SubAgentTask


# ---------------------------------------------------------------------------
# Task timeout calculation
# ---------------------------------------------------------------------------


# LLM: timeout off/none/disabled must mean no runner wrapper timeout, not auto dynamic timeout.
# 函数用途: 判断用户是否显式关闭 runner 超时；返回 true 时 runner 可一直等到模型自然返回。
def _runner_timeout_disabled(config: Any) -> bool:
    raw_value = getattr(config, "runner_timeout_seconds", "off")
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(raw_value) == 0.0
    except (TypeError, ValueError):
        return False


# LLM: get_task_timeout 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询任务超时需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def get_task_timeout(
    task: SubAgentTask,
    runner_timeout_seconds: float,
    config: Any,
) -> float:
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

    # Use configured static timeout
    if role_override is None and runner_timeout_seconds > 0:
        return runner_timeout_seconds
    if role_override is None and _runner_timeout_disabled(config):
        return 0.0

    # Check for pre-calculated dynamic timeout
    if task.attributes and "dynamic_timeout_seconds" in task.attributes:
        timeout = float(task.attributes["dynamic_timeout_seconds"])
        if timeout > 0:
            return timeout

    # Dynamic calculation
    estimated_input_tokens, estimated_output_tokens = estimate_task_tokens(task.goal, task.plan)
    return calculate_dynamic_timeout(
        config,
        estimated_input_tokens,
        estimated_output_tokens,
    )


# LLM: _runner_timeout_for_task_role lets tests and production use different wrapper budgets per hierarchy role.
# 函数用途: 先匹配 root，再匹配 task.role，最后匹配 default/*；未配置时返回 None 走全局 runner_timeout_seconds。
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
# 函数用途: 为当前任务生成角色匹配顺序；根节点优先 root，其次再看 coordinator/worker 等模板角色。
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


# LLM: _runner_timeout_root_like_role keeps a top-level worker from inheriting root's unlimited budget.
# 函数用途: 判断无父节点任务是否真是带队/root 类角色；普通 worker 即使在顶层也应按 worker 超时桶。
def _runner_timeout_root_like_role(role: str) -> bool:
    normalized = str(role or "").strip().lower().replace("-", "_")
    if not normalized:
        return True
    return normalized in {"root", "coordinator", "leader", "manager", "planner", "dispatcher"} or normalized.endswith(
        "_coordinator"
    )


# LLM: _runner_timeout_role_aliases hides internal template names from user-facing timeout config.
# 函数用途: 把 leaf_worker/repair_worker/review/critic/acceptance 等内部角色归到用户能理解的大类。
def _runner_timeout_role_aliases(role: str) -> list[str]:
    aliases: list[str] = []
    if "worker" in role and role != "worker":
        aliases.append("worker")
    if role in {"review", "reviewer", "critic", "qa", "tester"}:
        aliases.append("tester")
    if role in {"acceptor", "acceptance", "verifier"}:
        aliases.append("acceptor")
    if role in {"coordinator", "leader", "manager"}:
        aliases.append("coordinator")
    return aliases


# LLM: _runner_timeout_value_disabled mirrors runner_timeout_seconds without requiring a whole config object.
# 函数用途: 判断角色级 timeout 值是否表示不限制，用于 root/coordinator 长跑、worker 短超时的混合场景。
def _runner_timeout_value_disabled(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


# LLM: _runner_timeout_value_auto lets role overrides opt into dynamic timeout without inheriting the global value.
# 函数用途: 判断角色级 timeout 是否表示动态估算；只有 auto 会跳过全局固定超时进入后续动态计算。
def _runner_timeout_value_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == "auto"


# LLM: resolve_runner_config 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器config需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def resolve_runner_config(config: Any, job_count: int) -> tuple[float, int, int]:
    from .runner_dispatch import (
        _resolve_runner_concurrency,
        _resolve_runner_start_rate,
        _resolve_runner_timeout_seconds,
    )

    runner_start_rate = _resolve_runner_start_rate(config.runner_start_rate, job_count)
    if runner_start_rate and runner_start_rate < job_count:
        job_count = runner_start_rate
    runner_concurrency = _resolve_runner_concurrency(config.runner_concurrency, job_count)
    runner_timeout_seconds = _resolve_runner_timeout_seconds(config.runner_timeout_seconds)
    return runner_timeout_seconds, runner_concurrency, runner_start_rate


# ---------------------------------------------------------------------------
# Runner execution
# ---------------------------------------------------------------------------


# LLM: SingleRunnerParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存单个执行器参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SingleRunnerParams:
    agent: SimpleAgent
    run_id: str
    task_timeout: float
    instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool
    retry_reason: str


# LLM: ConcurrentRunnerParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存concurrent执行器参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ConcurrentRunnerParams:
    agent: SimpleAgent
    pending_jobs: list[tuple[str, Any, str]]
    runner_concurrency: int
    runner_timeout_seconds: float
    instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool


# LLM: RunnerFailureParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器失败参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerFailureParams:
    agent: SimpleAgent
    run_id: str
    before: Any
    result: Any
    effective_instruction: str


# LLM: _RunnerWorkerRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器工作器请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _RunnerWorkerRequest:
    params: ConcurrentRunnerParams
    run_id: str
    before: Any
    retry_reason: str


# LLM: run_single_runner 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进单个执行器的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_single_runner(params: SingleRunnerParams) -> SubAgentRunnerResult:
    from .runner_dispatch import RunSubagentWorkerParams, _run_subagent_worker
    from .subagent_params import SubagentRunParams

    if params.execute_runners and params.task_timeout > 0:
        worker_params = RunSubagentWorkerParams(
            config=params.agent.config,
            root=params.agent.root,
            run_id=params.run_id,
            instruction=params.instruction,
            dry_run=False,
            max_cards=params.max_cards,
            probe=params.probe,
            retry_reason=params.retry_reason,
            timeout_seconds=params.task_timeout,
            local_store=params.agent.local_store,
            backend_override=getattr(params.agent, "_subagent_worker_backend_override", None),
        )
        return _run_subagent_worker(worker_params)
    return params.agent.run_subagent(
        params=SubagentRunParams(
            run_id=params.run_id,
            instruction=params.instruction,
            dry_run=not params.execute_runners,
            max_cards=params.max_cards,
            probe=params.probe,
            retry_reason=params.retry_reason,
        )
    )


# LLM: run_concurrent_runners 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进concurrentrunners的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_concurrent_runners(params: ConcurrentRunnerParams) -> dict[str, tuple[SubAgentRunnerResult, Any]]:
    from .runner_dispatch import _run_subagent_worker

    future_to_job = {}
    with ThreadPoolExecutor(max_workers=params.runner_concurrency) as executor:
        for run_id, before, retry_reason in params.pending_jobs:
            future = executor.submit(
                _run_subagent_worker,
                _runner_worker_params(request=_RunnerWorkerRequest(params, run_id, before, retry_reason)),
            )
            future_to_job[future] = (run_id, before, retry_reason)

        completed: dict[str, tuple[SubAgentRunnerResult, Any]] = {}
        for future in as_completed(future_to_job):
            run_id, before, retry_reason = future_to_job[future]
            result = _collect_runner_future_result(params, future, run_id)
            after = params.agent.subagents.load(run_id)
            completed[run_id] = (result, after)

    return completed


# LLM: _runner_worker_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器工作器参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_worker_params(
    context: ConcurrentRunnerParams | None = None,
    *,
    request: _RunnerWorkerRequest | None = None,
    run_id: str = "",
    before: Any = None,
    retry_reason: str = "",
):
    from .runner_dispatch import RunSubagentWorkerParams

    request = request or _RunnerWorkerRequest(context, run_id, before, retry_reason)
    params = request.params
    task_timeout = get_task_timeout(request.before, params.runner_timeout_seconds, params.agent.config)
    return RunSubagentWorkerParams(
        config=params.agent.config,
        root=params.agent.root,
        run_id=request.run_id,
        instruction=params.instruction,
        dry_run=not params.execute_runners,
        max_cards=params.max_cards,
        probe=params.probe,
        retry_reason=request.retry_reason,
        timeout_seconds=task_timeout,
        local_store=params.agent.local_store,
        backend_override=getattr(params.agent, "_subagent_worker_backend_override", None),
    )


# LLM: _collect_runner_future_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器future结果需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _collect_runner_future_result(params: ConcurrentRunnerParams, future, run_id: str):
    try:
        return future.result()
    except Exception as exc:
        return params.agent.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=run_id,
                dry_run=False,
                ok=False,
                message=f"runner worker failed: {exc}",
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_worker_error",
            )
        )


# ---------------------------------------------------------------------------
# Runner failure handling
# ---------------------------------------------------------------------------


# LLM: handle_runner_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器失败的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def handle_runner_failure(params: RunnerFailureParams) -> str:
    failure_type = str(params.result.status or "").strip().upper()
    if failure_type not in {"BLOCKED", "TIMEOUT"}:
        return params.effective_instruction

    # Set pending work flag
    params.agent._has_pending_work = True

    # Inject relevant memories (push mode)
    effective_instruction = _inject_failure_memories(params, failure_type)

    # LLM introspection: analyze failure reason and suggest parameter adjustments
    params.agent._handle_failure_introspection(params.run_id, params.before, params.result)

    return effective_instruction


# LLM: _inject_failure_memories 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理inject失败memories相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _inject_failure_memories(params: RunnerFailureParams, failure_type: str) -> str:
    effective_instruction = params.effective_instruction
    try:
        from ..memory_push import format_memories_for_injection, push_relevant_memories

        task_context = {
            "task_id": params.run_id,
            "goal": getattr(params.before, "goal", ""),
            "failure_type": failure_type.lower(),
        }
        relevant_memories = push_relevant_memories(
            params.agent, failure_type.lower(), task_context, limit=3
        )
        if relevant_memories:
            memory_hint = format_memories_for_injection(relevant_memories)
            return _append_memory_hint(effective_instruction, memory_hint)
    except Exception:
        pass  # Memory injection failure doesn't affect main flow
    return effective_instruction


# LLM: _append_memory_hint 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入记忆hint的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_memory_hint(effective_instruction: str, memory_hint: str) -> str:
    if effective_instruction:
        return f"{effective_instruction}\n\n{memory_hint}"
    return memory_hint
