# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""LLM contract: SubAgentLifecycleMixin - thin facade delegating to lifecycle service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
内部已委托给 services/lifecycle.py 中的 SubAgentLifecycleService。
本文件只做薄包装，保持向后兼容。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .services.lifecycle import (
    RecordCapabilityGapParams,
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
    SetStatusParams,
    SubAgentLifecycleService,
)

# LLM: 生命周期门面接收参数包，同时保留旧管理器入口兼容性。
if TYPE_CHECKING:
    from ..local_store import LocalStore


# LLM: SubAgentLifecycleMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent生命周期混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentLifecycleMixin:
    """Thin facade for lifecycle operations delegating to SubAgentLifecycleService."""

    # LLM: _lifecycle_service 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理生命周期服务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _lifecycle_service(self):
        """Lazily get or create the lifecycle service."""

        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is None:
            lifecycle = SubAgentLifecycleService(self)
            self.lifecycle = lifecycle
        return lifecycle

    # LLM: record_capability_request 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入能力请求的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_capability_request(
        self,
        run_id: str,
        params: RecordCapabilityRequestParams,
    ):
        """Record a capability request on a subagent task."""

        return self._lifecycle_service().record_capability_request(run_id, params)

    # LLM: record_capability_grant 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入能力grant的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_capability_grant(
        self,
        run_id: str,
        params: RecordCapabilityGrantParams,
    ):
        """Record a capability grant on a subagent task."""
        return self._lifecycle_service().record_capability_grant(run_id, params=params)

    # LLM: record_capability_gap 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入能力缺口的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_capability_gap(
        self,
        run_id: str,
        params: RecordCapabilityGapParams,
    ):
        """Record a capability gap on a subagent task."""
        return self._lifecycle_service().record_capability_gap(run_id, params=params)

    # LLM: record_evidence 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入证据的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def record_evidence(
        self,
        run_id: str,
        params: RecordEvidenceParams,
    ):
        """Record verification evidence on a subagent task."""

        return self._lifecycle_service().record_evidence(run_id, params)

    # LLM: touch_heartbeat 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理touchheartbeat相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def touch_heartbeat(self, run_id: str) -> None:
        """Refresh subagent heartbeat timestamp."""

        self._lifecycle_service().touch_heartbeat(run_id)

    # LLM: set_status 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 更新状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def set_status(
        self,
        params: str | SetStatusParams,
        status: str = "",
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ):
        """Update task status with optional evidence requirement."""

        return self._lifecycle_service().set_status(
            params,
            status,
            result=result,
            failure_type=failure_type,
            require_evidence=require_evidence,
        )

    # LLM: prepare_runner_attempt 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理prepare执行器attempt相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def prepare_runner_attempt(self, run_id: str, *, retry_reason: str = ""):
        """Prepare task for runner execution (set RUNNING, reset verification)."""

        from .services.recovery_strategy import (
            SubagentRecoveryStrategyRequest,
            build_subagent_recovery_strategy,
        )
        from .utils import _new_id

        task = self.load(run_id)
        previous = f"{task.status}/{task.failure_type or 'none'}"
        strategy = build_subagent_recovery_strategy(
            SubagentRecoveryStrategyRequest(
                task=task,
                now=time.time(),
                packet_max_age_seconds=7 * 24 * 60 * 60,
            )
        )
        attempt_id = _new_id("attempt")
        now = time.time()
        task.status = "RUNNING"
        task.verification_status = "UNVERIFIED"
        task.failure_type = ""
        task.ended_at = 0.0
        task.runner_active_attempt_id = attempt_id
        # LLM: start time is recorded before result writeback so boards/recovery know this RUNNING run truly started.
        task.runner_last_attempt_at = now
        task.updated_at = now
        task.heartbeat_at = now
        _record_runner_recovery_preflight(task, strategy, previous)
        self.save(task)
        suffix = f" retry_reason={retry_reason}" if retry_reason else ""
        self._append_task_work_log(
            task,
            f"runner_attempt: start previous={previous} attempt={task.runner_attempts + 1} "
            f"attempt_id={attempt_id}{suffix}",
        )
        return task

    # LLM: abandon_runner_attempt 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理abandon执行器attempt相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def abandon_runner_attempt(self, run_id: str, attempt_id: str, *, reason: str = ""):
        """Mark a runner attempt as abandoned."""

        task = self.load(run_id)
        normalized = str(attempt_id or "").strip()
        if not normalized:
            return task
        if normalized not in task.runner_abandoned_attempt_ids:
            task.runner_abandoned_attempt_ids.append(normalized)
        if task.runner_active_attempt_id == normalized:
            task.runner_active_attempt_id = ""
        task.updated_at = time.time()
        self.save(task)
        if reason:
            self._append_task_work_log(
                task,
                f"runner_attempt: abandon attempt_id={normalized} reason={reason}",
            )
        return task


# LLM: _record_runner_recovery_preflight preserves damaged-packet evidence before save regenerates packet files.
# 函数用途: 在 runner 准备阶段记录启动前恢复包状态；packet 坏/缺/过期时让后续 prompt 仍能说明降级来源。
def _record_runner_recovery_preflight(task, strategy, previous_status: str) -> None:
    if str(getattr(strategy, "packet_status", "") or "") == "ready":
        _clear_runner_recovery_preflight(task)
        return
    attributes = dict(getattr(task, "attributes", {}) or {})
    attributes["runner_recovery_preflight"] = {
        "packet_status": str(getattr(strategy, "packet_status", "") or "unknown"),
        "packet_ref": str(getattr(strategy, "packet_ref", "") or ""),
        "fallback_refs": list(getattr(strategy, "fallback_refs", []) or []),
        "runner_instruction": str(getattr(strategy, "runner_instruction", "") or ""),
        "previous_status": previous_status,
        "observed_at": time.time(),
        "save_may_regenerate_continue_packet": True,
    }
    task.attributes = attributes


# LLM: _clear_runner_recovery_preflight prevents stale warnings from following healthy reruns.
# 函数用途: packet 正常时清理旧的 preflight 降级记录，避免 runner prompt 误报历史问题。
def _clear_runner_recovery_preflight(task) -> None:
    attributes = dict(getattr(task, "attributes", {}) or {})
    if "runner_recovery_preflight" in attributes:
        attributes.pop("runner_recovery_preflight", None)
        task.attributes = attributes
