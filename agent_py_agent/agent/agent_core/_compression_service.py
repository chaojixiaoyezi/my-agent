"""CompressionService: compression check, snapshot, and compression logic."""

from __future__ import annotations

import time as time_module

from ..memory_archive import (
    estimate_tokens,
    snapshots,
)
from ..memory_archive.snapshots import CompressionSnapshotInput
from . import runtime_services
from ._runtime_params import CompressionContext


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
            hook_result = runtime_services.write_compression_snapshot(
                self._agent.root,
                params=CompressionSnapshotInput(
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
                ),
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
            raise RuntimeError(
                f"compression blocked: pre-compression snapshot failed: {exc}"
            ) from exc
        compressed_memories = self._compress_memories(
            ctx.memories, keep_recent=max(self._agent.config.memory_top_k, 2)
        )
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
        summary_lines = [
            f"{getattr(item, 'role', 'memory')}: {getattr(item, 'content', '')}"
            for item in older[-12:]
        ]
        summary = MemoryRecord(
            role="system",
            kind="summary",
            content="历史轮次摘要（恢复时必须回到 task/route 权威文件核验）:\n"
            + "\n".join(summary_lines),
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