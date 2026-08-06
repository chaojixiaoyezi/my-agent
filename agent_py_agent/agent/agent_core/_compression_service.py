
from __future__ import annotations

import time as time_module
from dataclasses import dataclass

from ..memory_archive import estimate_tokens, write_compression_snapshot
from ..memory_archive.snapshots import CompressionSnapshotInput
from ._runtime_params import CompressionContext


@dataclass(frozen=True)
class CompressionSnapshotContentParams:

    user_prompt: str
    memories: list[object]
    runtime_injections: list[str]
    routed_context: object
    resume_context_section: str


class CompressionService:

    def __init__(self, agent):
        self._agent = agent

    # LLM: Threshold crossing first records one pre_compact Curator reason, then Compact proceeds independently.
    # 函数用途: 判断是否需要压缩，保存恢复快照并返回压缩后的当前上下文记忆。
    def check_and_apply(self, ctx: CompressionContext):
        if self._full_prompt_estimate(ctx) <= int(getattr(self._agent.config, "max_tokens", 1024)):
            return ctx.memories, "", "", False

        self._request_curator_pre_compact()
        try:
            hook_result = write_compression_snapshot(
                self._agent.root,
                params=self._snapshot_input(ctx),
            )
        except Exception as exc:
            self._record_snapshot_failure(ctx, exc)
            raise RuntimeError(
                f"compression blocked: pre-compression snapshot failed: {exc}"
            ) from exc

        compressed_memories = self._compress_memories(
            ctx.memories,
            keep_recent=max(self._agent.config.memory_top_k, 2),
        )
        return compressed_memories, hook_result.snapshot_id, hook_result.snapshot_file_path, True

    # LLM: pre-compact is only a durable high-priority request; compaction must never wait for
    # or depend on Curator model success.
    # 函数用途: 向统一 MemoryCuratorService 登记压缩前提炼原因，失败时继续正常压缩。
    def _request_curator_pre_compact(self) -> None:
        curator = getattr(self._agent, "memory_curator", None)
        request = getattr(curator, "request", None)
        if not callable(request):
            return
        try:
            request("pre_compact")
        except Exception:
            return

    def _full_prompt_estimate(self, ctx: CompressionContext) -> int:
        return estimate_tokens(
            {
                "user_prompt": ctx.user_prompt,
                "memories": [getattr(memory, "content", "") for memory in ctx.memories],
                "inject": ctx.runtime_injections,
                "prompt_files": [],
            }
        )

    def _snapshot_input(self, ctx: CompressionContext) -> CompressionSnapshotInput:
        return CompressionSnapshotInput(
            session_id=getattr(self._agent, "session_id", self._agent.config.agent_name),
            turn_id=self._turn_id(ctx),
            role="system",
            content=self._snapshot_content(ctx),
            archive_level=int(getattr(self._agent.config, "memory_hook_archive_level", 3)),
            request_id=ctx.request_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            source=ctx.source,
            content_paths=self._snapshot_content_paths(ctx),
            next_actions=["Verify task facts first, then use the compression snapshot to recover context."],
        )

    def _turn_id(self, ctx: CompressionContext) -> str:
        return ctx.request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"

    def _snapshot_content(self, ctx: CompressionContext) -> str:
        return self._build_compression_snapshot_content(
            CompressionSnapshotContentParams(
                user_prompt=ctx.user_prompt,
                memories=ctx.memories,
                runtime_injections=ctx.runtime_injections,
                routed_context=ctx.routed_context,
                resume_context_section=ctx.resume_context_section,
            )
        )

    def _snapshot_content_paths(self, ctx: CompressionContext) -> list[str]:
        return [
            *(getattr(ctx.routed_context, "required_read_paths", None) or []),
            *(getattr(ctx.routed_context, "candidate_paths", None) or []),
        ]

    def _record_snapshot_failure(self, ctx: CompressionContext, exc: Exception) -> None:
        if not getattr(self._agent, "local_store", None):
            return
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

    def _compress_memories(self, memories: list[object], *, keep_recent: int) -> list[object]:
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
            content="Historical turns summary (recover by checking task/route authority files first):\n"
            + "\n".join(summary_lines),
            tags=["compression", "summary"],
        )
        return [summary, *recent]

    def _build_compression_snapshot_content(self, params: CompressionSnapshotContentParams) -> str:
        lines = [
            f"user_prompt={params.user_prompt}",
            f"memory_count={len(params.memories)}",
            f"routed_required={getattr(params.routed_context, 'required_read_paths', [])}",
            f"routed_candidates={getattr(params.routed_context, 'candidate_paths', [])}",
        ]
        if params.resume_context_section:
            lines.append("resume_context=" + params.resume_context_section[:800])
        if params.runtime_injections:
            lines.append("runtime_injections=" + "\n---\n".join(params.runtime_injections)[:2000])
        history = [
            f"{getattr(memory, 'role', 'memory')}: {getattr(memory, 'content', '')}"
            for memory in params.memories[-12:]
        ]
        if history:
            lines.append("recent_memories=" + "\n".join(history)[:3000])
        return "\n".join(lines)
