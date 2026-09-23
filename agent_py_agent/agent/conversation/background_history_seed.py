# LLM: 后台种子与前台共用 conversation.history_projection 和 Compact 代次，不依赖 Gateway 执行器；读取失败必须阻止缺历史的模型调用。
# 模块用途: 一次加载并冻结后台历史范围，纯投影原生历史；不推进调度、压缩或消息投递状态。
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from ..runtime_errors import runtime_error_report
from .background_compact_context import background_compact_application, compact_covered_message_ids
from .background_context import (
    BackgroundContextLoad,
    BackgroundContextRequest,
    TaskScopeDecision,
    detached_task_rows,
    is_narrow_audit_event,
    load_context_bundle,
    task_scope_decision,
    tool_policy_request,
)
from .background_tool_policy import BackgroundToolPolicyRequest
from .compact_projection import ConversationCompactSource
from .compact_summary_view import AppliedCompactContext
from .models import ConversationHistorySeed, ConversationThread, MessageLogEntry
from .store import ConversationStore


# LLM: 会话历史的"范围裁决"与"展示摘要"必须分开：
# - 权威历史 = store 里全部未压缩 canonical 行（长会话不能被展示索引截短）；
# - 范围裁决只对 **显式 detached named task** 生效，规则完全复用既有创建锚点 + 精确 lineage；
# - context_bundle 的 recent_limit 只是有界 operational 展示，绝不作为"哪些历史有资格进模型"的白名单。
# 关键：裁决输入必须来自**已经读成功的同一份 scope 事实**（scoped["tasks"]），不得再单独读一次
# task link——那次读失败会被当成"没有 detached task"而放行全部历史（fail-open），把读取失败洗成普通会话。
# 普通连续会话（无 task / 非 detached）完整继承未压缩历史。
# 函数用途: 用已加载的 scope 事实过滤权威未压缩行，返回可进入模型的历史行。
def history_scope_rows(
    decision: TaskScopeDecision,
    rows: list[MessageLogEntry],
) -> list[MessageLogEntry]:
    if not rows:
        return []
    # 只有"本轮 exact task 确实是 detached named"才应用锚点/lineage；
    # 普通轮、无 task 轮（含 bundle 里恰好存在旧 Goal）必须完整继承历史。
    if not decision.detached or not decision.exact_link:
        return list(rows)
    # 同一份算法：把权威行投影成 dict 交给既有 detached 行选择器，再按 message_id 映射回对象，
    # 避免 operational 摘要与 native seed 各维护一份锚点/lineage 规则而长期漂移。
    by_id = {str(getattr(row, "message_id", "") or ""): row for row in rows}
    payload = [row.to_dict() for row in rows]
    selected_ids = {
        str(row.get("message_id") or "")
        for row in detached_task_rows(payload, dict(decision.exact_link), set(decision.task_ids))
    }
    return [row for message_id, row in by_id.items() if message_id in selected_ids]


# LLM: 解析种子并把"历史读不到"升级成 typed 错误：读不到时不许退回有界摘要继续跑模型，
# 那会把"读取失败"伪装成"上下文骤降"，让模型在缺历史时作答。抛错 → 本片失败、唤醒不确认、可重试。
# 函数用途: 取得同次历史种子、摘要视图和压缩来源，或抛出 BackgroundHistoryUnavailableError。
def prepare_background_history_or_raise(
    agent: object,
    store: ConversationStore | None,
    thread: ConversationThread | None,
    request: BackgroundContextRequest,
    *,
    proactive_delivery_available: bool | None = None,
) -> BackgroundHistorySeedResult:
    result = background_conversation_history_seed(
        agent,
        store,
        thread,
        request,
        tool_policy_request(
            agent,
            request,
            proactive_delivery_available=proactive_delivery_available,
        ),
    )
    if result.status == "unreadable":
        raise BackgroundHistoryUnavailableError(
            "background conversation history is unreadable",
            load_errors=list(result.load_errors),
            detail=result.detail,
        )
    return result


# LLM: 后台历史不可读是可恢复的运行事实，不是"空历史"。抛这个 typed 错误让上层：
# ① 不确认唤醒（保留 pending，可重试）；② 把 load_errors 写进失败诊断；③ 绝不带缺失历史继续调用模型。
# 类用途: 表示后台工作片所需的会话历史读取/解析失败。
class BackgroundHistoryUnavailableError(RuntimeError):
    # LLM: 错误码与结构化 load_errors 必须可被上层读取，禁止只留一句自然语言。
    # 函数用途: 构造一个带 load_errors 与细节的后台历史不可用错误。
    def __init__(
        self,
        message: str,
        *,
        load_errors: list[dict[str, Any]] | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = "BACKGROUND_HISTORY_UNAVAILABLE"
        self.load_errors = list(load_errors or [])
        self.detail = str(detail or "")


# LLM: 范围只来自同次已成功加载的bundle；复制锚点/lineage和thread投影，不让后续任务变化或渲染重新扩大权限。
# 类用途: 固定一次后台历史的范围、实际摘要覆盖与预算，供原准备和后续候选纯投影共用。
@dataclass(frozen=True)
class BackgroundHistoryProjection:
    thread_id: str
    scope: TaskScopeDecision
    thread: dict[str, Any] = field(repr=False)
    token_budget: int
    compact_context: AppliedCompactContext | None = None
    covered_message_ids: frozenset[str] = frozenset()

    # LLM: frozen dataclass中的嵌套字典同样须隔离，不能借后来的任务link或线程修改改变已冻结范围。
    # 函数用途: 复制实际scope和摘要字段，不产生额外读盘或持久状态。
    def __post_init__(self):
        object.__setattr__(self, "scope", deepcopy(self.scope))
        object.__setattr__(self, "thread", deepcopy(self.thread))


# LLM: 显式rows是唯一历史输入，范围与摘要来自同次准备；scope_applied只用于原Compact已筛选候选，不能重用全局cursor。
# 函数用途: 从已读行生成后台原生历史种子，使用原筛选、预算和provider消息投影。
def project_background_history_seed(agent, prepared: BackgroundHistoryProjection, rows, *, scope_applied: bool = False) -> ConversationHistorySeed:
    from .history_projection import conversation_history_rows
    from .native_history import provider_history_messages_from_rows

    scoped = list(rows) if scope_applied else history_scope_rows(prepared.scope, list(rows))
    selected_rows = conversation_history_rows(
        agent, prepared.thread_id, "", [], rows=tuple(row for row in scoped if row.message_id not in prepared.covered_message_ids),
        token_budget=prepared.token_budget,
    )
    return ConversationHistorySeed(
        compact_summary=str(prepared.thread.get("summary") or ""),
        compact_generation=max(0, int(prepared.thread.get("compact_generation", 0) or 0)),
        messages=tuple((row.role, row.content) for row in selected_rows),
        canonical_messages=tuple(provider_history_messages_from_rows(selected_rows)),
    )


# LLM: 后台历史种子的结果必须区分三态，不能把"读不到"和"确实没有"混成同一个 None：
# - ready: 拿到权威行并投影成功（可能为空历史，那是合法空）；
# - unreadable: 历史读取/解析失败（load_errors 非空或抛错）→ 调用方必须按 typed 错误处理，
#   保留可恢复状态（唤醒不确认、可重试），**不得**退回有界摘要继续跑模型——那会把"历史读取失败"
#   伪装成"上下文骤降"；
# - disabled: 窄范围审计事件等按设计不使用会话历史。
# 调用方（background_execution.run_background_turn_with_compact）读 status 决定是否继续，load_errors 一路带出去。
# 类用途: 承载三态历史与实际摘要范围；disabled只允许当前活动轮摘要，unreadable不伪造可用范围。
@dataclass(frozen=True)
class BackgroundHistorySeedResult:
    status: str
    seed: object | None = None
    load_errors: tuple[dict[str, Any], ...] = ()
    detail: str = ""
    projection: BackgroundHistoryProjection | None = field(default=None, repr=False)
    compact_context: AppliedCompactContext | None = field(default=None, repr=False)
    compact_source: ConversationCompactSource | None = field(default=None, repr=False)
    context_bundle: dict[str, Any] | None = field(default=None, repr=False)


# LLM: 后台工作片必须与前台共用同一份 canonical 历史投影与同一个 Compact 权威：
# Gateway 与后台从会话层共用任务范围行和 provider 消息投影，不能用有界运行摘要替代完整原生历史。同时：
#   ① 行选择必须复用既有结构化任务范围（detached named task 的创建锚点 + 精确 lineage），
#      不能直接吞全 thread 未压缩行（会把后来别的任务的消息带进 detached 工作）；
#   ② 读取失败给 typed 结果，绝不静默退回摘要；
#   ③ 只读：不在后台切片里另起一次压缩（压缩由本片 context_overflow 路径与前台 Compact 负责）。
# 函数用途: 为后台片一次读取历史并冻结范围，返回同源种子及可重复纯投影的内部材料。
def background_conversation_history_seed(
    agent: object,
    store: ConversationStore | None,
    thread: ConversationThread | None,
    request: BackgroundContextRequest,
    policy_request: BackgroundToolPolicyRequest | None = None,
) -> BackgroundHistorySeedResult:
    if store is None or thread is None:
        return BackgroundHistorySeedResult("unreadable", detail="conversation store unavailable")
    thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        return BackgroundHistorySeedResult("unreadable", detail="thread identity unavailable")
    # 窄范围审计事件（finding/capacity）是"一条结构化事件"，不是会话续接：
    # 它们必须只看到事件事实与审计目标，不能把 owner 的旧聊天历史带进模型输入。
    if is_narrow_audit_event(getattr(request, "reason", "")):
        try:
            application = background_compact_application(agent, thread, request)
        except Exception as exc:
            return BackgroundHistorySeedResult(
                "unreadable", load_errors=(runtime_error_report(exc, context="background_history.compact_scope"),),
                detail=f"narrow compact scope failed: {type(exc).__name__}",
            )
        return BackgroundHistorySeedResult("disabled", detail="narrow audit event", compact_context=application)
    load_errors: list[dict[str, Any]] = []
    scope_state = BackgroundContextLoad(
        agent,
        store,
        thread,
        str(getattr(request, "task_id", "") or "").strip(),
        getattr(agent, "config", None),
        policy_request,
        load_errors,
    )
    try:
        scoped = load_context_bundle(scope_state)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="background_history.scope"))
        return BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail=f"context bundle failed: {type(exc).__name__}",
        )
    if load_errors:
        # 读取/解析错误必须上报，不得被"空历史"掩盖。
        return BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail="conversation context reported load errors",
        )
    try:
        decision = task_scope_decision(scope_state, scoped)
        application = background_compact_application(agent, thread, request, decision)
        loaded = _load_scoped_history(agent, store, thread, decision, application, dict(scoped.get("thread") or {}))
        return replace(loaded, context_bundle=deepcopy(scoped))
    except InterruptedError:
        raise
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="background_history.projection"))
        return BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail=f"history projection failed: {type(exc).__name__}",
        )


# LLM: 只刷新同一冻结范围的原检查点和消息；不得重读任务link扩大范围，narrow始终不加载会话历史。
# 函数用途: 溢出后用最新CAS代次准备同一任务的压缩来源，保留宿主原范围裁决。
def refresh_background_history(agent, store, thread, previous: BackgroundHistorySeedResult) -> BackgroundHistorySeedResult:
    from .compact_summary_view import resolve_compact_summary_view

    old = previous.compact_context
    if old is None or old.thread_id != thread.thread_id:
        raise BackgroundHistoryUnavailableError("background compact scope is unavailable")
    application = replace(old, view=resolve_compact_summary_view(agent, thread, old.scope))
    if old.scope.kind == "turn":
        return BackgroundHistorySeedResult("disabled", detail="narrow audit event", compact_context=application)
    if previous.projection is None:
        raise BackgroundHistoryUnavailableError("background history projection is unavailable")
    return _load_scoped_history(agent, store, thread, previous.projection.scope, application, previous.projection.thread)


# LLM: 全局cursor不代表局部摘要覆盖；先按原任务事实筛选完整行，再按实际采用的view精确替代，不修改原记录。
# 函数用途: 共用首次和溢出准备的历史读取、范围过滤和来源构造；坏原文不能当成空历史。
def _load_scoped_history(agent, store, thread, decision, application, scoped_thread) -> BackgroundHistorySeedResult:
    from ..agent_core.runtime.context_compactor import runtime_compact_policy
    from .compact import _recent_operation_evidence

    rows, errors = store.messages.recent_report(thread.thread_id, limit=0)
    if errors:
        raise BackgroundHistoryUnavailableError("canonical background history is unreadable", load_errors=errors)
    covered = compact_covered_message_ids(application.view, rows)
    projected_thread = dict(scoped_thread)
    projected_thread.update(summary=application.view.summary, compact_generation=thread.compact_generation,
                            compact_operation_evidence=deepcopy(application.view.operation_evidence))
    policy = runtime_compact_policy(agent)
    prepared = BackgroundHistoryProjection(
        thread_id=thread.thread_id, scope=decision, thread=projected_thread,
        token_budget=int(getattr(policy, "trigger_tokens", 0) or 0),
        compact_context=application, covered_message_ids=covered,
    )
    pending = tuple(row for row in history_scope_rows(decision, rows) if row.message_id not in covered)
    source = ConversationCompactSource(thread, pending, policy, dict(_recent_operation_evidence(pending) or {}),
                                       compact_context=application)
    return BackgroundHistorySeedResult("ready", seed=project_background_history_seed(agent, prepared, rows),
                                       projection=prepared, compact_context=application, compact_source=source)
