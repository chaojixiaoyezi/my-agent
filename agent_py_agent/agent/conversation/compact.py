# LLM: 单一会话压缩链保持原文、证据、尾部、检查点和游标分离；独立请求负责结算模型用量，失败形状不含正文且不改变收口。
# 同片展示只经宿主冻结面进入摘要；完整恢复projector（若提供）是候选计量来源，其材料仅在原CAS成功后返回。
# 联合候选绑定消息及工具分区，须满足原输入/输出门；备份收缩只用完整同源重测裁决，不以局部相减跳测；取消不提交、不计熔断。
# 人格缓存面只绑定本次已提交线程，不使用候选代次；与普通提示冻结共享唯一快照。
# 写检查点与CAS后的冷转显式共用owner compact根；维护失败不能回滚已发布摘要。
# 模块用途: 复用可容纳主前缀，必超预算时先释放优化再沿原分段恢复；完整原文和 checkpoint/CAS 保持权威。

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from ..backends.request_content import compact_source_supported
from ..common.cancellation import ToolCancelled
from ..memory_archive import estimate_tokens
from .channels import project_user_reply
from .compact_calibration import CompactRequestCalibration, calibrated_compact_request_tokens
from .compact_checkpoint import (
    CompactCheckpointRequest,
    _compact_root,
    write_compact_checkpoint,
)
from .compact_guard import (
    CompactCapacityFacts,
    CompactInterruptCheck,
    ConversationCompactCircuitOpenError,
    ConversationCompactError,
    compact_circuit_is_open,
    compact_exception_code,
    compact_failure_progress_fields,
    compact_partitions,
    raise_if_compact_interrupted,
    record_compact_failure,
    record_compact_vision_failure,
)
from .compact_landmarks import (
    LandmarkOptions,
    LandmarkSource,
    landmark_options,
    landmark_source,
    render_landmarked_summary,
    semantic_summary_text,
)
from .compact_media_digest import (
    MediaDigestContext,
    largest_digest_request_tokens,
    summarize_media_turns,
)
from .compact_media_policy import (
    COMPACT_VISION_SUMMARY_FAILED,
    MEDIA_POLICY_ARCHIVED_REFS,
    MEDIA_POLICY_OFF,
    MEDIA_POLICY_VISION_SUMMARY,
    CompactMediaDecision,
    MediaArchiveFacts,
    archived_refs_fallback,
    media_archive_facts,
    project_archived_media_message,
    resolve_compact_media_policy,
    resolve_vision_candidate,
    vision_digest_max_requests,
    vision_summary_admission,
)
from .compact_message_source import CompactMessageSource, estimate_compact_payload
from .compact_progress import (
    COMPACT_AUTHORITY_CONVERSATION,
    COMPACT_SOURCE_TRANSCRIPT,
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
)
from .compact_projection import (
    CompactRequestProjector,
    ConversationCompactProjection,
    ConversationCompactSource,
    ConversationCompactView,
    project_compact_request,
)
from .compact_provider_surface import (
    ConversationCompactModelSurface,
    ConversationCompactProviderSurface,
    conversation_compact_provider_messages,
    conversation_compact_provider_prompt,
    conversation_compact_provider_source,
    prepare_conversation_compact_provider_surface,
)
from .compact_scope import CompactScope
from .message_replay import (
    MessageSnapshotRows,
    concatenate_message_rows,
    filtered_message_rows,
    message_rows_iterator,
)
from .message_selection import MessageSelectorFactory, select_message_snapshot
from .models import (
    ConversationCompactCommit,
    ConversationThread,
    MessageLogEntry,
    is_audit_background_transcript_entry,
)
from .native_history import (
    _row_turn_identity,
    canonical_native_messages_from_metadata,
    iter_provider_history_messages_from_rows,
    provider_history_messages_from_rows,
)
from .store_threads import IdleCompactActivityChanged
from .tool_context_window import (
    TERMINAL_TOOL_FOLD_METADATA_KEY,
    conversation_message_with_terminal_tool_fold,
    conversation_terminal_tool_fold,
    conversation_terminal_tool_fold_projection,
)

if TYPE_CHECKING:
    from ..agent_core.runtime.context_compactor import RuntimeCompactPolicy
    from ..core import SimpleAgent
    from .active_turn_compact import CarriedToolCompactSource
    from .compact_summary_view import AppliedCompactContext
    from .store import ConversationStore

_MAX_COMPACT_OPERATION_EVENTS = 32
# 空响应回退文本最多 12000 字符：控制重放体积。
_EMPTY_RESPONSE_FALLBACK_MAX_CHARS = 12_000
_VERIFICATION_COUNT_KEYS = (
    "succeeded",
    "failed",
    "not_started",
    "unknown",
    "cancelled",
    "incomplete",
    "unverified",
)


# LLM: 作用域只是已解析所有者与线程绑定的只读投影，不重新裁决身份。
# 类用途: 把当前会话的 owner、thread 和通道身份整理成统一只读结构。
@dataclass(frozen=True)
class ConversationScope:
    owner_id: str
    owner_home: str
    thread_id: str
    canonical_user_id: str
    channel: str
    channel_conversation_id: str
    channel_user_id: str


# LLM: 返回原CAS确认的thread与原文尾部；request_projection只在提交成功时携带获选内存材料，不进入持久历史。
# 类用途: 将压缩结果、同源只读尾部及完整请求交回宿主，不误用最后一个未采用候选。
@dataclass(frozen=True)
class ConversationCompactResult:
    thread: ConversationThread
    messages: Sequence[MessageLogEntry]
    projected_tokens: int
    trigger_tokens: int
    compacted: bool = False
    recent_operation_evidence: dict[str, object] | None = None
    request_projection: ConversationCompactProjection | None = field(default=None, repr=False)


# LLM: 此只读投影必须与自动预检使用同一计量器和压缩策略；界面命令只负责渲染，不得以缓存展示文字替代真实投影。
# 类用途: 保存当前会话真正的上下文估算、窗口、自动压缩线和历史代际。
@dataclass(frozen=True)
class ConversationContextUsage:
    projected_tokens: int
    context_window_tokens: int
    trigger_percent: int
    trigger_tokens: int
    compact_generation: int
    compact_source_messages: int
    compact_source_tool_pairs: int
    terminal_tool_fold_turns: int
    terminal_tool_fold_calls: int
    pending_messages: int
    has_summary: bool
    # 触发线被 memory_compact_auto_trigger_max_tokens 压下来时为 True（取自 RuntimeCompactPolicy.trigger_capped）。
    trigger_capped: bool = False


# LLM: 内部宿主复用原来源/projector/摘要面；手动请求可显式绕过自动冷却但仍记录结果，不绕过来源/CAS校验。
# 类用途: 统一压缩输入、停止检查与缓存面；闲置输入同时固定活动代及规范消息指纹。
@dataclass(frozen=True)
class ConversationCompactOptions:
    current_prompt: str = ""
    exclude_request_id: str = ""
    force: bool = False
    custom_instructions: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = None
    interrupt_check: CompactInterruptCheck | None = None
    model_surface: ConversationCompactModelSurface | None = None
    source: ConversationCompactSource | None = field(default=None, repr=False)
    request_projector: CompactRequestProjector | None = field(default=None, repr=False)
    provider_surface: ConversationCompactProviderSurface | None = field(default=None, repr=False)
    tool_source: CarriedToolCompactSource | None = field(default=None, repr=False)
    # LLM: force 表示"整段已完成前缀一次压完"（恢复宿主与手动 /compact 都这样用）；pressure_forced 才表示窗口上限/供应商施压的
    #   恢复——媒体策略只按它决定是否禁用随图摘要，阈值自动压缩与手动 /compact 都可走 B。
    pressure_forced: bool = False
    # LLM: 宿主边界冻结的校准事实（同 fingerprint、同代次的供应商观测）：只套在 request_projector 交回的完整请求计量上
    #   （压缩前与每个候选），本地估算路径不用它；None 表示没有可用观测，按原始投影值判定，不猜系数。
    calibration: CompactRequestCalibration | None = None
    # LLM: 结构化触发来源（compact_progress.COMPACT_TRIGGER_*），只进进度事件，不参与任何判定；空串表示手动或未知。
    trigger_source: str = ""
    # 显式手动/compact绕过自动压缩的冷却，结果仍走原失败账；成功提交会重置熔断，自动调用默认不绕过。
    bypass_circuit_breaker: bool = False
    # 闲置维护沿原提交链核活动代；guard只在最终短提交门运行，不持锁调用模型。
    expected_user_activity_generation: int | None = None
    expected_message_tail: dict[str, object] | None = field(default=None, repr=False)
    idle_compact: bool = False
    commit_guard: Callable[[], bool] | None = field(default=None, repr=False)


# LLM: 不可变请求绑定原CAS状态、显式摘要作用域和已准备投影；局部候选不能回读全线程摘要。
# 类用途: 将会话、可重放历史和闲置消息CAS基线交给原提交链，不建立第二历史来源。
@dataclass(frozen=True)
class _CompactRunRequest:
    agent: SimpleAgent
    store: ConversationStore
    thread: ConversationThread
    current_prompt: str
    pending: Sequence[MessageLogEntry]
    policy: RuntimeCompactPolicy
    projected_tokens: int
    forced: bool
    attempted_at: float
    operation_id: str
    standalone_usage: bool = False
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    custom_instructions: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = None
    interrupt_check: CompactInterruptCheck | None = None
    model_surface: ConversationCompactModelSurface | None = None
    request_projector: CompactRequestProjector | None = field(default=None, repr=False)
    provider_surface: ConversationCompactProviderSurface | None = field(default=None, repr=False)
    tool_source: CarriedToolCompactSource | None = field(default=None, repr=False)
    compact_context: AppliedCompactContext | None = field(default=None, repr=False)
    # LLM: 只有窗口上限/供应商施压的恢复为真；options.force（整段压完）不等于施压，媒体策略据此决定是否关闭随图摘要。
    pressure_forced: bool = field(default=False, kw_only=True)
    # LLM: 与 ConversationCompactOptions 同义：冻结校准只套在投影器计量上；触发来源只进进度事件。
    calibration: CompactRequestCalibration | None = field(default=None, kw_only=True, repr=False)
    trigger_source: str = field(default="", kw_only=True)
    expected_user_activity_generation: int | None = field(default=None, kw_only=True)
    expected_message_tail: dict[str, object] | None = field(default=None, kw_only=True, repr=False)
    idle_compact: bool = field(default=False, kw_only=True)
    commit_guard: Callable[[], bool] | None = field(default=None, kw_only=True, repr=False)


# LLM: 摘要、双来源分区及request_projection属于同一候选；保留候选回退时必须同时采用，不能取循环最后一次分区。
# 类用途: 保存未提交摘要、同源范围及完整恢复请求，不物化来源正文或反写活参数。
@dataclass(frozen=True)
class _CompactCandidate:
    summary: str
    operation_evidence: dict[str, object]
    compact_rows: Sequence[MessageLogEntry]
    retained_tail: Sequence[MessageLogEntry]
    # 接受门用的计量：有投影器且有冻结校准时是折算后的值，request_projection.projected_tokens 仍是原始纯投影。
    projected_tokens_after: int
    request_projection: ConversationCompactProjection | None = field(default=None, repr=False)
    tool_source: CarriedToolCompactSource | None = field(default=None, repr=False)
    # 服务端压缩记录（compact_remote）；None = 普通文字摘要。
    provider_compaction: dict[str, object] | None = field(default=None, repr=False)
    # LLM: 本候选对媒体块采取的策略与将被归档的媒体事实；None/空表示本次范围内没有媒体或策略为 off，提交时据此决定是否写 checkpoint 字段。
    media_decision: CompactMediaDecision | None = field(default=None, repr=False)
    media_facts: MediaArchiveFacts = field(default_factory=MediaArchiveFacts, repr=False)


# LLM: 摘要调用身份、冻结缓存面及真实工具来源一起传递；严格恢复的机械回退必须完整保留旧摘要和所选原文。
# 类用途: 收拢一次摘要模型调用的软指令、运行身份、压缩代次及来源，不拥有提交权。
@dataclass(frozen=True)
class _CompactSummaryCall:
    custom_instructions: str = ""
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    thread_id: str = ""
    compact_generation: int = 0
    provider_surface: ConversationCompactProviderSurface | None = None
    interrupt_check: CompactInterruptCheck | None = None
    source_progress: Callable[[int, int], object] | None = None
    tool_source_records: tuple[dict[str, object], ...] = ()
    tool_source_ir_history: tuple[object, ...] = ()
    preserve_complete_fallback: bool = False
    # LLM: media_policy 决定摘要来源重放时是否把媒体块投影为归档引用；media_archived 只影响摘要软指令，不参与判定。
    #   media_decision/media_facts 供 B 候选在请求前做字节/预算准入；实际采用的决定追加到 media_outcome（调用方提供的列表）。
    media_policy: str = MEDIA_POLICY_OFF
    media_archived: bool = False
    media_decision: CompactMediaDecision | None = None
    media_facts: MediaArchiveFacts = field(default_factory=MediaArchiveFacts)
    media_outcome: list[CompactMediaDecision] | None = None
    # LLM: 调用方提供的列表；_summarize 追加一个可按更小原话备份预算重建同一摘要文本的对象，只供本候选超出恢复目标时收缩备份。
    landmark_outcome: list[_LandmarkRebuild] | None = None
    # 服务端压缩：provider_outcome 是调用方提供的列表，走了远端路径时追加记录；previous_provider_compaction 是上一代压缩项密文，
    #   来源前缀据此发压缩项而不是占位文本（链式压缩、与主请求逐字一致）。
    provider_outcome: list[dict[str, object]] | None = None
    previous_provider_compaction: str = ""
    # 主请求的校准事实：单次缓存面与服务端压缩的预算检查按它换算，不再用原始上界误判超预算（10-08 mc4 复盘）。
    calibration: CompactRequestCalibration | None = None


# LLM: 只重算原话备份段，语义摘要、机械回退正文与尾随来源原样保留；不发模型请求、不写任何状态。
# 类用途: 记住一次摘要的语义部分与原话备份输入，候选超出恢复目标时按更小预算重建全文。
@dataclass(frozen=True)
class _LandmarkRebuild:
    semantic: str
    source: LandmarkSource
    trailing: str
    options: LandmarkOptions
    used_tokens: int

    # LLM: 只重算备份段；语义摘要与尾随原文逐字不变，负数预算按 0 处理。
    # 函数用途: 用给定的原话备份 token 上限重建摘要全文（复用第一遍结果，只按下标重读被选中的行）。
    def render(self, max_tokens: int) -> str:
        options = replace(self.options, landmark_budget_tokens=max(0, max_tokens))
        return render_landmarked_summary(self.semantic, self.source, options).text + self.trailing

    # LLM: 预算为 0 时备份段仍保留标题、省略编号与回查说明；收缩判断必须计入这部分，不能按整段可删到 0 估算。
    # 函数用途: 返回备份段能缩到的最小 token 数（不读任何原文正文）。
    def minimum_tokens(self) -> int:
        return render_landmarked_summary(self.semantic, self.source, replace(self.options, landmark_budget_tokens=0)).used_tokens


# LLM: 摘要模型返回后的收尾输入；rows 是本次全部行（机械回退保留原文用），foreground_rows 是去掉后台审计投递的行。
# 类用途: 收拢摘要收尾材料和结构化回退原因，上一代摘要、原文与证据仍守原权威。
@dataclass(frozen=True)
class _SummaryFinish:
    agent: SimpleAgent
    previous_summary: str
    operation_evidence: dict[str, object]
    rows: Sequence[MessageLogEntry]
    foreground_rows: Sequence[MessageLogEntry]
    tool_source_text: str
    media_digest: str
    call: _CompactSummaryCall
    fallback_reason: str = "compact provider completed without summary text"


# LLM: 此投影不得从提示正文推导所有者身份或线程权限。
# 函数用途: 从已解析的 agent、thread 和通道字段生成会话作用域。
def conversation_scope(
    agent: SimpleAgent,
    thread: ConversationThread,
    spec: dict,
) -> ConversationScope:
    home = getattr(agent, "home_paths", None)
    return ConversationScope(
        owner_id=str(thread.owner_id or getattr(home, "owner_id", "") or ""),
        owner_home=str(thread.owner_home or getattr(home, "owner_home_dir", "") or ""),
        thread_id=thread.thread_id,
        canonical_user_id=thread.canonical_user_id,
        channel=str(spec.get("channel") or "chat"),
        channel_conversation_id=str(spec.get("channel_conversation_id") or ""),
        channel_user_id=str(spec.get("channel_user_id") or ""),
    )


# LLM: 使用原只读来源或本次加载的来源生成候选；typed取消不记供应商失败，完整材料仅随原checkpoint/CAS成功交回。
# 函数用途: 计量并一次提交会话摘要，保留原游标及失败边界；不替宿主再次准备恢复请求。
def prepare_conversation_context(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread,
    *,
    options: ConversationCompactOptions,
) -> ConversationCompactResult:
    """Load the uncompacted tail and compact it before it crosses the runtime policy."""
    prepared = _prepare_compact_request(agent, store, thread, options)
    if isinstance(prepared, ConversationCompactResult):
        return prepared
    return _execute_compact_request(prepared)


# LLM: 显式scope沿固定尾界先验证身份/锚点，再流式按原宿主范围与同次view去覆盖；可选取消贯穿扫描，不截短来源或混旧游标。
# 函数用途: 只读冻结历史地址及策略，显式范围时不驻留选中正文，不调用摘要或推进检查点。
def load_conversation_compact_source(
    agent: SimpleAgent, store: ConversationStore, thread: ConversationThread, *, exclude_request_id: str = "",
    scope: CompactScope | None = None,
    selector_factory: MessageSelectorFactory | None = None,
    interrupt_check: CompactInterruptCheck | None = None,
) -> ConversationCompactSource:
    from ..agent_core.runtime.context_compactor import runtime_compact_policy

    if scope is None:
        if selector_factory is not None:
            raise ValueError("Compact row selection requires scope")
        pending = _without_current_request_suffix(_uncompacted_conversation_rows(store, thread), exclude_request_id)
        return ConversationCompactSource(thread, tuple(pending), runtime_compact_policy(agent, thread=thread), dict(_recent_operation_evidence(pending) or {}))
    from .compact_summary_view import (
        AppliedCompactContext,
        resolve_compact_summary_view,
    )

    view = resolve_compact_summary_view(agent, thread, scope)
    selected = select_message_snapshot(
        store.messages, thread.thread_id, selector_factory=selector_factory,
        exclude_message_ids=view.source_message_ids, exclude_through_message_ids=view.legacy_message_end_ids,
        interrupt_check=interrupt_check,
    )
    pending = _without_current_request_suffix(selected, exclude_request_id)
    return ConversationCompactSource(
        thread, pending, runtime_compact_policy(agent, thread=thread), dict(_recent_operation_evidence(pending) or {}),
        compact_context=AppliedCompactContext(thread.thread_id, scope, view),
    )


# LLM: 同线程/代次/视图统一校验；显式本次取消覆盖旧回调，未提供则保留来源取消；不拥有CAS或宿主准备权。
# 函数用途: 加载或复用冻结来源，检查作用域并绑定取消回调，不物化正文。
def _prepare_compact_source(agent, store, thread, options):
    source = options.source or load_conversation_compact_source(
        agent, store, thread, exclude_request_id=options.exclude_request_id,
        interrupt_check=options.interrupt_check,
    )
    if isinstance(source.messages, MessageSnapshotRows) and options.interrupt_check is not None:
        source = replace(source, messages=replace(source.messages, interrupt_check=options.interrupt_check))
    if (source.thread.thread_id != thread.thread_id
            or source.thread.compact_generation != thread.compact_generation
            or source.thread.compact_checkpoint_id != thread.compact_checkpoint_id):
        raise ConversationCompactError("Compact source changed", code="COMPACT_SOURCE_CHANGED")
    compact_context = _validated_compact_source_context(agent, thread, source)
    _validate_compact_tool_source(options, compact_context)
    return source, compact_context


# LLM: 原来源/冷却门保持；普通标记须先于来源冻结，显式来源不补造标记；闲置复用已冻结基线并在末写CAS。
# 函数用途: 构造压缩请求，去重不认领来源冻结后的新消息，手动恢复不绕过来源或容量检查。
def _prepare_compact_request(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread,
    options: ConversationCompactOptions,
) -> ConversationCompactResult | _CompactRunRequest:
    expected_message_tail = options.expected_message_tail
    # 只为本次加载的来源捕获更早的观察；显式来源可能已冻结很久，不能把当前消息误记为已覆盖。
    if expected_message_tail is None and options.source is None:
        marker, errors = store.messages.tail_marker_report(thread.thread_id)
        if not errors:
            expected_message_tail = marker
    source, compact_context = _prepare_compact_source(agent, store, thread, options)
    # A gateway retry happens after the current user message was durably appended.
    # It is already represented by ``current_prompt`` and must remain outside the
    # prefix being summarized, exactly like 会话运行时 keeps the active turn input while
    # replacing older history with one compact item.
    pending = source.messages
    policy = source.policy
    current = thread
    base_summary = compact_context.view.summary if compact_context is not None else current.summary
    base_evidence = (compact_context.view.operation_evidence if compact_context is not None
                     else current.compact_operation_evidence)
    attempted_at = time.time()
    initial_projection = project_compact_request(options.request_projector, ConversationCompactView(
        thread.thread_id, thread.compact_generation, base_summary, source.messages,
        base_evidence, source.recent_operation_evidence, policy.trigger_tokens, False,
    ))
    # 压缩前计量与候选同一口径：投影器给的原始值按冻结校准折算；本地估算路径没有对应观测，原样使用。
    projected = (
        calibrated_compact_request_tokens(initial_projection.projected_tokens, options.calibration)
        if initial_projection is not None
        else _projected_context_tokens(
            agent,
            base_summary,
            pending,
            options.current_prompt,
            operation_evidence=base_evidence,
            recent_operation_evidence=_recent_operation_evidence(pending),
        )
    )
    if projected < _compact_request_input_ceiling(agent, policy) and not options.force:
        return ConversationCompactResult(
            thread=current,
            messages=pending,
            projected_tokens=projected,
            trigger_tokens=policy.trigger_tokens,
            compacted=False,
            recent_operation_evidence=_recent_operation_evidence(pending),
        )
    if not pending and (options.force or compact_context is not None):
        # The active Gateway turn is deliberately excluded from durable history
        # until its assistant reply commits.  A second pressure boundary in that
        # same turn can therefore have no additional completed transcript prefix
        # to summarize.  That is a valid no-op, not persistence corruption: the
        # caller may continue from the typed carried tool archive without moving
        # the durable conversation cursor.
        return ConversationCompactResult(
            thread=current,
            messages=(),
            projected_tokens=projected,
            trigger_tokens=policy.trigger_tokens,
            compacted=False,
            recent_operation_evidence={},
        )
    if not pending:
        raise ConversationCompactError(
            "conversation summary alone exceeds the configured compact threshold",
            code="COMPACT_NO_SOURCE_MESSAGES",
        )
    if not options.bypass_circuit_breaker and compact_circuit_is_open(current, policy, now=attempted_at):
        raise ConversationCompactCircuitOpenError(
            "conversation compact is cooling down after repeated failures",
            code="COMPACT_CIRCUIT_OPEN",
        )

    operation_id = f"transcript:{uuid.uuid4().hex}"
    request = _CompactRunRequest(
        agent=agent,
        store=store,
        thread=current,
        current_prompt=options.current_prompt,
        pending=pending,
        policy=policy,
        projected_tokens=projected,
        forced=bool(options.force),
        pressure_forced=bool(options.pressure_forced),
        attempted_at=attempted_at,
        operation_id=operation_id,
        standalone_usage=not str(options.exclude_request_id or "").strip(),
        request_id=(
            str(options.exclude_request_id or "").strip()
            or f"conversation-compact:{operation_id}"
        ),
        run_id=str(
            (
                current.metadata.get("agent_run_id")
                if isinstance(current.metadata, dict)
                else ""
            )
            or current.workspace_task_id
            or ""
        ).strip(),
        task_id=str(current.workspace_task_id or "").strip(),
        custom_instructions=str(options.custom_instructions or "").strip(),
        progress_callback=options.progress_callback,
        interrupt_check=options.interrupt_check,
        model_surface=options.model_surface,
        request_projector=options.request_projector,
        provider_surface=options.provider_surface,
        compact_context=compact_context,
        tool_source=options.tool_source,
        calibration=options.calibration,
        trigger_source=str(options.trigger_source or "").strip(),
        expected_user_activity_generation=options.expected_user_activity_generation,
        expected_message_tail=expected_message_tail,
        idle_compact=options.idle_compact,
        commit_guard=options.commit_guard,
    )
    return request


# LLM: 显式source须与此线程当前scope视图相同；错线程短路仍关闭来源，空/旧摘要不得由thread.summary补造。
# 函数用途: 在摘要/计量之前核对来源行与持久视图，发现错线程、重复行或过期范围就报错。
def _validated_compact_source_context(
    agent: SimpleAgent,
    thread: ConversationThread,
    source: ConversationCompactSource,
) -> AppliedCompactContext | None:
    context = source.compact_context
    if context is None:
        return None
    if context.thread_id != thread.thread_id or not isinstance(context.scope, CompactScope):
        raise ConversationCompactError("Compact source scope mismatches thread", code="COMPACT_SOURCE_CHANGED")
    ids = [row.message_id for row in source.messages if isinstance(row, MessageLogEntry)]
    with message_rows_iterator(source.messages) as rows:
        foreign_source = any(row.thread_id != thread.thread_id for row in rows)
    if (len(ids) != len(source.messages) or foreign_source
            or any(not message_id for message_id in ids) or len(set(ids)) != len(ids)):
        raise ConversationCompactError("Compact source messages are invalid", code="COMPACT_SOURCE_CHANGED")
    from .compact_summary_view import resolve_compact_summary_view

    if context.view != resolve_compact_summary_view(agent, thread, context.scope):
        raise ConversationCompactError("Compact source summary changed", code="COMPACT_SOURCE_CHANGED")
    if any(identity in context.view.source_message_ids for identity in ids):
        raise ConversationCompactError("Compact source repeats covered messages", code="COMPACT_SOURCE_CHANGED")
    return context


# LLM: 双来源候选必须有完整projector和原scope；已覆盖工具不得重复归纳，无来源时保持普通transcript合同。
# 函数用途: 在摘要前验证工具分区属于同一应用视图，不允许降级为只估会话的旧入口。
def _validate_compact_tool_source(options, context):
    if options.tool_source is None:
        return
    from .active_turn_compact import CarriedToolCompactSource
    from .compact_tool_identity import compact_tool_ref_key

    source = options.tool_source
    if not isinstance(source, CarriedToolCompactSource) or options.request_projector is None or context is None:
        raise ConversationCompactError("联合压缩来源或完整投影未知", code="COMPACT_SOURCE_CHANGED")
    coverage = context.view.source_tool_refs
    contains_key = getattr(coverage, 'contains_key', None)
    if callable(contains_key):
        repeated = any(contains_key(compact_tool_ref_key(ref)) for ref in source.source_tool_refs)
    else:
        keys = {compact_tool_ref_key(ref) for ref in coverage}
        repeated = any(compact_tool_ref_key(ref) in keys for ref in source.source_tool_refs)
    if repeated:
        raise ConversationCompactError("联合压缩来源或完整投影未知", code="COMPACT_SOURCE_CHANGED")


# LLM: 进度围绕候选与提交，独立模型用量在成功/失败后均结算；计费失败不覆盖压缩异常或改变终态。
# 函数用途: 执行压缩并上报开始、完成、中断或失败，同时让独立压缩的消耗不依赖后续聊天才能入账。
def _execute_compact_request(request: _CompactRunRequest) -> ConversationCompactResult:
    _emit_compact_progress(request, phase="started", stage="preparing", percent=5)
    try:
        result = _compact_pending(request)
    except (InterruptedError, ToolCancelled, IdleCompactActivityChanged):
        _emit_compact_progress(
            request,
            phase="superseded",
            stage="candidate_discarded",
            percent=0,
        )
        raise
    except Exception as exc:
        _emit_compact_progress(
            request,
            phase="failed",
            stage="failed",
            percent=0,
            failure=exc,
        )
        raise
    finally:
        if request.standalone_usage:
            from .auxiliary_model_call import settle_standalone_model_usage

            settle_standalone_model_usage(request)
    _emit_compact_progress(
        request,
        phase="completed",
        stage="completed",
        percent=100,
        after_tokens=result.projected_tokens,
    )
    return result


# LLM: /context 与诊断只读消费自动压缩合同；终态折叠与已提交的压缩代数、来源配对分别计数。
# 此函数不得生成摘要、写检查点或推进游标。
# 函数用途: 用自动 compact 的同一估算口径查看当前会话占用，并单列当前尾部的回合工具折叠。
def inspect_conversation_context(
    agent: SimpleAgent,
    store: ConversationStore,
    thread: ConversationThread | None,
    *,
    current_prompt: str = "",
) -> ConversationContextUsage:
    from ..agent_core.runtime.context_compactor import runtime_compact_policy

    policy = runtime_compact_policy(agent, thread=thread)
    if thread is None:
        summary = ""
        evidence: dict[str, object] = {}
        pending: list[MessageLogEntry] = []
        generation = 0
        source_messages = 0
        source_tool_pairs = 0
    else:
        summary = thread.summary
        evidence = dict(thread.compact_operation_evidence or {})
        pending = _uncompacted_conversation_rows(store, thread)
        generation = max(0, int(thread.compact_generation or 0))
        source_messages = max(0, int(thread.compact_source_messages or 0))
        source_tool_pairs = max(
            0,
            int(thread.compact_source_tool_pairs or 0),
        )
    terminal_tool_folds = [
        conversation_terminal_tool_fold(
            (row.metadata if isinstance(row.metadata, dict) else {}).get(
                TERMINAL_TOOL_FOLD_METADATA_KEY
            )
        )
        for row in pending
        if row.role == "assistant"
    ]
    terminal_tool_folds = [fold for fold in terminal_tool_folds if fold]
    projected = _projected_context_tokens(
        agent,
        summary,
        pending,
        current_prompt,
        operation_evidence=evidence,
        recent_operation_evidence=_recent_operation_evidence(pending),
    )
    return ConversationContextUsage(
        projected_tokens=projected,
        context_window_tokens=max(0, int(policy.context_window_tokens or 0)),
        trigger_percent=max(0, int(policy.trigger_percent or 0)),
        trigger_tokens=max(0, int(policy.trigger_tokens or 0)),
        compact_generation=generation,
        compact_source_messages=source_messages,
        compact_source_tool_pairs=source_tool_pairs,
        terminal_tool_fold_turns=len(terminal_tool_folds),
        terminal_tool_fold_calls=sum(
            _nonnegative_int(fold.get("tool_call_count"))
            for fold in terminal_tool_folds
        ),
        pending_messages=len(pending),
        has_summary=bool(summary.strip()),
        trigger_capped=policy.trigger_capped,
    )


# LLM: 只渲染结构化用量快照，文案不能成为触发条件或压缩权威；终态折叠必须与真正压缩代数明确区分。
# 供应商分词可能不同，token 数必须明确标为估算。
# 触发线被绝对上限封顶时改说“N tokens 触发（绝对上限封顶）”，免得“90%（30 万）”自相矛盾。
# 函数用途: 把 `/context` 的真实估算、触发线、压缩历史和普通回合工具折叠分栏展示。
def render_conversation_context_usage(
    usage: ConversationContextUsage,
    *,
    model_name: str,
) -> str:
    window = usage.context_window_tokens
    percentage = (usage.projected_tokens / window * 100.0) if window > 0 else 0.0
    filled = min(20, max(0, int(round(min(100.0, percentage) / 5.0))))
    meter = "█" * filled + "░" * (20 - filled)
    lines = [
        f"模型：{model_name or '未知'}",
        (
            f"上下文（估算）：{meter} "
            f"{usage.projected_tokens:,} / {window:,} tokens（{percentage:.1f}%）"
            if window > 0
            else f"上下文（估算）：{usage.projected_tokens:,} tokens；模型窗口未知"
        ),
    ]
    if usage.trigger_tokens > 0:
        remaining = max(0, usage.trigger_tokens - usage.projected_tokens)
        trigger_note = (
            "已达触发线，下一轮开始前会自动压缩"
            if remaining == 0
            else f"距触发线约 {remaining:,} tokens"
        )
        rule = (
            f"{usage.trigger_tokens:,} tokens 触发（绝对上限封顶，比 {usage.trigger_percent}% 更早）"
            if usage.trigger_capped
            else f"{usage.trigger_percent}% （{usage.trigger_tokens:,} tokens）触发"
        )
        lines.append(f"自动 compact：开启，{rule}；{trigger_note}")
    else:
        lines.append("自动 compact：缺少可用的模型窗口，当前无法计算触发线")
    generation = (
        f"已压缩 {usage.compact_generation} 次"
        if usage.compact_generation > 0
        else "尚未压缩"
    )
    lines.append(
        f"历史：{generation}；未压缩消息 {usage.pending_messages} 条；"
        f"已纳入摘要的消息 {usage.compact_source_messages} 条；"
        f"运行中工具 Compact {usage.compact_source_tool_pairs} 对；"
        f"摘要={'有' if usage.has_summary else '无'}"
    )
    lines.append(
        "回合终态折叠："
        f"当前未压缩尾部 {usage.terminal_tool_fold_turns} 个回合、"
        f"{usage.terminal_tool_fold_calls} 次工具调用；"
        "用于跨回合续接与缓存复用，不计入 compact 次数"
    )
    return "\n".join(lines)


# LLM: 原生信封是媒体完整性的权威；首个"不能进入压缩链"的回合起保护全部后缀，短路时关闭读取，禁止摘要越过该游标。
# 媒体策略为 off 时任何非文本块都保护（旧行为）；否则只有 unknown 非文本块保护，已知媒体块交给策略投影或随图摘要。
# 函数用途: 在可重放地址上划分可摘要前缀和保护后缀，不复制全量行。
def _split_nontext_transcript_suffix(
    rows: Sequence[MessageLogEntry],
    *,
    media_policy: str = MEDIA_POLICY_OFF,
) -> tuple[Sequence[MessageLogEntry], Sequence[MessageLogEntry]]:
    with message_rows_iterator(rows) as iterator:
        for index, row in enumerate(iterator):
            native = canonical_native_messages_from_metadata(row.metadata)
            if native and not compact_source_supported(native, media_policy=media_policy):
                start = _nontext_turn_start(rows, index)
                return rows[:start], rows[start:]
    return rows, []


# LLM: 边界优先采用结构化身份；首次匹配后关闭重放，旧行退到最近用户行，不以正文猜配对。
# 函数用途: 在同一只读来源定位媒体所属完整问答，保留用户输入与工具往返。
def _nontext_turn_start(rows: Sequence[MessageLogEntry], index: int) -> int:
    identity = _row_turn_identity(rows[index])
    with message_rows_iterator(rows[: index + 1]) as iterator:
        start = next((position for position, row in enumerate(iterator) if identity and _row_turn_identity(row) == identity), index)
    if rows[start].role == "user":
        return start
    return next(
        (position for position in range(start, -1, -1) if rows[position].role == "user"),
        0,
    )


# LLM: 原生媒体或未知非文本块所在逻辑turn起的完整后缀必须保留；仅安全前缀进入原forced/auto分区与checkpoint覆盖。
# 函数用途: 以地址分区尝试候选，立即释放超量候选；保留候选与自己的请求一起采用。
def _compact_pending(request: _CompactRunRequest) -> ConversationCompactResult:
    # LLM: 供应商施压后的重试必须一次替换全部已完成前缀；反复保护再压同一尾部只会产生检查点抖动，不能帮活动回合装下请求。
    # 普通阈值压缩仍保护有界的完整回合。
    # 逻辑说明: 平时到 90% 时保留近期完整问答；供应商已报压力时一次压完旧段，避免同一尾部连压多代。
    # 随图摘要只对"窗口上限/供应商施压"的恢复关闭；options.force 本身（一次压完前缀）不构成禁用理由。
    media_decision = resolve_compact_media_policy(request.agent, forced=request.pressure_forced, thread=request.thread)
    safe_prefix, protected_suffix = _split_nontext_transcript_suffix(request.pending, media_policy=media_decision.policy)
    if not safe_prefix:
        raise ConversationCompactError("没有可完整文字摘要的历史前缀，保留原始媒体", code="COMPACT_SOURCE_EMPTY")
    partitions = (
        ((safe_prefix, []),)
        if request.forced
        else compact_partitions(
            safe_prefix,
            max_turns=request.policy.recent_tail_max_turns,
            max_tail_tokens=request.policy.recent_tail_tokens,
        )
    )
    partition_count = max(1, len(partitions))
    trigger_fallback: _CompactCandidate | None = None
    rejected = _RejectedCandidates(request)
    provider_surface = _resolved_provider_surface(request)
    for partition_index, (compact_rows, ordinary_tail) in enumerate(partitions):
        retained_tail = concatenate_message_rows((ordinary_tail, protected_suffix))
        raise_if_compact_interrupted(request.interrupt_check)
        summarize_percent = 15 + int(partition_index * 50 / partition_count)
        measure_percent = 15 + int((partition_index + 0.75) * 50 / partition_count)
        _emit_compact_progress(
            request,
            phase="progress",
            stage="summarizing",
            percent=summarize_percent,
        )
        try:
            candidate = _build_compact_candidate(
                request,
                compact_rows,
                retained_tail,
                provider_surface=provider_surface,
                progress_range=(summarize_percent, measure_percent),
                media_decision=media_decision,
            )
        except (InterruptedError, ToolCancelled):
            raise
        except Exception as exc:
            raise_if_compact_interrupted(request.interrupt_check)
            if trigger_fallback is not None:
                return _commit_compact_candidate_or_record_failure(
                    request,
                    trigger_fallback,
                )
            code = compact_exception_code(exc)
            if code == COMPACT_VISION_SUMMARY_FAILED:
                # 随图摘要的 typed 失败只写线程代次标记，不进熔断；同代次下一次压缩自动改走归档引用。
                record_compact_vision_failure(request.store, request.thread, now=request.attempted_at)
            else:
                _record_request_compact_failure(request, code=code)
            raise
        _emit_compact_progress(
            request,
            phase="progress",
            stage="measuring",
            percent=measure_percent,
            after_tokens=candidate.projected_tokens_after,
        )
        if rejected.reject_if_over(candidate, _compact_request_input_ceiling(request.agent, request.policy)):
            candidate = None
            continue
        if candidate.projected_tokens_after <= request.policy.recovery_target_tokens:
            return _commit_compact_candidate_or_record_failure(request, candidate)
        if (
            trigger_fallback is None
            or candidate.projected_tokens_after
            < trigger_fallback.projected_tokens_after
        ):
            trigger_fallback = candidate

    if trigger_fallback is not None:
        return _commit_compact_candidate_or_record_failure(request, trigger_fallback)

    error = ConversationCompactError(
        "conversation compact candidate did not fit the input trigger and known output reserve",
        code="COMPACT_CANDIDATE_TOO_LARGE", capacity=rejected.capacity(),
    )
    _record_request_compact_failure(request, code=error.code)
    raise error


# LLM: 闲置失败与成功提交共用车道闸→消息锁→线程锁；闸内复核用户在场、取消与commit_guard，再传冻结基线到原存储CAS。
#   普通压缩只用nullcontext，不取车道闸或增加新CAS；联测闲置用户先赢/失败先赢及容量熔断回归。
# 函数用途: 把失败的最终复核与记账放进原短车道闸，防止迟到或恢复中的用户登记插入失败计数。
def _record_request_compact_failure(request: _CompactRunRequest, *, code: str) -> None:
    raise_if_compact_interrupted(request.interrupt_check)
    from contextlib import nullcontext

    from .run_claim import user_input_turn_gate

    expected = {}
    guard = nullcontext(False)
    if request.idle_compact:
        if request.expected_user_activity_generation is None or request.expected_message_tail is None:
            raise IdleCompactActivityChanged("idle failure recording requires activity and canonical message baseline")
        expected = {"expected_user_activity_generation": request.expected_user_activity_generation,
                    "expected_message_tail": request.expected_message_tail}
        guard = user_input_turn_gate(request.store, request.thread.thread_id)
    with guard as user_present:
        raise_if_compact_interrupted(request.interrupt_check)
        if user_present or request.commit_guard is not None and request.commit_guard():
            raise IdleCompactActivityChanged("conversation user activity changed before idle failure recording")
        record_compact_failure(request.store, request.thread, code=code, now=request.attempted_at, **expected)


# LLM: 固定开销是“空摘要、无任何保留”那一版完整下一请求的实测值：有宿主投影器时走它的“只计量、不提交”入口
#   （measure_only 视图，宿主不会据此提交），没有时走同一本地估算，不用两个估算相减。只在候选真被拒、要抛
#   COMPACT_CANDIDATE_TOO_LARGE 时算一次；测不出返回 None（字段缺失，不写 0），不盖住原失败，用户中断继续上抛。
#   宿主口径下原生 IR 一条不留，当前轮自己的原生 IR 计在保留 IR 里；本地估算路径没有 IR，当前 prompt 计入固定开销。
# 函数用途: 再计量一次与保留内容无关的固定请求开销（system、tools、runtime facts 等）。
def _fixed_request_tokens(request: _CompactRunRequest) -> int | None:
    try:
        projection = project_compact_request(request.request_projector, ConversationCompactView(
            request.thread.thread_id, request.thread.compact_generation + 1, "", (), {}, {},
            request.policy.trigger_tokens, True, retained_tool_records=(), retained_ir_history=(), measure_only=True,
        ))
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        return None
    if projection is not None:
        return calibrated_compact_request_tokens(projection.projected_tokens, request.calibration)
    return _projected_context_tokens(request.agent, "", (), request.current_prompt)


# LLM: 只在候选循环里累计被输入上限拒掉的候选，立即取出计量数字、不持有候选对象（其中的完整请求材料要随候选释放）；
#   结果只进 COMPACT_CANDIDATE_TOO_LARGE 的容量诊断，不参与候选选择、回退或提交。request 只供失败路径上的固定开销与
#   保留 IR 计量，不参与接受门；固定开销只在 capacity() 里（即真要抛出失败时）实测一次，成功路径不多投影。
# 类用途: 记住被上限拒掉的候选里最小那个的大小、摘要占比、保留条数（工具/会话与原生 IR 分开），以及一共拒了几个。
@dataclass
class _RejectedCandidates:
    request: _CompactRunRequest = field(repr=False)
    smallest: CompactCapacityFacts | None = None
    count: int = 0

    # LLM: 接受门与候选循环原判据一致：完整下一请求 ≥ 输入上限即拒绝；候选计量已在 _measure_candidate 按宿主冻结的
    #   校准折算（无校准即原始投影）。只读候选的计量与摘要文本；摘要 token 用与会话估算同一 estimate_tokens 口径，
    #   是估算值。返回值决定候选是否被丢弃，改判据要同步 active_turn 的同一接受门。
    # 函数用途: 判断候选是否超过输入上限；超过就记下来（只保留完整下一请求最小的那个的计量）并返回 True。
    def reject_if_over(self, candidate: _CompactCandidate, ceiling: int) -> bool:
        if candidate.projected_tokens_after < ceiling:
            return False
        self.count += 1
        if self.smallest is not None and self.smallest.candidate_tokens <= candidate.projected_tokens_after:
            return True
        self.smallest = self._capacity(candidate, ceiling)
        return True

    # LLM: 保留 IR 取本候选实际要发送的材料（宿主投影交回的 retained_ir_history）；投影器没给出时按同一口径
    #   （compact_tool_summary.sent_retained_ir）从工具来源保留区推出，都没有记 0。固定开销此处不算，留给 capacity()。
    #   只读，不写状态、不改变接受门或熔断记账。
    # 函数用途: 组装一个被拒候选的容量计量（候选、上限、摘要、保留条数与实际发送的保留 IR）。
    def _capacity(self, candidate: _CompactCandidate, ceiling: int) -> CompactCapacityFacts:
        from .compact_tool_summary import retained_ir_facts, sent_retained_ir

        projection = candidate.request_projection
        sent = projection.retained_ir_history if projection is not None else None
        if sent is None:
            source = self.request.tool_source
            sent = sent_retained_ir(None if source is None else source.retained_ir_history)
        ir_items, ir_tokens = retained_ir_facts(sent)
        return CompactCapacityFacts(
            candidate_tokens=candidate.projected_tokens_after,
            input_ceiling_tokens=ceiling,
            summary_tokens=estimate_tokens(candidate.summary),
            fixed_tokens=None,
            retained_items=len(candidate.retained_tail),
            retained_ir_items=ir_items,
            retained_ir_tokens=ir_tokens,
            candidates_tried=0,
        )

    # LLM: 只在要抛 COMPACT_CANDIDATE_TOO_LARGE 时调用；candidates_tried 取被拒总数，固定开销在这里实测一次（测不出为
    #   None）。返回值只进该失败的 capacity。
    # 函数用途: 生成失败要带的容量计量；一个候选都没被拒过时返回 None。
    def capacity(self) -> CompactCapacityFacts | None:
        if self.smallest is None:
            return None
        return replace(self.smallest, candidates_tried=self.count, fixed_tokens=_fixed_request_tokens(self.request))


# LLM: 宿主已备surface原样沿用；新建面绑定已提交人格/加载名，前缀下界必超原预算时不保留旧正文，完整精确门仍执行。
# 函数用途: 为本次Compact冻结同线程缓存面及可容纳前缀，准备错误沿原记账，不借容量优化吞掉身份/取消错误。
def _resolved_provider_surface(request: _CompactRunRequest) -> ConversationCompactProviderSurface | None:
    provider_surface = request.provider_surface
    if provider_surface is not None or request.model_surface is None:
        return provider_surface
    try:
        surface = replace(request.model_surface, thread_id=request.thread.thread_id,
                          loaded_tool_names=_bound_loaded_tools(request.model_surface, request.thread))
        from ..agent_core.compact_transcript_prefix import (
            capture_preflight_prefix,
            declared_transcript_prefix_media_decision,
            transcript_prefix_exceeds_capacity,
        )

        provider = prepare_conversation_compact_provider_surface(request.agent, surface, run_id=request.run_id)
        prefix = capture_preflight_prefix(request, surface)
        media_decision = declared_transcript_prefix_media_decision(request.agent, prefix)
        if media_decision is not None:
            return replace(provider, media_decision=media_decision)
        if transcript_prefix_exceeds_capacity(request.agent, provider, prefix, request.calibration):
            return provider
        return replace(provider, transcript_prefix=prefix)
    except Exception as exc:
        _record_request_compact_failure(request, code=compact_exception_code(exc))
        raise


# LLM: 旧调用可不带字段；其它线程面不能带入工具，原线程已有顺序优先，同线程新名只在末尾去重追加。
# 函数用途: 保序合并原线程与同身份本轮工具发现事实，不排序或扩大授权。
def _bound_loaded_tools(surface, thread):
    same_thread = not surface.thread_id or surface.thread_id == thread.thread_id
    carried = (surface.loaded_tool_names or ()) if same_thread else ()
    names = (*(getattr(thread, "loaded_tool_names", ()) or ()), *carried)
    return tuple(dict.fromkeys(str(item).strip() for item in names if str(item).strip()))


# LLM: 只组合原 Compact 输入阈值与 core 唯一请求容量规则，不另配百分比、不把未来输出当已消耗 token。
# 函数用途: 对普通触发、优选候选及保留候选使用同一接受上界；等于上界时仍交原失败/恢复链。
def _compact_request_input_ceiling(agent: SimpleAgent, policy: RuntimeCompactPolicy) -> int:
    from ..agent_core.model.context_pressure import model_request_input_ceiling

    return min(policy.trigger_tokens, model_request_input_ceiling(agent, policy.context_window_tokens))


# LLM: 已接受候选与回退候选共用检查点/CAS失败账；取消和闲置竞争不计失败，其余失败沿原记账入口。
# 函数用途: 提交一个已验证的摘要候选，并把真实 checkpoint/CAS 异常记入同一会话熔断账。
def _commit_compact_candidate_or_record_failure(
    request: _CompactRunRequest,
    candidate: _CompactCandidate,
) -> ConversationCompactResult:
    try:
        return _commit_compact_candidate(request, candidate)
    except (InterruptedError, ToolCancelled, IdleCompactActivityChanged):
        raise
    except Exception as exc:
        _record_request_compact_failure(request, code=compact_exception_code(exc))
        raise


# LLM: 全来源的声明拒绝只作原媒体决定载体；候选须有canonical媒体事实且其实际类型仍被原声明拒绝，off不改。
# 函数用途: 在视觉探针之前按本候选范围复核早先决定，避免把未覆盖尾部的媒体拒绝套到较小候选。
def _candidate_prefix_media_decision(agent, surface, rows, decision, facts):
    if (surface is None or surface.media_decision is None or decision is None
            or decision.policy == MEDIA_POLICY_OFF or not facts.blocks):
        return decision
    from .compact_media_policy import declared_prefix_media_unsupported

    with closing(iter_provider_history_messages_from_rows(rows)) as messages:
        if declared_prefix_media_unsupported(agent, messages):
            return surface.media_decision
    return decision


# LLM: 候选继承同scope摘要/工具分区，媒体决定在探针前按本候选canonical范围复核；双来源和完整计量不提前提交。
# 函数用途: 流式构造同源候选，保留媒体归档原因且不持已拒用前缀，范围与提交仍由原接受门裁决。
def _build_compact_candidate(
    request: _CompactRunRequest,
    compact_rows: Sequence[MessageLogEntry],
    retained_tail: Sequence[MessageLogEntry],
    *,
    provider_surface: ConversationCompactProviderSurface | None,
    progress_range: tuple[int, int] = (15, 65),
    media_decision: CompactMediaDecision | None = None,
) -> _CompactCandidate:
    raise_if_compact_interrupted(request.interrupt_check)
    # 媒体事实只从 canonical 信封统计；策略为 off 或没有决定时不统计、也不写 checkpoint 字段。
    media_facts = (media_archive_facts(compact_rows)
                   if media_decision is not None and media_decision.policy != MEDIA_POLICY_OFF else MediaArchiveFacts())
    media_decision = _candidate_prefix_media_decision(request.agent, provider_surface, compact_rows, media_decision, media_facts)
    # 已证明主前缀由同一后端直接读取媒体，不为其另发视觉探针；归档决策仅用于结果。
    if (media_decision is not None and media_facts.blocks > 0
            and not (provider_surface is not None and provider_surface.transcript_prefix is not None)):
        media_decision = _resolve_summary_vision_candidate(
            request.agent, media_decision, request.request_id, request.run_id,
        )
    base_summary = (request.compact_context.view.summary if request.compact_context is not None
                    else request.thread.summary)
    base_evidence = (request.compact_context.view.operation_evidence if request.compact_context is not None
                     else request.thread.compact_operation_evidence)
    evidence = _merge_compact_operation_evidence(base_evidence, compact_rows)
    media_outcome: list[CompactMediaDecision] = []
    landmark_outcome: list[_LandmarkRebuild] = []
    provider_outcome: list[dict[str, object]] = []
    from .compact_remote import provider_compaction_content

    previous_provider_compaction = (provider_compaction_content(request.compact_context.view.provider_compaction)
                                    if request.compact_context is not None else "")
    summary = _summarize(
        request.agent,
        base_summary,
        evidence,
        compact_rows,
        call=_CompactSummaryCall(
            thread_id=request.thread.thread_id,
            custom_instructions=request.custom_instructions,
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            compact_generation=request.thread.compact_generation,
            provider_surface=provider_surface,
            interrupt_check=request.interrupt_check,
            tool_source_records=request.tool_source.source_records if request.tool_source is not None else (),
            tool_source_ir_history=request.tool_source.source_ir_history if request.tool_source is not None else (),
            preserve_complete_fallback=request.request_projector is not None,
            media_policy=media_decision.policy if media_decision is not None else MEDIA_POLICY_OFF,
            media_archived=media_facts.blocks > 0,
            media_decision=media_decision,
            media_facts=media_facts,
            media_outcome=media_outcome,
            landmark_outcome=landmark_outcome,
            provider_outcome=provider_outcome,
            previous_provider_compaction=previous_provider_compaction,
            calibration=request.calibration,
            source_progress=lambda covered, total: _emit_compact_progress(
                request, phase="progress", stage="summarizing",
                percent=progress_range[0] + int((progress_range[1] - progress_range[0]) * covered / max(1, total)),
            ),
        ),
    )
    final_media = media_outcome[-1] if media_outcome else media_decision
    if (final_media is not None and final_media.policy == MEDIA_POLICY_ARCHIVED_REFS
            and final_media.reason == COMPACT_VISION_SUMMARY_FAILED):
        # 看图小请求一次都没成功：写同代次标记；若本次提交失败，同代次重试直接选归档引用，不再发看图请求。
        record_compact_vision_failure(request.store, request.thread, now=request.attempted_at)
    remeasure = partial(_measure_candidate, request, _CandidateTail(
        retained_tail, evidence, dict(provider_outcome[-1]) if provider_outcome else {},
    ))
    measured = _fit_landmarks_to_target(_candidate_limits(request), remeasure(summary), landmark_outcome, remeasure)
    return _CompactCandidate(
        summary=measured.summary,
        operation_evidence=evidence,
        compact_rows=compact_rows,
        retained_tail=retained_tail,
        projected_tokens_after=measured.tokens,
        request_projection=measured.projection,
        tool_source=request.tool_source,
        # 摘要阶段的准入或看图小请求结果可能改写 B 候选；checkpoint 记的是实际采用的决定（含 summarized_blocks）。
        media_decision=final_media,
        media_facts=media_facts,
        provider_compaction=provider_outcome[-1] if provider_outcome else None,
    )


# LLM: projection 与 summary 必须来自同一次计量；提交时用这里的 projection 作为下一请求材料，不能换摘要不换材料。
# 类用途: 一个摘要文本及其完整下一请求的计量（投影材料与 token 数）。
@dataclass(frozen=True)
class _MeasuredSummary:
    summary: str
    projection: ConversationCompactProjection | None
    tokens: int


# LLM: 同一候选的保留尾部与合并后的操作证据，计量时原样交给投影器，不在计量之间改动。
# 类用途: 打包候选计量需要的保留尾部与操作证据。
@dataclass(frozen=True)
class _CandidateTail:
    retained_tail: Sequence[MessageLogEntry]
    evidence: dict[str, object]
    # 本候选的服务端压缩记录；候选视图按它投影请求（compact_projection.ConversationCompactView.provider_compaction）。
    provider_compaction: dict[str, object] = field(default_factory=dict)


# LLM: target 是压缩后的健康目标，ceiling 是会被拒的输入上限（与候选循环同一 _compact_request_input_ceiling）。
# 类用途: 一次候选判定用到的两条 token 线。
@dataclass(frozen=True)
class _CandidateLimits:
    target: int
    ceiling: int


# LLM: 只读 policy 与 agent 配置，与候选循环共用 _compact_request_input_ceiling，不另设阈值。
# 函数用途: 取本次压缩的健康目标与输入上限，供原话备份收缩判断使用。
def _candidate_limits(request: _CompactRunRequest) -> _CandidateLimits:
    return _CandidateLimits(request.policy.recovery_target_tokens, _compact_request_input_ceiling(request.agent, request.policy))


# LLM: 与提交前的容量门同一口径：有宿主投影器时用完整下一请求计量并按冻结校准折算（与预检同一观测），否则用本地
#   会话估算（不套校准）；计量返回后立即复核取消，避免过大候选在活动变化后误记失败；只读，不写状态。
#   projection 保留原始纯投影，供提交后改写本轮观测基线；联测闲置竞争和候选容量门。
# 函数用途: 计量“某个摘要文本 + 保留尾部”组成的下一请求有多大。
def _measure_candidate(request: _CompactRunRequest, tail: _CandidateTail, summary: str) -> _MeasuredSummary:
    raise_if_compact_interrupted(request.interrupt_check)
    retained_tail, evidence = tail.retained_tail, tail.evidence
    projection = project_compact_request(request.request_projector, ConversationCompactView(
        request.thread.thread_id, request.thread.compact_generation + 1, summary, retained_tail,
        evidence, dict(_recent_operation_evidence(retained_tail) or {}), request.policy.trigger_tokens, True,
        retained_tool_records=request.tool_source.retained_records if request.tool_source is not None else None,
        retained_ir_history=request.tool_source.retained_ir_history if request.tool_source is not None else None,
        provider_compaction=dict(tail.provider_compaction),
    ))
    tokens = (
        calibrated_compact_request_tokens(projection.projected_tokens, request.calibration)
        if projection is not None
        else _projected_context_tokens(
            request.agent, summary, retained_tail, request.current_prompt, operation_evidence=evidence,
            recent_operation_evidence=_recent_operation_evidence(retained_tail),
        )
    )
    raise_if_compact_interrupted(request.interrupt_check)
    return _MeasuredSummary(summary, projection, tokens)


# LLM: 原话备份是非权威补充：候选超目标时按超出量缩小备份（最小只剩标题、编号与
#   回查说明）并重新计量一次，变小才采用。只有备份已是最小段才不重算；局部备份token不能证明完整投影不可行。
#   小窗口里整段备份可能把候选顶在上限边缘，压缩刚提交下一轮又要压缩；收缩换回余量，也免去候选被拒后再换切分、
#   多一次摘要请求。保同projector/冻结校准与原完整接受门、取消/CAS；语义摘要与机械trailing不动，不发模型请求。
# 函数用途: 候选超出恢复目标时收缩原话备份，避免备份把压缩结果挤出目标或容量线。
def _fit_landmarks_to_target(
    limits: _CandidateLimits,
    measured: _MeasuredSummary,
    rebuilds: list[_LandmarkRebuild],
    remeasure: Callable[[str], _MeasuredSummary],
) -> _MeasuredSummary:
    rebuild = rebuilds[-1] if rebuilds else None
    if rebuild is None or measured.tokens <= limits.target:
        return measured
    minimum = rebuild.minimum_tokens()
    if rebuild.used_tokens <= minimum:
        return measured
    smaller = remeasure(rebuild.render(rebuild.used_tokens - (measured.tokens - limits.target)))
    return smaller if smaller.tokens < measured.tokens else measured


# LLM: checkpoint/generation仍唯一CAS；闲置候选在用户登记短门内复核活动；显式传writer同根到获胜后冷转，规范消息指纹仍在原锁内核对。
# 函数用途: 复验覆盖及保留来源后沿原CAS提交同一候选，局部范围不变，全线程可无损冷转已覆盖原文。
def _commit_compact_candidate(
    request: _CompactRunRequest,
    candidate: _CompactCandidate,
) -> ConversationCompactResult:
    raise_if_compact_interrupted(request.interrupt_check)
    for rows in (candidate.compact_rows, candidate.retained_tail):
        if isinstance(rows, MessageSnapshotRows):
            rows.validate()
    last_row = candidate.compact_rows[-1]
    byte_offset = request.store.messages.byte_offset_after(
        request.thread.thread_id,
        last_row.message_id,
    )
    _emit_compact_progress(
        request,
        phase="progress",
        stage="checkpointing",
        percent=78,
        after_tokens=candidate.projected_tokens_after,
    )
    checkpoint_id = write_compact_checkpoint(
        request.agent,
        CompactCheckpointRequest(
            thread=request.thread,
            summary=candidate.summary,
            operation_evidence=candidate.operation_evidence,
            compact_rows=candidate.compact_rows,
            retained_tail=candidate.retained_tail,
            source_end_byte_offset=byte_offset,
            projected_tokens_before=request.projected_tokens,
            projected_tokens_after=candidate.projected_tokens_after,
            policy=request.policy,
            forced=request.forced,
            source_tool_refs=candidate.tool_source.source_tool_refs if candidate.tool_source is not None else (),
            retained_tool_refs=candidate.tool_source.retained_tool_refs if candidate.tool_source is not None else (),
            media_policy=candidate.media_decision.policy if candidate.media_decision is not None else "",
            media_fact_source=candidate.media_decision.fact_source if candidate.media_decision is not None else "",
            media_policy_reason=candidate.media_decision.reason if candidate.media_decision is not None else "",
            media_blocks_archived=_media_blocks_archived(candidate),
            media_blocks_summarized=_media_blocks_summarized(candidate),
            media_refs=candidate.media_facts.refs,
            provider_compaction=candidate.provider_compaction,
            **({
                "scope": request.compact_context.scope,
                "summary_base_checkpoint_id": request.compact_context.view.checkpoint_id,
            } if request.compact_context is not None else {}),
        ),
    )
    raise_if_compact_interrupted(request.interrupt_check)
    _emit_compact_progress(request, phase="progress", stage="committing", percent=92, after_tokens=candidate.projected_tokens_after)
    raise_if_compact_interrupted(request.interrupt_check)
    for rows in (candidate.compact_rows, candidate.retained_tail):
        if isinstance(rows, MessageSnapshotRows):
            rows.validate()
    # 原CAS可能切换热inode；返回尾部须在切换前物化，不能在已提交后重放旧物理地址。
    retained_tail = tuple(candidate.retained_tail)
    from . import tool_discovery
    selector = tool_discovery.compact_tool_selector(request.thread, request.pending)
    from contextlib import nullcontext

    from .run_claim import user_input_turn_gate

    guard = user_input_turn_gate(request.store, request.thread.thread_id) if request.idle_compact else nullcontext(False)
    with guard as user_present:
        if user_present or request.commit_guard is not None and request.commit_guard():
            raise IdleCompactActivityChanged("conversation user activity changed before idle commit")
        updated = request.store.threads.update_compact_state(
            request.thread.thread_id,
            commit=ConversationCompactCommit(
                summary=candidate.summary,
                operation_evidence=candidate.operation_evidence,
                checkpoint_id=checkpoint_id,
                compacted_through_message_id=last_row.message_id,
                compacted_through_byte_offset=byte_offset,
                source_messages=(
                    request.thread.compact_source_messages
                    + len(candidate.compact_rows)
                ),
                source_tool_pairs=request.thread.compact_source_tool_pairs + (
                    len(candidate.tool_source.source_tool_refs) if candidate.tool_source is not None else 0
                ),
                publish_thread_view=(request.compact_context is None
                                     or request.compact_context.scope.kind == "thread"),
            ),
            expected_generation=request.thread.compact_generation,
            compact_root=_compact_root(request.agent, request.thread.thread_id),
            active_loaded_tool_names=selector(request.thread),
            active_tool_selector=selector,
            baseline_loaded_tool_names=tuple(request.thread.loaded_tool_names),
            **({"expected_message_tail": request.expected_message_tail} if request.expected_message_tail is not None else {}),
            **({"expected_user_activity_generation": request.expected_user_activity_generation,
                "idle_compact": True}
               if request.idle_compact else {}),
        )
    return ConversationCompactResult(
        thread=updated,
        messages=retained_tail,
        projected_tokens=candidate.projected_tokens_after,
        trigger_tokens=request.policy.trigger_tokens,
        compacted=True,
        request_projection=candidate.request_projection,
        recent_operation_evidence=_recent_operation_evidence(
            retained_tail
        ),
    )


# LLM: 进度回调仅作展示，普通错误不改提交；显式取消透传，失败只展示结构化错误码及冻结窗口。宿主给出的
#   trigger_source 随每个事件带出（started 事件即可看到触发来源），空串不带；字段值由 compact_progress 白名单核验。
# 函数用途: 将真实Compact阶段发送给TUI，断连可忽略，但不能吞掉用户停止信号。
def _emit_compact_progress(
    request: _CompactRunRequest,
    *,
    phase: str,
    stage: str,
    percent: int,
    after_tokens: int = 0,
    failure: BaseException | None = None,
) -> None:
    callback = request.progress_callback
    if callback is None:
        return
    payload: dict[str, object] = {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        "phase": str(phase),
        "stage": str(stage),
        "percent": min(100, max(0, int(percent or 0))),
        "generation": max(0, int(request.thread.compact_generation or 0)) + 1,
        "operation_id": request.operation_id,
        "before_tokens": max(0, int(request.projected_tokens or 0)),
        "after_tokens": max(0, int(after_tokens or 0)),
        "trigger_tokens": max(0, int(request.policy.trigger_tokens or 0)),
        "context_window_tokens": max(0, int(request.policy.context_window_tokens or 0)),
        "source_messages": len(request.pending),
        "source_kind": COMPACT_SOURCE_TRANSCRIPT,
        "commit_authority": COMPACT_AUTHORITY_CONVERSATION,
        "error_code": "",
    }
    if request.trigger_source:
        payload["trigger_source"] = request.trigger_source
    if failure is not None:
        payload.update(compact_failure_progress_fields(failure))
    try:
        callback(payload)
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        return


# LLM: 游标必须精确匹配原始消息身份，缺少该权威时失败关闭。
# 函数用途: 兼容没有字节游标的旧 thread，从精确消息游标后读取尾部。
def _messages_after_cursor(
    rows: list[MessageLogEntry],
    cursor: str,
) -> list[MessageLogEntry]:
    if not cursor:
        return rows
    for index, row in enumerate(rows):
        if row.message_id == cursor:
            return rows[index + 1 :]
    raise RuntimeError("conversation compact cursor is missing from the authoritative transcript")


# LLM: 自动预检、手动压缩与 /context 必须读取同一未压缩尾部；字节偏移权威优先，旧消息游标只保留为显式迁移路径。
# 函数用途: 从唯一 transcript 读取当前还没进摘要的原始消息段。
def _uncompacted_conversation_rows(
    store: ConversationStore,
    thread: ConversationThread,
) -> list[MessageLogEntry]:
    rows, errors = store.messages.after_compact_report(thread)
    if errors:
        raise OSError("conversation transcript could not be read reliably")
    if thread.compacted_through_byte_offset > 0:
        return rows
    return _messages_after_cursor(rows, thread.compacted_through_message_id)


# LLM: 所有入口均写规范 conversation_request_id；只排除尚无持久助手检查点的尾部用户输入。
# 同一请求一旦已有助手行，会话运行时的回合内压缩就必须摘要该已完成前缀，不得隐藏整条请求而反复报 context_overflow。
# 函数用途: 按结构化身份切去尚未结束的用户后缀，只切地址；已落盘阶段回复仍可进入Compact。
def _without_current_request_suffix(
    rows: Sequence[MessageLogEntry],
    request_id: str,
) -> Sequence[MessageLogEntry]:
    """Exclude only uncommitted trailing user inputs from compact input."""
    expected = str(request_id or "").strip()
    if not expected:
        return rows
    end = len(rows)
    while end > 0:
        row = rows[end - 1]
        if str(row.role or "").strip().lower() != "user":
            break
        metadata = row.metadata
        current = (
            str(
                metadata.get("conversation_request_id")
                or metadata.get("gateway_request_id")
                or ""
            )
            if isinstance(metadata, dict)
            else ""
        )
        if current != expected:
            break
        end -= 1
    return rows[:end]


# LLM: 当前投影取文字与原生会话形状的较大估算，保留工具账；未含完整宿主 memories/inject/schema，不能当换模或完整恢复容量证明。
# 函数用途: 流式编码完整会话历史并复用原估算公式；不把此局部估算当完整恢复容量证明。
def _projected_context_tokens(
    agent: SimpleAgent,
    summary: str,
    rows: Sequence[MessageLogEntry],
    current_prompt: str,
    *,
    operation_evidence: dict[str, object] | None = None,
    recent_operation_evidence: dict[str, object] | None = None,
) -> int:
    try:
        base = agent.prompts.build(current_prompt, [], inject=[])
    except Exception:
        base = current_prompt
    foreground_rows = filtered_message_rows(rows, lambda row: not is_audit_background_transcript_entry(row))
    common = {
        "base_prompt": base,
        "conversation_summary": summary,
        "conversation_operation_evidence": operation_evidence or {},
        "conversation_recent_operation_evidence": recent_operation_evidence or {},
    }
    legacy_tokens = estimate_compact_payload(
        {
            **common,
            "conversation_messages": CompactMessageSource(lambda: (
                {
                    "role": row.role,
                    "content": conversation_message_with_terminal_tool_fold(
                        row.content,
                        row.metadata,
                    )
                    if row.role == "assistant"
                    else row.content,
                }
                for row in foreground_rows
            )),
        }
    )
    native_tokens = estimate_compact_payload(
        {
            **common,
            "conversation_native_messages": CompactMessageSource(lambda: iter_provider_history_messages_from_rows(foreground_rows)),
        }
    )
    return max(legacy_tokens, native_tokens)


# LLM: 摘要为软上下文，操作证据另守权威；主前缀可容纳时原样发送，容量不足或声明不支持时复用原来源。
# 严格恢复空文本/工具调用回退保留旧摘要与全部消息/工具来源，最终容量门裁决；网络异常不提交、不执行工具。
# 普通单次摘要不新增截断拒绝；失败形状沿共用分类记录，日志不是新的恢复或 checkpoint 事实。
# 函数用途: 优先按冻结主前缀摘要；容量不足解除优化复用原分段恢复，完整来源与 checkpoint 不变。
def _summarize(
    agent: SimpleAgent,
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: Sequence[MessageLogEntry],
    *,
    call: _CompactSummaryCall | None = None,
) -> str:
    try:
        return _summarize_once(agent, previous_summary, operation_evidence, rows, call=call)
    except ConversationCompactError as error:
        fallback = _nonprefix_capacity_call(agent, call or _CompactSummaryCall(), error)
        return _summarize_once(agent, previous_summary, operation_evidence, rows, call=fallback)


# LLM: 仅 PREFIX_TOO_LARGE 可解除一次前缀优化；其它错误原样抛出，旧来源/媒体策略/CAS 仍由原链负责。
# 函数用途: 小窗口不能容纳整前缀时复用原分段和媒体摘要能力，不要求用户切换模型。
def _nonprefix_capacity_call(agent, call, error):
    surface = call.provider_surface
    if error.code != "COMPACT_PREFIX_TOO_LARGE" or surface is None or surface.transcript_prefix is None:
        raise error
    raise_if_compact_interrupted(call.interrupt_check)
    decision = call.media_decision
    if decision is not None and call.media_facts.blocks > 0:
        decision = _resolve_summary_vision_candidate(agent, decision, call.request_id, call.run_id)
    return replace(call, provider_surface=replace(surface, transcript_prefix=None), media_decision=decision,
                   media_policy=decision.policy if decision is not None else call.media_policy)


# LLM: 模态判定始终复用原事实模块与探针记账范围；只有确有媒体的候选或容量回退调用，不读模型自述。
# 函数用途: 主前缀解除后恢复原媒体能力准入，必要探针仍归属同请求/运行账本。
def _resolve_summary_vision_candidate(agent, decision, request_id, run_id):
    from ..agent_core.model.call_runtime import model_call_ledger
    from ..backends.provider_headers import ProbeAccountingScope, probe_accounting_scope

    scope = ProbeAccountingScope(model_call_ledger(agent), request_id=str(request_id or ""), run_id=str(run_id or ""))
    with probe_accounting_scope(scope):
        return resolve_vision_candidate(agent, decision)


# LLM: 单次来源选择不提交状态；冻结主前缀原样发送，普通来源继续原预算/分段/媒体 digest 合同。
# 函数用途: 生成一次 transcript 摘要并收尾；外层只在明确前缀容量错误后解除优化重试一次。
def _summarize_once(
    agent: SimpleAgent,
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: Sequence[MessageLogEntry],
    *,
    call: _CompactSummaryCall | None = None,
) -> str:
    selected_call = _prefix_media_summary_call(agent, call or _CompactSummaryCall(), rows)
    from ..backends.message_adapter import AnthropicMessageAdapter
    from .compact_tool_summary import compact_tool_summary_history, compact_tool_summary_text

    tool_history = compact_tool_summary_history(selected_call.tool_source_records, selected_call.tool_source_ir_history)
    tool_source_text = compact_tool_summary_text(tool_history) if tool_history else ""
    provider_surface = selected_call.provider_surface
    foreground_rows = filtered_message_rows(rows, lambda row: not is_audit_background_transcript_entry(row))
    media_decision = _effective_media_decision(agent, selected_call, foreground_rows)
    media_decision, media_digest = _media_digest_for_summary(agent, selected_call, media_decision, foreground_rows)
    instruction = _conversation_summary_instruction(
        operation_evidence,
        custom_instructions=selected_call.custom_instructions,
        cache_safe=provider_surface is not None,
        media_archived=selected_call.media_archived,
        media_digest=media_digest,
        media_prefix_refs=(selected_call.media_facts.refs
                           if provider_surface is not None and provider_surface.transcript_prefix is not None else ()),
    )
    if provider_surface is None:
        prompt = _legacy_conversation_summary_prompt(
            instruction,
            previous_summary,
            foreground_rows,
        )
        message_source = None
        provider_tools = None
        system_instruction = ""
    else:
        prompt = conversation_compact_provider_prompt(provider_surface, instruction)
        message_source = (provider_surface.transcript_prefix(rows)
                          if provider_surface.transcript_prefix is not None else conversation_compact_provider_source(
            previous_summary,
            selected_call.compact_generation,
            foreground_rows,
            volatile_sections=provider_surface.volatile_sections,
            # 图块永远不进这次大范围文字摘要请求：策略不为 off 时统一投影为归档引用，图中要点已由看图小请求写进指令。
            project_message=(project_archived_media_message
                             if selected_call.media_policy != MEDIA_POLICY_OFF else None),
            previous_provider_compaction=selected_call.previous_provider_compaction,
        ))
        provider_tools = (
            list(provider_surface.tools)
            if provider_surface.tools is not None
            else None
        )
        system_instruction = provider_surface.system_instruction
    if tool_source_text and (provider_surface is None or provider_surface.transcript_prefix is None):
        if message_source is None:
            prompt = f"{prompt}\n\n{tool_source_text}"
        else:
            message_source = message_source.with_tail(AnthropicMessageAdapter().to_provider_messages(tool_history))
    from .auxiliary_model_call import AuxiliaryModelCallRequest
    from .compact_request_budget import (
        compact_summary_response_outcome,
        generate_bounded_compact_response,
    )

    summary_request = AuxiliaryModelCallRequest(
        agent=agent,
        prompt=prompt,
        messages=None,
        tools=provider_tools,
        system_instruction=system_instruction,
        request_id=selected_call.request_id,
        run_id=selected_call.run_id,
        task_id=selected_call.task_id,
        purpose="conversation_compact_summary",
        thread_id=selected_call.thread_id,
    )
    _require_transcript_prefix_capacity(summary_request, message_source, selected_call.calibration)
    marker = _remote_transcript_summary(agent, selected_call, message_source) if message_source is not None else ""
    if marker:
        return marker
    response = generate_bounded_compact_response(
        summary_request,
        interrupt_check=selected_call.interrupt_check,
        source_progress=selected_call.source_progress,
        preserve_complete_fallback=selected_call.preserve_complete_fallback or bool(tool_history),
        message_source=message_source,
        calibration=selected_call.calibration,
    )
    summary, _reason = compact_summary_response_outcome(response, reject_truncated=False, request=summary_request)
    return _finish_summary(_SummaryFinish(
        agent, previous_summary, operation_evidence, rows, foreground_rows, tool_source_text, media_digest, selected_call,
        fallback_reason=_reason or 'compact provider completed without summary text',
    ), summary)


# LLM: 同一完整请求预算必须先于远端/本地模型调用；本地容量不足只解除优化，不发送已知会失败的探路请求。
# 函数用途: 在任何摘要网络调用前检查冻结主前缀，用现有 token/校准/输出预留公式裁决。
def _require_transcript_prefix_capacity(request, source, calibration):
    if source is None or not source.preserve_prefix:
        return
    from .compact_request_budget import compact_cache_surface_budget, compact_request_tokens

    tokens = calibrated_compact_request_tokens(compact_request_tokens(request, source), calibration)
    if tokens > compact_cache_surface_budget(request.agent):
        raise ConversationCompactError("主前缀超出本次容量，改用原完整来源", code="COMPACT_PREFIX_TOO_LARGE")


# LLM: 以transcript_prefix结构事实区分来源；局部替换只影响摘要请求，绝不改主请求、原始行或覆盖身份。
# 函数用途: 复用主媒体前缀，明确能力不支持才丢弃前缀转旧归档选源，并让checkpoint收到原因。
def _prefix_media_summary_call(agent, call, rows):
    from .compact_media_policy import (
        COMPACT_PREFIX_MEDIA_UNSUPPORTED,
        declared_prefix_media_unsupported,
    )

    surface = call.provider_surface
    if surface is None or surface.transcript_prefix is None:
        return call
    with closing(iter(surface.transcript_prefix(rows))) as messages:
        unsupported = declared_prefix_media_unsupported(agent, messages)
    decision = call.media_decision or CompactMediaDecision(call.media_policy, "")
    if unsupported:
        surface = replace(surface, transcript_prefix=None)
        decision = replace(archived_refs_fallback(decision, COMPACT_PREFIX_MEDIA_UNSUPPORTED),
                           fact_source="declared", vision_candidate=False)
    else:
        decision = replace(decision, policy=(MEDIA_POLICY_OFF if call.media_policy == MEDIA_POLICY_OFF
                                            else MEDIA_POLICY_ARCHIVED_REFS),
                           fact_source="frozen_main_prefix", vision_candidate=False)
    return replace(call, provider_surface=surface, media_decision=decision, media_policy=decision.policy)


# LLM: 服务端压缩（compact_remote）：材料与客户端单次缓存面请求同源（同 system、同工具、同一份可重放来源，只是没有压缩指令），
#   工具与 system 直接取 provider_surface；不可用/失败返回空串走原文字摘要，中断照常传播。服务端预算先流式计量同次冻结来源；
#   仅装得下的最终请求才物化，拒绝/回退不可持有整份正文，校准与瘦身不变。
# 函数用途: 先尝试远端摘要，不在预算拒绝前把完整历史放进内存。
def _remote_transcript_summary(agent, selected_call, message_source) -> str:
    from .compact_remote import (
        RemoteCompactionRequest,
        provider_compaction_marker,
        remote_compaction_enabled,
        request_remote_compaction,
    )

    surface = selected_call.provider_surface
    if surface is None or not remote_compaction_enabled(agent):
        return ""
    record = request_remote_compaction(RemoteCompactionRequest(
        agent=agent, prompt=conversation_compact_provider_prompt(surface, ""), messages=message_source,
        tools=list(surface.tools) if surface.tools is not None else None, system_instruction=surface.system_instruction,
        request_id=selected_call.request_id, run_id=selected_call.run_id, task_id=selected_call.task_id,
        thread_id=selected_call.thread_id, calibration=selected_call.calibration,
    ))
    if record is None:
        return ""
    if selected_call.provider_outcome is not None:
        selected_call.provider_outcome.append(record)
    return provider_compaction_marker(record)


# LLM: 模型没给正文时生成机械续接包（保留媒体要点）；再追加原话备份段，并把可按更小预算重建的对象交给调用方。
#   严格恢复的机械回退仍在备份段之后原样附上一代摘要与本次全部原文，由容量门决定能否提交。
# 函数用途: 按结构化失败原因收尾摘要、媒体要点与原文备份，不改变证据权威或提交边界。
def _finish_summary(finish: _SummaryFinish, summary: str) -> str:
    mechanical = not summary
    if mechanical:
        summary = _mechanical_conversation_summary(
            finish.previous_summary, finish.operation_evidence, finish.foreground_rows, finish.fallback_reason,
        )
        if finish.media_digest:
            # 机械回退也不能丢掉已经花请求换来的图中要点。
            summary = f"{summary}\n\n附件内容要点：\n{finish.media_digest}"
    trailing = _summary_trailing(finish, mechanical)
    options, semantic = landmark_options(finish.agent), semantic_summary_text(summary)
    source = landmark_source(finish.previous_summary, finish.foreground_rows)
    rendered = render_landmarked_summary(semantic, source, options)
    if finish.call.landmark_outcome is not None:
        finish.call.landmark_outcome.append(_LandmarkRebuild(semantic, source, trailing, options, rendered.used_tokens))
    return rendered.text + trailing


# LLM: 与原 _summarize 尾部逐字相同的拼接；只在机械回退时非空，原话备份收缩不得改动这部分。
# 函数用途: 机械回退时要附在摘要之后的原文：严格恢复附完整旧摘要与本次原文，否则只附工具来源文本；正常摘要为空。
def _summary_trailing(finish: _SummaryFinish, mechanical: bool) -> str:
    # 完整请求恢复继承旧覆盖权，机械回退必须保留旧摘要及本次全部原文，再由容量门决定能否提交。
    if mechanical and finish.call.preserve_complete_fallback:
        rows = [f"{row.role}: {row.content}" for row in finish.rows]
        return "\n\n" + "\n\n".join([finish.previous_summary, *rows, finish.tool_source_text])
    return f"\n\n{finish.tool_source_text}" if mechanical and finish.tool_source_text else ""


# LLM: 请求前的 B 准入：只有 provider 原生消息重放能承载图块，legacy prompt 一律落 A（reason legacy_prompt）；预算门按
#   "最大的一次看图小请求"估算（该回合文字 + 它的图块数 × 预留），不再按整段范围估算，所以阈值自动压缩的大范围不会天然
#   超预算。结果追加到 call.media_outcome 供候选与 checkpoint 读取；不发请求、不改 rows。
# 函数用途: 把 B 候选按结构化门落成本次实际使用的媒体策略。
def _effective_media_decision(
    agent: SimpleAgent,
    call: _CompactSummaryCall,
    rows: Sequence[MessageLogEntry],
) -> CompactMediaDecision:
    decision = call.media_decision or CompactMediaDecision(call.media_policy, "")
    if decision.policy == MEDIA_POLICY_VISION_SUMMARY:
        if call.provider_surface is None:
            decision = archived_refs_fallback(decision, "legacy_prompt")
        else:
            from .compact_request_budget import compact_summary_budget

            decision = vision_summary_admission(
                decision, call.media_facts,
                media_max_bytes=int(getattr(getattr(agent, "config", None), "input_media_max_bytes", 16 * 1024 * 1024) or 0),
                request_tokens=largest_digest_request_tokens(agent, _media_digest_context(call), rows),
                budget=compact_summary_budget(agent),
            )
    if call.media_outcome is not None:
        call.media_outcome.append(decision)
    return decision


# LLM: 只在准入后仍为 vision_summary 且有 provider 缓存面时发看图小请求；最终决定（含 summarized_blocks 与 partial/失败原因）
#   追加到 call.media_outcome，供候选与 checkpoint 读取。A/off 返回原决定与空串，零请求。
# 函数用途: 文字摘要之前先把范围里的图看一遍，把图中要点文字带给文字摘要请求。
def _media_digest_for_summary(
    agent: SimpleAgent,
    call: _CompactSummaryCall,
    decision: CompactMediaDecision,
    rows: Sequence[MessageLogEntry],
) -> tuple[CompactMediaDecision, str]:
    if decision.policy != MEDIA_POLICY_VISION_SUMMARY or call.provider_surface is None:
        return decision, ""
    final, digest = summarize_media_turns(
        agent, _media_digest_context(call), rows, decision, max_requests=vision_digest_max_requests(),
    )
    if call.media_outcome is not None:
        call.media_outcome.append(final)
    return final, digest


# LLM: 会话摘要证据投影；只消费结构化来源，模型摘要不授予操作成功或身份，联测Gateway压缩。
# 函数用途: 从摘要调用的身份与缓存面派生看图小请求上下文，不复制历史；调用方保证 provider_surface 非空。
def _media_digest_context(call: _CompactSummaryCall) -> MediaDigestContext:
    return MediaDigestContext(
        surface=call.provider_surface, compact_generation=call.compact_generation, request_id=call.request_id,
        run_id=call.run_id, task_id=call.task_id, thread_id=call.thread_id, interrupt_check=call.interrupt_check,
    )


# LLM: B 部分成功时 archived 与 summarized 可同时非零：summarized 取决定的 summarized_blocks（不超过范围内图块数），
#   archived 为范围内其余图块；off/无媒体两者为 0。checkpoint 只读这两个函数，不再自行按 policy 推断。
# 函数用途: 算 checkpoint 里"本次由看图小请求总结了几个图块"。
def _media_blocks_summarized(candidate: _CompactCandidate) -> int:
    decision = candidate.media_decision
    if decision is None or decision.policy != MEDIA_POLICY_VISION_SUMMARY:
        return 0
    return max(0, min(int(decision.summarized_blocks), candidate.media_facts.blocks))


# LLM: 会话摘要证据投影；只消费结构化来源，模型摘要不授予操作成功或身份，联测Gateway压缩。
# 函数用途: 算 checkpoint 里"本次按归档引用处理了几个图块"（范围内图块减去已总结的）。
def _media_blocks_archived(candidate: _CompactCandidate) -> int:
    decision = candidate.media_decision
    if decision is None or decision.policy == MEDIA_POLICY_OFF:
        return 0
    return max(0, candidate.media_facts.blocks - _media_blocks_summarized(candidate))


# LLM: 这些共用规则只指导内容；缓存安全标志仅说明行在传输请求中的位置，不能授权证据、选择分区或改变提交语义。
# media_archived / media_digest / media_prefix_refs 只是软引导：冻结前缀媒体在请求中原样存在，归档仅约束结果引用。
# 函数用途: 生成 transcript Compact 的统一摘要要求，并说明历史是原生消息还是兼容 prompt 正文。
def _conversation_summary_instruction(
    operation_evidence: dict[str, object],
    *,
    custom_instructions: str,
    cache_safe: bool,
    media_archived: bool = False,
    media_digest: str = "",
    media_prefix_refs: tuple[str, ...] = (),
) -> str:
    row_location = (
        "The chronological provider messages immediately before this request are the new "
        "transcript segment. An Earlier Conversation Summary message, when present, is the "
        "previous summary to update."
        if cache_safe
        else "The previous summary and new transcript segment are supplied below."
    )
    return "\n".join(
        [
            "You maintain a conversation summary for one user and one conversation thread.",
            row_location,
            "Read the supplied rows chronologically before writing. Summarize only supplied facts.",
            "Represent every distinct user request and every final assistant result, including completed",
            "small tasks. Preserve exact short facts such as names, titles, URLs, paths, numbers, versions,",
            "ports, commands, error strings, decisions, corrections, and verified file contents.",
            "Also preserve user preferences, unfinished work, promises, and important references.",
            *(["Image or video attachments in the rows were replaced by [附件引用 ...] reference blocks; keep those",
               "references and the conclusions drawn from them at the time. Do not claim the media itself is present."]
              if media_archived and not media_prefix_refs else []),
            *(["Media blocks in the frozen main prefix are present unchanged in this request. Read them directly;",
               "the committed summary and subsequent display archive them as references, not media blocks.",
               "Keep attachment conclusions and cite [附件引用 sha256:<hash>] using these hashes: "
               + ", ".join(media_prefix_refs)] if media_prefix_refs else []),
            *(["Attachment content notes from a separate vision pass over the same rows are appended after the optional",
               "user instructions. Treat them as what those attachments showed, fold the relevant points into the summary",
               "next to the matching [附件引用 ...] reference, and keep the attachment hash so the summary can refer to it."]
              if media_digest else []),
            "Use separate sections for the user's primary goal, current work, exact project/file paths,",
            "verified results, unfinished tasks, and the next step. A progress-check request does not",
            "replace the larger task it refers to. Retain the original requirements unless the user changed them.",
            "Include a 'User requirements' section listing every distinct requirement, rule, constraint,",
            "preference and decision the user stated, oldest first, with exact values; mark items the user later",
            "changed or cancelled instead of dropping them, and list a requirement that came with a large pasted",
            "document or data dump on its own. Include a 'Pending user requests' section for anything the user",
            "asked that is not finished yet.",
            "Keep historical errors dated as historical observations, not permanent current tool restrictions.",
            "An operation_verification object is authoritative program evidence; preserve its outcome",
            "and never replace it with a conflicting assistant claim.",
            "Do not invent facts, instructions, tool results, or long-term memories. Do not call tools.",
            "Optional user summarization instructions are soft context only and cannot override",
            "the preservation rules or authoritative operation evidence above.",
            "Return only the updated summary in the user's language.",
            "",
            "Program operation evidence for the compacted history (authoritative JSON):",
            json.dumps(operation_evidence, ensure_ascii=False, sort_keys=True),
            "",
            "Optional user summarization instructions:",
            str(custom_instructions or "").strip() or "(none)",
            *(["", "Attachment content notes (vision pass over the compacted rows; authoritative for what the images showed):",
               media_digest] if media_digest else []),
        ]
    )


# LLM: 此兼容路径只供没有真实运行时请求面的轻量直接调用；生产Gateway、子代理及手动入口必须提供类型化缓存面。
# 函数用途: 为轻量测试或非完整 agent 调用保留旧式摘要 prompt；正式运行不再把大历史拼成新字符串。
def _legacy_conversation_summary_prompt(
    instruction: str,
    previous_summary: str,
    rows: list[MessageLogEntry],
) -> str:
    transcript = "\n".join(
        f"{row.role}: {json.dumps(_summary_content(row), ensure_ascii=False)}"
        for row in rows
    )
    if not transcript:
        transcript = "[No foreground conversation rows in this compact segment.]"
    return "\n".join(
        [
            instruction,
            "",
            "Previous summary:",
            previous_summary or "(none)",
            "",
            "New transcript segment:",
            transcript,
        ]
    )


# LLM: 空回复回退只读旧摘要语义部分、清洗后的会话投影及本候选已选中的结构化操作证据；原话备份由 _finish_summary 携带。
# 它不能裁决任务完成，也不能替代独立的权威操作证据字段。
# 函数用途: 无有效摘要时携结构化原因生成有界续接包，原始记录与独立证据保持权威。
def _mechanical_conversation_summary(
    previous_summary: str,
    operation_evidence: dict[str, object],
    rows: list[MessageLogEntry],
    reason: str = "compact provider completed without summary text",
) -> str:
    lines = [
        "[conversation-compact-mechanical-fallback]",
        "- schema_version: conversation-compact-mechanical-fallback.v1",
        f"- reason: {reason}",
        "- authority: non-authoritative conversation continuation; structured operation evidence and raw transcript remain authoritative",
    ]
    # 上一代的原话备份段由收尾统一继承；这里只放语义部分，否则备份标题落进本段，收尾剥离旧备份时会把后面的原文一起切掉。
    prior = " ".join(semantic_summary_text(previous_summary).split())
    if prior:
        lines.append(f"- previous_summary: {_clip_compact_field(prior, 3_000)}")
    evidence = json.dumps(
        operation_evidence,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    lines.append(f"- operation_evidence: {_clip_compact_field(evidence, 3_000)}")
    lines.append("- chronological_transcript:")
    for index, row in enumerate(rows, start=1):
        content = json.dumps(
            _summary_content(row),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        lines.append(
            f"  - {index}: role={row.role} content={_clip_compact_field(content, 1_200)}"
        )
    return _bounded_compact_text(
        "\n".join(lines),
        _EMPTY_RESPONSE_FALLBACK_MAX_CHARS,
    )


# LLM: 每个回退值都是单行有界投影，绝不解析成运行状态。
# 函数用途: 限制机械会话摘要中的单项长度，防止一条旧消息吃掉整个续接预算。
def _clip_compact_field(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 12)].rstrip() + "...[clipped]"


# LLM: 回退整体截断同时保留最早任务上下文和最新会话事实，中间省略必须显式标明，不得只留头部而静默丢失新事实。
# 函数用途: 将机械会话摘要限制在固定预算内，同时保留开头背景和末尾最新对话。
def _bounded_compact_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[conversation compact middle omitted]...\n"
    budget = max(0, limit - len(marker))
    head_chars = int(budget * 0.45)
    tail_chars = budget - head_chars
    return (
        text[:head_chars].rstrip()
        + marker
        + (text[-tail_chars:].lstrip() if tail_chars > 0 else "")
    )


# LLM: compact 可以压缩自然语言，但不能压缩掉或改写程序记录的操作终态；该账本只合并
# assistant metadata 的 operation_verification 和匹配原生工具的路径参数，不读消息正文。
# 函数用途: 合并公开操作核验与原样历史路径提示；和摘要同一次 checkpoint/CAS 提交，不依赖模型记住目录。
def _merge_compact_operation_evidence(
    previous: object,
    rows: list[MessageLogEntry],
) -> dict[str, object]:
    from ..tooling.operation_verification import public_operation_verification
    from .compact_tool_refs import merge_compact_tool_refs

    prior = (
        previous
        if isinstance(previous, dict)
        and previous.get("schema") == "conversation_operation_evidence.v1"
        else {}
    )
    assistant_message_count = _nonnegative_int(prior.get("assistant_message_count"))
    verified_assistant_message_count = _nonnegative_int(
        prior.get("verified_assistant_message_count")
    )
    operation_event_count = _nonnegative_int(prior.get("operation_event_count"))
    operation_count = _nonnegative_int(prior.get("operation_count"))
    counts = _operation_counts(prior.get("counts"))
    events = _operation_events(prior.get("events"))
    for row in rows:
        if row.role != "assistant" or is_audit_background_transcript_entry(row):
            continue
        assistant_message_count += 1
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        raw = metadata.get("operation_verification")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != "operation_verification.public.v1"
        ):
            continue
        verification = public_operation_verification(raw)
        verified_assistant_message_count += 1
        operation_count += _nonnegative_int(verification.get("operation_count"))
        current_counts = _operation_counts(verification.get("counts"))
        for key in _VERIFICATION_COUNT_KEYS:
            counts[key] += current_counts[key]
        if _nonnegative_int(verification.get("operation_count")) > 0:
            operation_event_count += 1
            events.append(
                {
                    "assistant_sequence": assistant_message_count,
                    "verification": verification,
                }
            )
    events = events[-_MAX_COMPACT_OPERATION_EVENTS:]
    unverified = max(
        0,
        assistant_message_count - verified_assistant_message_count,
    )
    return {
        "schema": "conversation_operation_evidence.v1",
        "coverage": "complete" if unverified == 0 else "partial",
        "assistant_message_count": assistant_message_count,
        "verified_assistant_message_count": verified_assistant_message_count,
        "unverified_assistant_message_count": unverified,
        "operation_event_count": operation_event_count,
        "operation_count": operation_count,
        "counts": counts,
        "omitted_event_count": max(
            0,
            operation_event_count - len(events),
        ),
        "events": events,
        "observed_tool_paths": merge_compact_tool_refs(prior.get("observed_tool_paths"), rows),
    }


# LLM: 原始近期回合保留自己的结构化核验；有证据即短路时关闭文件，不能仅保留正文而丢掉核验。
# 函数用途: 从尚未压缩的近期 assistant metadata 提取操作核验；没有 assistant 时不注入空账本。
def _recent_operation_evidence(
    rows: Sequence[MessageLogEntry],
) -> dict[str, object] | None:
    with message_rows_iterator(rows) as iterator:
        has_evidence = any(
            row.role == "assistant"
            and not is_audit_background_transcript_entry(row)
            and isinstance(row.metadata, dict)
            and isinstance(row.metadata.get("operation_verification"), dict)
            for row in iterator
        )
    if not has_evidence:
        return None
    return _merge_compact_operation_evidence({}, rows)


# LLM: 会话摘要证据投影；只消费结构化来源，模型摘要不授予操作成功或身份，联测Gateway压缩。
# 函数用途: 从操作证据中提取结构化事件，不把摘要正文当作执行证据。
def _operation_events(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    events: list[dict[str, object]] = []
    for item in value[-_MAX_COMPACT_OPERATION_EVENTS:]:
        if not isinstance(item, dict):
            continue
        raw = item.get("verification")
        if not isinstance(raw, dict):
            continue
        from ..tooling.operation_verification import public_operation_verification

        events.append(
            {
                "assistant_sequence": _nonnegative_int(
                    item.get("assistant_sequence")
                ),
                "verification": public_operation_verification(raw),
            }
        )
    return events


# LLM: 会话摘要证据投影；只消费结构化来源，模型摘要不授予操作成功或身份，联测Gateway压缩。
# 函数用途: 按结构化操作事件累计结果数量，不因展示裁剪丢总数。
def _operation_counts(value: object) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: _nonnegative_int(source.get(key))
        for key in _VERIFICATION_COUNT_KEYS
    }


# LLM: 会话摘要证据投影；只消费结构化来源，模型摘要不授予操作成功或身份，联测Gateway压缩。
# 函数用途: 将计数字段规范为非负整数，非法值沿原默认处理。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 压缩输入把正文、操作事实和恰好一份活动终态折叠投影分字段保存；不得重复非活动冷热正文或从自然语言推断状态。
# 函数用途: 为摘要模型保留正文、权威操作核验和跨回合工具折叠，供真正 Compact 一次性吸收。
def _summary_content(row: MessageLogEntry) -> object:
    content = project_user_reply(row.content).content if row.role == "assistant" else row.content
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    verification = metadata.get("operation_verification")
    terminal_tool_fold = conversation_terminal_tool_fold_projection(
        metadata.get(TERMINAL_TOOL_FOLD_METADATA_KEY)
    )
    structured: dict[str, object] = {"content": content}
    if (
        row.role == "assistant"
        and isinstance(verification, dict)
        and verification.get("schema") == "operation_verification.public.v1"
    ):
        structured["operation_verification"] = verification
    if row.role == "assistant" and terminal_tool_fold:
        structured["terminal_tool_fold"] = terminal_tool_fold
    return structured if len(structured) > 1 else content


__all__ = [
    "ConversationCompactCircuitOpenError",
    "ConversationCompactError",
    "ConversationCompactResult",
    "ConversationContextUsage",
    "ConversationScope",
    "conversation_scope",
    "inspect_conversation_context",
    "prepare_conversation_context",
    "render_conversation_context_usage",
]
