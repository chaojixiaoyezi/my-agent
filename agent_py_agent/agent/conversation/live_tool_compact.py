# LLM: 活动IR压缩复用原ConversationThread检查点/CAS；精确refs决定覆盖，绑定scope决定是否发布全线程投影。
# 不拥有IR变更、provider消息或任务生命周期，宿主必须先准备适用摘要，失败保留原记录。
# 模块用途: 将运行中工具压缩提交到同一账本，区分局部摘要与全线程状态。

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..common.cancellation import ToolCancelled
from .authority import (
    AGENT_THREAD_ID_ATTR,
    conversation_transcript_is_authoritative,
)
from .compact_checkpoint import (
    LiveToolCompactCheckpointRequest,
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
from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from .compact_summary_view import AppliedCompactContext
from .compact_tool_identity import compact_tool_ref_key, compact_tool_refs
from .models import ConversationCompactCommit, ConversationThread

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent
    from .store import ConversationStore


# LLM: 绑定是某个CAS代次及适用scope的临时引用；scope只限制摘要范围，提交后不能复用旧绑定。
# 类用途: 固定本次活动压缩的原store、线程和摘要范围，不建立第二状态。
@dataclass(frozen=True)
class LiveToolCompactBinding:
    store: ConversationStore
    thread: ConversationThread
    scope: CompactScope = THREAD_COMPACT_SCOPE


# LLM: Exact source/retained refs and an explicit summary base travel through the original
# checkpoint/CAS; UI callback cannot grant authority or infer unknown source identity.
# 类用途: 打包活动摘要、四元调用边界、冻结摘要基础、计量和停止检查。
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
    source_tool_refs: tuple[dict[str, str], ...]
    retained_tool_refs: tuple[dict[str, str], ...] = ()
    summary_base_checkpoint_id: str | None = None
    forced: bool = False
    after_checkpoint: Callable[[], None] | None = None
    interrupt_check: CompactInterruptCheck | None = None


# LLM: Only a policy-approved transcript-authoritative turn may persist a live-tool Compact.
# save=True and an exact authoritative background slice are the two valid policy paths; agent
# threads take precedence over inherited parent ids; an explicit context fixes the same thread/scope.
# 函数用途: 找到当前代理压缩线程并绑定本次适用范围；辅助展示回合返回空，不写账。
def resolve_live_tool_compact_binding(
    agent: SimpleAgent,
    *,
    task_attributes: object,
    policy: RuntimeCompactPolicy,
    compact_context: AppliedCompactContext | None = None,
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
    if compact_context is not None:
        if not isinstance(compact_context, AppliedCompactContext):
            raise TypeError("Compact 应用上下文类型无效")
        if compact_context.thread_id != thread_id:
            raise ConversationCompactError(
                "applied compact context does not match the active thread",
                code="COMPACT_THREAD_ID_MISMATCH",
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
    return LiveToolCompactBinding(
        store=store,
        thread=thread,
        scope=compact_context.scope if compact_context is not None else THREAD_COMPACT_SCOPE,
    )


# LLM: scope/base在原CAS前保持同源；回调显式取消必须透传并在CAS前复查令牌，获胜提交不回滚。
# 函数用途: 按冻结范围与摘要基础可中断地提交工具压缩，停止时最多留下孤立恢复点。
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
            projected_tokens_before=request.projected_tokens_before,
            projected_tokens_after=request.projected_tokens_after,
            policy=request.policy,
            request_id=str(request.request_id or ""),
            attempt_id=str(request.attempt_id or ""),
            source_tool_refs=request.source_tool_refs,
            retained_tool_refs=request.retained_tool_refs,
            forced=bool(request.forced),
            scope=binding.scope,
            summary_base_checkpoint_id=request.summary_base_checkpoint_id,
        ),
    )
    if request.after_checkpoint is not None:
        try:
            request.after_checkpoint()
        except (InterruptedError, ToolCancelled):
            raise
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
            publish_thread_view=binding.scope.kind == "thread",
        ),
        expected_generation=binding.thread.compact_generation,
    )


# LLM: 只检查四元来源与保留区交集，不能用call_id展示别名拒绝合法跨轮调用；写候选前必须通过。
# 函数用途: 校验工具Compact的准确边界和完整摘要，不让未知调用身份推进检查点。
def _validated_live_tool_request(
    request: LiveToolCompactCommitRequest,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    source_refs = compact_tool_refs(list(request.source_tool_refs))
    retained_refs = compact_tool_refs(list(request.retained_tool_refs))
    source_ids = tuple(ref["call_id"] for ref in source_refs)
    retained_ids = tuple(ref["call_id"] for ref in retained_refs)
    if source_ids != request.source_tool_call_ids or retained_ids != request.retained_tool_call_ids:
        raise ConversationCompactError(
            "compact display call ids mismatch exact source references",
            code="COMPACT_TOOL_BOUNDARY_INVALID",
        )
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
    if {compact_tool_ref_key(ref) for ref in source_refs} & {compact_tool_ref_key(ref) for ref in retained_refs}:
        raise ConversationCompactError(
            "live tool compact source and retained calls overlap",
            code="COMPACT_TOOL_BOUNDARY_INVALID",
        )
    return source_ids, retained_ids, replacement


# LLM: 失败共用transcript熔断账；InterruptedError和ToolCancelled均为中性丢弃，不消耗失败预算。
# 函数用途: 真实压缩失败才累加同一 thread 的熔断事实；用户停止不算失败也不推进代次。
def record_live_tool_compact_failure(
    binding: LiveToolCompactBinding | None,
    exc: BaseException,
) -> None:
    if binding is None or isinstance(exc, (InterruptedError, ToolCancelled)):
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
