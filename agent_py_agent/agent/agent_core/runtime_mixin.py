from __future__ import annotations

"""LLM: implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

给人看的解释：
这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。

Facade pattern: delegates to service classes in runtime_services.py.
"""

from dataclasses import dataclass

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


@dataclass
class _RuntimeServices:

    tool_loop: ToolLoopService
    compression: CompressionService
    finalization: FinalizationService


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
        self, *, user_prompt, memories, runtime_injections, routed_context, resume_context_section
    ):
        from ._compression_service import CompressionSnapshotContentParams

        return self._get_services().compression._build_compression_snapshot_content(
            CompressionSnapshotContentParams(
                user_prompt=user_prompt,
                memories=memories,
                runtime_injections=runtime_injections,
                routed_context=routed_context,
                resume_context_section=resume_context_section,
            )
        )

    def run(
        self,
        user_prompt: str,
        *,
        params: RunParams = None,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        save: bool | None = None,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        task_attributes: dict | None = None,
        source: str | None = None,
        recovery_snapshot: bool | None = None,
        resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None,
        recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
    ):
        params = run_params_from_values(
            params,
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
            source=source,
            recovery_snapshot=recovery_snapshot,
            resume_context=resume_context,
            recovery_task_refs=recovery_task_refs,
            recovery_content_paths=recovery_content_paths,
            recovery_next_actions=recovery_next_actions,
            on_chunk=on_chunk,
        )
        prepared = _prepare_runtime_context(self, user_prompt, params.inject, params.resume_context)
        loop_result = _execute_runtime_loop(
            self,
            _runtime_loop_params(
                user_prompt,
                prepared.memories,
                prepared.runtime_injections,
                params,
            ),
        )
        ctx = self._build_finalize_context(_finalize_params(user_prompt, prepared, loop_result, params))
        return self._get_services().finalization.finalize(ctx)

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
        )

    def remember(self, content: str, *, kind: str = "note"):
        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        return self.memory.search(query, top_k or self.config.memory_top_k)
