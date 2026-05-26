# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。

Facade pattern: delegates to service classes in runtime_services.py.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace

from .compact_auto_continuation import (
    compact_auto_continuation_decision,
    mark_compact_auto_continued,
)
from .runtime_loop_models import RuntimeContextRequest
from .runtime_loop_support import (
    FinalizeParams,
    RunParams,
    _execute_runtime_loop,
    _finalize_params,
    _prepare_runtime_context,
    _runtime_loop_params,
)
from .runtime_run_params import (
    RunCompatibilityFields,
    run_params_from_compat,
    run_params_with_materialized_delivery_contract,
    run_params_with_request_id,
)
from .runtime_services import CompressionService, FinalizationService, ToolLoopService


# LLM: _RuntimeServices 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存运行时services字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass
class _RuntimeServices:

    tool_loop: ToolLoopService
    compression: CompressionService
    finalization: FinalizationService


# LLM: _CompressionSnapshotRequest 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存压缩snapshot请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _CompressionSnapshotRequest:
    # LLM: snapshot render inputs stay bundled before crossing into CompressionService.
    user_prompt: object
    memories: object
    runtime_injections: object
    routed_context: object
    resume_context_section: object


@dataclass(frozen=True)
class _PromptScopeSnapshot:
    had_prompt: bool
    previous_prompt: str
    had_params: bool
    previous_params: object


# LLM: _current_prompt_scope keeps run() flat while exposing current run identity to tools.
# 函数用途: 在一次 run 内设置当前用户 prompt 和 RunParams，退出时恢复旧值或删除临时字段。
@contextmanager
def _current_prompt_scope(agent, user_prompt: str, params: RunParams | None = None):
    had_current_prompt = hasattr(agent, "_current_user_prompt")
    previous_current_prompt = getattr(agent, "_current_user_prompt", "")
    had_current_run_params = hasattr(agent, "_current_run_params")
    previous_current_run_params = getattr(agent, "_current_run_params", None)
    agent._current_user_prompt = user_prompt
    if params is not None:
        agent._current_run_params = params
    snapshot = _PromptScopeSnapshot(
        had_current_prompt,
        previous_current_prompt,
        had_current_run_params,
        previous_current_run_params,
    )
    try:
        yield
    finally:
        _restore_current_prompt(agent, snapshot)


# LLM: _restore_current_prompt keeps the context manager below nesting limits.
# 函数用途: 退出 run 作用域时恢复旧 prompt/RunParams；旧字段不存在时删除临时字段。
def _restore_current_prompt(agent, snapshot: _PromptScopeSnapshot) -> None:
    if snapshot.had_prompt:
        agent._current_user_prompt = snapshot.previous_prompt
    elif hasattr(agent, "_current_user_prompt"):
        delattr(agent, "_current_user_prompt")
    if snapshot.had_params:
        agent._current_run_params = snapshot.previous_params
    elif hasattr(agent, "_current_run_params"):
        delattr(agent, "_current_run_params")


# LLM: SimpleAgentRuntimeMixin 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 拆分simpleagent运行时混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SimpleAgentRuntimeMixin:

    _services: _RuntimeServices | None = None

    # LLM: _get_services 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 读取或查询services需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _get_services(self) -> _RuntimeServices:
        if self._services is None:
            self._services = _RuntimeServices(
                tool_loop=ToolLoopService(self),
                compression=CompressionService(self),
                finalization=FinalizationService(self),
            )
        return self._services

    # LLM: _compress_memories 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理compressmemories相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        return self._get_services().compression._compress_memories(memories, keep_recent=keep_recent)

    # LLM: _build_compression_snapshot_content 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建压缩snapshot内容所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _build_compression_snapshot_content(
        self,
        *,
        params: _CompressionSnapshotRequest | None = None,
        user_prompt=None,
        memories=None,
        runtime_injections=None,
        routed_context=None,
        resume_context_section=None,
    ):
        from ._compression_service import CompressionSnapshotContentParams

        values = params or _CompressionSnapshotRequest(
            user_prompt, memories, runtime_injections, routed_context, resume_context_section
        )
        return self._get_services().compression._build_compression_snapshot_content(
            CompressionSnapshotContentParams(
                user_prompt=values.user_prompt,
                memories=values.memories,
                runtime_injections=values.runtime_injections,
                routed_context=values.routed_context,
                resume_context_section=values.resume_context_section,
            )
        )

    # LLM: run 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进run的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def run(
        self,
        user_prompt: str,
        *,
        params: RunParams = None,
        inject: list[str] | None = None, prompt_files: list[str] | None = None, save: bool | None = None,
        allowed_tools: list[str] | None = None, granted_capabilities: list[str] | None = None,
        write_boundary: dict[str, object] | None = None, request_id: str | None = None,
        run_id: str | None = None, task_id: str | None = None, task_attributes: dict | None = None,
        delivery_contract: dict | None = None,
        system_prompt_override: str | None = None, source: str | None = None,
        recovery_snapshot: bool | None = None, resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None, recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
        context_scope: str | None = None,
    ):
        provided_params = params
        params = run_params_from_compat(
            params,
            RunCompatibilityFields(
                inject=inject,
                prompt_files=prompt_files,
                save=save,
                allowed_tools=allowed_tools,
                granted_capabilities=granted_capabilities,
                write_boundary=write_boundary,
                request_id=request_id,
                run_id=run_id,
                task_id=task_id,
                task_attributes=task_attributes,
                delivery_contract=delivery_contract,
                system_prompt_override=system_prompt_override,
                source=source,
                recovery_snapshot=recovery_snapshot,
                resume_context=resume_context,
                recovery_task_refs=recovery_task_refs,
                recovery_content_paths=recovery_content_paths,
                recovery_next_actions=recovery_next_actions,
                on_chunk=on_chunk,
                context_scope=context_scope,
            ),
        )
        params = _apply_config_compact_auto_defaults(self.config, params, provided_params=provided_params is not None)
        return _run_with_params(self, user_prompt, params)

    # LLM: _build_finalize_context 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建finalize上下文所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _build_finalize_context(self, params: FinalizeParams):
        from .runtime_services import FinalizeContext
        rp = params.run_params
        return FinalizeContext(
            user_prompt=params.user_prompt,
            final_prompt=params.final_prompt,
            final_response=params.final_response,
            memories=params.memories,
            executed_tools=params.executed_tools,
            archive_tool_calls=params.archive_tool_calls,
            routed_context=params.routed_context,
            resume_context_result=params.resume_context_result,
            runtime_injections=params.runtime_injections,
            compression_snapshot_id=params.compression_snapshot_id,
            compression_snapshot_path=params.compression_snapshot_path,
            compression_applied=params.compression_applied,
            request_id=rp.request_id,
            run_id=rp.run_id,
            task_id=rp.task_id,
            source=rp.source,
            do_save=self.config.auto_save_memory if rp.save is None else rp.save,
            recovery_snapshot=rp.recovery_snapshot,
            recovery_task_refs=rp.recovery_task_refs,
            recovery_content_paths=rp.recovery_content_paths,
            recovery_next_actions=rp.recovery_next_actions,
            tool_rounds=params.tool_rounds,
            compact_auto_continue_depth=rp.compact_auto_continue_depth,
            compact_auto_continue_max_depth=rp.compact_auto_continue_max_depth,
            main_context_bundle_path=params.main_context_bundle_path,
            main_context_bundle_markdown_path=params.main_context_bundle_markdown_path,
        )

    # LLM: remember 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理remember相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def remember(self, content: str, *, kind: str = "note"):
        return self.memory.add("user", content, kind=kind)

    # LLM: recall 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理recall相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def recall(self, query: str, top_k: int | None = None):
        return self.memory.search(query, top_k or self.config.memory_top_k)


# LLM: _run_with_params keeps the public run() compatibility shim under code-size limits.
# 函数用途: 执行已归一化的 RunParams，串接准备上下文、工具循环和 finalization。
def _run_with_params(agent, user_prompt: str, params: RunParams):
    current_params = run_params_with_request_id(params)
    current_params = run_params_with_materialized_delivery_contract(agent, user_prompt, current_params)
    result = _run_once_with_params(agent, user_prompt, current_params)
    while True:
        decision = compact_auto_continuation_decision(
            result,
            depth=current_params.compact_auto_continue_depth,
            max_depth=current_params.compact_auto_continue_max_depth,
        )
        if not decision.should_continue:
            return result
        next_params = _compact_auto_continue_params(current_params, decision.injection)
        continued = _run_once_with_params(agent, decision.user_prompt, next_params)
        result = mark_compact_auto_continued(continued, result, depth=next_params.compact_auto_continue_depth)
        current_params = next_params


# LLM: _run_once_with_params contains one normal model/tool/finalize pass for reuse by auto continuation.
# 函数用途: 执行单轮 run，不处理自动 compact 后续跑，避免递归和重复上下文作用域。
def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    with _current_prompt_scope(agent, user_prompt, params):
        prepared = _prepare_runtime_context(
            agent,
            RuntimeContextRequest(
                user_prompt,
                params.inject,
                params.resume_context,
                params.context_scope,
                allowed_tools=params.allowed_tools,
                granted_capabilities=params.granted_capabilities,
                write_boundary=params.write_boundary,
                request_id=params.request_id,
                run_id=params.run_id,
                task_id=params.task_id,
                source=params.source,
                save=params.save,
                task_attributes=params.task_attributes,
            ),
        )
        loop_result = _execute_runtime_loop(
            agent,
            _runtime_loop_params(user_prompt, prepared, params),
        )
        ctx = agent._build_finalize_context(_finalize_params(user_prompt, prepared, loop_result, params))
        return agent._get_services().finalization.finalize(ctx)


# LLM: _compact_auto_continue_params injects the continue packet and bumps depth for exactly one guarded turn.
# 函数用途: 构造自动续跑参数，保留原有注入内容，同时防止续跑轮再次触发自动续跑链。
def _compact_auto_continue_params(params: RunParams, injection: str) -> RunParams:
    return replace(
        params,
        inject=[*(params.inject or []), injection],
        compact_auto_continue_depth=params.compact_auto_continue_depth + 1,
    )


# LLM: _apply_config_compact_auto_defaults gives public run() a config-backed continuation depth.
# 函数用途: 用户没有显式传 RunParams 时，用配置控制自动 compact/resume 最多连续续跑多少轮。
def _apply_config_compact_auto_defaults(config, params: RunParams, *, provided_params: bool) -> RunParams:
    if provided_params:
        return params
    depth = int(getattr(config, "memory_compact_auto_continue_max_depth", params.compact_auto_continue_max_depth) or 0)
    return replace(params, compact_auto_continue_max_depth=max(0, depth))
