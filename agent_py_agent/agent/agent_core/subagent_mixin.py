# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import logging

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import (
    RecordRunnerResultParams,
    SubAgentRunnerResult,
    SubAgentTask,
    parse_subagent_runner_output,
)
from ._subagent_planner_mixin import RunParentPlannerParams, _ParentPlannerMixin
from ._subagent_repair_mixin import (
    RecoverySnapshotParams,
    SubagentRepairParams,
    _SubagentRepairMixin,
)
from .planner import _build_parent_planner_state
from .runner_prompts import (
    _build_subagent_runner_prompt,
)
from .subagent_finalize_helpers import (
    FinalizedRecoverySnapshotRequest,
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
    write_finalized_recovery_snapshot,
)
from .subagent_params import (
    SpawnSubagentsParams,
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
    spawn_subagents_params,
    subagent_run_params,
)
from .subagent_run_flow import run_subagent_flow
from .subagent_spawn_flow import (
    SpawnSubagentsFlowRequest,
    spawn_subagents_flow,
)

logger = logging.getLogger(__name__)


# LLM: _config_workflow_dispatch_mode 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理config工作流调度mode相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _config_workflow_dispatch_mode(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "auto":
            return "auto"
        if normalized == "manual":
            return "plan"
    return "off"


# LLM: _initial_repair_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理initialrepair状态相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _initial_repair_state(result) -> dict[str, object]:
    return {
        "prompt_for_log": result.prompt,
        "response_for_log": result.response,
        "backend_name": result.backend,
        "message": "runner 已完成模型调用，等待独立验收。",
        "attempted": False,
        "ok": False,
        "error": "",
    }


# LLM: _tuple_repair_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理tuplerepair状态相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _tuple_repair_state(value: tuple) -> dict[str, object]:
    structured, ok, error, backend_name, prompt_for_log, response_for_log, message = value
    return {
        "structured": structured,
        "prompt_for_log": prompt_for_log,
        "response_for_log": response_for_log,
        "backend_name": backend_name,
        "message": message,
        "attempted": True,
        "ok": ok,
        "error": error,
    }


# LLM: _SubagentLifecycleBase 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装子代理生命周期基础相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class _SubagentLifecycleBase:

    # LLM: spawn_subagents 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理spawn子代理相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def spawn_subagents(
        self,
        goal: str | None = None,
        count: int | None = None,
        *,
        params: SpawnSubagentsParams | None = None,
    ) -> list[SubAgentTask]:

        if not self.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")

        options = spawn_subagents_params(
            params,
            goal=goal,
            count=count,
        )
        workflow_mode = _config_workflow_dispatch_mode(self.config.subagent_workflow_mode)
        return spawn_subagents_flow(
            SpawnSubagentsFlowRequest(agent=self, options=options, workflow_mode=workflow_mode)
        )

    # LLM: run_subagent 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进子代理的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def run_subagent(
        self,
        run_id: str | None = None,
        *,
        params: SubagentRunParams | None = None,
        instruction: str = "",
        dry_run: bool = True,
        max_cards: int = 0,
        probe: bool = True,
        retry_reason: str = "",
        attempt_id: str = "",
    ) -> SubAgentRunnerResult:
        options = subagent_run_params(
            params,
            run_id=run_id,
            instruction=instruction,
            dry_run=dry_run,
            max_cards=max_cards,
            probe=probe,
            retry_reason=retry_reason,
            attempt_id=attempt_id,
        )
        return run_subagent_flow(self, options)

    # LLM: _prepare_subagent_attempt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理prepare子代理attempt相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _prepare_subagent_attempt(self, run_id, *, dry_run, active_attempt_id, retry_reason):
        if dry_run or active_attempt_id:
            return active_attempt_id
        prepared = self.subagents.prepare_runner_attempt(run_id, retry_reason=retry_reason)
        return prepared.runner_active_attempt_id

    # LLM: _build_subagent_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建子代理提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _build_subagent_prompt(self, run_id, max_cards, instruction):
        context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
        prompt = _build_subagent_runner_prompt(context, instruction)
        return context, prompt

    # LLM: _record_subagent_dry_run 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入子代理dryrun的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _record_subagent_dry_run(self, run_id, active_attempt_id, prompt):
        return self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=run_id,
                attempt_id=active_attempt_id,
                dry_run=True,
                ok=True,
                message="dry-run: 已生成执行上下文和 runner prompt，未调用模型。",
                prompt=prompt,
            )
        )

    # LLM: _probe_subagent_channel 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理probe子代理通道相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _probe_subagent_channel(self, params: SubagentProbeParams):
        if not params.probe:
            return None
        probe_result = self.subagents.probe_channel(params.run_id)
        if probe_result.channel_status != "BROKEN":
            return None
        context, prompt = self._build_subagent_prompt(
            params.run_id, params.max_cards, params.instruction
        )
        return self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=params.active_attempt_id,
                dry_run=False,
                ok=False,
                message="通道健康检查为 BROKEN，未启动模型执行。",
                prompt=prompt,
                status="CHANNEL_ERROR",
                verification_status="UNVERIFIED",
                failure_type="channel",
            )
        )

    # LLM: _handle_subagent_run_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进子代理run失败的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _handle_subagent_run_failure(self, params: SubagentRunFailureParams):
        failed_result = self.subagents.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                attempt_id=params.active_attempt_id,
                dry_run=False,
                ok=False,
                message=f"runner 执行失败: {params.exc}",
                prompt=params.prompt,
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_error",
            )
        )
        self._write_subagent_recovery_snapshot(
            params=RecoverySnapshotParams(
                run_id=params.run_id,
                user_prompt=params.context.goal,
                response_text=failed_result.message,
                backend="",
                status=failed_result.status,
                error_code=failed_result.runner_last_error or "runner_error",
                tool_calls=[],
            )
        )
        return failed_result

    # LLM: _finalize_subagent_run 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理finalize子代理run相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _finalize_subagent_run(self, params: SubagentFinalizeParams):
        structured = parse_subagent_runner_output(params.result.response)
        repair_state = _initial_repair_state(params.result)
        if not (structured.found and structured.ok):
            repair_state = self._handle_subagent_repair(
                SubagentRepairParams(
                    context=params.context,
                    result=params.result,
                    structured=structured,
                    prompt_for_log=repair_state["prompt_for_log"],
                    response_for_log=repair_state["response_for_log"],
                    backend_name=repair_state["backend_name"],
                    message=repair_state["message"],
                )
            )
            structured = repair_state[0]
            repair_state = _tuple_repair_state(repair_state)

        runner_result = record_finalized_runner_result(
            FinalizedRunnerRecordRequest(self, params, structured, repair_state)
        )
        write_finalized_recovery_snapshot(FinalizedRecoverySnapshotRequest(self, params, runner_result, repair_state))
        return runner_result

# LLM: SimpleAgentSubagentMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分simpleagent子代理混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SimpleAgentSubagentMixin(
    _SubagentLifecycleBase,
    _SubagentRepairMixin,
    _ParentPlannerMixin,
):
    pass


# Re-export for backward compatibility
from ._subagent_planner_mixin import RunParentPlannerParams  # noqa: E402
