# LLM: 后台种子与前台共用 conversation.history_projection 和 Compact 代次，不依赖 Gateway 执行器；读取失败必须阻止缺历史的模型调用。
# 模块用途: 按当前任务范围加载后台原生历史，保留完整工具往返，不推进调度、压缩或消息投递状态。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime_errors import runtime_error_report
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
from .models import ConversationThread, MessageLogEntry
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
# 函数用途: 取得本片可用的历史种子，或抛出 BackgroundHistoryUnavailableError。
def background_history_seed_or_raise(
    agent: object,
    store: ConversationStore | None,
    thread: ConversationThread | None,
    request: BackgroundContextRequest,
    *,
    proactive_delivery_available: bool | None = None,
) -> object | None:
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
    return result.seed


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


# LLM: 后台历史种子的结果必须区分三态，不能把"读不到"和"确实没有"混成同一个 None：
# - ready: 拿到权威行并投影成功（可能为空历史，那是合法空）；
# - unreadable: 历史读取/解析失败（load_errors 非空或抛错）→ 调用方必须按 typed 错误处理，
#   保留可恢复状态（唤醒不确认、可重试），**不得**退回有界摘要继续跑模型——那会把"历史读取失败"
#   伪装成"上下文骤降"；
# - disabled: 窄范围审计事件等按设计不使用会话历史。
# 调用方（background_execution.run_background_turn_with_compact）读 status 决定是否继续，load_errors 一路带出去。
# 类用途: 承载后台历史种子的三态结果与结构化读取错误。
@dataclass(frozen=True)
class BackgroundHistorySeedResult:
    status: str
    seed: object | None = None
    load_errors: tuple[dict[str, Any], ...] = ()
    detail: str = ""


# LLM: 后台工作片必须与前台共用同一份 canonical 历史投影与同一个 Compact 权威：
# Gateway 与后台从会话层共用任务范围行和 provider 消息投影，不能用有界运行摘要替代完整原生历史。同时：
#   ① 行选择必须复用既有结构化任务范围（detached named task 的创建锚点 + 精确 lineage），
#      不能直接吞全 thread 未压缩行（会把后来别的任务的消息带进 detached 工作）；
#   ② 读取失败给 typed 结果，绝不静默退回摘要；
#   ③ 只读：不在后台切片里另起一次压缩（压缩由本片 context_overflow 路径与前台 Compact 负责）。
# 函数用途: 为后台工作片构造与前台同源的会话历史种子（三态结果）。
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
        return BackgroundHistorySeedResult("disabled", detail="narrow audit event")
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
        from ..agent_core.runtime.context_compactor import runtime_compact_policy
        from .compact import _uncompacted_conversation_rows
        from .history_projection import conversation_history_rows
        from .models import ConversationHistorySeed
        from .native_history import provider_history_messages_from_rows

        rows = _uncompacted_conversation_rows(store, thread)
        # 权威历史 = 全部未压缩行；范围裁决用与 operational 摘要**同一份** decision
        # （来自本轮显式 task 身份 + 已加载 bundle），既不重读盘、也不按第几个 task 猜身份。
        scoped_rows = history_scope_rows(
            task_scope_decision(scope_state, scoped),
            rows,
        )
        budget = int(getattr(runtime_compact_policy(agent), "trigger_tokens", 0) or 0)
        # 只用历史投影两步（行选择 + provider 消息），不牵入 recent_artifacts 等与续接无关的投影。
        selected_rows = conversation_history_rows(
            agent,
            thread_id,
            "",
            load_errors,
            rows=tuple(scoped_rows),
            token_budget=budget,
        )
        history = tuple((row.role, row.content) for row in selected_rows)
        canonical_history = provider_history_messages_from_rows(selected_rows)
    except InterruptedError:
        raise
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="background_history.projection"))
        return BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail=f"history projection failed: {type(exc).__name__}",
        )
    if load_errors:
        return BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail="history projection reported load errors",
        )
    scoped_thread = scoped.get("thread") if isinstance(scoped.get("thread"), dict) else {}
    return BackgroundHistorySeedResult(
        "ready",
        seed=ConversationHistorySeed(
            # detached named task 的 summary/代次按既有投影口径（创建锚点之前的摘要才继承）。
            compact_summary=str(scoped_thread.get("summary") or ""),
            compact_generation=max(0, int(scoped_thread.get("compact_generation", 0) or 0)),
            messages=tuple(history),
            canonical_messages=tuple(canonical_history),
        ),
    )
