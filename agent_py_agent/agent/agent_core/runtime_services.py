"""LLM: service classes for SimpleAgentRuntimeMixin.

- ToolLoopService: tool-calling loop execution
- CompressionService: compression check, snapshot, and compression logic
- FinalizationService: result finalization, archiving, and token estimation
"""

from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from typing import Any

from ..memory_archive import (
    archive_run_turn,
    estimate_tokens,
    write_compression_snapshot,
    write_recovery_snapshot,
)
from ..memory_archive.runtime.turn_archiver import ArchiveTurnContext
from ..memory_archive.tokens import append_session_token_usage
from ..tools import ToolExecutionResult
from .models import AgentRunResult
from .parameters import _one_shot_tool_call_key


@dataclass(frozen=True)
class FinalizeContext:
    """Bundle of all finalize() parameters into a single object."""
    user_prompt: str
    final_prompt: str
    final_response: Any
    memories: list
    executed_tools: list
    archive_tool_calls: list
    routed_context: Any
    resume_context_result: Any
    runtime_injections: list
    compression_snapshot_id: str
    compression_snapshot_path: str
    compression_applied: bool
    request_id: str
    run_id: str
    task_id: str
    source: str
    do_save: bool
    recovery_snapshot: Any
    recovery_task_refs: list | None
    recovery_content_paths: list | None
    recovery_next_actions: list | None
    tool_rounds: int = 0


class ToolLoopService:
    """Service for executing the main tool-calling loop."""

    def __init__(self, agent):
        self._agent = agent

    def execute(
        self,
        user_prompt,
        memories,
        runtime_injections,
        prompt_files,
        tool_catalog_section,
        tool_recommendations_section,
        tool_context,
        effective_on_chunk,
        allowed_tools,
        granted_capabilities,
        write_boundary,
        task_attributes,
        one_shot_tool_calls,
        executed_tools,
        archive_tool_calls,
        tool_rounds: int = 0,
    ):
        """Execute the main tool-calling loop."""
        final_prompt = ""
        final_response = None

        while True:
            final_prompt = self._agent.prompts.build(
                user_prompt,
                memories,
                inject=runtime_injections,
                prompt_files=prompt_files,
                tool_catalog_section=tool_catalog_section,
                tool_recommendations_section=tool_recommendations_section,
                tool_context=tool_context,
            )
            response = self._agent.backend.generate(final_prompt, on_chunk=effective_on_chunk)
            final_response = response

            if not self._agent.config.enable_tools:
                break

            calls = self._agent.tools.parse_tool_calls(response.text)
            if not calls:
                break

            effective_max_tool_rounds = self._agent.config.max_tool_rounds
            attrs_to_check = task_attributes if task_attributes else getattr(self._agent, '_current_task_attributes', None)
            if attrs_to_check and "max_tool_rounds" in attrs_to_check:
                effective_max_tool_rounds = int(attrs_to_check["max_tool_rounds"])

            if tool_rounds >= effective_max_tool_rounds:
                tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
                final_prompt = self._agent.prompts.build(
                    user_prompt,
                    memories,
                    inject=runtime_injections,
                    prompt_files=prompt_files,
                    tool_catalog_section=tool_catalog_section,
                    tool_recommendations_section=tool_recommendations_section,
                    tool_context=tool_context,
                )
                final_response = self._agent.backend.generate(final_prompt, on_chunk=effective_on_chunk)
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
                    result = self._agent.tools.execute_call(
                        payload,
                        allowed_tools=allowed_tools,
                        granted_capabilities=granted_capabilities,
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

        return final_prompt, final_response, tool_rounds


@dataclass(frozen=True)
class CompressionContext:
    """Bundle of check_and_apply parameters."""
    user_prompt: str
    memories: list
    runtime_injections: list
    routed_context: Any
    resume_context_section: str
    request_id: str
    run_id: str
    task_id: str
    source: str


class CompressionService:
    """Service for compression checking, snapshot writing, and memory compression."""

    def __init__(self, agent):
        self._agent = agent

    def check_and_apply(self, ctx: CompressionContext):
        """Check prompt size and apply compression if needed."""
        full_prompt_estimate = estimate_tokens(
            {
                "user_prompt": ctx.user_prompt,
                "memories": [getattr(memory, "content", "") for memory in ctx.memories],
                "inject": ctx.runtime_injections,
                "prompt_files": [],
            }
        )
        if full_prompt_estimate <= int(getattr(self._agent.config, "max_tokens", 1024)):
            return ctx.memories, "", "", False

        turn_id = ctx.request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
        snapshot_content = self._build_compression_snapshot_content(
            user_prompt=ctx.user_prompt,
            memories=ctx.memories,
            runtime_injections=ctx.runtime_injections,
            routed_context=ctx.routed_context,
            resume_context_section=ctx.resume_context_section,
        )
        try:
            hook_result = write_compression_snapshot(
                self._agent.root,
                session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                turn_id=turn_id,
                role="system",
                content=snapshot_content,
                archive_level=int(getattr(self._agent.config, "memory_hook_archive_level", 3)),
                request_id=ctx.request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
                source=ctx.source,
                content_paths=[
                    *(getattr(ctx.routed_context, "required_read_paths", None) or []),
                    *(getattr(ctx.routed_context, "candidate_paths", None) or []),
                ],
                next_actions=["先校验 task 事实源，再使用 compression snapshot 恢复上下文。"],
            )
        except Exception as exc:
            if getattr(self._agent, "local_store", None):
                self._agent.local_store.record_event(
                    "memory_compression_snapshot_failed",
                    payload={
                        "request_id": ctx.request_id,
                        "run_id": ctx.run_id,
                        "task_id": ctx.task_id,
                        "source": ctx.source,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            raise RuntimeError(f"compression blocked: pre-compression snapshot failed: {exc}") from exc
        compressed_memories = self._compress_memories(ctx.memories, keep_recent=max(self._agent.config.memory_top_k, 2))
        return compressed_memories, hook_result.snapshot_id, hook_result.snapshot_file_path, True

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
        """LLM: keep recent turns intact and replace older turns with one bounded summary record.

        给人看的解释：
        这里先做保守版组合压缩：最近 N 轮完整保留，更早的内容压成一条 summary 记忆。
        raw archive 和 compression snapshot 仍然是冷存和恢复锚点，不靠这条摘要当权威事实。
        """
        from ..memory_store.jsonl import MemoryRecord

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


class FinalizationService:
    """Service for result finalization, archiving, and token estimation."""

    def __init__(self, agent):
        self._agent = agent

    def finalize(self, ctx: FinalizeContext):
        """Finalize run result: archive, snapshots, token estimation, and return value."""
        assert ctx.final_response is not None
        run_request_id = ctx.request_id or f"run-{time_module.time_ns()}"

        archive_result = self._archive_run_if_needed(
            ctx.do_save, ctx.user_prompt, ctx.final_response, ctx.archive_tool_calls,
            run_request_id, ctx.run_id, ctx.task_id, ctx.source
        )
        snapshot_result = self._write_recovery_snapshot_if_needed(
            ctx.do_save, ctx.recovery_snapshot, ctx.user_prompt, ctx.final_response, ctx.archive_tool_calls,
            run_request_id, ctx.run_id, ctx.task_id, ctx.source, ctx.recovery_task_refs,
            ctx.recovery_content_paths, ctx.recovery_next_actions, ctx.routed_context
        )
        turn_id = run_request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"
        token_ledger = self._estimate_token_usage(
            ctx.user_prompt, ctx.runtime_injections, ctx.memories, ctx.final_response,
            ctx.archive_tool_calls, run_request_id, turn_id
        )

        return self._build_agent_run_result(ctx, archive_result, snapshot_result, token_ledger)

    def _archive_run_if_needed(self, do_save, user_prompt, final_response, archive_tool_calls,
                               run_request_id, run_id, task_id, source):
        """Archive run turn if do_save is enabled."""
        if not do_save:
            return None
        self._agent.memory.add("user", user_prompt)
        self._agent.memory.add("agent", final_response.text, tags=[final_response.backend])
        return archive_run_turn(
            self._agent.root,
            ArchiveTurnContext(
                session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
                request_id=run_request_id,
                run_id=run_id,
                task_id=task_id,
                user_prompt=user_prompt,
                response_text=final_response.text,
                backend=final_response.backend,
                tool_calls=archive_tool_calls or [],
                source=source,
                archive_level=int(getattr(self._agent.config, "memory_archive_level", 3)),
            ),
        )

    def _write_recovery_snapshot_if_needed(self, do_save, recovery_snapshot, user_prompt,
                                           final_response, archive_tool_calls, run_request_id,
                                           run_id, task_id, source, recovery_task_refs,
                                           recovery_content_paths, recovery_next_actions, routed_context):
        """Write recovery snapshot if enabled."""
        should_write = (
            bool(getattr(self._agent.config, "memory_hook_enabled", True))
            and (do_save if recovery_snapshot is None else bool(recovery_snapshot))
        )
        if not should_write:
            return None
        return write_recovery_snapshot(
            self._agent.root,
            session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
            request_id=run_request_id, run_id=run_id, task_id=task_id,
            user_prompt=user_prompt, response_text=final_response.text,
            backend=final_response.backend, source=source, status="ok",
            tool_calls=archive_tool_calls, task_refs=recovery_task_refs or [],
            content_paths=[
                *(recovery_content_paths or []),
                *(getattr(routed_context, "required_read_paths", None) or []),
                *(getattr(routed_context, "candidate_paths", None) or []),
            ],
            next_actions=recovery_next_actions or [],
            archive_level=int(getattr(self._agent.config, "memory_hook_archive_level", 3)),
        )

    def _estimate_token_usage(self, user_prompt, runtime_injections, memories,
                              final_response, archive_tool_calls, run_request_id, turn_id):
        """Estimate and record token usage."""
        input_tokens = estimate_tokens(user_prompt) + estimate_tokens(runtime_injections) + estimate_tokens(
            [getattr(memory, "content", "") for memory in memories]
        )
        output_tokens = estimate_tokens(final_response.text)
        tool_tokens = estimate_tokens(archive_tool_calls)
        ledger = append_session_token_usage(
            self._agent.root,
            session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
            turn_id=turn_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_tokens=tool_tokens,
            created_at=str(time_module.time()),
        )
        return {
            "turn": int(ledger["turn_total"]),
            "cumulative": int(ledger["cumulative_tokens"]),
        }

    def _build_agent_run_result(self, ctx: FinalizeContext, archive_result, snapshot_result, token_ledger):
        """Build the final AgentRunResult object."""
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
            runtime_injection_token_estimate=estimate_tokens(ctx.runtime_injections) if ctx.runtime_injections else 0,
            recovery_snapshot_id=snapshot_result.snapshot_id if snapshot_result else "",
            recovery_snapshot_path=snapshot_result.path if snapshot_result else "",
            recovery_snapshot_error=snapshot_result.error if snapshot_result else "",
            recovery_snapshot_token_estimate=snapshot_result.token_estimate if snapshot_result else 0,
            memory_resume_context_injected=ctx.resume_context_result.injected if ctx.resume_context_result else False,
            memory_resume_context_query=ctx.resume_context_result.query if ctx.resume_context_result else "",
            memory_resume_context_matches=(
                ctx.resume_context_result.archive_match_count
                + ctx.resume_context_result.local_match_count
                + ctx.resume_context_result.task_fact_source_count
            ) if ctx.resume_context_result else 0,
            memory_resume_context_token_estimate=estimate_tokens(ctx.resume_context_result.context_block)
            if ctx.resume_context_result and ctx.resume_context_result.injected
            else 0,
            memory_resume_context_error=ctx.resume_context_result.error if ctx.resume_context_result else "",
            compression_snapshot_id=ctx.compression_snapshot_id,
            compression_snapshot_path=ctx.compression_snapshot_path,
            compression_applied=ctx.compression_applied,
            turn_token_estimate=token_ledger["turn"],
            cumulative_token_estimate=token_ledger["cumulative"],
        )
