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
from .runtime_loop_support import (
    RunParams,
    _execute_runtime_loop,
    _finalize_params,
    _FinalizeParams,
    _prepare_runtime_context,
    _runtime_loop_params,
    run_params_from_values,
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


# LLM: _RunCompatibilityFields 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存runcompatibility字段字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class _RunCompatibilityFields:
    # LLM: 旧运行关键字字段先集中收集，再构造统一运行参数。
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    task_attributes: dict | None = None
    system_prompt_override: str | None = None
    source: str | None = None
    recovery_snapshot: bool | None = None
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None


# LLM: _current_prompt_scope keeps run() flat while preserving the legacy _current_user_prompt behavior.
# 函数用途: 在一次 run 内设置当前用户 prompt，退出时恢复旧值或删除临时字段。
@contextmanager
def _current_prompt_scope(agent, user_prompt: str):
    had_current_prompt = hasattr(agent, "_current_user_prompt")
    previous_current_prompt = getattr(agent, "_current_user_prompt", "")
    agent._current_user_prompt = user_prompt
    try:
        yield
    finally:
        _restore_current_prompt(agent, had_current_prompt, previous_current_prompt)


# LLM: _restore_current_prompt keeps the context manager below nesting limits.
# 函数用途: 退出 run 作用域时恢复旧 prompt；旧字段不存在时删除临时字段。
def _restore_current_prompt(agent, had_current_prompt: bool, previous_current_prompt: str) -> None:
    if had_current_prompt:
        agent._current_user_prompt = previous_current_prompt
    elif hasattr(agent, "_current_user_prompt"):
        delattr(agent, "_current_user_prompt")


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
        system_prompt_override: str | None = None, source: str | None = None,
        recovery_snapshot: bool | None = None, resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None, recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
    ):
        params = _run_params_from_compat(
            params,
            _RunCompatibilityFields(
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
                system_prompt_override=system_prompt_override,
                source=source,
                recovery_snapshot=recovery_snapshot,
                resume_context=resume_context,
                recovery_task_refs=recovery_task_refs,
                recovery_content_paths=recovery_content_paths,
                recovery_next_actions=recovery_next_actions,
                on_chunk=on_chunk,
            ),
        )
        return _run_with_params(self, user_prompt, params)

    # LLM: _build_finalize_context 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建finalize上下文所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _build_finalize_context(self, params: _FinalizeParams):
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
        )

    # LLM: remember 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理remember相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def remember(self, content: str, *, kind: str = "note"):
        return self.memory.add("user", content, kind=kind)

    # LLM: recall 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理recall相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def recall(self, query: str, top_k: int | None = None):
        return self.memory.search(query, top_k or self.config.memory_top_k)


# LLM: _run_params_from_compat 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自参数compat的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_params_from_compat(params: RunParams, fields: _RunCompatibilityFields) -> RunParams:
    return run_params_from_values(
        params,
        inject=fields.inject,
        prompt_files=fields.prompt_files,
        save=fields.save,
        allowed_tools=fields.allowed_tools,
        granted_capabilities=fields.granted_capabilities,
        write_boundary=fields.write_boundary,
        request_id=fields.request_id,
        run_id=fields.run_id,
        task_id=fields.task_id,
        task_attributes=fields.task_attributes,
        system_prompt_override=fields.system_prompt_override,
        source=fields.source,
        recovery_snapshot=fields.recovery_snapshot,
        resume_context=fields.resume_context,
        recovery_task_refs=fields.recovery_task_refs,
        recovery_content_paths=fields.recovery_content_paths,
        recovery_next_actions=fields.recovery_next_actions,
        on_chunk=fields.on_chunk,
    )


# LLM: _run_with_params keeps the public run() compatibility shim under code-size limits.
# 函数用途: 执行已归一化的 RunParams，串接准备上下文、工具循环和 finalization。
def _run_with_params(agent, user_prompt: str, params: RunParams):
    result = _run_once_with_params(agent, user_prompt, params)
    decision = compact_auto_continuation_decision(
        result,
        depth=params.compact_auto_continue_depth,
        max_depth=params.compact_auto_continue_max_depth,
    )
    if not decision.should_continue:
        return result
    next_params = _compact_auto_continue_params(params, decision.injection)
    continued = _run_once_with_params(agent, decision.user_prompt, next_params)
    return mark_compact_auto_continued(continued, result, depth=next_params.compact_auto_continue_depth)


# LLM: _run_once_with_params contains one normal model/tool/finalize pass for reuse by auto continuation.
# 函数用途: 执行单轮 run，不处理自动 compact 后续跑，避免递归和重复上下文作用域。
def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    with _current_prompt_scope(agent, user_prompt):
        prepared = _prepare_runtime_context(agent, user_prompt, params.inject, params.resume_context)
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
