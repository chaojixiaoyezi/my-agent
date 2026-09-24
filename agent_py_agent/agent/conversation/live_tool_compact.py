# LLM: This module adapts a persistent active-turn native IR reduction to the same
# ConversationThread checkpoint/CAS authority used by transcript Compact. It never owns IR
# mutation, provider messages, task lifecycle, or UI counters.
# 模块用途: 把主代理、子代理和孙代理运行中的工具历史压缩提交到唯一会话账本，失败时保留原历史。

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tooling.call_ref import ToolCallRef
from .authority import (
    AGENT_THREAD_ID_ATTR,
    conversation_transcript_is_authoritative,
)
from .compact_checkpoint import (
    LiveToolCompactCheckpointRequest,
    validate_live_tool_call_boundary,
    write_live_tool_compact_checkpoint,
)
from .compact_guard import (
    CompactInterruptCheck,
    ConversationCompactCircuitOpenError,
    ConversationCompactError,
    compact_circuit_is_open,
    compact_exception_code,
    raise_if_compact_interrupted,
    record_compact_failure,
)
from .models import ConversationCompactCommit, ConversationThread

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent
    from .store import ConversationStore


# LLM: The binding is a point-in-time CAS candidate; callers must not reuse it after a commit.
# 类用途: 绑定一次运行中压缩要写入的 owner store 和精确 ConversationThread 代次。
@dataclass(frozen=True)
class LiveToolCompactBinding:
    store: ConversationStore
    thread: ConversationThread


# LLM: This immutable request keeps one live-tool candidate's summary, boundaries, usage and
# interruption check aligned through checkpoint/CAS; source refs carry original call identity,
# never the submitter identity. The UI callback remains observation-only.
# 类用途: 打包摘要、逐调用来源、token、提交者身份和停止检查，避免提交阶段参数错位。
@dataclass(frozen=True)
class LiveToolCompactCommitRequest:
    summary: str
    source_tool_call_ids: tuple[str, ...]
    retained_tool_call_ids: tuple[str, ...]
    projected_tokens_before: int
    projected_tokens_after: int
    policy: RuntimeCompactPolicy
    request_id: str
    attempt_id: str
    source_tool_call_refs: tuple[ToolCallRef, ...]
    retained_tool_call_refs: tuple[ToolCallRef | None, ...]
    forced: bool = False
    after_checkpoint: Callable[[], None] | None = None
    interrupt_check: CompactInterruptCheck | None = None


# LLM: Only a policy-approved transcript-authoritative turn may persist a live-tool Compact.
# save=True and an exact authoritative background slice are the two valid policy paths; agent
# threads take precedence over inherited parent conversation ids by explicit typed identity.
# 函数用途: 找到当前 main/child/grandchild 自己的压缩线程；辅助展示回合返回空，不写账。
def resolve_live_tool_compact_binding(
    agent: SimpleAgent,
    *,
    task_attributes: object,
    policy: RuntimeCompactPolicy,
) -> LiveToolCompactBinding | None:
    if not bool(getattr(policy, "allow_persistent_apply", False)):
        return None
    if not conversation_transcript_is_authoritative(task_attributes):
        return None
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    thread_id = str(
        attrs.get(AGENT_THREAD_ID_ATTR)
        or attrs.get("conversation_thread_id")
        or ""
    ).strip()
    if not thread_id:
        raise ConversationCompactError(
            "authoritative live tool compact has no conversation thread",
            code="COMPACT_THREAD_ID_MISSING",
        )
    store = getattr(agent, "conversation_store", None)
    if store is None:
        raise ConversationCompactError(
            "authoritative live tool compact has no conversation store",
            code="COMPACT_STORE_UNAVAILABLE",
        )
    thread, load_error = store.threads.load_report(thread_id)
    if load_error is not None or thread is None:
        raise ConversationCompactError(
            "authoritative live tool compact thread is unavailable",
            code="COMPACT_THREAD_UNAVAILABLE",
        )
    if compact_circuit_is_open(thread, policy, now=time.time()):
        raise ConversationCompactCircuitOpenError(
            "conversation compact is cooling down after repeated failures",
            code="COMPACT_CIRCUIT_OPEN",
        )
    return LiveToolCompactBinding(store=store, thread=thread)


# LLM: The checkpoint is written before one generation CAS, with the shared interrupt check on
# both sides of that write. Once CAS wins it is authoritative and must not be rolled back.
# 函数用途: 可中断地提交工具历史压缩；停止时最多留下孤立恢复点，不推进代次或游标。
def commit_live_tool_compact(
    agent: SimpleAgent,
    binding: LiveToolCompactBinding,
    request: LiveToolCompactCommitRequest,
) -> ConversationThread:
    source_ids, retained_ids, replacement = _validated_live_tool_request(request)
    raise_if_compact_interrupted(request.interrupt_check)
    checkpoint_id = write_live_tool_compact_checkpoint(
        agent,
        LiveToolCompactCheckpointRequest(
            thread=binding.thread,
            summary=replacement,
            source_tool_call_ids=source_ids,
            retained_tool_call_ids=retained_ids,
            source_tool_call_refs=request.source_tool_call_refs,
            retained_tool_call_refs=request.retained_tool_call_refs,
            projected_tokens_before=request.projected_tokens_before,
            projected_tokens_after=request.projected_tokens_after,
            policy=request.policy,
            request_id=str(request.request_id or ""),
            attempt_id=str(request.attempt_id or ""),
            forced=bool(request.forced),
        ),
    )
    if request.after_checkpoint is not None:
        try:
            request.after_checkpoint()
        except Exception:
            # 进度投影不是 Compact 权威；即使 TUI 已断开也必须继续完成同一 CAS。
            pass
    # Check after the durable candidate exists but before it receives live authority. An orphan
    # checkpoint is recoverable evidence; advancing generation after /stop is not.
    raise_if_compact_interrupted(request.interrupt_check)
    return binding.store.threads.update_compact_state(
        binding.thread.thread_id,
        commit=ConversationCompactCommit(
            summary=replacement,
            operation_evidence=dict(
                binding.thread.compact_operation_evidence or {}
            ),
            checkpoint_id=checkpoint_id,
            compacted_through_message_id=(
                binding.thread.compacted_through_message_id
            ),
            compacted_through_byte_offset=(
                binding.thread.compacted_through_byte_offset
            ),
            source_messages=binding.thread.compact_source_messages,
            source_tool_pairs=(
                binding.thread.compact_source_tool_pairs + len(source_ids)
            ),
        ),
        expected_generation=binding.thread.compact_generation,
    )


# LLM: 写账前验证精确来源；裸编号允许跨域重号，不去重，也不用于判定来源和尾部重叠。
# 函数用途: 在写账前校验一次工具历史 Compact 的移除区、保留区和完整摘要。
def _validated_live_tool_request(
    request: LiveToolCompactCommitRequest,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    source_ids = request.source_tool_call_ids
    retained_ids = request.retained_tool_call_ids
    replacement = str(request.summary or "").strip()
    if not source_ids:
        raise ConversationCompactError(
            "live tool compact removed no complete tool pairs",
            code="COMPACT_NO_SOURCE_TOOL_PAIRS",
        )
    if not replacement:
        raise ConversationCompactError(
            "live tool compact summary is empty",
            code="COMPACT_EMPTY_SUMMARY",
        )
    try:
        validate_live_tool_call_boundary(
            source_ids, retained_ids,
            request.source_tool_call_refs, request.retained_tool_call_refs,
        )
    except ValueError as exc:
        raise ConversationCompactError(
            str(exc),
            code="COMPACT_TOOL_BOUNDARY_INVALID",
        ) from exc
    return source_ids, retained_ids, replacement


# LLM: Failure accounting shares the transcript Compact circuit, but typed user interruption is a
# neutral discarded candidate and must never consume the failure budget.
# 函数用途: 真实压缩失败才累加同一 thread 的熔断事实；用户停止不算失败也不推进代次。
def record_live_tool_compact_failure(
    binding: LiveToolCompactBinding | None,
    exc: BaseException,
) -> None:
    if binding is None or isinstance(exc, InterruptedError):
        return
    record_compact_failure(
        binding.store,
        binding.thread,
        code=compact_exception_code(exc),
        now=time.time(),
    )


__all__ = [
    "LiveToolCompactBinding",
    "LiveToolCompactCommitRequest",
    "commit_live_tool_compact",
    "record_live_tool_compact_failure",
    "resolve_live_tool_compact_binding",
]
