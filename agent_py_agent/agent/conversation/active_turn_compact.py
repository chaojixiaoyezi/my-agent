# LLM: 跨片工具压缩仍只写原 checkpoint/CAS；完整请求 projector 由宿主纯替换冻结材料，
# 容量未知或越界不得提交，也不能重建运行准备。完整归档始终是运行事实源。
# 模块用途: 共用工具来源分区与完整模型投影，将跨片工具压成正式摘要，验证完整请求后提交并返回获选材料。

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..common.cancellation import ToolCancelled
from ..memory_archive import estimate_tokens
from .authority import (
    AGENT_THREAD_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from .compact_checkpoint import committed_live_tool_compact_source_refs
from .compact_guard import (
    CompactInterruptCheck,
    ConversationCompactError,
    raise_if_compact_interrupted,
)
from .compact_progress import (
    COMPACT_AUTHORITY_CONVERSATION,
    COMPACT_SOURCE_ACTIVE_TURN,
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
)
from .compact_projection import ConversationCompactProjection
from .compact_summary_view import AppliedCompactContext
from .compact_tool_identity import compact_tool_ref_key, compact_tool_refs
from .live_tool_compact import (
    LiveToolCompactCommitRequest,
    commit_live_tool_compact,
)
from .models import ConversationThread

if TYPE_CHECKING:
    from .compact_provider_surface import ConversationCompactProviderSurface


# LLM: 结果区分持久提交和完整归档；request_projection 必须是已量且获选的原对象，不能重新准备。
# 类用途: 返回原 CAS 的线程、替代边界和真实容量，并保留随后发送应采用的同一候选材料。
@dataclass(frozen=True)
class ActiveTurnArchiveCompactResult:
    thread: ConversationThread
    compacted: bool = False
    source_call_ids: tuple[str, ...] = ()
    retained_call_ids: tuple[str, ...] = ()
    projected_tokens_before: int = 0
    projected_tokens_after: int = 0
    request_projection: ConversationCompactProjection | None = None


# LLM: 纯来源分区只含模型可见工具记录及精确四元引用；未知旧记录保留在retained，不能获摘要覆盖权。
# 类用途: 冻结一次工具压缩的被替代区和保留区，供活动归档与混合来源共用同一选择结果。
@dataclass(frozen=True)
class CarriedToolCompactSource:
    source_records: tuple[dict[str, object], ...]
    retained_records: tuple[dict[str, object], ...]
    source_tool_refs: tuple[dict[str, str], ...]
    retained_tool_refs: tuple[dict[str, str], ...]

    # LLM: 原archive及嵌套model_parameters以后可能改写；分区对象必须和入参完全隔离，且不新增持久权威。
    # 函数用途: 核对摘要素材与可覆盖refs完全一致并复制嵌套容器，防止未读工具获得覆盖权。
    def __post_init__(self) -> None:
        for name in ("source_records", "retained_records", "source_tool_refs", "retained_tool_refs"):
            object.__setattr__(self, name, tuple(deepcopy(getattr(self, name))))
        source_keys = [compact_tool_ref_key(item) for item in self.source_records]
        retained_keys = [compact_tool_ref_key(item) for item in self.retained_records]
        known_retained = [item for item, key in zip(self.retained_records, retained_keys) if key is not None]
        if (
            not self.source_records
            or any(not isinstance(item, dict) for item in (*self.source_records, *self.retained_records))
            or any(key is None for key in source_keys)
            or len(set(source_keys)) != len(source_keys)
            or len({key for key in retained_keys if key is not None}) != len(known_retained)
            or not set(source_keys).isdisjoint(key for key in retained_keys if key is not None)
            or self.source_tool_refs != compact_tool_refs(list(self.source_records))
            or self.retained_tool_refs != compact_tool_refs(known_retained)
        ):
            raise ConversationCompactError("工具压缩来源与覆盖引用不一致", code="COMPACT_TOOL_COVERAGE_UNKNOWN")


# LLM: 按原四元身份划区，未知原序保留；内部近期预算0明确不保留已知尾部，供强制完整恢复使用。
# 函数用途: 纯计算最近工具保留区和摘要来源，供各种Compact宿主同源投影与提交。
def partition_carried_tool_records(
    records: list[dict[str, object]] | tuple[dict[str, object], ...],
    *,
    recent_tail_tokens: int,
) -> CarriedToolCompactSource | None:
    if not isinstance(records, (list, tuple)) or any(not isinstance(item, dict) for item in records):
        raise TypeError("工具压缩来源必须是归档记录序列")
    if type(recent_tail_tokens) is not int or recent_tail_tokens < 0:
        raise ValueError("工具压缩近期预算无效")
    known = [item for item in records if compact_tool_ref_key(item) is not None]
    if not known:
        return None
    recent = _recent_records_within_budget(known, recent_tail_tokens) if recent_tail_tokens else []
    retained_keys = {compact_tool_ref_key(item) for item in recent}
    source_keys = {compact_tool_ref_key(item) for item in known if compact_tool_ref_key(item) not in retained_keys}
    if not source_keys:
        # 真实overflow时即使只有一条完整调用，也应摘要该调用并让原未知记录继续可见。
        source_keys = {compact_tool_ref_key(known[0])}
    source_records = tuple(item for item in records if compact_tool_ref_key(item) in source_keys)
    retained_records = tuple(item for item in records if compact_tool_ref_key(item) not in source_keys)
    return CarriedToolCompactSource(
        source_records=source_records,
        retained_records=retained_records,
        source_tool_refs=compact_tool_refs(list(source_records)),
        retained_tool_refs=compact_tool_refs([
            item for item in retained_records if compact_tool_ref_key(item) is not None
        ]),
    )


# LLM: projector 只按摘要、保留记录、预计提交代次替换原材料；provider_surface 沿原准备复用，
# 不授予重新准备、状态变更或扩范围权限。未接 projector 的宿主保留原局部计量行为。
# 类用途: 冻结本次身份、摘要视图和完整请求投影入口，供跨片压缩在同次准备内计量与提交。
@dataclass(frozen=True)
class ActiveTurnArchiveCompactRequest:
    task_attributes: object
    request_id: str
    attempt_id: str
    task_prompt: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = None
    interrupt_check: CompactInterruptCheck | None = None
    compact_context: AppliedCompactContext | None = None
    request_projector: Callable[
        [str, tuple[dict[str, object], ...], int], ConversationCompactProjection
    ] | None = None
    projected_tokens_before: int | None = None
    provider_surface: ConversationCompactProviderSurface | None = None


# LLM: This candidate freezes one exact checkpoint boundary before the summary model call. The
# full archive is intentionally absent because only the model-visible replacement belongs here.
# 类用途: 保存一次待提交压缩的线程、策略、替代区、保留区和进度计量。
@dataclass(frozen=True)
class _ActiveTurnArchiveCompactPlan:
    thread: ConversationThread
    binding: object
    policy: object
    visible_records: tuple[dict[str, object], ...]
    source_records: tuple[dict[str, object], ...]
    source_call_ids: tuple[str, ...]
    source_tool_refs: tuple[dict[str, str], ...]
    retained_records: tuple[dict[str, object], ...]
    retained_call_ids: tuple[str, ...]
    retained_tool_refs: tuple[dict[str, str], ...]
    projected_tokens_before: int
    progress: dict[str, object]


# LLM: Runtime authority keeps all records. Explicit context hides only its own view refs;
# callers without context retain the original thread-wide checkpoint lookup.
# 函数用途: 先核对摘要与当前线程，即使无工具也不能串线程；再保留未覆盖和旧未知记录。
def model_visible_active_turn_tool_calls(
    agent: object,
    task_attributes: object,
    records: list[dict[str, object]],
    *,
    compact_context: AppliedCompactContext | None = None,
) -> list[dict[str, object]]:
    values = [dict(item) for item in records if isinstance(item, dict)]
    if compact_context is not None:
        if not isinstance(compact_context, AppliedCompactContext):
            raise TypeError("Compact 应用上下文类型无效")
        _assert_context_thread(task_attributes, compact_context)
        if compact_context.view.source_tool_refs and not compact_context.view.summary.strip():
            raise OSError("Compact 来源已覆盖但适用摘要缺失")
    if not values or not _transcript_authoritative(task_attributes):
        return values
    if compact_context is not None:
        refs = compact_context.view.source_tool_refs
    else:
        thread = _load_authoritative_thread(agent, task_attributes)
        refs = committed_live_tool_compact_source_refs(agent, thread)
    hidden = {compact_tool_ref_key(ref) for ref in refs}
    hidden.discard(None)
    if not hidden:
        return values
    return [item for item in values if compact_tool_ref_key(item) not in hidden]


# LLM: 跨片恢复复用 native Compact 的原 checkpoint/CAS；显式 projector 负责实际完整请求，
# 本入口只提供精确替换边界，未知身份不能参与覆盖，也不能重新运行 Agent.run 或补历史种子。
# 函数用途: 把已溢出的跨工作片工具历史压成正式摘要，完整候选通过计量后才推进唯一 Compact。
def compact_carried_active_turn_archive(
    agent: object,
    store: object,
    thread: ConversationThread,
    records: list[dict[str, object]],
    request: ActiveTurnArchiveCompactRequest,
) -> ActiveTurnArchiveCompactResult:
    if not _transcript_authoritative(request.task_attributes):
        return ActiveTurnArchiveCompactResult(thread=thread)
    visible = model_visible_active_turn_tool_calls(
        agent,
        request.task_attributes,
        records,
        compact_context=request.compact_context,
    )
    if not any(compact_tool_ref_key(item) is not None for item in visible):
        return ActiveTurnArchiveCompactResult(thread=thread)

    from ..agent_core.runtime.context_compactor import runtime_compact_policy
    from .live_tool_compact import resolve_live_tool_compact_binding

    policy = runtime_compact_policy(
        agent,
        save=False,
        task_attributes=request.task_attributes,
    )
    binding = resolve_live_tool_compact_binding(
        agent,
        task_attributes=request.task_attributes,
        policy=policy,
        compact_context=request.compact_context,
    )
    if binding is None:
        return ActiveTurnArchiveCompactResult(thread=thread)
    if binding.thread.thread_id != thread.thread_id or binding.store is not store:
        raise OSError("active-turn compact binding does not match the caller thread")
    source = partition_carried_tool_records(
        visible, recent_tail_tokens=0 if request.request_projector is not None else policy.recent_tail_tokens,
    )
    if source is None:
        return ActiveTurnArchiveCompactResult(thread=thread)
    plan = _build_active_turn_compact_plan(
        binding, policy, visible, source, projected_tokens_before=request.projected_tokens_before,
    )
    return _execute_active_turn_compact(agent, plan, request)


# LLM: 来源边界在摘要调用前冻结，未知身份保留原顺序且不授覆盖权；宿主前计量坏值不能退回局部估算。
# 函数用途: 按统一近期预算冻结准确来源与全部未覆盖行，记录宿主已准备请求的真实前计量。
def _build_active_turn_compact_plan(
    binding: object,
    policy: object,
    visible: list[dict[str, object]],
    source: CarriedToolCompactSource,
    *,
    projected_tokens_before: int | None = None,
) -> _ActiveTurnArchiveCompactPlan:
    source_refs = source.source_tool_refs
    retained_refs = source.retained_tool_refs
    source_ids = tuple(ref["call_id"] for ref in source_refs)
    retained_ids = tuple(ref["call_id"] for ref in retained_refs)
    thread = binding.thread
    generation = max(0, int(thread.compact_generation or 0)) + 1
    before_tokens = projected_tokens_before
    if before_tokens is None:
        before_tokens = estimate_tokens({"active_turn_tool_calls": visible})
    elif type(before_tokens) is not int or before_tokens < 0:
        raise ConversationCompactError("完整恢复请求前计量不可用", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    return _ActiveTurnArchiveCompactPlan(
        thread=thread,
        binding=binding,
        policy=policy,
        visible_records=tuple(deepcopy(visible)),
        source_records=source.source_records,
        source_call_ids=source_ids,
        source_tool_refs=source_refs,
        retained_records=source.retained_records,
        retained_call_ids=retained_ids,
        retained_tool_refs=retained_refs,
        projected_tokens_before=before_tokens,
        progress={
            "generation": generation,
            "operation_id": f"active-turn-archive:{uuid.uuid4().hex}",
            "source_kind": COMPACT_SOURCE_ACTIVE_TURN,
            "commit_authority": COMPACT_AUTHORITY_CONVERSATION,
            "before_tokens": before_tokens,
            "trigger_tokens": max(0, int(policy.trigger_tokens or 0)),
            "context_window_tokens": max(0, int(policy.context_window_tokens or 0)),
            "source_messages": len(source_ids) * 2,
        },
    )


# LLM: 摘要后、checkpoint 前只投影一次完整请求；失败沿原熔断，停止中性撤销，CAS 成功后原样返回投影。
# 函数用途: 可中断地生成、计量并提交跨片摘要，防止局部小估算让实际仍过大的请求取得覆盖权。
def _execute_active_turn_compact(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
) -> ActiveTurnArchiveCompactResult:
    from .compact_guard import compact_exception_code
    from .live_tool_compact import record_live_tool_compact_failure

    callback = request.progress_callback
    progress = plan.progress
    raise_if_compact_interrupted(request.interrupt_check)
    _emit_progress(callback, progress, phase="started", stage="preparing", percent=5)
    _emit_progress(callback, progress, phase="progress", stage="summarizing", percent=20)
    try:
        replacement = _active_turn_replacement_summary(agent, plan, request)
        raise_if_compact_interrupted(request.interrupt_check)
        projection = _project_active_turn_request(agent, plan, request, replacement)
        after_tokens = projection.projected_tokens if projection is not None else estimate_tokens(
            {
                "compact_summary": replacement,
                "retained_active_turn_tool_calls": plan.retained_records,
            }
        )
        _emit_progress(
            callback,
            progress,
            phase="progress",
            stage="measuring",
            percent=65,
            after_tokens=after_tokens,
        )
        _emit_progress(
            callback,
            progress,
            phase="progress",
            stage="checkpointing",
            percent=82,
            after_tokens=after_tokens,
        )
        updated = _commit_active_turn_compact(agent, plan, request, replacement, after_tokens)
    except (InterruptedError, ToolCancelled):
        _emit_progress(
            callback,
            progress,
            phase="superseded",
            stage="candidate_discarded",
            percent=0,
        )
        raise
    except Exception as exc:
        # A transport may surface its own exception after the stop callback closes it. Recheck
        # typed interruption before classifying that close as a Compact/provider failure.
        try:
            raise_if_compact_interrupted(request.interrupt_check)
        except (InterruptedError, ToolCancelled):
            _emit_progress(
                callback,
                progress,
                phase="superseded",
                stage="candidate_discarded",
                percent=0,
            )
            raise
        record_live_tool_compact_failure(plan.binding, exc)
        _emit_progress(
            callback,
            progress,
            phase="failed",
            stage="failed",
            percent=0,
            error_code=compact_exception_code(exc),
        )
        raise
    _emit_progress(
        callback,
        {**progress, "generation": max(1, int(updated.compact_generation or 1))},
        phase="completed",
        stage="completed",
        percent=100,
        after_tokens=after_tokens,
    )
    return ActiveTurnArchiveCompactResult(
        thread=updated,
        compacted=True,
        source_call_ids=plan.source_call_ids,
        retained_call_ids=plan.retained_call_ids,
        projected_tokens_before=plan.projected_tokens_before,
        projected_tokens_after=after_tokens,
        request_projection=projection,
    )


# LLM: 宿主返回原完整候选，类型与计量未知必须失败；接受门复用 transcript 的阈值和已知输出预留。
# 函数用途: 在写任何检查点前校验完整请求，保留恰好获选的材料对象给同次发送。
def _project_active_turn_request(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
    replacement: str,
) -> ConversationCompactProjection | None:
    if request.request_projector is None:
        return None
    from .compact import _compact_request_input_ceiling

    projection = request.request_projector(
        replacement, plan.retained_records, max(0, int(plan.thread.compact_generation or 0)) + 1,
    )
    raise_if_compact_interrupted(request.interrupt_check)
    if (not isinstance(projection, ConversationCompactProjection)
            or type(projection.projected_tokens) is not int
            or projection.projected_tokens < 0 or projection.material is None):
        raise ConversationCompactError("完整恢复请求投影不可用", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    if projection.projected_tokens >= _compact_request_input_ceiling(agent, plan.policy):
        raise ConversationCompactError(
            "完整恢复请求仍超出输入阈值或已知输出预留", code="COMPACT_CANDIDATE_TOO_LARGE",
        )
    return projection


# LLM: 摘要读取全部选中工具的原逐条模型投影，不使用截短handoff；scope/base与provider面保持，过长沿原分段器。
# 函数用途: 复用已冻结供应商缓存面与停止信号，生成可独立替代前摘要的跨片交接。
def _active_turn_replacement_summary(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
) -> str:
    from ..backends.tool_ir import RuntimeFactsTurn
    from ..memory_archive.compact_semantic_summary import (
        LiveToolHistorySummaryRequest,
        semantic_summary_config,
        summarize_live_tool_history,
    )
    from .compact_provider_surface import (
        conversation_compact_provider_messages,
        conversation_compact_provider_prompt,
    )
    from .compact_tool_summary import carried_compact_source_text

    config = semantic_summary_config(agent)
    if not config.enabled:
        raise ValueError("active-turn carried compact semantic summary is disabled")
    scope_id = str(getattr(plan.thread, "workspace_task_id", "") or "")
    source_text = carried_compact_source_text(plan.source_records)
    context = request.compact_context
    previous_summary = str(context.view.summary if context is not None else plan.thread.summary or "")
    surface = request.provider_surface
    provider_history = tuple(conversation_compact_provider_messages(
        previous_summary, context.view.generation if context is not None else plan.thread.compact_generation,
        (), volatile_sections=surface.volatile_sections,
    )) if surface is not None else ()
    replacement = summarize_live_tool_history(
        LiveToolHistorySummaryRequest(
            history=[RuntimeFactsTurn(source_text, source="compact_tool_source")],
            backend=getattr(agent, "backend", None),
            agent=agent,
            request_id=str(request.request_id or ""),
            run_id=scope_id,
            task_id=scope_id,
            task_prompt=str(request.task_prompt or "继续当前任务。"),
            previous_summary=previous_summary,
            max_output_chars=config.max_input_chars,
            provider_prompt=conversation_compact_provider_prompt(surface, "") if surface is not None else "",
            provider_history_messages=provider_history,
            tools=surface.tools or () if surface is not None else (),
            system_instruction=surface.system_instruction if surface is not None else "",
            interrupt_check=request.interrupt_check,
        )
    )
    if not replacement:
        raise ValueError("active-turn carried compact summary is empty")
    return replacement


# LLM: This helper forwards the frozen scope and summary base alongside the same interrupt check;
# progress at 92% remains between checkpoint append and CAS.
# 函数用途: 用冻结范围与摘要基础提交 active-turn Compact，并在恢复点后更新进度。
def _commit_active_turn_compact(
    agent: object,
    plan: _ActiveTurnArchiveCompactPlan,
    request: ActiveTurnArchiveCompactRequest,
    replacement: str,
    after_tokens: int,
) -> ConversationThread:
    return commit_live_tool_compact(
        agent,
        plan.binding,
        LiveToolCompactCommitRequest(
            summary=replacement,
            source_tool_call_ids=plan.source_call_ids,
            source_tool_refs=plan.source_tool_refs,
            retained_tool_refs=plan.retained_tool_refs,
            retained_tool_call_ids=plan.retained_call_ids,
            projected_tokens_before=plan.projected_tokens_before,
            projected_tokens_after=after_tokens,
            policy=plan.policy,
            request_id=str(request.request_id or ""),
            attempt_id=str(request.attempt_id or request.request_id or ""),
            summary_base_checkpoint_id=(
                request.compact_context.view.checkpoint_id
                if request.compact_context is not None else None
            ),
            forced=True,
            interrupt_check=request.interrupt_check,
            after_checkpoint=lambda: _emit_progress(
                request.progress_callback,
                plan.progress,
                phase="progress",
                stage="committing",
                percent=92,
                after_tokens=after_tokens,
            ),
        ),
    )


# LLM: The newest whole records are retained up to the shared recent-tail token budget. At least
# one older record remains eligible for replacement when possible; no record body is clipped.
# 函数用途: 从尾部选择仍直接给模型看的近期完整调用，超预算的旧调用交给摘要。
def _recent_records_within_budget(
    known_records: list[dict[str, object]],
    token_budget: int,
) -> list[dict[str, object]]:
    if len(known_records) <= 1:
        return []
    budget = max(1, int(token_budget or 0))
    selected: list[dict[str, object]] = []
    used = 0
    for item in reversed(known_records[1:]):
        cost = max(1, estimate_tokens(item))
        if selected and used + cost > budget:
            break
        if not selected and cost > budget:
            break
        selected.append(item)
        used += cost
    selected.reverse()
    return selected


# LLM: The exact agent thread takes precedence over its parent conversation thread. This mirrors
# live-tool binding and prevents a delegated Compact from advancing the parent's generation.
# 函数用途: 按结构化属性加载本代理自己的唯一会话线程，缺失或损坏时停止恢复。
def _load_authoritative_thread(agent: object, task_attributes: object) -> ConversationThread:
    attrs = task_attributes if isinstance(task_attributes, Mapping) else {}
    thread_id = str(
        attrs.get(AGENT_THREAD_ID_ATTR) or attrs.get("conversation_thread_id") or ""
    ).strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        raise OSError("authoritative active turn has no conversation thread store")
    thread, load_error = store.threads.load_report(thread_id)
    if load_error is not None or thread is None:
        raise OSError("authoritative active turn conversation thread is unavailable")
    return thread


# LLM: A transient applied view may hide calls only in the exact thread named by the structured
# task attributes; do not load a newer thread view or fall back to a parent conversation id.
# 函数用途: 核对临时摘要视图属于当前代理线程，防止错线程来源被隐藏。
def _assert_context_thread(task_attributes: object, context: AppliedCompactContext) -> None:
    attrs = task_attributes if isinstance(task_attributes, Mapping) else {}
    thread_id = str(attrs.get(AGENT_THREAD_ID_ATTR) or attrs.get("conversation_thread_id") or "").strip()
    if thread_id != context.thread_id:
        raise OSError("Compact 应用视图与当前线程不匹配")


# LLM: 进度只是展示投影，普通回调错误不影响提交；显式typed取消必须透传，不能被UI容错吞掉。
# 函数用途: 发出同一压缩操作的阶段与百分比，保留真正停止信号，忽略界面断连错误。
def _emit_progress(
    callback: Callable[[dict[str, object]], object] | None,
    base: dict[str, object],
    *,
    phase: str,
    stage: str,
    percent: int,
    after_tokens: int = 0,
    error_code: str = "",
) -> None:
    if callback is None:
        return
    payload = {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        **base,
        "phase": str(phase),
        "stage": str(stage),
        "percent": min(100, max(0, int(percent or 0))),
        "after_tokens": max(0, int(after_tokens or 0)),
        "error_code": str(error_code or ""),
    }
    try:
        callback(payload)
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        return


# LLM: This helper accepts only the typed transcript-authority flag; user text cannot enable writes.
# 函数用途: 判断本轮是否允许把恢复压缩提交到唯一会话账本。
def _transcript_authoritative(attributes: object) -> bool:
    return bool(
        isinstance(attributes, Mapping)
        and attributes.get(CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR) is True
    )


__all__ = [
    "ActiveTurnArchiveCompactRequest",
    "ActiveTurnArchiveCompactResult",
    "CarriedToolCompactSource",
    "compact_carried_active_turn_archive",
    "model_visible_active_turn_tool_calls",
    "partition_carried_tool_records",
]
