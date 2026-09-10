# LLM: This module owns structural recent-tail selection and the persisted conversation compact
# failure circuit. It never reads natural-language meaning or mutates transcript/cursor state.
# 模块用途: 给唯一会话压缩链提供近期完整回合选择和连续失败熔断，防止截断对话或重复空烧模型。

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeAlias

from ..memory_archive import estimate_tokens
from ..runtime_errors import RecoverableRuntimeError
from .models import ConversationThread, MessageLogEntry
from .native_history import provider_history_messages_from_rows

if TYPE_CHECKING:
    from .store import ConversationStore


# LLM: Every Compact producer uses this read-only callback contract at expensive and mutating
# boundaries. Callback errors fail closed because losing the stop signal is more dangerous than
# discarding an uncommitted candidate.
# 类型用途: 表示一次压缩是否已被当前回合要求停止；只读状态，不允许回调自己修改压缩账本。
CompactInterruptCheck: TypeAlias = Callable[[], bool]


# LLM: Compact failures are typed runtime facts; callers must not infer a retry class from text.
# 类用途: 表示摘要候选或 checkpoint 未通过，Gateway 会按可恢复运行时错误如实上报。
class ConversationCompactError(RecoverableRuntimeError):
    category = "conversation_compact"

    # LLM: Keep the Compact-specific code and generic runtime error_code identical for Gateway propagation.
    # 函数用途: 保留具体压缩失败原因，避免外围把窗口越界误报成会话文件损坏。
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.error_code = self.code


# LLM: This typed failure prevents repeated model calls while the persisted thread circuit cools.
# 类用途: 表示同一会话连续压缩失败已经熔断，冷却结束后才允许下一次真实尝试。
class ConversationCompactCircuitOpenError(ConversationCompactError):
    category = "conversation_compact_circuit"


# LLM: Transcript, active-turn archive and native-IR Compact must share this exact interruption
# interpretation. It raises before checkpoint/CAS; a successful CAS is never rolled back later.
# 函数用途: 在压缩的模型调用、内存候选、恢复点和最终提交前统一检查用户停止信号。
def raise_if_compact_interrupted(check: CompactInterruptCheck | None) -> None:
    if check is None:
        return
    try:
        interrupted = bool(check())
    except Exception:
        interrupted = True
    if interrupted:
        raise InterruptedError("conversation compact interrupted by user")


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


# LLM: Tail budgeting includes canonical native tool history, not only visible prose;
# otherwise a short final reply can hide hundreds of thousands of retained tokens.
# 函数用途: 按正文和真实原生工具历史的较大值计算尾部，避免保留区暗藏整轮巨大工具输出。
def _message_rows_tokens(rows: list[MessageLogEntry]) -> int:
    return max(
        estimate_tokens([{"role": row.role, "content": row.content} for row in rows]),
        estimate_tokens(provider_history_messages_from_rows(rows)),
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


# LLM: Failure classification prefers a typed provider/runtime error_code, then falls back to
# exception identity. It never parses message text and only emits bounded alphanumeric codes.
# 函数用途: 把真实异常的结构化错误码转成稳定 Compact 错误码，供熔断状态和界面诊断展示。
def compact_exception_code(exc: BaseException) -> str:
    typed_code = str(getattr(exc, "error_code", "") or "").strip()
    source = (
        str(exc.code)
        if isinstance(exc, ConversationCompactError)
        else (typed_code or exc.__class__.__name__)
    )
    name = "".join(
        character if character.isalnum() else "_"
        for character in source.upper()
    )[:96].strip("_")
    if name.startswith("COMPACT_"):
        return name
    return f"COMPACT_{name or 'FAILED'}"


__all__ = [
    "CompactInterruptCheck",
    "ConversationCompactCircuitOpenError",
    "ConversationCompactError",
    "compact_circuit_is_open",
    "compact_exception_code",
    "compact_partitions",
    "raise_if_compact_interrupted",
    "record_compact_failure",
    "split_recent_complete_turns",
]
