from __future__ import annotations

"""LLM: implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

给人看的解释：
这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。

Facade pattern: delegates to service classes in runtime_services.py.
"""

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


class SimpleAgentRuntimeMixin:
    """LLM: mixin for the primary model/tool execution loop.

    给人看的解释：
    `run()` 就在这里。普通聊天、gateway ask、runner 调用最后都会经过这条链路。
    """

    # expose service instances (lazy init in first run)
    _tool_loop_service: ToolLoopService | None = None
    _compression_service: CompressionService | None = None
    _finalization_service: FinalizationService | None = None

    def _get_tool_loop_service(self) -> ToolLoopService:
        if self._tool_loop_service is None:
            self._tool_loop_service = ToolLoopService(self)
        return self._tool_loop_service

    def _get_compression_service(self) -> CompressionService:
        if self._compression_service is None:
            self._compression_service = CompressionService(self)
        return self._compression_service

    def _get_finalization_service(self) -> FinalizationService:
        if self._finalization_service is None:
            self._finalization_service = FinalizationService(self)
        return self._finalization_service

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        return self._get_compression_service()._compress_memories(memories, keep_recent=keep_recent)

    def _build_compression_snapshot_content(self, *, user_prompt, memories, runtime_injections, routed_context, resume_context_section):
        return self._get_compression_service()._build_compression_snapshot_content(
            user_prompt=user_prompt, memories=memories, runtime_injections=runtime_injections,
            routed_context=routed_context, resume_context_section=resume_context_section,
        )

    def run(
        self,
        user_prompt: str,
        *,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        save: bool | None = None,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
        request_id: str = "",
        run_id: str = "",
        task_id: str = "",
        task_attributes: dict | None = None,
        source: str = "run",
        recovery_snapshot: bool | None = None,
        resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None,
        recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
    ):
        """执行一轮智能体请求。"""
        memories, runtime_injections, routed_context, resume_context_result = self._prepare_runtime_context(
            user_prompt, inject, resume_context
        )
        tool_catalog_section, tool_recommendations_section = self._resolve_tool_sections(
            allowed_tools, granted_capabilities
        )
        compression_svc = self._get_compression_service()
        (
            memories,
            compression_snapshot_id,
            compression_snapshot_path,
            compression_applied,
        ) = compression_svc.check_and_apply(
            user_prompt, memories, runtime_injections, routed_context, resume_context_section="",
            request_id=request_id, run_id=run_id, task_id=task_id, source=source
        )

        effective_on_chunk = on_chunk
        tool_context: list[str] = []
        tool_rounds = 0
        final_prompt = ""
        final_response = None
        one_shot_tool_calls: set[str] = set()
        executed_tools: list[str] = []
        archive_tool_calls: list[dict[str, object]] = []

        tool_loop_svc = self._get_tool_loop_service()
        final_prompt, final_response, tool_rounds = tool_loop_svc.execute(
            user_prompt, memories, runtime_injections, prompt_files,
            tool_catalog_section, tool_recommendations_section, tool_context,
            effective_on_chunk, allowed_tools, granted_capabilities,
            write_boundary, task_attributes, one_shot_tool_calls,
            executed_tools, archive_tool_calls,
            tool_rounds=tool_rounds,
        )

        finalization_svc = self._get_finalization_service()
        return finalization_svc.finalize(
            user_prompt, final_prompt, final_response, memories, executed_tools,
            archive_tool_calls, routed_context, resume_context_result,
            runtime_injections=runtime_injections,
            compression_snapshot_id=compression_snapshot_id,
            compression_snapshot_path=compression_snapshot_path,
            compression_applied=compression_applied,
            request_id=request_id, run_id=run_id, task_id=task_id, source=source,
            do_save=self.config.auto_save_memory if save is None else save,
            recovery_snapshot=recovery_snapshot, recovery_task_refs=recovery_task_refs,
            recovery_content_paths=recovery_content_paths,
            recovery_next_actions=recovery_next_actions,
            tool_rounds=tool_rounds,
        )

    def _prepare_runtime_context(self, user_prompt, inject, resume_context):
        """Prepare memories, routing, and injections for a run."""
        memories = self.memory.search(user_prompt, self.config.memory_top_k)
        route_mode = str(getattr(self.config, "memory_rule_routing_mode", "soft") or "soft")
        route_enabled = bool(getattr(self.config, "memory_rule_routing_enabled", True)) and route_mode != "off"
        route_auto_read_limit = int(getattr(self.config, "memory_rule_auto_read_limit", 3))
        routed_context = build_routed_memory_context(
            self.root,
            user_prompt,
            enabled=route_enabled,
            mode=route_mode if route_mode != "off" else "soft",
            auto_read_limit=route_auto_read_limit,
            limit=max(route_auto_read_limit, 5),
        )
        resume_context_result = build_auto_resume_context(self, user_prompt, enabled=resume_context)
        resume_context_section = (
            "### Auto Recovery Context\n"
            f"{resume_context_result.context_block}"
            if resume_context_result.injected
            else ""
        )
        runtime_injections = [
            *(inject or []),
            *([resume_context_section] if resume_context_section else []),
            *routed_context.injected_sections,
        ]
        return memories, runtime_injections, routed_context, resume_context_result

    def _resolve_tool_sections(self, allowed_tools, granted_capabilities):
        """Resolve tool catalog and recommendations sections."""
        runtime_capabilities = resolve_runtime_capabilities(
            None,  # user_prompt not needed for capability resolution
            inject=None,
            granted_capabilities=granted_capabilities,
        )
        tool_catalog_section = (
            self.tools.render_catalog_section(
                allowed_tools=allowed_tools,
                granted_capabilities=runtime_capabilities,
            )
            if self.config.enable_tools
            else ""
        )
        tool_recommendations_section = (
            self.tools.render_recommended_tools_section(
                None,  # user_prompt not needed for recommendations
                allowed_tools=allowed_tools,
                granted_capabilities=runtime_capabilities,
            )
            if self.config.enable_tools
            else ""
        )
        return tool_catalog_section, tool_recommendations_section

    def remember(self, content: str, *, kind: str = "note"):
        """手动写入一条记忆。"""

        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        """召回相关记忆。"""

        return self.memory.search(query, top_k or self.config.memory_top_k)
