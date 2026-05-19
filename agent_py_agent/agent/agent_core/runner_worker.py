# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""Worker execution helpers for runner dispatch."""

import threading
from dataclasses import dataclass
from pathlib import Path

from ..backends import ModelResponse
from ..config import AgentConfig
from ..subagent import RecordRunnerResultParams, SubAgentRunnerResult
from ..subagents.artifact_integrity_repair_worker import maybe_run_artifact_integrity_repair_worker
from ..subagents.parsing import parse_subagent_runner_output
from .subagent_params import SubagentRunParams
from .subagent_progress_closeout import subagent_progress_timeout_closeout_response


# LLM: RunSubagentWorkerParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存run子代理工作器参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunSubagentWorkerParams:
    config: AgentConfig
    root: Path
    run_id: str
    instruction: str
    dry_run: bool
    max_cards: int
    probe: bool
    retry_reason: str
    timeout_seconds: float = 0.0
    local_store: object | None = None
    backend_override: object | None = None


# LLM: _run_subagent_worker 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进子代理工作器的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_subagent_worker(params: RunSubagentWorkerParams) -> SubAgentRunnerResult:
    from ..core import SimpleAgent

    worker = SimpleAgent(params.config, params.root)
    if params.backend_override is not None:
        worker.backend = params.backend_override
    _attach_worker_local_store(worker, params.local_store)
    deterministic_result = _run_deterministic_worker_if_supported(worker, params)
    if deterministic_result is not None:
        return deterministic_result
    if params.dry_run or params.timeout_seconds <= 0:
        return worker.run_subagent(
            params=SubagentRunParams(
                run_id=params.run_id,
                instruction=params.instruction,
                dry_run=params.dry_run,
                max_cards=params.max_cards,
                probe=params.probe,
                retry_reason=params.retry_reason,
            )
        )
    return _run_subagent_worker_with_timeout(worker, params)


# LLM: _run_deterministic_worker_if_supported is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _run_deterministic_worker_if_supported(worker, params: RunSubagentWorkerParams) -> SubAgentRunnerResult | None:
    if params.dry_run:
        return None
    try:
        task = worker.subagents.load(params.run_id)
    except (FileNotFoundError, KeyError):
        return None
    if (getattr(task, "attributes", {}) or {}).get("repair_kind") != "artifact_integrity":
        return None
    prepared = worker.subagents.prepare_runner_attempt(
        params.run_id, retry_reason=params.retry_reason
    )
    return maybe_run_artifact_integrity_repair_worker(
        worker.subagents,
        params.run_id,
        attempt_id=prepared.runner_active_attempt_id,
    )


# LLM: _attach_worker_local_store 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理attach工作器local存储相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _attach_worker_local_store(worker, local_store: object | None) -> None:
    if local_store is None:
        return
    worker.local_store = local_store
    worker.subagents.local_store = local_store
    worker.memory.local_store = local_store


# LLM: _run_subagent_worker_with_timeout 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进子代理工作器超时的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_subagent_worker_with_timeout(worker, params: RunSubagentWorkerParams):
    prepared = worker.subagents.prepare_runner_attempt(
        params.run_id, retry_reason=params.retry_reason
    )
    attempt_id = prepared.runner_active_attempt_id
    payload: dict[str, object] = {}

    # LLM: _target 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理target相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _target() -> None:
        try:
            payload["result"] = worker.run_subagent(
                params=SubagentRunParams(
                    run_id=params.run_id,
                    instruction=params.instruction,
                    dry_run=False,
                    max_cards=params.max_cards,
                    probe=params.probe,
                    retry_reason=params.retry_reason,
                    attempt_id=attempt_id,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive wrapper
            payload["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(params.timeout_seconds)
    if thread.is_alive():
        timeout_message = f"runner timed out after {params.timeout_seconds:.2f}s"
        progress_response = subagent_progress_timeout_closeout_response(
            worker,
            params.run_id,
            ModelResponse(text="", backend="progress_timeout_closeout"),
        )
        timeout_result = (
            _record_progress_timeout_closeout(worker, params.run_id, attempt_id, timeout_message, progress_response)
            if progress_response is not None
            else worker.subagents.record_runner_result(
                RecordRunnerResultParams(
                    run_id=params.run_id,
                    attempt_id=attempt_id,
                    dry_run=False,
                    ok=False,
                    message=timeout_message,
                    status="TIMEOUT",
                    verification_status="UNVERIFIED",
                    failure_type="runner_timeout",
                )
            )
        )
        worker.subagents.abandon_runner_attempt(params.run_id, attempt_id, reason=timeout_message)
        return timeout_result
    if "error" in payload:
        raise payload["error"]  # type: ignore[misc]
    return payload["result"]  # type: ignore[return-value]


# LLM: _record_progress_timeout_closeout records recovered progress as structured output, not raw timeout.
# 函数用途: 超时线程仍会被废弃，但已落盘的 task-local 进度可进入验收/修复状态机。
def _record_progress_timeout_closeout(worker, run_id: str, attempt_id: str, timeout_message: str, response: ModelResponse):
    structured = parse_subagent_runner_output(response.text)
    return worker.subagents.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=True,
            message=f"{timeout_message}; recovered task-local progress closeout",
            response=response.text,
            backend=response.backend,
            structured_output=structured,
        )
    )
