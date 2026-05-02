from __future__ import annotations

"""LLM: implements the SimpleAgent prompt/model/tool loop plus memory facade methods.

给人看的解释：
这个文件是主代理最基础的一轮对话链路：召回记忆、构建 prompt、调用模型、解析工具调用、把工具结果再喂回模型。
它不处理子代理调度细节，那些已经拆到别的 mixin。
"""

import sys
import time

from ..memory_archive import (
    archive_run_turn,
    build_auto_resume_context,
    estimate_tokens,
    write_compression_snapshot,
    write_recovery_snapshot,
)
from ..memory_archive.tokens import append_session_token_usage
from ..memory_routing import build_routed_memory_context
from ..memory_store.jsonl import MemoryRecord
from ..tools import ToolExecutionResult
from .models import AgentRunResult
from .parameters import _one_shot_tool_call_key
from .runtime_capabilities import resolve_runtime_capabilities


class SimpleAgentRuntimeMixin:
    """LLM: mixin for the primary model/tool execution loop.

    给人看的解释：
    `run()` 就在这里。普通聊天、gateway ask、runner 调用最后都会经过这条链路。
    """

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
        source: str = "run",
        recovery_snapshot: bool | None = None,
        resume_context: bool | None = None,
        recovery_task_refs: list[str] | None = None,
        recovery_content_paths: list[str] | None = None,
        recovery_next_actions: list[str] | None = None,
        on_chunk: object = None,
    ) -> AgentRunResult:
        """执行一轮智能体请求。"""

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
        runtime_capabilities = resolve_runtime_capabilities(
            user_prompt,
            inject=inject,
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
                user_prompt,
                allowed_tools=allowed_tools,
                granted_capabilities=runtime_capabilities,
            )
            if self.config.enable_tools
            else ""
        )
        tool_context: list[str] = []
        tool_rounds = 0
        final_prompt = ""
        final_response = None
        one_shot_tool_calls: set[str] = set()
        executed_tools: list[str] = []
        archive_tool_calls: list[dict[str, object]] = []
        compression_snapshot_id = ""
        compression_snapshot_path = ""
        cumulative_token_estimate = 0
        turn_token_estimate = 0
        compression_applied = False

        # LLM: only stream when the caller explicitly provides a callback.
        effective_on_chunk = on_chunk

        full_prompt_estimate = estimate_tokens(
            {
                "user_prompt": user_prompt,
                "memories": [getattr(memory, "content", "") for memory in memories],
                "inject": runtime_injections,
                "prompt_files": prompt_files or [],
            }
        )
        if full_prompt_estimate > int(getattr(self.config, "max_tokens", 1024)):
            turn_id = request_id or run_id or task_id or f"turn-{time.time_ns()}"
            snapshot_content = self._build_compression_snapshot_content(
                user_prompt=user_prompt,
                memories=memories,
                runtime_injections=runtime_injections,
                routed_context=routed_context,
                resume_context_section=resume_context_section,
            )
            try:
                hook_result = write_compression_snapshot(
                    self.root,
                    session_id=getattr(self, "session_id", self.config.agent_name),
                    turn_id=turn_id,
                    role="system",
                    content=snapshot_content,
                    archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
                    request_id=request_id,
                    run_id=run_id,
                    task_id=task_id,
                    source=source,
                    content_paths=[
                        *(routed_context.required_read_paths or []),
                        *(routed_context.candidate_paths or []),
                    ],
                    next_actions=["先校验 task 事实源，再使用 compression snapshot 恢复上下文。"],
                )
            except Exception as exc:
                if getattr(self, "local_store", None):
                    self.local_store.record_event(
                        "memory_compression_snapshot_failed",
                        payload={
                            "request_id": request_id,
                            "run_id": run_id,
                            "task_id": task_id,
                            "source": source,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                raise RuntimeError(f"compression blocked: pre-compression snapshot failed: {exc}") from exc
            compression_snapshot_id = hook_result.snapshot_id
            compression_snapshot_path = hook_result.snapshot_file_path
            memories = self._compress_memories(memories, keep_recent=max(self.config.memory_top_k, 2))
            compression_applied = True

        while True:
            final_prompt = self.prompts.build(
                user_prompt,
                memories,
                inject=runtime_injections,
                prompt_files=prompt_files,
                tool_catalog_section=tool_catalog_section,
                tool_recommendations_section=tool_recommendations_section,
                tool_context=tool_context,
            )
            response = self.backend.generate(final_prompt, on_chunk=effective_on_chunk)
            final_response = response

            if not self.config.enable_tools:
                break

            calls = self.tools.parse_tool_calls(response.text)
            if not calls:
                break

            if tool_rounds >= self.config.max_tool_rounds:
                tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
                final_prompt = self.prompts.build(
                    user_prompt,
                    memories,
                    inject=runtime_injections,
                    prompt_files=prompt_files,
                    tool_catalog_section=tool_catalog_section,
                    tool_recommendations_section=tool_recommendations_section,
                    tool_context=tool_context,
                )
                final_response = self.backend.generate(final_prompt, on_chunk=effective_on_chunk)
                break

            tool_rounds += 1
            tool_context.append(f"[assistant-tool-round-{tool_rounds}]\n{response.text}")
            for idx, payload in enumerate(calls, start=1):
                one_shot_key = _one_shot_tool_call_key(payload)
                if one_shot_key and one_shot_key in one_shot_tool_calls:
                    tool_name = str(payload.get("tool") or "unknown")
                    result = ToolExecutionResult(
                        tool_name,
                        False,
                        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
                        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
                    )
                else:
                    result = self.tools.execute_call(
                        payload,
                        allowed_tools=allowed_tools,
                        granted_capabilities=runtime_capabilities,
                        write_boundary=write_boundary,
                    )
                    if one_shot_key and result.ok:
                        one_shot_tool_calls.add(one_shot_key)
                if result.ok and result.tool not in {"__parse_error__", "unknown"}:
                    executed_tools.append(result.tool)
                archive_tool_calls.append(
                    {
                        "tool": result.tool,
                        "id": f"{tool_rounds}-{idx}",
                        "ok": result.ok,
                        "parameters": payload,
                    }
                )
                tool_context.append(
                    f"[tool-call-{tool_rounds}-{idx}]\n{payload}\n"
                    f"[tool-result-{tool_rounds}-{idx}]\n{result.render_for_prompt()}"
                )

        assert final_response is not None
        do_save = self.config.auto_save_memory if save is None else save
        run_request_id = request_id or f"run-{time.time_ns()}"
        if do_save:
            self.memory.add("user", user_prompt)
            self.memory.add("agent", final_response.text, tags=[final_response.backend])
            archive_result = archive_run_turn(
                self.root,
                session_id=getattr(self, "session_id", self.config.agent_name),
                request_id=run_request_id,
                run_id=run_id,
                task_id=task_id,
                user_prompt=user_prompt,
                response_text=final_response.text,
                backend=final_response.backend,
                tool_calls=archive_tool_calls,
                source=source,
                archive_level=int(getattr(self.config, "memory_archive_level", 3)),
            )
        else:
            archive_result = None
        should_write_snapshot = (
            bool(getattr(self.config, "memory_hook_enabled", True))
            and (do_save if recovery_snapshot is None else bool(recovery_snapshot))
        )
        snapshot_result = (
            write_recovery_snapshot(
                self.root,
                session_id=getattr(self, "session_id", self.config.agent_name),
                request_id=run_request_id,
                run_id=run_id,
                task_id=task_id,
                user_prompt=user_prompt,
                response_text=final_response.text,
                backend=final_response.backend,
                source=source,
                status="ok",
                tool_calls=archive_tool_calls,
                task_refs=recovery_task_refs or [],
                content_paths=[
                    *(recovery_content_paths or []),
                    *(routed_context.required_read_paths or []),
                    *(routed_context.candidate_paths or []),
                ],
                next_actions=recovery_next_actions or [],
                archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
            )
            if should_write_snapshot
            else None
        )
        turn_id = run_request_id or run_id or task_id or f"turn-{time.time_ns()}"
        input_tokens = estimate_tokens(user_prompt) + estimate_tokens(runtime_injections) + estimate_tokens(
            [getattr(memory, "content", "") for memory in memories]
        )
        output_tokens = estimate_tokens(final_response.text)
        tool_tokens = estimate_tokens(archive_tool_calls)
        ledger = append_session_token_usage(
            self.root,
            session_id=getattr(self, "session_id", self.config.agent_name),
            turn_id=turn_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_tokens=tool_tokens,
            created_at=str(time.time()),
        )
        turn_token_estimate = int(ledger["turn_total"])
        cumulative_token_estimate = int(ledger["cumulative_tokens"])

        return AgentRunResult(
            prompt=final_prompt,
            response=final_response.text,
            backend=final_response.backend,
            used_memories=len(memories),
            tool_rounds=tool_rounds,
            executed_tools=executed_tools,
            memory_route_matches=len(routed_context.matches),
            memory_route_paths=[
                *routed_context.required_read_paths,
                *routed_context.candidate_paths,
            ],
            archive_events=archive_result.event_count if archive_result else 0,
            archive_token_estimate=archive_result.token_estimate if archive_result else 0,
            prompt_token_estimate=estimate_tokens(final_prompt),
            runtime_injection_token_estimate=estimate_tokens(runtime_injections) if runtime_injections else 0,
            recovery_snapshot_id=snapshot_result.snapshot_id if snapshot_result else "",
            recovery_snapshot_path=snapshot_result.path if snapshot_result else "",
            recovery_snapshot_error=snapshot_result.error if snapshot_result else "",
            recovery_snapshot_token_estimate=snapshot_result.token_estimate if snapshot_result else 0,
            memory_resume_context_injected=resume_context_result.injected,
            memory_resume_context_query=resume_context_result.query,
            memory_resume_context_matches=(
                resume_context_result.archive_match_count
                + resume_context_result.local_match_count
                + resume_context_result.task_fact_source_count
            ),
            memory_resume_context_token_estimate=estimate_tokens(resume_context_result.context_block)
            if resume_context_result.injected
            else 0,
            memory_resume_context_error=resume_context_result.error,
            compression_snapshot_id=compression_snapshot_id,
            compression_snapshot_path=compression_snapshot_path,
            compression_applied=compression_applied,
            turn_token_estimate=turn_token_estimate,
            cumulative_token_estimate=cumulative_token_estimate,
        )

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        """LLM: keep recent turns intact and replace older turns with one bounded summary record.

        给人看的解释：
        这里先做保守版组合压缩：最近 N 轮完整保留，更早的内容压成一条 summary 记忆。
        raw archive 和 compression snapshot 仍然是冷存和恢复锚点，不靠这条摘要当权威事实。
        """

        if len(memories) <= keep_recent:
            return memories
        older = memories[:-keep_recent]
        recent = memories[-keep_recent:]
        summary_lines = [f"{getattr(item, 'role', 'memory')}: {getattr(item, 'content', '')}" for item in older[-12:]]
        summary = MemoryRecord(
            role="system",
            kind="summary",
            content="历史轮次摘要（恢复时必须回到 task/route 权威文件核验）:\n" + "\n".join(summary_lines),
            tags=["compression", "summary"],
        )
        return [summary, *recent]

    def _build_compression_snapshot_content(
        self,
        *,
        user_prompt: str,
        memories: list[object],
        runtime_injections: list[str],
        routed_context: object,
        resume_context_section: str,
    ) -> str:
        """LLM: assemble the bounded snapshot body captured before context compression.

        给人看的解释：
        快照正文不追求完整 prompt 复刻，只保留压缩决策前最重要的上下文块，供恢复和审计使用。
        """

        lines = [
            f"user_prompt={user_prompt}",
            f"memory_count={len(memories)}",
            f"routed_required={getattr(routed_context, 'required_read_paths', [])}",
            f"routed_candidates={getattr(routed_context, 'candidate_paths', [])}",
        ]
        if resume_context_section:
            lines.append("resume_context=" + resume_context_section[:800])
        if runtime_injections:
            lines.append("runtime_injections=" + "\n---\n".join(runtime_injections)[:2000])
        history = [
            f"{getattr(memory, 'role', 'memory')}: {getattr(memory, 'content', '')}"
            for memory in memories[-12:]
        ]
        if history:
            lines.append("recent_memories=" + "\n".join(history)[:3000])
        return "\n".join(lines)

    def remember(self, content: str, *, kind: str = "note"):
        """手动写入一条记忆。"""

        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        """召回相关记忆。"""

        return self.memory.search(query, top_k or self.config.memory_top_k)
