from __future__ import annotations

"""LLM: selects runner candidates, handles retry policy, creates dispatch records, and runs worker agents.

给人看的解释：
dispatch 阶段不应该把"谁能跑、能不能重试、并发 worker 怎么启动"都塞在一个大函数里。
这个文件专门处理 runner 相关的规则和小工具。
"""

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import AgentConfig
from ..subagent import SubAgentRunnerResult, SubAgentTask
from .dynamic_timeout import calculate_dynamic_timeout

if TYPE_CHECKING:
    from ..core import SimpleAgent


RETRYABLE_RUNNER_FAILURE_TYPES = {
    "runner_error",
    "structured_output_parse_error",
    "tool_result_missing",
    "model_error",
    "api_error",
    "transient_error",
    "runner_timeout",
}


def _runner_max_attempts(policy: str) -> int:
    """把 runner_failure_policy 转成总尝试次数。

    `auto` 第一版等价于"最多 2 次"：初次失败后再补一次机会。
    这里返回的是总尝试次数，不是额外 retry 次数。
    """

    value = str(policy or "auto").strip().lower()
    if value in {"", "auto"}:
        return 2
    if value in {"off", "none", "disabled", "false", "no"}:
        return 1
    try:
        return max(1, int(value))
    except ValueError:
        return 2


def _runner_failure_type(task: SubAgentTask) -> str:
    """标准化 runner failure_type，兼容模型输出大小写。"""

    return str(task.failure_type or "").strip().lower()


def _runner_retry_reason(task: SubAgentTask, runner_max_attempts: int) -> str:
    """判断一个已失败任务是否还能自动重试。"""

    if runner_max_attempts <= 1:
        return ""
    if task.status not in {"BLOCKED", "FAILED", "TIMEOUT"}:
        return ""
    failure_type = _runner_failure_type(task)
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    attempts = max(0, int(task.runner_attempts or 0))
    if attempts >= runner_max_attempts:
        return ""
    return f"failure_type={failure_type}; attempt={attempts + 1}/{runner_max_attempts}"


def _resolve_runner_concurrency(value: object, job_count: int) -> int:
    """把 runner_concurrency 配置转成实际 worker 数。

    `auto` 先保持 1，避免默认并发消耗真实 API；明确写数字时才并行。
    """

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return 1
        try:
            parsed = int(normalized)
        except ValueError:
            return 1
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1
    return max(1, min(parsed, job_count))


def _resolve_runner_start_rate(value: object, job_count: int) -> int:
    """把 runner_start_rate 配置转成本轮最多启动多少个 runner。"""

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return job_count
        try:
            parsed = int(normalized)
        except ValueError:
            return job_count
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return job_count
    return max(0, min(parsed, job_count))


def _resolve_runner_timeout_seconds(value: object) -> float:
    """把 runner_timeout_seconds 配置转成超时秒数；0 表示不启用。"""

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto", "off", "none", "disabled", "false", "no"}:
            return 0.0
        try:
            parsed = float(normalized)
        except ValueError:
            return 0.0
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
    return max(0.0, parsed)


def _run_subagent_worker(
    config: AgentConfig,
    root: Path,
    run_id: str,
    instruction: str,
    dry_run: bool,
    max_cards: int,
    probe: bool,
    retry_reason: str,
    timeout_seconds: float = 0.0,
    local_store: object | None = None,
) -> SubAgentRunnerResult:
    """LLM: run one subagent in an isolated worker SimpleAgent instance.

    给人看的解释：
    并发跑 runner 时，不能多个线程共用同一个 SimpleAgent、backend 或 LocalStore 连接。
    所以这里临时创建一个新的 SimpleAgent，只负责当前 run_id。
    如果传入 local_store，则复用同一个连接，避免 SQLite 并发问题。
    """

    from ..core import SimpleAgent

    worker = SimpleAgent(config, root)
    if local_store is not None:
        worker.local_store = local_store
        worker.subagents.local_store = local_store
        worker.memory.local_store = local_store
    if dry_run or timeout_seconds <= 0:
        return worker.run_subagent(
            run_id,
            instruction=instruction,
            dry_run=dry_run,
            max_cards=max_cards,
            probe=probe,
            retry_reason=retry_reason,
        )

    prepared = worker.subagents.prepare_runner_attempt(run_id, retry_reason=retry_reason)
    attempt_id = prepared.runner_active_attempt_id
    payload: dict[str, object] = {}

    def _target() -> None:
        try:
            payload["result"] = worker.run_subagent(
                run_id,
                instruction=instruction,
                dry_run=False,
                max_cards=max_cards,
                probe=probe,
                retry_reason=retry_reason,
                attempt_id=attempt_id,
            )
        except Exception as exc:  # pragma: no cover - defensive wrapper
            payload["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        timeout_message = f"runner timed out after {timeout_seconds:.2f}s"
        timeout_result = worker.subagents.record_runner_result(
            run_id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=False,
            message=timeout_message,
            status="TIMEOUT",
            verification_status="UNVERIFIED",
            failure_type="runner_timeout",
        )
        worker.subagents.abandon_runner_attempt(run_id, attempt_id, reason=timeout_message)
        return timeout_result
    if "error" in payload:
        raise payload["error"]  # type: ignore[misc]
    return payload["result"]  # type: ignore[return-value]


def _runner_dispatch_record(
    agent: SimpleAgent,
    *,
    run_id: str,
    before: SubAgentTask,
    after: SubAgentTask,
    result: SubAgentRunnerResult,
    retry_reason: str,
    execute_runners: bool,
):
    """把 runner 执行结果转成 dispatch record。"""

    return agent.subagents.make_dispatch_record(
        step="runner",
        action=(
            "retry_runner"
            if retry_reason and execute_runners
            else "execute_runner"
            if execute_runners
            else "runner_dry_run"
        ),
        run_id=run_id,
        dry_run=result.dry_run,
        applied=not result.dry_run,
        ok=result.ok,
        message=result.message,
        before_status=before.status,
        after_status=after.status,
        before_verification_status=before.verification_status,
        after_verification_status=after.verification_status,
        evidence_paths=[
            result.execution_context_json,
            result.result_json,
            result.output_json,
        ],
    )


def _dispatch_runner_candidates(
    tasks: list[SubAgentTask],
    max_runners: int,
    *,
    runner_max_attempts: int = 1,
) -> list[SubAgentTask]:
    """挑选一轮 dispatch 可推进的 runner。"""

    if max_runners <= 0:
        return []
    candidates: list[SubAgentTask] = []
    for task in tasks:
        if not _is_dispatch_runner_candidate(task, runner_max_attempts=runner_max_attempts):
            continue
        candidates.append(task)
        if len(candidates) >= max_runners:
            break
    return candidates


def _limit_items(items: list, limit: int) -> list:
    """按调度 limit 截断列表；0 表示不限制。"""

    if limit <= 0:
        return list(items)
    return list(items)[:limit]


def _is_dispatch_runner_candidate(
    task: SubAgentTask,
    *,
    runner_max_attempts: int = 1,
) -> bool:
    """判断任务是否可以由 dispatch 启动 runner。"""

    if task.status in {
        "AWAITING_ACCEPTANCE",
        "DONE",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }:
        return False
    if task.verification_status in {"NEEDS_ACCEPTANCE", "VERIFIED"}:
        return False
    if task.channel_status == "BROKEN":
        return False
    if any(item.status == "OPEN" for item in task.capability_requests):
        return False
    if any(item.status == "OPEN" for item in task.capability_gaps):
        return False
    if task.status == "BLOCKED":
        if _runner_failure_type(task) == "capability_request" and bool(task.capability_grants):
            return True
        return bool(_runner_retry_reason(task, runner_max_attempts))
    if task.status in {"FAILED", "TIMEOUT"}:
        return bool(_runner_retry_reason(task, runner_max_attempts))
    return task.status in {"PLANNING", "RUNNING"}


def _dispatch_patch_review_run_ids(tasks: list[SubAgentTask]) -> list[str]:
    """挑选本轮调度需要审核 patch 的 run。"""

    run_ids: list[str] = []
    for task in tasks:
        if task.status != "AWAITING_ACCEPTANCE" and task.verification_status != "NEEDS_ACCEPTANCE":
            continue
        if _task_has_runner_patches(task):
            run_ids.append(task.id)
    return run_ids


def _task_has_runner_patches(task: SubAgentTask) -> bool:
    """读取 output.json 判断是否有 patch 记录。"""

    try:
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload.get("patches"), list) and bool(payload.get("patches"))
