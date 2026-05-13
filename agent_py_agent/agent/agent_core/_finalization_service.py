# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import time as time_module

from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
    run_memory_compact_auto_cycle,
    write_recovery_snapshot,
)
from ..memory_archive.compact import MemoryCompactPlanOptions
from ..memory_archive.compact_auto import MemoryCompactAutoCycleOptions
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.runtime_fact_source import RuntimeFactSourceRequest, write_runtime_fact_source
from ..memory_archive.snapshots import (
    RecoverySnapshotInput,
)
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from ..user_space.run_workspace import EnsureRunWorkspaceRequest, ensure_run_workspace
from ._runtime_params import (
    ArchiveRunParams,
    EstimateTokenParams,
    FinalizeContext,
    WriteRecoverySnapshotParams,
)
from .models import AgentRunResult


# LLM: FinalizationService 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装收尾服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class FinalizationService:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent):
        self._agent = agent

    # LLM: finalize 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理finalize相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def finalize(self, ctx: FinalizeContext):
        assert ctx.final_response is not None
        run_request_id = ctx.request_id or f"run-{time_module.time_ns()}"

        archive_params = ArchiveRunParams(
            do_save=ctx.do_save,
            user_prompt=ctx.user_prompt,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            source=ctx.source,
        )
        archive_result = self._archive_run_if_needed(archive_params)
        runtime_fact_source = self._write_runtime_fact_source_if_needed(ctx, run_request_id)

        recovery_params = WriteRecoverySnapshotParams(
            do_save=ctx.do_save,
            recovery_snapshot=ctx.recovery_snapshot,
            user_prompt=ctx.user_prompt,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            source=ctx.source,
            recovery_task_refs=ctx.recovery_task_refs,
            recovery_content_paths=_recovery_content_paths(ctx, runtime_fact_source),
            recovery_next_actions=ctx.recovery_next_actions,
            routed_context=ctx.routed_context,
        )
        snapshot_result = self._write_recovery_snapshot_if_needed(recovery_params)
        turn_id = run_request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
        token_params = EstimateTokenParams(
            user_prompt=ctx.user_prompt,
            runtime_injections=ctx.runtime_injections,
            memories=ctx.memories,
            final_response=ctx.final_response,
            archive_tool_calls=ctx.archive_tool_calls,
            run_request_id=run_request_id,
            turn_id=turn_id,
        )
        token_ledger = self._estimate_token_usage(token_params)

        return self._build_agent_run_result(ctx, archive_result, snapshot_result, token_ledger)

    # LLM: _write_runtime_fact_source_if_needed makes real run facts visible to later compact apply.
    # 函数用途: 保存真实 run 的显式验收、约束和测试事实源，并把目录交给 recovery snapshot。
    def _write_runtime_fact_source_if_needed(self, ctx: FinalizeContext, run_request_id: str) -> str:
        if not ctx.do_save:
            return ""
        return write_runtime_fact_source(
            RuntimeFactSourceRequest(
                root=self._agent.root,
                request_id=run_request_id,
                user_prompt=ctx.user_prompt,
                response_text=ctx.final_response.text,
                backend=ctx.final_response.backend,
                status="ok",
                next_actions=ctx.recovery_next_actions or [],
                archive_tool_calls=ctx.archive_tool_calls or [],
            )
        )

    # LLM: _archive_run_if_needed 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入ifneeded的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _archive_run_if_needed(self, params: ArchiveRunParams):
        if not params.do_save:
            return None
        _write_run_task_workspace_if_needed(self._agent, params)
        self._agent.memory.add("user", params.user_prompt)
        self._agent.memory.add(
            "agent", params.final_response.text, tags=[params.final_response.backend]
        )
        return archive_run_turn(
            ArchiveRunTurnParams(
                root=self._agent.root,
                ctx=ArchiveTurnContext(
                    session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                    request_id=params.run_request_id,
                    run_id=params.run_id,
                    task_id=params.task_id,
                    user_prompt=params.user_prompt,
                    response_text=params.final_response.text,
                    backend=params.final_response.backend,
                    tool_calls=params.archive_tool_calls or [],
                    source=params.source,
                    archive_level=int(getattr(self._agent.config, "memory_archive_level", 3)),
                    preview_limits=_memory_archive_preview_limits(self._agent.config),
                    summary_chars=int(getattr(self._agent.config, "memory_archive_summary_chars", 96) or 96),
                ),
            )
        )

    # LLM: _write_recovery_snapshot_if_needed 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入恢复snapshotifneeded的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _write_recovery_snapshot_if_needed(self, params: WriteRecoverySnapshotParams):
        should_write = bool(getattr(self._agent.config, "memory_hook_enabled", True)) and (
            params.do_save if params.recovery_snapshot is None else bool(params.recovery_snapshot)
        )
        if not should_write:
            return None
        return write_recovery_snapshot(
            self._agent.root,
            params=RecoverySnapshotInput(
                session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                user_prompt=params.user_prompt,
                response_text=params.final_response.text,
                backend=params.final_response.backend,
                source=params.source,
                request_id=params.run_request_id,
                run_id=params.run_id,
                task_id=params.task_id,
                status="ok",
                tool_calls=params.archive_tool_calls,
                task_refs=params.recovery_task_refs or [],
                content_paths=[
                    *(params.recovery_content_paths or []),
                    *(getattr(params.routed_context, "required_read_paths", None) or []),
                    *(getattr(params.routed_context, "candidate_paths", None) or []),
                ],
                next_actions=params.recovery_next_actions or [],
                archive_level=int(getattr(self._agent.config, "memory_hook_archive_level", 3)),
            ),
        )

    # LLM: _estimate_token_usage 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 计算令牌usage的预算、数量或限制，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _estimate_token_usage(self, params: EstimateTokenParams):
        input_tokens = (
            estimate_tokens(params.user_prompt)
            + estimate_tokens(params.runtime_injections)
            + estimate_tokens([getattr(memory, "content", "") for memory in params.memories])
        )
        output_tokens = estimate_tokens(params.final_response.text)
        tool_tokens = estimate_tokens(params.archive_tool_calls)
        ledger = append_session_token_usage(
            self._agent.root,
            usage=TurnTokenUsage(
                session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                turn_id=params.turn_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_tokens=tool_tokens,
                created_at=str(time_module.time()),
            ),
        )
        return {
            "turn": int(ledger["turn_total"]),
            "cumulative": int(ledger["cumulative_tokens"]),
        }

    # LLM: _build_agent_run_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建agentrun结果所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _build_agent_run_result(
        self, ctx: FinalizeContext, archive_result, snapshot_result, token_ledger
    ):
        routed_context = ctx.routed_context
        return AgentRunResult(
            prompt=ctx.final_prompt,
            response=ctx.final_response.text,
            backend=ctx.final_response.backend,
            used_memories=len(ctx.memories),
            tool_rounds=ctx.tool_rounds,
            executed_tools=ctx.executed_tools,
            memory_route_matches=len(routed_context.matches),
            memory_route_paths=[
                *routed_context.required_read_paths,
                *routed_context.candidate_paths,
            ],
            archive_events=archive_result.event_count if archive_result else 0,
            archive_token_estimate=archive_result.token_estimate if archive_result else 0,
            prompt_token_estimate=estimate_tokens(ctx.final_prompt),
            runtime_injection_token_estimate=estimate_tokens(ctx.runtime_injections)
            if ctx.runtime_injections
            else 0,
            **_snapshot_result_fields(snapshot_result),
            **_resume_context_fields(ctx),
            compression_snapshot_id=ctx.compression_snapshot_id,
            compression_snapshot_path=ctx.compression_snapshot_path,
            compression_applied=ctx.compression_applied,
            turn_token_estimate=token_ledger["turn"],
            cumulative_token_estimate=token_ledger["cumulative"],
            **_compact_auto_cycle_fields(self._agent, ctx, token_ledger),
        )


# LLM: _recovery_content_paths appends runtime fact source refs without mutating FinalizeContext.
# 函数用途: 合并调用方 recovery_content_paths 和本轮 run fact 目录，供 hook snapshot 记录恢复入口。
def _recovery_content_paths(ctx: FinalizeContext, runtime_fact_source: str) -> list[str]:
    paths = list(ctx.recovery_content_paths or [])
    if runtime_fact_source:
        paths.append(runtime_fact_source)
    return paths


# LLM: _write_run_task_workspace_if_needed gives saved runs a clean home task folder without changing legacy archive paths.
# 函数用途: 在主代理 run 保存时创建 home/workspace/tasks/date/task 的产物区、运行区和 refs-only 状态文件。
def _write_run_task_workspace_if_needed(agent, params: ArchiveRunParams) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return ""
    result = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=home_paths.root,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=params.task_id or params.run_id or params.run_request_id or params.user_prompt,
            user_prompt=params.user_prompt,
            request_id=params.run_request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            source=params.source,
        )
    )
    return str(result.root)


# LLM: _snapshot_result_fields 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理snapshot结果字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _snapshot_result_fields(snapshot_result) -> dict:
    # LLM: snapshot result projection is kept outside AgentRunResult assembly.
    return {
        "recovery_snapshot_id": snapshot_result.snapshot_id if snapshot_result else "",
        "recovery_snapshot_path": snapshot_result.path if snapshot_result else "",
        "recovery_snapshot_error": snapshot_result.error if snapshot_result else "",
        "recovery_snapshot_token_estimate": snapshot_result.token_estimate if snapshot_result else 0,
    }


# LLM: _resume_context_fields 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理恢复上下文字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _resume_context_fields(ctx: FinalizeContext) -> dict:
    resume = ctx.resume_context_result
    return {
        "memory_resume_context_injected": resume.injected if resume else False,
        "memory_resume_context_query": resume.query if resume else "",
        "memory_resume_context_matches": (
            resume.archive_match_count + resume.local_match_count + resume.task_fact_source_count
        )
        if resume
        else 0,
        "memory_resume_context_token_estimate": estimate_tokens(resume.context_block)
        if resume and resume.injected
        else 0,
        "memory_resume_context_error": resume.error if resume else "",
    }


# LLM: _compact_auto_cycle_fields honors do_save before any opt-in compact apply write.
# 函数用途: 在 run 收尾时触发自动 compact/resume 协调器；`save=False` 时即使配置允许也只做计划，不写 apply 产物。
def _compact_auto_cycle_fields(agent, ctx: FinalizeContext, token_ledger: dict[str, int]) -> dict:
    allow_apply = ctx.do_save and bool(getattr(agent.config, "memory_compact_auto_allow_apply", False))
    cycle = run_memory_compact_auto_cycle(
        agent.root,
        MemoryCompactAutoCycleOptions(
            current_tokens=int(token_ledger["cumulative"]),
            max_context_tokens=_compact_context_window_tokens(agent),
            plan_options=MemoryCompactPlanOptions(
                session_id=getattr(agent, "session_id", agent.config.agent_name),
                request_id=ctx.request_id or "",
                run_id=ctx.run_id or "",
                task_id=ctx.task_id or "",
            ),
            allow_apply=allow_apply,
        ),
    )
    suggestion = cycle["suggestion"]
    return {
        "memory_compact_suggested": bool(suggestion["should_prompt"]),
        "memory_compact_status": str(suggestion["status"]),
        "memory_compact_ratio": float(suggestion["token_budget"]["ratio"]),
        "memory_compact_message": str(suggestion["message"]),
        "memory_compact_commands": list(suggestion["recommended_commands"]),
        "memory_compact_auto_status": str(cycle["status"]),
        "memory_compact_auto_next_action": str(cycle["next_action"]),
        "memory_compact_auto_allowed_to_continue": bool(cycle["allowed_to_continue"]),
        "memory_compact_auto_tool_execution": str(cycle["automatic_tool_execution"]),
        "memory_compact_auto_apply_id": str(cycle["apply_id"]),
        "memory_compact_auto_continue_ready": bool(cycle["continue_packet"].get("ready_to_continue")),
    }


# LLM: _compact_context_window_tokens keeps compact suggestions conservative until real context windows exist.
# 函数用途: 读取可选配置 memory_compact_context_window_tokens；未设置时用 max_tokens 的保守倍数估算窗口。
def _compact_context_window_tokens(agent) -> int:
    configured = int(getattr(agent.config, "memory_compact_context_window_tokens", 0) or 0)
    if configured > 0:
        return configured
    max_tokens = int(getattr(agent.config, "max_tokens", 1024) or 1024)
    return max(8192, max_tokens * 16)


# LLM: _memory_archive_preview_limits centralizes archive preview sizing so callers do not bake defaults.
# 函数用途: 从 AgentConfig 读取 raw archive 各 archive_level 的预览字符数，供归档事件构建复用。
def _memory_archive_preview_limits(config) -> dict[int, int]:
    return {
        0: int(getattr(config, "memory_archive_preview_level_0_chars", 2048) or 0),
        1: int(getattr(config, "memory_archive_preview_level_1_chars", 1024) or 0),
        2: int(getattr(config, "memory_archive_preview_level_2_chars", 512) or 0),
        3: int(getattr(config, "memory_archive_preview_level_3_chars", 160) or 0),
    }
