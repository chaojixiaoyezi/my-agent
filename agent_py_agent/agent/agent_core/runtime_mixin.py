
from __future__ import annotations

"""implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。

Facade pattern: delegates to service classes in runtime/services.py.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace

from .compact_auto_continuation import (
    compact_auto_continuation_decision,
    mark_compact_auto_continued,
)
from .run_task_workspace_writer import attach_run_task_workspace_context
from .runtime.loop_models import RuntimeContextRequest
from .runtime.loop_support import (
    FinalizeParams,
    RunParams,
    _execute_runtime_loop,
    _finalize_params,
    _prepare_runtime_context,
    _runtime_loop_params,
)
from .runtime.run_params import (
    RunCompatibilityFields,
    run_params_from_compat,
    run_params_with_materialized_delivery_contract,
    run_params_with_request_id,
)
from .runtime.services import CompressionService, FinalizationService, ToolLoopService


@dataclass
class _RuntimeServices:

    tool_loop: ToolLoopService
    compression: CompressionService
    finalization: FinalizationService


@dataclass(frozen=True)
class _CompressionSnapshotRequest:
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


def _restore_current_prompt(agent, snapshot: _PromptScopeSnapshot) -> None:
    if snapshot.had_prompt:
        agent._current_user_prompt = snapshot.previous_prompt
    elif hasattr(agent, "_current_user_prompt"):
        delattr(agent, "_current_user_prompt")
    if snapshot.had_params:
        agent._current_run_params = snapshot.previous_params
    elif hasattr(agent, "_current_run_params"):
        delattr(agent, "_current_run_params")


class SimpleAgentRuntimeMixin:

    _services: _RuntimeServices | None = None

    def _get_services(self) -> _RuntimeServices:
        if self._services is None:
            self._services = _RuntimeServices(
                tool_loop=ToolLoopService(self),
                compression=CompressionService(self),
                finalization=FinalizationService(self),
            )
        return self._services

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        return self._get_services().compression._compress_memories(memories, keep_recent=keep_recent)

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
        resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None, recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
        context_scope: str | None = None,
    ):
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
                resume_context=resume_context,
                recovery_task_refs=recovery_task_refs,
                recovery_content_paths=recovery_content_paths,
                recovery_next_actions=recovery_next_actions,
                on_chunk=on_chunk,
                context_scope=context_scope,
            ),
        )
        return _run_with_params(self, user_prompt, params)

    def _build_finalize_context(self, params: FinalizeParams):
        from .runtime.services import FinalizeContext
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
            recovery_task_refs=rp.recovery_task_refs,
            recovery_content_paths=rp.recovery_content_paths,
            recovery_next_actions=rp.recovery_next_actions,
            tool_rounds=params.tool_rounds,
            compact_auto_continue_depth=rp.compact_auto_continue_depth,
            main_context_bundle_path=params.main_context_bundle_path,
            main_context_bundle_markdown_path=params.main_context_bundle_markdown_path,
        )

    def remember(self, content: str, *, kind: str = "note"):
        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        return self.memory.search(query, top_k or self.config.memory_top_k)


def _run_with_params(agent, user_prompt: str, params: RunParams):
    current_params = run_params_with_request_id(params)
    current_params = run_params_with_materialized_delivery_contract(agent, user_prompt, current_params)
    if not current_params.root_user_prompt:
        current_params = replace(current_params, root_user_prompt=user_prompt)
    result = _run_once_with_params(agent, user_prompt, current_params)
    while True:
        decision = compact_auto_continuation_decision(
            result,
            depth=current_params.compact_auto_continue_depth,
        )
        if not decision.should_continue:
            return result
        next_params = _compact_auto_continue_params(current_params, decision.injection)
        continued = _run_once_with_params(agent, decision.user_prompt, next_params)
        result = mark_compact_auto_continued(continued, result, depth=next_params.compact_auto_continue_depth)
        current_params = next_params


def _run_once_with_params(agent, user_prompt: str, params: RunParams):
    params = attach_run_task_workspace_context(agent, params, user_prompt)
    root_user_prompt = params.root_user_prompt or user_prompt
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
        ctx = agent._build_finalize_context(_finalize_params(root_user_prompt, prepared, loop_result, params))
        return agent._get_services().finalization.finalize(ctx)


def _compact_auto_continue_params(params: RunParams, injection: str) -> RunParams:
    return replace(
        params,
        inject=[*(params.inject or []), injection],
        compact_auto_continue_depth=params.compact_auto_continue_depth + 1,
    )
