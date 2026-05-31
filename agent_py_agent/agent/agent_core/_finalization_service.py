# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from pathlib import Path

from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
)
from ..memory_archive.runtime.turn_archiver import ArchiveRunTurnParams, ArchiveTurnContext
from ..memory_archive.runtime_fact_source import RuntimeFactSourceRequest, write_runtime_fact_source
from ..memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from ..user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    update_main_context_bundle_artifacts,
)
from ..user_space.run_workspace import EnsureRunWorkspaceRequest, ensure_run_workspace
from ._runtime_params import (
    ArchiveRunParams,
    EstimateTokenParams,
    FinalizeContext,
)
from .finalization_compact_auto import compact_auto_cycle_fields
from .model_usage import input_token_usage, output_token_usage
from .models import AgentRunResult
from .runtime_owner_roots import runtime_archive_roots


# LLM: BuildAgentRunResultParams keeps final AgentRunResult assembly inputs bundled and extensible.
# 类用途: 保存收尾阶段组装 AgentRunResult 需要的归档、快照、token 和 request_id 字段。
@dataclass(frozen=True)
class BuildAgentRunResultParams:
    ctx: FinalizeContext
    archive_result: object
    token_ledger: dict[str, int]
    run_request_id: str


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
        self._write_runtime_fact_source_if_needed(ctx, run_request_id)
        self._update_main_context_bundle_artifacts(ctx, run_request_id)
        token_ledger = self._estimate_token_usage(_estimate_token_params(ctx, run_request_id))

        return self._build_agent_run_result(
            BuildAgentRunResultParams(ctx, archive_result, token_ledger, run_request_id)
        )

    # LLM: _write_runtime_fact_source_if_needed makes real run facts visible to later compact apply.
    # 函数用途: 保存真实 run 的显式验收、约束和测试事实源，并把目录交给 recovery snapshot。
    def _write_runtime_fact_source_if_needed(self, ctx: FinalizeContext, run_request_id: str) -> str:
        if not ctx.do_save:
            return ""
        written = ""
        for root in runtime_archive_roots(self._agent):
            written = write_runtime_fact_source(
                RuntimeFactSourceRequest(
                    root=root,
                    request_id=run_request_id,
                    user_prompt=ctx.user_prompt,
                    response_text=ctx.final_response.text,
                    backend=ctx.final_response.backend,
                    status="ok",
                    next_actions=ctx.recovery_next_actions or [],
                    archive_tool_calls=ctx.archive_tool_calls or [],
                    runtime_injections=tuple(str(item) for item in ctx.runtime_injections or []),
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                    source=ctx.source,
                    phase="final",
                    tool_rounds=ctx.tool_rounds,
                    executed_tools=list(ctx.executed_tools or []),
                    latest_archive_refs=_latest_archive_refs(ctx.archive_tool_calls or []),
                    artifact_refs=_artifact_refs(ctx.archive_tool_calls or []),
                )
            )
        return written

    # LLM: _update_main_context_bundle_artifacts links post-tool artifact refs back to the root run card.
    # 函数用途: run 收尾时把同 scope 的工具输出 artifact refs 写回 context bundle；失败不阻断主流程。
    def _update_main_context_bundle_artifacts(self, ctx: FinalizeContext, run_request_id: str) -> None:
        if not ctx.do_save or not ctx.main_context_bundle_path:
            return
        update_main_context_bundle_artifacts(
            MainContextBundleArtifactUpdateRequest(
                context_bundle_path=ctx.main_context_bundle_path,
                workspace_root=self._agent.root,
                request_id=ctx.request_id or run_request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
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
        result = None
        for root in runtime_archive_roots(self._agent):
            result = archive_run_turn(
                ArchiveRunTurnParams(
                    root=root,
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
                ),
            )
        return result

    # LLM: _estimate_token_usage 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 计算令牌usage的预算、数量或限制，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _estimate_token_usage(self, params: EstimateTokenParams):
        input_tokens = input_token_usage(params.final_response)
        if input_tokens is None:
            input_tokens = (
                estimate_tokens(params.user_prompt)
                + estimate_tokens(params.runtime_injections)
                + estimate_tokens([getattr(memory, "content", "") for memory in params.memories])
            )
        output_tokens = output_token_usage(params.final_response)
        if output_tokens is None:
            output_tokens = estimate_tokens(params.final_response.text)
        tool_tokens = estimate_tokens(params.archive_tool_calls)
        ledger = {}
        for root in runtime_archive_roots(self._agent):
            ledger = append_session_token_usage(
                root,
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
            "active": int(input_tokens) + int(output_tokens) + int(tool_tokens),
        }

    # LLM: _build_agent_run_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建agentrun结果所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _build_agent_run_result(self, params: BuildAgentRunResultParams):
        ctx = params.ctx
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
            archive_events=params.archive_result.event_count if params.archive_result else 0,
            archive_token_estimate=params.archive_result.token_estimate if params.archive_result else 0,
            prompt_token_estimate=estimate_tokens(ctx.final_prompt),
            runtime_injection_token_estimate=estimate_tokens(ctx.runtime_injections)
            if ctx.runtime_injections
            else 0,
            **_snapshot_result_fields(),
            **_resume_context_fields(ctx),
            compression_snapshot_id=ctx.compression_snapshot_id,
            compression_snapshot_path=ctx.compression_snapshot_path,
            compression_applied=ctx.compression_applied,
            turn_token_estimate=params.token_ledger["turn"],
            cumulative_token_estimate=params.token_ledger["cumulative"],
            main_context_bundle_path=ctx.main_context_bundle_path,
            main_context_bundle_markdown_path=ctx.main_context_bundle_markdown_path,
            runtime_status=str(getattr(ctx.final_response, "runtime_status", "ok") or "ok"),
            runtime_reason=str(getattr(ctx.final_response, "runtime_reason", "") or ""),
            **compact_auto_cycle_fields(self._agent, ctx, params.token_ledger, request_id=params.run_request_id),
        )


# LLM: _estimate_token_params keeps FinalizationService.finalize focused on lifecycle ordering.
# 函数用途: 组装 token 估算参数包，保持 turn_id 生成规则和调用方解耦。
def _estimate_token_params(ctx: FinalizeContext, run_request_id: str) -> EstimateTokenParams:
    turn_id = run_request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
    return EstimateTokenParams(
        user_prompt=ctx.user_prompt,
        runtime_injections=ctx.runtime_injections,
        memories=ctx.memories,
        final_response=ctx.final_response,
        archive_tool_calls=ctx.archive_tool_calls,
        run_request_id=run_request_id,
        turn_id=turn_id,
    )


# LLM: _write_run_task_workspace_if_needed gives saved runs a clean home task folder without changing legacy archive paths.
# 函数用途: 在主代理 run 保存时创建 home/tasks/date/task 的 output 交付区、work 过程区和 refs-only 状态文件。
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
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home and Path(owner_home) != Path(home_paths.root):
        ensure_run_workspace(
            EnsureRunWorkspaceRequest(
                home=owner_home,
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
def _snapshot_result_fields() -> dict:
    # LLM: recovery_snapshot is legacy; runtime compact now relies on runtime_fact/raw archive/compact refs.
    return {
        "recovery_snapshot_id": "",
        "recovery_snapshot_path": "",
        "recovery_snapshot_error": "",
        "recovery_snapshot_token_estimate": 0,
    }


# LLM: _latest_archive_refs extracts recent raw archive refs for runtime_fact without reading their contents.
# 函数用途: 从工具归档记录里收集最近 raw_archive_path，供 compact/resume 做事实源定位。
def _latest_archive_refs(records: list[object]) -> list[str]:
    return [
        str(record.get("raw_archive_path") or "")
        for record in records[-20:]
        if isinstance(record, dict) and str(record.get("raw_archive_path") or "")
    ]


# LLM: _artifact_refs extracts artifact-like refs from tool records for runtime_fact.
# 函数用途: 收集工具记录里的 artifact/path 引用，帮助恢复时找到产物或大工具输出。
def _artifact_refs(records: list[object]) -> list[str]:
    keys = ("artifact_ref", "artifact_path", "output_artifact_ref", "raw_archive_path")
    refs: list[str] = []
    for record in records[-20:]:
        if not isinstance(record, dict):
            continue
        refs.extend(str(record.get(key) or "") for key in keys if str(record.get(key) or ""))
    return refs


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


# LLM: _memory_archive_preview_limits centralizes archive preview sizing so callers do not bake defaults.
# 函数用途: 从 AgentConfig 读取 raw archive 各 archive_level 的预览字符数，供归档事件构建复用。
def _memory_archive_preview_limits(config) -> dict[int, int]:
    return {
        0: int(getattr(config, "memory_archive_preview_level_0_chars", 2048) or 0),
        1: int(getattr(config, "memory_archive_preview_level_1_chars", 1024) or 0),
        2: int(getattr(config, "memory_archive_preview_level_2_chars", 512) or 0),
        3: int(getattr(config, "memory_archive_preview_level_3_chars", 160) or 0),
    }
