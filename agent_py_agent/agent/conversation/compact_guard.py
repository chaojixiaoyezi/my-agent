# LLM: This module owns structural recent-tail selection and the persisted conversation compact
# failure circuit. It never reads natural-language meaning or mutates transcript/cursor state.
# 模块用途: 给唯一会话压缩链提供近期完整回合选择和连续失败熔断，防止截断对话或重复空烧模型。

from __future__ import annotations

from typing import TYPE_CHECKING

from ..memory_archive import estimate_tokens
from ..runtime_errors import RecoverableRuntimeError
from .models import ConversationThread, MessageLogEntry

if TYPE_CHECKING:
    from .store import ConversationStore


# LLM: Compact failures are typed runtime facts; callers must not infer a retry class from text.
# 类用途: 表示摘要候选或 checkpoint 未通过，Gateway 会按可恢复运行时错误如实上报。
class ConversationCompactError(RecoverableRuntimeError):
    category = "conversation_compact"

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = str(code)


# LLM: This typed failure prevents repeated model calls while the persisted thread circuit cools.
# 类用途: 表示同一会话连续压缩失败已经熔断，冷却结束后才允许下一次真实尝试。
class ConversationCompactCircuitOpenError(ConversationCompactError):
    category = "conversation_compact_circuit"


# LLM: Prefer one bounded suffix of complete user/assistant turns, then retry once with no tail.
# The second partition prevents a very large recent turn from making compaction impossible.
# 函数用途: 先尝试保留近期完整对话；若候选仍太大，再用完整旧段生成一个无尾部候选。
def compact_partitions(
    rows: list[MessageLogEntry],
    *,
    max_turns: int,
    max_tail_tokens: int,
) -> tuple[tuple[list[MessageLogEntry], list[MessageLogEntry]], ...]:
    prefix, tail = split_recent_complete_turns(
        rows,
        max_turns=max_turns,
        max_tail_tokens=max_tail_tokens,
    )
    if prefix and tail:
        return ((prefix, tail), (rows, []))
    return ((rows, []),)


# LLM: A protected turn is structural: one user message followed by at least one assistant
# message before the next user. No natural-language wording participates in this decision.
# 函数用途: 从历史末尾保留最多若干个完整问答，受统一 token 上限约束，其余旧段交给摘要。
def split_recent_complete_turns(
    rows: list[MessageLogEntry],
    *,
    max_turns: int,
    max_tail_tokens: int,
) -> tuple[list[MessageLogEntry], list[MessageLogEntry]]:
    if max_turns <= 0 or max_tail_tokens <= 0 or len(rows) < 2:
        return rows, []
    user_starts = [index for index, row in enumerate(rows) if row.role == "user"]
    complete_starts = _complete_turn_starts(rows, user_starts)
    selected_start = len(rows)
    for selected_turns, start in enumerate(reversed(complete_starts), start=1):
        if start <= 0 or selected_turns > max_turns:
            break
        if _message_rows_tokens(rows[start:]) > max_tail_tokens:
            break
        selected_start = start
    if selected_start >= len(rows):
        return rows, []
    return rows[:selected_start], rows[selected_start:]


# LLM: Completion is role-structural and deliberately ignores prose and message language.
# 函数用途: 找出含有 assistant 回复的 user 起点，供近期完整回合选择使用。
def _complete_turn_starts(
    rows: list[MessageLogEntry],
    user_starts: list[int],
) -> list[int]:
    complete: list[int] = []
    for position, start in enumerate(user_starts):
        end = user_starts[position + 1] if position + 1 < len(user_starts) else len(rows)
        if any(row.role == "assistant" for row in rows[start + 1 : end]):
            complete.append(start)
    return complete


# LLM: Tail budgeting uses the same estimator as the complete request projection.
# 函数用途: 估算一段原始对话消息的 token，用于近期尾部上限。
def _message_rows_tokens(rows: list[MessageLogEntry]) -> int:
    return estimate_tokens(
        [{"role": row.role, "content": row.content} for row in rows]
    )


# LLM: Circuit state is persisted on ConversationThread; it blocks only repeated automatic
# summary work and never changes transcript, task, or memory authority.
# 函数用途: 判断同一会话是否在连续失败后的冷却期内，避免每条消息都重复烧模型。
def compact_circuit_is_open(
    thread: ConversationThread,
    policy: object,
    *,
    now: float,
) -> bool:
    threshold = max(0, int(getattr(policy, "failure_threshold", 0) or 0))
    cooldown = max(
        0.0,
        float(getattr(policy, "failure_cooldown_seconds", 0.0) or 0.0),
    )
    if threshold <= 0 or thread.compact_consecutive_failures < threshold:
        return False
    return (now - thread.compact_failure_updated_at) < cooldown


# LLM: Failure bookkeeping is best-effort and must not replace the original model or I/O error.
# 函数用途: 在不遮住真实异常的前提下，给 thread 累加一次压缩失败。
def record_compact_failure(
    store: ConversationStore,
    thread: ConversationThread,
    *,
    code: str,
    now: float,
) -> None:
    try:
        store.record_compact_failure(
            thread.thread_id,
            failure_code=code,
            expected_generation=thread.compact_generation,
            now=now,
        )
    except Exception:
        return


# LLM: Failure classification is based on typed exception identity, never message text.
# 函数用途: 把真实异常类型转成小型稳定错误码，供熔断状态和诊断展示。
def compact_exception_code(exc: BaseException) -> str:
    if isinstance(exc, ConversationCompactError):
        return exc.code
    name = "".join(
        character if character.isalnum() else "_"
        for character in exc.__class__.__name__.upper()
    )
    return f"COMPACT_{name or 'FAILED'}"


__all__ = [
    "ConversationCompactCircuitOpenError",
    "ConversationCompactError",
    "compact_circuit_is_open",
    "compact_exception_code",
    "compact_partitions",
    "record_compact_failure",
    "split_recent_complete_turns",
]
