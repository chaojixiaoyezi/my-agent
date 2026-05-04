from __future__ import annotations

"""LLM: implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

给人看的解释：
这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。

Facade pattern: delegates to service classes in runtime_services.py.
"""

from dataclasses import dataclass
from typing import Any

from ..memory_archive import (
    archive_run_turn,
    build_auto_resume_context,
    estimate_tokens,
    write_compression_snapshot,
    write_recovery_snapshot,
)
from ..memory_archive.tokens import append_session_token_usage
from ..memory_routing import build_routed_memory_context
from .runtime_capabilities import resolve_runtime_capabilities
from .runtime_services import CompressionService, FinalizationService, ToolLoopService


@dataclass
class RunParams:
    """Bundle of all run() parameters."""

    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool | None = None
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    task_attributes: dict | None = None
    source: str = "run"
    recovery_snapshot: bool | None = None
    resume_context: bool | None = None
    recovery_task_refs: list[str] | None = None
    recovery_content_paths: list[str] | None = None
    recovery_next_actions: list[str] | None = None
    on_chunk: object = None


@dataclass
class _RuntimeServices:
    """Bundles the three lazy-initialized service instances."""

    tool_loop: ToolLoopService
    compression: CompressionService
    finalization: FinalizationService


class SimpleAgentRuntimeMixin:
    """LLM: mixin for the primary model/tool execution loop.

    给人看的解释：
    `run()` 就在这里。普通聊天、gateway ask、runner 调用最后都会经过这条链路。
    """

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
        return self._get_services().compression._build_compression_snapshot_content(
            user_prompt=user_prompt,
            memories=memories,
            runtime_injections=runtime_injections,
            routed_context=routed_context,
            resume_context_section=resume_context_section,
        )

    def run(
        self,
        user_prompt: str,
        *,
        params: RunParams = None,
        **kwargs,
    ):
        """执行一轮智能体请求。"""
        if params is None:
            params = RunParams()
        elif not isinstance(params, RunParams):
            raise TypeError("run() requires params: RunParams keyword argument")

        for key in [
            "inject", "prompt_files", "save", "allowed_tools", "granted_capabilities",
            "write_boundary", "request_id", "run_id", "task_id", "task_attributes",
            "source", "recovery_snapshot", "resume_context", "recovery_task_refs",
            "recovery_content_paths", "recovery_next_actions", "on_chunk",
        ]:
            if key in kwargs:
                setattr(params, key, kwargs[key])

        memories, runtime_injections, routed_context, resume_context_result = (
            _prepare_runtime_context(self, user_prompt, params.inject, params.resume_context)
        )
        final_prompt, final_response, tool_rounds, compression_snapshot_id, compression_snapshot_path, compression_applied, executed_tools, archive_tool_calls = (
            _execute_runtime_loop(
                self, user_prompt, memories, runtime_injections,
                params.allowed_tools, params.granted_capabilities,
                params.prompt_files, params.write_boundary, params.task_attributes,
                params.on_chunk, params.request_id, params.run_id, params.task_id, params.source,
            )
        )

        ctx = self._build_finalize_context(
            user_prompt, final_prompt, final_response, memories,
            executed_tools, archive_tool_calls, routed_context, resume_context_result,
            runtime_injections, compression_snapshot_id, compression_snapshot_path,
            compression_applied, params, tool_rounds,
        )
        return self._get_services().finalization.finalize(ctx)

    def _build_finalize_context(
        self, user_prompt, final_prompt, final_response, memories,
        executed_tools, archive_tool_calls, routed_context, resume_context_result,
        runtime_injections, compression_snapshot_id, compression_snapshot_path,
        compression_applied, params, tool_rounds,
    ):
        from .runtime_services import FinalizeContext
        return FinalizeContext(
            user_prompt=user_prompt,
            final_prompt=final_prompt,
            final_response=final_response,
            memories=memories,
            executed_tools=executed_tools,
            archive_tool_calls=archive_tool_calls,
            routed_context=routed_context,
            resume_context_result=resume_context_result,
            runtime_injections=runtime_injections,
            compression_snapshot_id=compression_snapshot_id,
            compression_snapshot_path=compression_snapshot_path,
            compression_applied=compression_applied,
            request_id=params.request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            source=params.source,
            do_save=self.config.auto_save_memory if params.save is None else params.save,
            recovery_snapshot=params.recovery_snapshot,
            recovery_task_refs=params.recovery_task_refs,
            recovery_content_paths=params.recovery_content_paths,
            recovery_next_actions=params.recovery_next_actions,
            tool_rounds=tool_rounds,
        )

    def remember(self, content: str, *, kind: str = "note"):
        """手动写入一条记忆。"""
        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        """召回相关记忆。"""
        return self.memory.search(query, top_k or self.config.memory_top_k)


def _resolve_tool_sections(agent, allowed_tools, granted_capabilities):
    """Resolve tool catalog and recommendations sections."""
    runtime_capabilities = resolve_runtime_capabilities(
        None, inject=None, granted_capabilities=granted_capabilities,
    )
    if not agent.config.enable_tools:
        return "", ""
    tool_catalog = agent.tools.render_catalog_section(
        allowed_tools=allowed_tools, granted_capabilities=runtime_capabilities,
    )
    tool_recommendations = agent.tools.render_recommended_tools_section(
        None, allowed_tools=allowed_tools, granted_capabilities=runtime_capabilities,
    )
    return tool_catalog, tool_recommendations


def _prepare_runtime_context(agent, user_prompt, inject, resume_context):
    """Prepare memories, routing, and injections for a run."""
    memories = agent.memory.search(user_prompt, agent.config.memory_top_k)
    route_mode = str(getattr(agent.config, "memory_rule_routing_mode", "soft") or "soft")
    route_enabled = bool(getattr(agent.config, "memory_rule_routing_enabled", True)) and route_mode != "off"
    route_auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))
    routed_context = build_routed_memory_context(
        agent.root, user_prompt,
        enabled=route_enabled,
        mode=route_mode if route_mode != "off" else "soft",
        auto_read_limit=route_auto_read_limit,
        limit=max(route_auto_read_limit, 5),
    )
    resume_context_result = build_auto_resume_context(agent, user_prompt, enabled=resume_context)
    resume_context_section = (
        f"### Auto Recovery Context\n{resume_context_result.context_block}"
        if resume_context_result.injected else ""
    )
    runtime_injections = [
        *(inject or []),
        *([resume_context_section] if resume_context_section else []),
        *routed_context.injected_sections,
    ]
    return memories, runtime_injections, routed_context, resume_context_result


def _execute_runtime_loop(
    agent, user_prompt, memories, runtime_injections,
    allowed_tools, granted_capabilities, prompt_files, write_boundary,
    task_attributes, on_chunk, request_id, run_id, task_id, source,
):
    """Execute the core tool loop and compression for a run."""
    tool_catalog_section, tool_recommendations_section = _resolve_tool_sections(
        agent, allowed_tools, granted_capabilities,
    )
    compression_svc = agent._get_services().compression
    from .runtime_services import CompressionContext

    compression_ctx = CompressionContext(
        user_prompt=user_prompt, memories=memories, runtime_injections=runtime_injections,
        routed_context=None, resume_context_section="",
        request_id=request_id, run_id=run_id, task_id=task_id, source=source,
    )
    memories, compression_snapshot_id, compression_snapshot_path, compression_applied = (
        compression_svc.check_and_apply(compression_ctx)
    )

    tool_context: list[str] = []
    tool_rounds = 0
    final_prompt = ""
    final_response = None
    one_shot_tool_calls: set[str] = set()
    executed_tools: list[str] = []
    archive_tool_calls: list[dict[str, object]] = []

    from .runtime_services import ToolLoopExecuteParams
    loop_params = ToolLoopExecuteParams(
        user_prompt=user_prompt, memories=memories, runtime_injections=runtime_injections,
        prompt_files=prompt_files, tool_catalog_section=tool_catalog_section,
        tool_recommendations_section=tool_recommendations_section,
        tool_context=tool_context, effective_on_chunk=on_chunk,
        allowed_tools=allowed_tools, granted_capabilities=granted_capabilities,
        write_boundary=write_boundary, task_attributes=task_attributes,
        one_shot_tool_calls=one_shot_tool_calls, executed_tools=executed_tools,
        archive_tool_calls=archive_tool_calls, tool_rounds=tool_rounds,
    )
    final_prompt, final_response, tool_rounds = agent._get_services().tool_loop.execute(loop_params)
    return final_prompt, final_response, tool_rounds, compression_snapshot_id, compression_snapshot_path, compression_applied, executed_tools, archive_tool_calls