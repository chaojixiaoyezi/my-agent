# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import time as time_module
from dataclasses import dataclass

from ..memory_archive import estimate_tokens
from ..memory_archive.snapshots import CompressionSnapshotInput
from . import runtime_services
from ._runtime_params import CompressionContext


# LLM: CompressionSnapshotContentParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存压缩snapshot内容参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class CompressionSnapshotContentParams:

    user_prompt: str
    memories: list[object]
    runtime_injections: list[str]
    routed_context: object
    resume_context_section: str


# LLM: CompressionService 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装压缩服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class CompressionService:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent):
        self._agent = agent

    # LLM: check_and_apply 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 校验应用需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 会更新运行循环、工具调用、调度记录和最终响应，需避免破坏既有状态机约定。
    def check_and_apply(self, ctx: CompressionContext):
        if self._full_prompt_estimate(ctx) <= int(getattr(self._agent.config, "max_tokens", 1024)):
            return ctx.memories, "", "", False

        try:
            hook_result = runtime_services.write_compression_snapshot(
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

    # LLM: _full_prompt_estimate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理full提示词estimate相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def _full_prompt_estimate(self, ctx: CompressionContext) -> int:
        return estimate_tokens(
            {
                "user_prompt": ctx.user_prompt,
                "memories": [getattr(memory, "content", "") for memory in ctx.memories],
                "inject": ctx.runtime_injections,
                "prompt_files": [],
            }
        )

    # LLM: _snapshot_input 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理snapshotinput相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: _turn_id 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理turnid相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _turn_id(self, ctx: CompressionContext) -> str:
        return ctx.request_id or ctx.run_id or ctx.task_id or f"turn-{time_module.time_ns()}"

    # LLM: _snapshot_content 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理snapshot内容相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: _snapshot_content_paths 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理snapshot内容路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _snapshot_content_paths(self, ctx: CompressionContext) -> list[str]:
        return [
            *(getattr(ctx.routed_context, "required_read_paths", None) or []),
            *(getattr(ctx.routed_context, "candidate_paths", None) or []),
        ]

    # LLM: _record_snapshot_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入snapshot失败的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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

    # LLM: _compress_memories 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理compressmemories相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: _build_compression_snapshot_content 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建压缩snapshot内容所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
