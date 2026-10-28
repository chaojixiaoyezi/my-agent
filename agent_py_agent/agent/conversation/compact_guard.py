# LLM: 此模块拥有结构化近期尾部选择、持久会话压缩失败熔断与四字段可降级续跑分类器；不解析错误自然语言做控制或修改会话正文/游标，硬配额、取消与权限边界保持不可降级。
# 模块用途: 给唯一会话压缩链提供近期完整回合选择和连续失败熔断，防止截断对话或重复空烧模型。

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, TypeAlias

from ..runtime_errors import RecoverableRuntimeError
from .compact_message_source import CompactMessageSource, estimate_compact_messages
from .message_replay import message_rows_iterator
from .models import ConversationThread, MessageLogEntry
from .native_history import iter_provider_history_messages_from_rows
from .store_threads import IdleCompactActivityChanged

if TYPE_CHECKING:
    from .store import ConversationStore


# LLM: 所有压缩生产者在高开销及写入边界共用此只读回调合同；回调报错按失败关闭处理，丢弃未提交候选比丢失停止信号安全。
# 类型用途: 表示一次压缩是否已被当前回合要求停止；只读状态，不允许回调自己修改压缩账本。
CompactInterruptCheck: TypeAlias = Callable[[], bool]


# LLM: 只装候选容量的非负整数计量，字段名即公开进度字段名（与 compact_progress.COMPACT_CAPACITY_PROGRESS_FIELDS
#   由测试锁同步）；不带摘要正文、消息或路径，也不写进线程记录。改字段要同步 normalizer、TUI 白名单和两处生产者。
#   fixed_tokens 是同一投影器“只计量”入口在“空摘要、无保留”形态下的实测值，测不出为 None（公开字段里缺失，不写 0）；retained_ir_items/retained_ir_tokens 只统计
#   没被摘要、仍留在请求里的原生 IR，与只算工具/会话保留条数的 retained_items 分开，三个数都不由估算相减得出。
# 类用途: 说明“候选为什么装不下”：最小候选的完整下一请求多大、输入上限多少、其中摘要与固定开销各约占多少、
#   保留了几条（工具/会话、原生 IR 分开）、试了几个候选。
@dataclass(frozen=True)
class CompactCapacityFacts:
    candidate_tokens: int
    input_ceiling_tokens: int
    summary_tokens: int
    fixed_tokens: int | None
    retained_items: int
    retained_ir_items: int
    retained_ir_tokens: int
    candidates_tried: int


# LLM: 压缩失败是类型化运行时事实，调用方不得从文字推断重试分类。
#   capacity 只是失败时的诊断计量，不参与重试、熔断或错误码判定。
# 类用途: 表示摘要候选或 checkpoint 未通过，Gateway 会按可恢复运行时错误如实上报。
class ConversationCompactError(RecoverableRuntimeError):
    category = "conversation_compact"

    # LLM: 压缩专用code与通用运行时error_code必须一致，供Gateway原样传播。
    # 函数用途: 保留具体压缩失败原因，避免外围把窗口越界误报成会话文件损坏；容量类失败可附带候选计量。
    def __init__(self, message: str, *, code: str, capacity: CompactCapacityFacts | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.error_code = self.code
        self.capacity = capacity


# LLM: 此类型化失败在持久线程熔断冷却期间阻止重复调用模型。
# 类用途: 表示同一会话连续压缩失败已经熔断，冷却结束后才允许下一次真实尝试。
class ConversationCompactCircuitOpenError(ConversationCompactError):
    category = "conversation_compact_circuit"


# LLM: 会话、活动回合归档及原生IR压缩必须共用同一中断语义；检查点或CAS前抛出取消，已成功CAS绝不事后回滚。
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


# LLM: 优先保留有界的完整用户与助手回合后缀，再以不保留尾部的分区重试一次，防止单个巨大近期回合让压缩完全不可行。
# 函数用途: 以只读范围先保留近期完整对话，必要时用完整来源重试，不物化全部行。
def compact_partitions(
    rows: Sequence[MessageLogEntry],
    *,
    max_turns: int,
    max_tail_tokens: int,
) -> tuple[tuple[Sequence[MessageLogEntry], Sequence[MessageLogEntry]], ...]:
    prefix, tail = split_recent_complete_turns(
        rows,
        max_turns=max_turns,
        max_tail_tokens=max_tail_tokens,
    )
    if prefix and tail:
        return ((prefix, tail), (rows, []))
    return ((rows, []),)


# LLM: 被保护回合只按结构确定：用户消息之后、下个用户消息之前至少有一条助手消息；自然语言措辞不参与裁决。
# 函数用途: 在同一可重放历史上选择完整问答尾部，原token上限不变，分区只保存范围。
def split_recent_complete_turns(
    rows: Sequence[MessageLogEntry],
    *,
    max_turns: int,
    max_tail_tokens: int,
) -> tuple[Sequence[MessageLogEntry], Sequence[MessageLogEntry]]:
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


# LLM: 完整回合只依角色裁决；找到assistant即短路时也关闭地址读取，不依赖生成器GC。
# 函数用途: 在同一只读范围找出含assistant回复的user起点，不复制正文。
def _complete_turn_starts(
    rows: Sequence[MessageLogEntry],
    user_starts: list[int],
) -> list[int]:
    complete: list[int] = []
    for position, start in enumerate(user_starts):
        end = user_starts[position + 1] if position + 1 < len(user_starts) else len(rows)
        with message_rows_iterator(rows[start + 1 : end]) as turn:
            if any(row.role == "assistant" for row in turn):
                complete.append(start)
    return complete


# LLM: 尾部预算包含规范原生工具历史，不只计可见正文，否则短最终回复可能掩盖数十万保留token。
# 函数用途: 流式计量可见正文和真实原生历史，避免试算巨大完整回合时物化全部正文。
def _message_rows_tokens(rows: Sequence[MessageLogEntry]) -> int:
    return max(
        estimate_compact_messages(CompactMessageSource(lambda: ({'role': row.role, 'content': row.content} for row in rows))),
        estimate_compact_messages(CompactMessageSource(lambda: iter_provider_history_messages_from_rows(rows))),
    )


# LLM: 熔断状态持久化在ConversationThread，只阻止重复自动摘要，不改变会话记录、任务或记忆权威。
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


# LLM: 原模型/I/O错误优先于尽力记账异常；但闲置活动或消息CAS拒绝必须原样上抛，交Gateway干净让位。
# 函数用途: 向原线程失败写口传递闲置基线，存储竞争不吞掉、不增加失败数。
def record_compact_failure(
    store: ConversationStore,
    thread: ConversationThread,
    *,
    code: str,
    now: float,
    expected_user_activity_generation: int | None = None,
    expected_message_tail: dict[str, object] | None = None,
) -> None:
    expected = {}
    if expected_user_activity_generation is not None or expected_message_tail is not None:
        expected = {"expected_user_activity_generation": expected_user_activity_generation,
                    "expected_message_tail": expected_message_tail}
    try:
        store.threads.record_compact_failure(
            thread.thread_id,
            failure_code=code,
            expected_generation=thread.compact_generation,
            now=now,
            **expected,
        )
    except IdleCompactActivityChanged:
        raise
    except Exception:
        return


# LLM: B 路径（随图摘要）的 typed 失败只写线程 compact_vision_failed_generation，不增加 compact_consecutive_failures、
#   不触发熔断冷却；同代次下一次压缩读到它就选归档引用。存储异常吞掉，与 record_compact_failure 同一口径。
# 函数用途: 记录一次随图摘要失败，让同代次的下一次压缩改走归档引用。
def record_compact_vision_failure(
    store: ConversationStore,
    thread: ConversationThread,
    *,
    now: float,
) -> None:
    from .store_threads import record_thread_compact_vision_failure

    try:
        record_thread_compact_vision_failure(
            store.threads,
            thread.thread_id,
            expected_generation=thread.compact_generation,
            now=now,
        )
    except Exception:
        return


# LLM: 失败分类优先读类型化供应商或运行时error_code，再回退到异常身份；不解析异常正文，只输出有界字母数字错误码。
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


# 压缩调用撞模型额度用完时 compact_exception_code 给出的码：COMPACT_ 前缀加 ProviderQuotaExhaustedError.error_code。
COMPACT_PROVIDER_QUOTA_EXHAUSTED = "COMPACT_PROVIDER_QUOTA_EXHAUSTED"


# LLM: 只认结构化事实：ConversationCompactError 的码等于 COMPACT_PROVIDER_QUOTA_EXHAUSTED，或它的 __cause__ 链上有
#   ProviderQuotaExhaustedError；不看文案，其它异常类型一律 False。后台各处不直接调它，统一经 is_provider_quota_failure；
#   backends.errors.is_provider_quota_exhausted_error 的 isinstance 语义不变。改动时联测 test_compact_quota_wake.py。
# 函数用途: 判断一次压缩失败是不是因为模型额度用完，让后台按额度用完处理（发额度通知、Goal 记 usage_limited）。
def compact_error_is_provider_quota(exc: BaseException) -> bool:
    from ..backends.errors import is_provider_quota_exhausted_error

    if not isinstance(exc, ConversationCompactError):
        return False
    if exc.code == COMPACT_PROVIDER_QUOTA_EXHAUSTED:
        return True
    seen: set[int] = set()
    cause = exc.__cause__
    while cause is not None and id(cause) not in seen:
        if is_provider_quota_exhausted_error(cause):
            return True
        seen.add(id(cause))
        cause = cause.__cause__
    return False


# LLM: 会话层「模型额度用完」的唯一判定：直接的 ProviderQuotaExhaustedError，或 compact_error_is_provider_quota 认得的压缩包装
#   （大线程请求前先压缩，压缩调用撞额度时就是这种形态）。唤醒额度分路（runtime._run_wake_signal）、车道环境暂停
#   （cli/gateway_lane_retry._next_failure）、持久策略失败账（background_claim._counts_as_policy_failure）和毒丸不计数
#   （wake_poison._is_transient）都只读这一处。只看类型、结构化错误码和异常链，不看文案。放在会话层是因为后端层
#   （backends.errors）不能反向依赖会话层；那里的 is_provider_quota_exhausted_error 仍是 isinstance，后端内部调用方不受影响。
#   改动时联测 test_compact_quota_wake.py。
# 函数用途: 判断一次后台失败是不是模型额度用完（包括压缩时撞到的），让唤醒、车道、策略失败账和毒丸口径一致。
def is_provider_quota_failure(exc: BaseException) -> bool:
    from ..backends.errors import is_provider_quota_exhausted_error

    return is_provider_quota_exhausted_error(exc) or compact_error_is_provider_quota(exc)


# LLM: 工具循环内与宿主恢复统一先规范化 code/error_code/reason_code/failure_reason 四字段；
#   任一硬阻断码优先于瞬态异常类型。仅显式标注摘要解析的分支允许包含 JSONDecodeError；未知或结构性分支（含未标注 JSON 错误）一律保持不可降级失败关闭。
# 函数用途: 用统一结构化错误码判定摘要能否续跑，补齐软码但不让瞬断类型洗掉取消、权限或CAS硬停。
def compact_failure_is_degradable(exc: BaseException) -> tuple[bool, str]:
    import builtins
    from asyncio import CancelledError as AsyncCancelledError
    from concurrent.futures import CancelledError as FutureCancelledError
    from json import JSONDecodeError
    from urllib.error import HTTPError, URLError

    from ..backends.errors import (
        ProviderConfigurationError,
        ProviderQuotaExhaustedError,
        ProviderTimeoutError,
        ProviderTransientError,
        provider_error_http_status,
    )
    from ..common.cancellation import ToolCancelled
    from ..runtime_errors import DataCorruptionError

    hard = {
        "AUTHORITY_CONFLICT", "PIN_CONFLICT", "LEDGER_CORRUPT", "STORE_CORRUPT",
        "SCOPE_VIOLATION", "OWNER_MISMATCH", "SOURCE_CHANGED", "REQUEST_PROJECTION_UNKNOWN",
        "REQUEST_PROJECTION_CHANGED", "TOOL_COVERAGE_UNKNOWN", "REQUEST_NON_TEXT",
        "PROVIDER_QUOTA_EXHAUSTED", "QUOTA_EXCEEDED", "INSUFFICIENT_QUOTA", "BILLING",
        "AUTHENTICATION_ERROR", "INVALID_API_KEY", "CANCELLED", "CANCELED", "INTERRUPTED",
        "CAS_CONFLICT", "SCOPE_MISMATCH", "USER_CANCELLED", "AUTH_ERROR", "PERMISSION_DENIED",
    }
    soft = {"FAILED", "SUMMARY_EMPTY", "EMPTY_SUMMARY", "BUDGET_EXCEEDED", "TIMEOUT", "CIRCUIT_OPEN",
            "MODEL_CALL_FAILED", "PARSE_FAILED", "BACKEND_UNAVAILABLE"}
    pending, seen, eligible = [(exc, False)], set(), False
    reason = compact_exception_code(exc)
    while pending:
        current, parse_scope = pending.pop()
        visit = (id(current), parse_scope)
        if visit in seen:
            continue
        seen.add(visit)
        code = compact_exception_code(current).removeprefix("COMPACT_")
        codes = {code}
        for field in ("error_code", "code", "reason_code", "failure_reason"):
            value = getattr(current, field, None)
            if isinstance(value, str):
                codes.add(value.strip().upper().removeprefix("COMPACT_"))
        status = provider_error_http_status(current)
        if isinstance(current, HTTPError) and type(current.code) is int:
            status = current.code
        if codes & hard:
            return False, f"COMPACT_{sorted(codes & hard)[0]}"
        parse_scope = parse_scope or "PARSE_FAILED" in codes
        parse_leaf = parse_scope and isinstance(current, JSONDecodeError)
        if (status in {401, 402, 403, 407}
                or (isinstance(current, ValueError) and not parse_leaf)
                or isinstance(current, (ToolCancelled, AsyncCancelledError, FutureCancelledError,
                    KeyboardInterrupt, SystemExit, InterruptedError, PermissionError, DataCorruptionError,
                    ProviderQuotaExhaustedError, ProviderConfigurationError,
                    TypeError, LookupError, AssertionError))):
            return False, compact_exception_code(current)
        if current is exc and codes & soft:
            reason = f"COMPACT_{sorted(codes & soft)[0]}"
        known_soft = parse_leaf or bool(codes & soft) or status == 429 or (status is not None and status >= 500)
        known_soft = known_soft or isinstance(current, (ProviderTransientError, ProviderTimeoutError, TimeoutError, ConnectionError))
        # HTTPError继承URLError，但明确的永久4xx不能按网络瞬断放行；429/5xx已在上面判定。
        known_soft = known_soft or (isinstance(current, URLError) and not isinstance(current, HTTPError))
        # 只有真实异常组可作透明容器；任意未知叶/包装分支都拒绝，不能被外层soft洗白。
        if not known_soft and not isinstance(current, builtins.BaseExceptionGroup):
            return False, compact_exception_code(current)
        eligible = eligible or known_soft
        pending.extend((item, parse_scope) for item in (current.__cause__, current.__context__) if item is not None)
        if isinstance(current, builtins.BaseExceptionGroup):
            pending.extend((item, parse_scope) for item in current.exceptions)
    return eligible, reason


# LLM: 两条压缩链（会话 transcript 与活动回合）的 failed 进度都从这里取失败字段：安全错误码，加上错误自带的
#   容量计量（若有；测不出为 None 的项整项缺失）。只读异常对象，不改熔断账；字段必须经 compact_progress 白名单才会外发。
# 函数用途: 把一次压缩失败整理成进度事件要带的字段，让“候选过大”失败留下候选大小与上限。
def compact_failure_progress_fields(exc: BaseException) -> dict[str, object]:
    fields: dict[str, object] = {"error_code": compact_exception_code(exc)}
    capacity = getattr(exc, "capacity", None)
    if isinstance(capacity, CompactCapacityFacts):
        # 测不出的计量（None）整项缺失，不交给 normalizer 补成 0。
        fields.update({key: value for key, value in asdict(capacity).items() if value is not None})
    return fields


__all__ = [
    "CompactCapacityFacts",
    "CompactInterruptCheck",
    "ConversationCompactCircuitOpenError",
    "ConversationCompactError",
    "compact_circuit_is_open",
    "compact_exception_code",
    "compact_failure_progress_fields",
    "compact_failure_is_degradable",
    "compact_partitions",
    "raise_if_compact_interrupted",
    "record_compact_failure",
    "record_compact_vision_failure",
    "split_recent_complete_turns",
]
